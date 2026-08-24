"""Local tool implementations and their OpenAI function schemas."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlparse
import json
import re
import subprocess

import requests
from bs4 import BeautifulSoup

from .config import Config, settings
from .skills import SKILLS_DIRNAME, list_skills as _list_skills, load_skill as _load_skill
from .mcp_bridge import MCPBridge, seed_default_config
from .config import MCP_SERVERS_PATH
from .schedule_store import ScheduleStore
from .db import ConversationStore
from .memory_store import MemoryStore, VALID_TARGETS
from .rag_store import ChunkStore


# Patterns that identify a shell command as destructive/irreversible enough to warrant a
# human confirmation gate before execution. Deliberately conservative (favors false negatives
# over annoying the user on ordinary commands like ls/python/pip install/git clone).
_RISKY_SHELL_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\brm\s+-\w*[rf]\w*\b", re.I), "刪除檔案／資料夾（rm -rf／-r 之類）"),
    (re.compile(r"\bsudo\b", re.I), "使用 sudo 提權執行"),
    (re.compile(r"\b(shutdown|reboot|halt|poweroff)\b", re.I), "關機／重新開機系統"),
    (re.compile(r"\b(kill\s+-9|killall|pkill)\b", re.I), "強制終止行程"),
    (re.compile(r"\b(chmod|chown)\s+(-R|--recursive)\b", re.I), "遞迴變更檔案權限／擁有者"),
    (re.compile(r"\bmkfs(\.\w+)?\b|\bdd\s+if=", re.I), "格式化／低階寫入磁碟"),
    (re.compile(r">\s*/dev/(sd|nvme|disk|hd)\w*", re.I), "直接寫入磁碟裝置"),
    (re.compile(r"\bgit\s+push\b.*(--force|-f\b)", re.I), "git 強制推送（可能覆蓋遠端歷史）"),
    (re.compile(r"\bgit\s+reset\s+--hard\b", re.I), "git 強制重置（會丟棄未提交的變更）"),
    (re.compile(r"\b(DROP|TRUNCATE)\s+(TABLE|DATABASE|SCHEMA)\b", re.I), "刪除資料表／資料庫（DROP／TRUNCATE）"),
    (re.compile(r"\bDELETE\s+FROM\b", re.I), "刪除資料列（DELETE FROM）"),
    (re.compile(r"(curl|wget)\b[^|;&]*\|\s*(sudo\s+)?(sh|bash|zsh)\b", re.I), "從網路下載後直接執行程式碼"),
    (re.compile(r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}", re.I), "疑似 fork bomb"),
]


def _chunk_text(text: str, chunk_size: int = 800, overlap: int = 120) -> list[str]:
    """Paragraph-aware sliding-window chunking, sized in characters (not tokens) to stay
    dependency-free. Splits on blank lines first so related sentences stay together; any
    paragraph longer than chunk_size gets further split with `overlap` characters of repeated
    context at each boundary, so a fact split across a chunk edge still appears whole in at
    least one chunk."""
    text = (text or "").strip()
    if not text:
        return []
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if not paragraphs:
        paragraphs = [text]
    chunks: list[str] = []
    buffer = ""
    for para in paragraphs:
        candidate = f"{buffer}\n\n{para}" if buffer else para
        if len(candidate) <= chunk_size:
            buffer = candidate
            continue
        if buffer:
            chunks.append(buffer)
            buffer = ""
        if len(para) <= chunk_size:
            buffer = para
            continue
        start = 0
        step = max(chunk_size - overlap, 1)
        while start < len(para):
            end = start + chunk_size
            chunks.append(para[start:end])
            start += step
    if buffer:
        chunks.append(buffer)
    return chunks


def classify_shell_risk(command: str) -> str | None:
    """Return a human-readable risk description if `command` matches a known-destructive
    pattern, else None. Best-effort text matching, not a sandboxed guarantee."""
    for pattern, label in _RISKY_SHELL_PATTERNS:
        if pattern.search(command):
            return label
    return None


TOOLS_SCHEMA = [
    {"type": "function", "function": {"name": "read_file", "description": "Read a UTF-8 text file inside your workspace. Each user has their own private workspace; use the 'shared/' path prefix (e.g. shared/notes.txt) to read/write a folder everyone can see.", "parameters": {"type": "object", "properties": {"path": {"type": "string", "description": "Relative path within the allowed workspace"}}, "required": ["path"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "write_file", "description": "Create or overwrite a UTF-8 text file inside your workspace. Each user has their own private workspace; use the 'shared/' path prefix (e.g. shared/notes.txt) to write into a folder everyone can see.", "parameters": {"type": "object", "properties": {"path": {"type": "string", "description": "Relative path within the allowed workspace"}, "content": {"type": "string", "description": "Complete file content"}}, "required": ["path", "content"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "run_shell", "description": "Run a shell command with timeout. Working directory is your workspace's temp/ (a scratch subfolder) — relative paths in the command resolve there, so files/scripts created without an explicit path land there. Use ../<name> or an explicit workspace-relative path (e.g. via write_file) to save deliverables outside temp; use '../shared/<name>' to reach the folder everyone can see. Returns stdout, stderr, and exit_code. Use cautiously.", "parameters": {"type": "object", "properties": {"command": {"type": "string", "description": "Shell command to execute"}}, "required": ["command"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "web_search", "description": "Search the web through DuckDuckGo HTML and return structured title/link/snippet results.", "parameters": {"type": "object", "properties": {"query": {"type": "string", "description": "Search query"}}, "required": ["query"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "fetch_url", "description": "Fetch an HTTP(S) URL and extract readable plain text from HTML.", "parameters": {"type": "object", "properties": {"url": {"type": "string", "description": "Public http or https URL"}}, "required": ["url"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "db_query", "description": "Query an external MySQL or SQL Server database. Read-only SELECT/CTE queries are allowed by default.", "parameters": {"type": "object", "properties": {"sql": {"type": "string", "description": "SQL query"}, "db_type": {"type": "string", "enum": ["mysql", "mssql"], "description": "Database engine"}}, "required": ["sql", "db_type"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "list_skills", "description": "List reusable skills (SKILL.md playbooks) stored under workspace/skills/. Call this before starting a non-trivial or possibly-recurring task to check whether a documented procedure already exists.", "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}},
    {"type": "function", "function": {"name": "load_skill", "description": "Load the full step-by-step instructions of one skill (by name) so you can follow it. Also lists any resource files (scripts, templates) stored alongside it, which you can then open individually with read_file.", "parameters": {"type": "object", "properties": {"name": {"type": "string", "description": "Skill name or directory name, from list_skills"}}, "required": ["name"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "schedule_task", "description": "設定一則排程任務。到了指定時間，系統會自動用 message 的內容開一個新對話執行一次（就像使用者重新交代了這件事），執行完的結果會記錄下來；若有填 notify_email 還會寄信通知結果。不填 date 代表每天固定時間重複執行；填 date（YYYY-MM-DD，必須是今天或未來）代表只在那個時間點觸發一次，執行後自動失效。注意：排程執行時沒有真人在場，任何原本需要人工核准的危險操作（例如刪除檔案、DROP TABLE）屆時一律會被自動略過、不會真的執行，結果裡會註明。", "parameters": {"type": "object", "properties": {"message": {"type": "string", "description": "到時間要自動執行的任務內容／指令，寫清楚、完整"}, "time": {"type": "string", "description": "24 小時制 HH:MM"}, "date": {"type": "string", "description": "選填，YYYY-MM-DD；有填代表只執行一次，不填代表每天重複"}, "notify_email": {"type": "string", "description": "選填，執行完把結果寄到這個信箱；不填則只記錄結果、不寄信"}}, "required": ["message", "time"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "list_schedules", "description": "列出目前所有排程任務，包含仍在生效中與已停用／已執行過的一次性排程，含每則的 id、下次執行時間、最近一次執行結果。", "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}},
    {"type": "function", "function": {"name": "cancel_schedule", "description": "取消一則排程任務，使其不再自動觸發。", "parameters": {"type": "object", "properties": {"schedule_id": {"type": "string", "description": "從 list_schedules 取得的排程 id"}}, "required": ["schedule_id"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "update_plan", "description": "建立或更新目前這個任務的步驟清單（plan/checklist），會即時顯示給使用者看目前進度。適合在著手一個有多個步驟、或步驟間有先後依賴的任務前先呼叫一次列出完整計畫；之後每完成一步，就再呼叫一次、把該步驟 status 改成 completed（通常同時把下一步改成 in_progress）。每次呼叫都要帶入「完整」的步驟清單（不是只帶有變動的那幾項），系統會整份覆蓋掉舊的。簡單、一兩步就能做完的任務不需要呼叫這個工具。", "parameters": {"type": "object", "properties": {"steps": {"type": "array", "description": "依執行順序排列的完整步驟清單", "items": {"type": "object", "properties": {"content": {"type": "string", "description": "這一步要做的事，簡短描述"}, "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]}}, "required": ["content", "status"], "additionalProperties": False}}}, "required": ["steps"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "memory_add", "description": "在跨對話的長期記憶裡新增一則筆記，之後每次對話都會自動顯示給你參考，不用使用者重複告訴你。target='memory' 存你自己學到、想長期記住的事（例如系統固定行為、之前踩過的坑）；target='user' 存關於使用者本人的側寫（稱呼、偏好、背景）。內容重複的筆記會被自動略過。", "parameters": {"type": "object", "properties": {"target": {"type": "string", "enum": ["memory", "user"], "description": "筆記類別"}, "content": {"type": "string", "description": "筆記內容，簡潔扼要"}}, "required": ["target", "content"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "memory_replace", "description": "用子字串比對找到一則現有的長期記憶筆記並整則替換內容。old_text 必須唯一比對到一則筆記，比對到多則或找不到都會回傳錯誤。", "parameters": {"type": "object", "properties": {"target": {"type": "string", "enum": ["memory", "user"], "description": "筆記類別"}, "old_text": {"type": "string", "description": "要找的舊內容片段（子字串）"}, "content": {"type": "string", "description": "新的完整內容"}}, "required": ["target", "old_text", "content"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "memory_remove", "description": "用子字串比對找到一則現有的長期記憶筆記並刪除。old_text 必須唯一比對到一則筆記，比對到多則或找不到都會回傳錯誤。", "parameters": {"type": "object", "properties": {"target": {"type": "string", "enum": ["memory", "user"], "description": "筆記類別"}, "old_text": {"type": "string", "description": "要找的內容片段（子字串）"}}, "required": ["target", "old_text"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "memory_list", "description": "列出目前長期記憶裡的所有筆記（可選擇只看某個類別），方便確認目前已經記住哪些內容、或找出要用 memory_replace／memory_remove 處理的既有筆記。", "parameters": {"type": "object", "properties": {"target": {"type": "string", "enum": ["memory", "user"], "description": "選填，只列出這個類別"}}, "additionalProperties": False}}},
    {"type": "function", "function": {"name": "kb_add_document", "description": "把工作目錄裡的一個純文字檔（.txt/.md 等 UTF-8 文字檔）加入知識庫：自動切成多個片段、產生 embedding 向量並索引起來，之後可以用 kb_search 檢索。需要先在設置頁的「連線」分頁設定好 Embedding Base URL／Model，否則會失敗。", "parameters": {"type": "object", "properties": {"path": {"type": "string", "description": "工作目錄內的相對路徑"}, "title": {"type": "string", "description": "選填，這份文件在知識庫中顯示的標題；不填則用檔名"}}, "required": ["path"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "kb_search", "description": "在知識庫中做語意檢索，回傳最相關的內容片段（含來源文件與片段位置），可用來回答需要引用已索引文件內容的問題。若知識庫是空的會回傳提示訊息。", "parameters": {"type": "object", "properties": {"query": {"type": "string", "description": "要查詢的問題或關鍵字"}, "top_k": {"type": "integer", "description": "選填，回傳幾筆最相關的片段，預設 5，最多 20"}}, "required": ["query"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "kb_list_documents", "description": "列出知識庫目前已索引的所有文件（標題、片段數、加入時間），方便確認目前已經有哪些內容可供檢索。", "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}},
    {"type": "function", "function": {"name": "kb_remove_document", "description": "把一份文件連同它所有的片段從知識庫移除，之後 kb_search 就不會再檢索到它。", "parameters": {"type": "object", "properties": {"document_id": {"type": "integer", "description": "從 kb_list_documents 取得的文件 id"}}, "required": ["document_id"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "run_subagent", "description": "把一個獨立、可以自成一體描述清楚的子任務，交給一個暫時的子代理人同步執行完成，並拿回它的最終結果文字。適合：(a) 任務可以拆成幾個彼此獨立、不互相依賴先後順序的子任務，你想分別委派出去各自處理再自己彙整；或 (b) 一段可能會產生大量中間過程／工具呼叫雜訊的探索型子工作（例如爬很多網頁、跑很多次 shell 試錯），先讓子代理人處理完只回傳精簡結論，避免這些過程塞滿你自己的對話紀錄。注意：子代理人看不到目前對話的任何歷史，所以 task 必須寫成自成一體、包含它需要知道的全部背景與明確目標；它只有一輪、無法再往下開子代理人（不支援巢狀），也不能呼叫 schedule_task/update_plan 之類跟主線相關的工具；遇到危險操作會自動跳過不執行；且有步驟與時間上限，超過會回傳目前為止的部分結果。", "parameters": {"type": "object", "properties": {"task": {"type": "string", "description": "自成一體、包含完整背景與明確目標的子任務描述"}}, "required": ["task"], "additionalProperties": False}}},
]


@dataclass
class ToolRegistry:
    config: Config = field(default_factory=lambda: settings)
    # Optional callback (task: str, conversation_id: str|None) -> dict, injected by Agent —
    # lets the run_subagent tool call back into Agent's own LLM-loop logic without
    # ToolRegistry needing to depend on LLMClient/Agent itself.
    subagent_runner: Callable[..., dict[str, Any]] | None = None

    def __post_init__(self) -> None:
        self.config.prepare()
        self._handlers: dict[str, Callable[..., Any]] = {
            "read_file": self.read_file,
            "write_file": self.write_file,
            "run_shell": self.run_shell,
            "web_search": self.web_search,
            "fetch_url": self.fetch_url,
            "db_query": self.db_query,
            "list_skills": self.list_skills,
            "load_skill": self.load_skill,
            "schedule_task": self.schedule_task,
            "list_schedules": self.list_schedules,
            "cancel_schedule": self.cancel_schedule,
            "update_plan": self.update_plan,
            "run_subagent": self.run_subagent,
            "memory_add": self.memory_add,
            "memory_replace": self.memory_replace,
            "memory_remove": self.memory_remove,
            "memory_list": self.memory_list,
            "kb_add_document": self.kb_add_document,
            "kb_search": self.kb_search,
            "kb_list_documents": self.kb_list_documents,
            "kb_remove_document": self.kb_remove_document,
        }
        # Handlers that need to know which conversation triggered them: schedule_task purely
        # for a provenance field on the stored row; update_plan actually needs it to know which
        # conversation's plan to persist (never exposed to the model as an argument it fills in
        # itself — see execute()'s `conversation_id` param).
        self._context_aware = {"schedule_task", "update_plan", "run_subagent"}
        # Handlers that need to know which logged-in user triggered them, for per-user data
        # isolation (multi-user memory/schedule ownership) -- always None when multi-user
        # auth is disabled, which reproduces the old single-shared-bucket behaviour exactly.
        self._user_aware = {
            "memory_add", "memory_replace", "memory_remove", "memory_list",
            "schedule_task", "list_schedules", "cancel_schedule", "run_subagent",
            "read_file", "write_file", "run_shell", "kb_add_document",
        }
        # Handlers whose *creation* of a new record should be tagged with who actually typed
        # it, when that's an admin acting on someone else's behalf (impersonation, see
        # web.py) rather than the person themselves -- lets that person later see in their
        # own UI that a given note/schedule/message wasn't self-authored. Not applied to
        # edits/deletes (those are self-evident) or to read_file/write_file/run_shell (no
        # DB row to tag -- see the multi-user workspace design discussion).
        self._assisted_aware = {"memory_add", "schedule_task"}
        self.schedules = ScheduleStore(self.config.sqlite_path.parent / "schedules.sqlite")
        # Separate ConversationStore handle just for reading/writing the plan column — the
        # Agent owns the "real" one for message history; both point at the same sqlite file.
        self.conversation_store = ConversationStore(self.config.sqlite_path)
        self.memory = MemoryStore(self.config.sqlite_path.parent / "memory.sqlite")
        self.kb = ChunkStore(self.config.sqlite_path.parent / "kb.sqlite")
        seed_default_config(MCP_SERVERS_PATH, self.config.allowed_root)
        self.mcp = MCPBridge(MCP_SERVERS_PATH, self.config.allowed_root)
        self.mcp.start()

    def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        conversation_id: str | None = None,
        user_id: str | None = None,
        assisted_by_email: str | None = None,
    ) -> dict[str, Any]:
        handler = self._handlers.get(name)
        if handler:
            try:
                extra: dict[str, Any] = {}
                if name in self._context_aware:
                    extra["conversation_id"] = conversation_id
                if name in self._user_aware:
                    extra["user_id"] = user_id
                if name in self._assisted_aware:
                    extra["assisted_by_email"] = assisted_by_email
                return {"ok": True, "result": handler(**extra, **arguments)}
            except Exception as exc:
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        if self.mcp.has_tool(name):
            try:
                return {"ok": True, "result": self.mcp.call_tool(name, arguments, timeout=self.config.request_timeout)}
            except Exception as exc:
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        return {"ok": False, "error": f"Unknown tool: {name}"}

    def assess_risk(self, name: str, arguments: dict[str, Any]) -> str | None:
        """Return a human-readable risk description if this call should be paused for a
        human confirmation before execution, else None. Kept intentionally narrow: only
        flags calls that can cause real, hard-to-undo damage (destructive shell commands,
        DB writes when DB_READ_ONLY has been explicitly disabled)."""
        if name == "run_shell":
            return classify_shell_risk(str(arguments.get("command") or ""))
        if name == "db_query":
            sql = str(arguments.get("sql") or "")
            if not self.config.db_read_only and not self._is_read_only(sql):
                return "資料庫寫入／結構變更操作（DB_READ_ONLY 目前已關閉）"
        return None

    def full_schema(self) -> list[dict[str, Any]]:
        """Built-in tools plus every currently-connected MCP server's tools. run_subagent is
        omitted entirely (not just left to error out when called) unless explicitly turned on
        in Settings — default off, since spinning up sub-agents costs extra LLM calls/compute
        that may not be available, and the model shouldn't even be tempted to reach for it."""
        base = TOOLS_SCHEMA if self.config.subagent_enabled else [
            t for t in TOOLS_SCHEMA if t["function"]["name"] != "run_subagent"
        ]
        return base + self.mcp.schema()

    SHARED_DIRNAME = "_shared"
    USERS_DIRNAME = "_users"

    def _admin_id(self) -> str | None:
        # LiteAgent is single-user only -- there is no admin/users concept, so every
        # workspace-scoping check below that branches on "is multi-user mode on" always
        # takes the single-user path (see workspace_root/safe_path/workspace_display_rel).
        return None

    def workspace_root(self, user_id: str | None = None) -> Path:
        """Resolves which physical folder a given user's file/shell tools operate in.

        - user_id is None (multi-user auth disabled, e.g. this app's own local/dev instance):
          the single shared top-level workspace, exactly like before this feature existed.
        - user_id is the admin's own id (their normal, non-impersonating session): also the
          top-level workspace -- deliberately *not* moved into its own subfolder, so existing
          pre-multi-user files stay exactly where they were (no migration needed), and the
          admin incidentally retains full visibility into every subfolder below (_users/*,
          _shared) simply because they all live inside that same top-level tree.
        - any other user_id (a plain user's own session, or an admin *impersonating* someone
          -- see web.py's _current_user_id, which returns the impersonation target's id in
          that case): a private subfolder scoped to just that person, auto-created on first use.
        """
        root = self.config.allowed_root
        if user_id is None or user_id == self._admin_id():
            return root
        user_root = root / self.USERS_DIRNAME / user_id
        user_root.mkdir(parents=True, exist_ok=True)
        (user_root / self.config.temp_dirname).mkdir(parents=True, exist_ok=True)
        return user_root

    def safe_path(self, path: str, user_id: str | None = None) -> Path:
        """Resolve a workspace-relative path and enforce the sandbox boundary. A leading
        'shared/' path segment (e.g. 'shared/notes.txt') is special-cased to always resolve
        inside the one common folder every user can read/write, regardless of whose workspace
        root this call would otherwise use -- see workspace_root()."""
        norm = str(path).replace("\\", "/").strip().lstrip("/")
        # The 'shared/' alias only exists once multi-user mode is actually active (an admin
        # account exists) -- on a single-user deployment (auth disabled, e.g. this app's own
        # local/dev instance) it's just an ordinary relative path with no special meaning,
        # exactly like before this feature existed.
        if self._admin_id() is not None and (norm == "shared" or norm.startswith("shared/")):
            shared_root = (self.config.allowed_root / self.SHARED_DIRNAME).resolve()
            shared_root.mkdir(parents=True, exist_ok=True)
            rel = norm[len("shared"):].lstrip("/")
            candidate = (shared_root / rel).resolve() if rel else shared_root
            if candidate != shared_root and shared_root not in candidate.parents:
                raise PermissionError(f"Path is outside allowed root: {shared_root}")
            return candidate
        root = self.workspace_root(user_id)
        candidate = (root / path).resolve()
        if candidate != root and root not in candidate.parents:
            raise PermissionError(f"Path is outside allowed root: {root}")
        admin_id = self._admin_id()
        if root == self.config.allowed_root and admin_id is not None:
            # Only reachable from the admin's own (non-impersonating) session -- multi-user
            # auth being off entirely (admin_id is None) never hits this, so nothing changes
            # for a single-user deployment. Blocks reaching into another specific user's
            # private subfolder this way; the *only* sanctioned way to operate inside a given
            # user's workspace on their behalf is to actually impersonate them (see web.py's
            # /api/admin/impersonate/*), which resolves a different, scoped `root` here and
            # never hits this branch.
            users_root = (self.config.allowed_root / self.USERS_DIRNAME).resolve()
            if candidate == users_root or users_root in candidate.parents:
                raise PermissionError("這個路徑屬於其他使用者的個人工作區，請先用「代理登入」該使用者，再操作他的工作區。")
        return candidate

    # Backward-compatible alias for existing internal/external callers.
    def _safe_path(self, path: str, user_id: str | None = None) -> Path:
        return self.safe_path(path, user_id)

    def workspace_display_rel(self, target: Path, user_id: str | None = None) -> str:
        """Inverse of safe_path(): the workspace-relative string to show/store/link back for
        an already-resolved absolute path (e.g. 'shared/notes.txt' or 'report.png')."""
        if self._admin_id() is not None:
            shared_root = (self.config.allowed_root / self.SHARED_DIRNAME).resolve()
            if target == shared_root or shared_root in target.parents:
                rel = target.relative_to(shared_root).as_posix()
                return "shared" if rel == "." else f"shared/{rel}"
        root = self.workspace_root(user_id)
        return target.relative_to(root).as_posix()

    def read_file(self, path: str, user_id: str | None = None) -> dict[str, Any]:
        target = self.safe_path(path, user_id)
        data = target.read_bytes()
        binary_magic = (b"%PDF-", b"\x89PNG\r\n\x1a\n", b"GIF87a", b"GIF89a", b"PK\x03\x04", b"\xff\xd8\xff")
        if b"\x00" in data[:8192] or data.startswith(binary_magic):
            raise ValueError("此檔案看起來不是純文字檔，目前 read_file 工具無法解析二進位格式(如 PDF/圖片)，僅支援文字類檔案")
        try:
            content = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("此檔案看起來不是純文字檔，目前 read_file 工具無法解析二進位格式(如 PDF/圖片)，僅支援文字類檔案") from exc
        return {"path": self.workspace_display_rel(target, user_id), "content": content}

    def write_file(self, path: str, content: str, user_id: str | None = None) -> dict[str, Any]:
        target = self.safe_path(path, user_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return {"path": self.workspace_display_rel(target, user_id), "bytes": len(content.encode("utf-8"))}

    def _skills_root(self) -> Path:
        return self.config.allowed_root / SKILLS_DIRNAME

    def list_skills(self) -> list[dict[str, str]]:
        return _list_skills(self._skills_root())

    def load_skill(self, name: str) -> dict[str, Any]:
        return _load_skill(self._skills_root(), name, self.config.allowed_root)

    def schedule_task(
        self,
        message: str,
        time: str,
        date: str | None = None,
        notify_email: str | None = None,
        conversation_id: str | None = None,
        user_id: str | None = None,
        assisted_by_email: str | None = None,
    ) -> dict[str, Any]:
        notify_email = notify_email or (self.config.default_notify_email or None)
        row = self.schedules.create(
            message=message, time=time, date=date, notify_email=notify_email,
            conversation_id=conversation_id, owner_id=user_id, assisted_by_email=assisted_by_email,
        )
        return {
            "schedule_id": row["id"],
            "next_run_at": row["next_run_at"],
            "repeat_daily": row["date"] is None,
            "notify_email": row["notify_email"],
        }

    def list_schedules(self, user_id: str | None = None) -> list[dict[str, Any]]:
        # A None user_id means "auth disabled" -- see execute()'s user_id plumbing -- in
        # which case every schedule is visible, matching pre-multi-user behaviour.
        return self.schedules.list_all(owner_id=user_id)

    def cancel_schedule(self, schedule_id: str, user_id: str | None = None) -> dict[str, Any]:
        row = self.schedules.get(schedule_id)
        if row and user_id is not None and row.get("owner_id") not in (None, user_id):
            raise ValueError("這則排程不是你建立的，無法取消。")
        if not self.schedules.cancel(schedule_id):
            raise ValueError("找不到這個排程 id，或它已經被取消／已失效。")
        return {"cancelled": schedule_id}

    _PLAN_STATUSES = {"pending", "in_progress", "completed"}

    def update_plan(self, steps: list[dict[str, Any]], conversation_id: str | None = None) -> dict[str, Any]:
        if not conversation_id:
            raise ValueError("update_plan 只能在對話進行中呼叫")
        if not isinstance(steps, list) or not steps:
            raise ValueError("steps 不能是空的，至少要有一個步驟；任務結束不需要的話，直接不要呼叫這個工具即可")
        cleaned: list[dict[str, Any]] = []
        for i, step in enumerate(steps):
            if not isinstance(step, dict):
                raise ValueError(f"第 {i + 1} 個步驟格式不對，必須是物件")
            content = str(step.get("content") or "").strip()
            status = step.get("status") or "pending"
            if not content:
                raise ValueError(f"第 {i + 1} 個步驟缺少 content")
            if status not in self._PLAN_STATUSES:
                raise ValueError(f"第 {i + 1} 個步驟的 status 不合法：{status}（只能是 pending/in_progress/completed）")
            cleaned.append({"content": content, "status": status})
        self.conversation_store.set_plan(conversation_id, cleaned)
        return {"steps": cleaned}

    def run_subagent(self, task: str, conversation_id: str | None = None, user_id: str | None = None) -> dict[str, Any]:
        if not self.config.subagent_enabled:
            raise RuntimeError("Sub-agent 功能目前在設置中關閉，請先到「設置 → Sub-agent」開啟開關")
        if not str(task or "").strip():
            raise ValueError("task 不能是空的")
        if not self.subagent_runner:
            raise RuntimeError("Sub-agent 功能未啟用")
        return self.subagent_runner(task, conversation_id=conversation_id, user_id=user_id)

    @staticmethod
    def _validate_memory_target(target: str) -> str:
        target = str(target or "").strip().lower()
        if target not in VALID_TARGETS:
            raise ValueError(f"target 必須是 {' 或 '.join(VALID_TARGETS)} 其中之一")
        return target

    def memory_add(self, target: str, content: str, user_id: str | None = None, assisted_by_email: str | None = None) -> dict[str, Any]:
        target = self._validate_memory_target(target)
        content = str(content or "").strip()
        if not content:
            raise ValueError("content 不能是空的")
        budget = self.config.memory_char_budget
        current = self.memory.total_chars(target, user_id)
        if current + len(content) > budget:
            raise ValueError(
                f"這則筆記會讓「{target}」記憶超過字數上限（目前 {current}/{budget} 字元，"
                f"這則筆記 {len(content)} 字元）。請先用 memory_replace 把既有筆記精簡，"
                "或用 memory_remove 刪除不需要的筆記騰出空間，再重新新增。"
            )
        return self.memory.add(target, content, user_id, assisted_by_email)

    def _find_unique_memory_note(self, target: str, old_text: str, user_id: str | None = None) -> dict[str, Any]:
        old_text = str(old_text or "").strip()
        if not old_text:
            raise ValueError("old_text 不能是空的")
        matches = self.memory.find_by_substring(target, old_text, user_id)
        if not matches:
            raise ValueError("找不到符合的筆記，請確認 old_text 是既有筆記內容的子字串")
        if len(matches) > 1:
            raise ValueError(f"比對到 {len(matches)} 則筆記，請提供更明確、能唯一比對到一則的片段")
        return matches[0]

    def memory_replace(self, target: str, old_text: str, content: str, user_id: str | None = None) -> dict[str, Any]:
        target = self._validate_memory_target(target)
        content = str(content or "").strip()
        if not content:
            raise ValueError("content 不能是空的")
        note = self._find_unique_memory_note(target, old_text, user_id)
        budget = self.config.memory_char_budget
        current = self.memory.total_chars(target, user_id) - len(note["content"])
        if current + len(content) > budget:
            raise ValueError(
                f"替換後會讓「{target}」記憶超過字數上限（目前其餘筆記共 {current}/{budget} 字元，"
                f"新內容 {len(content)} 字元），請再精簡一些。"
            )
        self.memory.replace(note["id"], content)
        return {"id": note["id"]}

    def memory_remove(self, target: str, old_text: str, user_id: str | None = None) -> dict[str, Any]:
        target = self._validate_memory_target(target)
        note = self._find_unique_memory_note(target, old_text, user_id)
        self.memory.remove(note["id"])
        return {"removed_id": note["id"]}

    def memory_list(self, target: str | None = None, user_id: str | None = None) -> list[dict[str, Any]]:
        if target:
            target = self._validate_memory_target(target)
        return self.memory.list(target, user_id)

    # ---- Knowledge base / RAG -------------------------------------------------------
    # Phase 1 (MVP): UTF-8 plain-text files only (.txt/.md/...); pdf/docx/pptx parsing is a
    # planned Phase 2. One global knowledge base (LiteAgent is single-user, see _admin_id).

    def _kb_embed(self, texts: list[str]) -> list[list[float]]:
        from .llm_client import LLMClient
        client = LLMClient(self.config)
        batch_size = 64
        vectors: list[list[float]] = []
        for i in range(0, len(texts), batch_size):
            vectors.extend(client.embed(texts[i : i + batch_size]))
        return vectors

    def kb_add_document(self, path: str, title: str | None = None, user_id: str | None = None) -> dict[str, Any]:
        target = self.safe_path(path, user_id)
        if not target.exists() or not target.is_file():
            raise ValueError(f"找不到檔案：{path}")
        data = target.read_bytes()
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(
                "目前知識庫僅支援 UTF-8 純文字檔（.txt/.md 等），此檔案看起來是二進位或其他編碼格式"
            ) from exc
        chunks = _chunk_text(text, self.config.kb_chunk_size, self.config.kb_chunk_overlap)
        if not chunks:
            raise ValueError("此檔案沒有可索引的文字內容")
        vectors = self._kb_embed(chunks)
        doc_title = (title or target.name).strip() or target.name
        return self.kb.add_document(doc_title, self.workspace_display_rel(target, user_id), chunks, vectors)

    def kb_search(self, query: str, top_k: int = 5, user_id: str | None = None) -> dict[str, Any]:
        query = str(query or "").strip()
        if not query:
            raise ValueError("query 不能是空的")
        if self.kb.is_empty():
            return {"results": [], "note": "知識庫目前是空的，請先用 kb_add_document 加入文件"}
        vector = self._kb_embed([query])[0]
        top_k = max(1, min(int(top_k or 5), 20))
        return {"results": self.kb.search(vector, top_k)}

    def kb_list_documents(self) -> list[dict[str, Any]]:
        return self.kb.list_documents()

    def kb_remove_document(self, document_id: int) -> dict[str, bool]:
        removed = self.kb.remove_document(int(document_id))
        if not removed:
            raise ValueError(f"找不到文件 id={document_id}")
        return {"removed": True}

    @staticmethod
    def _shell_text(value: Any) -> str:
        # subprocess.run(text=True) normally decodes stdout/stderr to str, but when a
        # TimeoutExpired is raised, the partial output captured on the exception can
        # still be raw bytes (a CPython quirk) — decode defensively so it's always
        # JSON-serializable downstream (SSE events / DB storage).
        if value is None:
            return ""
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return value

    def run_shell(self, command: str, user_id: str | None = None) -> dict[str, Any]:
        temp_dir = self.workspace_root(user_id) / self.config.temp_dirname
        temp_dir.mkdir(parents=True, exist_ok=True)
        try:
            proc = subprocess.run(
                command,
                cwd=temp_dir,
                shell=True,
                text=True,
                capture_output=True,
                timeout=self.config.shell_timeout,
            )
            return {"stdout": self._shell_text(proc.stdout), "stderr": self._shell_text(proc.stderr), "exit_code": proc.returncode}
        except subprocess.TimeoutExpired as exc:
            return {
                "stdout": self._shell_text(exc.stdout),
                "stderr": self._shell_text(exc.stderr),
                "exit_code": None,
                "timed_out": True,
                "timeout_seconds": self.config.shell_timeout,
            }

    def web_search(self, query: str) -> list[dict[str, str]]:
        response = requests.post(
            "https://html.duckduckgo.com/html/",
            data={"q": query},
            headers={"User-Agent": "Mozilla/5.0 LiteAgent/1.0"},
            timeout=self.config.request_timeout,
        )
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        results = []
        for result in soup.select(".result"):
            link = result.select_one(".result__a")
            if not link:
                continue
            href = link.get("href", "")
            parsed = urlparse(href)
            if "uddg" in parse_qs(parsed.query):
                href = unquote(parse_qs(parsed.query)["uddg"][0])
            snippet = result.select_one(".result__snippet")
            results.append({"title": link.get_text(" ", strip=True), "url": href, "snippet": snippet.get_text(" ", strip=True) if snippet else ""})
            if len(results) >= self.config.web_search_results:
                break
        return results

    def fetch_url(self, url: str) -> dict[str, Any]:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Only absolute http/https URLs are allowed")
        response = requests.get(url, headers={"User-Agent": "Mozilla/5.0 LiteAgent/1.0"}, timeout=self.config.request_timeout)
        response.raise_for_status()
        content_type = response.headers.get("content-type", "")
        if "html" in content_type or "<html" in response.text[:500].lower():
            soup = BeautifulSoup(response.text, "html.parser")
            for node in soup(["script", "style", "noscript", "svg"]):
                node.decompose()
            text = "\n".join(line.strip() for line in soup.get_text("\n").splitlines() if line.strip())
            title = soup.title.get_text(" ", strip=True) if soup.title else ""
        else:
            text, title = response.text, ""
        truncated = len(text) > self.config.fetch_max_chars
        return {"url": response.url, "title": title, "content": text[:self.config.fetch_max_chars], "truncated": truncated}

    @staticmethod
    def _is_read_only(sql: str) -> bool:
        cleaned = re.sub(r"/\*.*?\*/|--[^\n]*", "", sql, flags=re.S).strip()
        if not re.match(r"^(SELECT|WITH|SHOW|DESCRIBE|DESC|EXPLAIN)\b", cleaned, re.I):
            return False
        dangerous = r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|REPLACE|MERGE|GRANT|REVOKE|EXEC(?:UTE)?|CALL|INTO\s+OUTFILE|LOAD\s+DATA)\b"
        return re.search(dangerous, cleaned, re.I) is None

    def db_query(self, sql: str, db_type: str) -> dict[str, Any]:
        if self.config.db_read_only and not self._is_read_only(sql):
            raise PermissionError("DB_READ_ONLY is enabled; only SELECT/read-only queries are allowed")
        if db_type == "mysql":
            return self._mysql(sql)
        if db_type == "mssql":
            return self._mssql(sql)
        raise ValueError("db_type must be 'mysql' or 'mssql'")

    @staticmethod
    def _required_env(names: list[str]) -> dict[str, str]:
        import os
        missing = [name for name in names if not os.getenv(name)]
        if missing:
            raise RuntimeError("Missing database environment variables: " + ", ".join(missing))
        return {name: os.environ[name] for name in names}

    def _mysql(self, sql: str) -> dict[str, Any]:
        import os
        import pymysql
        env = self._required_env(["MYSQL_HOST", "MYSQL_USER", "MYSQL_PASSWORD", "MYSQL_DATABASE"])
        conn = pymysql.connect(host=env["MYSQL_HOST"], port=int(os.getenv("MYSQL_PORT", "3306")), user=env["MYSQL_USER"], password=env["MYSQL_PASSWORD"], database=env["MYSQL_DATABASE"], cursorclass=pymysql.cursors.DictCursor, connect_timeout=10)
        try:
            with conn.cursor() as cursor:
                cursor.execute(sql)
                rows = cursor.fetchall() if cursor.description else []
                if not self.config.db_read_only:
                    conn.commit()
                return {"rows": rows, "row_count": cursor.rowcount}
        finally:
            conn.close()

    def _mssql(self, sql: str) -> dict[str, Any]:
        import os
        import pymssql
        env = self._required_env(["MSSQL_HOST", "MSSQL_USER", "MSSQL_PASSWORD", "MSSQL_DATABASE"])
        conn = pymssql.connect(server=env["MSSQL_HOST"], port=int(os.getenv("MSSQL_PORT", "1433")), user=env["MSSQL_USER"], password=env["MSSQL_PASSWORD"], database=env["MSSQL_DATABASE"], login_timeout=10, timeout=30, as_dict=True)
        try:
            cursor = conn.cursor()
            cursor.execute(sql)
            rows = cursor.fetchall() if cursor.description else []
            if not self.config.db_read_only:
                conn.commit()
            return {"rows": rows, "row_count": cursor.rowcount}
        finally:
            conn.close()


def json_result(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)
