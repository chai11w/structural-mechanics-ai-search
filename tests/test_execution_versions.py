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

    def test_default_runtime_graph_tracks_nested_prompts_options_and_tools(self):
        from scripts.run_tiku_agent_8896 import build_runtime
        from tiku_agent.execution_versions import runtime_version
        from unittest.mock import patch
        with patch("urllib.request.urlopen") as send:
            runtime = build_runtime(self.prompt.parent / "runtime")
            first = runtime_version(runtime)
            self.assertEqual(runtime_version(runtime), first)
            runtime.image_triage_authority.observer.prompt_path = self.prompt
            before = runtime_version(runtime)
            self.prompt.write_text("changed triage prompt", encoding="utf-8")
            self.assertNotEqual(runtime_version(runtime), before)
            for target, field, value in (
                (runtime.page_observer, "model", "new-model"),
                (runtime.auto_cropper, "timeout_seconds", 17.0),
                (runtime, "auto_prepare_all_units", not runtime.auto_prepare_all_units),
                (runtime.image_triage_authority.reply_client, "SYSTEM_PROMPT", "new reply prompt"),
            ):
                with self.subTest(field=field):
                    before = runtime_version(runtime)
                    setattr(target, field, value)
                    self.assertNotEqual(runtime_version(runtime), before)
            # Replacing a factory with an undeclared adapter cannot retain the
            # version of the old standard tool/model pipeline.
            runtime.a2_runtime.agent_factory = lambda state: None
            with self.assertRaises(ExecutionError):
                runtime_version(runtime)
            send.assert_not_called()

    def test_attach_rejects_unversioned_factory_before_replacing_store(self):
        from tiku_agent.execution_runtime import attach_execution
        from tiku_agent.execution_store import ExecutionStore, ExecutionSessionStore
        from tiku_agent.session_runtime import AgentSessionRuntime
        from tiku_agent.session_artifacts import SessionArtifacts
        authority = ExecutionStore(self.prompt.parent / "execution.db")
        original = ExecutionSessionStore(authority)
        runtime = AgentSessionRuntime(original, artifacts=SessionArtifacts(self.prompt.parent / "media"),
                                      agent_factory=lambda state: None)
        with self.assertRaises(ExecutionError):
            attach_execution(runtime, authority)
        self.assertIs(runtime.store, original)
        self.assertFalse(authority.require_writer)
        options = {"custom_pipeline": "v1"}
        attach_execution(runtime, authority, configuration_version=lambda: options)
        before = runtime.execution_operations.current_producer
        options["custom_pipeline"] = "v2"
        self.assertNotEqual(runtime.execution_operations.current_producer, before)


if __name__ == "__main__":
    unittest.main()
