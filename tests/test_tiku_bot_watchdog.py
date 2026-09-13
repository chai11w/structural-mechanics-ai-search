import unittest
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


class TikuBotWatchdogTest(unittest.TestCase):
    def test_managed_configuration_binds_cache_to_runtime_without_starting_service(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder).resolve(); runtime=root/'runtime'; config=root/'role.json'; config.write_text('{}')
            fee=root/'costs.sqlite3'; fee.write_bytes(b'fixture')
            script=Path(__file__).resolve().parents[1]/'scripts/tiku_bot_watchdog.ps1'
            env={**os.environ,'TIKU_BANK_STORE':str(root/'published'),'TIKU_CONFIG_FILE':str(config),
                 'LIDA_TEST_SCRIPT':str(script),'LIDA_TEST_RUNTIME':str(runtime),
                 'LIDA_TEST_FEE':str(fee),'LIDA_TEST_PYTHON':sys.executable}
            env.pop('TIKU_SEARCH_STATE_DIR',None)
            code=r'''
$ErrorActionPreference='Stop'
. $env:LIDA_TEST_SCRIPT -RuntimeDir $env:LIDA_TEST_RUNTIME -PythonExe $env:LIDA_TEST_PYTHON -AdminFeeDatabase $env:LIDA_TEST_FEE -FunctionsOnly
if ($env:TIKU_SEARCH_STATE_DIR -cne (Join-Path $env:LIDA_TEST_RUNTIME 'search-cache')) { throw 'Wrong cache source' }
if (Test-Path -LiteralPath $env:LIDA_TEST_RUNTIME) { throw 'Unexpected runtime mutation' }
'''
            command=[shutil.which('pwsh') or 'pwsh','-NoProfile','-NonInteractive','-Command',code]
            result=subprocess.run(command,env=env,capture_output=True,text=True,encoding='utf-8',timeout=15)
            self.assertEqual(result.returncode,0,result.stderr)
            env['TIKU_SEARCH_STATE_DIR']=str(root/'outside-cache')
            rejected=subprocess.run(command,env=env,capture_output=True,text=True,encoding='utf-8',timeout=15)
            self.assertNotEqual(rejected.returncode,0)
            self.assertIn('cache must stay within',rejected.stderr)
            self.assertFalse(runtime.exists())

    def test_fixed_python_and_external_runtime_reach_actual_child_as_single_arguments(self):
        with tempfile.TemporaryDirectory(prefix="feishu launch fixture ") as folder:
            root=Path(folder).resolve(); runtime=root/'runtime with spaces'; fee=root/'fee source.sqlite3'; fee.write_bytes(b'fixture')
            probe=root/'argument probe.py'; probe.write_text('import json,sys; print(json.dumps(sys.argv[1:]))')
            script=Path(__file__).resolve().parents[1]/'scripts/tiku_bot_watchdog.ps1'
            env={**os.environ, 'LIDA_TEST_SCRIPT':str(script), 'LIDA_TEST_RUNTIME':str(runtime),
                 'LIDA_TEST_PYTHON':sys.executable, 'LIDA_TEST_FEE':str(fee), 'LIDA_TEST_PROBE':str(probe)}
            env.pop('TIKU_BANK_STORE', None)
            code=r'''
$ErrorActionPreference='Stop'
. $env:LIDA_TEST_SCRIPT -RuntimeDir $env:LIDA_TEST_RUNTIME -PythonExe $env:LIDA_TEST_PYTHON -AdminFeeDatabase $env:LIDA_TEST_FEE -FunctionsOnly -EnableStoreTextOrientation
if (Test-Path -LiteralPath $LogDir) { throw 'FunctionsOnly created runtime state' }
[IO.Directory]::CreateDirectory($LogDir) | Out-Null
$BotEntrypoint=$env:LIDA_TEST_PROBE
$child=Start-Bot
if (-not $child.WaitForExit(15000)) { throw 'Argument fixture did not finish' }
if ($child.ExitCode -ne 0) { throw 'Argument fixture failed' }
Get-Content -LiteralPath $BotOutLog -Raw
'''
            process=subprocess.run([shutil.which('pwsh') or 'pwsh','-NoProfile','-NonInteractive','-Command',code],
                env=env,capture_output=True,text=True,encoding='utf-8',timeout=30)
            self.assertEqual(process.returncode,0,process.stderr)
            actual=json.loads(process.stdout.strip().splitlines()[-1])
            self.assertEqual(actual,['--port','8788','--max-message-age-minutes','15','--temp-dir',str(runtime),
                                     '--admin-fee-db',str(fee),'--enable-store-text-orientation'])
            self.assertEqual(fee.read_bytes(),b'fixture')

    def test_managed_start_refuses_implicit_personal_runtime_before_creating_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder).resolve(); runtime=root/'unused'
            env={**os.environ,'TIKU_BANK_STORE':str(root/'published'),'LIDA_TEST_RUNTIME':str(runtime)}
            script=Path(__file__).resolve().parents[1]/'scripts/tiku_bot_watchdog.ps1'
            process=subprocess.run([shutil.which('pwsh') or 'pwsh','-NoProfile','-NonInteractive','-File',str(script),
                '-RuntimeDir',str(runtime),'-FunctionsOnly'],env=env,capture_output=True,text=True,encoding='utf-8',timeout=15)
            self.assertNotEqual(process.returncode,0)
            self.assertIn('requires explicit runtime',process.stderr)
            self.assertFalse(runtime.exists())

    def test_restart_cleans_stale_listener_and_fails_closed(self):
        script = (
            Path(__file__).resolve().parents[1] / "scripts" / "tiku_bot_watchdog.ps1"
        ).read_text(encoding="utf-8")

        self.assertIn("function Stop-PortProcess", script)
        self.assertIn("for ($attempt = 1; $attempt -le 2; $attempt++)", script)
        self.assertIn("function Wait-PortFree", script)
        self.assertIn("function Wait-BotHealthy", script)
        self.assertIn("if (Wait-BotHealthy)", script)
        self.assertNotIn("Start-Sleep -Seconds 4", script)
        self.assertIn("Get-NetTCPConnection -LocalPort $Port -State Listen", script)
        self.assertIn("Stop-Process -Id $processId -Force -ErrorAction Stop", script)
        restart = script.index("if (-not $botProcess -or $botProcess.HasExited")
        cleanup = script.index("Stop-PortProcess", restart)
        start = script.index("$botProcess = Start-Bot", restart)
        self.assertLess(cleanup, start)
        self.assertLess(script.index("Wait-PortFree", cleanup), start)
        self.assertIn("[switch]$EnableStoreTextOrientation", script)
        self.assertIn('$arguments += "--enable-store-text-orientation"', script)


if __name__ == "__main__":
    unittest.main()
