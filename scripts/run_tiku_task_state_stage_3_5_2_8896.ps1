param(
    [Parameter(Mandatory = $true)][string]$ExpectedCommit,
    [Parameter(Mandatory = $true)][string]$CheckoutDir,
    [Parameter(Mandatory = $true)][string]$GitExe,
    [Parameter(Mandatory = $true)][string]$PythonExe,
    [Parameter(Mandatory = $true)][string]$PowerShellExe,
    [Parameter(Mandatory = $true)][string]$RuntimeRoot,
    [ValidateRange(10, 90)][int]$StartupTimeoutSeconds = 45,
    [ValidateRange(1, 30)][int]$VerifierTimeoutSeconds = 5,
    [ValidateRange(20, 300)][int]$PostHttpHoldSeconds = 25,
    [switch]$StaticPreflightOnly
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "watchdog_process_guard.ps1")
. (Join-Path $PSScriptRoot "tiku_agent_watchdog_8896_safety.ps1")
. (Join-Path $PSScriptRoot "tiku_task_state_stage_3_5_2_8896_safety.ps1")

$ScriptCheckout = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if (-not [System.IO.Path]::IsPathRooted($CheckoutDir)) {
    throw "The explicit stage checkout must be absolute."
}
$RequestedCheckout = (Resolve-Path -LiteralPath $CheckoutDir -ErrorAction Stop).Path
if (-not [string]::Equals(
    $RequestedCheckout,
    $ScriptCheckout,
    [System.StringComparison]::OrdinalIgnoreCase
)) {
    throw "The explicit stage checkout does not own this runner."
}

