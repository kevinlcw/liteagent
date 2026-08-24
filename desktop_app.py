"""LiteAgent 桌面版啟動器（macOS 用，pywebview 版）。

用途：在本機開一個原生視窗（不是瀏覽器分頁），內部啟動自己的 FastAPI/uvicorn
服務，綁定在 127.0.0.1 的隨機可用 port，完全不對外開放、也不連任何遠端伺服器。

單人使用，沒有登入機制，開窗就能直接聊天。

執行方式（跟現有 `uvicorn liteagent.web:app` 的慣例一致，cwd 必須是這個
package 的「上一層」目錄）：
    cd <liteagent 資料夾的上一層目錄>
    .venv/bin/python3 -m liteagent.desktop_app

打包成 .app 由 packaging/macos/build_macos_app.sh 處理，該腳本產生的
.app 內的啟動 script 最終也是照上面這行指令呼叫。
"""
from __future__ import annotations

import socket
import sys
import threading
import time
import urllib.error
import urllib.request

WINDOW_TITLE = "LiteAgent"
WINDOW_WIDTH = 1280
WINDOW_HEIGHT = 860
READY_TIMEOUT_SECONDS = 30.0


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _run_server(port: int) -> None:
    import uvicorn

    from . import web  # 相對匯入本 package 內的 web.py，避免另外依賴 import string

    # uvicorn 偵測到不是在主執行緒時，install_signal_handlers() 會自動跳過安裝
    # signal handler，不會噴錯，可以放心在背景執行緒裡跑。
    uvicorn.run(web.app, host="127.0.0.1", port=port, log_level="warning")


def _wait_ready(url: str, timeout: float = READY_TIMEOUT_SECONDS) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=1)
            return True
        except (urllib.error.URLError, ConnectionError, OSError):
            time.sleep(0.3)
    return False


def start_backend_and_wait() -> str:
    """啟動背景 FastAPI 服務並等待就緒，回傳本機 URL。給桌面模式跟自我測試共用。"""
    port = _free_port()
    t = threading.Thread(target=_run_server, args=(port,), daemon=True)
    t.start()
    url = f"http://127.0.0.1:{port}/"
    if not _wait_ready(url):
        raise SystemExit("LiteAgent 本機服務啟動逾時（30 秒內未就緒）")
    return url


def main() -> None:
    url = start_backend_and_wait()

    import webview

    webview.create_window(
        WINDOW_TITLE,
        url,
        width=WINDOW_WIDTH,
        height=WINDOW_HEIGHT,
        min_size=(900, 600),
    )
    webview.start()


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        # headless 自我測試用：只驗證後端能不能正常啟動、就緒，不開視窗
        # （給沒有桌面環境的機器，例如本機開發用的 Linux 主機，驗證邏輯用）。
        u = start_backend_and_wait()
        body = urllib.request.urlopen(u, timeout=5).read()
        print(f"SELFTEST_OK url={u} bytes={len(body)}")
    else:
        main()
