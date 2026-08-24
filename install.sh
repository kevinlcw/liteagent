#!/usr/bin/env bash
# LiteAgent 傻瓜安裝腳本
#
# 用法（一行安裝，全自動，不會問任何問題）：
#   curl -fsSL https://raw.githubusercontent.com/kevinlcw/liteagent/main/install.sh | bash -s -- --yes
#
# 用法（互動式，會在每個關鍵步驟詢問）：
#   curl -fsSL https://raw.githubusercontent.com/kevinlcw/liteagent/main/install.sh | bash
#
# 也可以先 git clone 整個 repo 之後，直接執行 liteagent/install.sh。
#
# 這支腳本會做的事：
#   1. 檢查 python3 / git 是否存在
#   2. 下載（或更新）LiteAgent 原始碼
#   3. 建立虛擬環境並安裝套件
#   4. 偵測本機有沒有 Ollama（地端模型服務），沒有的話可選擇自動安裝並下載一個預設模型
#   5. 產生 .env 設定檔
#   6. 產生 start.sh（之後啟動用）與 macOS 專屬的可雙擊 LiteAgent.command
#   7. 詢問是否要立刻啟動並開啟瀏覽器

set -euo pipefail

REPO_URL="https://github.com/kevinlcw/liteagent.git"
DEFAULT_MODEL="llama3.2"
PORT="${LITEAGENT_PORT:-8000}"

AUTO_YES=false
SKIP_OLLAMA=false
for arg in "$@"; do
  case "$arg" in
    --yes|-y) AUTO_YES=true ;;
    --skip-ollama) SKIP_OLLAMA=true ;;
  esac
done

info()  { printf '\033[1;34m[LiteAgent]\033[0m %s\n' "$1"; }
warn()  { printf '\033[1;33m[警告]\033[0m %s\n' "$1"; }
err()   { printf '\033[1;31m[錯誤]\033[0m %s\n' "$1" >&2; }

# 互動詢問；curl | bash 的情況下 stdin 是腳本本身，所以改從 /dev/tty 讀鍵盤輸入。
# --yes 或無法取得 /dev/tty（例如完全非互動環境）時一律採用預設值。
confirm() {
  local prompt="$1" default_yes="$2" ans=""
  if $AUTO_YES; then
    return 0
  fi
  if [[ -r /dev/tty ]]; then
    read -r -p "$prompt " ans < /dev/tty || ans=""
  else
    ans=""
  fi
  case "$ans" in
    [Yy]*) return 0 ;;
    [Nn]*) return 1 ;;
    *) [[ "$default_yes" == "y" ]] ;;
  esac
}

OS="$(uname -s)"

# 1. 檢查 python3
if ! command -v python3 >/dev/null 2>&1; then
  err "找不到 python3，請先安裝 Python 3.10 以上再重新執行本腳本。"
  if [[ "$OS" == "Darwin" ]]; then
    echo "  macOS 可執行：brew install python3"
  else
    echo "  Debian/Ubuntu 可執行：sudo apt-get install -y python3 python3-venv git"
  fi
  exit 1
fi
PY_VER=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
info "偵測到 Python $PY_VER"

# 2. 定位或下載原始碼
#    支援兩種情境：
#    a) 已經 clone 過整個 repo，直接執行 liteagent/install.sh -> 沿用現有原始碼
#    b) curl | bash 全新安裝 -> clone 到 ~/LiteAgent/liteagent
SCRIPT_SOURCE="${BASH_SOURCE[0]:-}"
SCRIPT_DIR=""
if [[ -n "$SCRIPT_SOURCE" && -f "$SCRIPT_SOURCE" ]]; then
  SCRIPT_DIR="$(cd "$(dirname "$SCRIPT_SOURCE")" && pwd)"
fi

if [[ -n "$SCRIPT_DIR" && -f "$SCRIPT_DIR/requirements.txt" && -f "$SCRIPT_DIR/web.py" ]]; then
  REPO_DIR="$SCRIPT_DIR"
  PARENT_DIR="$(dirname "$REPO_DIR")"
  info "偵測到本機既有原始碼，直接在此安裝：$REPO_DIR"
else
  PARENT_DIR="${LITEAGENT_INSTALL_DIR:-$HOME/LiteAgent}"
  REPO_DIR="$PARENT_DIR/liteagent"
  mkdir -p "$PARENT_DIR"
  if [[ -d "$REPO_DIR/.git" ]]; then
    info "偵測到既有安裝於 $REPO_DIR，更新中..."
    git -C "$REPO_DIR" pull --ff-only
  else
    command -v git >/dev/null 2>&1 || { err "找不到 git，請先安裝 git。"; exit 1; }
    info "下載 LiteAgent 原始碼到 $REPO_DIR ..."
    git clone --depth 1 "$REPO_URL" "$REPO_DIR"
  fi
fi

cd "$PARENT_DIR"

