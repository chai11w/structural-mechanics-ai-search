param(
    [switch]$Apply,
    [int]$CurrentWatchdogPid = 0,
    [int]$Port = 8790,
    [string]$RuntimeDir,
    [string]$ControlDb,
    [string]$LegacyInviteConfig,
    [double]$RollbackDailyBudgetCny = 30,
    [double]$RollbackPerInviteDailyBudgetCny = 3,
    [string]$PythonExe = "C:\Users\31492\AppData\Local\Programs\Python\Python312\python.exe"
)

$ErrorActionPreference = "Stop"

$ProjectDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if (-not $RuntimeDir) { $RuntimeDir = Join-Path $ProjectDir ".tmp_tiku_agent_v2_prod_8790" }
if (-not $ControlDb) { $ControlDb = Join-Path $ProjectDir ".tmp_tiku_admin_8795\control.sqlite3" }
if (-not $LegacyInviteConfig) { $LegacyInviteConfig = Join-Path $RuntimeDir "invite_access.json" }
foreach ($name in @("RuntimeDir", "ControlDb", "LegacyInviteConfig", "PythonExe")) {
    $value = Get-Variable -Name $name -ValueOnly
    if ($name -ne "PythonExe" -and -not [System.IO.Path]::IsPathRooted($value)) {
        Set-Variable -Name $name -Value (Join-Path $ProjectDir $value)
    }
}
$RuntimeDir = [System.IO.Path]::GetFullPath($RuntimeDir)
$ControlDb = [System.IO.Path]::GetFullPath($ControlDb)
$LegacyInviteConfig = [System.IO.Path]::GetFullPath($LegacyInviteConfig)
$StatusFile = Join-Path $RuntimeDir "watchdog_8790.status"
$WatchdogPidFile = Join-Path $RuntimeDir "watchdog_8790.pid"
$BotPidFile = Join-Path $RuntimeDir "tiku_8790.pid"
$ModeFile = Join-Path $RuntimeDir "deployment_mode.json"
$WatchdogScript = Join-Path $PSScriptRoot "tiku_agent_watchdog_8790.ps1"

function Test-Health {
    try {
        $response = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 3
        return ($response.status -eq "ok") -or ($response.ok -eq $true)
    } catch {
        return $false
    }
}

function Get-ListenerPid {
    $listeners = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
    if ($listeners.Count -ne 1) { throw "Expected exactly one listener on port $Port." }
    return [int]$listeners[0].OwningProcess
}

function Get-PidFileValue([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw "PID file not found: $Path" }
    $value = (Get-Content -LiteralPath $Path -Encoding UTF8).Trim()
    if ($value -notmatch '^\d+$' -or [int]$value -le 0) { throw "Invalid PID file: $Path" }
    return [int]$value
}

function Resolve-CurrentWatchdogPid {
    $firstStatus = Get-Content -LiteralPath $StatusFile -Encoding UTF8 | Select-Object -First 1
    if ($firstStatus -notmatch '^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) Watchdog started\.') {
        throw "Cannot validate the current watchdog start time."
    }
    $expectedStart = [datetime]::ParseExact($Matches[1], "yyyy-MM-dd HH:mm:ss", $null)
    $candidates = @(
        Get-Process -Name powershell,pwsh -ErrorAction SilentlyContinue |
            Where-Object { [math]::Abs(($_.StartTime - $expectedStart).TotalSeconds) -le 10 }
    )
    if ($candidates.Count -ne 1) {
        throw "Expected exactly one PowerShell process matching the recorded watchdog start time."
    }
    return [int]$candidates[0].Id
}

