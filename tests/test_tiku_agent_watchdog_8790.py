import unittest
from pathlib import Path


class TikuAgentWatchdog8790Test(unittest.TestCase):
    def test_watchdog_runs_a3_v1_with_control_db_and_manual_crop_rollback(self):
        script = (
            Path(__file__).resolve().parents[1]
            / "scripts"
            / "tiku_agent_watchdog_8790.ps1"
        ).read_text(encoding="utf-8")

        self.assertIn('watchdog_process_guard.ps1', script)
        self.assertIn('if ($Port -ne 8790)', script)
        self.assertIn('"scripts\\run_tiku_agent_8790.py"', script)
        self.assertIn('".tmp_tiku_admin_8795\\control.sqlite3"', script)
        self.assertIn("[switch]$DisableAutoCrop", script)
        self.assertIn('"--disable-auto-crop"', script)
        self.assertIn("[switch]$DisableOutputWatchdog", script)
        self.assertIn('"--disable-output-watchdog"', script)
        self.assertIn('"--disable-a3-text-orientation"', script)
        self.assertIn('"--max-concurrent-tasks", "$MaxConcurrentTasks"', script)
        self.assertIn('"--max-queued-tasks", "$MaxQueuedTasks"', script)
        self.assertIn('"--queue-wait-seconds", "$QueueWaitSeconds"', script)
        self.assertIn("[int]$MaxConcurrentTasks = 1", script)
        self.assertIn("[int]$MaxQueuedTasks = 2", script)
        self.assertIn("[int]$QueueWaitSeconds = 55", script)
        self.assertIn("Assert-WatchdogPidFileAvailable", script)
        self.assertIn("Get-ManagedBotProcess", script)
        self.assertIn("Test-BotProcess -ProcessId $botProcess.Id", script)
        self.assertIn("Test-BotProcess -ProcessId $candidate.Id", script)
        self.assertIn("Test-BotLaunch -ProcessId $candidate.Id", script)
        self.assertIn("Wait-WatchdogProcessReady", script)
        self.assertNotIn("Start-Sleep -Seconds 4", script)
        self.assertIn("Stop-Process -InputObject $candidate", script)
        self.assertIn("Stop-Process -InputObject $botProcess", script)
        self.assertNotIn("Stop-Process -Id $botProcess.Id", script)
        self.assertIn("Set-Content -LiteralPath $BotPidFile -Value $candidate.Id", script)
        ready_body = script[
            script.index('"ready" {') : script.index('"exited" {')
        ]
        self.assertIn("Set-Content -LiteralPath $BotPidFile -Value $candidate.Id", ready_body)
        ready_recheck = "Test-BotProcess -ProcessId $candidate.Id"
        ready_write = "Set-Content -LiteralPath $BotPidFile -Value $candidate.Id"
        self.assertIn(ready_recheck, ready_body)
        self.assertLess(ready_body.index(ready_recheck), ready_body.index(ready_write))
        self.assertEqual(
            script.count("Set-Content -LiteralPath $BotPidFile -Value $candidate.Id"),
            1,
        )
        timeout_body = script[
            script.index('"timeout_verified" {') : script.index('"timeout_unverified" {')
        ]
        self.assertIn("Test-BotLaunch -ProcessId $candidate.Id", timeout_body)
        self.assertIn("Stop-Process -InputObject $candidate", timeout_body)
        adoption_body = script[
            script.index("$botProcess = Get-ManagedBotProcess") : script.index("while ($true)")
        ]
        adoption_recheck = "Test-BotProcess -ProcessId $botProcess.Id"
        adoption_write = "Set-Content -LiteralPath $BotPidFile -Value $botProcess.Id"
        self.assertIn(adoption_recheck, adoption_body)
        self.assertIn(adoption_write, adoption_body)
        self.assertLess(
            adoption_body.index(adoption_recheck), adoption_body.index(adoption_write)
        )
        self.assertNotIn("function Stop-PortProcess", script)
        self.assertNotIn("Stopping process on port", script)
        start_body = script[
            script.index("function Start-Bot") : script.index("function Test-BotProcess")
        ]
        self.assertNotIn("Set-Content -LiteralPath $BotPidFile", start_body)
        self.assertIn("PID file was not changed", script)
        self.assertNotIn('"scripts\\run_tiku_agent_demo.py"', script)
        lock_call = "Enter-WatchdogInstanceLock -Port $Port"
        watchdog_pid_write = (
            "Set-Content -LiteralPath $WatchdogPidFile -Value $PID"
        )
        self.assertIn(lock_call, script)
        self.assertLess(script.index(lock_call), script.index(watchdog_pid_write))
        managed_lookup = "$botProcess = Get-ManagedBotProcess"
        self.assertLess(script.index(managed_lookup), script.index(watchdog_pid_write))
        finally_body = script[script.rindex("} finally {") :]
        self.assertIn("$ownsWatchdogPidFile", finally_body)
        self.assertIn("Remove-WatchdogPidFileIfOwned", finally_body)
        self.assertIn("Exit-WatchdogInstanceLock", finally_body)


if __name__ == "__main__":
    unittest.main()
