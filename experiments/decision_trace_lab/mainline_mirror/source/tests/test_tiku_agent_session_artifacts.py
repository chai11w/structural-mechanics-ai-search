import unittest
from pathlib import Path
from uuid import uuid4

from tiku_agent.session_artifacts import SessionArtifacts


RUNTIME_DIR = Path(__file__).resolve().parents[1] / ".tmp_tiku_agent"
RUNTIME_DIR.mkdir(exist_ok=True)


class SessionArtifactsTest(unittest.TestCase):
    def test_persisted_upload_is_session_scoped_and_clearable(self):
        root = RUNTIME_DIR / f"artifact_test_{uuid4().hex}"
        source = RUNTIME_DIR / f"artifact_source_{uuid4().hex}.jpg"
        source.write_bytes(b"image content")
        self.addCleanup(lambda: source.unlink(missing_ok=True))
        artifacts = SessionArtifacts(root)
        self.addCleanup(lambda: artifacts.clear_session("session-1"))

        copied = artifacts.persist_image("session-1", source)

        self.assertTrue(copied.is_file())
        self.assertEqual(copied.read_bytes(), b"image content")
        self.assertEqual(copied.parents[1], artifacts.session_dir("session-1"))
        artifacts.clear_session("session-1")
        self.assertFalse(artifacts.session_dir("session-1").exists())


if __name__ == "__main__":
    unittest.main()
