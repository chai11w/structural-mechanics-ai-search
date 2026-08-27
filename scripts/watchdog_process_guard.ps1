function ConvertTo-WatchdogCommandLineTokens {
    param([Parameter(Mandatory = $true)][string]$CommandLine)

    $pattern = '"([^"]*)"|''([^'']*)''|(\S+)'
    foreach ($match in [regex]::Matches($CommandLine, $pattern)) {
        if ($match.Groups[1].Success) {
            $match.Groups[1].Value
        } elseif ($match.Groups[2].Success) {
            $match.Groups[2].Value
        } else {
            $match.Groups[3].Value
        }
    }
}

function Resolve-WatchdogExecutablePath {
    param([Parameter(Mandatory = $true)][string]$Executable)

    if ([System.IO.Path]::IsPathRooted($Executable)) {
        return (Resolve-Path -LiteralPath $Executable -ErrorAction Stop).Path
    }
    $command = Get-Command $Executable -CommandType Application -ErrorAction Stop |
        Select-Object -First 1
    if (-not $command -or -not $command.Source) {
        throw "Executable could not be resolved: $Executable"
    }
    return (Resolve-Path -LiteralPath $command.Source -ErrorAction Stop).Path
}

function Enter-WatchdogInstanceLock {
    param([Parameter(Mandatory = $true)][int]$Port)

    if ($Port -le 0 -or $Port -gt 65535) {
        throw "Invalid watchdog port: $Port"
    }

    $mutexName = "Global\TikuQuestionBank.Watchdog.Port.$Port"
    $mutex = [System.Threading.Mutex]::new($false, $mutexName)
    $acquired = $false
    try {
        try {
            $acquired = $mutex.WaitOne(0)
        } catch [System.Threading.AbandonedMutexException] {
            # The previous owner exited without releasing the mutex. WaitOne has
            # transferred ownership to this process, so the stale lock is safe.
            $acquired = $true
        }
        if (-not $acquired) {
            throw "Another watchdog already owns the instance lock for port $Port."
        }
        return $mutex
    } catch {
        if (-not $acquired) {
            $mutex.Dispose()
        }
        throw
    }
}

function Exit-WatchdogInstanceLock {
    param([Parameter(Mandatory = $true)][System.Threading.Mutex]$Mutex)

    try {
        $Mutex.ReleaseMutex()
    } finally {
        $Mutex.Dispose()
    }
}

function Remove-WatchdogPidFileIfOwned {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][int]$OwnerProcessId
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return
    }
    try {
        $recordedOwner = ([string](Get-Content -LiteralPath $Path -Raw -ErrorAction Stop)).Trim()
        if ($recordedOwner -eq [string]$OwnerProcessId) {
            Remove-Item -LiteralPath $Path -Force -ErrorAction Stop
        }
    } catch [System.Management.Automation.ItemNotFoundException] {
        # A missing file is already clean. Other errors must remain visible.
    }
}

function Assert-WatchdogPidFileAvailable {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][int]$OwnerProcessId
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return
    }
    $raw = ([string](Get-Content -LiteralPath $Path -Raw -ErrorAction Stop)).Trim()
    $recordedOwner = 0
    if (-not [int]::TryParse($raw, [ref]$recordedOwner) -or $recordedOwner -le 0) {
        throw "Watchdog PID file is malformed and was not overwritten: $Path"
    }
    if ($recordedOwner -eq $OwnerProcessId) {
        return
    }
    $existing = Get-Process -Id $recordedOwner -ErrorAction SilentlyContinue
    if ($existing -and -not $existing.HasExited) {
        throw "Existing watchdog PID $recordedOwner is still alive; PID file was not overwritten."
    }
}

function Get-WatchdogListeningProcessIds {
    param([Parameter(Mandatory = $true)][int]$Port)

    $processIds = @()
    try {
        $processIds += Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop |
            Select-Object -ExpandProperty OwningProcess -Unique
    } catch {
        # Fall back to netstat on hosts where Get-NetTCPConnection is unavailable.
    }
    if (-not $processIds) {
        $escapedPort = [regex]::Escape([string]$Port)
        $pattern = "^\s*TCP\s+\S+:${escapedPort}\s+\S+\s+LISTENING\s+(\d+)\s*$"
        $processIds += netstat -ano 2>$null |
            ForEach-Object {
                $match = [regex]::Match($_, $pattern)
                if ($match.Success) {
                    [int]$match.Groups[1].Value
                }
            }
    }
    return @(
        $processIds |
            Where-Object { $_ -and [int]$_ -gt 0 } |
            ForEach-Object { [int]$_ } |
            Sort-Object -Unique
    )
}

