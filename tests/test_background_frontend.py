"""Browser-client contract checks with adversarial transport/storage boundaries."""
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]


class BackgroundFrontendTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node.js required')
    def test_detached_client_failure_and_recovery_contract(self):
        result = subprocess.run([shutil.which('node'), 'tests/phase6_background_client_checks.js'],
                                cwd=ROOT, capture_output=True, text=True, encoding='utf-8', timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r'\b\d+ client checks passed')
