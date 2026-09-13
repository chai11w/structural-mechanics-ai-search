import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tiku_shared.configuration import external_configuration, MAX_CONFIGURATION_BYTES


class ExternalConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="tiku-role-config-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.source = self.root / "source"; self.source.mkdir()
        self.config = self.root / "role.json"
        self.config.write_text(json.dumps({"marker": "explicit", "root": str(self.root / "bank")}), encoding="utf-8")

    def test_actual_search_and_recognition_loaders_share_explicit_config_and_keep_legacy_order(self):
        code = r'''
import json, os, sys
from pathlib import Path
import search
from scripts import classify_question_bank, build_symbolic_bank, apply_main_bank_update, evaluate_complex_image_routing
source=Path(sys.argv[1]); expected=json.loads(Path(os.environ['TIKU_CONFIG_FILE']).read_bytes())
search.__file__=str(source/'search.py')
modules=[classify_question_bank, build_symbolic_bank, apply_main_bank_update, evaluate_complex_image_routing]
for module in modules: module.BASE=source
loaders=[search.load_local_config, classify_question_bank.load_config, build_symbolic_bank.load_config,
         apply_main_bank_update.load_config, evaluate_complex_image_routing.load_local_config]
(source/'config.json').write_text('{"marker":"base","local_only":true}')
(source/'config.local.json').write_text('{"marker":"local"}')
for load in loaders: assert load()==expected
del os.environ['TIKU_CONFIG_FILE']
for load in loaders: assert load()=={'marker':'local','local_only':True}
print('five actual loaders verified')
'''
        env = {**os.environ, "TIKU_CONFIG_FILE": str(self.config)}
        env.pop("TIKU_BANK_STORE", None)
        process = subprocess.run([sys.executable, "-B", "-X", "utf8", "-c", code, str(self.source)],
            cwd=Path(__file__).resolve().parents[1], env=env, capture_output=True, text=True, encoding="utf-8", timeout=45)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertIn("five actual loaders verified", process.stdout)

    def test_selected_missing_or_invalid_file_never_falls_back(self):
        (self.source / "config.json").write_text('{"marker":"local"}')
        with patch.dict(os.environ, {"TIKU_CONFIG_FILE": ""}):
            self.assertIsNone(external_configuration(self.source))
        for value in ([], {"a": float("nan")}):
            self.config.write_text(json.dumps(value))
            with self.subTest(value=type(value).__name__), patch.dict(os.environ, {"TIKU_CONFIG_FILE": str(self.config)}):
                with self.assertRaisesRegex(ValueError, "unavailable or invalid"):
                    external_configuration(self.source)
        for raw in ('{"a":1,"a":2}', '{"nested":{"a":1,"a":2}}', '{"a":1e400}', ' ' * (MAX_CONFIGURATION_BYTES + 1)):
            self.config.write_text(raw)
            with patch.dict(os.environ, {"TIKU_CONFIG_FILE": str(self.config)}):
                with self.assertRaises(ValueError): external_configuration(self.source)
        for path in (self.root / "missing.json", self.source / "config.json", Path("relative.json")):
            with self.subTest(path=path.name), patch.dict(os.environ, {"TIKU_CONFIG_FILE": str(path)}):
                with self.assertRaises(ValueError): external_configuration(self.source)

    def test_linked_config_is_rejected_without_reading_an_alternate_file(self):
        linked = self.root / "linked.json"
        os.link(self.config, linked)
        before = self.config.read_bytes()
        with patch.dict(os.environ, {"TIKU_CONFIG_FILE": str(linked)}):
            with self.assertRaises(ValueError): external_configuration(self.source)
        self.assertEqual(self.config.read_bytes(), before)

    def test_feishu_fee_database_option_preserves_default_and_accepts_explicit_source(self):
        code = r'''
import sys
from pathlib import Path
from unittest.mock import patch
from scripts import feishu_tiku_bot as bot
with patch.object(bot, 'get_env_or_user', return_value=''):
    assert bot.load_options(bot.build_parser().parse_args([])).admin_fee_db == bot.DEFAULT_FEE_DB
    chosen=Path(sys.argv[1])
    assert bot.load_options(bot.build_parser().parse_args(['--admin-fee-db',str(chosen)])).admin_fee_db == chosen
'''
        env = {**os.environ, "TIKU_CONFIG_FILE": str(self.config)}; env.pop("TIKU_BANK_STORE", None)
        result = subprocess.run([sys.executable, "-B", "-X", "utf8", "-c", code, str(self.root / "costs.sqlite3")],
            cwd=Path(__file__).resolve().parents[1], env=env, capture_output=True, text=True, encoding="utf-8", timeout=45)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
