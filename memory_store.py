"""Persistent, cross-conversation memory notes (separate from per-conversation chat history
in db.py). Two independent buckets ("targets"): 'memory' for facts/lessons the agent itself
learned, 'user' for a profile of the human it's talking to — mirrors the memory_add/replace/
remove pattern used elsewhere in this project's own tooling, so the LLM already "knows" the
ergonomics: substring match on old_text, must resolve to exactly one note.

Multi-user note: target='memory' stays a single global bucket shared by everyone (it's the
agent's own knowledge about how *systems* behave, equally useful no matter who's asking) --
those rows always have user_id=NULL. target='user' is a per-person profile, so those rows are
scoped by user_id (NULL only for pre-multi-user legacy rows, until backfilled to the admin
account -- see web.py's startup migration). When multi-user auth is disabled, user_id is
always None everywhere, which behaves exactly like the pre-multi-user single shared bucket.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import sqlite3

VALID_TARGETS = ("memory", "user")


class MemoryStore:
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
                CREATE TABLE IF NOT EXISTS memory_notes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    target TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_notes_target ON memory_notes(target, id)")
            existing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(memory_notes)").fetchall()}
            if "user_id" not in existing_cols:
                conn.execute("ALTER TABLE memory_notes ADD COLUMN user_id TEXT")
            if "assisted_by_email" not in existing_cols:
                conn.execute("ALTER TABLE memory_notes ADD COLUMN assisted_by_email TEXT")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_notes_user ON memory_notes(target, user_id)")

    @staticmethod
    def _owner_clause(target: str, user_id: str | None) -> tuple[str, list[Any]]:
        """target='memory' rows are always global (user_id IS NULL, ignore the caller's
        user_id); target='user' rows are scoped to the given user_id (or NULL/legacy rows
        when user_id is None, i.e. multi-user auth is off)."""
        if target == "memory":
            return "target=? AND user_id IS NULL", [target]
        return "target=? AND user_id IS ?", [target, user_id]

    def add(self, target: str, content: str, user_id: str | None = None, assisted_by_email: str | None = None) -> dict[str, Any]:
        """assisted_by_email: set when an admin created this note while impersonating another
        user (see web.py) -- surfaced back to that user later so they can see it wasn't
        something they wrote themselves. None in the normal (non-impersonated) case."""
        content = content.strip()
        owner = None if target == "memory" else user_id
        clause, params = self._owner_clause(target, user_id)
        with self._connect() as conn:
            existing = conn.execute(
                f"SELECT id FROM memory_notes WHERE {clause} AND content=?", (*params, content)
            ).fetchone()
            if existing:
                return {"id": existing["id"], "skipped": True}
            cur = conn.execute(
                "INSERT INTO memory_notes (target, content, created_at, user_id, assisted_by_email) VALUES (?, ?, ?, ?, ?)",
                (target, content, datetime.now(timezone.utc).isoformat(), owner, assisted_by_email),
            )
            return {"id": cur.lastrowid, "skipped": False}

    def list(self, target: str | None = None, user_id: str | None = None) -> list[dict[str, Any]]:
        """No target given: every global 'memory' note plus this user's own 'user' notes
        (exactly what should be injected into that user's system prompt)."""
        with self._connect() as conn:
            if target:
                clause, params = self._owner_clause(target, user_id)
                rows = conn.execute(
                    f"SELECT id, target, content, created_at, assisted_by_email FROM memory_notes WHERE {clause} ORDER BY id",
                    params,
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT id, target, content, created_at, assisted_by_email FROM memory_notes
                       WHERE (target='memory' AND user_id IS NULL) OR (target='user' AND user_id IS ?)
                       ORDER BY target, id""",
                    (user_id,),
                ).fetchall()
        return [dict(row) for row in rows]

    def total_chars(self, target: str, user_id: str | None = None) -> int:
        clause, params = self._owner_clause(target, user_id)
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT COALESCE(SUM(LENGTH(content)), 0) AS total FROM memory_notes WHERE {clause}", params
            ).fetchone()
        return int(row["total"] or 0)

    def find_by_substring(self, target: str, needle: str, user_id: str | None = None) -> list[dict[str, Any]]:
        clause, params = self._owner_clause(target, user_id)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT id, content FROM memory_notes WHERE {clause} AND content LIKE ? ESCAPE '\\'",
                (*params, "%" + needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"),
            ).fetchall()
        return [dict(row) for row in rows]

    def get(self, note_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, target, content, created_at, user_id, assisted_by_email FROM memory_notes WHERE id=?", (note_id,)
            ).fetchone()
        return dict(row) if row else None

    def remove(self, note_id: int) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM memory_notes WHERE id=?", (note_id,))

    def replace(self, note_id: int, content: str) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE memory_notes SET content=? WHERE id=?", (content.strip(), note_id))

    def backfill_owner(self, user_id: str) -> int:
        """One-time multi-user migration helper: attributes every pre-existing target='user'
        note (created back when this deployment was single-user, so user_id is still NULL)
        to the given user (the newly-created admin account)."""
        with self._connect() as conn:
            cur = conn.execute("UPDATE memory_notes SET user_id=? WHERE target='user' AND user_id IS NULL", (user_id,))
            return cur.rowcount
