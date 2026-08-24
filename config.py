"""Centralized configuration loaded from environment variables and .env."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import json
import os

from dotenv import load_dotenv

PACKAGE_DIR = Path(__file__).resolve().parent
load_dotenv(PACKAGE_DIR / ".env")
load_dotenv()

RUNTIME_SETTINGS_PATH = PACKAGE_DIR / "data" / "runtime_settings.json"
MCP_SERVERS_PATH = PACKAGE_DIR / "data" / "mcp_servers.json"


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    return default if value is None else value.strip().lower() in {"1", "true", "yes", "on"}


def _default_base_url() -> str:
    """Bare OpenAI-compatible base URL, e.g. http://host:port/v1 (no trailing path)."""
    explicit = os.getenv("LITEAGENT_BASE_URL")
    if explicit:
        return explicit.rstrip("/")
    # Back-compat: older .env files stored the full chat/completions URL in LITEAGENT_API_BASE.
    legacy = os.getenv("LITEAGENT_API_BASE", "http://localhost:11434/v1/chat/completions")
    if legacy.endswith("/chat/completions"):
        legacy = legacy[: -len("/chat/completions")]
    return legacy.rstrip("/")


def _load_runtime_overrides() -> dict:
    try:
        return json.loads(RUNTIME_SETTINGS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_runtime_overrides(patch: dict) -> dict:
    """Merge `patch` into the persisted runtime_settings.json (instead of clobbering keys
    written by a different settings section, e.g. LLM vs SMTP) and return the merged dict."""
    overrides = _load_runtime_overrides()
    overrides.update(patch)
    RUNTIME_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    RUNTIME_SETTINGS_PATH.write_text(json.dumps(overrides, ensure_ascii=False, indent=2), encoding="utf-8")
    return overrides


@dataclass
class Config:
    base_url: str = field(default_factory=_default_base_url)
    api_key: str = field(default_factory=lambda: os.getenv("LITEAGENT_API_KEY", "not-required"))
    model: str = field(default_factory=lambda: os.getenv("LITEAGENT_MODEL", "local-model"))
    max_iterations: int = field(default_factory=lambda: int(os.getenv("LITEAGENT_MAX_ITERATIONS", "100")))
    request_timeout: int = field(default_factory=lambda: int(os.getenv("LITEAGENT_REQUEST_TIMEOUT", "300")))
    shell_timeout: int = field(default_factory=lambda: int(os.getenv("LITEAGENT_SHELL_TIMEOUT", "30")))
    allowed_root: Path = field(default_factory=lambda: Path(os.getenv("LITEAGENT_ALLOWED_ROOT", str(PACKAGE_DIR / "workspace"))).expanduser().resolve())
    temp_dirname: str = field(default_factory=lambda: os.getenv("LITEAGENT_TEMP_DIRNAME", "temp"))
    sqlite_path: Path = field(default_factory=lambda: Path(os.getenv("LITEAGENT_SQLITE_PATH", str(PACKAGE_DIR / "data" / "conversations.sqlite"))).expanduser().resolve())
    log_path: Path = field(default_factory=lambda: Path(os.getenv("LITEAGENT_LOG_PATH", str(PACKAGE_DIR / "data" / "agent.log"))).expanduser().resolve())
    db_read_only: bool = field(default_factory=lambda: _bool("DB_READ_ONLY", True))
    web_search_results: int = field(default_factory=lambda: int(os.getenv("LITEAGENT_WEB_SEARCH_RESULTS", "5")))
    fetch_max_chars: int = field(default_factory=lambda: int(os.getenv("LITEAGENT_FETCH_MAX_CHARS", "50000")))
    system_prompt: str = field(default_factory=lambda: os.getenv("LITEAGENT_SYSTEM_PROMPT", DEFAULT_SYSTEM_PROMPT))
    context_window_tokens: int = field(default_factory=lambda: int(os.getenv("LITEAGENT_CONTEXT_WINDOW_TOKENS", "128000")))
    compact_trigger_ratio: float = field(default_factory=lambda: float(os.getenv("LITEAGENT_COMPACT_TRIGGER_RATIO", "0.65")))
    compact_keep_recent_turns: int = field(default_factory=lambda: int(os.getenv("LITEAGENT_COMPACT_KEEP_TURNS", "3")))
    smtp_host: str = field(default_factory=lambda: os.getenv("LITEAGENT_SMTP_HOST", ""))
    smtp_port: int = field(default_factory=lambda: int(os.getenv("LITEAGENT_SMTP_PORT", "587")))
    smtp_user: str = field(default_factory=lambda: os.getenv("LITEAGENT_SMTP_USER", ""))
    smtp_password: str = field(default_factory=lambda: os.getenv("LITEAGENT_SMTP_PASSWORD", ""))
    smtp_from: str = field(default_factory=lambda: os.getenv("LITEAGENT_SMTP_FROM", ""))
    default_notify_email: str = field(default_factory=lambda: os.getenv("LITEAGENT_DEFAULT_NOTIFY_EMAIL", ""))
    subagent_max_iterations: int = field(default_factory=lambda: int(os.getenv("LITEAGENT_SUBAGENT_MAX_ITERATIONS", "8")))
    subagent_max_seconds: int = field(default_factory=lambda: int(os.getenv("LITEAGENT_SUBAGENT_MAX_SECONDS", "120")))
    subagent_max_per_turn: int = field(default_factory=lambda: int(os.getenv("LITEAGENT_SUBAGENT_MAX_PER_TURN", "3")))
    subagent_enabled: bool = field(default_factory=lambda: _bool("LITEAGENT_SUBAGENT_ENABLED", False))
    subagent_concurrency: str = field(default_factory=lambda: os.getenv("LITEAGENT_SUBAGENT_CONCURRENCY", "sequential"))
    memory_char_budget: int = field(default_factory=lambda: int(os.getenv("LITEAGENT_MEMORY_CHAR_BUDGET", "6000")))
    embedding_base_url: str = field(default_factory=lambda: os.getenv("LITEAGENT_EMBEDDING_BASE_URL", ""))
    embedding_api_key: str = field(default_factory=lambda: os.getenv("LITEAGENT_EMBEDDING_API_KEY", ""))
    embedding_model: str = field(default_factory=lambda: os.getenv("LITEAGENT_EMBEDDING_MODEL", ""))
    kb_chunk_size: int = field(default_factory=lambda: int(os.getenv("LITEAGENT_KB_CHUNK_SIZE", "800")))
    kb_chunk_overlap: int = field(default_factory=lambda: int(os.getenv("LITEAGENT_KB_CHUNK_OVERLAP", "120")))

    def __post_init__(self) -> None:
        overrides = _load_runtime_overrides()
        if overrides.get("base_url"):
            self.base_url = str(overrides["base_url"]).rstrip("/")
        if overrides.get("model"):
            self.model = str(overrides["model"])
        if overrides.get("api_key") is not None:
            self.api_key = str(overrides["api_key"])
        if overrides.get("system_prompt"):
            self.system_prompt = str(overrides["system_prompt"])
        if overrides.get("context_window_tokens"):
            try:
                self.context_window_tokens = int(overrides["context_window_tokens"])
            except (TypeError, ValueError):
                pass
        if overrides.get("request_timeout"):
            try:
                self.request_timeout = int(overrides["request_timeout"])
            except (TypeError, ValueError):
                pass
        if overrides.get("max_iterations"):
            try:
                self.max_iterations = int(overrides["max_iterations"])
            except (TypeError, ValueError):
                pass
        if overrides.get("shell_timeout"):
            try:
                self.shell_timeout = int(overrides["shell_timeout"])
            except (TypeError, ValueError):
                pass
        if overrides.get("web_search_results"):
            try:
                self.web_search_results = int(overrides["web_search_results"])
            except (TypeError, ValueError):
                pass
        if overrides.get("fetch_max_chars"):
            try:
                self.fetch_max_chars = int(overrides["fetch_max_chars"])
            except (TypeError, ValueError):
                pass
        if overrides.get("compact_trigger_ratio"):
            try:
                self.compact_trigger_ratio = float(overrides["compact_trigger_ratio"])
            except (TypeError, ValueError):
                pass
        if overrides.get("compact_keep_recent_turns") is not None:
            try:
                self.compact_keep_recent_turns = int(overrides["compact_keep_recent_turns"])
            except (TypeError, ValueError):
                pass
        if overrides.get("smtp_host") is not None:
            self.smtp_host = str(overrides["smtp_host"])
        if overrides.get("smtp_port"):
            try:
                self.smtp_port = int(overrides["smtp_port"])
            except (TypeError, ValueError):
                pass
        if overrides.get("smtp_user") is not None:
            self.smtp_user = str(overrides["smtp_user"])
        if overrides.get("smtp_password") is not None:
            self.smtp_password = str(overrides["smtp_password"])
        if overrides.get("smtp_from") is not None:
            self.smtp_from = str(overrides["smtp_from"])
        if overrides.get("default_notify_email") is not None:
            self.default_notify_email = str(overrides["default_notify_email"])
        for _key in ("subagent_max_iterations", "subagent_max_seconds", "subagent_max_per_turn"):
            if overrides.get(_key):
                try:
                    setattr(self, _key, int(overrides[_key]))
                except (TypeError, ValueError):
                    pass
        if overrides.get("subagent_enabled") is not None:
            self.subagent_enabled = bool(overrides["subagent_enabled"])
        if overrides.get("subagent_concurrency") in ("sequential", "parallel"):
            self.subagent_concurrency = str(overrides["subagent_concurrency"])
        if overrides.get("memory_char_budget"):
            try:
                self.memory_char_budget = int(overrides["memory_char_budget"])
            except (TypeError, ValueError):
                pass
        if overrides.get("embedding_base_url") is not None:
            self.embedding_base_url = str(overrides["embedding_base_url"]).rstrip("/")
        if overrides.get("embedding_api_key") is not None:
            self.embedding_api_key = str(overrides["embedding_api_key"])
        if overrides.get("embedding_model") is not None:
            self.embedding_model = str(overrides["embedding_model"])
        if overrides.get("kb_chunk_size"):
            try:
                self.kb_chunk_size = int(overrides["kb_chunk_size"])
            except (TypeError, ValueError):
                pass
        if overrides.get("kb_chunk_overlap") is not None:
            try:
                self.kb_chunk_overlap = int(overrides["kb_chunk_overlap"])
            except (TypeError, ValueError):
                pass

    @property
    def temp_dir(self) -> Path:
        """Scratch/working directory for shell commands, kept inside the workspace (workspace/temp by default)."""
        return self.allowed_root / self.temp_dirname

    @property
    def chat_completions_url(self) -> str:
        return self.base_url.rstrip("/") + "/chat/completions"

    @property
    def models_url(self) -> str:
        return self.base_url.rstrip("/") + "/models"

    def update_llm(
        self,
        base_url: str | None = None,
        model: str | None = None,
        system_prompt: str | None = None,
        context_window_tokens: int | None = None,
        api_key: str | None = None,
        request_timeout: int | None = None,
    ) -> None:
        """Apply new LLM settings live, and persist them so they survive a restart. `api_key`
        left as None keeps the previously-saved key (mirrors the SMTP password UX of never
        echoing a saved secret back, but still letting other fields be edited). `request_timeout`
        governs every outbound HTTP call this process makes (chat/embeddings/web fetch/MCP tool
        calls -- see llm_client.py and tools.py, which all read config.request_timeout live at
        call time), so raising it here takes effect on the very next request, no restart needed."""
        if base_url:
            self.base_url = base_url.rstrip("/")
        if model:
            self.model = model
        if system_prompt:
            self.system_prompt = system_prompt
        if context_window_tokens:
            self.context_window_tokens = int(context_window_tokens)
        if api_key:
            self.api_key = api_key
        if request_timeout:
            self.request_timeout = int(request_timeout)
        _save_runtime_overrides({
            "base_url": self.base_url,
            "model": self.model,
            "system_prompt": self.system_prompt,
            "context_window_tokens": self.context_window_tokens,
            "api_key": self.api_key,
            "request_timeout": self.request_timeout,
        })

    def update_behavior(
        self,
        max_iterations: int | None = None,
        shell_timeout: int | None = None,
        web_search_results: int | None = None,
        fetch_max_chars: int | None = None,
        memory_char_budget: int | None = None,
        compact_trigger_ratio: float | None = None,
        compact_keep_recent_turns: int | None = None,
    ) -> None:
        """Apply new agent-behavior limits live, and persist them so they survive a restart.
        Grouped together because they're all "how the agent loop / tools behave" knobs that
        were previously only settable via .env (a restart required); each is read live at the
        call site (agent.py / tools.py) so changing it here takes effect on the next request."""
        if max_iterations:
            self.max_iterations = int(max_iterations)
        if shell_timeout:
            self.shell_timeout = int(shell_timeout)
        if web_search_results:
            self.web_search_results = int(web_search_results)
        if fetch_max_chars:
            self.fetch_max_chars = int(fetch_max_chars)
        if memory_char_budget:
            self.memory_char_budget = int(memory_char_budget)
        if compact_trigger_ratio:
            self.compact_trigger_ratio = float(compact_trigger_ratio)
        if compact_keep_recent_turns is not None:
            self.compact_keep_recent_turns = int(compact_keep_recent_turns)
        _save_runtime_overrides({
            "max_iterations": self.max_iterations,
            "shell_timeout": self.shell_timeout,
            "web_search_results": self.web_search_results,
            "fetch_max_chars": self.fetch_max_chars,
            "memory_char_budget": self.memory_char_budget,
            "compact_trigger_ratio": self.compact_trigger_ratio,
            "compact_keep_recent_turns": self.compact_keep_recent_turns,
        })

    def update_smtp(
        self,
        host: str | None = None,
        port: int | None = None,
        user: str | None = None,
        password: str | None = None,
        from_addr: str | None = None,
        default_notify_email: str | None = None,
    ) -> None:
        """Apply new SMTP settings live, and persist them so they survive a restart. `password`
        left as None keeps the previously-saved password (mirrors the settings-panel UX of
        never echoing a saved secret back, but still letting other fields be edited)."""
        if host is not None:
            self.smtp_host = host.strip()
        if port:
            self.smtp_port = int(port)
        if user is not None:
            self.smtp_user = user.strip()
        if password:
            self.smtp_password = password
        if from_addr is not None:
            self.smtp_from = from_addr.strip()
        if default_notify_email is not None:
            self.default_notify_email = default_notify_email.strip()
        _save_runtime_overrides({
            "smtp_host": self.smtp_host,
            "smtp_port": self.smtp_port,
            "smtp_user": self.smtp_user,
            "smtp_password": self.smtp_password,
            "smtp_from": self.smtp_from,
            "default_notify_email": self.default_notify_email,
        })

    def update_subagent(
        self,
        max_iterations: int | None = None,
        max_seconds: int | None = None,
        max_per_turn: int | None = None,
        enabled: bool | None = None,
        concurrency: str | None = None,
    ) -> None:
        """Apply new sub-agent limits live, and persist them so they survive a restart."""
        if max_iterations:
            self.subagent_max_iterations = int(max_iterations)
        if max_seconds:
            self.subagent_max_seconds = int(max_seconds)
        if max_per_turn:
            self.subagent_max_per_turn = int(max_per_turn)
        if enabled is not None:
            self.subagent_enabled = bool(enabled)
        if concurrency in ("sequential", "parallel"):
            self.subagent_concurrency = concurrency
        _save_runtime_overrides({
            "subagent_max_iterations": self.subagent_max_iterations,
            "subagent_max_seconds": self.subagent_max_seconds,
            "subagent_max_per_turn": self.subagent_max_per_turn,
            "subagent_enabled": self.subagent_enabled,
            "subagent_concurrency": self.subagent_concurrency,
        })

    def update_embedding(
        self,
        base_url: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
    ) -> None:
        """Apply new embedding-provider settings live, and persist them so they survive a
        restart. Mirrors update_llm's UX: `api_key` left as None keeps the previously-saved
        key. Deliberately kept independent from the main chat LLM's base_url/api_key/model —
        the embedding model powering future knowledge-base retrieval may live on a different
        endpoint or use a different model than the chat model."""
        if base_url is not None:
            self.embedding_base_url = base_url.rstrip("/")
        if model is not None:
            self.embedding_model = model
        if api_key:
            self.embedding_api_key = api_key
        _save_runtime_overrides({
            "embedding_base_url": self.embedding_base_url,
            "embedding_model": self.embedding_model,
            "embedding_api_key": self.embedding_api_key,
        })

    def update_kb(
        self,
        chunk_size: int | None = None,
        chunk_overlap: int | None = None,
    ) -> None:
        """Apply new chunking parameters live, and persist them so they survive a restart.
        Only affects documents indexed *after* the change -- existing chunks already stored
        in kb.sqlite keep whatever size they were created with."""
        if chunk_size:
            self.kb_chunk_size = int(chunk_size)
        if chunk_overlap is not None:
            self.kb_chunk_overlap = int(chunk_overlap)
        _save_runtime_overrides({
            "kb_chunk_size": self.kb_chunk_size,
            "kb_chunk_overlap": self.kb_chunk_overlap,
        })

    def prepare(self) -> None:
        self.allowed_root.mkdir(parents=True, exist_ok=True)
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)


