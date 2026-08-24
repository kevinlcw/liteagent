"""Minimal SMTP email sender used for scheduled-task result notifications.

Deliberately tiny (stdlib smtplib only, no extra dependency): this app only ever sends
short plain-text notification emails, so there's no need for a templating/async mail
library. Configuration is read from `Config` (see config.py's smtp_* fields), which can be
set either via env vars (LITEAGENT_SMTP_*) or live through the web settings panel.
"""

from __future__ import annotations

import smtplib
from email.mime.text import MIMEText

from .config import Config


class EmailNotConfigured(RuntimeError):
    """Raised when send_email is called before SMTP has been configured."""


def send_email(config: Config, to: str, subject: str, body: str) -> None:
    to = (to or "").strip()
    if not to:
        raise ValueError("收件人 email 不可為空白")
    if not config.smtp_host or not config.smtp_user or not config.smtp_password:
        raise EmailNotConfigured("尚未設定 SMTP，請先在「設置」面板的 Email 通知區塊填入 SMTP 主機／帳號／密碼。")

    from_addr = config.smtp_from or config.smtp_user
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to

    with smtplib.SMTP(config.smtp_host, int(config.smtp_port or 587), timeout=30) as server:
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(config.smtp_user, config.smtp_password)
        server.sendmail(from_addr, [to], msg.as_string())
