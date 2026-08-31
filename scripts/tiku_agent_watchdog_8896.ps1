param(
    [int]$Port = 8896,
    [string]$RuntimeDir,
    [string]$PythonExe = "python",
    [switch]$DisableAutoCrop,
    [switch]$DisableA3TextOrientation,
    [switch]$DisableMediaCache,
    [switch]$DisableOutputWatchdog,
    [switch]$RequireScopedListenerQuery
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "watchdog_process_guard.ps1")
. (Join-Path $PSScriptRoot "tiku_agent_watchdog_8896_safety.ps1")

if ($Port -ne 8896) {
    throw "This watchdog is restricted to port 8896."
}

$ProjectDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if (-not $RuntimeDir) {
    $RuntimeDir = ".tmp_tiku_agent_a3_mvp_8896"
}
$RuntimeDir = Resolve-Tiku8896RuntimeDirectory `
    -RuntimeDirectory $RuntimeDir `
    -ProjectDirectory $ProjectDir

$ExpectedPythonPath = Resolve-WatchdogExecutablePath -Executable $PythonExe
$AgentEntrypoint = (Resolve-Path `
    -LiteralPath (Join-Path $ProjectDir "scripts\run_tiku_agent_8896.py") `
    -ErrorAction Stop
).Path
$AgentArguments = @(
    "-B",
    $AgentEntrypoint,
    "--host", "127.0.0.1",
    "--port", "$Port",
    "--runtime-dir", "$RuntimeDir"
)
if ($DisableAutoCrop) {
    $AgentArguments += "--disable-auto-crop"
}
if ($DisableA3TextOrientation) {
    $AgentArguments += "--disable-a3-text-orientation"
}
if ($DisableMediaCache) {
    $AgentArguments += "--disable-media-cache"
}
if ($DisableOutputWatchdog) {
    $AgentArguments += "--disable-output-watchdog"
}
$AgentLaunchArguments = @(
    $AgentArguments |
        ForEach-Object { ConvertTo-Tiku8896CommandLineArgument -Argument ([string]$_) }
)

$LogDir = Join-Path $RuntimeDir "service_logs"
$StatusFile = Join-Path $LogDir "watchdog_8896.status"
$WatchdogPidFile = Join-Path $LogDir "watchdog_8896.pid"
$AgentPidFile = Join-Path $LogDir "tiku_8896.pid"
$AgentOutLog = Join-Path $LogDir "tiku_8896.out.log"
$AgentErrLog = Join-Path $LogDir "tiku_8896.err.log"
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

function Start-Agent {
    $process = Start-Process $ExpectedPythonPath `
        -ArgumentList $AgentLaunchArguments `
        -WorkingDirectory $ProjectDir `
        -RedirectStandardOutput $AgentOutLog `
        -RedirectStandardError $AgentErrLog `
        -WindowStyle Hidden `
        -PassThru
    Write-Status "Launched 8896 tiku agent candidate: PID $($process.Id)"
    return $process
}

function Get-8896ListenerProcessIds {
    if ($RequireScopedListenerQuery) {
        return @(Get-Tiku8896ScopedListeningProcessIds -Port $Port)
    }
    return @(Get-Tiku8896ListeningProcessIds -Port $Port)
}

