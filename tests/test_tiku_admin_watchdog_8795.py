from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class TikuAdminWatchdog8795Test(unittest.TestCase):
    def test_watchdog_records_both_supervisor_and_admin_process_ids(self):
        script = (
            ROOT / "scripts" / "tiku_admin_watchdog_8795.ps1"
        ).read_text(encoding="utf-8")

        self.assertIn('watchdog_process_guard.ps1', script)
        self.assertIn('if ($Port -ne 8795)', script)
        self.assertIn('"watchdog_8795.pid"', script)
        self.assertIn("Assert-WatchdogPidFileAvailable", script)
        self.assertIn("Set-Content -LiteralPath $WatchdogPidFile -Value $PID", script)
        self.assertIn('"tiku_admin_8795.pid"', script)
        self.assertIn("Get-ManagedAdminProcess", script)
        self.assertIn("Test-AdminProcess -ProcessId $adminProcess.Id", script)
        self.assertIn("Test-AdminProcess -ProcessId $candidate.Id", script)
        self.assertIn("Test-AdminLaunch -ProcessId $candidate.Id", script)
        self.assertIn("Wait-WatchdogProcessReady", script)
        self.assertNotIn("Start-Sleep -Seconds 4", script)
        self.assertIn("Stop-Process -InputObject $candidate", script)
        self.assertIn("Stop-Process -InputObject $adminProcess", script)
        self.assertNotIn("Stop-Process -Id $adminProcess.Id", script)
        self.assertIn("Set-Content -LiteralPath $PidFile -Value $candidate.Id", script)
        ready_body = script[
            script.index('"ready" {') : script.index('"exited" {')
        ]
        self.assertIn("Set-Content -LiteralPath $PidFile -Value $candidate.Id", ready_body)
        ready_recheck = "Test-AdminProcess -ProcessId $candidate.Id"
        ready_write = "Set-Content -LiteralPath $PidFile -Value $candidate.Id"
        self.assertIn(ready_recheck, ready_body)
        self.assertLess(ready_body.index(ready_recheck), ready_body.index(ready_write))
        self.assertEqual(
            script.count("Set-Content -LiteralPath $PidFile -Value $candidate.Id"),
            1,
        )
        timeout_body = script[
            script.index('"timeout_verified" {') : script.index('"timeout_unverified" {')
        ]
        self.assertIn("Test-AdminLaunch -ProcessId $candidate.Id", timeout_body)
        self.assertIn("Stop-Process -InputObject $candidate", timeout_body)
        adoption_body = script[
            script.index("$adminProcess = Get-ManagedAdminProcess") : script.index("while ($true)")
        ]
        adoption_recheck = "Test-AdminProcess -ProcessId $adminProcess.Id"
        adoption_write = "Set-Content -LiteralPath $PidFile -Value $adminProcess.Id"
        self.assertIn(adoption_recheck, adoption_body)
        self.assertIn(adoption_write, adoption_body)
        self.assertLess(
            adoption_body.index(adoption_recheck), adoption_body.index(adoption_write)
        )
        start_body = script[
            script.index("function Start-Admin") : script.index("function Test-AdminProcess")
        ]
        self.assertNotIn("Set-Content -LiteralPath $PidFile", start_body)
        self.assertIn("PID file was not changed", script)
        self.assertIn("-WindowStyle Hidden", script)
        lock_call = "Enter-WatchdogInstanceLock -Port $Port"
        watchdog_pid_write = (
            "Set-Content -LiteralPath $WatchdogPidFile -Value $PID"
        )
        self.assertIn(lock_call, script)
        self.assertLess(script.index(lock_call), script.index(watchdog_pid_write))
        managed_lookup = "$adminProcess = Get-ManagedAdminProcess"
        self.assertLess(script.index(managed_lookup), script.index(watchdog_pid_write))
        finally_body = script[script.rindex("} finally {") :]
        self.assertIn("$ownsWatchdogPidFile", finally_body)
        self.assertIn("Remove-WatchdogPidFileIfOwned", finally_body)
        self.assertIn("Exit-WatchdogInstanceLock", finally_body)


if __name__ == "__main__":
    unittest.main()
