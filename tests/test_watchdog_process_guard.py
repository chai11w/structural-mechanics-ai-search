from __future__ import annotations

from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = shutil.which("powershell.exe") or shutil.which("powershell")
GUARD = ROOT / "scripts" / "watchdog_process_guard.ps1"


@unittest.skipUnless(POWERSHELL, "Windows PowerShell is required")
class WatchdogProcessGuardTest(unittest.TestCase):
    def _run_powershell(self, command: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                POWERSHELL,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                command,
            ],
            cwd=ROOT,
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
        )

    def test_process_evidence_requires_exact_pid_port_executable_and_arguments(self):
        guard = str(GUARD).replace("'", "''")
        command = f"""
. '{guard}'
$expected = @(
  '-B', 'scripts\\run_tiku_admin.py',
  '--host', '127.0.0.1',
  '--port', '8795',
  '--admin-runtime', 'F:\\cc\\runtime with space'
)
$line = '"C:\\Python 312\\python.exe" -B scripts\\run_tiku_admin.py --host 127.0.0.1 --port 8795 --admin-runtime "F:\\cc\\runtime with space"'
$valid = Test-WatchdogProcessEvidence -ProcessId 321 -ListeningProcessIds @(321) -ExecutablePath 'C:\\Python 312\\python.exe' -ExpectedExecutablePath 'C:\\Python 312\\python.exe' -CommandLine $line -ExpectedArguments $expected
$wrongPid = Test-WatchdogProcessEvidence -ProcessId 321 -ListeningProcessIds @(654) -ExecutablePath 'C:\\Python 312\\python.exe' -ExpectedExecutablePath 'C:\\Python 312\\python.exe' -CommandLine $line -ExpectedArguments $expected
$multipleOwners = Test-WatchdogProcessEvidence -ProcessId 321 -ListeningProcessIds @(321, 654) -ExecutablePath 'C:\\Python 312\\python.exe' -ExpectedExecutablePath 'C:\\Python 312\\python.exe' -CommandLine $line -ExpectedArguments $expected
$wrongExecutable = Test-WatchdogProcessEvidence -ProcessId 321 -ListeningProcessIds @(321) -ExecutablePath 'C:\\Other\\python.exe' -ExpectedExecutablePath 'C:\\Python 312\\python.exe' -CommandLine $line -ExpectedArguments $expected
$wrongArguments = @($expected)
$wrongArguments[5] = '8790'
$wrongCommand = Test-WatchdogProcessEvidence -ProcessId 321 -ListeningProcessIds @(321) -ExecutablePath 'C:\\Python 312\\python.exe' -ExpectedExecutablePath 'C:\\Python 312\\python.exe' -CommandLine $line -ExpectedArguments $wrongArguments
$extraArgument = Test-WatchdogProcessEvidence -ProcessId 321 -ListeningProcessIds @(321) -ExecutablePath 'C:\\Python 312\\python.exe' -ExpectedExecutablePath 'C:\\Python 312\\python.exe' -CommandLine ($line + ' --unexpected') -ExpectedArguments $expected
Write-Output "$valid,$wrongPid,$multipleOwners,$wrongExecutable,$wrongCommand,$extraArgument"
"""
        result = subprocess.run(
            [
                POWERSHELL,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                command,
            ],
            cwd=ROOT,
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.strip().splitlines()[-1],
            "True,False,False,False,False,False",
        )

    def test_wait_for_process_ready_distinguishes_slow_and_unverified_startups(self):
        guard = str(GUARD).replace("'", "''")
        command = f"""
. '{guard}'
$process = [pscustomobject]@{{ HasExited = $false }}
$script:slowAttempts = 0
$slow = Wait-WatchdogProcessReady `
  -Process $process `
  -TimeoutSeconds 3 `
  -PollSeconds 1 `
  -SleepAction {{ param($Seconds) }} `
  -HealthProbe {{ $true }} `
  -FullMatchProbe {{ $script:slowAttempts += 1; $script:slowAttempts -ge 3 }} `
  -LaunchIdentityProbe {{ $true }}
$verifiedTimeout = Wait-WatchdogProcessReady `
  -Process $process `
  -TimeoutSeconds 2 `
  -PollSeconds 1 `
  -SleepAction {{ param($Seconds) }} `
  -HealthProbe {{ $false }} `
  -FullMatchProbe {{ $false }} `
  -LaunchIdentityProbe {{ $true }}
$unverifiedTimeout = Wait-WatchdogProcessReady `
  -Process $process `
  -TimeoutSeconds 2 `
  -PollSeconds 1 `
  -SleepAction {{ param($Seconds) }} `
  -HealthProbe {{ $false }} `
  -FullMatchProbe {{ $false }} `
  -LaunchIdentityProbe {{ $false }}
Write-Output "$slow,$script:slowAttempts,$verifiedTimeout,$unverifiedTimeout"
"""
        result = subprocess.run(
            [
                POWERSHELL,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                command,
            ],
            cwd=ROOT,
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.strip().splitlines()[-1],
            "ready,3,timeout_verified,timeout_unverified",
        )

    def test_instance_lock_rejects_a_second_owner_and_recovers_after_exit(self):
        guard = str(GUARD).replace("'", "''")
        port = 40000 + (os.getpid() % 20000)
        holder_command = f"""
. '{guard}'
$lock = Enter-WatchdogInstanceLock -Port {port}
Write-Output 'READY'
[Console]::Out.Flush()
$null = [Console]::In.ReadLine()
"""
        holder = subprocess.Popen(
            [
                POWERSHELL,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                holder_command,
            ],
            cwd=ROOT,
            text=True,
            encoding="utf-8",
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            self.assertEqual(holder.stdout.readline().strip(), "READY")
            blocked = self._run_powershell(
                f"""
. '{guard}'
try {{
  $lock = Enter-WatchdogInstanceLock -Port {port}
  Write-Output 'UNEXPECTED'
}} catch {{
  Write-Output $_.Exception.Message
}}
"""
            )
            self.assertEqual(blocked.returncode, 0, blocked.stderr)
            self.assertIn(
                f"Another watchdog already owns the instance lock for port {port}.",
                blocked.stdout,
            )
        finally:
            if holder.poll() is None:
                holder.stdin.write("exit\n")
                holder.stdin.flush()
            holder.communicate(timeout=10)

        recovered = self._run_powershell(
            f"""
. '{guard}'
$lock = Enter-WatchdogInstanceLock -Port {port}
Write-Output 'RECOVERED'
Exit-WatchdogInstanceLock -Mutex $lock
"""
        )
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        self.assertEqual(recovered.stdout.strip().splitlines()[-1], "RECOVERED")

    def test_pid_file_cleanup_only_removes_the_current_owners_record(self):
        guard = str(GUARD).replace("'", "''")
        with tempfile.TemporaryDirectory() as temp_dir:
            pid_file = str(Path(temp_dir) / "watchdog.pid").replace("'", "''")
            result = self._run_powershell(
                f"""
. '{guard}'
Set-Content -LiteralPath '{pid_file}' -Value '1234' -Encoding ASCII
Remove-WatchdogPidFileIfOwned -Path '{pid_file}' -OwnerProcessId 5678
$preserved = Test-Path -LiteralPath '{pid_file}' -PathType Leaf
Remove-WatchdogPidFileIfOwned -Path '{pid_file}' -OwnerProcessId 1234
$removed = -not (Test-Path -LiteralPath '{pid_file}')
Write-Output "$preserved,$removed"
"""
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip().splitlines()[-1], "True,True")

    def test_pid_file_guard_rejects_a_live_previous_owner(self):
        guard = str(GUARD).replace("'", "''")
        with tempfile.TemporaryDirectory() as temp_dir:
            pid_file = str(Path(temp_dir) / "watchdog.pid").replace("'", "''")
            result = self._run_powershell(
                f"""
. '{guard}'
Set-Content -LiteralPath '{pid_file}' -Value $PID -Encoding ASCII
$blocked = $false
try {{
  Assert-WatchdogPidFileAvailable -Path '{pid_file}' -OwnerProcessId ($PID + 1)
}} catch {{
  $blocked = $_.Exception.Message -like 'Existing watchdog PID*'
}}
Assert-WatchdogPidFileAvailable -Path '{pid_file}' -OwnerProcessId $PID
Set-Content -LiteralPath '{pid_file}' -Value '2147483646' -Encoding ASCII
Assert-WatchdogPidFileAvailable -Path '{pid_file}' -OwnerProcessId $PID
Write-Output "$blocked"
"""
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip().splitlines()[-1], "True")


if __name__ == "__main__":
    unittest.main()