function Test-AgentProcess {
    param([Parameter(Mandatory = $true)][int]$ProcessId)
    try {
        $owners = @(Get-8896ListenerProcessIds)
        $record = Get-CimInstance `
            Win32_Process `
            -Filter "ProcessId = $ProcessId" `
            -ErrorAction Stop
        if (-not $record) {
            return $false
        }
        return Test-WatchdogProcessEvidence `
            -ProcessId $ProcessId `
            -ListeningProcessIds $owners `
            -ExecutablePath ([string]$record.ExecutablePath) `
            -ExpectedExecutablePath $ExpectedPythonPath `
            -CommandLine ([string]$record.CommandLine) `
            -ExpectedArguments $AgentArguments
    } catch {
        return $false
    }
}

function Test-AgentLaunch {
    param([Parameter(Mandatory = $true)][int]$ProcessId)
    return Test-WatchdogLaunchIdentity `
        -ProcessId $ProcessId `
        -ExpectedExecutablePath $ExpectedPythonPath `
        -ExpectedArguments $AgentArguments
}

function Get-ManagedAgentProcess {
    $owners = @(Get-8896ListenerProcessIds)
    if ($owners.Count -eq 0) {
        return $null
    }
    if ($owners.Count -ne 1) {
        throw "Refusing to manage port $Port because it has multiple listening owners."
    }
    $candidateId = [int]$owners[0]
    if (-not (Test-AgentProcess -ProcessId $candidateId)) {
        throw "Refusing to manage unverified process PID $candidateId on port $Port."
    }
    return Get-Process -Id $candidateId -ErrorAction Stop
}

$watchdogMutex = $null
$ownsWatchdogPidFile = $false
try {
    $watchdogMutex = Enter-WatchdogInstanceLock -Port $Port
    Assert-WatchdogPidFileAvailable `
        -Path $WatchdogPidFile `
        -OwnerProcessId $PID
    $agentProcess = Get-ManagedAgentProcess
    Set-Content -LiteralPath $WatchdogPidFile -Value $PID -Encoding ASCII
    $ownsWatchdogPidFile = $true
    Set-Content -LiteralPath $StatusFile -Value "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') Watchdog started. Project=$ProjectDir Port=$Port RuntimeDir=$RuntimeDir" -Encoding UTF8
    foreach ($path in @($AgentOutLog, $AgentErrLog)) {
        if (-not (Test-Path -LiteralPath $path)) {
            New-Item -ItemType File -Path $path -Force | Out-Null
        }
    }

    if ($agentProcess -and (Test-Health)) {
        if ($agentProcess.HasExited -or -not (Test-AgentProcess -ProcessId $agentProcess.Id)) {
            throw "Existing 8896 process lost verified port ownership before PID update. PID file was not changed."
        }
        Set-Content -LiteralPath $AgentPidFile -Value $agentProcess.Id -Encoding ASCII
        Write-Status "Adopted validated existing 8896 process: PID $($agentProcess.Id)"
    }

    while ($true) {
        $healthy = Test-Health
        if ($healthy) {
            if (-not $agentProcess -or $agentProcess.HasExited) {
                $agentProcess = Get-ManagedAgentProcess
            }
            if (-not $agentProcess -or -not (Test-AgentProcess -ProcessId $agentProcess.Id)) {
                throw "Healthy service on port $Port is not the validated 8896 process."
            }
            $recordedPid = if (Test-Path -LiteralPath $AgentPidFile) {
                ([string](Get-Content -LiteralPath $AgentPidFile -Raw)).Trim()
            } else {
                ""
            }
            if ($recordedPid -ne [string]$agentProcess.Id) {
                Set-Content -LiteralPath $AgentPidFile -Value $agentProcess.Id -Encoding ASCII
            }
        } else {
            if (-not $agentProcess -or $agentProcess.HasExited) {
                $agentProcess = Get-ManagedAgentProcess
            }
            if ($agentProcess -and -not $agentProcess.HasExited) {
                if (-not (Test-AgentProcess -ProcessId $agentProcess.Id)) {
                    throw "Refusing to stop unverified PID $($agentProcess.Id) for 8896."
                }
                Write-Status "Health check failed; stopping validated 8896 process PID $($agentProcess.Id)."
                Stop-Process -InputObject $agentProcess -Force -ErrorAction Stop
                Wait-Process -InputObject $agentProcess -Timeout 5 -ErrorAction SilentlyContinue
                $agentProcess = $null
            }
            $remainingOwners = @(Get-8896ListenerProcessIds)
            if ($remainingOwners.Count -gt 0) {
                throw "Refusing to start 8896 while an unverified process still owns port $Port."
            }
            Write-Status "Health check failed; restarting 8896 agent."
            $candidate = Start-Agent
            $startupState = Wait-WatchdogProcessReady `
                -Process $candidate `
                -HealthProbe { Test-Health } `
                -FullMatchProbe { Test-AgentProcess -ProcessId $candidate.Id } `
                -LaunchIdentityProbe { Test-AgentLaunch -ProcessId $candidate.Id }
            switch ($startupState) {
                "ready" {
                    if ($candidate.HasExited -or -not (Test-AgentProcess -ProcessId $candidate.Id)) {
                        throw "Candidate PID $($candidate.Id) lost verified port ownership before PID update. PID file was not changed."
                    }
                    Set-Content -LiteralPath $AgentPidFile -Value $candidate.Id -Encoding ASCII
                    $agentProcess = $candidate
                    Write-Status "Health check passed."
                }
                "exited" {
                    Write-Status "Candidate PID $($candidate.Id) exited before becoming ready."
                    $agentProcess = $null
                }
                "timeout_verified" {
                    if ($candidate.HasExited -or -not (Test-AgentLaunch -ProcessId $candidate.Id)) {
                        throw "Candidate PID $($candidate.Id) changed identity after startup timeout; refusing cleanup. PID file was not changed."
                    }
                    Write-Status "Candidate PID $($candidate.Id) timed out before becoming healthy; stopping verified launch candidate."
                    Stop-Process -InputObject $candidate -Force -ErrorAction Stop
                    Wait-Process -InputObject $candidate -Timeout 5 -ErrorAction SilentlyContinue
                    $agentProcess = $null
                }
                "timeout_unverified" {
                    throw "Candidate PID $($candidate.Id) timed out and could not be verified; refusing cleanup. PID file was not changed."
                }
                default {
                    throw "Unexpected 8896 startup state: $startupState. PID file was not changed."
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
