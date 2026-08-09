"""Admin-only, read-only fee statistics for the 8788 Feishu bot.

Trigger: the whole text message is exactly a half-width or full-width
question mark ("?" / "？").  Normal sentences containing a question mark
never trigger this feature.

The 8790 model-cost ledger (``.tmp_tiku_agent_v2_prod_8790/model_costs.sqlite3``)
is opened with SQLite read-only URI semantics and a ``query_only`` pragma;
this module never writes to it.  The per-admin cursor and optional one-time
locally enrolled sender are persisted inside the 8788 bot's own state
directory, completely separate from the 8790 runtime state.

Cursor semantics:
- first successful query counts the trailing 24 hours;
- later queries count (last cutoff, this query start] strictly;
- the cutoff only advances after a query succeeds AND the cursor write
  succeeds; DB missing, schema mismatch, query failure and state-write
  failure all leave the cursor unchanged.

Replies are short Chinese summaries without session_key / search_key /
run_id / user text / prompt / image / path information.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import sqlite3
import threading
from typing import Any, Sequence

from tiku_shared.model_costs import utc_now


BASE = Path(__file__).resolve().parents[1]
DEFAULT_FEE_DB = BASE / ".tmp_tiku_agent_v2_prod_8790" / "model_costs.sqlite3"
DEFAULT_CURSOR_FILE_NAME = "admin_fee_cursor.json"
DEFAULT_ENROLLED_SENDER_FILE_NAME = "admin_fee_enrolled_sender.json"

ADMIN_FEE_TRIGGERS = ("?", "？")
FIRST_QUERY_WINDOW_HOURS = 24
OVER_THRESHOLD_MICROS = 50_000  # strictly more than 0.05 yuan

REQUIRED_RUN_COLUMNS = frozenset({
    "run_id",
    "session_key",
    "search_key",
    "task_kind",
    "started_at",
    "finished_at",
    "outcome",
    "call_count",
    "total_tokens",
    "estimated_cost_micros",
    "warning_codes_json",
    "schema_version",
})

REPLY_NO_PERMISSION = "无权限查看费用统计。"
REPLY_NO_RECORDS = "暂无费用记录。"
REPLY_FAILURE = "费用统计暂不可用，请稍后重试。"

_STATE_LOCK = threading.Lock()


class FeeQueryError(Exception):
    """Safe-to-reply failure while computing admin fee statistics."""

    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


@dataclass(frozen=True)
class CostSummary:
    search_count: int
    over_count: int
    max_cost_micros: int
    total_cost_micros: int


def is_admin_fee_query(text: str) -> bool:
    """True only when the whole stripped message is exactly ? or ？."""

    return str(text or "").strip() in ADMIN_FEE_TRIGGERS


def normalize_admin_sender_ids(raw: Any) -> tuple[str, ...]:
    """Normalize the configured admin whitelist; empty means feature is off."""

    if raw is None:
        return ()
    if isinstance(raw, str):
        raw = [raw]
    try:
        items = list(raw)
    except TypeError:
        return ()
    return tuple(str(item).strip() for item in items if str(item).strip())


def load_enrolled_sender(state_file: Path | str) -> str | None:
    """Read the single locally enrolled sender without exposing its value."""

    state_file = Path(state_file)
    if not state_file.exists():
        return None
    if not state_file.is_file():
        raise FeeQueryError("enrollment_read_failed")
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FeeQueryError("enrollment_read_failed") from exc
    sender = data.get("sender") if isinstance(data, dict) else None
    if not isinstance(sender, str) or not sender.strip():
        raise FeeQueryError("enrollment_read_failed")
    return sender.strip()


def enroll_sender_once(state_file: Path | str, sender: str) -> bool:
    """Atomically store the first explicitly enrolled sender; never replace it."""

    state_file = Path(state_file)
    sender = str(sender or "").strip()
    if not sender:
        raise FeeQueryError("enrollment_write_failed")
    with _STATE_LOCK:
        existing = load_enrolled_sender(state_file)
        if existing is not None:
            return False
        state_file.parent.mkdir(parents=True, exist_ok=True)
        temp = state_file.with_name(state_file.name + ".tmp")
        try:
            temp.write_text(
                json.dumps({"version": 1, "sender": sender, "enrolled_at": utc_now()}, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            os.replace(temp, state_file)
        except OSError as exc:
            raise FeeQueryError("enrollment_write_failed") from exc
    return True


def resolve_interval(last_cutoff: str | None, query_started_at: str) -> tuple[str, str]:
    """Return (exclusive start, inclusive end) ISO timestamps for this query."""

    if last_cutoff is None:
        start = (
            datetime.fromisoformat(query_started_at)
            - timedelta(hours=FIRST_QUERY_WINDOW_HOURS)
        ).isoformat()
    else:
        start = last_cutoff
    return start, query_started_at


def validate_schema(connection: sqlite3.Connection) -> bool:
    """True when model_cost_runs exists with every column this query needs."""

    table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'model_cost_runs'"
    ).fetchone()
    if table is None:
        return False
    columns = {
        str(row[1])
        for row in connection.execute("PRAGMA table_info(model_cost_runs)")
    }
    return REQUIRED_RUN_COLUMNS.issubset(columns)


def query_cost_summary(
    db_path: Path | str,
    since_exclusive: str,
    until_inclusive: str,
) -> CostSummary:
    """Return full costs for searches updated in (since, until].

    Read-only: the database is opened with ``mode=ro`` and ``query_only``.
    A search can span several model-cost rows. Its full cost is aggregated
    before applying the threshold; the interval is selected by the search's
    latest finished row so a search crossing a cutoff is not under-counted.
    """

    db_path = Path(db_path)
    if not db_path.is_file():
        raise FeeQueryError("db_missing")
    uri = f"file:{db_path.resolve().as_posix()}?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True, timeout=5) as connection:
            connection.execute("PRAGMA query_only=ON")
            if not validate_schema(connection):
                raise FeeQueryError("schema_mismatch")
            rows = connection.execute(
                """
                SELECT search_key,
                       SUM(estimated_cost_micros) AS cost_micros
                FROM model_cost_runs
                WHERE search_key != ''
                  AND call_count > 0
                GROUP BY search_key
                HAVING MAX(finished_at) > ?
                   AND MAX(finished_at) <= ?
                """,
                (since_exclusive, until_inclusive),
            ).fetchall()
    except FeeQueryError:
        raise
    except Exception as exc:
        raise FeeQueryError("query_failed") from exc

    costs = sorted(int(row[1] or 0) for row in rows)
    return CostSummary(
        search_count=len(costs),
        over_count=sum(1 for cost in costs if cost > OVER_THRESHOLD_MICROS),
        max_cost_micros=costs[-1] if costs else 0,
        total_cost_micros=sum(costs),
    )


def load_cursor(state_file: Path | str, sender: str) -> str | None:
    """Return the last successful cutoff for one admin, or None for first run."""

    state_file = Path(state_file)
    sender = str(sender or "").strip()
    if not sender or not state_file.exists():
        return None
    if not state_file.is_file():
        raise FeeQueryError("state_read_failed")
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FeeQueryError("state_read_failed") from exc
    senders = data.get("senders") if isinstance(data, dict) else None
    if not isinstance(senders, dict):
        raise FeeQueryError("state_read_failed")
    entry = senders.get(sender)
    cutoff = entry.get("cutoff") if isinstance(entry, dict) else None
    if not isinstance(cutoff, str) or not cutoff:
        return None
    try:
        datetime.fromisoformat(cutoff)
    except ValueError as exc:
        raise FeeQueryError("state_read_failed") from exc
    return cutoff


def save_cursor(state_file: Path | str, sender: str, cutoff: str) -> None:
    """Atomically persist one admin's cutoff; raises OSError on failure."""

    state_file = Path(state_file)
    sender = str(sender or "").strip()
    cutoff = str(cutoff or "")
    with _STATE_LOCK:
        state_file.parent.mkdir(parents=True, exist_ok=True)
        data: dict[str, Any] = {}
        if state_file.exists() and not state_file.is_file():
            raise OSError("cursor path is not a file")
        if state_file.is_file():
            try:
                loaded = json.loads(state_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise OSError("cursor state is unreadable") from exc
            if not isinstance(loaded, dict) or not isinstance(loaded.get("senders", {}), dict):
                raise OSError("cursor state has an invalid schema")
            data = loaded
        senders = data.setdefault("senders", {})
        if not isinstance(senders, dict):
            data["senders"] = {}
            senders = data["senders"]
        data["version"] = 1
        senders[sender] = {"cutoff": cutoff, "updated_at": utc_now()}
        temp = state_file.with_name(state_file.name + ".tmp")
        temp.write_text(
            json.dumps(data, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(temp, state_file)


def format_timestamp(iso: str) -> str:
    """Short local-time label such as 08-02 09:30 for a UTC ISO timestamp."""

    try:
        parsed = datetime.fromisoformat(str(iso))
    except ValueError:
        return str(iso)
    local = parsed.astimezone() if parsed.tzinfo is not None else parsed
    return local.strftime("%m-%d %H:%M")


def _format_cny(micros: int) -> str:
    value = round(int(micros or 0) / 1_000_000, 4)
    return format(value, ".4f").rstrip("0").rstrip(".") or "0"


def format_cost_summary(
    summary: CostSummary,
    interval_start: str,
    interval_end: str,
    cutoff: str,
) -> str:
    lines = [
        "费用检查",
        f"区间：{format_timestamp(interval_start)}—{format_timestamp(interval_end)}",
        f"新增搜题：{summary.search_count} 次",
    ]
    if summary.over_count:
        lines.append(f"超过0.05元：{summary.over_count} 次")
    else:
        lines.append("没有超过0.05元")
    lines.extend([
        f"最贵一次：{_format_cny(summary.max_cost_micros)} 元",
        f"本区间总费用：{_format_cny(summary.total_cost_micros)} 元",
        f"已记录本次截止时间：{format_timestamp(cutoff)}",
    ])
    return "\n".join(lines)


class AdminFeeQueryService:
    """One-shot admin fee statistics with a persisted per-admin cursor."""

    def __init__(
        self,
        *,
        fee_db: Path | str,
        state_path: Path | str,
        admin_sender_ids: Sequence[str] = (),
    ) -> None:
        self.fee_db = Path(fee_db)
        self.state_path = Path(state_path)
        self.admin_sender_ids = frozenset(normalize_admin_sender_ids(admin_sender_ids))
        self._query_lock = threading.Lock()

    def query_reply(self, sender: str) -> str:
        """Return a short, safe Chinese reply; never raises into the caller."""

        sender = str(sender or "").strip()
        if not sender or sender not in self.admin_sender_ids:
            return REPLY_NO_PERMISSION
        try:
            with self._query_lock:
                return self._run(sender)
        except FeeQueryError as exc:
            if exc.kind == "db_missing":
                return REPLY_NO_RECORDS
            return REPLY_FAILURE
        except Exception:
            return REPLY_FAILURE

    def _run(self, sender: str) -> str:
        query_started_at = utc_now()
        last_cutoff = load_cursor(self.state_path, sender)
        interval_start, interval_end = resolve_interval(last_cutoff, query_started_at)
        if not self.fee_db.is_file():
            raise FeeQueryError("db_missing")
        summary = query_cost_summary(self.fee_db, interval_start, interval_end)
        try:
            save_cursor(self.state_path, sender, query_started_at)
        except OSError as exc:
            raise FeeQueryError("state_write_failed") from exc
        return format_cost_summary(summary, interval_start, interval_end, query_started_at)
