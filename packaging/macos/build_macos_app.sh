#!/bin/bash
# 把 liteagent 打包成本機單人用的 macOS 桌面 App（.app）。
#
# 用法（要在 macOS 上執行，Linux 開發機沒有 GUI 沒辦法真的跑出視窗）：
#   1. 把整個 liteagent 專案資料夾（這支腳本的上上層，即含 desktop_app.py、
#      requirements.txt、requirements-desktop.txt 的那個資料夾）複製到你想
#      安裝的位置，例如 ~/LiteAgentDesktop/liteagent
#   2. cd 進 liteagent 這個資料夾，執行：
#        bash packaging/macos/build_macos_app.sh
#   3. 完成後會在 ~/Applications/LiteAgent.app 產生一個可以直接雙擊開啟的 App。
#
# 這個 App 是「獨立的一份」：有自己的 .venv（跟 liteagent 同一層）、自己的
# data/、workspace/（首次啟動時自動建立）。單人模式，沒有登入機制，開啟後可
# 直接使用。

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"   # .../liteagent
RUN_DIR="$(dirname "$REPO_DIR")"                                  # liteagent 的上一層
APP_NAME="LiteAgent"
INSTALL_DIR="${LITEAGENT_DESKTOP_INSTALL_DIR:-$HOME/Applications}"
BUNDLE="$INSTALL_DIR/$APP_NAME.app"
VENV_DIR="$RUN_DIR/.venv"

echo "[1/5] 專案套件目錄：$REPO_DIR"
echo "[1/5] 執行目錄（cwd）：$RUN_DIR"
echo "[1/5] 產出位置：$BUNDLE"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "錯誤：這支腳本必須在 macOS 上執行（要打包出 .app 才有意義）。" >&2
  exit 1
fi

echo "[2/5] 準備虛擬環境：$VENV_DIR"
# mcp 套件要求 Python >= 3.10，macOS 系統內建的 /usr/bin/python3 常常是舊版
# （例如 3.9），這裡優先找系統上已有的較新版 python3，找不到才退回 python3。
PY_BIN=""
for cand in \
  /Library/Frameworks/Python.framework/Versions/3.12/bin/python3.12 \
  /Library/Frameworks/Python.framework/Versions/3.11/bin/python3.11 \
  /Library/Frameworks/Python.framework/Versions/3.10/bin/python3.10 \
  /opt/homebrew/bin/python3.12 /opt/homebrew/bin/python3.11 /opt/homebrew/bin/python3.10 \
  python3.12 python3.11 python3.10; do
  if command -v "$cand" >/dev/null 2>&1; then
    ver="$("$cand" -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null || true)"
    if [[ -n "$ver" ]]; then
      PY_BIN="$cand"
      break
    fi
  fi
done
if [[ -z "$PY_BIN" ]]; then
  sys_ver="$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
  echo "警告：找不到 3.10 以上的 python3，退回系統的 python3（版本 $sys_ver），mcp 套件安裝可能會失敗。" >&2
  PY_BIN="python3"
fi
echo "[2/5] 使用的 Python：$PY_BIN ($($PY_BIN --version 2>&1))"
if [[ ! -x "$VENV_DIR/bin/python3" ]]; then
  "$PY_BIN" -m venv "$VENV_DIR"
fi
"$VENV_DIR/bin/pip" install --quiet --upgrade pip
"$VENV_DIR/bin/pip" install --quiet -r "$REPO_DIR/requirements.txt" -r "$REPO_DIR/requirements-desktop.txt"

echo "[3/5] 建立 .app 骨架"
rm -rf "$BUNDLE"
mkdir -p "$BUNDLE/Contents/MacOS" "$BUNDLE/Contents/Resources"

cat > "$BUNDLE/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key>
  <string>LiteAgent</string>
  <key>CFBundleDisplayName</key>
  <string>LiteAgent</string>
  <key>CFBundleIdentifier</key>
  <string>com.liteagent.desktop</string>
  <key>CFBundleVersion</key>
  <string>1.0</string>
  <key>CFBundleShortVersionString</key>
  <string>1.0</string>
  <key>CFBundlePackageType</key>
  <string>APPL</string>
  <key>CFBundleExecutable</key>
  <string>$APP_NAME</string>
  <key>LSMinimumSystemVersion</key>
  <string>11.0</string>
  <key>NSHighResolutionCapable</key>
  <true/>
  <key>LSUIElement</key>
  <false/>
</dict>
</plist>
PLIST

echo "[4/5] 寫入啟動腳本"
cat > "$BUNDLE/Contents/MacOS/$APP_NAME" <<LAUNCH
#!/bin/bash
# 由 build_macos_app.sh 自動產生，路徑已寫死指向打包當下的專案目錄。
cd "$RUN_DIR" || exit 1
exec "$VENV_DIR/bin/python3" -m liteagent.desktop_app
LAUNCH
chmod +x "$BUNDLE/Contents/MacOS/$APP_NAME"

echo "[5/5] 完成"
echo "可以用 open \"$BUNDLE\" 開啟，或到 Finder 雙擊 $BUNDLE"
