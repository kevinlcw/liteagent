"""Background poll loop that dispatches due scheduled tasks (see schedule_store.py) and
emails their result. Runs in a plain daemon thread — not asyncio — because the agent's LLM
calls are synchronous/blocking (requests-based), and a thread keeps that off the FastAPI
event loop without needing an extra async HTTP client just for this.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from .agent import Agent
from .config import Config
from .mailer import EmailNotConfigured, send_email
from .schedule_store import ScheduleStore

POLL_INTERVAL_SECONDS = 20


class SchedulerRunner:
    def __init__(self, agent: Agent, store: ScheduleStore, config: Config):
        self.agent = agent
        self.store = store
        self.config = config
        self.logger = logging.getLogger("liteagent.scheduler")
        if not self.logger.handlers:
            handler = logging.FileHandler(config.log_path, encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            self.logger.addHandler(handler)
        self.logger.setLevel(logging.INFO)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="liteagent-scheduler", daemon=True)
        self._thread.start()
        self.logger.info("排程輪詢已啟動（每 %s 秒檢查一次）", POLL_INTERVAL_SECONDS)

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:
                self.logger.exception("排程輪詢發生未預期錯誤")
            self._stop.wait(POLL_INTERVAL_SECONDS)

    def _tick(self) -> None:
        # claim_due() already advances/deactivates each row atomically as part of claiming it
        # (see its docstring) — safe even with multiple app processes polling the same DB file
        # concurrently (e.g. separate :80/:443 daemons for HTTP and HTTPS), each due schedule is claimed
        # and dispatched by exactly one of them.
        for row in self.store.claim_due():
            self._run_one(row)

    def _run_one(self, row: dict[str, Any]) -> None:
        self.logger.info("執行排程任務 id=%s message=%r", row["id"], row["message"][:80])
        try:
            result, auto_denied = self.agent.run_unattended(row["message"], user_id=row.get("owner_id"))
            summary = result.content or "(無內容)"
            status_label = "success"
        except Exception as exc:
            summary = f"執行失敗：{type(exc).__name__}: {exc}"
            auto_denied = 0
            status_label = "error"
            self.logger.exception("排程 id=%s 執行時發生例外", row["id"])

        note = ""
        if auto_denied:
            note = f"\n\n（本次為排程自動執行，過程中有 {auto_denied} 個危險操作因無人在場核准而自動略過、未實際執行）"

        self.store.record_result(row["id"], f"[{status_label}] {summary}")

        if row.get("notify_email"):
            subject = f"[LiteAgent 排程通知] {row['message'][:40]}"
            body = (
                f"排程任務內容：{row['message']}\n"
                f"觸發時間：{row['time']}" + (f"（{row['date']}，僅此一次）" if row.get("date") else "（每日重複）") +
                f"\n\n執行結果：\n{summary}{note}"
            )
            try:
                send_email(self.config, row["notify_email"], subject, body)
                self.logger.info("排程 id=%s 結果已寄信至 %s", row["id"], row["notify_email"])
            except EmailNotConfigured as exc:
                self.logger.warning("排程 id=%s 執行完成，但未寄信：%s", row["id"], exc)
            except Exception:
                self.logger.exception("排程 id=%s 寄信失敗", row["id"])
