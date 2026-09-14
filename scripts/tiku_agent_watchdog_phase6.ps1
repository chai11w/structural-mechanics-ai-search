param([Parameter(Mandatory=$true)][string]$ManifestPath, [switch]$CheckOnly)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'watchdog_process_guard.ps1')
. (Join-Path $PSScriptRoot 'tiku_agent_watchdog_8896_safety.ps1')

$manifest = Get-Content -LiteralPath $ManifestPath -Raw | ConvertFrom-Json
$project = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
if ($manifest.schema -ne 1 -or $manifest.port -ne 8898 -or $manifest.release -ne $project) {
    throw 'Manifest must identify this fixed release and separate port 8898.'
}
$revision = (& git -C $project rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or $revision -ne $manifest.revision) { throw 'Release revision mismatch.' }
& git -C $project diff --quiet HEAD -- scripts tiku_agent tiku_admin tiku_shared tiku_diagnostics
if ($LASTEXITCODE -ne 0) { throw 'Tracked release sources are modified.' }
$runtime = (Resolve-Path -LiteralPath $manifest.runtime).Path
if ((Split-Path $runtime -Leaf) -ne '.tmp_tiku_agent_phase6_8898') { throw 'Unexpected runtime.' }
$python = Resolve-WatchdogExecutablePath -Executable $manifest.python
$arguments = @('-B', (Join-Path $project 'scripts/run_tiku_agent_phase6.py'), '--host', '127.0.0.1', '--port', '8898', '--runtime-dir', $runtime)
$allowed = @('--max-checkpoint-rows','--max-artifact-rows','--max-audit-rows','--max-trace-rows','--max-artifact-bytes','--min-free-bytes','--max-artifacts-per-checkpoint','--checkpoint-retention-backup-root','--checkpoint-retention-interval-seconds','--checkpoint-retention-backup-keep-runs','--checkpoint-code-revision')
foreach ($property in $manifest.options.PSObject.Properties) {
    if ($property.Name -notin $allowed) { throw 'Unexpected launch option.' }
    $arguments += @($property.Name, [string]$property.Value)
}
$arguments += @('--enable-a2-checkpoint-capture', '--enable-a3-checkpoint-capture')
$launch = @($arguments | ForEach-Object { ConvertTo-Tiku8896CommandLineArgument -Argument ([string]$_) })
if ($CheckOnly) { Write-Output 'Verified fixed release, isolated runtime and launch options.'; return }
$logs = Join-Path $runtime 'service_logs'
New-Item -ItemType Directory -Force -Path $logs | Out-Null
$lock = $null
try {
    $lock = Enter-WatchdogInstanceLock -Port 8898
    Assert-WatchdogPidFileAvailable -Path (Join-Path $logs 'watchdog.pid') -OwnerProcessId $PID
    Set-Content -LiteralPath (Join-Path $logs 'watchdog.pid') -Value $PID
    while ($true) {
        $owners = @(Get-NetTCPConnection -State Listen -LocalPort 8898 -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique)
        if ($owners.Count -gt 1) { throw 'Multiple port owners; refusing to act.' }
        if ($owners.Count -eq 1) {
            $record = Get-CimInstance Win32_Process -Filter "ProcessId = $($owners[0])"
            if (-not (Test-WatchdogProcessEvidence -ProcessId $record.ProcessId -ListeningProcessIds $owners -ExecutablePath $record.ExecutablePath -ExpectedExecutablePath $python -CommandLine $record.CommandLine -ExpectedArguments $arguments)) {
                throw 'Foreign listener; refusing to adopt, stop or replace it.'
            }
            Set-Content -LiteralPath (Join-Path $logs 'agent.pid') -Value $record.ProcessId
            try {
                $health = Invoke-RestMethod 'http://127.0.0.1:8898/health' -TimeoutSec 3
                if (-not $health.ok -and $health.status -ne 'ok') { throw 'Health unavailable' }
                Add-Content -LiteralPath (Join-Path $logs 'watchdog.status') -Value "$(Get-Date -Format o) healthy pid=$($record.ProcessId)"
            } catch {
                # A slow provider must not cause an automatic hard kill and UNKNOWN cost.
                Add-Content -LiteralPath (Join-Path $logs 'watchdog.status') -Value "$(Get-Date -Format o) health unavailable; preserving verified process"
            }
        } else {
            if ($candidate -and -not $candidate.HasExited) { throw 'Verified child lost listener; preserving process for inspection.' }
            $candidate = Start-Process $python -ArgumentList $launch -WorkingDirectory $project -WindowStyle Hidden -RedirectStandardOutput (Join-Path $logs 'agent.out.log') -RedirectStandardError (Join-Path $logs 'agent.err.log') -PassThru
            Add-Content -LiteralPath (Join-Path $logs 'watchdog.status') -Value "$(Get-Date -Format o) started pid=$($candidate.Id)"
        }
        Start-Sleep -Seconds 20
    }
} finally {
    Remove-WatchdogPidFileIfOwned -Path (Join-Path $logs 'watchdog.pid') -OwnerProcessId $PID
    if ($lock) { Exit-WatchdogInstanceLock -Mutex $lock }
}
