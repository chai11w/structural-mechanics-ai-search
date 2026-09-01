import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


POWERSHELL = shutil.which("powershell.exe") or shutil.which("powershell")
GIT = shutil.which("git.exe") or shutil.which("git")


class TikuAgentWatchdog8790Test(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parents[1]
        self.script_path = self.root / "scripts" / "tiku_agent_watchdog_8790.ps1"
        self.safety_path = (
            self.root / "scripts" / "tiku_agent_watchdog_8790_safety.ps1"
        )
        self.guard_path = self.root / "scripts" / "watchdog_process_guard.ps1"
        self.switch_path = (
            self.root / "scripts" / "switch_tiku_agent_8790_control.ps1"
        )
        self.script = self.script_path.read_text(encoding="utf-8")
        self.switch_script = self.switch_path.read_text(encoding="utf-8")

    def _run_powershell(self, command: str) -> subprocess.CompletedProcess[str]:
        assert POWERSHELL is not None
        return subprocess.run(
            [
                POWERSHELL,
                "-NoProfile",
                "-NonInteractive",
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

    def test_watchdog_runs_a3_v1_with_control_db_and_manual_crop_rollback(self):
        script = self.script

        self.assertIn('watchdog_process_guard.ps1', script)
        self.assertIn('tiku_agent_watchdog_8790_safety.ps1', script)
        self.assertIn('if ($Port -ne 8790)', script)
        self.assertIn(
            'Join-Path $ProjectDir "scripts\\run_tiku_agent_8790.py"',
            script,
        )
        self.assertIn("$AgentEntrypoint = (Resolve-Path", script)
        self.assertIn('"-B",', script)
        self.assertIn("$BotLaunchArguments", script)
        self.assertIn("Start-Process -FilePath $ExpectedPythonPath", script)
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
        self.assertIn(
            "[Parameter(Mandatory = $true)][string]$ReleaseManifest", script
        )
        self.assertIn(
            "[Parameter(Mandatory = $true)][string]$ExpectedCommit", script
        )
        self.assertIn("Assert-Tiku8790ReleaseIdentity", script)
        self.assertNotIn("[switch]$DisableExternalLoadScreen", script)
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
        self.assertIn("-FilePath $ExpectedPythonPath", start_body)
        self.assertIn("-ArgumentList $BotLaunchArguments", start_body)
        self.assertLess(
            start_body.index("Assert-Tiku8790ReleaseIdentity"),
            start_body.index("Start-Process -FilePath $ExpectedPythonPath"),
        )
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

    def test_control_switch_is_release_bound_and_apply_gated(self):
        script = self.switch_script

        for parameter in ("ReleaseManifest", "ExpectedCommit", "BackupProjectRoot"):
            self.assertIn(
                "[Parameter(Mandatory = $true)][string]$" + parameter,
                script,
            )
        self.assertIn("watchdog_process_guard.ps1", script)
        self.assertIn("tiku_agent_watchdog_8790_safety.ps1", script)

        first_release_check = (
            "$ReleaseIdentity = Assert-Tiku8790ReleaseIdentity "
            "@releaseIdentityParameters"
        )
        self.assertIn(first_release_check, script)
        self.assertLess(
            script.index(first_release_check),
            script.index("function Test-Health"),
        )

        start_body = script[
            script.index("function Start-Watchdog") : script.index(
                "function Stop-ExactProcess"
            )
        ]
        self.assertIn(
            "$verifiedRelease = Assert-Tiku8790ReleaseIdentity "
            "@releaseIdentityParameters",
            start_body,
        )
        self.assertIn('"-ReleaseManifest", $verifiedRelease.manifest', start_body)
        self.assertIn('"-ExpectedCommit", $verifiedRelease.commit', start_body)
        self.assertIn('"-PythonExe", $verifiedRelease.python', start_body)
        self.assertIn("ConvertTo-Tiku8790CommandLineArgument", start_body)
        self.assertIn("FilePath = $PowerShellExe", start_body)
        self.assertNotIn("Start-Process powershell.exe", start_body)
        self.assertLess(
            start_body.index("$verifiedRelease = Assert-Tiku8790ReleaseIdentity"),
            start_body.index("Start-Process @startParameters"),
        )

        apply_gate = script.index("if (-not $Apply)")
        manage_import = script.index('Join-Path $PSScriptRoot "manage_tiku_admin.py"')
        backup = script.index("source.backup(destination)")
        revalidate = script.index(
            "$revalidatedListenerPid = Assert-CurrentProcesses"
        )
        first_stop = script.index("Stop-ExactProcess $detectedWatchdogPid")
        self.assertLess(apply_gate, backup)
        self.assertLess(backup, manage_import)
        self.assertLess(manage_import, revalidate)
        self.assertLess(revalidate, first_stop)
        self.assertIn("--apply-import", script)
        pre_apply = script[:apply_gate]
        self.assertNotIn("manage_tiku_admin.py", pre_apply)
        self.assertNotIn("http://127.0.0.1:8795/health", pre_apply)

    @unittest.skipUnless(POWERSHELL, "Windows PowerShell is required")
    def test_release_identity_requires_matching_clean_manifest(self):
        safety = str(self.safety_path).replace("'", "''")
        commit = "a" * 40
        with tempfile.TemporaryDirectory(prefix="tiku 8790 release ") as temporary:
            project = Path(temporary) / "release checkout"
            entrypoint = project / "scripts" / "run_tiku_agent_8790.py"
            runtime = Path(temporary) / "runtime with space"
            entrypoint.parent.mkdir(parents=True)
            entrypoint.write_text("# fixture\n", encoding="utf-8")
            (project / ".git").write_text("gitdir: fixture\n", encoding="utf-8")
            git_dir = Path(temporary) / ".git" / "worktrees" / "release"
            common_dir = Path(temporary) / ".git"
            manifest = project / "release.json"
            missing_commit = project / "missing-commit.json"
            payload = {
                "schema": "tiku-agent-8790-release-v1",
                "commit": commit,
                "checkout": ".",
                "agent_entrypoint": r"scripts\run_tiku_agent_8790.py",
                "python": str(Path(sys.executable).resolve()),
                "runtime": str(runtime),
            }
            manifest.write_text(json.dumps(payload), encoding="utf-8")
            missing_commit.write_text(
                json.dumps({**payload, "commit": ""}), encoding="utf-8"
            )
            project_value = str(project).replace("'", "''")
            entrypoint_value = str(entrypoint).replace("'", "''")
            python_value = str(Path(sys.executable).resolve()).replace("'", "''")
            runtime_value = str(runtime).replace("'", "''")
            git_dir_value = str(git_dir).replace("'", "''")
            common_dir_value = str(common_dir).replace("'", "''")
            result = self._run_powershell(
                f"""
. '{safety}'
$clean = {{
  param([string]$ProjectDirectory, [string[]]$GitArguments)
  switch ($GitArguments -join ' ') {{
    'rev-parse --show-toplevel' {{ return $ProjectDirectory }}
    'rev-parse --path-format=absolute --git-dir' {{ return '{git_dir_value}' }}
    'rev-parse --path-format=absolute --git-common-dir' {{ return '{common_dir_value}' }}
    'rev-parse --verify HEAD' {{ return '{commit}' }}
    'status --porcelain=v1 --untracked-files=all --ignore-submodules=none' {{ return @() }}
    default {{ throw 'unexpected git query' }}
  }}
}}
$valid = Assert-Tiku8790ReleaseIdentity -ManifestPath 'release.json' `
  -ExpectedCommit '{commit}' -ProjectDirectory '{project_value}' `
  -AgentEntrypoint '{entrypoint_value}' -PythonExecutable '{python_value}' `
  -RuntimeDirectory '{runtime_value}' -GitQuery $clean
$missing = $false
try {{ Assert-Tiku8790ReleaseIdentity -ManifestPath 'absent.json' -ExpectedCommit '{commit}' -ProjectDirectory '{project_value}' -AgentEntrypoint '{entrypoint_value}' -PythonExecutable '{python_value}' -RuntimeDirectory '{runtime_value}' -GitQuery $clean }} catch {{ $missing = $_.Exception.Message -like '*manifest not found*' }}
$inconsistent = $false
try {{ Assert-Tiku8790ReleaseIdentity -ManifestPath 'release.json' -ExpectedCommit '{'b' * 40}' -ProjectDirectory '{project_value}' -AgentEntrypoint '{entrypoint_value}' -PythonExecutable '{python_value}' -RuntimeDirectory '{runtime_value}' -GitQuery $clean }} catch {{ $inconsistent = $_.Exception.Message -like '*does not match the release manifest*' }}
$noCommit = $false
try {{ Assert-Tiku8790ReleaseIdentity -ManifestPath 'missing-commit.json' -ExpectedCommit '{commit}' -ProjectDirectory '{project_value}' -AgentEntrypoint '{entrypoint_value}' -PythonExecutable '{python_value}' -RuntimeDirectory '{runtime_value}' -GitQuery $clean }} catch {{ $noCommit = $_.Exception.Message -like '*missing a release commit*' }}
$dirtyQuery = {{ param([string]$ProjectDirectory, [string[]]$GitArguments) switch ($GitArguments -join ' ') {{ 'rev-parse --show-toplevel' {{ return $ProjectDirectory }} 'rev-parse --path-format=absolute --git-dir' {{ return '{git_dir_value}' }} 'rev-parse --path-format=absolute --git-common-dir' {{ return '{common_dir_value}' }} 'rev-parse --verify HEAD' {{ return '{commit}' }} default {{ return '?? drift.txt' }} }} }}
$dirty = $false
try {{ Assert-Tiku8790ReleaseIdentity -ManifestPath 'release.json' -ExpectedCommit '{commit}' -ProjectDirectory '{project_value}' -AgentEntrypoint '{entrypoint_value}' -PythonExecutable '{python_value}' -RuntimeDirectory '{runtime_value}' -GitQuery $dirtyQuery }} catch {{ $dirty = $_.Exception.Message -like '*not clean*' }}
$primary = $false
Remove-Item -LiteralPath (Join-Path '{project_value}' '.git') -Force
New-Item -ItemType Directory -Path (Join-Path '{project_value}' '.git') | Out-Null
try {{ Assert-Tiku8790ReleaseIdentity -ManifestPath 'release.json' -ExpectedCommit '{commit}' -ProjectDirectory '{project_value}' -AgentEntrypoint '{entrypoint_value}' -PythonExecutable '{python_value}' -RuntimeDirectory '{runtime_value}' -GitQuery $clean }} catch {{ $primary = $_.Exception.Message -like '*linked Git worktree*' }}
Write-Output "$($valid.commit)|$missing|$inconsistent|$noCommit|$dirty|$primary"
"""
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.strip().splitlines()[-1],
            f"{commit}|True|True|True|True|True",
        )

    @unittest.skipUnless(
        POWERSHELL and GIT,
        "Windows PowerShell and Git are required",
    )
    def test_release_git_query_decodes_unicode_without_global_config(self):
        safety = str(self.safety_path).replace("'", "''")
        with tempfile.TemporaryDirectory(prefix="tiku release git ") as temporary:
            repository = Path(temporary) / "题库 release"
            initialized = subprocess.run(
                [str(GIT), "init", str(repository)],
                text=True,
                encoding="utf-8",
                capture_output=True,
                check=False,
            )
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            repository_value = str(repository.resolve()).replace("'", "''")
            result = self._run_powershell(
                f"""
$env:GIT_CONFIG_GLOBAL = 'NUL'
$env:GIT_CONFIG_SYSTEM = 'NUL'
. '{safety}'
$output = @(Invoke-Tiku8790ReleaseGit -ProjectDirectory '{repository_value}' -GitArguments @('rev-parse', '--show-toplevel'))
$matches = $output.Count -eq 1 -and [string]::Equals(
  [System.IO.Path]::GetFullPath(([string]$output[0]).Trim()),
  [System.IO.Path]::GetFullPath('{repository_value}'),
  [System.StringComparison]::OrdinalIgnoreCase
)
Write-Output "$matches|$($output.Count)"
"""
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip().splitlines()[-1], "True|1")

    @unittest.skipUnless(POWERSHELL, "Windows PowerShell is required")
    def test_windows_argv_encoding_preserves_absolute_entrypoint(self):
        safety = str(self.safety_path).replace("'", "''")
        guard = str(self.guard_path).replace("'", "''")
        python = str(Path(sys.executable).resolve()).replace("'", "''")
        entrypoint = r"F:\release checkout --port 8794\scripts\run 8790.py"
        result = self._run_powershell(
            f"""
. '{safety}'
. '{guard}'
$logical = @('-B', '{entrypoint}', '--host', '127.0.0.1', '--port', '8790')
$encoded = @($logical | ForEach-Object {{ ConvertTo-Tiku8790CommandLineArgument -Argument ([string]$_) }})
$line = (ConvertTo-Tiku8790CommandLineArgument -Argument '{python}') + ' ' + ($encoded -join ' ')
$matched = Test-WatchdogLaunchEvidence -ProcessId 321 -ExecutablePath '{python}' -ExpectedExecutablePath '{python}' -CommandLine $line -ExpectedArguments $logical
Write-Output $matched
"""
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip().splitlines()[-1], "True")


if __name__ == "__main__":
    unittest.main()
