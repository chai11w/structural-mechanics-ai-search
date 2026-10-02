from pathlib import Path
import shutil
import subprocess
import unittest


class ChineseErrorFrontendTests(unittest.TestCase):
    def test_error_display_boundary(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('Node.js is unavailable')
        result = subprocess.run(
            [node, 'tests/phase6_chinese_error_checks.js'],
            cwd=Path(__file__).resolve().parents[1],
            text=True, capture_output=True, encoding='utf-8',
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Chinese error display checks passed', result.stdout)
