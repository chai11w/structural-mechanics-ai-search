import unittest
from pathlib import Path


class TikuAgentWatchdog8896Test(unittest.TestCase):
    def setUp(self):
        self.script = (
            Path(__file__).resolve().parents[1]
            / "scripts"
            / "tiku_agent_watchdog_8896.ps1"
        ).read_text(encoding="utf-8")

    def test_watchdog_uses_isolated_8896_entrypoint_and_runtime(self):
        self.assertIn("[int]$Port = 8896", self.script)
        self.assertIn('"scripts\\run_tiku_agent_8896.py"', self.script)
        self.assertIn('".tmp_tiku_agent_a3_mvp_8896"', self.script)
        self.assertIn('Join-Path $RuntimeDir "service_logs"', self.script)
        self.assertNotIn("run_tiku_agent_demo.py", self.script)
        self.assertNotIn(".tmp_tiku_agent_v2_prod_8790", self.script)

    def test_watchdog_restarts_failed_service_without_duplicate_instances(self):
        self.assertIn('Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health"', self.script)
        self.assertIn("Stop-PortProcess", self.script)
        self.assertIn("Start-Sleep -Seconds 20", self.script)
        self.assertIn('Set-Content -LiteralPath $WatchdogPidFile -Value $PID', self.script)

    def test_watchdog_exposes_manual_crop_rollback_switch(self):
        self.assertIn("[switch]$DisableAutoCrop", self.script)
        self.assertIn('$arguments += "--disable-auto-crop"', self.script)


if __name__ == "__main__":
    unittest.main()
