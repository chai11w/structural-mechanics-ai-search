function ConvertTo-Tiku8790CommandLineArgument {
    param(
        [Parameter(Mandatory = $true)]
        [AllowEmptyString()]
        [string]$Argument
    )

    if ($Argument.IndexOf([char]0) -ge 0) {
        throw "A process argument cannot contain a null character."
    }
    if ($Argument.Length -gt 0 -and $Argument -notmatch '[\s"]') {
        return $Argument
    }

    # Start-Process joins ArgumentList before CreateProcess. Encode each item
    # with the Windows argv rules so spaces cannot create extra switches.
    $builder = [System.Text.StringBuilder]::new()
    [void]$builder.Append('"')
    $backslashes = 0
    foreach ($character in $Argument.ToCharArray()) {
        if ($character -eq '\') {
            $backslashes += 1
            continue
        }
        if ($character -eq '"') {
            [void]$builder.Append(('\' * (($backslashes * 2) + 1)))
            [void]$builder.Append('"')
            $backslashes = 0
            continue
        }
        if ($backslashes -gt 0) {
            [void]$builder.Append(('\' * $backslashes))
            $backslashes = 0
        }
        [void]$builder.Append($character)
    }
    if ($backslashes -gt 0) {
        [void]$builder.Append(('\' * ($backslashes * 2)))
    }
    [void]$builder.Append('"')
    return $builder.ToString()
}

function Resolve-Tiku8790ManifestPath {
    param(
        [Parameter(Mandatory = $true)][string]$Value,
        [Parameter(Mandatory = $true)][string]$BaseDirectory
    )

    if ($Value.IndexOf([char]0) -ge 0 -or $Value.Contains('"')) {
        throw "The 8790 release path contains an invalid character."
    }
    $candidate = if ([System.IO.Path]::IsPathRooted($Value)) {
        $Value
    } else {
        Join-Path $BaseDirectory $Value
    }
    return [System.IO.Path]::GetFullPath($candidate)
}

function Invoke-Tiku8790ReleaseGit {
    param(
        [Parameter(Mandatory = $true)][string]$ProjectDirectory,
        [Parameter(Mandatory = $true)][string[]]$GitArguments
    )

    $command = Get-Command git.exe -CommandType Application -ErrorAction Stop |
        Select-Object -First 1
    if (-not $command -or -not $command.Source) {
        throw "Git executable could not be resolved for 8790 release verification."
    }
    $logicalArguments = @(
        "-c",
        "core.quotePath=false",
        "-C",
        $ProjectDirectory
    ) + @($GitArguments)
    $encodedArguments = @(
        $logicalArguments |
            ForEach-Object {
                ConvertTo-Tiku8790CommandLineArgument -Argument ([string]$_)
            }
    )
    $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $command.Source
    $startInfo.Arguments = $encodedArguments -join " "
    $startInfo.WorkingDirectory = $ProjectDirectory
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $utf8 = [System.Text.UTF8Encoding]::new($false)
    $startInfo.StandardOutputEncoding = $utf8
    $startInfo.StandardErrorEncoding = $utf8
    $startInfo.EnvironmentVariables["GIT_OPTIONAL_LOCKS"] = "0"
    $process = [System.Diagnostics.Process]::new()
    $process.StartInfo = $startInfo
    try {
        if (-not $process.Start()) {
            throw "8790 Git release identity verification failed to start."
        }
        $standardOutput = $process.StandardOutput.ReadToEnd()
        [void]$process.StandardError.ReadToEnd()
        $process.WaitForExit()
        $exitCode = $process.ExitCode
    } finally {
        $process.Dispose()
    }
    if ($exitCode -ne 0) {
        throw "8790 Git release identity verification failed."
    }
    return @(
        $standardOutput -split "\r?\n" |
            Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
    )
}

function Assert-Tiku8790ReleaseIdentity {
    param(
        [Parameter(Mandatory = $true)][string]$ManifestPath,
        [Parameter(Mandatory = $true)][string]$ExpectedCommit,
        [Parameter(Mandatory = $true)][string]$ProjectDirectory,
        [Parameter(Mandatory = $true)][string]$AgentEntrypoint,
        [Parameter(Mandatory = $true)][string]$PythonExecutable,
        [Parameter(Mandatory = $true)][string]$RuntimeDirectory,
        [scriptblock]$GitQuery
    )

    $project = [System.IO.Path]::GetFullPath($ProjectDirectory)
    $entrypoint = [System.IO.Path]::GetFullPath($AgentEntrypoint)
    $python = [System.IO.Path]::GetFullPath($PythonExecutable)
    $runtime = [System.IO.Path]::GetFullPath($RuntimeDirectory)
    if (-not (Test-Path -LiteralPath (Join-Path $project ".git") -PathType Leaf)) {
        throw "8790 release must run from a linked Git worktree."
    }
    $resolvedManifest = Resolve-Tiku8790ManifestPath `
        -Value $ManifestPath `
        -BaseDirectory $project
    if (-not (Test-Path -LiteralPath $resolvedManifest -PathType Leaf)) {
        throw "8790 release manifest not found: $resolvedManifest"
    }
    try {
        $manifest = Get-Content -LiteralPath $resolvedManifest -Raw -Encoding UTF8 |
            ConvertFrom-Json -ErrorAction Stop
    } catch {
        throw "8790 release manifest is not valid JSON: $resolvedManifest"
    }
    if ([string]$manifest.schema -ne "tiku-agent-8790-release-v1") {
        throw "8790 release manifest schema is not supported."
    }

    $manifestCommit = if ($manifest -and $manifest.commit) {
        [string]$manifest.commit
    } elseif ($manifest -and $manifest.expected_commit) {
        [string]$manifest.expected_commit
    } else {
        ""
    }
    if ($ExpectedCommit -notmatch '^[0-9a-fA-F]{40}$') {
        throw "8790 expected commit must be a full 40-character Git object ID."
    }
    if ($manifest -and -not $manifestCommit) {
        throw "8790 release manifest is missing a release commit."
    }
    if ($manifestCommit -and $manifestCommit -notmatch '^[0-9a-fA-F]{40}$') {
        throw "8790 release manifest commit must be a full 40-character Git object ID."
    }
    if ($manifestCommit -and -not [string]::Equals(
        $ExpectedCommit,
        $manifestCommit,
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
        throw "8790 expected commit does not match the release manifest."
    }
    $commit = $ExpectedCommit

    if ($manifest) {
        $manifestCheckout = [string]$manifest.checkout
        $manifestEntrypoint = [string]$manifest.agent_entrypoint
        $manifestPython = if ($manifest.python) {
            [string]$manifest.python
        } elseif ($manifest.python_executable) {
            [string]$manifest.python_executable
        } else {
            ""
        }
        $manifestRuntime = [string]$manifest.runtime
        if (-not $manifestCheckout -or -not $manifestEntrypoint -or
            -not $manifestPython -or -not $manifestRuntime) {
            throw "8790 release manifest is missing checkout, agent_entrypoint, python, or runtime."
        }
        $comparisons = @(
            @(
                (Resolve-Tiku8790ManifestPath -Value $manifestCheckout -BaseDirectory $project),
                $project,
                "checkout"
            ),
            @(
                (Resolve-Tiku8790ManifestPath -Value $manifestEntrypoint -BaseDirectory $project),
                $entrypoint,
                "agent entrypoint"
            ),
            @(
                (Resolve-Tiku8790ManifestPath -Value $manifestPython -BaseDirectory $project),
                $python,
                "Python executable"
            ),
            @(
                (Resolve-Tiku8790ManifestPath -Value $manifestRuntime -BaseDirectory $project),
                $runtime,
                "runtime"
            )
        )
        foreach ($comparison in $comparisons) {
            if (-not [string]::Equals(
                [string]$comparison[0],
                [string]$comparison[1],
                [System.StringComparison]::OrdinalIgnoreCase
            )) {
                throw "8790 release manifest $($comparison[2]) does not match the watchdog."
            }
        }
    }

    if (-not $GitQuery) {
        $GitQuery = ${function:Invoke-Tiku8790ReleaseGit}
    }
    $topLevel = @(& $GitQuery `
        -ProjectDirectory $project `
        -GitArguments @("rev-parse", "--show-toplevel"))
    if ($topLevel.Count -ne 1 -or -not [string]::Equals(
        [System.IO.Path]::GetFullPath(([string]$topLevel[0]).Trim()),
        $project,
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
        throw "8790 release script is not running from the expected checkout root."
    }
    $gitDirectory = @(& $GitQuery `
        -ProjectDirectory $project `
        -GitArguments @("rev-parse", "--path-format=absolute", "--git-dir"))
    $commonDirectory = @(& $GitQuery `
        -ProjectDirectory $project `
        -GitArguments @("rev-parse", "--path-format=absolute", "--git-common-dir"))
    if ($gitDirectory.Count -ne 1 -or $commonDirectory.Count -ne 1 -or
        [string]::Equals(
            [System.IO.Path]::GetFullPath(([string]$gitDirectory[0]).Trim()),
            [System.IO.Path]::GetFullPath(([string]$commonDirectory[0]).Trim()),
            [System.StringComparison]::OrdinalIgnoreCase
        )) {
        throw "8790 release must use an isolated linked Git worktree."
    }
    $head = @(& $GitQuery `
        -ProjectDirectory $project `
        -GitArguments @("rev-parse", "--verify", "HEAD"))
    if ($head.Count -ne 1 -or -not [string]::Equals(
        ([string]$head[0]).Trim(),
        $commit,
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
        throw "8790 checkout HEAD does not match the expected release commit."
    }
    $status = @(& $GitQuery `
        -ProjectDirectory $project `
        -GitArguments @(
            "status", "--porcelain=v1", "--untracked-files=all", "--ignore-submodules=none"
        ))
    if (@($status | Where-Object { ([string]$_).Trim() }).Count -ne 0) {
        throw "8790 release checkout is not clean."
    }

    return [pscustomobject]@{
        commit = $commit.ToLowerInvariant()
        checkout = $project
        agent_entrypoint = $entrypoint
        python = $python
        runtime = $runtime
        manifest = $resolvedManifest
    }
}
