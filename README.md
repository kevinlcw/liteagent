# LiteAgent

一個直接呼叫**任何 OpenAI-compatible Chat Completions 端點**（Ollama、vLLM、LM Studio、
或其他本機/自架的地端模型）的原生 Python Agent。單人使用、不經過任何雲端中介、資料完全
留在自己的機器上。支援原生 function calling、完整工具迴圈、SQLite 多輪記憶、CLI、FastAPI
網頁聊天，以及可打包成 macOS 桌面 App。

> LiteAgent 只是「換一個端點」就能用——不綁定特定模型或特定廠牌，只要是相容 OpenAI Chat
> Completions（含 tools/function calling）格式的地端服務都能接。

## 功能一覽

- **Agent Loop**：原生 function calling 的完整工具呼叫迴圈，支援串流、危險操作確認、對話
  自動壓縮（超過 context window 門檻自動摘要舊訊息）。
- **內建工具**：讀寫檔案、執行 shell、網路搜尋、讀取網頁、MySQL/SQL Server 查詢（唯讀保護）、
  長期記憶筆記（跨對話保存）、任務清單（Planning）。
- **MCP client**：可連接任意數量的外部 MCP 伺服器，把它們的工具併入同一個工具集。
- **Sub-agent**：可選功能（預設關閉），讓主 Agent 委派子任務給獨立的子代理人執行；同一輪
  多個子代理人可選擇「單線程」（依序執行）或「多線程」（透過 thread pool 真正平行執行）。
- **Embedding 設定**：可另外設定一組獨立的 embedding 端點/模型（供未來知識庫檢索功能使用，
  目前僅提供設定介面）。
- **排程任務**：類似 cron 的定時/單次任務，完成後可選擇寄 email 通知（需自行設定 SMTP）。
- **Skills 機制**：讀取 `workspace/skills/<dir>/SKILL.md` 格式的技能包，讓 Agent 依需要載入
  額外的操作指南。
- **桌面版**：`packaging/macos/build_macos_app.sh` 可把整個服務打包成 macOS 上可雙擊開啟的
  獨立 `.app`（內建自己的 FastAPI 服務，綁定 127.0.0.1，不對外開放）。

LiteAgent 刻意保持**單人**：沒有登入、沒有帳號、沒有多人協作機制——就是一套個人在自己機器
上跑的 Agent。

## 安裝

在包含 `liteagent/` 的上層目錄執行：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r liteagent/requirements.txt
cp liteagent/.env.example liteagent/.env
```

Python 3.10 以上（型別語法需求）為佳。`pymssql` 在少數平台可能需要 FreeTDS 或編譯工具；
若暫時不使用 SQL Server，可先略過該套件，但呼叫 MSSQL 工具前必須安裝。

## 啟動

```bash
# 網頁介面（預設 http://127.0.0.1:8000）
.venv/bin/uvicorn liteagent.web:app --host 0.0.0.0 --port 8000

# 或用 CLI
.venv/bin/python3 -m liteagent.cli
```

## 設定

所有核心設定集中於 `config.py`，環境變數優先於預設值；也會讀取 `liteagent/.env` 和目前目錄
的 `.env`。網頁介面的「設置」面板可直接調整大部分設定（存進 `data/runtime_settings.json`，
會覆蓋環境變數且重啟後保留）。

| 環境變數 | 預設值 | 用途 |
|---|---|---|
| `LITEAGENT_API_BASE` | `http://localhost:11434/v1/chat/completions` | 完整 Chat Completions URL（預設對應 Ollama 本機端口） |
| `LITEAGENT_API_KEY` | `not-required` | Authorization bearer token；端點不驗證時可維持預設 |
| `LITEAGENT_MODEL` | `local-model` | 模型名稱，請換成你實際服務的模型名稱 |
| `LITEAGENT_MAX_ITERATIONS` | `100` | 每次使用者請求最多 LLM/tool 迴圈 |
| `LITEAGENT_REQUEST_TIMEOUT` | `300` | LLM/Web HTTP timeout 秒數 |
| `LITEAGENT_SHELL_TIMEOUT` | `30` | shell timeout 秒數 |
| `LITEAGENT_ALLOWED_ROOT` | `liteagent/workspace` | 檔案與 shell 的安全根目錄 |
| `LITEAGENT_SQLITE_PATH` | `liteagent/data/conversations.sqlite` | Agent 自己的對話記憶 |
| `LITEAGENT_LOG_PATH` | `liteagent/data/agent.log` | reasoning、工具呼叫及結果紀錄 |
| `DB_READ_ONLY` | `true` | 外部資料庫唯讀保護 |
| `LITEAGENT_WEB_SEARCH_RESULTS` | `5` | 搜尋結果數 |
| `LITEAGENT_FETCH_MAX_CHARS` | `50000` | 網頁文字最大長度 |
| `LITEAGENT_SUBAGENT_ENABLED` | `false` | 是否啟用 Sub-agent 工具 |
| `LITEAGENT_SUBAGENT_CONCURRENCY` | `sequential` | 同一輪多個 Sub-agent 呼叫的執行方式：`sequential`（單線程）或 `parallel`（多線程） |

路徑設定建議使用絕對路徑。程式啟動時會自動建立 allowed root、SQLite 和 log 的父目錄。

### 外部資料庫環境變數（選填，供 `db_query` 工具使用）

```
MYSQL_HOST / MYSQL_PORT / MYSQL_USER / MYSQL_PASSWORD / MYSQL_DATABASE
MSSQL_HOST / MSSQL_PORT / MSSQL_USER / MSSQL_PASSWORD / MSSQL_DATABASE
```

### SMTP（選填，供排程結果通知信使用）

```
LITEAGENT_SMTP_HOST / LITEAGENT_SMTP_PORT / LITEAGENT_SMTP_USER / LITEAGENT_SMTP_PASSWORD
LITEAGENT_SMTP_FROM / LITEAGENT_DEFAULT_NOTIFY_EMAIL
```

## 桌面版（macOS）

```bash
bash liteagent/packaging/macos/build_macos_app.sh
```

會在 `~/Applications/LiteAgent.app` 產生一個可雙擊開啟的獨立 App（自己的 `.venv`、`data/`、
`workspace/`，不影響任何其他部署）。額外套件見 `requirements-desktop.txt`。

## 授權

MIT License，詳見 [LICENSE](LICENSE)。
