[中文](README.md) | **English** | [日本語](README.ja.md)

# LiteAgent

A native Python agent that talks directly to **any OpenAI-compatible Chat Completions endpoint**
(Ollama, vLLM, LM Studio, or any other locally/self-hosted model). Single-user, no cloud
middleman — your data stays entirely on your own machine. Supports native function calling, a
full tool-use loop, SQLite multi-turn memory, a CLI, a FastAPI web chat UI, and can be packaged
into a standalone macOS desktop app.

> LiteAgent works by simply pointing at a different endpoint — it isn't locked to any specific
> model or vendor. Any local service that speaks the OpenAI Chat Completions format (including
> tools/function calling) can be plugged in.

## Features

- **Agent loop**: a full tool-calling loop built on native function calling, with streaming,
  confirmation prompts for risky actions, and automatic conversation compression (older messages
  get summarized once the context window threshold is exceeded).
- **Built-in tools**: file read/write, shell execution, web search, web page reading,
  MySQL/SQL Server queries (read-only protected), long-term memory notes (persisted across
  conversations), and a task list (planning).
- **MCP client**: connect to any number of external MCP servers and merge their tools into the
  same toolset.
- **Sub-agents**: optional (disabled by default) — lets the main agent delegate subtasks to
  independent sub-agents; multiple sub-agents in the same turn can run either "sequential"
  (one after another) or "parallel" (true concurrency via a thread pool).
- **Knowledge base / RAG**: once a separate embedding endpoint/model is configured, use
  `kb_add_document` to chunk, embed, and index text files (.txt/.md) from the workspace, then use
  `kb_search` for semantic retrieval with source citations. Vectors are stored locally in SQLite
  (`kb.sqlite`) with brute-force cosine similarity — no separate vector database service needed.
  `kb_list_documents` / `kb_remove_document` manage indexed files; currently UTF-8 plain text only,
  with pdf/docx/pptx parsing planned for a future release.
- **Scheduled tasks**: cron-like recurring or one-time tasks, with optional email notification on
  completion (requires your own SMTP setup).
- **Skills mechanism**: reads `workspace/skills/<dir>/SKILL.md`-formatted skill packages, letting
  the agent load extra operating instructions on demand.
- **Desktop app**: `packaging/macos/build_macos_app.sh` packages the whole service into a
  standalone, double-clickable macOS `.app` (with its own built-in FastAPI service bound to
  `127.0.0.1`, not exposed externally).

LiteAgent is deliberately kept **single-user**: no login, no accounts, no multi-user
collaboration — just an agent that runs on your own machine.

## Installation

### One-line install (recommended)

On macOS / Linux, just open a terminal and paste this one line — it's fully automated (it can
detect and optionally auto-install Ollama plus pull a default model):

```bash
curl -fsSL https://raw.githubusercontent.com/kevinlcw/liteagent/main/install.sh | bash -s -- --yes
```

Omit `-- --yes` for interactive mode, where each key step (install Ollama? download a model?
start it now?) will ask for confirmation first. After installation, you'll get `~/LiteAgent/`
in your home directory (override the path with the `LITEAGENT_INSTALL_DIR` env var), containing:

- `~/LiteAgent/start.sh` — run this any time you want to start LiteAgent
- `~/LiteAgent/LiteAgent.command` (macOS only) — **double-click** this in Finder to start it,
  no terminal needed

The script itself also lives in the repo at [`install.sh`](install.sh) — if you've already
`git clone`d the repo manually, running `bash liteagent/install.sh` does the same thing (won't
re-download). If you already have your own local model service and don't want the script to
touch Ollama, add `--skip-ollama`.

### Manual installation

From the parent directory containing `liteagent/`:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r liteagent/requirements.txt
cp liteagent/.env.example liteagent/.env
```

Python 3.10+ is recommended (for the type-hint syntax used). `pymssql` may require FreeTDS or
build tools on some platforms; you can skip it if you're not using SQL Server for now, but it
must be installed before calling the MSSQL tool.

## Running it

```bash
# Web UI (default http://127.0.0.1:8000)
.venv/bin/uvicorn liteagent.web:app --host 0.0.0.0 --port 8000

