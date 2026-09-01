from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


POWERSHELL = shutil.which("powershell.exe") or shutil.which("powershell")
GIT = shutil.which("git.exe") or shutil.which("git")


class TikuTaskStateStage352RunnerTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parents[1]
        self.runner_path = (
            self.root / "scripts" / "run_tiku_task_state_stage_3_5_2_8896.ps1"
        )
        self.safety_path = (
            self.root / "scripts" / "tiku_task_state_stage_3_5_2_8896_safety.ps1"
        )
        self.runner = self.runner_path.read_text(encoding="utf-8")
        self.safety = self.safety_path.read_text(encoding="utf-8")

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

    def test_runner_has_only_the_fixed_stage_surface(self):
        for required in (
            "[Parameter(Mandatory = $true)][string]$ExpectedCommit",
            "[Parameter(Mandatory = $true)][string]$CheckoutDir",
            "[Parameter(Mandatory = $true)][string]$GitExe",
            "[Parameter(Mandatory = $true)][string]$PythonExe",
            "[Parameter(Mandatory = $true)][string]$PowerShellExe",
            "[Parameter(Mandatory = $true)][string]$RuntimeRoot",
            "[switch]$StaticPreflightOnly",
        ):
            self.assertIn(required, self.runner)
        self.assertNotIn("[int]$Port", self.runner)
        self.assertNotIn("[string]$BaseUrl", self.runner)
        for protected_port in (8788, 8790, 8794, 8795):
            self.assertNotIn(str(protected_port), self.runner)
            self.assertNotIn(str(protected_port), self.safety)

    def test_runner_binds_checkout_runtime_and_both_process_identities(self):
        for required in (
            "Assert-TikuTaskStateStageCheckout",
            "Resolve-TikuTaskStateStageGit",
            "Resolve-TikuTaskStateStageRuntimeRoot",
            "New-TikuTaskStateStageFreshRuntime",
            "Get-Tiku8896ScopedListeningProcessIds -Port 8896",
            "Test-StageWatchdogIdentity",
            "Test-StageAgentIdentity",
            "Test-StageAgentLaunchIdentity",
            "Get-TikuTaskStateStageExactChildProcessIds",
            "Get-TikuTaskStateStageChildProcessIds",
            "ParentProcessId",
            '"-File", $WatchdogScript',
            '"-RuntimeDir", $runtime',
            '"-PythonExe", $ResolvedPython',
            '"-RequireScopedListenerQuery"',
            '"--runtime-dir", $runtime',
            "--expected-commit $ExpectedCommit",
            "--runtime-identity $runtime",
        ):
            self.assertIn(required, self.runner)
        self.assertIn("linked Git worktree", self.safety)
        self.assertIn("primary checkout", self.safety)
        self.assertIn("The stage checkout is not clean", self.safety)

    def test_runner_stops_only_reverified_objects_in_the_required_order(self):
        self.assertIn("Stop-Process -InputObject $process", self.safety)
        self.assertNotIn("Stop-Process -Id", self.runner)
        self.assertNotIn("Stop-Process -Id", self.safety)
        finally_body = self.runner[self.runner.index("} finally {") :]
        watchdog_stop = finally_body.index('-Role "watchdog"')
        agent_stop = finally_body.index('-Role "agent"')
        final_listener_check = finally_body.index("$remainingOwners")
        self.assertLess(watchdog_stop, agent_stop)
        self.assertLess(agent_stop, final_listener_check)
        self.assertIn("Refusing to stop an unverified", self.safety)
        self.assertIn("$remainingChildren", finally_body)
        self.assertIn("$clearCheck -lt 2", finally_body)
        self.assertIn("surviving direct watchdog child PIDs", finally_body)
        self.assertNotIn("Cleanup found a changed 8896 agent PID", finally_body)

    def test_runner_stops_before_reading_sqlite_evidence(self):
        cleanup = self.runner.index("} finally {")
        offline = self.runner.index("$offlineOutput")
        manifest = self.runner.index("$manifest =")
        self.assertLess(cleanup, offline)
        self.assertLess(offline, manifest)
        self.assertIn("--evidence-output $EvidenceFile", self.runner)
        self.assertIn("--evidence-input $EvidenceFile", self.runner)
        self.assertIn('status = "http_sqlite_ok"', self.runner)
        self.assertIn('cleanup = "stopped"', self.runner)

    def test_runner_preserves_primary_and_cleanup_failure_diagnostics(self):
        self.assertIn("$primaryError.Exception.Message", self.runner)
        self.assertIn("$cleanupErrors", self.runner)
        self.assertIn("$cleanupMessage", self.runner)
        self.assertIn("$primaryError.Exception", self.runner)

    @unittest.skipUnless(POWERSHELL, "Windows PowerShell is required")
    def test_checkout_guard_accepts_only_clean_linked_expected_commit(self):
        safety = str(self.safety_path).replace("'", "''")
        expected = "a" * 40
        with tempfile.TemporaryDirectory(prefix="tiku stage checkout ") as temp_dir:
            temp = Path(temp_dir)
            checkout = temp / "linked checkout"
            checkout.mkdir()
            (checkout / ".git").write_text("gitdir: placeholder\n", encoding="utf-8")
            checkout_value = str(checkout).replace("'", "''")
            git_dir = str(temp / ".git" / "worktrees" / "stage").replace("'", "''")
            common_dir = str(temp / ".git").replace("'", "''")
            result = self._run_powershell(
                f"""
. '{safety}'
$gitQuery = {{
  param([string]$ProjectDirectory, [string[]]$GitArguments)
  $key = $GitArguments -join ' '
  switch ($key) {{
    'rev-parse --show-toplevel' {{ return $ProjectDirectory }}
    'rev-parse --path-format=absolute --git-dir' {{ return '{git_dir}' }}
    'rev-parse --path-format=absolute --git-common-dir' {{ return '{common_dir}' }}
    'rev-parse HEAD' {{ return '{expected}' }}
    'status --porcelain=v1 --untracked-files=all --ignore-submodules=none' {{ return @() }}
    default {{ throw 'unexpected git query' }}
  }}
}}
$accepted = Assert-TikuTaskStateStageCheckout `
  -ProjectDirectory '{checkout_value}' `
  -ExpectedCommit '{expected}' `
  -GitQuery $gitQuery
$dirtyQuery = {{
  param([string]$ProjectDirectory, [string[]]$GitArguments)
  $key = $GitArguments -join ' '
  switch ($key) {{
    'rev-parse --show-toplevel' {{ return $ProjectDirectory }}
    'rev-parse --path-format=absolute --git-dir' {{ return '{git_dir}' }}
    'rev-parse --path-format=absolute --git-common-dir' {{ return '{common_dir}' }}
    'rev-parse HEAD' {{ return '{expected}' }}
    'status --porcelain=v1 --untracked-files=all --ignore-submodules=none' {{
      return ' M scripts/file.py'
    }}
    default {{ throw 'unexpected git query' }}
  }}
}}
$dirtyBlocked = $false
try {{
  Assert-TikuTaskStateStageCheckout `
    -ProjectDirectory '{checkout_value}' `
    -ExpectedCommit '{expected}' `
    -GitQuery $dirtyQuery | Out-Null
}} catch {{
  $dirtyBlocked = $_.Exception.Message -like '*not clean*'
}}
$wrongCommitBlocked = $false
try {{
  Assert-TikuTaskStateStageCheckout `
    -ProjectDirectory '{checkout_value}' `
    -ExpectedCommit '{'b' * 40}' `
    -GitQuery $gitQuery | Out-Null
}} catch {{
  $wrongCommitBlocked = $_.Exception.Message -like '*expected commit*'
}}
Write-Output "$accepted|$dirtyBlocked|$wrongCommitBlocked"
"""
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.strip().splitlines()[-1],
            f"{checkout}|True|True",
        )

    @unittest.skipUnless(POWERSHELL, "Windows PowerShell is required")
    def test_runtime_guard_requires_external_dedicated_root_and_fresh_child(self):
        safety = str(self.safety_path).replace("'", "''")
        with tempfile.TemporaryDirectory(prefix="tiku stage runtime ") as temp_dir:
            temp = Path(temp_dir)
            checkout = temp / "checkout"
            common = temp / ".git"
            runtime_root = temp / ".tmp_tiku_task_state_stage_3_5_2_8896_runs"
            checkout.mkdir()
            common.mkdir()
            checkout_value = str(checkout).replace("'", "''")
            common_value = str(common).replace("'", "''")
            runtime_value = str(runtime_root).replace("'", "''")
            inside_value = str(
                checkout / ".tmp_tiku_task_state_stage_3_5_2_8896_runs"
            ).replace("'", "''")
            result = self._run_powershell(
                f"""
. '{safety}'
$root = Resolve-TikuTaskStateStageRuntimeRoot `
  -RuntimeRoot '{runtime_value}' `
  -ProjectDirectory '{checkout_value}' `
  -GitCommonDirectory '{common_value}'
$runtime = New-TikuTaskStateStageFreshRuntime -RuntimeRoot $root
$insideBlocked = $false
try {{
  Resolve-TikuTaskStateStageRuntimeRoot `
    -RuntimeRoot '{inside_value}' `
    -ProjectDirectory '{checkout_value}' `
    -GitCommonDirectory '{common_value}' | Out-Null
}} catch {{
  $insideBlocked = $_.Exception.Message -like '*outside the fixed checkout*'
}}
$relativeBlocked = $false
try {{
  Resolve-TikuTaskStateStageRuntimeRoot `
    -RuntimeRoot '.tmp_tiku_task_state_stage_3_5_2_8896_runs' `
    -ProjectDirectory '{checkout_value}' `
    -GitCommonDirectory '{common_value}' | Out-Null
}} catch {{
  $relativeBlocked = $_.Exception.Message -like '*must be absolute*'
}}
$reparseBlocked = $false
try {{
  Assert-TikuTaskStateStageNoReparseAncestors `
    -Path $root `
    -ItemQuery {{
      param($Candidate)
      [pscustomobject]@{{
        Attributes = [System.IO.FileAttributes]::ReparsePoint
      }}
    }}
}} catch {{
  $reparseBlocked = $_.Exception.Message -like '*reparse point*'
}}
Write-Output "$([System.IO.Path]::GetFileName($root))|$([System.IO.Path]::GetFileName($runtime))|$insideBlocked|$relativeBlocked|$reparseBlocked"
"""
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        fields = result.stdout.strip().splitlines()[-1].split("|")
        self.assertEqual(fields[0], ".tmp_tiku_task_state_stage_3_5_2_8896_runs")
        self.assertRegex(fields[1], r"^run_\d{8}T\d{9}Z_[0-9a-f]{12}$")
        self.assertEqual(fields[2:], ["True", "True", "True"])

    @unittest.skipUnless(POWERSHELL, "Windows PowerShell is required")
    def test_exact_child_query_requires_parent_executable_and_complete_argv(self):
        safety = str(self.safety_path).replace("'", "''")
        guard = str(self.root / "scripts" / "watchdog_process_guard.ps1").replace(
            "'", "''"
        )
        python = str(Path(sys.executable).resolve()).replace("'", "''")
        result = self._run_powershell(
            f"""
. '{guard}'
. '{safety}'
$expected = @('-B', 'F:\\checkout\\run.py', '--runtime-dir', 'F:\\runtime\\fresh')
$line = '"{python}" -B F:\\checkout\\run.py --runtime-dir F:\\runtime\\fresh'
$records = @(
  [pscustomobject]@{{
    ProcessId = 321; ParentProcessId = 123; ExecutablePath = '{python}'; CommandLine = $line
  }},
  [pscustomobject]@{{
    ProcessId = 322; ParentProcessId = 999; ExecutablePath = '{python}'; CommandLine = $line
  }},
  [pscustomobject]@{{
    ProcessId = 323; ParentProcessId = 123; ExecutablePath = '{python}';
    CommandLine = '"{python}" -B F:\\checkout\\run.py --runtime-dir F:\\runtime\\other'
  }}
)
$ids = @(Get-TikuTaskStateStageExactChildProcessIds `
  -ParentProcessId 123 `
  -ExpectedExecutablePath '{python}' `
  -ExpectedArguments $expected `
  -ProcessQuery {{ param($ParentProcessId) $records }})
$rawIds = @(Get-TikuTaskStateStageChildProcessIds `
  -ParentProcessId 123 `
  -ProcessQuery {{ param($ParentProcessId) $records }})
Write-Output "$(($ids -join ','))|$(($rawIds -join ','))"
"""
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip().splitlines()[-1], "321|321,323")

    @unittest.skipUnless(POWERSHELL and GIT, "Windows PowerShell and Git are required")
    def test_executable_guards_reject_path_names_and_wrong_binaries(self):
        safety = str(self.safety_path).replace("'", "''")
        git = str(Path(GIT).resolve()).replace("'", "''")
        python = str(Path(sys.executable).resolve()).replace("'", "''")
        powershell = str(Path(POWERSHELL).resolve()).replace("'", "''")
        result = self._run_powershell(
            f"""
. '{safety}'
$git = Resolve-TikuTaskStateStageGit -GitExecutable '{git}'
$python = Resolve-TikuTaskStateStagePython -PythonExecutable '{python}'
$powershell = Resolve-TikuTaskStateStagePowerShell -PowerShellExecutable '{powershell}'
$relativeGitBlocked = $false
try {{ Resolve-TikuTaskStateStageGit -GitExecutable 'git' }} catch {{
  $relativeGitBlocked = $_.Exception.Message -like '*must be absolute*'
}}
$relativePythonBlocked = $false
try {{ Resolve-TikuTaskStateStagePython -PythonExecutable 'python' }} catch {{
  $relativePythonBlocked = $_.Exception.Message -like '*must be absolute*'
}}
$wrongBinaryBlocked = $false
try {{ Resolve-TikuTaskStateStagePython -PythonExecutable '{powershell}' }} catch {{
  $wrongBinaryBlocked = $_.Exception.Message -like '*not an explicit Python*'
}}
$head = @(Invoke-TikuTaskStateStageGit `
  -ProjectDirectory '{str(self.root).replace("'", "''")}' `
  -GitArguments @('rev-parse', 'HEAD') `
  -GitExecutable $git)
Write-Output "$([System.IO.Path]::GetFileName($git))|$([System.IO.Path]::GetFileName($python))|$([System.IO.Path]::GetFileName($powershell))|$relativeGitBlocked|$relativePythonBlocked|$wrongBinaryBlocked|$($head[0].Length)"
"""
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        fields = result.stdout.strip().splitlines()[-1].split("|")
        self.assertEqual(fields[0].lower(), "git.exe")
        self.assertEqual(fields[1].lower(), "python.exe")
        self.assertIn(fields[2].lower(), {"powershell.exe", "pwsh.exe"})
        self.assertEqual(fields[3:], ["True", "True", "True", "40"])


if __name__ == "__main__":
    unittest.main()
