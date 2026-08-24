"""SQLite-backed store for scheduled tasks (native Cron-like scheduling inside liteagent).

A schedule is either:
- repeating: `date` is None, fires at `time` (HH:MM, local server time) every day.
- one-off: `date` is a specific "YYYY-MM-DD"; fires once at that date+time, then deactivates.

Kept intentionally simple (a poll loop checks `next_run_at`, see scheduler_runner.py) rather
than a real cron-expression engine, since the only two shapes anyone actually asked for are
"every day at HH:MM" and "once, at this specific date+time".
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
import re
import sqlite3
import uuid

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _parse_time(time_str: str) -> tuple[int, int]:
    match = _TIME_RE.match((time_str or "").strip())
    if not match:
        raise ValueError(f"time 格式錯誤，需為 24 小時制 HH:MM，收到：{time_str!r}")
    return int(match.group(1)), int(match.group(2))


def _parse_date(date_str: str) -> tuple[int, int, int]:
    if not _DATE_RE.match((date_str or "").strip()):
        raise ValueError(f"date 格式錯誤，需為 YYYY-MM-DD，收到：{date_str!r}")
    try:
        d = datetime.strptime(date_str.strip(), "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError(f"date 不是合法日期：{date_str!r}") from exc
    return d.year, d.month, d.day


class ScheduleStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS schedules (
                    id TEXT PRIMARY KEY,
                    conversation_id TEXT,
                    message TEXT NOT NULL,
                    time TEXT NOT NULL,
                    date TEXT,
                    notify_email TEXT,
                    active INTEGER NOT NULL DEFAULT 1,
                    next_run_at TEXT NOT NULL,
                    last_run_at TEXT,
                    last_result TEXT,
                    created_at TEXT NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_schedules_active_next ON schedules(active, next_run_at)")
            existing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(schedules)").fetchall()}
            if "owner_id" not in existing_cols:
                conn.execute("ALTER TABLE schedules ADD COLUMN owner_id TEXT")
            if "assisted_by_email" not in existing_cols:
                conn.execute("ALTER TABLE schedules ADD COLUMN assisted_by_email TEXT")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_schedules_owner ON schedules(owner_id)")

    @staticmethod
    def _next_daily_run(time_str: str, after: datetime) -> datetime:
        hour, minute = _parse_time(time_str)
        candidate = after.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= after:
            candidate += timedelta(days=1)
        return candidate

    @staticmethod
    def _one_off_run(time_str: str, date_str: str) -> datetime:
        hour, minute = _parse_time(time_str)
        year, month, day = _parse_date(date_str)
        return datetime(year, month, day, hour, minute)

    def create(
        self,
        message: str,
        time: str,
        date: str | None = None,
        notify_email: str | None = None,
        conversation_id: str | None = None,
        owner_id: str | None = None,
        assisted_by_email: str | None = None,
    ) -> dict[str, Any]:
        """assisted_by_email: set when an admin created this schedule while impersonating
        another user -- surfaced back to that user so they can see it wasn't self-created."""
        message = (message or "").strip()
        if not message:
            raise ValueError("message 不可為空白")
        date = (date or "").strip() or None
        notify_email = (notify_email or "").strip() or None
        now = datetime.now()
        if date:
            next_run = self._one_off_run(time, date)
            if next_run <= now:
                raise ValueError("指定的日期時間已經過去，請確認 date／time 是否正確（必須是今天或未來）。")
        else:
            _parse_time(time)  # validate format even though _next_daily_run also parses it
            next_run = self._next_daily_run(time, now)
        schedule_id = str(uuid.uuid4())
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO schedules
                   (id, conversation_id, message, time, date, notify_email, active, next_run_at, created_at, owner_id, assisted_by_email)
                   VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)""",
                (schedule_id, conversation_id, message, time.strip(), date, notify_email, next_run.isoformat(), now.isoformat(), owner_id, assisted_by_email),
            )
        return self.get(schedule_id)  # type: ignore[return-value]

    def get(self, schedule_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM schedules WHERE id=?", (schedule_id,)).fetchone()
        return dict(row) if row else None

    def list_all(self, conversation_id: str | None = None, owner_id: str | None = None) -> list[dict[str, Any]]:
        """owner_id=None means "no user filtering" (multi-user auth disabled, or an admin
        wanting to see everything)."""
        query = "SELECT * FROM schedules"
        clauses: list[str] = []
        params: list[Any] = []
        if conversation_id:
            clauses.append("conversation_id=?")
            params.append(conversation_id)
        if owner_id is not None:
            clauses.append("owner_id=?")
            params.append(owner_id)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY active DESC, next_run_at"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def claim_due(self, now: datetime | None = None) -> list[dict[str, Any]]:
        """Atomically claim every currently-due schedule and return only the ones *this call*
        won the race for.

        Why this matters: more than one app process can be polling the same schedules.sqlite
        at once (e.g. a deployment that runs a separate full process per port — :80 and
        :443 — both importing the same Agent/ScheduleStore). A naive "SELECT due rows, then
        separately mark them" has a race window where two processes both see a row as due and
        both dispatch it. Instead, each candidate row's UPDATE uses its *current* next_run_at
        as an optimistic-lock token in the WHERE clause: the update also changes next_run_at
        (or deactivates), so whichever process's UPDATE lands first invalidates the token for
        everyone else's — their UPDATE affects 0 rows and they simply skip it. A plain SQLite
        UPDATE statement is atomic per-call even across separate OS processes, so no explicit
        cross-process locking is needed.

        Also folds in what `reschedule_or_deactivate` used to do separately: the row is
        rescheduled/deactivated *before* dispatch (not after), so a slow-running task — or the
        process dying mid-run — can never cause the same trigger to fire twice either. Daily
        schedules get `next_run_at` recomputed fresh from *now* (not from the old next_run_at),
        so a missed trigger (server was down) fires once when noticed instead of catching up in
        a burst of back-to-back runs.
        """
        now = now or datetime.now()
        with self._connect() as conn:
            candidates = conn.execute(
                "SELECT * FROM schedules WHERE active=1 AND next_run_at<=? ORDER BY next_run_at",
                (now.isoformat(),),
            ).fetchall()
        claimed: list[dict[str, Any]] = []
        for candidate in candidates:
            row = dict(candidate)
            old_next_run_at = row["next_run_at"]
            if row["date"]:
                new_active, new_next_run_at = 0, old_next_run_at
            else:
                new_active, new_next_run_at = 1, self._next_daily_run(row["time"], now).isoformat()
            with self._connect() as conn:
                cur = conn.execute(
                    "UPDATE schedules SET active=?, next_run_at=? WHERE id=? AND active=1 AND next_run_at=?",
                    (new_active, new_next_run_at, row["id"], old_next_run_at),
                )
            if cur.rowcount == 1:
                claimed.append(row)
        return claimed

    def cancel(self, schedule_id: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute("UPDATE schedules SET active=0 WHERE id=? AND active=1", (schedule_id,))
        return cur.rowcount > 0

    def backfill_owner(self, user_id: str) -> int:
        """One-time multi-user migration helper: attributes every pre-existing schedule
        (owner_id still NULL, from back when this deployment was single-user) to the given
        user (the newly-created admin account)."""
        with self._connect() as conn:
            cur = conn.execute("UPDATE schedules SET owner_id=? WHERE owner_id IS NULL", (user_id,))
            return cur.rowcount

    def record_result(self, schedule_id: str, result_summary: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE schedules SET last_run_at=?, last_result=? WHERE id=?",
                (datetime.now().isoformat(), result_summary[:4000], schedule_id),
            )
