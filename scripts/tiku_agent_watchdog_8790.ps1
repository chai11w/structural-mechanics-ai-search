param(
    [int]$Port = 8790,
    [string]$RuntimeDir,
    [int]$MaxConcurrentTasks = 1,
    [int]$MaxQueuedTasks = 2,
    [int]$QueueWaitSeconds = 55,
    [double]$DailyBudgetCny = 0,
    [double]$PerInviteDailyBudgetCny = 0,
    [string]$InviteConfig,
    [string]$ControlDb,
    [switch]$DisableExternalLoadScreen,
    [switch]$DisableAutoCrop,
    [switch]$DisableOutputWatchdog,
    [string]$PythonExe = "python"
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "watchdog_process_guard.ps1")

if ($Port -ne 8790) {
    throw "This watchdog is restricted to port 8790."
}

$ProjectDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if (-not $RuntimeDir) {
    $RuntimeDir = Join-Path $ProjectDir ".tmp_tiku_agent_v2_prod_8790"
} elseif (-not [System.IO.Path]::IsPathRooted($RuntimeDir)) {
    $RuntimeDir = Join-Path $ProjectDir $RuntimeDir
}
if (-not $ControlDb -and -not $InviteConfig) {
    $ControlDb = Join-Path $ProjectDir ".tmp_tiku_admin_8795\control.sqlite3"
}
if ($InviteConfig -and -not [System.IO.Path]::IsPathRooted($InviteConfig)) {
    $InviteConfig = Join-Path $ProjectDir $InviteConfig
}
if ($ControlDb -and -not [System.IO.Path]::IsPathRooted($ControlDb)) {
    $ControlDb = Join-Path $ProjectDir $ControlDb
}
if ($ControlDb -and $InviteConfig) {
    throw "Use either -ControlDb or -InviteConfig, not both."
}
if ($ControlDb -and ($DailyBudgetCny -gt 0 -or $PerInviteDailyBudgetCny -gt 0)) {
    throw "ControlDb provides dynamic budgets; do not also set static budget arguments."
}
if ($PerInviteDailyBudgetCny -gt 0 -and -not $InviteConfig) {
    throw "Per-invitation budget requires -InviteConfig."
}
if ($InviteConfig -and -not (Test-Path -LiteralPath $InviteConfig -PathType Leaf)) {
    throw "Invitation config not found: $InviteConfig"
}
if ($ControlDb -and -not (Test-Path -LiteralPath $ControlDb -PathType Leaf)) {
    throw "Administrator control database not found: $ControlDb"
}
$ExpectedPythonPath = Resolve-WatchdogExecutablePath -Executable $PythonExe
$BotArguments = @(
    "scripts\run_tiku_agent_8790.py",
    "--host", "127.0.0.1",
    "--port", "$Port",
    "--runtime-dir", "$RuntimeDir",
    "--max-concurrent-tasks", "$MaxConcurrentTasks",
    "--max-queued-tasks", "$MaxQueuedTasks",
    "--queue-wait-seconds", "$QueueWaitSeconds"
)
if ($InviteConfig) {
    $BotArguments += @("--invite-config", "$InviteConfig")
}
if ($ControlDb) {
    $BotArguments += @("--control-db", "$ControlDb")
}
if ($DisableAutoCrop) {
    $BotArguments += "--disable-auto-crop"
}
if ($DisableOutputWatchdog) {
    $BotArguments += "--disable-output-watchdog"
}
$LogDir = $RuntimeDir
$StatusFile = Join-Path $LogDir "watchdog_8790.status"
$WatchdogPidFile = Join-Path $LogDir "watchdog_8790.pid"
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

function Start-Bot {
    $process = Start-Process $PythonExe `
        -ArgumentList $BotArguments `
        -WorkingDirectory $ProjectDir `
        -RedirectStandardOutput $BotOutLog `
        -RedirectStandardError $BotErrLog `
        -WindowStyle Hidden `
        -PassThru
    Write-Status "Launched 8790 tiku agent candidate: PID $($process.Id)"
    return $process
}

function Test-BotProcess {
    param([Parameter(Mandatory = $true)][int]$ProcessId)
    return Test-WatchdogProcessMatch `
        -ProcessId $ProcessId `
        -Port $Port `
        -ExpectedExecutablePath $ExpectedPythonPath `
        -ExpectedArguments $BotArguments
}

function Test-BotLaunch {
    param([Parameter(Mandatory = $true)][int]$ProcessId)
    return Test-WatchdogLaunchIdentity `
        -ProcessId $ProcessId `
        -ExpectedExecutablePath $ExpectedPythonPath `
        -ExpectedArguments $BotArguments
}

function Get-ManagedBotProcess {
    return Get-WatchdogManagedProcess `
        -Port $Port `
        -ExpectedExecutablePath $ExpectedPythonPath `
        -ExpectedArguments $BotArguments
}

