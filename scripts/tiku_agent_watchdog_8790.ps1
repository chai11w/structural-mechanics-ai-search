param(
    [int]$Port = 8790,
    [string]$RuntimeDir,
    [int]$MaxConcurrentTasks = 1,
    [int]$MaxQueuedTasks = 2,
    [int]$QueueWaitSeconds = 55,
    [double]$DailyBudgetCny = 0,
    [double]$PerInviteDailyBudgetCny = 0,
    [string]$InviteConfig
)

$ErrorActionPreference = "Stop"

$ProjectDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if (-not $RuntimeDir) {
    $RuntimeDir = Join-Path $ProjectDir ".tmp_tiku_agent_v2_prod_8790"
} elseif (-not [System.IO.Path]::IsPathRooted($RuntimeDir)) {
    $RuntimeDir = Join-Path $ProjectDir $RuntimeDir
}
if ($InviteConfig -and -not [System.IO.Path]::IsPathRooted($InviteConfig)) {
    $InviteConfig = Join-Path $ProjectDir $InviteConfig
}
if ($PerInviteDailyBudgetCny -gt 0 -and -not $InviteConfig) {
    throw "Per-invitation budget requires -InviteConfig."
}
if ($InviteConfig -and -not (Test-Path -LiteralPath $InviteConfig -PathType Leaf)) {
    throw "Invitation config not found: $InviteConfig"
}
$LogDir = $RuntimeDir
$StatusFile = Join-Path $LogDir "watchdog_8790.status"
$BotPidFile = Join-Path $LogDir "tiku_8790.pid"
$BotOutLog = Join-Path $LogDir "tiku_8790.out.log"
$BotErrLog = Join-Path $LogDir "tiku_8790.err.log"
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
        return ($response.ok -eq $true) -or ($response.status -eq "ok")
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
        "scripts\run_tiku_agent_demo.py",
        "--host", "127.0.0.1",
        "--port", "$Port",
        "--runtime-dir", "$RuntimeDir"
        "--max-concurrent-tasks", "$MaxConcurrentTasks"
        "--max-queued-tasks", "$MaxQueuedTasks"
        "--queue-wait-seconds", "$QueueWaitSeconds"
    )
    if ($DailyBudgetCny -gt 0) {
        $arguments += @("--daily-budget-cny", "$DailyBudgetCny")
    }
    if ($PerInviteDailyBudgetCny -gt 0) {
        $arguments += @("--per-invite-daily-budget-cny", "$PerInviteDailyBudgetCny")
    }
    if ($InviteConfig) {
        $arguments += @("--invite-config", "$InviteConfig")
    }
    $process = Start-Process python `
        -ArgumentList $arguments `
        -WorkingDirectory $ProjectDir `
        -RedirectStandardOutput $BotOutLog `
        -RedirectStandardError $BotErrLog `
        -WindowStyle Hidden `
        -PassThru
    Set-Content -LiteralPath $BotPidFile -Value $process.Id -Encoding ASCII
    Write-Status "Started 8790 tiku agent: PID $($process.Id)"
    return $process
}

Set-Content -LiteralPath $StatusFile -Value "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') Watchdog started. Project=$ProjectDir Port=$Port RuntimeDir=$RuntimeDir" -Encoding UTF8
foreach ($path in @($BotOutLog, $BotErrLog)) {
    if (-not (Test-Path -LiteralPath $path)) { New-Item -ItemType File -Path $path -Force | Out-Null }
}

$botProcess = $null

while ($true) {
    if (-not $botProcess -or $botProcess.HasExited -or -not (Test-Health)) {
        if ($botProcess -and -not $botProcess.HasExited) {
            Stop-Process -Id $botProcess.Id -Force -ErrorAction SilentlyContinue
        }
        Write-Status "Health check failed; restarting 8790 agent."
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
