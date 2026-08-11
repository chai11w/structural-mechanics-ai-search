param(
    [int]$Port = 8795,
    [string]$AdminRuntime,
    [string]$SourceRuntime,
    [string]$PythonExe = "python"
)

$ErrorActionPreference = "Stop"

$ProjectDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if (-not $AdminRuntime) {
    $AdminRuntime = Join-Path $ProjectDir ".tmp_tiku_admin_8795"
} elseif (-not [System.IO.Path]::IsPathRooted($AdminRuntime)) {
    $AdminRuntime = Join-Path $ProjectDir $AdminRuntime
}
if (-not $SourceRuntime) {
    $SourceRuntime = Join-Path $ProjectDir ".tmp_tiku_agent_v2_prod_8790"
} elseif (-not [System.IO.Path]::IsPathRooted($SourceRuntime)) {
    $SourceRuntime = Join-Path $ProjectDir $SourceRuntime
}

$LogDir = Join-Path $AdminRuntime "service_logs"
$StatusFile = Join-Path $LogDir "watchdog_8795.status"
$PidFile = Join-Path $LogDir "tiku_admin_8795.pid"
$OutLog = Join-Path $LogDir "tiku_admin_8795.out.log"
$ErrLog = Join-Path $LogDir "tiku_admin_8795.err.log"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

function Write-Status {
    param([string]$Message)
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $Message"
    Add-Content -LiteralPath $StatusFile -Value $line -Encoding UTF8
    Write-Host $line
}

function Test-Health {
    try {
        $response = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 3
        return $response.status -eq "ok"
    } catch {
        return $false
    }
}

function Start-Admin {
    $arguments = @(
        "-B",
        "scripts\run_tiku_admin.py",
        "--host", "127.0.0.1",
        "--port", "$Port",
        "--admin-runtime", "$AdminRuntime",
        "--source-runtime", "$SourceRuntime"
    )
    $process = Start-Process $PythonExe `
        -ArgumentList $arguments `
        -WorkingDirectory $ProjectDir `
        -RedirectStandardOutput $OutLog `
        -RedirectStandardError $ErrLog `
        -WindowStyle Hidden `
        -PassThru
    Set-Content -LiteralPath $PidFile -Value $process.Id -Encoding ASCII
    Write-Status "Started 8795 administration console: PID $($process.Id)"
    return $process
}

Set-Content -LiteralPath $StatusFile -Value "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') Watchdog started. Project=$ProjectDir Port=$Port AdminRuntime=$AdminRuntime SourceRuntime=$SourceRuntime" -Encoding UTF8
foreach ($path in @($OutLog, $ErrLog)) {
    if (-not (Test-Path -LiteralPath $path)) {
        New-Item -ItemType File -Path $path -Force | Out-Null
    }
}

$adminProcess = $null
while ($true) {
    if (-not (Test-Health)) {
        if ($adminProcess -and -not $adminProcess.HasExited) {
            Write-Status "Health check failed; stopping tracked 8795 process PID $($adminProcess.Id)."
            Stop-Process -Id $adminProcess.Id -Force -ErrorAction Stop
        }
        Write-Status "Health check failed; starting 8795 administration console."
        $adminProcess = Start-Admin
        Start-Sleep -Seconds 4
        if (Test-Health) {
            Write-Status "Health check passed."
        }
    }
    Start-Sleep -Seconds 20
}
