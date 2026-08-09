from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import shutil
import sqlite3
import time
import unittest
from uuid import uuid4

from scripts.admin_fee_query import (
    AdminFeeQueryService,
    REPLY_FAILURE,
    REPLY_NO_PERMISSION,
    REPLY_NO_RECORDS,
    enroll_sender_once,
    is_admin_fee_query,
    load_cursor,
    load_enrolled_sender,
    normalize_admin_sender_ids,
    query_cost_summary,
    resolve_interval,
    save_cursor,
    _format_cny,
    _format_cny,
)
from scripts.feishu_tiku_bot import (
    FeishuClient,
    FeishuTikuBridge,
    FeishuTikuOptions,
    TikuBot,
    TikuSession,
    load_options,
)
from tiku_shared.model_costs import ModelCostCollector, SQLiteModelCostLedger


def make_directory(parent: Path, name: str) -> Path:
    directory = parent / name / uuid4().hex
    directory.mkdir(parents=True)
    return directory


def write_run(
    database: Path,
    *,
    run_id: str,
    search_key: str,
    started_at: str,
    cost_micros: int,
    call_count: int = 1,
    session_key: str = "hashed-session",
) -> None:
    collector = ModelCostCollector(
        run_id=run_id,
        session_key=session_key,
        search_key=search_key,
    )
    collector.started_at = started_at
    for _ in range(max(0, call_count)):
        collector.record(
            provider="zhipu",
            model="glm-4.6v",
            call_type="zhipu_shape_rerank",
            status="success",
            started_at=started_at,
            finished_at=started_at,
            latency_ms=1,
            usage={"input_tokens": 1, "output_tokens": 0},
        )
    SQLiteModelCostLedger(database).write_run(
        collector,
        finished_at=started_at,
        outcome="candidates",
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE model_cost_runs SET estimated_cost_micros = ? WHERE run_id = ?",
            (cost_micros, run_id),
        )


class AdminFeeQueryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).resolve().parents[1] / ".tmp_tests"
        self.directory = make_directory(self.root, "admin_fee")
        self.addCleanup(lambda: shutil.rmtree(self.directory, ignore_errors=True))

    def iso(self, delta_minutes: float) -> str:
        return (datetime.now(UTC) + timedelta(minutes=delta_minutes)).isoformat()

    def test_half_width_and_full_width_exact_trigger(self):
        for text in ("?", "？", " ? ", "\t？\n", "  ?  "):
            self.assertTrue(is_admin_fee_query(text), text)

    def test_normal_sentences_do_not_trigger(self):
        for text in (
            "这道题怎么做？",
            "选1吗?",
            "？？",
            "??",
            "a?",
            "?a",
            "是这一题吗？",
            "help?",
            "？ ？",
            "a ?",
            "",
        ):
            self.assertFalse(is_admin_fee_query(text), text)

    def test_unconfigured_whitelist_denies_everyone(self):
        service = AdminFeeQueryService(
            fee_db=self.directory / "costs.sqlite3",
            state_path=self.directory / "cursor.json",
            admin_sender_ids=(),
        )
        self.assertEqual(service.query_reply("any-user"), REPLY_NO_PERMISSION)
        self.assertNotIn("费用更新", service.query_reply("any-user"))
        self.assertNotIn("已记录截止", service.query_reply("any-user"))

    def test_non_admin_is_denied_without_fee_data(self):
        database = self.directory / "costs.sqlite3"
        write_run(database, run_id="r1", search_key="k1", started_at=self.iso(-5), cost_micros=9000)
        service = AdminFeeQueryService(
            fee_db=database,
            state_path=self.directory / "cursor.json",
            admin_sender_ids=("admin-1",),
        )
        reply = service.query_reply("someone-else")
        self.assertEqual(reply, REPLY_NO_PERMISSION)
        self.assertNotIn("费用更新", reply)
        self.assertNotIn("已记录截止", reply)

    def test_normalize_admin_sender_ids(self):
        self.assertEqual(normalize_admin_sender_ids(None), ())
        self.assertEqual(normalize_admin_sender_ids([]), ())
        self.assertEqual(normalize_admin_sender_ids([" a ", "b", "", "  "]), ("a", "b"))
        self.assertEqual(normalize_admin_sender_ids("admin"), ("admin",))
        self.assertEqual(normalize_admin_sender_ids(123), ())

    def test_cny_display_rounds_to_four_decimal_places(self):
        self.assertEqual(_format_cny(9601), "0.0096")
        self.assertEqual(_format_cny(999999), "1")
        self.assertEqual(_format_cny(10000), "0.01")

    def test_cny_display_rounds_to_four_decimal_places(self):
        self.assertEqual(_format_cny(9601), "0.0096")
        self.assertEqual(_format_cny(999999), "1")
        self.assertEqual(_format_cny(10000), "0.01")

    def test_local_enrollment_keeps_first_sender_and_never_overwrites(self):
        state = self.directory / "enrolled_sender.json"
        self.assertTrue(enroll_sender_once(state, "admin-1"))
        self.assertFalse(enroll_sender_once(state, "admin-2"))
        self.assertEqual(load_enrolled_sender(state), "admin-1")

    def test_malformed_local_enrollment_fails_closed(self):
        state = self.directory / "enrolled_sender.json"
        state.write_text("not-json", encoding="utf-8")
        with self.assertRaises(Exception):
            load_enrolled_sender(state)

    def test_first_query_counts_last_24_hours_and_persists_cursor(self):
        database = self.directory / "costs.sqlite3"
        write_run(database, run_id="r1", search_key="key-a", started_at=self.iso(-120), cost_micros=1000)
        write_run(database, run_id="r2", search_key="key-b", started_at=self.iso(-26 * 60), cost_micros=50000)
        state = self.directory / "cursor.json"
        service = AdminFeeQueryService(
            fee_db=database,
            state_path=state,
            admin_sender_ids=("admin-1",),
        )
        reply = service.query_reply("admin-1")
        self.assertIn("新增搜题：1 次", reply)
        self.assertIn("本区间总费用：0.001 元", reply)
        cutoff = load_cursor(state, "admin-1")
        self.assertIsNotNone(cutoff)
        parsed = datetime.fromisoformat(cutoff)
        self.assertLessEqual(parsed, datetime.now(UTC))
        self.assertGreaterEqual(parsed, datetime.now(UTC) - timedelta(minutes=1))

    def test_incremental_cursor_excludes_previous_interval(self):
        database = self.directory / "costs.sqlite3"
        state = self.directory / "cursor.json"
        previous_cutoff = self.iso(-90)
        save_cursor(state, "admin-1", previous_cutoff)
        write_run(database, run_id="r1", search_key="old-key", started_at=self.iso(-120), cost_micros=999999)
        write_run(database, run_id="r2", search_key="new-key", started_at=self.iso(-30), cost_micros=7000)
        service = AdminFeeQueryService(
            fee_db=database,
            state_path=state,
            admin_sender_ids=("admin-1",),
        )
        reply = service.query_reply("admin-1")
        self.assertIn("新增搜题：1 次", reply)
        self.assertIn("本区间总费用：0.007 元", reply)
        self.assertIn("最贵一次：0.007 元", reply)
        self.assertNotIn("0.999", reply)

    def test_aggregation_by_search_key_and_filters(self):
        database = self.directory / "costs.sqlite3"
        write_run(database, run_id="r1", search_key="key-a", started_at=self.iso(-120), cost_micros=10000)
        write_run(database, run_id="r2", search_key="key-a", started_at=self.iso(-60), cost_micros=20000)
        write_run(database, run_id="r3", search_key="", started_at=self.iso(-30), cost_micros=999999)
        write_run(database, run_id="r4", search_key="key-c", started_at=self.iso(-20), cost_micros=10000, call_count=0)
        summary = query_cost_summary(database, self.iso(-24 * 60), self.iso(1))
        self.assertEqual(summary.search_count, 1)
        self.assertEqual(summary.total_cost_micros, 30000)
        self.assertEqual(summary.max_cost_micros, 30000)

    def test_search_crossing_cutoff_uses_full_cost(self):
        database = self.directory / "costs.sqlite3"
        cutoff = self.iso(-90)
        write_run(database, run_id="r1", search_key="key-a", started_at=self.iso(-120), cost_micros=40000)
        write_run(database, run_id="r2", search_key="key-a", started_at=self.iso(-30), cost_micros=20000)
        summary = query_cost_summary(database, cutoff, self.iso(1))
        self.assertEqual(summary.search_count, 1)
        self.assertEqual(summary.total_cost_micros, 60000)
        self.assertEqual(summary.over_count, 1)

    def test_zero_point_zero_five_boundary_is_strict(self):
        database = self.directory / "costs.sqlite3"
        write_run(database, run_id="r1", search_key="k-a", started_at=self.iso(-30), cost_micros=50000)
        write_run(database, run_id="r2", search_key="k-b", started_at=self.iso(-29), cost_micros=50001)
        write_run(database, run_id="r3", search_key="k-c", started_at=self.iso(-28), cost_micros=49999)
        summary = query_cost_summary(database, self.iso(-24 * 60), self.iso(1))
        self.assertEqual(summary.search_count, 3)
        self.assertEqual(summary.over_count, 1)
        self.assertEqual(summary.max_cost_micros, 50001)
        self.assertEqual(summary.total_cost_micros, 150000)

    def test_db_missing_replies_no_records_without_advancing_cursor(self):
        state = self.directory / "cursor.json"
        save_cursor(state, "admin-1", self.iso(-60))
        before = state.read_text(encoding="utf-8")
        service = AdminFeeQueryService(
            fee_db=self.directory / "missing.sqlite3",
            state_path=state,
            admin_sender_ids=("admin-1",),
        )
        self.assertEqual(service.query_reply("admin-1"), REPLY_NO_RECORDS)
        self.assertEqual(state.read_text(encoding="utf-8"), before)

    def test_schema_mismatch_does_not_advance_cursor(self):
        database = self.directory / "costs.sqlite3"
        with sqlite3.connect(database) as connection:
            connection.execute("CREATE TABLE model_cost_runs (run_id TEXT PRIMARY KEY, search_key TEXT NOT NULL)")
        state = self.directory / "cursor.json"
        save_cursor(state, "admin-1", self.iso(-60))
        before = state.read_text(encoding="utf-8")
        service = AdminFeeQueryService(
            fee_db=database,
            state_path=state,
            admin_sender_ids=("admin-1",),
        )
        self.assertEqual(service.query_reply("admin-1"), REPLY_FAILURE)
        self.assertEqual(state.read_text(encoding="utf-8"), before)

    def test_query_failure_does_not_advance_cursor(self):
        database = self.directory / "costs.sqlite3"
        database.write_bytes(b"not a sqlite database")
        state = self.directory / "cursor.json"
        save_cursor(state, "admin-1", self.iso(-60))
        before = state.read_text(encoding="utf-8")
        service = AdminFeeQueryService(
            fee_db=database,
            state_path=state,
            admin_sender_ids=("admin-1",),
        )
        self.assertEqual(service.query_reply("admin-1"), REPLY_FAILURE)
        self.assertEqual(state.read_text(encoding="utf-8"), before)

    def test_state_write_failure_does_not_advance_cursor(self):
        database = self.directory / "costs.sqlite3"
        write_run(database, run_id="r1", search_key="k1", started_at=self.iso(-5), cost_micros=9000)
        blocked = self.directory / "cursor.json"
        blocked.mkdir()
        service = AdminFeeQueryService(
            fee_db=database,
            state_path=blocked,
            admin_sender_ids=("admin-1",),
        )
        self.assertEqual(service.query_reply("admin-1"), REPLY_FAILURE)
        self.assertTrue(blocked.is_dir())
        self.assertFalse((self.directory / "cursor.json" / "cursor.json").exists())

    def test_corrupt_cursor_fails_closed_without_overwrite(self):
        database = self.directory / "costs.sqlite3"
        write_run(database, run_id="r1", search_key="k1", started_at=self.iso(-5), cost_micros=9000)
        state = self.directory / "cursor.json"
        state.write_text("not-json", encoding="utf-8")
        before = state.read_bytes()
        service = AdminFeeQueryService(
            fee_db=database,
            state_path=state,
            admin_sender_ids=("admin-1",),
        )
        self.assertEqual(service.query_reply("admin-1"), REPLY_FAILURE)
        self.assertEqual(state.read_bytes(), before)

    def test_no_new_searches_still_advances_and_reports(self):
        database = self.directory / "costs.sqlite3"
        write_run(database, run_id="r1", search_key="old", started_at=self.iso(-25 * 60), cost_micros=9000)
        state = self.directory / "cursor.json"
        service = AdminFeeQueryService(
            fee_db=database,
            state_path=state,
            admin_sender_ids=("admin-1",),
        )
        reply = service.query_reply("admin-1")
        self.assertIn("新增搜题：0 次", reply)
        self.assertIn("已记录本次截止时间", reply)
        self.assertIsNotNone(load_cursor(state, "admin-1"))

    def test_reply_has_no_sensitive_fields(self):
        database = self.directory / "costs.sqlite3"
        write_run(
            database,
            run_id="run-secret-1",
            search_key="key-secret-1",
            session_key="session-secret-1",
            started_at=self.iso(-5),
            cost_micros=123456,
        )
        state = self.directory / "cursor.json"
        service = AdminFeeQueryService(
            fee_db=database,
            state_path=state,
            admin_sender_ids=("admin-1",),
        )
        for reply in (
            service.query_reply("admin-1"),
            service.query_reply("other"),
            AdminFeeQueryService(
                fee_db=self.directory / "missing.sqlite3",
                state_path=state,
                admin_sender_ids=("admin-1",),
            ).query_reply("admin-1"),
        ):
            for sensitive in (
                "run-secret-1",
                "key-secret-1",
                "session-secret-1",
                "admin-1",
                ".sqlite3",
                "model_cost",
                "tmp_tiku_agent_v2_prod_8790",
                str(database),
                "Prompt",
                "prompt",
            ):
                self.assertNotIn(sensitive, reply)

    def test_resolve_interval_semantics(self):
        now = datetime.now(UTC)
        start, end = resolve_interval(None, now.isoformat())
        self.assertEqual(start, (now - timedelta(hours=24)).isoformat())
        self.assertEqual(end, now.isoformat())
        cutoff = (now - timedelta(minutes=5)).isoformat()
        start, end = resolve_interval(cutoff, now.isoformat())
        self.assertEqual(start, cutoff)
        self.assertEqual(end, now.isoformat())


class AdminFeeBotIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).resolve().parents[1] / ".tmp_tests"
        self.directory = make_directory(self.root, "admin_fee_bot")
        self.addCleanup(lambda: shutil.rmtree(self.directory, ignore_errors=True))

    def iso(self, delta_minutes: float) -> str:
        return (datetime.now(UTC) + timedelta(minutes=delta_minutes)).isoformat()

    def make_bot(self, *, admins: tuple[str, ...], database: Path | None = None) -> TikuBot:
        options = FeishuTikuOptions(
            dry_run=True,
            temp_dir=self.directory / "state",
            admin_sender_ids=admins,
            admin_fee_db=database or (self.directory / "costs.sqlite3"),
        )
        return TikuBot(options=options)

    def test_bot_trigger_priority_over_session_state(self):
        database = self.directory / "costs.sqlite3"
        write_run(database, run_id="r1", search_key="k1", started_at=self.iso(-5), cost_micros=9000)
        bot = self.make_bot(admins=("admin-1",), database=database)
        self.assertIn("费用检查", "\n".join(bot.receive_text("admin-1", "?").texts))
        self.assertIn("费用检查", "\n".join(bot.receive_text("admin-1", "？").texts))

        bot.sessions.save(
            "admin-1",
            TikuSession(state="waiting_choice", results=[{"rank": 1, "path": "x.jpg", "score": 1.0}]),
        )
        self.assertIn("费用检查", "\n".join(bot.receive_text("admin-1", "?").texts))
        self.assertNotIn("费用统计", "\n".join(bot.receive_text("admin-1", "这道题怎么做？").texts))

    def test_bot_whitelist_denies_non_admin_and_empty_config(self):
        database = self.directory / "costs.sqlite3"
        write_run(database, run_id="r1", search_key="k1", started_at=self.iso(-5), cost_micros=9000)
        bot = self.make_bot(admins=("admin-1",), database=database)
        self.assertEqual("\n".join(bot.receive_text("someone-else", "?").texts), REPLY_NO_PERMISSION)
        bot_no_admin = self.make_bot(admins=(), database=database)
        self.assertEqual("\n".join(bot_no_admin.receive_text("anyone", "?").texts), REPLY_NO_PERMISSION)

    def test_bot_failure_does_not_affect_other_flows(self):
        database = self.directory / "costs.sqlite3"
        database.write_bytes(b"not a sqlite database")
        bot = self.make_bot(admins=("admin-1",), database=database)
        self.assertEqual("\n".join(bot.receive_text("admin-1", "?").texts), REPLY_FAILURE)
        self.assertIn("已取消", "\n".join(bot.receive_text("admin-1", "0").texts))

    def test_load_options_reads_admin_whitelist(self):
        import search as search_module
        from scripts.feishu_tiku_bot import build_parser

        previous = search_module.cfg.get("feishu_admin_sender_ids", None)
        had_key = "feishu_admin_sender_ids" in search_module.cfg
        search_module.cfg["feishu_admin_sender_ids"] = [" admin-a ", " ", "admin-b"]
        try:
            options = load_options(build_parser().parse_args([]))
        finally:
            if had_key:
                search_module.cfg["feishu_admin_sender_ids"] = previous
            else:
                del search_module.cfg["feishu_admin_sender_ids"]
        self.assertEqual(options.admin_sender_ids, ("admin-a", "admin-b"))

    def test_feishu_dimension_filter_defaults_on_with_explicit_rollback(self):
        import search as search_module
        from scripts.feishu_tiku_bot import build_parser

        previous = search_module.cfg.get("dimension_filter_enabled", None)
        had_key = "dimension_filter_enabled" in search_module.cfg
        search_module.cfg.pop("dimension_filter_enabled", None)
        try:
            enabled = load_options(build_parser().parse_args([]))
            disabled = load_options(
                build_parser().parse_args(["--disable-dimension-filter"])
            )
        finally:
            if had_key:
                search_module.cfg["dimension_filter_enabled"] = previous
        self.assertTrue(enabled.dimension_filter_enabled)
        self.assertFalse(disabled.dimension_filter_enabled)

    def test_config_example_placeholder(self):
        example = json.loads(
            (Path(__file__).resolve().parents[1] / "config.example.json").read_text(encoding="utf-8")
        )
        self.assertIn("feishu_admin_sender_ids", example)
        self.assertEqual(example["feishu_admin_sender_ids"], [])
        self.assertIs(example["dimension_filter_enabled"], True)


if __name__ == "__main__":
    unittest.main()
