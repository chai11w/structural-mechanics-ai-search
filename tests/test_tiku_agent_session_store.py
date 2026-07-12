import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from tiku_agent.session_store import (
    DEFAULT_SESSION_TTL,
    SESSION_STATE_SCHEMA_VERSION,
    SQLiteSessionStore,
    SessionStore,
)
from tiku_agent.state import AgentState


TEST_RUNTIME_DIR = Path(__file__).resolve().parents[1] / ".tmp_tiku_agent"
TEST_RUNTIME_DIR.mkdir(exist_ok=True)


class SessionStoreContractTest(unittest.TestCase):
    def database_path(self) -> Path:
        path = TEST_RUNTIME_DIR / f"session_store_test_{uuid4().hex}.db"
        self.addCleanup(lambda: path.unlink(missing_ok=True))
        return path

    def test_contract_is_abstract_until_a_storage_backend_is_chosen(self):
        with self.assertRaises(TypeError):
            SessionStore()

    def test_default_session_lifetime_is_two_hours(self):
        self.assertEqual(DEFAULT_SESSION_TTL, timedelta(hours=2))
        self.assertEqual(SESSION_STATE_SCHEMA_VERSION, 1)

    def test_sqlite_store_round_trips_complete_state(self):
        now = datetime(2026, 7, 12, 10, 0, tzinfo=UTC)
        store = SQLiteSessionStore(self.database_path(), now=lambda: now)
        original = AgentState(
            session_id="session-a",
            phase="WAIT_CANDIDATE_CHOICE",
            current_image_path="uploads/session-a/question.jpg",
            current_loads=[{"type": "集中", "raw": "P"}],
            current_chapter="4力法",
            questions=[{"label": "4", "loads": [{"type": "集中", "raw": "P"}]}],
            selected_question=1,
            candidates=[{"rank": 1, "path": "bank/q1.jpg", "score": 0.9}],
        )
        store.save(original)

        restored = store.load("session-a")

        self.assertIsNotNone(restored)
        self.assertEqual(restored.to_dict(), original.to_dict())

    def test_sqlite_store_expires_after_two_hours_and_returns_session_id_for_cleanup(self):
        clock = [datetime(2026, 7, 12, 10, 0, tzinfo=UTC)]
        store = SQLiteSessionStore(self.database_path(), now=lambda: clock[0])
        store.save(AgentState(session_id="expired-session"))
        clock[0] += timedelta(hours=2)

        self.assertEqual(store.purge_expired(), ["expired-session"])
        self.assertIsNone(store.load("expired-session"))

    def test_save_refreshes_sliding_expiry(self):
        clock = [datetime(2026, 7, 12, 10, 0, tzinfo=UTC)]
        state = AgentState(session_id="active-session")
        store = SQLiteSessionStore(self.database_path(), now=lambda: clock[0])
        store.save(state)
        clock[0] += timedelta(hours=1, minutes=59)
        store.save(state)
        clock[0] += timedelta(minutes=2)

        self.assertIsNotNone(store.load("active-session"))


if __name__ == "__main__":
    unittest.main()
