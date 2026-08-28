[中文](README.md) | [English](README.en.md) | **日本語**

# LiteAgent

**OpenAI互換のChat Completionsエンドポイント**（Ollama、vLLM、LM Studio、その他ローカル/自
前ホストのモデル）に直接接続する、ネイティブPython製のAIエージェントです。単一ユーザー向け
で、クラウドを経由せず、データは完全に自分のマシン内に留まります。ネイティブfunction
calling、完全なツール呼び出しループ、SQLiteによる複数ターンの記憶、CLI、FastAPI製のWebチャッ
トUIに対応し、macOSのデスクトップアプリとしてパッケージ化することもできます。

> LiteAgentは「エンドポイントを差し替えるだけ」で動作します——特定のモデルやベンダーに縛られ
> ません。OpenAIのChat Completions形式（tools/function calling含む）に対応したローカルサービ
> スであれば、どれでも接続できます。

## 主な機能

- **Agentループ**：ネイティブfunction callingによる完全なツール呼び出しループ。ストリーミン
  グ、リスクの高い操作の確認プロンプト、自動的な会話圧縮（コンテキストウィンドウのしきい値を
  超えると古いメッセージを自動要約）に対応。
- **標準搭載ツール**：ファイルの読み書き、シェル実行、Web検索、Webページの読み取り、
  MySQL/SQL Serverへのクエリ（読み取り専用で保護）、長期記憶メモ（会話をまたいで保持）、タス
  クリスト（プランニング）。
- **MCPクライアント**：任意の数の外部MCPサーバーに接続し、それらのツールを同じツールセットに
  統合できます。
- **サブエージェント**：オプション機能（デフォルトは無効）。メインエージェントがサブタスクを
  独立したサブエージェントに委任できます。同じターン内の複数のサブエージェント呼び出しは、
  「sequential」（順次実行）または「parallel」（スレッドプールによる真の並列実行）を選択でき
  ます。
- **ナレッジベース / RAG**：独立したembeddingエンドポイント/モデルを設定すると、
  `kb_add_document` でワークスペース内のテキストファイル（.txt/.md）をチャンク分割・ベクトル
  化してインデックス化できます。その後 `kb_search` でセマンティック検索を行い、回答時に出典を
  引用できます。ベクトルはローカルのSQLite（`kb.sqlite`）に保存され、コサイン類似度による総当
  たり比較を行うため、別途ベクトルデータベースサービスは不要です。`kb_list_documents` /
  `kb_remove_document` でインデックス済みファイルを管理できます。現時点ではUTF-8のプレーンテ
  キストのみに対応しており、pdf/docx/pptxの解析は今後のバージョンで対応予定です。
- **スケジュールタスク**：cronのような定期/単発タスク。完了時にメール通知を送ることも可能で
  す（別途SMTP設定が必要）。
- **Skills機構**：`workspace/skills/<dir>/SKILL.md` 形式のスキルパッケージを読み込み、必要に
  応じてエージェントが追加の操作手順をロードできます。
- **デスクトップ版**：`packaging/macos/build_macos_app.sh` により、サービス全体をダブルクリッ
  クで起動できるmacOS用の独立した `.app` にパッケージ化できます（内蔵のFastAPIサービスは
  `127.0.0.1` にバインドされ、外部には公開されません）。

LiteAgentは意図的に**単一ユーザー**向けに保たれています——ログインもアカウントもマルチユーザー
協働機構もない、自分のマシン上で動くエージェントです。

## インストール

### ワンライン・インストール（推奨）

macOS / Linuxでは、ターミナルを開いて以下の一行を貼り付けるだけで、全自動でセットアップされ
ます（Ollamaの検出・任意インストール、デフォルトモデルのダウンロードにも対応）。

```bash
curl -fsSL https://raw.githubusercontent.com/kevinlcw/liteagent/main/install.sh | bash -s -- --yes
```

`-- --yes` を付けなければ対話モードになり、各重要ステップ（Ollamaをインストールするか、モデル
をダウンロードするか、今すぐ起動するか）ごとに確認を求められます。インストール完了後、ホーム
ディレクトリに `~/LiteAgent/`（環境変数 `LITEAGENT_INSTALL_DIR` でパス変更可）が作成され、以
下が含まれます。

- `~/LiteAgent/start.sh` — 次回以降の起動はこれを実行するだけ
- `~/LiteAgent/LiteAgent.command`（macOS専用）— Finderでこのファイルを**ダブルクリック**する
  だけで起動でき、ターミナル操作は不要

このスクリプト自体はリポジトリ内の [`install.sh`](install.sh) にもあります。すでに手動で
`git clone` 済みであれば、`bash liteagent/install.sh` を実行しても同じ結果になります（再ダウ
ンロードはされません）。すでに自前のローカルモデルサービスがあり、スクリプトにOllamaを触らせ
たくない場合は `--skip-ollama` を付けてください。

### 手動インストール

