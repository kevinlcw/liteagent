"""Per-user, self-managed system-prompt customization (separate from the admin-only global
DEFAULT_SYSTEM_PROMPT/settings.system_prompt in config.py). A regular user cannot see or edit
the base persona/tool-safety rules (that stays admin-only, same as before), but can set a short
personal instruction ("please reply in Simplified Chinese", "keep answers short", etc.) that
Agent._system_message() appends on top of the base prompt for just their own conversations.

Follows the same NULL-safe ownership pattern used throughout this codebase (memory_store.py,
schedule_store.py, custom_domains_store.py): user_id is a nullable column, not a primary key,
so the "multi-user auth disabled" case (user_id always None) naturally degrades to a single
shared row -- exactly like the pre-multi-user behaviour of everything else here.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sqlite3

MAX_CHARS = 4000


class UserPrefsStore:
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
                CREATE TABLE IF NOT EXISTS user_prefs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT,
                    custom_system_prompt TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_user_prefs_user ON user_prefs(user_id)")

    def get(self, user_id: str | None) -> str:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT custom_system_prompt FROM user_prefs WHERE user_id IS ? ORDER BY id LIMIT 1",
                (user_id,),
            ).fetchone()
        return row["custom_system_prompt"] if row else ""

    def set(self, user_id: str | None, content: str) -> str:
        content = str(content or "").strip()
        if len(content) > MAX_CHARS:
            raise ValueError(f"自訂指令長度不可超過 {MAX_CHARS} 字元（目前 {len(content)} 字元）")
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT id FROM user_prefs WHERE user_id IS ? ORDER BY id LIMIT 1", (user_id,)
            ).fetchone()
            now = datetime.now(timezone.utc).isoformat()
            if existing:
                conn.execute(
                    "UPDATE user_prefs SET custom_system_prompt=?, updated_at=? WHERE id=?",
                    (content, now, existing["id"]),
                )
            else:
                conn.execute(
                    "INSERT INTO user_prefs (user_id, custom_system_prompt, updated_at) VALUES (?, ?, ?)",
                    (user_id, content, now),
                )
        return content
