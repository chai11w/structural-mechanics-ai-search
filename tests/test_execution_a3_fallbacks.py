"""Known A3 fallback results must survive the background execution wrapper."""
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from tests import test_execution_dispatch_a3 as fixtures
from tests.test_a3_runtime import FakeAutoCropper, FakeVerifier, FakeObserver
from tiku_shared.model_costs import timed_model_call


class A3FallbackTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.ExecutionDispatchA3Tests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.failures = []

    def fail(self, call_type):
        def send():
            self.failures.append(call_type)
            raise TimeoutError("controlled model outage")
        return timed_model_call(send, provider="dashscope", model="fixture",
                                call_type=call_type, usage_getter=lambda value: value)

    def assert_preserved(self, kind):
        with self.f.store.transaction() as c:
            rows = c.execute("SELECT status,usage_known FROM execution_effects WHERE status='UNKNOWN'").fetchall()
            self.assertTrue(rows)
            self.assertTrue(all(row["usage_known"] == 0 for row in rows))
            results = [json.loads(row[0]) for row in c.execute("SELECT result FROM execution_operations WHERE result IS NOT NULL")]
            self.assertTrue(any(kind in result.get("model_fallbacks", {}).values() for result in results))

    def test_auto_grounding_failure_still_reaches_manual_crop(self):
        f = self.f
        f.a3.auto_cropper = FakeAutoCropper()
        with patch.object(f.a3.auto_cropper, "ground", side_effect=lambda *a: self.fail("glm_a3_page_auto_crop")):
            f.run_command("handle_image", {}, image=f.image)
        self.assertFalse(f.a3.store.load("s").auto_crop_enabled)
        f.run_command("select_unit", {"unit_id": "g1-u1", **f.target()})
        self.assertEqual(f.a3.store.load("s").phase, "CROP_REQUIRED")
        self.assert_preserved("crop_manual")
        self.assertEqual(len(self.failures), 1)

    def test_auto_verifier_failure_preserves_manual_option_for_each_unit(self):
        f = self.f
        f.a3.auto_cropper = FakeAutoCropper(second_status="auto_ready")
        f.run_command("handle_image", {}, image=f.image)
        with patch.object(f.a3.crop_verifier, "verify", side_effect=lambda *a: self.fail("qwen_a3_crop_compare")):
            f.run_command("prepare_units", {"unit_ids": ["g1-u1", "g1-u2"], **f.target()})
        state = f.a3.store.load("s")
        self.assertTrue(all(state.auto_crops[u]["validation_status"] == "manual_required" for u in ["g1-u1", "g1-u2"]))
        f.run_command("select_unit", {"unit_id": "g1-u1", **f.target()})
        self.assertEqual(f.a3.store.load("s").phase, "CROP_REQUIRED")
        self.assert_preserved("crop_manual")

    def test_auto_load_screen_failure_does_not_hide_manual_crop(self):
        f = self.f
        f.a3.auto_cropper = FakeAutoCropper(second_status="auto_ready")
        f.run_command("handle_image", {}, image=f.image)
        f.a3.external_load_screen = lambda path: self.fail("external_load_screen")
        f.a3.external_load_screen.execution_version = "test-load-outage-v1"
        f.run_command("prepare_units", {"unit_ids": ["g1-u1", "g1-u2"], **f.target()})
        self.assertTrue(all(r["validation_status"] == "manual_required" for r in f.a3.store.load("s").auto_crops.values()))
        self.assert_preserved("crop_manual")

    def manual_scenario(self, stage):
        f = self.f
        f.run_command("handle_image", {}, image=f.image)
        f.run_command("select_unit", {"unit_id": "g1-u1", **f.target()})
        if stage == "load":
            f.a3.external_load_screen = lambda path: self.fail("external_load_screen")
        else:
            f.a3.crop_verifier.verify = lambda *args: self.fail("qwen_a3_crop_compare")
        f.run_command("handle_crop", {"bounds": {"x": 0.1, "y": 0.1, "width": 0.7, "height": 0.7},
                                     "unit_id": "g1-u1", **f.target()})
        state = f.a3.store.load("s")
        self.assertEqual(state.phase, "CROP_REQUIRED")
        self.assertTrue(Path(state.crop_drafts["g1-u1"]["path"]).is_file())
        self.assert_preserved("crop_draft")

    def test_manual_crop_verifier_outage_keeps_draft_and_reply(self):
        self.manual_scenario("verify")

    def test_manual_crop_load_outage_keeps_draft_and_reply(self):
        self.manual_scenario("load")

    def test_page_error_keeps_image_and_allows_explicit_new_retry(self):
        f = self.f
        f.a3.page_observer.observe = lambda *args: self.fail("qwen_a3_page_understanding")
        f.run_command("handle_image", {}, image=f.image)
        self.assertEqual(f.a3.store.load("s").phase, "ERROR")
        self.assertTrue(Path(f.a3.store.load("s").source_page_path).is_file())
        f.run_command("handle_text", {"text": "重试"})
        self.assertEqual(f.a3.store.load("s").phase, "ERROR")
        self.assert_preserved("page_retry")
        self.assertEqual(len(self.failures), 2)  # two user actions, no automatic retry

    def test_local_crop_write_failure_can_fall_back_without_publishing_partial_files(self):
        f = self.f
        f.a3.auto_cropper = FakeAutoCropper(second_status="auto_ready")
        with patch("PIL.Image.Image.save", side_effect=OSError("controlled output failure")):
            f.run_command("handle_image", {}, image=f.image)
        with f.store.transaction() as c:
            aborted = c.execute("SELECT path,temporary_path FROM execution_files WHERE status='ABORTED'").fetchall()
        self.assertTrue(aborted)
        self.assertTrue(all(not Path(p).exists() for row in aborted for p in row))
        f.run_command("select_unit", {"unit_id": "g1-u1", **f.target()})
        self.assertEqual(f.a3.store.load("s").phase, "CROP_REQUIRED")


if __name__ == "__main__":
    unittest.main()
