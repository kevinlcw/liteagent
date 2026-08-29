"""SQLite-backed conversation memory (separate from the external db_query tool)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import json
import sqlite3
import uuid


class ConversationStore:
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
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT,
                    tool_calls TEXT,
                    tool_call_id TEXT,
                    name TEXT,
                    reasoning TEXT,
                    created_at TEXT NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id, id)")
            existing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(messages)").fetchall()}
            if "stats" not in existing_cols:
                conn.execute("ALTER TABLE messages ADD COLUMN stats TEXT")
            if "assisted_by_email" not in existing_cols:
                conn.execute("ALTER TABLE messages ADD COLUMN assisted_by_email TEXT")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS conversation_meta (
                    conversation_id TEXT PRIMARY KEY,
                    title TEXT,
                    updated_at TEXT NOT NULL
                )
            """)
            existing_meta_cols = {row["name"] for row in conn.execute("PRAGMA table_info(conversation_meta)").fetchall()}
            if "summary" not in existing_meta_cols:
                conn.execute("ALTER TABLE conversation_meta ADD COLUMN summary TEXT")
            if "summarized_through_id" not in existing_meta_cols:
                conn.execute("ALTER TABLE conversation_meta ADD COLUMN summarized_through_id INTEGER DEFAULT 0")
            if "plan" not in existing_meta_cols:
                conn.execute("ALTER TABLE conversation_meta ADD COLUMN plan TEXT")
            if "owner_id" not in existing_meta_cols:
                conn.execute("ALTER TABLE conversation_meta ADD COLUMN owner_id TEXT")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_conversation_meta_owner ON conversation_meta(owner_id)")

    def ensure_owner(self, conversation_id: str, user_id: str) -> None:
        """Records who a (possibly brand-new) conversation belongs to. Only ever *sets* the
        owner if it isn't already set (COALESCE) -- safe to call on every turn, never
        clobbers an existing owner. Called right when a conversation_id is first minted
        (see Agent.chat/run_stream) so every conversation has an owner from message #1."""
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO conversation_meta (conversation_id, owner_id, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(conversation_id) DO UPDATE SET
                       owner_id=COALESCE(conversation_meta.owner_id, excluded.owner_id)""",
                (conversation_id, user_id, datetime.now(timezone.utc).isoformat()),
            )

    def get_owner(self, conversation_id: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT owner_id FROM conversation_meta WHERE conversation_id=?", (conversation_id,)
            ).fetchone()
        return row["owner_id"] if row else None

    def backfill_owner(self, user_id: str) -> int:
        """One-time multi-user migration helper: attributes every pre-existing conversation
        (created back when this deployment was single-user, so owner_id is still NULL) to
        the given user. Unused in the single-user build (kept for parity with call sites that
        still pass a user_id) -- see backfill_owner's docstring history for context."""
        with self._connect() as conn:
            # Count *before* inserting -- a freshly-INSERTed meta row already has owner_id
            # set (not NULL), so the UPDATE below alone would miss counting it.
            missing = conn.execute(
                """SELECT COUNT(DISTINCT conversation_id) AS c FROM messages
                   WHERE conversation_id NOT IN (SELECT conversation_id FROM conversation_meta)"""
            ).fetchone()["c"]
            conn.execute(
                """INSERT INTO conversation_meta (conversation_id, owner_id, updated_at)
                   SELECT DISTINCT conversation_id, ?, ?
                   FROM messages
                   WHERE conversation_id NOT IN (SELECT conversation_id FROM conversation_meta)""",
                (user_id, datetime.now(timezone.utc).isoformat()),
            )
            cur = conn.execute("UPDATE conversation_meta SET owner_id=? WHERE owner_id IS NULL", (user_id,))
            return missing + cur.rowcount

    @staticmethod
    def new_id() -> str:
        return str(uuid.uuid4())

    def append(self, conversation_id: str, message: dict[str, Any]) -> None:
        """message may carry an "assisted_by_email" key (only ever set on a 'user'-role
        message, when an admin sent it while impersonating another user -- see web.py) so the
        real owner can later see in their own history that particular turn wasn't self-typed."""
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO messages
                   (conversation_id, role, content, tool_calls, tool_call_id, name, reasoning, created_at, stats, assisted_by_email)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    conversation_id,
                    message["role"],
                    message.get("content"),
                    json.dumps(message.get("tool_calls"), ensure_ascii=False) if message.get("tool_calls") else None,
                    message.get("tool_call_id"),
                    message.get("name"),
                    message.get("reasoning"),
                    datetime.now(timezone.utc).isoformat(),
                    json.dumps(message.get("stats"), ensure_ascii=False) if message.get("stats") else None,
                    message.get("assisted_by_email"),
                ),
            )

    def load(self, conversation_id: str, after_id: int = 0, before_id: int | None = None) -> list[dict[str, Any]]:
        query = "SELECT role, content, tool_calls, tool_call_id, name FROM messages WHERE conversation_id=? AND id>?"
        params: list[Any] = [conversation_id, after_id]
        if before_id is not None:
            query += " AND id<?"
            params.append(before_id)
        query += " ORDER BY id"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        messages = []
        for row in rows:
            item = {"role": row["role"], "content": row["content"]}
            if row["tool_calls"]:
                item["tool_calls"] = json.loads(row["tool_calls"])
            if row["tool_call_id"]:
                item["tool_call_id"] = row["tool_call_id"]
            if row["name"]:
                item["name"] = row["name"]
            messages.append(item)
        return messages

    def user_turn_ids(self, conversation_id: str, after_id: int = 0) -> list[int]:
        """Ids of user-authored messages after after_id; each marks the start of a "turn" and is
        therefore a safe point to cut history at (never splits an assistant/tool_call pairing)."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id FROM messages WHERE conversation_id=? AND id>? AND role='user' ORDER BY id",
                (conversation_id, after_id),
            ).fetchall()
        return [row["id"] for row in rows]

    def latest_input_tokens(self, conversation_id: str) -> int:
        """Real prompt_tokens the LLM reported for the most recently completed turn, used as the
        (already-accurate, no local tokenizer needed) signal for whether to auto-compact history."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT stats FROM messages WHERE conversation_id=? AND stats IS NOT NULL ORDER BY id DESC LIMIT 1",
                (conversation_id,),
            ).fetchone()
        if not row or not row["stats"]:
            return 0
        try:
            return int(json.loads(row["stats"]).get("usage", {}).get("input_tokens") or 0)
        except Exception:
            return 0

    def get_meta(self, conversation_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT summary, summarized_through_id FROM conversation_meta WHERE conversation_id=?",
                (conversation_id,),
            ).fetchone()
        if not row:
            return {"summary": None, "summarized_through_id": 0}
        return {"summary": row["summary"], "summarized_through_id": row["summarized_through_id"] or 0}

    def set_summary(self, conversation_id: str, summary: str, through_id: int) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO conversation_meta (conversation_id, summary, summarized_through_id, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(conversation_id) DO UPDATE SET
                       summary=excluded.summary, summarized_through_id=excluded.summarized_through_id, updated_at=excluded.updated_at""",
                (conversation_id, summary, through_id, datetime.now(timezone.utc).isoformat()),
            )

    def get_plan(self, conversation_id: str) -> list[dict[str, Any]] | None:
        """The model's self-maintained step checklist for this conversation (see the
        update_plan tool), used to render a pinned progress card in the UI. None if the
        model hasn't called update_plan yet for this conversation."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT plan FROM conversation_meta WHERE conversation_id=?",
                (conversation_id,),
            ).fetchone()
        if not row or not row["plan"]:
            return None
        try:
            return json.loads(row["plan"])
        except Exception:
            return None

    def set_plan(self, conversation_id: str, steps: list[dict[str, Any]]) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO conversation_meta (conversation_id, plan, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(conversation_id) DO UPDATE SET
                       plan=excluded.plan, updated_at=excluded.updated_at""",
                (conversation_id, json.dumps(steps, ensure_ascii=False), datetime.now(timezone.utc).isoformat()),
            )

    def display_history(self, conversation_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT role, content, tool_calls, name, reasoning, created_at, stats, assisted_by_email FROM messages WHERE conversation_id=? ORDER BY id",
                (conversation_id,),
            ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["stats"] = json.loads(item["stats"]) if item.get("stats") else None
            items.append(item)
        return items

    def set_title(self, conversation_id: str, title: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO conversation_meta (conversation_id, title, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(conversation_id) DO UPDATE SET
                       title=excluded.title, updated_at=excluded.updated_at""",
                (conversation_id, title, datetime.now(timezone.utc).isoformat()),
            )

    def delete_conversation(self, conversation_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM messages WHERE conversation_id=?", (conversation_id,))
            conn.execute("DELETE FROM conversation_meta WHERE conversation_id=?", (conversation_id,))

    def delete_from_user_turn(self, conversation_id: str, turn_index: int) -> None:
        """Deletes the turn_index-th (0-based, counting only role='user') user
        message and everything at/after it (by id) in this conversation. Used by the "edit
        and resend" UI feature: when a user edits an earlier message of theirs, the old
        (now-superseded) exchange must be physically removed from storage, not just hidden in
        the browser DOM -- otherwise it silently reappears once the page/history is reloaded."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id FROM messages WHERE conversation_id=? AND role='user' ORDER BY id LIMIT 1 OFFSET ?",
                (conversation_id, turn_index),
            ).fetchone()
            if row is None:
                return
            conn.execute("DELETE FROM messages WHERE conversation_id=? AND id>=?", (conversation_id, row["id"]))

    def list_conversations(self, owner_id: str | None = None) -> list[dict[str, Any]]:
        """owner_id=None means "no user filtering" (multi-user auth disabled, or an admin
        wanting to see everything) -- pass the current session's user id to scope the list
        to just that person's own conversations."""
        query = """SELECT grouped.conversation_id, grouped.created_at, grouped.updated_at,
                          grouped.message_count, meta.title, meta.owner_id, first_user.content AS first_user_content
                   FROM (
                       SELECT conversation_id, MIN(created_at) AS created_at,
                              MAX(created_at) AS updated_at, COUNT(*) AS message_count
                       FROM messages GROUP BY conversation_id
                   ) AS grouped
                   LEFT JOIN conversation_meta AS meta
                     ON meta.conversation_id = grouped.conversation_id
                   LEFT JOIN messages AS first_user
                     ON first_user.id = (
                         SELECT MIN(user_message.id) FROM messages AS user_message
                         WHERE user_message.conversation_id = grouped.conversation_id
                           AND user_message.role = 'user'
                     )"""
        params: list[Any] = []
        if owner_id is not None:
            query += " WHERE meta.owner_id=?"
            params.append(owner_id)
        query += " ORDER BY grouped.updated_at DESC"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        conversations = []
        for row in rows:
            item = dict(row)
            first_user = " ".join((item.pop("first_user_content") or "").split())
            item["title"] = item["title"] or (first_user[:30] if first_user else "(空白對話)")
            conversations.append(item)
        return conversations
