param(
    [int]$Port = 8888,
    [string]$RuntimeDir,
    [string]$PythonExe = "python"
)

$ErrorActionPreference = "Stop"
$ProjectDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if (-not $RuntimeDir) {
    $RuntimeDir = Join-Path $ProjectDir ".tmp_tiku_agent_demo_8888"
} elseif (-not [System.IO.Path]::IsPathRooted($RuntimeDir)) {
    $RuntimeDir = Join-Path $ProjectDir $RuntimeDir
}

$LogDir = Join-Path $RuntimeDir "service_logs"
$StatusFile = Join-Path $LogDir "watchdog_8888.status"
$WatchdogPidFile = Join-Path $LogDir "watchdog_8888.pid"
$AgentPidFile = Join-Path $LogDir "tiku_8888.pid"
$AgentOutLog = Join-Path $LogDir "tiku_8888.out.log"
$AgentErrLog = Join-Path $LogDir "tiku_8888.err.log"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

function Write-Status {
    param([string]$Message)
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $Message"
    Add-Content -LiteralPath $StatusFile -Value $line -Encoding UTF8
}

function Test-Health {
    try {
        $response = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 3
        return ($response.ok -eq $true) -or ($response.status -eq "ok")
    } catch {
        return $false
    }
}

function Stop-PortProcess {
    $processIds = @(Get-NetTCPConnection -LocalPort $Port -ErrorAction SilentlyContinue |
        Where-Object { $_.State -eq "Listen" } |
        Select-Object -ExpandProperty OwningProcess -Unique)
    foreach ($processId in ($processIds | Where-Object { $_ -and $_ -ne 0 })) {
        Write-Status "Stopping process on port ${Port}: PID $processId"
        Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
    }
}

function Start-Agent {
    $arguments = @(
        "-B",
        "scripts\run_tiku_agent_8888.py",
        "--host", "127.0.0.1",
        "--port", "$Port",
        "--runtime-dir", "$RuntimeDir"
    )
    $process = Start-Process $PythonExe `
        -ArgumentList $arguments `
        -WorkingDirectory $ProjectDir `
        -RedirectStandardOutput $AgentOutLog `
        -RedirectStandardError $AgentErrLog `
        -WindowStyle Hidden `
        -PassThru
    Set-Content -LiteralPath $AgentPidFile -Value $process.Id -Encoding ASCII
    Write-Status "Started 8888 demo agent: PID $($process.Id)"
    return $process
}

Set-Content -LiteralPath $WatchdogPidFile -Value $PID -Encoding ASCII
Set-Content -LiteralPath $StatusFile -Value "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') Watchdog started. Project=$ProjectDir Port=$Port RuntimeDir=$RuntimeDir" -Encoding UTF8
foreach ($path in @($AgentOutLog, $AgentErrLog)) {
    if (-not (Test-Path -LiteralPath $path)) {
        New-Item -ItemType File -Path $path -Force | Out-Null
    }
}

$agentProcess = $null
try {
    while ($true) {
        if (-not $agentProcess -or $agentProcess.HasExited -or -not (Test-Health)) {
            if ($agentProcess -and -not $agentProcess.HasExited) {
                Stop-Process -Id $agentProcess.Id -Force -ErrorAction SilentlyContinue
            }
            Write-Status "Health check failed; restarting 8888 demo agent."
            Stop-PortProcess
            Start-Sleep -Seconds 2
            $agentProcess = Start-Agent
            Start-Sleep -Seconds 4
            if (Test-Health) {
                Write-Status "Health check passed."
            }
        }
        Start-Sleep -Seconds 20
    }
} finally {
    if (Test-Path -LiteralPath $WatchdogPidFile) {
        Remove-Item -LiteralPath $WatchdogPidFile -Force -ErrorAction SilentlyContinue
    }
}
