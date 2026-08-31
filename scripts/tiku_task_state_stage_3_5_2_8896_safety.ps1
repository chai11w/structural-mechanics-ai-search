function Invoke-TikuTaskStateStageGit {
    param(
        [Parameter(Mandatory = $true)][string]$ProjectDirectory,
        [Parameter(Mandatory = $true)][string[]]$GitArguments,
        [string]$GitExecutable
    )

    $git = if ($GitExecutable) {
        Resolve-TikuTaskStateStageGit -GitExecutable $GitExecutable
    } else {
        $command = Get-Command git.exe -CommandType Application -ErrorAction Stop |
            Select-Object -First 1
        (Resolve-Path -LiteralPath $command.Source -ErrorAction Stop).Path
    }
    $previousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $output = @(& $git -C $ProjectDirectory @GitArguments 2>$null)
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousPreference
    }
    if ($exitCode -ne 0) {
        throw "Git preflight failed."
    }
    return $output
}

function Resolve-TikuTaskStateStageGit {
    param([Parameter(Mandatory = $true)][string]$GitExecutable)

    if (-not [System.IO.Path]::IsPathRooted($GitExecutable)) {
        throw "The stage Git executable path must be absolute."
    }
    $resolved = (Resolve-Path -LiteralPath $GitExecutable -ErrorAction Stop).Path
    if (-not (Test-Path -LiteralPath $resolved -PathType Leaf)) {
        throw "The stage Git executable is not a file."
    }
    if ([System.IO.Path]::GetFileName($resolved) -ne "git.exe") {
        throw "The stage executable is not an explicit Git executable."
    }
    return $resolved
}