# 3. 建立虛擬環境並安裝套件
if [[ ! -d ".venv" ]]; then
  info "建立 Python 虛擬環境..."
  python3 -m venv .venv
fi
info "安裝套件（第一次執行會花幾分鐘）..."
./.venv/bin/pip install --quiet --upgrade pip
./.venv/bin/pip install --quiet -r liteagent/requirements.txt

# 4. 偵測 / 安裝 Ollama
MODEL_NAME=""
if $SKIP_OLLAMA; then
  info "已指定 --skip-ollama，略過地端模型服務設定，稍後請自行編輯 liteagent/.env。"
elif command -v ollama >/dev/null 2>&1; then
  info "偵測到已安裝 Ollama。"
  MODEL_NAME="$DEFAULT_MODEL"
else
  warn "沒有偵測到 Ollama（地端模型服務）。LiteAgent 本身不含模型，需要一個地端服務才能對話。"
  if confirm "要現在自動安裝 Ollama 嗎？(Y/n)" y; then
    if [[ "$OS" == "Darwin" ]]; then
      if command -v brew >/dev/null 2>&1; then
        brew install ollama
      else
        curl -fsSL https://ollama.com/install.sh | sh
      fi
    elif [[ "$OS" == "Linux" ]]; then
      curl -fsSL https://ollama.com/install.sh | sh
    else
      warn "此作業系統無法自動安裝，請自行至 https://ollama.com 下載安裝。"
    fi
    command -v ollama >/dev/null 2>&1 && MODEL_NAME="$DEFAULT_MODEL"
  else
    warn "略過安裝。之後請自行準備一個 OpenAI-compatible 端點，並修改 liteagent/.env。"
  fi
fi

if [[ -n "$MODEL_NAME" ]] && command -v ollama >/dev/null 2>&1; then
  if ! curl -fsS http://localhost:11434/api/tags >/dev/null 2>&1; then
    info "啟動 Ollama 背景服務..."
    (nohup ollama serve >/tmp/liteagent-ollama.log 2>&1 &)
    sleep 2
  fi
  if ! ollama list 2>/dev/null | grep -q "$MODEL_NAME"; then
    if confirm "要下載預設模型 $MODEL_NAME 嗎？(視網速可能需要幾分鐘) (Y/n)" y; then
      ollama pull "$MODEL_NAME" || { warn "模型下載失敗，可稍後手動執行：ollama pull $MODEL_NAME"; MODEL_NAME=""; }
    else
      MODEL_NAME=""
    fi
  fi
fi

# 5. 產生 .env
ENV_FILE="liteagent/.env"
if [[ ! -f "$ENV_FILE" ]]; then
  cp liteagent/.env.example "$ENV_FILE"
fi
if [[ -n "$MODEL_NAME" ]]; then
  if grep -q '^LITEAGENT_MODEL=' "$ENV_FILE"; then
    sed -i.bak "s/^LITEAGENT_MODEL=.*/LITEAGENT_MODEL=$MODEL_NAME/" "$ENV_FILE" && rm -f "$ENV_FILE.bak"
  else
    printf 'LITEAGENT_MODEL=%s\n' "$MODEL_NAME" >> "$ENV_FILE"
  fi
  if grep -q '^LITEAGENT_API_BASE=' "$ENV_FILE"; then
    sed -i.bak "s#^LITEAGENT_API_BASE=.*#LITEAGENT_API_BASE=http://localhost:11434/v1/chat/completions#" "$ENV_FILE" && rm -f "$ENV_FILE.bak"
  fi
fi

# 6. 產生啟動腳本
cat > start.sh <<EOS
#!/usr/bin/env bash
cd "\$(dirname "\$0")"
source .venv/bin/activate
PORT="\${LITEAGENT_PORT:-$PORT}"
( sleep 1.5; command -v open >/dev/null 2>&1 && open "http://127.0.0.1:\$PORT" ; command -v xdg-open >/dev/null 2>&1 && xdg-open "http://127.0.0.1:\$PORT" ) &
exec uvicorn liteagent.web:app --host 127.0.0.1 --port "\$PORT"
EOS
chmod +x start.sh

if [[ "$OS" == "Darwin" ]]; then
  cat > "LiteAgent.command" <<'EOS'
#!/usr/bin/env bash
cd "$(dirname "$0")"
./start.sh
EOS
  chmod +x "LiteAgent.command"
fi

info "安裝完成！位置：$PARENT_DIR"
echo
echo "  之後要啟動，執行：cd \"$PARENT_DIR\" && ./start.sh"
if [[ "$OS" == "Darwin" ]]; then
  echo "  或直接在 Finder 雙擊：$PARENT_DIR/LiteAgent.command"
fi
echo "  網頁介面預設在 http://127.0.0.1:$PORT"
echo

if confirm "要現在立刻啟動 LiteAgent 嗎？(Y/n)" y; then
  ./start.sh
else
  info "好的，之後隨時可以用上面的指令啟動。"
fi