`liteagent/` を含む親ディレクトリで以下を実行します。

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r liteagent/requirements.txt
cp liteagent/.env.example liteagent/.env
```

Python 3.10以上を推奨します（使用している型ヒント構文の要件）。`pymssql` は一部のプラットフ
ォームでFreeTDSやビルドツールが必要になる場合があります。SQL Serverを当面使わないのであれば
一旦省略して構いませんが、MSSQLツールを呼び出す前には必ずインストールしてください。

## 起動方法

```bash
# WebUI（デフォルト http://127.0.0.1:8000）
.venv/bin/uvicorn liteagent.web:app --host 0.0.0.0 --port 8000

# またはCLI
.venv/bin/python3 -m liteagent.cli
```

## 設定

主要な設定はすべて `config.py` に集約されており、環境変数がデフォルト値より優先されます。
`liteagent/.env` とカレントディレクトリの `.env` も読み込まれます。WebUIの「設定」パネルから
主要な設定を直接変更でき（`data/runtime_settings.json` に保存され、環境変数を上書きし、再起
動後も保持されます）。

| 環境変数 | デフォルト値 | 用途 |
|---|---|---|
| `LITEAGENT_API_BASE` | `http://localhost:11434/v1/chat/completions` | Chat Completionsの完全なURL（デフォルトはローカルOllamaのポートを指す） |
| `LITEAGENT_API_KEY` | `not-required` | Authorization bearerトークン。エンドポイント側で検証しないならデフォルトのままで可 |
| `LITEAGENT_MODEL` | `local-model` | モデル名。実際に使用しているサービスのモデル名に置き換えてください |
| `LITEAGENT_MAX_ITERATIONS` | `100` | 1回のユーザーリクエストあたりのLLM/ツールループの最大回数 |
| `LITEAGENT_REQUEST_TIMEOUT` | `300` | LLM/WebのHTTPタイムアウト秒数 |
| `LITEAGENT_SHELL_TIMEOUT` | `30` | シェルコマンドのタイムアウト秒数 |
| `LITEAGENT_ALLOWED_ROOT` | `liteagent/workspace` | ファイル・シェル操作のサンドボックスとなるルートディレクトリ |
| `LITEAGENT_SQLITE_PATH` | `liteagent/data/conversations.sqlite` | エージェント自身の会話記憶 |
| `LITEAGENT_LOG_PATH` | `liteagent/data/agent.log` | 推論、ツール呼び出し、結果のログ |
| `DB_READ_ONLY` | `true` | 外部データベースアクセスの読み取り専用保護 |
| `LITEAGENT_WEB_SEARCH_RESULTS` | `5` | 返す検索結果の件数 |
| `LITEAGENT_FETCH_MAX_CHARS` | `50000` | 取得するWebページテキストの最大文字数 |
| `LITEAGENT_SUBAGENT_ENABLED` | `false` | サブエージェントツールを有効にするか |
| `LITEAGENT_SUBAGENT_CONCURRENCY` | `sequential` | 同一ターン内の複数サブエージェント呼び出しの実行方式：`sequential`（順次）または `parallel`（並列） |
| `LITEAGENT_EMBEDDING_BASE_URL` / `LITEAGENT_EMBEDDING_MODEL` / `LITEAGENT_EMBEDDING_API_KEY` | 空 | ナレッジベース用のembeddingエンドポイント/モデル/キー。OpenAIの `/embeddings` API互換であること（Ollamaも可） |
| `LITEAGENT_KB_CHUNK_SIZE` | `800` | ナレッジベース文書のチャンクサイズ（文字数） |
| `LITEAGENT_KB_CHUNK_OVERLAP` | `120` | チャンク間の重複文字数 |

パス設定には絶対パスを推奨します。起動時にallowed root、SQLite、ログファイルの親ディレクトリ
は自動的に作成されます。

### 外部データベース用の環境変数（任意、`db_query` ツールで使用）

```
MYSQL_HOST / MYSQL_PORT / MYSQL_USER / MYSQL_PASSWORD / MYSQL_DATABASE
MSSQL_HOST / MSSQL_PORT / MSSQL_USER / MSSQL_PASSWORD / MSSQL_DATABASE
```

### SMTP（任意、スケジュールタスクの結果通知メールで使用）

```
LITEAGENT_SMTP_HOST / LITEAGENT_SMTP_PORT / LITEAGENT_SMTP_USER / LITEAGENT_SMTP_PASSWORD
LITEAGENT_SMTP_FROM / LITEAGENT_DEFAULT_NOTIFY_EMAIL
```

## デスクトップ版（macOS）

```bash
bash liteagent/packaging/macos/build_macos_app.sh
```

`~/Applications/LiteAgent.app` にダブルクリックで起動できる独立したアプリが生成されます（専
用の `.venv`、`data/`、`workspace/` を持ち、他のデプロイには影響しません）。追加の依存パッケ
ージは `requirements-desktop.txt` を参照してください。

## ライセンス

MIT License — 詳細は [LICENSE](LICENSE) を参照してください。
