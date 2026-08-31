import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


POWERSHELL = shutil.which("powershell.exe") or shutil.which("powershell")


class TikuAgentWatchdog8896Test(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parents[1]
        self.script_path = self.root / "scripts" / "tiku_agent_watchdog_8896.ps1"
        self.safety_path = (
            self.root / "scripts" / "tiku_agent_watchdog_8896_safety.ps1"
        )
        self.guard_path = self.root / "scripts" / "watchdog_process_guard.ps1"
        self.script = self.script_path.read_text(encoding="utf-8")
        self.safety = self.safety_path.read_text(encoding="utf-8")

    def _run_powershell(self, command: str) -> subprocess.CompletedProcess[str]:
        assert POWERSHELL is not None
        return subprocess.run(
            [
                POWERSHELL,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                command,
            ],
            cwd=self.root,
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
        )

    def test_watchdog_uses_isolated_8896_entrypoint_and_runtime(self):
        self.assertIn("watchdog_process_guard.ps1", self.script)
        self.assertIn("tiku_agent_watchdog_8896_safety.ps1", self.script)
        self.assertIn("[int]$Port = 8896", self.script)
        self.assertIn("if ($Port -ne 8896)", self.script)
        self.assertIn("Resolve-Tiku8896RuntimeDirectory", self.script)
        self.assertIn('$AgentEntrypoint = (Resolve-Path', self.script)
        self.assertIn('$AgentEntrypoint,', self.script)
        self.assertIn('"scripts\\run_tiku_agent_8896.py"', self.script)
        self.assertIn('".tmp_tiku_agent_a3_mvp_8896"', self.script)
        self.assertIn('Join-Path $RuntimeDir "service_logs"', self.script)
        self.assertNotIn("run_tiku_agent_demo.py", self.script)
        self.assertNotIn(".tmp_tiku_agent_v2_prod_8790", self.script)
        for protected_port in (8788, 8790, 8794, 8795):
            self.assertNotIn(str(protected_port), self.script)
        self.assertLess(
            self.script.index("if ($Port -ne 8896)"),
            self.script.index("New-Item -ItemType Directory"),
        )

    def test_watchdog_only_manages_fully_verified_8896_processes(self):
        self.assertIn('Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health"', self.script)
        self.assertIn("Resolve-WatchdogExecutablePath", self.script)
        self.assertIn("Assert-WatchdogPidFileAvailable", self.script)
        self.assertIn("Get-ManagedAgentProcess", self.script)
        self.assertIn("Get-Tiku8896ListeningProcessIds", self.script)
        self.assertIn("Get-Tiku8896ScopedListeningProcessIds", self.script)
        self.assertIn("[switch]$RequireScopedListenerQuery", self.script)
        self.assertIn("Test-WatchdogProcessEvidence", self.script)
        self.assertIn("Test-AgentProcess -ProcessId $agentProcess.Id", self.script)
        self.assertIn("Test-AgentProcess -ProcessId $candidate.Id", self.script)
        self.assertIn("Test-AgentLaunch -ProcessId $candidate.Id", self.script)
        self.assertIn("Wait-WatchdogProcessReady", self.script)
        self.assertIn("Stop-Process -InputObject $agentProcess", self.script)
        self.assertIn("Stop-Process -InputObject $candidate", self.script)
        self.assertNotIn("Stop-Process -Id", self.script)
        self.assertNotIn("function Stop-PortProcess", self.script)
        self.assertNotIn("Get-WatchdogListeningProcessIds", self.script)
        self.assertNotIn("Test-WatchdogProcessMatch", self.script)
        self.assertNotIn("Stopping process on port", self.script)
        self.assertNotIn("Start-Sleep -Seconds 4", self.script)
        self.assertIn("Start-Sleep -Seconds 20", self.script)
        self.assertIn("PID file was not changed", self.script)

        start_body = self.script[
            self.script.index("function Start-Agent") : self.script.index(
                "function Test-AgentProcess"
            )
        ]
        self.assertIn("Start-Process $ExpectedPythonPath", start_body)
        self.assertIn("-ArgumentList $AgentLaunchArguments", start_body)
        self.assertNotIn("-ArgumentList $AgentArguments", start_body)
        self.assertNotIn("Set-Content -LiteralPath $AgentPidFile", start_body)

        ready_body = self.script[
            self.script.index('"ready" {') : self.script.index('"exited" {')
        ]
        ready_recheck = "Test-AgentProcess -ProcessId $candidate.Id"
        ready_write = "Set-Content -LiteralPath $AgentPidFile -Value $candidate.Id"
        self.assertIn(ready_recheck, ready_body)
        self.assertIn(ready_write, ready_body)
        self.assertLess(ready_body.index(ready_recheck), ready_body.index(ready_write))
        self.assertEqual(self.script.count(ready_write), 1)

        timeout_body = self.script[
            self.script.index('"timeout_verified" {') : self.script.index(
                '"timeout_unverified" {'
            )
        ]
        self.assertIn("Test-AgentLaunch -ProcessId $candidate.Id", timeout_body)
        self.assertIn("Stop-Process -InputObject $candidate", timeout_body)

        adoption_body = self.script[
            self.script.index("$agentProcess = Get-ManagedAgentProcess") : self.script.index(
                "while ($true)"
            )
        ]
        adoption_recheck = "Test-AgentProcess -ProcessId $agentProcess.Id"
        adoption_write = "Set-Content -LiteralPath $AgentPidFile -Value $agentProcess.Id"
        self.assertIn(adoption_recheck, adoption_body)
        self.assertIn(adoption_write, adoption_body)
        self.assertLess(
            adoption_body.index(adoption_recheck), adoption_body.index(adoption_write)
        )

    def test_watchdog_has_single_instance_and_owned_pid_file_cleanup(self):
        lock_call = "Enter-WatchdogInstanceLock -Port $Port"
        watchdog_pid_write = "Set-Content -LiteralPath $WatchdogPidFile -Value $PID"
        managed_lookup = "$agentProcess = Get-ManagedAgentProcess"
        self.assertIn(lock_call, self.script)
        self.assertIn(watchdog_pid_write, self.script)
        self.assertLess(self.script.index(lock_call), self.script.index(watchdog_pid_write))
        self.assertLess(
            self.script.index(managed_lookup), self.script.index(watchdog_pid_write)
        )
        finally_body = self.script[self.script.rindex("} finally {") :]
        self.assertIn("$ownsWatchdogPidFile", finally_body)
        self.assertIn("Remove-WatchdogPidFileIfOwned", finally_body)
        self.assertIn("Exit-WatchdogInstanceLock", finally_body)

    @unittest.skipUnless(POWERSHELL, "Windows PowerShell is required")
    def test_start_process_encoding_keeps_runtime_as_one_argument(self):
        safety = str(self.safety_path).replace("'", "''")
        guard = str(self.guard_path).replace("'", "''")
        python = str(Path(sys.executable).resolve()).replace("'", "''")
        runtime = r"F:\accept --port 8794" + "\\"
        expected_runtime = runtime.rstrip("\\/")
        with tempfile.TemporaryDirectory(prefix="tiku watchdog argv ") as temp_dir:
            temp = Path(temp_dir)
            probe = temp / "argv probe.py"
            output = temp / "argv output.json"
            probe.write_text(
                "import json, sys\n"
                "from pathlib import Path\n"
                "Path(sys.argv[1]).write_text(json.dumps(sys.argv[2:]), encoding='utf-8')\n",
                encoding="utf-8",
            )
            probe_value = str(probe).replace("'", "''")
            output_value = str(output).replace("'", "''")
            runtime_value = runtime.replace("'", "''")
            result = self._run_powershell(
                f"""
. '{safety}'
. '{guard}'
$runtime = Resolve-Tiku8896RuntimeDirectory `
  -RuntimeDirectory '{runtime_value}' `
  -ProjectDirectory 'F:\\checkout'
$logical = @('{probe_value}', '{output_value}', $runtime)
$encoded = @(
  $logical | ForEach-Object {{
    ConvertTo-Tiku8896CommandLineArgument -Argument ([string]$_)
  }}
)
$encodedExecutable = ConvertTo-Tiku8896CommandLineArgument -Argument '{python}'
$commandLine = $encodedExecutable + ' ' + ($encoded -join ' ')
$identityMatches = Test-WatchdogLaunchEvidence `
  -ProcessId 321 `
  -ExecutablePath '{python}' `
  -ExpectedExecutablePath '{python}' `
  -CommandLine $commandLine `
  -ExpectedArguments $logical
if (-not $identityMatches) {{ exit 9 }}
$rootBlocked = $false
try {{
  Resolve-Tiku8896RuntimeDirectory `
    -RuntimeDirectory 'F:\\' `
    -ProjectDirectory 'F:\\checkout'
}} catch {{
  $rootBlocked = $_.Exception.Message -like '*cannot be a filesystem root*'
}}
if (-not $rootBlocked) {{ exit 10 }}
$process = Start-Process `
  -FilePath '{python}' `
  -ArgumentList $encoded `
  -WindowStyle Hidden `
  -Wait `
  -PassThru
if ($process.ExitCode -ne 0) {{ exit $process.ExitCode }}
"""
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                json.loads(output.read_text(encoding="utf-8")),
                [expected_runtime],
            )

    @unittest.skipUnless(POWERSHELL, "Windows PowerShell is required")
    def test_listener_enumeration_fails_closed_when_both_sources_fail(self):
        safety = str(self.safety_path).replace("'", "''")
        result = self._run_powershell(
            f"""
. '{safety}'
$empty = @(Get-Tiku8896ListeningProcessIds `
  -Port 8896 `
  -PrimaryQuery {{ throw 'primary failed' }} `
  -FallbackQuery {{ @() }})
$fallback = @(Get-Tiku8896ListeningProcessIds `
  -Port 8896 `
  -PrimaryQuery {{ throw 'primary failed' }} `
  -FallbackQuery {{ '  TCP    127.0.0.1:8896    0.0.0.0:0    LISTENING    321' }})
$failedClosed = $false
try {{
  Get-Tiku8896ListeningProcessIds `
    -Port 8896 `
    -PrimaryQuery {{ throw 'primary failed' }} `
    -FallbackQuery {{ throw 'fallback failed' }}
}} catch {{
  $failedClosed = $_.Exception.Message -like 'Unable to verify port 8896 ownership*'
}}
Write-Output "$($empty.Count),$($fallback[0]),$failedClosed"
"""
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip().splitlines()[-1], "0,321,True")

    @unittest.skipUnless(POWERSHELL, "Windows PowerShell is required")
    def test_scoped_listener_query_never_uses_the_broad_fallback(self):
        safety = str(self.safety_path).replace("'", "''")
        result = self._run_powershell(
            f"""
. '{safety}'
$failedClosed = $false
try {{
  Get-Tiku8896ScopedListeningProcessIds `
    -Port 8896 `
    -PrimaryQuery {{ throw 'scoped query failed' }}
}} catch {{
  $failedClosed = `
    $_.Exception.Message -like 'Unable to verify port 8896 ownership*' -and `
    $_.Exception.Message -like '*Broad listener fallback is disabled*'
}}
Write-Output $failedClosed
"""
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip().splitlines()[-1], "True")

    @unittest.skipUnless(POWERSHELL, "Windows PowerShell is required")
    def test_scoped_listener_query_only_accepts_net_tcp_no_match_errors(self):
        safety = str(self.safety_path).replace("'", "''")
        result = self._run_powershell(
            f"""
. '{safety}'
$acceptedIds = @(
  'CmdletizationQuery_NotFound,Get-NetTCPConnection',
  'CmdletizationQuery_NotFound_LocalPort,Get-NetTCPConnection'
)
$accepted = foreach ($errorId in $acceptedIds) {{
  $record = [System.Management.Automation.ErrorRecord]::new(
    [System.InvalidOperationException]::new('no matching listener'),
    $errorId,
    [System.Management.Automation.ErrorCategory]::ObjectNotFound,
    'MSFT_NetTCPConnection'
  )
  $ids = @(Get-Tiku8896ScopedListeningProcessIds `
    -Port 8896 `
    -PrimaryQuery {{ throw $record }})
  $ids.Count -eq 0
}}
$rejectedCases = @(
  @(
    'CimJob_AccessDenied,Get-NetTCPConnection',
    [System.Management.Automation.ErrorCategory]::PermissionDenied
  ),
  @(
    'CimJob_BrokenCimSession,Get-NetTCPConnection',
    [System.Management.Automation.ErrorCategory]::ResourceUnavailable
  ),
  @(
    'CmdletizationQuery_NotFound_LocalPort,Get-OtherCommand',
    [System.Management.Automation.ErrorCategory]::ObjectNotFound
  ),
  @(
    'CmdletizationQuery_NotFound_LocalPort,Get-NetTCPConnection',
    [System.Management.Automation.ErrorCategory]::PermissionDenied
  )
)
$rejected = foreach ($case in $rejectedCases) {{
  $record = [System.Management.Automation.ErrorRecord]::new(
    [System.InvalidOperationException]::new('query failed'),
    [string]$case[0],
    [System.Management.Automation.ErrorCategory]$case[1],
    'MSFT_NetTCPConnection'
  )
  $failedClosed = $false
  try {{
    Get-Tiku8896ScopedListeningProcessIds `
      -Port 8896 `
      -PrimaryQuery {{ throw $record }}
  }} catch {{
    $failedClosed = `
      $_.Exception.Message -like 'Unable to verify port 8896 ownership*' -and `
      $_.Exception.Message -like '*Broad listener fallback is disabled*'
  }}
  $failedClosed
}}
Write-Output "$(-not ($accepted -contains $false))|$(-not ($rejected -contains $false))"
"""
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip().splitlines()[-1], "True|True")

    @unittest.skipUnless(POWERSHELL, "Windows PowerShell is required")
    def test_process_identity_rejects_a_different_checkout(self):
        guard = str(self.guard_path).replace("'", "''")
        result = self._run_powershell(
            f"""
. '{guard}'
$expected = @(
  '-B', 'F:\\checkout-a\\scripts\\run_tiku_agent_8896.py',
  '--host', '127.0.0.1', '--port', '8896',
  '--runtime-dir', 'F:\\runtime\\8896'
)
$line = '"C:\\Python312\\python.exe" -B F:\\checkout-b\\scripts\\run_tiku_agent_8896.py --host 127.0.0.1 --port 8896 --runtime-dir F:\\runtime\\8896'
$matches = Test-WatchdogLaunchEvidence `
  -ProcessId 321 `
  -ExecutablePath 'C:\\Python312\\python.exe' `
  -ExpectedExecutablePath 'C:\\Python312\\python.exe' `
  -CommandLine $line `
  -ExpectedArguments $expected
Write-Output $matches
"""
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip().splitlines()[-1], "False")

    def test_watchdog_exposes_manual_crop_rollback_switch(self):
        self.assertIn("[switch]$DisableAutoCrop", self.script)
        self.assertIn('$AgentArguments += "--disable-auto-crop"', self.script)
        self.assertIn("[switch]$DisableOutputWatchdog", self.script)
        self.assertIn('$AgentArguments += "--disable-output-watchdog"', self.script)
        self.assertIn("[switch]$DisableA3TextOrientation", self.script)
        self.assertIn('$AgentArguments += "--disable-a3-text-orientation"', self.script)
        self.assertIn("[switch]$DisableMediaCache", self.script)
        self.assertIn('$AgentArguments += "--disable-media-cache"', self.script)


if __name__ == "__main__":
    unittest.main()