# or the CLI
.venv/bin/python3 -m liteagent.cli
```

## Configuration

All core settings live in `config.py`; environment variables take priority over defaults. It
also reads `liteagent/.env` and a `.env` in the current directory. The web UI's "Settings" panel
lets you adjust most of these directly (saved to `data/runtime_settings.json`, which overrides
env vars and persists across restarts).

| Env var | Default | Purpose |
|---|---|---|
| `LITEAGENT_API_BASE` | `http://localhost:11434/v1/chat/completions` | Full Chat Completions URL (default points at a local Ollama port) |
| `LITEAGENT_API_KEY` | `not-required` | Authorization bearer token; fine to leave default if the endpoint doesn't check it |
| `LITEAGENT_MODEL` | `local-model` | Model name — replace with the actual model name your service serves |
| `LITEAGENT_MAX_ITERATIONS` | `100` | Max LLM/tool loop iterations per user request |
| `LITEAGENT_REQUEST_TIMEOUT` | `300` | LLM/web HTTP timeout in seconds |
| `LITEAGENT_SHELL_TIMEOUT` | `30` | Shell command timeout in seconds |
| `LITEAGENT_ALLOWED_ROOT` | `liteagent/workspace` | Sandboxed root directory for file/shell operations |
| `LITEAGENT_SQLITE_PATH` | `liteagent/data/conversations.sqlite` | The agent's own conversation memory |
| `LITEAGENT_LOG_PATH` | `liteagent/data/agent.log` | Log of reasoning, tool calls, and results |
| `DB_READ_ONLY` | `true` | Read-only protection for external database access |
| `LITEAGENT_WEB_SEARCH_RESULTS` | `5` | Number of search results returned |
| `LITEAGENT_FETCH_MAX_CHARS` | `50000` | Max characters of fetched web page text |
| `LITEAGENT_SUBAGENT_ENABLED` | `false` | Whether the sub-agent tool is enabled |
| `LITEAGENT_SUBAGENT_CONCURRENCY` | `sequential` | How multiple sub-agent calls in the same turn run: `sequential` or `parallel` |
| `LITEAGENT_EMBEDDING_BASE_URL` / `LITEAGENT_EMBEDDING_MODEL` / `LITEAGENT_EMBEDDING_API_KEY` | empty | Embedding endpoint/model/key for the knowledge base, must be compatible with the OpenAI `/embeddings` API (Ollama works too) |
| `LITEAGENT_KB_CHUNK_SIZE` | `800` | Knowledge base document chunk size (characters) |
| `LITEAGENT_KB_CHUNK_OVERLAP` | `120` | Overlap characters between chunks |

Absolute paths are recommended for path settings. Parent directories for the allowed root,
SQLite file, and log file are created automatically on startup.

### External database env vars (optional, used by the `db_query` tool)

```
MYSQL_HOST / MYSQL_PORT / MYSQL_USER / MYSQL_PASSWORD / MYSQL_DATABASE
MSSQL_HOST / MSSQL_PORT / MSSQL_USER / MSSQL_PASSWORD / MSSQL_DATABASE
```

### SMTP (optional, used for scheduled-task notification emails)

```
LITEAGENT_SMTP_HOST / LITEAGENT_SMTP_PORT / LITEAGENT_SMTP_USER / LITEAGENT_SMTP_PASSWORD
LITEAGENT_SMTP_FROM / LITEAGENT_DEFAULT_NOTIFY_EMAIL
```

## Desktop app (macOS)

```bash
bash liteagent/packaging/macos/build_macos_app.sh
```

Produces a standalone, double-clickable `~/Applications/LiteAgent.app` (with its own `.venv`,
`data/`, and `workspace/`, independent of any other deployment). Extra dependencies are listed in
`requirements-desktop.txt`.

## License

MIT License — see [LICENSE](LICENSE).