DEFAULT_SYSTEM_PROMPT = """你是一個直接運行於自架地端模型的自主 Agent，可使用讀寫檔案、執行 shell、查詢資料庫、上網搜尋／讀取網頁、排程任務等工具完成任務；系統也可能另外連接 MCP 伺服器，屆時會取得更多工具，用法比照下述原則。

【工作目錄與檔案規則】
- 檔案與 shell 操作只會在安全工作目錄內進行，run_shell 的工作目錄固定是 workspace/temp。
- 需要寫程式時（轉換腳本、分析程式、一次性工具等），預設一律用 write_file 寫到 workspace/temp 底下，不要直接寫到 workspace 根目錄或其他子資料夾；temp 內容可能隨時被清空，程式本身只是達成目的的手段。
- 只有使用者真正需要保留的「最終成果」（例如報表、圖表、匯出的資料檔、最終文件），才用 write_file 另外明確存到 workspace 根目錄或其他非 temp 的子資料夾；產生這些成果所用到的程式仍然留在 temp，不要一起搬出去。
- 回覆裡如果要附上你產生或使用者上傳的檔案（圖片、報表、匯出檔等）供查看或下載，請用 markdown 連結或圖片語法，網址一律用 /api/files/download/<相對於 workspace 根目錄的路徑>（路徑各段需 URL 編碼），不要只寫裸檔名或相對路徑，否則瀏覽器會找不到檔案而顯示壞圖。例如：![日度趨勢圖](/api/files/download/202501_日度趨勢圖.png)

【工具使用原則】
- 使用工具前先確認參數是否正確，避免不必要的破壞性操作；資料庫查詢預設只允許唯讀 SELECT/CTE。
- 系統對明顯有風險的操作（例如刪除檔案、清空資料）會另外跳出人工確認，不用因此過度猶豫，但也不要主動提出非必要的危險指令。
- 開始一項不簡單、或未來可能重複執行的任務前，先呼叫 list_skills 檢查 workspace/skills/ 底下是否已有現成的 SKILL.md 流程可以直接照做，避免重造輪子。
- 使用者如果提到「以後定期」「每天」「到某個時間點自動做」某件事，主動用 schedule_task 幫忙排好，不用等使用者自己設定；請留意排程執行時沒有真人在場，任何原本需要人工核准的危險操作屆時會被自動略過、不會真的執行。
- 遇到明顯有多個步驟、或步驟之間有先後依賴的任務時，先呼叫 update_plan 列出完整的步驟清單，讓使用者能即時看到進度；之後每完成一步，就再呼叫一次 update_plan，把該步驟 status 改成 completed（通常同時把下一步改成 in_progress）。每次呼叫都要帶入「完整」的步驟清單，不是只帶有變動的那幾項。簡單、一兩步就能做完的任務不需要特地列 plan，避免多此一舉。

【回覆風格】
- 取得足夠資訊後，用清楚、精簡、可直接採取行動的文字回答，不需要交代不必要的思考過程。
- 一律使用繁體中文（正體字、台灣用語，例如「檔案」「軟體」「網路」）回覆，絕對不可以輸出簡體字或大陸用語，即使使用者用簡體中文或英文提問也一樣；下工具（如 write_file／run_shell）產生的檔案內容若也含中文說明文字，同樣要用繁體中文。"""


settings = Config()