$watchdogMutex = $null
$ownsWatchdogPidFile = $false
try {
    $watchdogMutex = Enter-WatchdogInstanceLock -Port $Port
    Assert-WatchdogPidFileAvailable `
        -Path $WatchdogPidFile `
        -OwnerProcessId $PID
    $botProcess = Get-ManagedBotProcess
    Set-Content -LiteralPath $WatchdogPidFile -Value $PID -Encoding ASCII
    $ownsWatchdogPidFile = $true
    Set-Content -LiteralPath $StatusFile -Value "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') Watchdog started. Project=$ProjectDir Port=$Port RuntimeDir=$RuntimeDir" -Encoding UTF8
    foreach ($path in @($BotOutLog, $BotErrLog)) {
        if (-not (Test-Path -LiteralPath $path)) { New-Item -ItemType File -Path $path -Force | Out-Null }
    }

    if ($botProcess -and (Test-Health)) {
        if ($botProcess.HasExited -or -not (Test-BotProcess -ProcessId $botProcess.Id)) {
            throw "Existing 8790 process lost verified port ownership before PID update. PID file was not changed."
        }
        Set-Content -LiteralPath $BotPidFile -Value $botProcess.Id -Encoding ASCII
        Write-Status "Adopted validated existing 8790 process: PID $($botProcess.Id)"
    }

    while ($true) {
        $healthy = Test-Health
        if ($healthy) {
            if (-not $botProcess -or $botProcess.HasExited) {
                $botProcess = Get-ManagedBotProcess
            }
            if (-not $botProcess -or -not (Test-BotProcess -ProcessId $botProcess.Id)) {
                throw "Healthy service on port $Port is not the validated 8790 process."
            }
            $recordedPid = if (Test-Path -LiteralPath $BotPidFile) {
                ([string](Get-Content -LiteralPath $BotPidFile -Raw)).Trim()
            } else {
                ""
            }
            if ($recordedPid -ne [string]$botProcess.Id) {
                Set-Content -LiteralPath $BotPidFile -Value $botProcess.Id -Encoding ASCII
            }
        } else {
            if (-not $botProcess -or $botProcess.HasExited) {
                $botProcess = Get-ManagedBotProcess
            }
            if ($botProcess -and -not $botProcess.HasExited) {
                if (-not (Test-BotProcess -ProcessId $botProcess.Id)) {
                    throw "Refusing to stop unverified PID $($botProcess.Id) for 8790."
                }
                Write-Status "Health check failed; stopping validated 8790 process PID $($botProcess.Id)."
                Stop-Process -InputObject $botProcess -Force -ErrorAction Stop
                Wait-Process -InputObject $botProcess -Timeout 5 -ErrorAction SilentlyContinue
                $botProcess = $null
            }
            $remainingOwners = @(Get-WatchdogListeningProcessIds -Port $Port)
            if ($remainingOwners.Count -gt 0) {
                throw "Refusing to start 8790 while an unverified process still owns port $Port."
            }
            Write-Status "Health check failed; restarting 8790 agent."
            $candidate = Start-Bot
            $startupState = Wait-WatchdogProcessReady `
                -Process $candidate `
                -TimeoutSeconds 30 `
                -HealthProbe { Test-Health } `
                -FullMatchProbe { Test-BotProcess -ProcessId $candidate.Id } `
                -LaunchIdentityProbe { Test-BotLaunch -ProcessId $candidate.Id }
            switch ($startupState) {
                "ready" {
                    if ($candidate.HasExited -or -not (Test-BotProcess -ProcessId $candidate.Id)) {
                        throw "Candidate PID $($candidate.Id) lost verified port ownership before PID update. PID file was not changed."
                    }
                    Set-Content -LiteralPath $BotPidFile -Value $candidate.Id -Encoding ASCII
                    $botProcess = $candidate
                    Write-Status "Health check passed."
                }
                "exited" {
                    Write-Status "Candidate PID $($candidate.Id) exited before validation; PID file was not changed."
                    $botProcess = $null
                }
                "timeout_verified" {
                    if ($candidate.HasExited -or -not (Test-BotLaunch -ProcessId $candidate.Id)) {
                        throw "Candidate PID $($candidate.Id) changed identity after startup timeout; refusing cleanup. PID file was not changed."
                    }
                    Write-Status "Candidate PID $($candidate.Id) timed out before becoming healthy; stopping verified launch candidate."
                    Stop-Process -InputObject $candidate -Force -ErrorAction Stop
                    Wait-Process -InputObject $candidate -Timeout 5 -ErrorAction SilentlyContinue
                    $botProcess = $null
                }
                "timeout_unverified" {
                    throw "Candidate PID $($candidate.Id) timed out and could not be verified; refusing cleanup. PID file was not changed."
                }
                default {
                    throw "Unexpected 8790 startup state: $startupState. PID file was not changed."
                }
            }
        }
        Start-Sleep -Seconds 20
    }
} finally {
    if ($ownsWatchdogPidFile) {
        Remove-WatchdogPidFileIfOwned -Path $WatchdogPidFile -OwnerProcessId $PID
    }
    if ($watchdogMutex) {
        Exit-WatchdogInstanceLock -Mutex $watchdogMutex
    }
}
