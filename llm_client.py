"""Small OpenAI-compatible HTTP client; no intermediary LLM is used."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
import json
import logging
import time
import requests

from .config import Config, settings

_logger = logging.getLogger(__name__)

# How many times to retry a chat-completions request when the connection to the LLM
# backend itself can't be established (DNS/TCP/connect-timeout) -- e.g. a brief network
# blip or the inference server restarting. Deliberately scoped to *connection-level*
# failures only: never retries a ReadTimeout (model may just be slow generating) or an
# HTTP error status, since the server may already be mid-generation for that request and
# re-sending it would risk duplicate work / confusing side effects.
_CONNECT_RETRY_ATTEMPTS = 3
_CONNECT_RETRY_BACKOFF_SECONDS = 2.0


def _post_with_connect_retry(url: str, **kwargs: Any) -> requests.Response:
    """requests.post() with automatic retry on connection-establishment failures only."""
    last_exc: requests.exceptions.ConnectionError | None = None
    for attempt in range(1, _CONNECT_RETRY_ATTEMPTS + 1):
        try:
            return requests.post(url, **kwargs)
        except requests.exceptions.ConnectionError as exc:
            last_exc = exc
            if attempt < _CONNECT_RETRY_ATTEMPTS:
                wait = _CONNECT_RETRY_BACKOFF_SECONDS * attempt
                _logger.warning(
                    "LLM connect failed (attempt %d/%d): %s; retrying in %.0fs",
                    attempt, _CONNECT_RETRY_ATTEMPTS, exc, wait,
                )
                time.sleep(wait)
    assert last_exc is not None
    raise last_exc


class LLMClient:
    def __init__(self, config: Config = settings):
        self.config = config

    @staticmethod
    def fetch_models(base_url: str, api_key: str = "") -> list[str]:
        """Query an OpenAI-compatible /models endpoint and return sorted model ids."""
        url = base_url.rstrip("/") + "/models"
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        data = response.json()
        items = data.get("data") if isinstance(data, dict) else data
        ids = [item.get("id") for item in items or [] if isinstance(item, dict) and item.get("id")]
        return sorted(set(ids))

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"model": self.config.model, "messages": messages}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        response = _post_with_connect_retry(
            self.config.chat_completions_url,
            headers=headers,
            json=payload,
            timeout=self.config.request_timeout,
        )
        response.raise_for_status()
        data = response.json()
        if not data.get("choices"):
            raise RuntimeError(f"LLM response has no choices: {data}")
        choice = data["choices"][0]
        message = choice.get("message") or {}
        return {
            "message": message,
            "finish_reason": choice.get("finish_reason"),
            "usage": data.get("usage"),
        }

    def stream_chat_completion(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> Iterator[dict[str, Any]]:
        """Yield normalized events from an OpenAI-compatible SSE response."""
        payload = {
            "model": self.config.model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"

        tool_calls: dict[int, dict[str, Any]] = {}
        finish_reason: str | None = None
        with _post_with_connect_retry(
            self.config.chat_completions_url,
            headers=headers,
            json=payload,
            timeout=self.config.request_timeout,
            stream=True,
        ) as response:
            response.raise_for_status()
            # Some OpenAI-compatible servers omit charset on text/event-stream;
            # requests would otherwise decode UTF-8 Chinese as ISO-8859-1.
            response.encoding = "utf-8"
            for raw_line in response.iter_lines(decode_unicode=True):
                if not raw_line:
                    continue
                line = raw_line.strip()
                if not line.startswith("data:"):
                    continue
                data_text = line[5:].strip()
                if data_text == "[DONE]":
                    break
                try:
                    data = json.loads(data_text)
                except json.JSONDecodeError as exc:
                    raise RuntimeError(f"Invalid SSE JSON from LLM: {data_text[:200]}") from exc
                choices = data.get("choices") or []
                usage = data.get("usage")
                if usage:
                    prompt_tokens = int(usage.get("prompt_tokens") or 0)
                    completion_tokens = int(usage.get("completion_tokens") or 0)
                    yield {
                        "type": "usage",
                        "prompt_tokens": prompt_tokens,
                        "completion_tokens": completion_tokens,
                        "total_tokens": int(usage.get("total_tokens") or prompt_tokens + completion_tokens),
                    }
                if not choices:
                    continue
                choice = choices[0]
                delta = choice.get("delta") or {}
                if delta.get("content") is not None:
                    yield {"type": "content_delta", "text": delta["content"]}
                reasoning = delta.get("reasoning")
                if reasoning is None:
                    reasoning = delta.get("reasoning_content")
                if reasoning is not None:
                    yield {"type": "reasoning_delta", "text": reasoning}
                for part in delta.get("tool_calls") or []:
                    index = int(part.get("index", 0))
                    assembled = tool_calls.setdefault(
                        index,
                        {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
                    )
                    if part.get("id"):
                        assembled["id"] = part["id"]
                    if part.get("type"):
                        assembled["type"] = part["type"]
                    function = part.get("function") or {}
                    if function.get("name"):
                        assembled["function"]["name"] += function["name"]
                    arguments_delta = function.get("arguments") or ""
                    assembled["function"]["arguments"] += arguments_delta
                    yield {
                        "type": "tool_call_delta",
                        "index": index,
                        "id": part.get("id"),
                        "name": function.get("name"),
                        "arguments_delta": arguments_delta,
                    }
                if choice.get("finish_reason") is not None:
                    finish_reason = choice["finish_reason"]

        yield {
            "type": "done",
            "finish_reason": finish_reason,
            "tool_calls": [tool_calls[index] for index in sorted(tool_calls)],
        }
