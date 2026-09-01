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
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

$createdNew = $false
$watchdogMutex = [System.Threading.Mutex]::new($true, "Local\TikuAgentDemo8888Watchdog", [ref]$createdNew)
if (-not $createdNew) {
    exit 0
}

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
        Wait-Process -Id $processId -Timeout 5 -ErrorAction SilentlyContinue
    }
}

function Start-Agent {
    $logStamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $agentOutLog = Join-Path $LogDir "tiku_8888.$logStamp.out.log"
    $agentErrLog = Join-Path $LogDir "tiku_8888.$logStamp.err.log"
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
        -RedirectStandardOutput $agentOutLog `
        -RedirectStandardError $agentErrLog `
        -WindowStyle Hidden `
        -PassThru
    Set-Content -LiteralPath $AgentPidFile -Value $process.Id -Encoding ASCII
    Write-Status "Started 8888 demo agent: PID $($process.Id) stdout=$([IO.Path]::GetFileName($agentOutLog)) stderr=$([IO.Path]::GetFileName($agentErrLog))"
    return $process
}

try {
    Set-Content -LiteralPath $WatchdogPidFile -Value $PID -Encoding ASCII
    Write-Status "Watchdog started. Project=$ProjectDir Port=$Port RuntimeDir=$RuntimeDir"
    $consecutiveFailures = 0
    while ($true) {
        try {
            if (Test-Health) {
                $consecutiveFailures = 0
            } else {
                $consecutiveFailures += 1
                $listeners = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
                if ($listeners.Count -eq 0 -or $consecutiveFailures -ge 2) {
                    Write-Status "Health check failed ($consecutiveFailures); restarting 8888 demo agent."
                    Stop-PortProcess
                    Start-Sleep -Seconds 2
                    Start-Agent | Out-Null
                    Start-Sleep -Seconds 4
                    if (Test-Health) {
                        Write-Status "Health check passed."
                        $consecutiveFailures = 0
                    } else {
                        Write-Status "Health check still failing after restart."
                    }
                }
            }
        } catch {
            Write-Status "Watchdog cycle failed: type=$($_.Exception.GetType().Name) hresult=$($_.Exception.HResult) line=$($_.InvocationInfo.ScriptLineNumber)"
        }
        Start-Sleep -Seconds 10
    }
} finally {
    if (Test-Path -LiteralPath $WatchdogPidFile) {
        Remove-Item -LiteralPath $WatchdogPidFile -Force -ErrorAction SilentlyContinue
    }
    if ($watchdogMutex) {
        $watchdogMutex.ReleaseMutex()
        $watchdogMutex.Dispose()
    }
}
