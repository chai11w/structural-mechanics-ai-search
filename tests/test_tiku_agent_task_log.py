import unittest
import json
from pathlib import Path
from uuid import uuid4

from tiku_agent.task_log import JsonlTaskLogger, TASK_LOG_SCHEMA_VERSION, TaskLogEntry, TaskLogger


RUNTIME_DIR = Path(__file__).resolve().parents[1] / ".tmp_tiku_agent"
RUNTIME_DIR.mkdir(exist_ok=True)


class TaskLogContractTest(unittest.TestCase):
    def test_contract_is_abstract(self):
        with self.assertRaises(TypeError):
            TaskLogger()

    def test_entry_contains_diagnostics_without_raw_user_content_or_paths(self):
        entry = TaskLogEntry(
            task_id="task-1",
            session_key="hashed-session",
            kind="image",
            started_at="2026-07-12T10:00:00+00:00",
            finished_at="2026-07-12T10:00:08+00:00",
            duration_ms=8000,
            phase_before="IDLE",
            phase_after="WAIT_CANDIDATE_CHOICE",
            outcome="candidates",
            question_count=0,
            candidate_count=3,
            chapter="7矩阵位移",
            route="main",
            trace_id="trace_0123456789abcdef0123456789abcdef",
        )

        data = entry.to_dict()

        self.assertEqual(data["schema_version"], TASK_LOG_SCHEMA_VERSION)
        self.assertEqual(
            data["trace_id"],
            "trace_0123456789abcdef0123456789abcdef",
        )
        self.assertNotIn("user_text", data)
        self.assertNotIn("image_path", data)
        self.assertNotIn("raw_content", data)

    def test_jsonl_logger_appends_v3_without_rewriting_v2_history(self):
        path = RUNTIME_DIR / f"task_log_test_{uuid4().hex}.jsonl"
        self.addCleanup(lambda: path.unlink(missing_ok=True))
        historical = '{"schema_version":2,"task_id":"historical"}'
        path.write_text(historical + "\n", encoding="utf-8")
        entry = TaskLogEntry(
            task_id="task-1",
            session_key="hashed-session",
            kind="text",
            started_at="2026-07-12T10:00:00+00:00",
            finished_at="2026-07-12T10:00:01+00:00",
            duration_ms=1000,
            phase_before="WAIT_CANDIDATE_CHOICE",
            phase_after="ANSWERED",
            outcome="answered",
            question_count=0,
            candidate_count=1,
        )

        JsonlTaskLogger(path).write(entry)

        lines = path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0], historical)
        self.assertEqual(json.loads(lines[1]), entry.to_dict())


if __name__ == "__main__":
    unittest.main()