function Test-WatchdogLaunchEvidence {
    param(
        [Parameter(Mandatory = $true)][int]$ProcessId,
        [Parameter(Mandatory = $true)][string]$ExecutablePath,
        [Parameter(Mandatory = $true)][string]$ExpectedExecutablePath,
        [Parameter(Mandatory = $true)][string]$CommandLine,
        [Parameter(Mandatory = $true)][string[]]$ExpectedArguments
    )

    if ($ProcessId -le 0) {
        return $false
    }
    if (-not $ExecutablePath -or -not $ExpectedExecutablePath) {
        return $false
    }
    try {
        $actualExecutable = [System.IO.Path]::GetFullPath($ExecutablePath)
        $expectedExecutable = [System.IO.Path]::GetFullPath($ExpectedExecutablePath)
    } catch {
        return $false
    }
    if (-not [string]::Equals(
        $actualExecutable,
        $expectedExecutable,
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
        return $false
    }

    $tokens = @(ConvertTo-WatchdogCommandLineTokens -CommandLine $CommandLine)
    if ($tokens.Count -ne ($ExpectedArguments.Count + 1)) {
        return $false
    }
    for ($index = 0; $index -lt $ExpectedArguments.Count; $index += 1) {
        if (-not [string]::Equals(
            [string]$tokens[$index + 1],
            [string]$ExpectedArguments[$index],
            [System.StringComparison]::OrdinalIgnoreCase
        )) {
            return $false
        }
    }
    return $true
}

function Test-WatchdogProcessEvidence {
    param(
        [Parameter(Mandatory = $true)][int]$ProcessId,
        [Parameter(Mandatory = $true)][int[]]$ListeningProcessIds,
        [Parameter(Mandatory = $true)][string]$ExecutablePath,
        [Parameter(Mandatory = $true)][string]$ExpectedExecutablePath,
        [Parameter(Mandatory = $true)][string]$CommandLine,
        [Parameter(Mandatory = $true)][string[]]$ExpectedArguments
    )

    $owners = @(
        $ListeningProcessIds |
            Where-Object { $_ -and [int]$_ -gt 0 } |
            ForEach-Object { [int]$_ } |
            Sort-Object -Unique
    )
    if ($owners.Count -ne 1 -or $owners[0] -ne $ProcessId) {
        return $false
    }
    return Test-WatchdogLaunchEvidence `
        -ProcessId $ProcessId `
        -ExecutablePath $ExecutablePath `
        -ExpectedExecutablePath $ExpectedExecutablePath `
        -CommandLine $CommandLine `
        -ExpectedArguments $ExpectedArguments
}

function Test-WatchdogLaunchIdentity {
    param(
        [Parameter(Mandatory = $true)][int]$ProcessId,
        [Parameter(Mandatory = $true)][string]$ExpectedExecutablePath,
        [Parameter(Mandatory = $true)][string[]]$ExpectedArguments
    )

    try {
        $record = Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction Stop
        if (-not $record) {
            return $false
        }
        return Test-WatchdogLaunchEvidence `
            -ProcessId $ProcessId `
            -ExecutablePath ([string]$record.ExecutablePath) `
            -ExpectedExecutablePath $ExpectedExecutablePath `
            -CommandLine ([string]$record.CommandLine) `
            -ExpectedArguments $ExpectedArguments
    } catch {
        return $false
    }
}

function Test-WatchdogProcessMatch {
    param(
        [Parameter(Mandatory = $true)][int]$ProcessId,
        [Parameter(Mandatory = $true)][int]$Port,
        [Parameter(Mandatory = $true)][string]$ExpectedExecutablePath,
        [Parameter(Mandatory = $true)][string[]]$ExpectedArguments
    )

    try {
        $owners = @(Get-WatchdogListeningProcessIds -Port $Port)
        $record = Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction Stop
        if (-not $record) {
            return $false
        }
        return Test-WatchdogProcessEvidence `
            -ProcessId $ProcessId `
            -ListeningProcessIds $owners `
            -ExecutablePath ([string]$record.ExecutablePath) `
            -ExpectedExecutablePath $ExpectedExecutablePath `
            -CommandLine ([string]$record.CommandLine) `
            -ExpectedArguments $ExpectedArguments
    } catch {
        return $false
    }
}

function Get-WatchdogManagedProcess {
    param(
        [Parameter(Mandatory = $true)][int]$Port,
        [Parameter(Mandatory = $true)][string]$ExpectedExecutablePath,
        [Parameter(Mandatory = $true)][string[]]$ExpectedArguments
    )

    $owners = @(Get-WatchdogListeningProcessIds -Port $Port)
    if ($owners.Count -eq 0) {
        return $null
    }
    if ($owners.Count -ne 1) {
        throw "Refusing to manage port $Port because it has multiple listening owners."
    }
    $candidateId = [int]$owners[0]
    if (-not (Test-WatchdogProcessMatch `
        -ProcessId $candidateId `
        -Port $Port `
        -ExpectedExecutablePath $ExpectedExecutablePath `
        -ExpectedArguments $ExpectedArguments
    )) {
        throw "Refusing to manage unverified process PID $candidateId on port $Port."
    }
    return Get-Process -Id $candidateId -ErrorAction Stop
}

function Wait-WatchdogProcessReady {
    param(
        [Parameter(Mandatory = $true)]$Process,
        [Parameter(Mandatory = $true)][scriptblock]$HealthProbe,
        [Parameter(Mandatory = $true)][scriptblock]$FullMatchProbe,
        [Parameter(Mandatory = $true)][scriptblock]$LaunchIdentityProbe,
        [int]$TimeoutSeconds = 30,
        [int]$PollSeconds = 1,
        [scriptblock]$SleepAction = { param($Seconds) Start-Sleep -Seconds $Seconds }
    )

    $timeout = [Math]::Max(1, $TimeoutSeconds)
    $poll = [Math]::Max(1, $PollSeconds)
    $elapsed = 0
    while ($elapsed -lt $timeout) {
        if ($Process.HasExited) {
            return "exited"
        }
        if ((& $FullMatchProbe) -and (& $HealthProbe)) {
            return "ready"
        }
        & $SleepAction $poll
        $elapsed += $poll
    }
    if ($Process.HasExited) {
        return "exited"
    }
    if (& $LaunchIdentityProbe) {
        return "timeout_verified"
    }
    return "timeout_unverified"
}
