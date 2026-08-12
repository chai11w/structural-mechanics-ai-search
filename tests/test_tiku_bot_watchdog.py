import unittest
from pathlib import Path


class TikuBotWatchdogTest(unittest.TestCase):
    def test_restart_cleans_stale_listener_and_fails_closed(self):
        script = (
            Path(__file__).resolve().parents[1] / "scripts" / "tiku_bot_watchdog.ps1"
        ).read_text(encoding="utf-8")

        self.assertIn("function Stop-PortProcess", script)
        self.assertIn("Get-NetTCPConnection -LocalPort $Port -State Listen", script)
        self.assertIn("Stop-Process -Id $processId -Force -ErrorAction Stop", script)
        restart = script.index("if (-not $botProcess -or $botProcess.HasExited")
        cleanup = script.index("Stop-PortProcess", restart)
        start = script.index("$botProcess = Start-Bot", restart)
        self.assertLess(cleanup, start)


if __name__ == "__main__":
    unittest.main()
