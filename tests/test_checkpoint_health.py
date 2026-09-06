import time
from pathlib import Path
import unittest

from fastapi.testclient import TestClient

from tiku_agent.fastapi_demo import create_app


class _HealthRuntime:
    def purge_expired(self) -> None:
        pass


class _DegradedTraceRecorder:
    def record(self, *args, **kwargs):
        return None

    def health(self):
        return {"status": "degraded", "current_reasons": ["capacity_exhausted"]}

    def close(self) -> None:
        pass


class CheckpointHealthTest(unittest.TestCase):
    def test_phase_4_2_does_not_install_a2_or_a3_checkpoint_emitters(self):
        root = Path(__file__).resolve().parents[1]
        for relative in (
            "tiku_agent/agent.py",
            "tiku_agent/a3_runtime.py",
            "tiku_agent/session_runtime.py",
            "scripts/run_tiku_agent_8896.py",
        ):
            source = (root / relative).read_text(encoding="utf-8")
            for emitter in ("CheckpointRecorder", "put_checkpoint", "put_artifact"):
                self.assertNotIn(emitter, source, f"{relative} enabled {emitter}")

    def test_health_is_disabled_until_the_control_plane_is_explicitly_supplied(self):
        response = TestClient(create_app(runtime=_HealthRuntime())).get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")
        self.assertEqual(response.json()["checkpoint_evidence"]["status"], "disabled")

    def test_evidence_degradation_is_public_but_strictly_sanitized(self):
        def health():
            return {
                "status": "degraded",
                "current_reasons": ["artifact_rows", "C:\\private\\secret"],
                "counters": {
                    "capacity_rejections": 3,
                    "password=secret": 9,
                },
                "pending": 2,
                "queue_capacity": 8,
                "accepting": True,
                "last_failure_code": "artifact_rows",
                "last_failure_at": "C:\\private\\evidence.sqlite3",
                "database_path": "C:\\private\\evidence.sqlite3",
            }

        response = TestClient(
            create_app(
                runtime=_HealthRuntime(),
                checkpoint_evidence_health_provider=health,
            )
        ).get("/health")

        payload = response.json()
        self.assertEqual(payload["status"], "degraded")
        self.assertEqual(
            payload["checkpoint_evidence"],
            {
                "status": "degraded",
                "current_reasons": ["artifact_rows"],
                "counters": {"capacity_rejections": 3},
                "pending": 2,
                "queue_capacity": 8,
                "accepting": True,
                "last_failure_code": "artifact_rows",
                "last_failure_at": "",
            },
        )
        self.assertNotIn("private", response.text)
        self.assertNotIn("secret", response.text)

    def test_health_provider_failure_has_only_a_stable_public_code(self):
        def broken_health():
            raise RuntimeError("token=secret C:\\private")

        response = TestClient(
            create_app(
                runtime=_HealthRuntime(),
                checkpoint_evidence_health_provider=broken_health,
            )
        ).get("/health")

        self.assertEqual(response.json()["status"], "degraded")
        evidence = response.json()["checkpoint_evidence"]
        self.assertEqual(evidence["current_reasons"], ["health_unavailable"])
        self.assertNotIn("secret", response.text)
        self.assertNotIn("private", response.text)

    def test_trace_capacity_degradation_also_degrades_top_level_health(self):
        response = TestClient(
            create_app(
                runtime=_HealthRuntime(),
                trace_event_recorder=_DegradedTraceRecorder(),
            )
        ).get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "degraded")
        self.assertEqual(
            response.json()["trace_events"]["current_reasons"],
            ["capacity_exhausted"],
        )

    def test_periodic_retention_runs_without_blocking_app_lifecycle(self):
        calls = []

        def run_once():
            calls.append(len(calls) + 1)
            if len(calls) == 1:
                raise RuntimeError("private maintenance detail")

        with TestClient(
            create_app(
                runtime=_HealthRuntime(),
                checkpoint_retention_runner=run_once,
                checkpoint_retention_interval_seconds=0.01,
            )
        ) as client:
            self.assertEqual(client.get("/health").status_code, 200)
            time.sleep(0.05)

        self.assertGreaterEqual(len(calls), 2)

    def test_retention_runner_requires_a_positive_explicit_interval(self):
        for interval in (0, -1, float("nan"), float("inf")):
            with self.subTest(interval=interval), self.assertRaisesRegex(
                ValueError, "retention interval"
            ):
                create_app(
                    runtime=_HealthRuntime(),
                    checkpoint_retention_runner=lambda: None,
                    checkpoint_retention_interval_seconds=interval,
                )


if __name__ == "__main__":
    unittest.main()
