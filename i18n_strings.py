"""Minimal backend i18n helper, ported from qwen_agent's i18n_strings.py.

Currently only used for the "reply language hint" nudge appended to the system prompt
based on the UI's selected language (see agent.py._system_message). The frontend
(static/index.html) already sends the user's chosen language on every request via the
`X-UI-Lang` header (see the window.fetch wrapper in applyLang()); this module just
normalizes that header value and maps it to a soft instruction fragment.
"""

from __future__ import annotations


def normalize_lang(value: str | None) -> str:
    value = (value or "").strip().lower()
    if value.startswith("ja"):
        return "ja"
    if value.startswith("en"):
        return "en"
    return "zh-Hant"


STRINGS: dict[str, dict[str, str]] = {
    "system.reply_lang_hint": {
        "zh-Hant": "",
        "en": "\n\n【Interface language note】The user's UI is currently set to English. Unless their message is clearly written in a different language, please reply in English.",
        "ja": "\n\n【インターフェース言語について】ユーザーのUIは現在日本語に設定されています。メッセージが明らかに別の言語で書かれている場合を除き、日本語で返信してください。",
    },
}