function Assert-CurrentProcesses([int]$WatchdogPid) {
    $watchdog = Get-Process -Id $WatchdogPid -ErrorAction Stop
    if ($watchdog.ProcessName -notin @("powershell", "pwsh")) {
        throw "Current watchdog PID is not a PowerShell process."
    }
    $firstStatus = Get-Content -LiteralPath $StatusFile -Encoding UTF8 | Select-Object -First 1
    if ($firstStatus -notmatch '^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) Watchdog started\.') {
        throw "Cannot validate the current watchdog start time."
    }
    $expectedStart = [datetime]::ParseExact($Matches[1], "yyyy-MM-dd HH:mm:ss", $null)
    if ([math]::Abs(($watchdog.StartTime - $expectedStart).TotalSeconds) -gt 10) {
        throw "Current watchdog PID does not match the recorded watchdog start time."
    }
    $listenerPid = Get-ListenerPid
    $recordedPid = Get-PidFileValue $BotPidFile
    if ($listenerPid -ne $recordedPid) { throw "8790 listener does not match its PID file." }
    $listener = Get-Process -Id $listenerPid -ErrorAction Stop
    if ($listener.ProcessName -notmatch '^python') { throw "8790 listener is not a Python process." }
    if ($listener.StartTime -lt $watchdog.StartTime) { throw "8790 listener predates its watchdog." }
    return $listenerPid
}

function Start-Watchdog([string]$Mode) {
    $arguments = @(
        "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $WatchdogScript,
        "-Port", "$Port", "-RuntimeDir", $RuntimeDir,
        "-PythonExe", $PythonExe
    )
    if ($Mode -eq "control") {
        $arguments += @("-ControlDb", $ControlDb)
    } else {
        $arguments += @(
            "-InviteConfig", $LegacyInviteConfig,
            "-DailyBudgetCny", "$RollbackDailyBudgetCny",
            "-PerInviteDailyBudgetCny", "$RollbackPerInviteDailyBudgetCny"
        )
    }
    return Start-Process powershell.exe -ArgumentList $arguments -WindowStyle Hidden -PassThru
}

function Stop-ExactProcess([int]$ProcessId, [string]$ExpectedName) {
    $process = Get-Process -Id $ProcessId -ErrorAction Stop
    if ($process.ProcessName -notmatch $ExpectedName) {
        throw "Refusing to stop unexpected process PID $ProcessId ($($process.ProcessName))."
    }
    Stop-Process -Id $ProcessId -Force -ErrorAction Stop
    $process.WaitForExit(5000) | Out-Null
}

function Wait-Healthy([int]$Seconds = 20) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-Health) { return $true }
        Start-Sleep -Milliseconds 500
    }
    return $false
}

foreach ($path in @($RuntimeDir, $ControlDb, $LegacyInviteConfig, $PythonExe, $WatchdogScript, $StatusFile, $BotPidFile)) {
    if (-not (Test-Path -LiteralPath $path)) { throw "Required path not found: $path" }
}
if (-not (Test-Health)) { throw "8790 is not healthy before the switch." }
$adminHealth = Invoke-RestMethod -Uri "http://127.0.0.1:8795/health" -TimeoutSec 3
if ($adminHealth.status -ne "ok") { throw "8795 is not healthy before the switch." }

