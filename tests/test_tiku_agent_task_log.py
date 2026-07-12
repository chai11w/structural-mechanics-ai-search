import unittest

from tiku_agent.task_log import TASK_LOG_SCHEMA_VERSION, TaskLogEntry, TaskLogger


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
        )

        data = entry.to_dict()

        self.assertEqual(data["schema_version"], TASK_LOG_SCHEMA_VERSION)
        self.assertNotIn("user_text", data)
        self.assertNotIn("image_path", data)
        self.assertNotIn("raw_content", data)


if __name__ == "__main__":
    unittest.main()
