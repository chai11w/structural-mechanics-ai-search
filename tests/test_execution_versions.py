from pathlib import Path
import tempfile
import unittest

from tiku_agent.a3_models import QwenA3CropVerifier
from tiku_agent.execution_store import ExecutionError
from tiku_agent.execution_versions import component_version
from tiku_agent.external_load_screen import QwenExternalLoadScreen, ZhipuExternalLoadScreen


class ExecutionVersionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.prompt = Path(temporary.name) / "custom.txt"
        self.prompt.write_text("first prompt", encoding="utf-8")

    def test_prompt_content_changes_without_path_change(self):
        client = QwenA3CropVerifier(prompt_path=self.prompt, api_key="test-key")
        before = component_version(client)
        self.prompt.write_text("other prompt", encoding="utf-8")
        self.assertNotEqual(component_version(client), before)
        self.assertRegex(before, r"^[0-9a-f]{64}$")

    def test_equal_prompt_bytes_in_different_locations_are_equivalent(self):
        client = QwenA3CropVerifier(prompt_path=self.prompt, api_key="test-key")
        before = component_version(client)
        alternate = self.prompt.with_name("renamed.txt")
        alternate.write_bytes(self.prompt.read_bytes())
        client.prompt_path = alternate
        self.assertEqual(component_version(client), before)

    def test_standard_request_fields_are_versioned_but_key_is_not(self):
        for client in (QwenExternalLoadScreen(api_key="test-key"), ZhipuExternalLoadScreen()):
            original = component_version(client)
            client.api_key = "another-test-key"
            self.assertEqual(component_version(client), original)
            for field, value in (("model", "another-model"), ("endpoint", "https://example.invalid"), ("timeout_seconds", 1)):
                with self.subTest(client=type(client).__name__, field=field):
                    before = component_version(client)
                    setattr(client, field, value)
                    self.assertNotEqual(component_version(client), before)

    def test_custom_structured_version_is_live_and_ignores_counters(self):
        class Client:
            options = {"implementation": "v1", "nested": {"threshold": 0.9}}
            calls = 0
            @property
            def api_key(self):
                raise AssertionError("must not inspect credentials")
            def execution_version(self):
                return self.options
        client = Client()
        before = component_version(client)
        client.calls += 1
        self.assertEqual(component_version(client), before)
        client.options["nested"]["threshold"] = 0.95
        self.assertNotEqual(component_version(client), before)

    def test_unversioned_custom_or_subclass_is_rejected(self):
        class Custom(QwenA3CropVerifier):
            pass
        for client in (lambda: None, Custom(prompt_path=self.prompt, api_key="test-key")):
            with self.assertRaises(ExecutionError) as error:
                component_version(client)
            self.assertEqual(error.exception.code, "EXECUTION_RESULT_UNAVAILABLE")

    def test_invalid_or_oversized_declaration_fails_closed(self):
        class Client:
            pass
        client = Client()
        for value in (None, "", {}, [], float("nan"), object(), "x" * 16385, [[0] * 256] * 256):
            with self.subTest(kind=type(value).__name__), self.assertRaises(ExecutionError):
                client.execution_version = value
                component_version(client)

    def test_prompt_read_failure_and_oversize_fail_closed(self):
        client = QwenA3CropVerifier(prompt_path=self.prompt, api_key="test-key")
        self.prompt.write_bytes(b"x" * (1024 * 1024 + 1))
        with self.assertRaises(ExecutionError):
            component_version(client)
        self.prompt.unlink()
        with self.assertRaises(ExecutionError) as error:
            component_version(client)
        self.assertNotIn(str(self.prompt), str(error.exception))


if __name__ == "__main__":
    unittest.main()