$ResolvedGit = Resolve-TikuTaskStateStageGit -GitExecutable $GitExe
$StageGitQuery = {
    param([string]$ProjectDirectory, [string[]]$GitArguments)
    Invoke-TikuTaskStateStageGit `
        -ProjectDirectory $ProjectDirectory `
        -GitArguments $GitArguments `
        -GitExecutable $ResolvedGit
}
$ProjectDir = Assert-TikuTaskStateStageCheckout `
    -ProjectDirectory $RequestedCheckout `
    -ExpectedCommit $ExpectedCommit `
    -GitQuery $StageGitQuery
$ResolvedPython = Resolve-TikuTaskStateStagePython -PythonExecutable $PythonExe
$ResolvedPowerShell = Resolve-TikuTaskStateStagePowerShell `
    -PowerShellExecutable $PowerShellExe
$GitCommonOutput = @(& $StageGitQuery `
    -ProjectDirectory $ProjectDir `
    -GitArguments @("rev-parse", "--path-format=absolute", "--git-common-dir"))
if ($GitCommonOutput.Count -ne 1) {
    throw "Unable to resolve the Git common directory."
}
$GitCommonDir = [System.IO.Path]::GetFullPath(([string]$GitCommonOutput[0]).Trim())
$ResolvedRuntimeRoot = Resolve-TikuTaskStateStageRuntimeRoot `
    -RuntimeRoot $RuntimeRoot `
    -ProjectDirectory $ProjectDir `
    -GitCommonDirectory $GitCommonDir

$WatchdogScript = (Resolve-Path `
    -LiteralPath (Join-Path $PSScriptRoot "tiku_agent_watchdog_8896.ps1") `
    -ErrorAction Stop
).Path
$AgentEntrypoint = (Resolve-Path `
    -LiteralPath (Join-Path $PSScriptRoot "run_tiku_agent_8896.py") `
    -ErrorAction Stop
).Path
$VerifierScript = (Resolve-Path `
    -LiteralPath (Join-Path $PSScriptRoot "verify_tiku_task_state_8896.py") `
    -ErrorAction Stop
).Path

if ($StaticPreflightOnly) {
    [ordered]@{
        status = "static_preflight_ok"
        expected_commit = $ExpectedCommit.ToLowerInvariant()
        checkout = $ProjectDir
        git = $ResolvedGit
        python = $ResolvedPython
        powershell = $ResolvedPowerShell
        runtime_root = $ResolvedRuntimeRoot
    } | ConvertTo-Json -Compress
    return
}

$runnerMutex = [System.Threading.Mutex]::new(
    $false,
    "Global\TikuQuestionBank.Stage.3.5.2.Port.8896"
)
$ownsRunnerMutex = $false
$watchdogProcess = $null
$watchdogPid = 0
$agentPid = 0
$runtime = ""
$primaryError = $null
$cleanupErrors = [System.Collections.Generic.List[object]]::new()
$startedAt = [datetime]::UtcNow
$httpVerified = $false

function Test-StageHealth {
    try {
        $response = Invoke-RestMethod `
            -Uri "http://127.0.0.1:8896/health" `
            -TimeoutSec 3
        return ($response.status -eq "ok") -or ($response.ok -eq $true)
    } catch {
        return $false
    }
}

function Get-Stage8896ListeningProcessIds {
    return @(Get-Tiku8896ScopedListeningProcessIds -Port 8896)
}

function Test-StageWatchdogIdentity {
    param([Parameter(Mandatory = $true)][int]$ProcessId)

    if ($ProcessId -le 0 -or -not $script:WatchdogArguments) {
        return $false
    }
    try {
        $record = Get-CimInstance `
            Win32_Process `
            -Filter "ProcessId = $ProcessId" `
            -ErrorAction Stop
        if (-not $record) {
            return $false
        }
        return Test-WatchdogLaunchEvidence `
            -ProcessId $ProcessId `
            -ExecutablePath ([string]$record.ExecutablePath) `
            -ExpectedExecutablePath $ResolvedPowerShell `
            -CommandLine ([string]$record.CommandLine) `
            -ExpectedArguments $script:WatchdogArguments
    } catch {
        return $false
    }
}

function Test-StageAgentLaunchIdentity {
    param([Parameter(Mandatory = $true)][int]$ProcessId)

    if ($ProcessId -le 0 -or -not $script:AgentArguments -or $watchdogPid -le 0) {
        return $false
    }
    try {
        $record = Get-CimInstance `
            Win32_Process `
            -Filter "ProcessId = $ProcessId" `
            -ErrorAction Stop
        if (-not $record -or [int]$record.ParentProcessId -ne $watchdogPid) {
            return $false
        }
        return Test-WatchdogLaunchEvidence `
            -ProcessId $ProcessId `
            -ExecutablePath ([string]$record.ExecutablePath) `
            -ExpectedExecutablePath $ResolvedPython `
            -CommandLine ([string]$record.CommandLine) `
            -ExpectedArguments $script:AgentArguments
    } catch {
        return $false
    }
}

function Test-StageAgentIdentity {
    param([Parameter(Mandatory = $true)][int]$ProcessId)

    if (-not (Test-StageAgentLaunchIdentity -ProcessId $ProcessId)) {
        return $false
    }
    try {
        $owners = @(Get-Stage8896ListeningProcessIds)
        return $owners.Count -eq 1 -and [int]$owners[0] -eq $ProcessId
    } catch {
        return $false
    }
}

function Assert-StageStableProcesses {
    if (-not (Test-StageWatchdogIdentity -ProcessId $watchdogPid)) {
        throw "The stage watchdog identity changed."
    }
    $recordedWatchdog = Get-TikuTaskStateStagePidFileValue -Path $WatchdogPidFile
    if ($recordedWatchdog -ne $watchdogPid) {
        throw "The stage watchdog PID file changed."
    }
    $recordedAgent = Get-TikuTaskStateStagePidFileValue -Path $AgentPidFile
    if ($recordedAgent -ne $agentPid) {
        throw "The stage agent PID changed."
    }
    if (-not (Test-StageAgentIdentity -ProcessId $agentPid)) {
        throw "The stage agent identity changed."
    }
    if (-not (Test-StageHealth)) {
        throw "The stage service is not healthy."
    }
}

try {
    try {
        $ownsRunnerMutex = $runnerMutex.WaitOne(0)
    } catch [System.Threading.AbandonedMutexException] {
        $ownsRunnerMutex = $true
    }
    if (-not $ownsRunnerMutex) {
        throw "Another stage 3.5.2 runner is active."
    }

    $existingOwners = @(Get-Stage8896ListeningProcessIds)
    if ($existingOwners.Count -ne 0) {
        throw "Stage 3.5.2 requires an unused 8896 listener before start."
    }

    $runtime = New-TikuTaskStateStageFreshRuntime -RuntimeRoot $ResolvedRuntimeRoot
    $LogDir = Join-Path $runtime "service_logs"
    $WatchdogPidFile = Join-Path $LogDir "watchdog_8896.pid"
    $AgentPidFile = Join-Path $LogDir "tiku_8896.pid"
    $EvidenceFile = Join-Path $runtime "task_state_8896_http_evidence.json"
    $WatchdogOutLog = Join-Path $runtime "stage_watchdog.out.log"
    $WatchdogErrLog = Join-Path $runtime "stage_watchdog.err.log"

    $script:WatchdogArguments = @(
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy", "Bypass",
        "-File", $WatchdogScript,
        "-Port", "8896",
        "-RuntimeDir", $runtime,
        "-PythonExe", $ResolvedPython,
        "-RequireScopedListenerQuery"
    )
    $script:AgentArguments = @(
        "-B",
        $AgentEntrypoint,
        "--host", "127.0.0.1",
        "--port", "8896",
        "--runtime-dir", $runtime
    )
    $encodedWatchdogArguments = @(
        $script:WatchdogArguments |
            ForEach-Object {
                ConvertTo-Tiku8896CommandLineArgument -Argument ([string]$_)
            }
    )
    $watchdogProcess = Start-Process `
        -FilePath $ResolvedPowerShell `
        -ArgumentList $encodedWatchdogArguments `
        -WorkingDirectory $ProjectDir `
        -RedirectStandardOutput $WatchdogOutLog `
        -RedirectStandardError $WatchdogErrLog `
        -WindowStyle Hidden `
        -PassThru
    $watchdogPid = [int]$watchdogProcess.Id

    $startupDeadline = (Get-Date).AddSeconds($StartupTimeoutSeconds)
    $ready = $false
    while ((Get-Date) -lt $startupDeadline) {
        if ($watchdogProcess.HasExited) {
            throw "The stage watchdog exited during startup."
        }
        if ((Test-Path -LiteralPath $WatchdogPidFile -PathType Leaf) -and
            (Test-Path -LiteralPath $AgentPidFile -PathType Leaf)) {
            try {
                $recordedWatchdog = Get-TikuTaskStateStagePidFileValue `
                    -Path $WatchdogPidFile
                $candidateAgent = Get-TikuTaskStateStagePidFileValue -Path $AgentPidFile
                if ($recordedWatchdog -eq $watchdogPid -and
                    (Test-StageWatchdogIdentity -ProcessId $watchdogPid) -and
                    (Test-StageAgentIdentity -ProcessId $candidateAgent) -and
                    (Test-StageHealth)) {
                    $agentPid = $candidateAgent
                    $ready = $true
                    break
                }
            } catch {
                # PID files can be observed between create and complete write.
            }
        }
        Start-Sleep -Milliseconds 500
    }
    if (-not $ready) {
        throw "The stage service did not become ready with verified identities."
    }

    $verifierOutput = @(& $ResolvedPython `
        -B `
        $VerifierScript `
        --base-url "http://127.0.0.1:8896" `
        --timeout-seconds "$VerifierTimeoutSeconds" `
        --expected-commit $ExpectedCommit `
        --runtime-identity $runtime `
        --evidence-output $EvidenceFile)
    $verifierExitCode = $LASTEXITCODE
    if ($verifierExitCode -ne 0 -or $verifierOutput.Count -ne 1) {
        throw "The stage HTTP verifier failed."
    }
    try {
        $httpResult = ([string]$verifierOutput[0]) | ConvertFrom-Json -ErrorAction Stop
    } catch {
        throw "The stage HTTP verifier did not return one JSON result."
    }
    if ($httpResult.schema -ne "tiku-task-state-8896-smoke-evidence-v1" -or
        $httpResult.sqlite_evidence -ne "pending_offline_verification" -or
        -not (Test-Path -LiteralPath $EvidenceFile -PathType Leaf)) {
        throw "The stage HTTP verifier result is incomplete."
    }
    Assert-StageStableProcesses
    $httpVerified = $true
    Write-Output "BROWSER_WINDOW_READY_SECONDS=$PostHttpHoldSeconds"
    Write-Output "BROWSER_URL=http://127.0.0.1:8896/"

    $holdDeadline = (Get-Date).AddSeconds($PostHttpHoldSeconds)
    while ((Get-Date) -lt $holdDeadline) {
        Assert-StageStableProcesses
        $remaining = [Math]::Max(0, [int][Math]::Ceiling(
            ($holdDeadline - (Get-Date)).TotalSeconds
        ))
        if ($remaining -eq 0) {
            break
        }
        Start-Sleep -Seconds ([Math]::Min(5, $remaining))
    }
    Assert-StageStableProcesses
    Assert-TikuTaskStateStageCheckout `
        -ProjectDirectory $ProjectDir `
        -ExpectedCommit $ExpectedCommit `
        -GitQuery $StageGitQuery | Out-Null
} catch {
    $primaryError = $_
} finally {
    $watchdogFrozen = $false
    try {
        if ($watchdogPid -gt 0) {
            Stop-TikuTaskStateStageExactProcess `
                -ProcessId $watchdogPid `
                -Role "watchdog" `
                -IdentityProbe { param($ProcessId) Test-StageWatchdogIdentity -ProcessId $ProcessId }
        }
        $watchdogFrozen = $true
    } catch {
        [void]$cleanupErrors.Add($_)
    }

    if ($watchdogFrozen) {
        $candidateIds = [System.Collections.Generic.HashSet[int]]::new()
        if ($agentPid -gt 0) {
            [void]$candidateIds.Add($agentPid)
        }
        if ($AgentPidFile -and (Test-Path -LiteralPath $AgentPidFile -PathType Leaf)) {
            try {
                [void]$candidateIds.Add(
                    (Get-TikuTaskStateStagePidFileValue -Path $AgentPidFile)
                )
            } catch {
                [void]$cleanupErrors.Add($_)
            }
        }
        if ($watchdogPid -gt 0 -and $script:AgentArguments) {
            try {
                $exactChildren = @(Get-TikuTaskStateStageExactChildProcessIds `
                    -ParentProcessId $watchdogPid `
                    -ExpectedExecutablePath $ResolvedPython `
                    -ExpectedArguments $script:AgentArguments)
                foreach ($childId in $exactChildren) {
                    [void]$candidateIds.Add([int]$childId)
                }
            } catch {
                [void]$cleanupErrors.Add($_)
            }
        }
        foreach ($candidateId in @($candidateIds | Sort-Object)) {
            try {
                $candidateProcess = Get-Process -Id $candidateId -ErrorAction SilentlyContinue
                if (-not $candidateProcess -or $candidateProcess.HasExited) {
                    continue
                }
                if (-not (Test-StageAgentLaunchIdentity -ProcessId $candidateId)) {
                    throw "Refusing to stop an unverified recorded stage agent PID $candidateId."
                }
                $agentPid = [int]$candidateId
                Stop-TikuTaskStateStageExactProcess `
                    -ProcessId $candidateId `
                    -Role "agent" `
                    -IdentityProbe { param($ProcessId) Test-StageAgentLaunchIdentity -ProcessId $ProcessId }
            } catch {
                [void]$cleanupErrors.Add($_)
            }
        }

        if ($watchdogPid -gt 0) {
            try {
                for ($clearCheck = 0; $clearCheck -lt 2; $clearCheck += 1) {
                    $remainingChildren = @(Get-TikuTaskStateStageChildProcessIds `
                        -ParentProcessId $watchdogPid)
                    if ($remainingChildren.Count -ne 0) {
                        throw "Cleanup found surviving direct watchdog child PIDs: $($remainingChildren -join ',')."
                    }
                    if ($clearCheck -eq 0) {
                        Start-Sleep -Milliseconds 100
                    }
                }
            } catch {
                [void]$cleanupErrors.Add($_)
            }
        }
        try {
            $remainingOwners = @(Get-Stage8896ListeningProcessIds)
            if ($remainingOwners.Count -ne 0) {
                throw "Cleanup found an unverified or surviving 8896 listener."
            }
        } catch {
            [void]$cleanupErrors.Add($_)
        }
    }
    try {
        if ($ownsRunnerMutex) {
            $runnerMutex.ReleaseMutex()
        }
    } catch {
        [void]$cleanupErrors.Add($_)
    } finally {
        try {
            $runnerMutex.Dispose()
        } catch {
            [void]$cleanupErrors.Add($_)
        }
    }
}

