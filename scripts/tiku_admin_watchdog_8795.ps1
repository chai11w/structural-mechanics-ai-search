param(
    [int]$Port = 8795,
    [string]$AdminRuntime,
    [string]$SourceRuntime,
    [string]$PythonExe = "python"
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "watchdog_process_guard.ps1")

if ($Port -ne 8795) {
    throw "This watchdog is restricted to port 8795."
}

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

$ExpectedPythonPath = Resolve-WatchdogExecutablePath -Executable $PythonExe
$AdminArguments = @(
    "-B",
    "scripts\run_tiku_admin.py",
    "--host", "127.0.0.1",
    "--port", "$Port",
    "--admin-runtime", "$AdminRuntime",
    "--source-runtime", "$SourceRuntime"
)

$LogDir = Join-Path $AdminRuntime "service_logs"
$StatusFile = Join-Path $LogDir "watchdog_8795.status"
$WatchdogPidFile = Join-Path $LogDir "watchdog_8795.pid"
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
    $process = Start-Process $PythonExe `
        -ArgumentList $AdminArguments `
        -WorkingDirectory $ProjectDir `
        -RedirectStandardOutput $OutLog `
        -RedirectStandardError $ErrLog `
        -WindowStyle Hidden `
        -PassThru
    Write-Status "Launched 8795 administration console candidate: PID $($process.Id)"
    return $process
}

function Test-AdminProcess {
    param([Parameter(Mandatory = $true)][int]$ProcessId)
    return Test-WatchdogProcessMatch `
        -ProcessId $ProcessId `
        -Port $Port `
        -ExpectedExecutablePath $ExpectedPythonPath `
        -ExpectedArguments $AdminArguments
}

function Test-AdminLaunch {
    param([Parameter(Mandatory = $true)][int]$ProcessId)
    return Test-WatchdogLaunchIdentity `
        -ProcessId $ProcessId `
        -ExpectedExecutablePath $ExpectedPythonPath `
        -ExpectedArguments $AdminArguments
}

function Get-ManagedAdminProcess {
    return Get-WatchdogManagedProcess `
        -Port $Port `
        -ExpectedExecutablePath $ExpectedPythonPath `
        -ExpectedArguments $AdminArguments
}

$watchdogMutex = $null
$ownsWatchdogPidFile = $false
try {
    $watchdogMutex = Enter-WatchdogInstanceLock -Port $Port
    Assert-WatchdogPidFileAvailable `
        -Path $WatchdogPidFile `
        -OwnerProcessId $PID
    $adminProcess = Get-ManagedAdminProcess
    Set-Content -LiteralPath $WatchdogPidFile -Value $PID -Encoding ASCII
    $ownsWatchdogPidFile = $true
    Set-Content -LiteralPath $StatusFile -Value "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') Watchdog started. Project=$ProjectDir Port=$Port AdminRuntime=$AdminRuntime SourceRuntime=$SourceRuntime" -Encoding UTF8
    foreach ($path in @($OutLog, $ErrLog)) {
        if (-not (Test-Path -LiteralPath $path)) {
            New-Item -ItemType File -Path $path -Force | Out-Null
        }
    }

    if ($adminProcess -and (Test-Health)) {
        if ($adminProcess.HasExited -or -not (Test-AdminProcess -ProcessId $adminProcess.Id)) {
            throw "Existing 8795 process lost verified port ownership before PID update. PID file was not changed."
        }
        Set-Content -LiteralPath $PidFile -Value $adminProcess.Id -Encoding ASCII
        Write-Status "Adopted validated existing 8795 process: PID $($adminProcess.Id)"
    }
    while ($true) {
        $healthy = Test-Health
        if ($healthy) {
            if (-not $adminProcess -or $adminProcess.HasExited) {
                $adminProcess = Get-ManagedAdminProcess
            }
            if (-not $adminProcess -or -not (Test-AdminProcess -ProcessId $adminProcess.Id)) {
                throw "Healthy service on port $Port is not the validated 8795 process."
            }
            $recordedPid = if (Test-Path -LiteralPath $PidFile) {
                ([string](Get-Content -LiteralPath $PidFile -Raw)).Trim()
            } else {
                ""
            }
            if ($recordedPid -ne [string]$adminProcess.Id) {
                Set-Content -LiteralPath $PidFile -Value $adminProcess.Id -Encoding ASCII
            }
        } else {
            if (-not $adminProcess -or $adminProcess.HasExited) {
                $adminProcess = Get-ManagedAdminProcess
            }
            if ($adminProcess -and -not $adminProcess.HasExited) {
                if (-not (Test-AdminProcess -ProcessId $adminProcess.Id)) {
                    throw "Refusing to stop unverified PID $($adminProcess.Id) for 8795."
                }
                Write-Status "Health check failed; stopping tracked 8795 process PID $($adminProcess.Id)."
                Stop-Process -InputObject $adminProcess -Force -ErrorAction Stop
                Wait-Process -InputObject $adminProcess -Timeout 5 -ErrorAction SilentlyContinue
                $adminProcess = $null
            }
            $remainingOwners = @(Get-WatchdogListeningProcessIds -Port $Port)
            if ($remainingOwners.Count -gt 0) {
                throw "Refusing to start 8795 while an unverified process still owns port $Port."
            }
            Write-Status "Health check failed; starting 8795 administration console."
            $candidate = Start-Admin
            $startupState = Wait-WatchdogProcessReady `
                -Process $candidate `
                -TimeoutSeconds 30 `
                -HealthProbe { Test-Health } `
                -FullMatchProbe { Test-AdminProcess -ProcessId $candidate.Id } `
                -LaunchIdentityProbe { Test-AdminLaunch -ProcessId $candidate.Id }
            switch ($startupState) {
                "ready" {
                    if ($candidate.HasExited -or -not (Test-AdminProcess -ProcessId $candidate.Id)) {
                        throw "Candidate PID $($candidate.Id) lost verified port ownership before PID update. PID file was not changed."
                    }
                    Set-Content -LiteralPath $PidFile -Value $candidate.Id -Encoding ASCII
                    $adminProcess = $candidate
                    Write-Status "Health check passed."
                }
                "exited" {
                    Write-Status "Candidate PID $($candidate.Id) exited before validation; PID file was not changed."
                    $adminProcess = $null
                }
                "timeout_verified" {
                    if ($candidate.HasExited -or -not (Test-AdminLaunch -ProcessId $candidate.Id)) {
                        throw "Candidate PID $($candidate.Id) changed identity after startup timeout; refusing cleanup. PID file was not changed."
                    }
                    Write-Status "Candidate PID $($candidate.Id) timed out before becoming healthy; stopping verified launch candidate."
                    Stop-Process -InputObject $candidate -Force -ErrorAction Stop
                    Wait-Process -InputObject $candidate -Timeout 5 -ErrorAction SilentlyContinue
                    $adminProcess = $null
                }
                "timeout_unverified" {
                    throw "Candidate PID $($candidate.Id) timed out and could not be verified; refusing cleanup. PID file was not changed."
                }
                default {
                    throw "Unexpected 8795 startup state: $startupState. PID file was not changed."
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
