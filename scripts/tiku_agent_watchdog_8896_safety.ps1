function Resolve-Tiku8896RuntimeDirectory {
    param(
        [Parameter(Mandatory = $true)][string]$RuntimeDirectory,
        [Parameter(Mandatory = $true)][string]$ProjectDirectory
    )

    $candidate = if ([System.IO.Path]::IsPathRooted($RuntimeDirectory)) {
        $RuntimeDirectory
    } else {
        Join-Path $ProjectDirectory $RuntimeDirectory
    }
    if ($candidate.IndexOf([char]0) -ge 0 -or $candidate.Contains('"')) {
        throw "The 8896 runtime directory contains an invalid character."
    }
    $fullPath = [System.IO.Path]::GetFullPath($candidate)
    $rootPath = [System.IO.Path]::GetPathRoot($fullPath)
    $separators = [char[]]@('\', '/')
    $normalized = $fullPath.TrimEnd($separators)
    $normalizedRoot = $rootPath.TrimEnd($separators)
    if ([string]::Equals(
        $normalized,
        $normalizedRoot,
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
        throw "The 8896 runtime directory cannot be a filesystem root."
    }
    return $normalized
}

function ConvertTo-Tiku8896CommandLineArgument {
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

function Get-Tiku8896ListeningProcessIds {
    param(
        [Parameter(Mandatory = $true)][int]$Port,
        [scriptblock]$PrimaryQuery,
        [scriptblock]$FallbackQuery
    )

    if ($Port -ne 8896) {
        throw "The 8896 listener guard cannot inspect port $Port."
    }
    if (-not $PrimaryQuery) {
        $PrimaryQuery = {
            param($QueryPort)
            Get-NetTCPConnection `
                -LocalPort $QueryPort `
                -State Listen `
                -ErrorAction Stop |
                Select-Object -ExpandProperty OwningProcess -Unique
        }
    }
    try {
        $primaryIds = @(& $PrimaryQuery $Port)
        return @(
            $primaryIds |
                Where-Object { $_ -and [int]$_ -gt 0 } |
                ForEach-Object { [int]$_ } |
                Sort-Object -Unique
        )
    } catch {
        $primaryFailure = $_.Exception.Message
    }

    if (-not $FallbackQuery) {
        $FallbackQuery = {
            param($QueryPort)
            $netstat = Get-Command netstat.exe -CommandType Application -ErrorAction Stop |
                Select-Object -First 1
            $lines = @(& $netstat.Source -ano 2>$null)
            if ($LASTEXITCODE -ne 0) {
                throw "netstat exited with code $LASTEXITCODE."
            }
            return $lines
        }
    }
    try {
        $netstatLines = @(& $FallbackQuery $Port)
    } catch {
        throw "Unable to verify port 8896 ownership. Primary: $primaryFailure Fallback: $($_.Exception.Message)"
    }

    $escapedPort = [regex]::Escape([string]$Port)
    $pattern = "^\s*TCP\s+\S+:${escapedPort}\s+\S+\s+LISTENING\s+(\d+)\s*$"
    $fallbackIds = foreach ($line in $netstatLines) {
        $match = [regex]::Match([string]$line, $pattern)
        if ($match.Success) {
            [int]$match.Groups[1].Value
        }
    }
    return @(
        $fallbackIds |
            Where-Object { $_ -and [int]$_ -gt 0 } |
            Sort-Object -Unique
    )
}

function Get-Tiku8896ScopedListeningProcessIds {
    param(
        [Parameter(Mandatory = $true)][int]$Port,
        [scriptblock]$PrimaryQuery
    )

    $blockedFallback = {
        param($QueryPort)
        throw "Broad listener fallback is disabled for scoped port $QueryPort verification."
    }
    if ($PrimaryQuery) {
        return @(
            Get-Tiku8896ListeningProcessIds `
                -Port $Port `
                -PrimaryQuery $PrimaryQuery `
                -FallbackQuery $blockedFallback
        )
    }
    return @(
        Get-Tiku8896ListeningProcessIds `
            -Port $Port `
            -FallbackQuery $blockedFallback
    )
}