if ($primaryError) {
    if ($cleanupErrors.Count -gt 0) {
        $cleanupMessage = @(
            $cleanupErrors | ForEach-Object { $_.Exception.Message }
        ) -join " | "
        $combinedMessage = "Stage run failed: {0} Exact cleanup also failed: {1}" -f `
            $primaryError.Exception.Message, $cleanupMessage
        throw [System.InvalidOperationException]::new(
            $combinedMessage,
            $primaryError.Exception
        )
    }
    throw $primaryError
}
if ($cleanupErrors.Count -gt 0) {
    $cleanupMessage = @(
        $cleanupErrors | ForEach-Object { $_.Exception.Message }
    ) -join " | "
    throw [System.InvalidOperationException]::new(
        "Exact cleanup failed: $cleanupMessage",
        $cleanupErrors[0].Exception
    )
}
if (-not $httpVerified) {
    throw "Stage HTTP verification did not complete."
}

$offlineOutput = @(& $ResolvedPython `
    -B `
    $VerifierScript `
    --runtime-dir $runtime `
    --evidence-input $EvidenceFile `
    --expected-commit $ExpectedCommit)
$offlineExitCode = $LASTEXITCODE
if ($offlineExitCode -ne 0 -or $offlineOutput.Count -ne 1) {
    throw "The stopped-runtime SQLite verifier failed."
}
try {
    $offlineResult = ([string]$offlineOutput[0]) | ConvertFrom-Json -ErrorAction Stop
} catch {
    throw "The stopped-runtime SQLite verifier did not return one JSON result."
}
if ($offlineResult.runtime_evidence -ne "ok") {
    throw "The stopped-runtime SQLite evidence is incomplete."
}
Assert-TikuTaskStateStageCheckout `
    -ProjectDirectory $ProjectDir `
    -ExpectedCommit $ExpectedCommit `
    -GitQuery $StageGitQuery | Out-Null

$manifest = [ordered]@{
    schema = "tiku-task-state-stage-3-5-2-run-v1"
    status = "http_sqlite_ok"
    expected_commit = $ExpectedCommit.ToLowerInvariant()
    checkout = $ProjectDir
    runtime = $runtime
    python = $ResolvedPython
    powershell = $ResolvedPowerShell
    watchdog_pid = $watchdogPid
    agent_pid = $agentPid
    started_at = $startedAt.ToString("o")
    finished_at = [datetime]::UtcNow.ToString("o")
    post_http_hold_seconds = $PostHttpHoldSeconds
    http_evidence = $EvidenceFile
    cleanup = "stopped"
    runtime_evidence = $offlineResult
}
$ManifestFile = Join-Path $runtime "stage_3_5_2_result.json"
$manifest | ConvertTo-Json -Depth 6 | Set-Content `
    -LiteralPath $ManifestFile `
    -Encoding UTF8
$manifest | ConvertTo-Json -Depth 6 -Compress
