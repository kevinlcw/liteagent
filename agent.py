"""Shared agent loop for CLI and web clients."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from collections.abc import Iterator
from typing import Any, Callable
from urllib.parse import quote
import json
import logging
import re
import threading
import time

from .config import Config, settings
from .db import ConversationStore
from .skills import SKILLS_DIRNAME, list_skills
from .llm_client import LLMClient
from .tools import TOOLS_SCHEMA, ToolRegistry, json_result


SUMMARY_SYSTEM_PROMPT = (
    "你是對話歷史壓縮助手，任務是把使用者提供的一段較舊的原始對話內容（使用者訊息、AI 回覆、"
    "工具呼叫與工具回傳結果），濃縮成一份精簡的『目前為止摘要』，讓 AI 之後只讀這份摘要也能接續任務、"
    "不需要看到原始逐字內容。請用條列式寫出：使用者的目標與需求、已經確認或決定的重要事實、關鍵數字、"
    "檔案／資料表路徑、使用者交代過的偏好或限制、尚待處理的事項。捨棄不重要的細節（尤其是大量工具輸出"
    "的逐字內容），但務必保留其中會影響後續判斷的關鍵結論。輸出時直接更新／取代舊摘要（不是附加在後面），"
    "全部使用繁體中文，控制在 800 字以內。"
)


SUBAGENT_SYSTEM_PROMPT = (
    "你是被主要助理暫時委派出去處理單一子任務的子代理人（sub-agent）。"
    "你看不到主線對話的任何歷史，只會收到一段任務描述——請直接動手完成它，"
    "不需要跟使用者互動確認，因為執行過程中沒有真人在場。"
    "遇到你自己判斷屬於危險／不可逆的操作（例如刪除檔案、DROP TABLE 等）一律視為不允許執行，"
    "直接跳過、並在結果中註明，不要嘗試迂迴繞過。"
    "完成後請直接輸出精簡扼要的最終結果或結論文字，不需要客套話。"
)

# Tools intentionally hidden from a sub-agent: no nesting (run_subagent itself), and nothing
# that mutates main-line-only state it has no business touching (the plan belongs to the
# parent conversation; scheduling is a user-facing concept a throwaway helper shouldn't create).
_SUBAGENT_EXCLUDED_TOOLS = {"run_subagent", "schedule_task", "list_schedules", "cancel_schedule", "update_plan"}


def _render_for_summary(messages: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for m in messages:
        role = m.get("role")
        if role == "tool":
            content = m.get("content") or ""
            if len(content) > 1500:
                content = content[:1500] + f"...(截斷，原長度 {len(content)} 字元)"
            lines.append(f"[工具結果／{m.get('name') or '?'}] {content}")
        elif role == "assistant":
            parts = []
            if m.get("content"):
                parts.append(m["content"])
            for call in m.get("tool_calls") or []:
                function = call.get("function") or {}
                parts.append(f"(呼叫工具 {function.get('name')} 參數={function.get('arguments')})")
            if parts:
                lines.append("[AI] " + " ".join(parts))
        elif role == "user":
            lines.append(f"[使用者] {m.get('content') or ''}")
    return "\n".join(lines)


@dataclass
class AgentResult:
    content: str
    conversation_id: str
    events: list[dict[str, Any]] = field(default_factory=list)
    reasoning: list[str] = field(default_factory=list)
    max_iterations_reached: bool = False
    duration_s: float = 0.0
    usage: dict[str, int] = field(default_factory=lambda: {"input_tokens": 0, "output_tokens": 0})


class Agent:
    def __init__(self, config: Config = settings, verbose: bool = False, event_callback: Callable[[dict[str, Any]], None] | None = None):
        self.config = config
        self.config.prepare()
        self.verbose = verbose
        self.event_callback = event_callback
        self.store = ConversationStore(config.sqlite_path)
        self.client = LLMClient(config)
        self.tools = ToolRegistry(config, subagent_runner=self._run_subagent)
        # Per-conversation counter of run_subagent calls made *during the turn currently in
        # progress* (reset at the top of chat()/run_stream() for that conversation_id) — caps
        # how many sub-agents a single user message can fan out to (config.subagent_max_per_turn).
        self._subagent_calls: dict[str, int] = {}
        # Guards the read-modify-write below -- needed now that "parallel" concurrency mode
        # can invoke _run_subagent from several worker threads at once for the same turn.
        self._subagent_calls_lock = threading.Lock()
        self.logger = logging.getLogger(f"liteagent.{id(self)}")
        if not self.logger.handlers:
            handler = logging.FileHandler(config.log_path, encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            self.logger.addHandler(handler)
        self.logger.setLevel(logging.INFO)

    def _event(self, event: dict[str, Any]) -> None:
        self.logger.info(json_result(event))
        if self.verbose:
            print(f"[agent] {json_result(event)}")
        if self.event_callback:
            self.event_callback(event)

    SKILLS_GUIDE = (
        "\n\n【技能（skills）機制】除了固定工具，你還有 list_skills／load_skill 可查詢與載入"
        "可重複使用的技能（格式慣例與 Claude Code、OpenClaw 的 SKILL.md 相同）：\n"
        "- 開始處理較複雜、或看起來會重複出現的任務前，先呼叫 list_skills 確認是否已有現成技能；"
        "有相關的話用 load_skill(name) 讀取完整步驟後照著執行，技能目錄下若有其他檔案"
        "（範例、腳本、模板），視需要再用 read_file 個別讀取，不用一次全部載入。\n"
        "- 若使用者要你把一套流程記錄下來供之後重複使用，用 write_file 在 "
        "workspace/skills/<英文短名稱>/SKILL.md 建立新技能，檔案開頭需有以下格式的 frontmatter，"
        "之後接詳細步驟說明：\n"
        "  ---\n  name: 技能名稱\n  description: 一句話說明什麼情況該用這個技能\n  ---\n"
        "  （詳細步驟...）"
    )

    MEMORY_GUIDE = (
        "\n\n【長期記憶機制】除了這次對話本身的歷史，你還有 memory_add／memory_replace／"
        "memory_remove／memory_list 四個工具可以維護一份『跨對話』都會自動顯示給你參考的長期筆記，"
        "不需要使用者每次重新告訴你：\n"
        "- target='memory'：你自己學到、值得記住的事實／慣例／教訓（例如某個系統的固定行為、"
        "之前踩過的坑、使用者交代過但還沒完成的長期任務）。\n"
        "- target='user'：關於使用者本人的側寫（例如稱呼、偏好、背景）。\n"
        "- 對話中發現值得長期記住的事，或使用者提到重要偏好／背景時，主動呼叫 memory_add 記下來，"
        "不用等使用者要求；內容盡量簡潔扼要。\n"
        "- 每個 target 各有字數上限，寫不進去時用 memory_replace 把舊筆記精簡後再寫，"
        "或用 memory_remove 刪除不需要的筆記。\n"
        "- memory_replace／memory_remove 用 old_text 子字串比對既有筆記，必須唯一比對到一則，"
        "比對到多則或找不到都會出錯，這時用更明確的片段或先 memory_list 確認。"
    )

    _MD_LINK_RE = re.compile(r'(!?)\[([^\]]*)\]\(([^()]+)\)')

    def _fix_file_links(self, content: str | None) -> str | None:
        """Models often reference generated/uploaded files with a bare relative path
        (e.g. ![chart](foo.png)), which the browser resolves against the page URL and
        404s. Rewrite any markdown link/image whose target is a real file under the
        workspace to the actual download route; leave everything else untouched."""
        if not content:
            return content

        def repl(match: re.Match) -> str:
            bang, text, path = match.group(1), match.group(2), match.group(3).strip()
            if path.startswith(("http://", "https://", "/api/files/download/", "data:", "#", "mailto:")):
                return match.group(0)
            candidate = path.lstrip("/")
            try:
                target = self.tools.safe_path(candidate)
            except PermissionError:
                return match.group(0)
            if not target.is_file():
                return match.group(0)
            encoded = "/".join(quote(part) for part in candidate.split("/"))
            return f"{bang}[{text}](/api/files/download/{encoded})"

        return self._MD_LINK_RE.sub(repl, content)

    def _memory_block(self, user_id: str | None = None) -> str:
        """Render the persistent cross-conversation memory notes (see memory_store.py /
        the memory_add|replace|remove|list tools) as a block to fold into the system prompt.
        Empty string if nothing has been saved yet. user_id scopes the personal 'user' notes
        to whoever is actually chatting right now (see memory_store.py's multi-user note);
        the global 'memory' bucket is unaffected."""
        notes = self.tools.memory.list(user_id=user_id)
        if not notes:
            return ""
        memory_notes = [n["content"] for n in notes if n["target"] == "memory"]
        user_notes = [n["content"] for n in notes if n["target"] == "user"]
        parts: list[str] = []
        if memory_notes:
            parts.append("你自己過去學到、想長期記住的事：\n" + "\n".join(f"- {c}" for c in memory_notes))
        if user_notes:
            parts.append("關於使用者的側寫：\n" + "\n".join(f"- {c}" for c in user_notes))
        return "\n\n".join(parts)

    def _system_message(self, summary: str | None = None, user_id: str | None = None) -> dict[str, str]:
        content = self.config.system_prompt + self.SKILLS_GUIDE + self.MEMORY_GUIDE
        skills = list_skills(self.config.allowed_root / SKILLS_DIRNAME)
        if skills:
            lines = "\n".join(f"- {s['name']}：{s['description']}" for s in skills)
            content += "\n\n目前已存在的技能：\n" + lines
        memory_block = self._memory_block(user_id)
        if memory_block:
            content += (
                "\n\n【長期記憶（跨對話持久保存，供你參考背景脈絡；不需要主動提起，"
                "除非使用者問起或明顯用得到；可用 memory_add／memory_replace／memory_remove 主動維護）】\n"
                + memory_block
            )
        if summary:
            content += (
                "\n\n【先前對話摘要（較舊的內容已自動壓縮，僅供你參考背景脈絡，"
                "不需要主動提起，除非使用者問起或明顯需要用到）】\n" + summary
            )
        return {"role": "system", "content": content}

    def _compact_if_needed(self, conversation_id: str) -> dict[str, Any] | None:
        """If the last turn's real prompt-token usage crossed the configured threshold, fold the
        older part of this conversation's history into a rolling summary (kept in conversation_meta),
        leaving only the most recent N turns verbatim. Returns a "compacted" event dict on success,
        or None if nothing was done (below threshold / not enough history yet / summarization failed)."""
        threshold = self.config.context_window_tokens * self.config.compact_trigger_ratio
        if self.store.latest_input_tokens(conversation_id) < threshold:
            return None
        meta = self.store.get_meta(conversation_id)
        boundaries = self.store.user_turn_ids(conversation_id, after_id=meta["summarized_through_id"])
        keep = self.config.compact_keep_recent_turns
        if len(boundaries) <= keep:
            return None
        cut_id = boundaries[-keep]
        old_segment = self.store.load(conversation_id, after_id=meta["summarized_through_id"], before_id=cut_id)
        if not old_segment:
            return None
        try:
            summarize_messages = [
                {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                        f"【目前摘要】\n{meta['summary'] or '（無，這是第一次壓縮）'}\n\n"
                        f"【要摺進摘要的新一段原始對話】\n{_render_for_summary(old_segment)}"
                    ),
                },
            ]
            response = self.client.chat(summarize_messages)
            new_summary = (response["message"].get("content") or "").strip()
            if not new_summary:
                return None
        except Exception as exc:
            self.logger.warning(f"auto-compact summarization failed, skipping this round: {exc}")
            return None
        self.store.set_summary(conversation_id, new_summary, cut_id - 1)
        return {
            "type": "compacted",
            "folded_messages": len(old_segment),
            "kept_recent_turns": keep,
            "summary_chars": len(new_summary),
        }

    def _run_subagent(self, task: str, conversation_id: str | None = None, user_id: str | None = None, assisted_by: str | None = None) -> dict[str, Any]:
        """Run a short-lived, isolated agent loop for one self-contained sub-task (invoked via
        the run_subagent tool). Synchronous/blocking — the parent loop is paused until this
        returns. Deliberately minimal compared to chat()/run_stream(): no DB persistence of its
        internal turns (only the final summary re-enters the parent conversation as a normal
        tool result), no nested sub-agents (its own tool schema omits run_subagent), and any
        risky tool call it attempts is auto-denied on the spot (there's no human watching a
        sub-agent run, same posture as run_unattended for scheduled tasks)."""
        key = conversation_id or ""
        with self._subagent_calls_lock:
            count = self._subagent_calls.get(key, 0) + 1
            self._subagent_calls[key] = count
        if count > self.config.subagent_max_per_turn:
            raise RuntimeError(
                f"這一輪已經呼叫過 {self.config.subagent_max_per_turn} 次 run_subagent，已達上限，"
                "請自行完成剩餘工作或彙整既有結果，不要再開新的 sub-agent"
            )

        started_at = time.monotonic()
        schema = [t for t in TOOLS_SCHEMA if t["function"]["name"] not in _SUBAGENT_EXCLUDED_TOOLS]
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SUBAGENT_SYSTEM_PROMPT},
            {"role": "user", "content": task},
        ]
        steps = 0
        truncated = False
        last_content = ""
        for _ in range(self.config.subagent_max_iterations):
            if time.monotonic() - started_at > self.config.subagent_max_seconds:
                truncated = True
                break
            response = self.client.chat(messages, schema)
            raw = response["message"]
            content = raw.get("content") or ""
            if content:
                last_content = content
            assistant: dict[str, Any] = {"role": "assistant", "content": content or None}
            if raw.get("tool_calls"):
                assistant["tool_calls"] = raw["tool_calls"]
            messages.append(assistant)
            has_tool_calls = response["finish_reason"] == "tool_calls" or bool(raw.get("tool_calls"))
            if not has_tool_calls:
                break
            for call in raw.get("tool_calls", []):
                steps += 1
                function = call.get("function") or {}
                name = function.get("name", "")
                try:
                    arguments = json.loads(function.get("arguments") or "{}")
                    if not isinstance(arguments, dict):
                        raise ValueError("arguments must be a JSON object")
                except Exception as exc:
                    result: dict[str, Any] = {"ok": False, "error": f"Invalid tool arguments: {exc}"}
                else:
                    risk = self.tools.assess_risk(name, arguments)
                    if risk:
                        result = {
                            "ok": False,
                            "denied": True,
                            "error": f"此操作需要人工核准（{risk}），但 sub-agent 執行時沒有真人在場，已自動略過、未實際執行。",
                        }
                    else:
                        result = self.tools.execute(name, arguments, conversation_id=conversation_id, user_id=user_id, assisted_by_email=assisted_by)
                messages.append({"role": "tool", "tool_call_id": call.get("id", ""), "name": name, "content": json_result(result)})
        else:
            truncated = True

        duration_s = round(time.monotonic() - started_at, 1)
        if not last_content:
            last_content = (
                "（子代理人已達步驟／時間上限，尚未產出文字結論；以上為部分執行過程）"
                if truncated else "（子代理人沒有回傳文字內容）"
            )
        return {"content": last_content, "steps": steps, "duration_s": duration_s, "truncated": truncated}

    def chat(
        self,
        user_message: str,
        conversation_id: str | None = None,
        llm_content: Any = None,
        user_id: str | None = None,
        assisted_by: str | None = None,
    ) -> AgentResult:
        started_at = time.monotonic()
        conversation_id = conversation_id or self.store.new_id()
        if user_id:
            self.store.ensure_owner(conversation_id, user_id)
        self._subagent_calls[conversation_id] = 0
        events: list[dict[str, Any]] = []
        compacted = self._compact_if_needed(conversation_id)
        if compacted:
            self._event(compacted)
            events.append(compacted)
        meta = self.store.get_meta(conversation_id)
        history = self.store.load(conversation_id, after_id=meta["summarized_through_id"])
        user: dict[str, Any] = {"role": "user", "content": user_message}
        if assisted_by:
            # This turn was sent by an admin impersonating `user_id` rather than typed by that
            # person themselves -- tagged so they can see that later in their own history.
            user["assisted_by_email"] = assisted_by
        self.store.append(conversation_id, user)
        first_message = {"role": "user", "content": llm_content if llm_content is not None else user_message}
        messages = [self._system_message(meta["summary"], user_id), *history, first_message]
        reasoning_parts: list[str] = []
        input_tokens = 0
        output_tokens = 0

        for iteration in range(1, self.config.max_iterations + 1):
            response = self.client.chat(messages, self.tools.full_schema())
            round_usage = response.get("usage") or {}
            input_tokens += int(round_usage.get("prompt_tokens") or 0)
            output_tokens += int(round_usage.get("completion_tokens") or 0)
            raw = response["message"]
            fixed_content = self._fix_file_links(raw.get("content"))
            reasoning = raw.get("reasoning")
            if reasoning:
                reasoning_parts.append(reasoning)
                self._event({"type": "reasoning", "iteration": iteration, "reasoning": reasoning})
            assistant = {"role": "assistant", "content": fixed_content}
            if raw.get("tool_calls"):
                assistant["tool_calls"] = raw["tool_calls"]
            has_tool_calls = response["finish_reason"] == "tool_calls" or bool(raw.get("tool_calls"))
            content_preview = fixed_content or ""
            is_final = not has_tool_calls and (response["finish_reason"] == "stop" or bool(content_preview))
            stored_assistant = dict(assistant)
            if reasoning:
                stored_assistant["reasoning"] = reasoning
            if is_final:
                stored_assistant["stats"] = {
                    "duration_s": round(time.monotonic() - started_at, 1),
                    "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
                }
            messages.append(assistant)
            self.store.append(conversation_id, stored_assistant)

            if has_tool_calls:
                for call in raw.get("tool_calls", []):
                    function = call.get("function", {})
                    name = function.get("name", "")
                    try:
                        arguments = json.loads(function.get("arguments") or "{}")
                        if not isinstance(arguments, dict):
                            raise ValueError("arguments must be a JSON object")
                    except Exception as exc:
                        result = {"ok": False, "error": f"Invalid tool arguments: {exc}"}
                        arguments = {"_raw": function.get("arguments")}
                    else:
                        self._event({"type": "tool_call", "iteration": iteration, "name": name, "arguments": arguments})
                        result = self.tools.execute(name, arguments, conversation_id=conversation_id, user_id=user_id, assisted_by_email=assisted_by)
                    result_text = json_result(result)
                    event = {"type": "tool_result", "iteration": iteration, "name": name, "result": result}
                    events.extend([{"type": "tool_call", "iteration": iteration, "name": name, "arguments": arguments}, event])
                    self._event(event)
                    tool_message = {"role": "tool", "tool_call_id": call.get("id", ""), "name": name, "content": result_text}
                    messages.append(tool_message)
                    self.store.append(conversation_id, tool_message)
                continue

            content = fixed_content or ""
            if response["finish_reason"] == "stop" or content:
                return AgentResult(
                    content=content,
                    conversation_id=conversation_id,
                    events=events,
                    reasoning=reasoning_parts,
                    duration_s=round(time.monotonic() - started_at, 1),
                    usage={"input_tokens": input_tokens, "output_tokens": output_tokens},
                )

        notice = "已達最大工具呼叫次數"
        notice_stats = {
            "duration_s": round(time.monotonic() - started_at, 1),
            "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
        }
        self.store.append(conversation_id, {"role": "assistant", "content": notice, "stats": notice_stats})
        self._event({"type": "max_iterations", "limit": self.config.max_iterations})
        return AgentResult(
            content=notice,
            conversation_id=conversation_id,
            events=events,
            reasoning=reasoning_parts,
            max_iterations_reached=True,
            duration_s=round(time.monotonic() - started_at, 1),
            usage={"input_tokens": input_tokens, "output_tokens": output_tokens},
        )

    def run(
        self,
        user_message: str,
        conversation_id: str | None = None,
        llm_content: Any = None,
        user_id: str | None = None,
        assisted_by: str | None = None,
    ) -> AgentResult:
        """Explicit non-streaming entry point kept for API callers."""
        return self.chat(user_message, conversation_id, llm_content=llm_content, user_id=user_id, assisted_by=assisted_by)

    def _append_tool_message(self, conversation_id: str, messages: list[dict[str, Any]], call_id: str, name: str, result: Any) -> None:
        tool_message = {"role": "tool", "tool_call_id": call_id, "name": name, "content": json_result(result)}
        messages.append(tool_message)
        self.store.append(conversation_id, tool_message)

    def _run_tool_calls(
        self,
        conversation_id: str,
        iteration: int,
        messages: list[dict[str, Any]],
        calls: list[dict[str, Any]],
        decisions: dict[str, bool] | None = None,
        user_id: str | None = None,
        assisted_by: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Execute `calls` in order, appending tool result messages to `messages`/DB as we go.

        `decisions` optionally supplies pre-made approve(True)/deny(False) answers for calls
        (keyed by tool_call_id) that were previously paused awaiting human confirmation.

        This is a *generator*: it yields each event the moment it's produced, rather than
        collecting a whole batch and returning it at the end — that matters for a long-running
        call (e.g. run_subagent) whose `tool_start`/`subagent_start` event needs to reach the
        client *before* the blocking work finishes, not bundled together with its result
        afterwards. Its generator return value is (events, paused) — recoverable via
        `yield from`, same pattern as `_run_llm_iteration`. When a call has no risk
        classification it's executed immediately. When it IS flagged risky and has no decision
        yet, execution stops right there (a `confirm_required` event is emitted and paused=True
        is returned) — that call and everything after it in this batch are left unexecuted, and
        stay recoverable purely from DB state (the assistant message with tool_calls is already
        persisted, and any tool_call_id with no matching tool-role reply is "still pending") —
        see `_find_pending_calls` / `resume_stream`.

        Concurrency: everything here still runs one call at a time *except* run_subagent, whose
        behavior is governed by config.subagent_concurrency. In "sequential" mode (the default)
        it's identical to any other tool. In "parallel" mode, a run of consecutive run_subagent
        calls in `calls` that are all immediately executable (no invalid args / denial / risk
        pause among them) is executed concurrently via a thread pool, with their tool_start
        events emitted up front and their tool_result events emitted afterwards in the same
        original order -- other tool calls are unaffected.
        """
        events: list[dict[str, Any]] = []

        def _parse_call(c: dict[str, Any]) -> tuple[str, str, dict[str, Any] | None, str | None]:
            """Returns (call_id, name, arguments_or_None, parse_error_or_None)."""
            c_id = c.get("id", "")
            fn = c.get("function") or {}
            c_name = fn.get("name", "")
            try:
                args = json.loads(fn.get("arguments") or "{}")
                if not isinstance(args, dict):
                    raise ValueError("arguments must be a JSON object")
            except Exception as exc:
                return c_id, c_name, None, f"Invalid tool arguments: {exc}"
            return c_id, c_name, args, None

        i = 0
        n = len(calls)
        while i < n:
            call_id, name, arguments, parse_error = _parse_call(calls[i])
            if parse_error is not None:
                result = {"ok": False, "error": parse_error}
                self._append_tool_message(conversation_id, messages, call_id, name, result)
                ev = {"type": "tool_result", "iteration": iteration, "name": name, "result": result}
                self._event(ev)
                events.append(ev)
                yield ev
                i += 1
                continue

            decision = decisions.get(call_id) if decisions else None
            if decision is False:
                result = {"ok": False, "error": "使用者拒絕執行此操作，因此未實際執行。", "denied": True}
                self._append_tool_message(conversation_id, messages, call_id, name, result)
                ev = {"type": "tool_result", "iteration": iteration, "name": name, "result": result}
                self._event(ev)
                events.append(ev)
                yield ev
                i += 1
                continue
            if decision is None:
                risk = self.tools.assess_risk(name, arguments)
                if risk:
                    plan = self.store.get_plan(conversation_id)
                    current_step = next(
                        (step.get("content") for step in plan if step.get("status") == "in_progress"), None
                    ) if plan else None
                    ev = {
                        "type": "confirm_required",
                        "iteration": iteration,
                        "conversation_id": conversation_id,
                        "tool_call_id": call_id,
                        "name": name,
                        "arguments": arguments,
                        "risk": risk,
                        "current_step": current_step,
                    }
                    self._event(ev)
                    events.append(ev)
                    yield ev
                    return events, True

            # This call is clear to execute. If it's run_subagent, in "parallel" concurrency
            # mode, and immediately followed by other run_subagent calls that are *also*
            # clear to execute right away (no invalid args / no denial / no risk pause needed),
            # run that whole run of sub-agents concurrently in a thread pool instead of one at
            # a time -- this is what actually makes "多線程" mean anything: without it, the
            # calls would still just be executed back-to-back like "單線程" regardless of the
            # setting. Any other tool (or a lone run_subagent call) still runs exactly as
            # before -- sequentially, one call fully finishing before the next starts.
            if name == "run_subagent" and self.config.subagent_concurrency == "parallel":
                batch: list[tuple[str, str, dict[str, Any]]] = [(call_id, name, arguments)]
                j = i + 1
                while j < n:
                    nxt_id, nxt_name, nxt_args, nxt_error = _parse_call(calls[j])
                    if nxt_name != "run_subagent" or nxt_error is not None:
                        break
                    nxt_decision = decisions.get(nxt_id) if decisions else None
                    if nxt_decision is False:
                        break
                    if nxt_decision is None and self.tools.assess_risk(nxt_name, nxt_args):
                        break
                    batch.append((nxt_id, nxt_name, nxt_args))
                    j += 1
                if len(batch) > 1:
                    for b_id, b_name, b_args in batch:
                        start = {"type": "tool_start", "iteration": iteration, "name": b_name, "arguments": b_args}
                        self._event(start)
                        events.append(start)
                        yield start
                        sub_start = {"type": "subagent_start", "iteration": iteration, "task": str(b_args.get("task") or "")}
                        self._event(sub_start)
                        events.append(sub_start)
                        yield sub_start
                    results: list[dict[str, Any]] = [{}] * len(batch)
                    with ThreadPoolExecutor(max_workers=len(batch)) as pool:
                        future_to_index = {
                            pool.submit(
                                self.tools.execute, b_name, b_args,
                                conversation_id=conversation_id, user_id=user_id, assisted_by_email=assisted_by,
                            ): idx
                            for idx, (b_id, b_name, b_args) in enumerate(batch)
                        }
                        for future in future_to_index:
                            idx = future_to_index[future]
                            try:
                                results[idx] = future.result()
                            except Exception as exc:
                                results[idx] = {"ok": False, "error": f"子代理人執行失敗：{exc}"}
                    for (b_id, b_name, b_args), result in zip(batch, results):
                        self._append_tool_message(conversation_id, messages, b_id, b_name, result)
                        result_event = {"type": "tool_result", "iteration": iteration, "name": b_name, "result": result}
                        self._event(result_event)
                        events.append(result_event)
                        yield result_event
                        sub_result = result.get("result") or {}
                        sub_end = {
                            "type": "subagent_end",
                            "iteration": iteration,
                            "ok": bool(result.get("ok")),
                            "truncated": bool(sub_result.get("truncated")),
                        }
                        self._event(sub_end)
                        events.append(sub_end)
                        yield sub_end
                    i = j
                    continue

            start = {"type": "tool_start", "iteration": iteration, "name": name, "arguments": arguments}
            self._event(start)
            events.append(start)
            yield start
            if name == "run_subagent":
                sub_start = {"type": "subagent_start", "iteration": iteration, "task": str(arguments.get("task") or "")}
                self._event(sub_start)
                events.append(sub_start)
                yield sub_start
            result = self.tools.execute(name, arguments, conversation_id=conversation_id, user_id=user_id, assisted_by_email=assisted_by)
            self._append_tool_message(conversation_id, messages, call_id, name, result)
            result_event = {"type": "tool_result", "iteration": iteration, "name": name, "result": result}
            self._event(result_event)
            events.append(result_event)
            yield result_event
            if name == "run_subagent":
                sub_result = result.get("result") or {}
                sub_end = {
                    "type": "subagent_end",
                    "iteration": iteration,
                    "ok": bool(result.get("ok")),
                    "truncated": bool(sub_result.get("truncated")),
                }
                self._event(sub_end)
                events.append(sub_end)
                yield sub_end
            if name == "update_plan" and result.get("ok"):
                plan_event = {
                    "type": "plan_update",
                    "iteration": iteration,
                    "conversation_id": conversation_id,
                    "steps": (result.get("result") or {}).get("steps", []),
                }
                self._event(plan_event)
                events.append(plan_event)
                yield plan_event
            i += 1
        return events, False

    @staticmethod
    def _find_pending_calls(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Scan `history` in order and return the tool_calls from the most recent assistant
        turn that still have no matching tool-role reply — i.e. the calls that were left
        unexecuted when a prior run paused on a `confirm_required` event."""
        last_calls: list[dict[str, Any]] = []
        answered: set[str] = set()
        for m in history:
            if m.get("role") == "assistant" and m.get("tool_calls"):
                last_calls = m["tool_calls"]
                answered = set()
            elif m.get("role") == "tool":
                answered.add(m.get("tool_call_id"))
        return [c for c in last_calls if c.get("id") not in answered]

    def _run_llm_iteration(
        self,
        messages: list[dict[str, Any]],
        iteration: int,
        input_tokens: int,
        output_tokens: int,
    ) -> Iterator[dict[str, Any]]:
        """Stream one LLM call, yielding client-facing progress events. On completion
        (generator exhaustion) its return value is (finish_reason, tool_calls, content,
        reasoning, input_tokens, output_tokens) — retrievable via `yield from`."""
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        finish_reason: str | None = None
        tool_calls: list[dict[str, Any]] = []

        for event in self.client.stream_chat_completion(messages, self.tools.full_schema()):
            event_type = event["type"]
            if event_type == "content_delta":
                content_parts.append(event["text"])
                yield {**event, "iteration": iteration}
            elif event_type == "reasoning_delta":
                reasoning_parts.append(event["text"])
                yield {**event, "iteration": iteration}
            elif event_type == "tool_call_delta":
                yield {**event, "iteration": iteration}
            elif event_type == "usage":
                input_tokens += int(event.get("prompt_tokens") or 0)
                output_tokens += int(event.get("completion_tokens") or 0)
                yield {**event, "iteration": iteration}
            elif event_type == "done":
                finish_reason = event.get("finish_reason")
                tool_calls = event.get("tool_calls") or []

        content = self._fix_file_links("".join(content_parts)) or ""
        reasoning = "".join(reasoning_parts)
        return finish_reason, tool_calls, content, reasoning, input_tokens, output_tokens

    def _iterate_stream(
        self,
        conversation_id: str,
        messages: list[dict[str, Any]],
        started_at: float,
        input_tokens: int,
        output_tokens: int,
        start_iteration: int = 1,
        user_id: str | None = None,
        assisted_by: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        """The core think/tool-call/observe loop, shared by a fresh run_stream() call and a
        resume_stream() continuation after a confirmation. Ends either by yielding a `final`
        event, or (if paused on a risky tool call) simply returning with no `final` — the
        caller/frontend is expected to already have surfaced the `confirm_required` event."""
        for iteration in range(start_iteration, self.config.max_iterations + 1):
            finish_reason, tool_calls, content, reasoning, input_tokens, output_tokens = (
                yield from self._run_llm_iteration(messages, iteration, input_tokens, output_tokens)
            )
            assistant: dict[str, Any] = {"role": "assistant", "content": content or None}
            if tool_calls:
                assistant["tool_calls"] = tool_calls
            has_tool_calls = finish_reason == "tool_calls" or bool(tool_calls)
            is_final = not has_tool_calls and (finish_reason == "stop" or bool(content))
            stored_assistant = dict(assistant)
            if reasoning:
                stored_assistant["reasoning"] = reasoning
                self._event({"type": "reasoning", "iteration": iteration, "reasoning": reasoning})
            if is_final:
                stored_assistant["stats"] = {
                    "duration_s": round(time.monotonic() - started_at, 1),
                    "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
                }
            messages.append(assistant)
            self.store.append(conversation_id, stored_assistant)

            if has_tool_calls:
                events, paused = yield from self._run_tool_calls(conversation_id, iteration, messages, tool_calls, user_id=user_id, assisted_by=assisted_by)
                if paused:
                    return
                continue

            if finish_reason == "stop" or content:
                yield {
                    "type": "final",
                    "content": content,
                    "conversation_id": conversation_id,
                    "max_iterations_reached": False,
                    "duration_s": round(time.monotonic() - started_at, 1),
                    "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
                }
                return

        notice = "已達最大工具呼叫次數"
        notice_stats = {
            "duration_s": round(time.monotonic() - started_at, 1),
            "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
        }
        self.store.append(conversation_id, {"role": "assistant", "content": notice, "stats": notice_stats})
        self._event({"type": "max_iterations", "limit": self.config.max_iterations})
        yield {
            "type": "final",
            "content": notice,
            "conversation_id": conversation_id,
            "max_iterations_reached": True,
            "duration_s": round(time.monotonic() - started_at, 1),
            "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
        }

    def run_stream(
        self,
        user_message: str,
        conversation_id: str | None = None,
        llm_content: Any = None,
        user_id: str | None = None,
        assisted_by: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Run the agent loop while yielding model and tool progress events."""
        started_at = time.monotonic()
        conversation_id = conversation_id or self.store.new_id()
        if user_id:
            self.store.ensure_owner(conversation_id, user_id)
        self._subagent_calls[conversation_id] = 0
        compacted = self._compact_if_needed(conversation_id)
        if compacted:
            self._event(compacted)
            yield compacted
        meta = self.store.get_meta(conversation_id)
        history = self.store.load(conversation_id, after_id=meta["summarized_through_id"])
        user: dict[str, Any] = {"role": "user", "content": user_message}
        if assisted_by:
            user["assisted_by_email"] = assisted_by
        self.store.append(conversation_id, user)
        yield {"type": "start", "conversation_id": conversation_id}
        first_message = {"role": "user", "content": llm_content if llm_content is not None else user_message}
        messages = [self._system_message(meta["summary"], user_id), *history, first_message]
        yield from self._iterate_stream(conversation_id, messages, started_at, 0, 0, start_iteration=1, user_id=user_id, assisted_by=assisted_by)

    def resume_stream(
        self,
        conversation_id: str,
        decisions: dict[str, bool],
        user_id: str | None = None,
        assisted_by: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Continue a run that was paused by a `confirm_required` event, once the client has
        supplied approve/deny decisions (by tool_call_id) for the pending call(s)."""
        started_at = time.monotonic()
        meta = self.store.get_meta(conversation_id)
        history = self.store.load(conversation_id, after_id=meta["summarized_through_id"])
        pending = self._find_pending_calls(history)
        if not pending:
            yield {"type": "error", "error": "沒有待確認的操作（可能已經處理過了）"}
            return
        messages = [self._system_message(meta["summary"], user_id), *history]
        # The pending calls belong to the assistant message already at the tail of `history`,
        # so there's no separate "iteration" counter to recover here — 0 is just a label.
        events, paused = yield from self._run_tool_calls(conversation_id, 0, messages, pending, decisions=decisions, user_id=user_id, assisted_by=assisted_by)
        if paused:
            return
        yield from self._iterate_stream(conversation_id, messages, started_at, 0, 0, start_iteration=1, user_id=user_id, assisted_by=assisted_by)

    # Each confirm_required->auto-deny cycle starts a *fresh* resume_stream(), which resets
    # _iterate_stream's local `iteration` counter back to 1 (see resume_stream below) — so
    # config.max_iterations alone can't bound a stubborn model that keeps re-issuing (denied)
    # risky calls in an unattended run with nobody around to stop clicking "approve". These
    # two caps are the actual backstop for that case.
    UNATTENDED_MAX_AUTO_DENIALS = 5
    UNATTENDED_MAX_SECONDS = 240

    def run_unattended(self, message: str, conversation_id: str | None = None, user_id: str | None = None) -> tuple[AgentResult, int]:
        """Run one turn to completion with nobody present to approve risky tool calls — used
        by the background scheduler (see scheduler_runner.py) for scheduled/unattended runs.

        Any `confirm_required` pause is answered with an automatic deny (never silently
        executed): a scheduled task firing unattended must never be the vector for a
        destructive command just because the model decided to issue one. Returns
        (AgentResult, auto_denied_count) so the caller can note in the notification email
        that some step(s) were skipped for this reason. Bails out early (with whatever partial
        conversation exists so far) if the model keeps retrying denied calls or just runs long
        — see UNATTENDED_MAX_AUTO_DENIALS/UNATTENDED_MAX_SECONDS.
        """
        conversation_id = conversation_id or self.store.new_id()
        run_started_at = time.monotonic()
        stream: Iterator[dict[str, Any]] = self.run_stream(message, conversation_id, user_id=user_id)
        final: dict[str, Any] | None = None
        auto_denied = 0
        bailed_out = False
        while True:
            try:
                event = next(stream)
            except StopIteration:
                break
            if event["type"] == "confirm_required":
                auto_denied += 1
                if (
                    auto_denied > self.UNATTENDED_MAX_AUTO_DENIALS
                    or time.monotonic() - run_started_at > self.UNATTENDED_MAX_SECONDS
                ):
                    bailed_out = True
                    # Still deny this specific call for the record, then stop pulling further
                    # events entirely rather than opening yet another resume_stream().
                    for _ in self._run_tool_calls(
                        conversation_id, 0,
                        [self._system_message()],  # placeholder messages list, unused for a single deny-only call
                        [{"id": event["tool_call_id"], "function": {"name": event["name"], "arguments": "{}"}}],
                        decisions={event["tool_call_id"]: False},
                        user_id=user_id,
                    ):
                        pass  # _run_tool_calls is now a generator — must be drained to actually execute
                    break
                stream = self.resume_stream(conversation_id, {event["tool_call_id"]: False}, user_id=user_id)
                continue
            if event["type"] == "final":
                final = event
        if final is None:
            content = (
                "（已中止：這個排程任務反覆嘗試執行需要人工核准的危險操作，已自動略過但模型仍持續重試，"
                "為避免無止盡循環系統已強制中止本次執行；已完成的部分請見對話紀錄。）"
                if bailed_out else
                "（排程執行未取得任何回覆，請檢查 log）"
            )
            final = {
                "content": content,
                "max_iterations_reached": False,
                "duration_s": round(time.monotonic() - run_started_at, 1),
                "usage": {"input_tokens": 0, "output_tokens": 0},
            }
        result = AgentResult(
            content=final["content"],
            conversation_id=conversation_id,
            max_iterations_reached=final.get("max_iterations_reached", False),
            duration_s=final.get("duration_s", 0.0),
            usage=final.get("usage") or {"input_tokens": 0, "output_tokens": 0},
        )
        return result, auto_denied