& $PythonExe -B (Join-Path $PSScriptRoot "manage_tiku_admin.py") `
    --control-db $ControlDb --import-invites $LegacyInviteConfig
if ($LASTEXITCODE -ne 0) { throw "Control database preflight failed." }

$detectedWatchdogPid = Resolve-CurrentWatchdogPid
if ($CurrentWatchdogPid -gt 0 -and $CurrentWatchdogPid -ne $detectedWatchdogPid) {
    throw "Supplied watchdog PID does not match the uniquely detected current watchdog."
}
Write-Host "Preflight passed: 8790 and 8795 are healthy; control database import is conflict-free."
Write-Host "Verified current 8790 watchdog PID: $detectedWatchdogPid"
if (-not $Apply) {
    Write-Host "No processes changed. Rerun with -Apply after the maintenance window is approved."
    return
}

$oldListenerPid = Assert-CurrentProcesses $detectedWatchdogPid
$dateFolder = Get-Date -Format "yyyy-MM-dd"
$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$projectName = Split-Path $ProjectDir -Leaf
$projectBackupRoot = Join-Path "F:\cc\_backups" $projectName
$backupBase = [System.IO.Path]::GetFullPath((Join-Path $projectBackupRoot $dateFolder))
$backupDir = [System.IO.Path]::GetFullPath((Join-Path $backupBase "8790_control_switch_$stamp"))
$boundary = $backupBase.TrimEnd([System.IO.Path]::DirectorySeparatorChar) + [System.IO.Path]::DirectorySeparatorChar
if (-not $backupDir.StartsWith($boundary, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Backup directory escaped the approved project backup root."
}
if (Test-Path -LiteralPath $backupDir) { throw "Backup directory already exists: $backupDir" }
New-Item -ItemType Directory -Path $backupDir -ErrorAction Stop | Out-Null
$sqliteBackup = Join-Path $backupDir "control.sqlite3"
$backupCode = @'
import sqlite3
import sys
from pathlib import Path
source = sqlite3.connect(Path(sys.argv[1]).resolve().as_uri() + '?mode=ro', uri=True)
destination = sqlite3.connect(sys.argv[2])
try:
    source.backup(destination)
    if destination.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
        raise SystemExit('SQLite backup integrity check failed')
finally:
    destination.close()
    source.close()
'@
& $PythonExe -c $backupCode $ControlDb $sqliteBackup
if ($LASTEXITCODE -ne 0) { throw "Control database backup failed." }
Copy-Item -LiteralPath $LegacyInviteConfig -Destination (Join-Path $backupDir "invite_access.json") -ErrorAction Stop

$newWatchdog = $null
$oldWatchdogStopped = $false
try {
    Stop-ExactProcess $detectedWatchdogPid '^(powershell|pwsh)$'
    $oldWatchdogStopped = $true
    if ((Get-ListenerPid) -ne $oldListenerPid) { throw "8790 listener changed before stop." }
    Stop-ExactProcess $oldListenerPid '^python'
    $newWatchdog = Start-Watchdog "control"
    if (-not (Wait-Healthy 25)) { throw "8790 did not become healthy in control mode." }
    $recordedWatchdogPid = Get-PidFileValue $WatchdogPidFile
    if ($recordedWatchdogPid -ne $newWatchdog.Id) { throw "New watchdog PID file does not match." }
    & $PythonExe -B (Join-Path $PSScriptRoot "verify_tiku_control_runtime.py") `
        --control-db $ControlDb --base-url "http://127.0.0.1:$Port"
    if ($LASTEXITCODE -ne 0) { throw "Control-mode runtime verification failed." }
    @{
        mode = "control_db"
        switched_at = [datetime]::UtcNow.ToString("o")
        watchdog_pid = $newWatchdog.Id
        backup_dir = $backupDir
    } | ConvertTo-Json | Set-Content -LiteralPath $ModeFile -Encoding UTF8
    Write-Host "8790 switched to the administrator control database and passed runtime verification."
} catch {
    $switchError = $_
    if (-not $oldWatchdogStopped) {
        throw ("Control-mode switch stopped before the old watchdog was changed: {0}" -f $switchError)
    }
    if ($newWatchdog -and -not $newWatchdog.HasExited) {
        Stop-ExactProcess $newWatchdog.Id '^(powershell|pwsh)$'
    }
    $currentListener = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
    if ($currentListener.Count -eq 1) {
        $failedPid = [int]$currentListener[0].OwningProcess
        Stop-ExactProcess $failedPid '^python'
    } elseif ($currentListener.Count -gt 1) {
        throw "Switch failed and rollback stopped because multiple 8790 listeners were found. Original error: $switchError"
    }
    $rollbackWatchdog = Start-Watchdog "legacy"
    if (-not (Wait-Healthy 25)) {
        throw "Switch failed and legacy rollback did not become healthy. Original error: $switchError"
    }
    @{
        mode = "legacy_rollback"
        switched_at = [datetime]::UtcNow.ToString("o")
        watchdog_pid = $rollbackWatchdog.Id
        backup_dir = $backupDir
        switch_error = "$switchError"
    } | ConvertTo-Json | Set-Content -LiteralPath $ModeFile -Encoding UTF8
    throw ("Control-mode switch failed; legacy 8790 was restored and is healthy. Original error: {0}" -f $switchError)
}
