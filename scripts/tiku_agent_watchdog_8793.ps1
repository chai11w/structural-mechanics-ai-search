param(
    [int]$Port = 8793,
    [string]$DeployRoot
)

$ErrorActionPreference = "Stop"

$ProjectDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if (-not $DeployRoot) {
    $DeployRoot = Join-Path $ProjectDir ".tmp_review_tiku_prod_8793"
} elseif (-not [System.IO.Path]::IsPathRooted($DeployRoot)) {
    $DeployRoot = Join-Path $ProjectDir $DeployRoot
}
$ServiceLogDir = Join-Path $DeployRoot "service_logs"
$StatusFile = Join-Path $ServiceLogDir "watchdog_8793.status"
$BotPidFile = Join-Path $ServiceLogDir "tiku_8793.pid"
$BotOutLog = Join-Path $ServiceLogDir "tiku_8793.out.log"
$BotErrLog = Join-Path $ServiceLogDir "tiku_8793.err.log"
New-Item -ItemType Directory -Force -Path $ServiceLogDir | Out-Null

# Environment consumed by app\run_mainline_observed_web.py (mirrors start_review_tiku_8793.ps1).
$env:REVIEW_TIKU_RUNTIME_ROOT = Join-Path $DeployRoot "runtime\mainline_web"
$env:REVIEW_TIKU_DATA_ROOT = Join-Path $DeployRoot "data\mainline_observed"
$env:REVIEW_TIKU_RUNTIME_NAMESPACE = "review-8793-prod"

function Write-Status {
    param([string]$Message)
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $Message"
    Add-Content -LiteralPath $StatusFile -Value $line -Encoding UTF8
    Write-Host $line
}

function Test-Health {
    try {
        $response = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/observation/source" -TimeoutSec 3
        return $response.runtime_namespace -eq "review-8793-prod"
    } catch {
        return $false
    }
}

function Stop-PortProcess {
    $processIds = @()
    $processIds += Get-NetTCPConnection -LocalPort $Port -ErrorAction SilentlyContinue |
        Where-Object { $_.State -eq "Listen" } |
        Select-Object -ExpandProperty OwningProcess -Unique

    $netstatPattern = "^\s*TCP\s+\S+:$Port\s+\S+\s+LISTENING\s+(\d+)\s*$"
    $processIds += netstat -ano |
        ForEach-Object {
            $match = [regex]::Match($_, $netstatPattern)
            if ($match.Success) { [int]$match.Groups[1].Value }
        }

    $processIds = $processIds | Where-Object { $_ -and $_ -ne 0 } | Sort-Object -Unique
    foreach ($processId in $processIds) {
        Write-Status "Stopping process on port ${Port}: PID $processId"
        Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
    }
}

function Start-Bot {
    $arguments = @(
        "-B",
        "app\run_mainline_observed_web.py"
    )
    $process = Start-Process python `
        -ArgumentList $arguments `
        -WorkingDirectory $DeployRoot `
        -RedirectStandardOutput $BotOutLog `
        -RedirectStandardError $BotErrLog `
        -WindowStyle Hidden `
        -PassThru
    Set-Content -LiteralPath $BotPidFile -Value $process.Id -Encoding ASCII
    Write-Status "Started 8793 review app: PID $($process.Id)"
    return $process
}

Set-Content -LiteralPath $StatusFile -Value "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') Watchdog started. Project=$ProjectDir Port=$Port DeployRoot=$DeployRoot" -Encoding UTF8
foreach ($path in @($BotOutLog, $BotErrLog)) {
    if (-not (Test-Path -LiteralPath $path)) { New-Item -ItemType File -Path $path -Force | Out-Null }
}

$botProcess = $null

while ($true) {
    if (-not $botProcess -or $botProcess.HasExited -or -not (Test-Health)) {
        if ($botProcess -and -not $botProcess.HasExited) {
            Stop-Process -Id $botProcess.Id -Force -ErrorAction SilentlyContinue
        }
        Write-Status "Health check failed; restarting 8793 review app."
        Stop-PortProcess
        Start-Sleep -Seconds 2
        $botProcess = Start-Bot
        Start-Sleep -Seconds 4
        if (Test-Health) {
            Write-Status "Health check passed."
        }
    }
    Start-Sleep -Seconds 20
}