function Assert-TikuTaskStateStageCheckout {
    param(
        [Parameter(Mandatory = $true)][string]$ProjectDirectory,
        [Parameter(Mandatory = $true)][string]$ExpectedCommit,
        [scriptblock]$GitQuery
    )

    if (-not [System.IO.Path]::IsPathRooted($ProjectDirectory)) {
        throw "The stage checkout path must be absolute."
    }
    if ($ExpectedCommit -notmatch '^[0-9a-fA-F]{40}$') {
        throw "The expected stage commit must be a full 40-character Git object ID."
    }
    $project = (Resolve-Path -LiteralPath $ProjectDirectory -ErrorAction Stop).Path
    if (-not (Test-Path -LiteralPath (Join-Path $project ".git") -PathType Leaf)) {
        throw "Stage 3.5.2 must run from a linked Git worktree."
    }
    if (-not $GitQuery) {
        $GitQuery = ${function:Invoke-TikuTaskStateStageGit}
    }

    $topLevel = @(& $GitQuery `
        -ProjectDirectory $project `
        -GitArguments @("rev-parse", "--show-toplevel"))
    if ($topLevel.Count -ne 1) {
        throw "Unable to resolve a unique stage checkout root."
    }
    $resolvedTop = [System.IO.Path]::GetFullPath(([string]$topLevel[0]).Trim())
    if (-not [string]::Equals(
        $resolvedTop,
        $project,
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
        throw "The stage script is not running at the linked worktree root."
    }

    $gitDirectory = @(& $GitQuery `
        -ProjectDirectory $project `
        -GitArguments @("rev-parse", "--path-format=absolute", "--git-dir"))
    $commonDirectory = @(& $GitQuery `
        -ProjectDirectory $project `
        -GitArguments @("rev-parse", "--path-format=absolute", "--git-common-dir"))
    if ($gitDirectory.Count -ne 1 -or $commonDirectory.Count -ne 1) {
        throw "Unable to resolve linked-worktree Git directories."
    }
    if ([string]::Equals(
        [System.IO.Path]::GetFullPath(([string]$gitDirectory[0]).Trim()),
        [System.IO.Path]::GetFullPath(([string]$commonDirectory[0]).Trim()),
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
        throw "Stage 3.5.2 cannot run from the primary checkout."
    }

    $head = @(& $GitQuery `
        -ProjectDirectory $project `
        -GitArguments @("rev-parse", "HEAD"))
    if ($head.Count -ne 1 -or -not [string]::Equals(
        ([string]$head[0]).Trim(),
        $ExpectedCommit,
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
        throw "The stage checkout does not match the expected commit."
    }

    $status = @(& $GitQuery `
        -ProjectDirectory $project `
        -GitArguments @(
            "status", "--porcelain=v1", "--untracked-files=all", "--ignore-submodules=none"
        ))
    if (@($status | Where-Object { ([string]$_).Trim() }).Count -ne 0) {
        throw "The stage checkout is not clean."
    }
    return $project
}

function Resolve-TikuTaskStateStagePython {
    param([Parameter(Mandatory = $true)][string]$PythonExecutable)

    if (-not [System.IO.Path]::IsPathRooted($PythonExecutable)) {
        throw "The stage Python executable path must be absolute."
    }
    $resolved = (Resolve-Path -LiteralPath $PythonExecutable -ErrorAction Stop).Path
    if (-not (Test-Path -LiteralPath $resolved -PathType Leaf)) {
        throw "The stage Python executable is not a file."
    }
    if ([System.IO.Path]::GetFileName($resolved) -notmatch '^python(?:3(?:\.\d+)?)?\.exe$') {
        throw "The stage executable is not an explicit Python executable."
    }
    return $resolved
}

function Resolve-TikuTaskStateStagePowerShell {
    param([Parameter(Mandatory = $true)][string]$PowerShellExecutable)

    if (-not [System.IO.Path]::IsPathRooted($PowerShellExecutable)) {
        throw "The stage PowerShell executable path must be absolute."
    }
    $resolved = (Resolve-Path -LiteralPath $PowerShellExecutable -ErrorAction Stop).Path
    if (-not (Test-Path -LiteralPath $resolved -PathType Leaf)) {
        throw "The stage PowerShell executable is not a file."
    }
    if ([System.IO.Path]::GetFileName($resolved) -notmatch '^(?:powershell|pwsh)\.exe$') {
        throw "The stage executable is not an explicit PowerShell executable."
    }
    return $resolved
}

function Resolve-TikuTaskStateStageRuntimeRoot {
    param(
        [Parameter(Mandatory = $true)][string]$RuntimeRoot,
        [Parameter(Mandatory = $true)][string]$ProjectDirectory,
        [Parameter(Mandatory = $true)][string]$GitCommonDirectory
    )

    if (-not [System.IO.Path]::IsPathRooted($RuntimeRoot)) {
        throw "The stage runtime root must be absolute."
    }
    $root = [System.IO.Path]::GetFullPath($RuntimeRoot).TrimEnd('\', '/')
    $project = [System.IO.Path]::GetFullPath($ProjectDirectory).TrimEnd('\', '/')
    $gitCommon = [System.IO.Path]::GetFullPath($GitCommonDirectory).TrimEnd('\', '/')
    $separator = [System.IO.Path]::DirectorySeparatorChar
    $projectPrefix = $project + $separator
    $gitPrefix = $gitCommon + $separator
    if ([string]::Equals($root, $project, [System.StringComparison]::OrdinalIgnoreCase) -or
        $root.StartsWith($projectPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "The stage runtime root must stay outside the fixed checkout."
    }
    if ([string]::Equals($root, $gitCommon, [System.StringComparison]::OrdinalIgnoreCase) -or
        $root.StartsWith($gitPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "The stage runtime root must stay outside the Git common directory."
    }
    if ([System.IO.Path]::GetFileName($root) -ne ".tmp_tiku_task_state_stage_3_5_2_8896_runs") {
        throw "The stage runtime root is outside the dedicated 3.5.2 namespace."
    }
    if (Test-Path -LiteralPath $root -PathType Leaf) {
        throw "The stage runtime root is a file."
    }
    Assert-TikuTaskStateStageNoReparseAncestors -Path $root
    return $root
}

function Assert-TikuTaskStateStageNoReparseAncestors {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [scriptblock]$ItemQuery
    )

    if (-not $ItemQuery) {
        $ItemQuery = {
            param($Candidate)
            Get-Item -LiteralPath $Candidate -Force -ErrorAction Stop
        }
    }
    $candidate = [System.IO.Path]::GetFullPath($Path).TrimEnd('\', '/')
    while ($candidate) {
        if (Test-Path -LiteralPath $candidate) {
            $item = & $ItemQuery $candidate
            if (-not $item) {
                throw "Unable to inspect the stage runtime path: $candidate"
            }
            if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "The stage runtime path cannot traverse a reparse point: $candidate"
            }
        }
        $parent = [System.IO.Directory]::GetParent($candidate)
        if (-not $parent -or [string]::Equals(
            $parent.FullName,
            $candidate,
            [System.StringComparison]::OrdinalIgnoreCase
        )) {
            break
        }
        $candidate = $parent.FullName
    }
}

function New-TikuTaskStateStageFreshRuntime {
    param([Parameter(Mandatory = $true)][string]$RuntimeRoot)

    Assert-TikuTaskStateStageNoReparseAncestors -Path $RuntimeRoot
    if (-not (Test-Path -LiteralPath $RuntimeRoot)) {
        New-Item -ItemType Directory -Path $RuntimeRoot -ErrorAction Stop | Out-Null
    }
    Assert-TikuTaskStateStageNoReparseAncestors -Path $RuntimeRoot
    $stamp = [datetime]::UtcNow.ToString("yyyyMMddTHHmmssfffZ")
    $suffix = [guid]::NewGuid().ToString("N").Substring(0, 12)
    $runtime = Join-Path $RuntimeRoot "run_${stamp}_$suffix"
    if (Test-Path -LiteralPath $runtime) {
        throw "The generated stage runtime already exists."
    }
    New-Item -ItemType Directory -Path $runtime -ErrorAction Stop | Out-Null
    Assert-TikuTaskStateStageNoReparseAncestors -Path $runtime
    return (Resolve-Path -LiteralPath $runtime -ErrorAction Stop).Path
}

function Get-TikuTaskStateStageChildProcessRecords {
    param(
        [Parameter(Mandatory = $true)][int]$ParentProcessId,
        [scriptblock]$ProcessQuery
    )

    if ($ParentProcessId -le 0) {
        throw "The stage parent process ID is invalid."
    }
    if (-not $ProcessQuery) {
        $ProcessQuery = {
            param($ExpectedParentProcessId)
            Get-CimInstance `
                Win32_Process `
                -Filter "ParentProcessId = $ExpectedParentProcessId" `
                -ErrorAction Stop
        }
    }
    try {
        $records = @(& $ProcessQuery $ParentProcessId)
    } catch {
        throw "Unable to enumerate stage child processes: $($_.Exception.Message)"
    }
    $children = foreach ($record in $records) {
        $processId = 0
        $recordedParent = 0
        if (-not $record -or
            -not [int]::TryParse([string]$record.ProcessId, [ref]$processId) -or
            -not [int]::TryParse([string]$record.ParentProcessId, [ref]$recordedParent) -or
            $processId -le 0) {
            throw "Stage child process evidence is malformed."
        }
        if ($recordedParent -ne $ParentProcessId) {
            continue
        }
        $record
    }
    return @($children)
}

function Get-TikuTaskStateStageChildProcessIds {
    param(
        [Parameter(Mandatory = $true)][int]$ParentProcessId,
        [scriptblock]$ProcessQuery
    )

    $records = if ($ProcessQuery) {
        @(Get-TikuTaskStateStageChildProcessRecords `
            -ParentProcessId $ParentProcessId `
            -ProcessQuery $ProcessQuery)
    } else {
        @(Get-TikuTaskStateStageChildProcessRecords `
            -ParentProcessId $ParentProcessId)
    }
    return @(
        $records |
            ForEach-Object { [int]$_.ProcessId } |
            Sort-Object -Unique
    )
}

function Get-TikuTaskStateStageExactChildProcessIds {
    param(
        [Parameter(Mandatory = $true)][int]$ParentProcessId,
        [Parameter(Mandatory = $true)][string]$ExpectedExecutablePath,
        [Parameter(Mandatory = $true)][string[]]$ExpectedArguments,
        [scriptblock]$ProcessQuery
    )

    $records = if ($ProcessQuery) {
        @(Get-TikuTaskStateStageChildProcessRecords `
            -ParentProcessId $ParentProcessId `
            -ProcessQuery $ProcessQuery)
    } else {
        @(Get-TikuTaskStateStageChildProcessRecords `
            -ParentProcessId $ParentProcessId)
    }
    $matches = foreach ($record in $records) {
        $processId = [int]$record.ProcessId
        if (Test-WatchdogLaunchEvidence `
            -ProcessId $processId `
            -ExecutablePath ([string]$record.ExecutablePath) `
            -ExpectedExecutablePath $ExpectedExecutablePath `
            -CommandLine ([string]$record.CommandLine) `
            -ExpectedArguments $ExpectedArguments) {
            $processId
        }
    }
    return @($matches | Sort-Object -Unique)
}

function Get-TikuTaskStateStagePidFileValue {
    param([Parameter(Mandatory = $true)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Stage PID file is missing: $Path"
    }
    $raw = ([string](Get-Content -LiteralPath $Path -Raw -ErrorAction Stop)).Trim()
    $processId = 0
    if (-not [int]::TryParse($raw, [ref]$processId) -or $processId -le 0) {
        throw "Stage PID file is malformed: $Path"
    }
    return $processId
}

function Stop-TikuTaskStateStageExactProcess {
    param(
        [Parameter(Mandatory = $true)][int]$ProcessId,
        [Parameter(Mandatory = $true)][string]$Role,
        [Parameter(Mandatory = $true)][scriptblock]$IdentityProbe
    )

    $process = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if (-not $process -or $process.HasExited) {
        return
    }
    if (-not (& $IdentityProbe $ProcessId)) {
        throw "Refusing to stop an unverified $Role process PID $ProcessId."
    }
    $process = Get-Process -Id $ProcessId -ErrorAction Stop
    if (-not (& $IdentityProbe $ProcessId)) {
        throw "The $Role process identity changed before stop."
    }
    Stop-Process -InputObject $process -Force -ErrorAction Stop
    Wait-Process -InputObject $process -Timeout 10 -ErrorAction SilentlyContinue
    if (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue) {
        throw "The verified $Role process did not stop."
    }
}
