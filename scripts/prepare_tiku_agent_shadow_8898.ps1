$ErrorActionPreference = "Stop"

$ProjectDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$ShadowRoot = Join-Path $ProjectDir ".shadow_8898"
$SourceDir = Join-Path $ShadowRoot "src"
$StagingDir = Join-Path $ShadowRoot "src.next"
$PreviousDir = Join-Path $ShadowRoot "src.previous"
$Port = 8898
$OutputCommits = @("5eacf7d", "5731e31", "3698d62", "99934cb")

function Assert-ShadowPath {
    param([string]$Path)
    $resolved = [System.IO.Path]::GetFullPath($Path)
    $prefix = [System.IO.Path]::GetFullPath($ShadowRoot) + [System.IO.Path]::DirectorySeparatorChar
    if (-not $resolved.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to modify a path outside the 8898 shadow root: $resolved"
    }
}

$listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($listener) {
    throw "Stop the 8898 shadow process before refreshing its source snapshot."
}

foreach ($commit in $OutputCommits) {
    git -C $ProjectDir merge-base --is-ancestor $commit HEAD
    if ($LASTEXITCODE -ne 0) {
        throw "Required output-layer commit is not present in the current baseline: $commit"
    }
}

New-Item -ItemType Directory -Force -Path $ShadowRoot | Out-Null
foreach ($path in @($StagingDir, $PreviousDir)) {
    Assert-ShadowPath $path
    if (Test-Path -LiteralPath $path) {
        Remove-Item -LiteralPath $path -Recurse -Force
    }
}
New-Item -ItemType Directory -Force -Path $StagingDir | Out-Null

$tracked = git -C $ProjectDir ls-files
if ($LASTEXITCODE -ne 0) {
    throw "Unable to list tracked project files."
}
$selected = @(
    $tracked | Where-Object {
        $_ -match "^[^/]+\.py$" -or
        $_ -match "^(scripts|tiku_agent|tiku_shared|tiku_admin)/" -or
        $_ -in @(
            "experiments/complex_image_eval/observation_prompt_scratch.md",
            "experiments/complex_image_eval/observation_prompt_8897_boundary_v1.md"
        )
    }
)
$untrackedEntrypoint = "scripts/run_tiku_agent_8898.py"
if ((Test-Path -LiteralPath (Join-Path $ProjectDir $untrackedEntrypoint)) -and
    $untrackedEntrypoint -notin $selected) {
    $selected += $untrackedEntrypoint
}

foreach ($relative in ($selected | Sort-Object -Unique)) {
    $source = Join-Path $ProjectDir ($relative -replace "/", "\")
    if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
        throw "Snapshot source file is missing: $relative"
    }
    $destination = Join-Path $StagingDir ($relative -replace "/", "\")
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $destination) | Out-Null
    Copy-Item -LiteralPath $source -Destination $destination -Force
}

foreach ($forbidden in @("config.json", "config.local.json", ".env")) {
    if (Test-Path -LiteralPath (Join-Path $StagingDir $forbidden)) {
        throw "Sensitive local configuration entered the shadow snapshot: $forbidden"
    }
}

$metadata = [ordered]@{
    schema = "tiku-agent-shadow-source-v1"
    prepared_at = (Get-Date).ToUniversalTime().ToString("o")
    baseline_commit = (git -C $ProjectDir rev-parse HEAD).Trim()
    output_layer_commits = $OutputCommits
    copied_file_count = @($selected | Sort-Object -Unique).Count
    includes_worktree_content = $true
    excludes = @(
        "local configuration and secrets",
        "tests and documentation",
        "question-bank assets",
        "runtime state and logs"
    )
}
$metadata | ConvertTo-Json -Depth 5 | Set-Content `
    -LiteralPath (Join-Path $StagingDir "shadow_source.json") `
    -Encoding UTF8

if (Test-Path -LiteralPath $SourceDir) {
    Assert-ShadowPath $SourceDir
    Move-Item -LiteralPath $SourceDir -Destination $PreviousDir
}
Move-Item -LiteralPath $StagingDir -Destination $SourceDir
if (Test-Path -LiteralPath $PreviousDir) {
    Remove-Item -LiteralPath $PreviousDir -Recurse -Force
}

Write-Output "SOURCE=$SourceDir"
Write-Output "FILES=$($metadata.copied_file_count)"
Write-Output "BASELINE=$($metadata.baseline_commit)"
