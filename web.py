"""FastAPI API and static single-page chat UI.

LiteAgent is deliberately single-user: there is no login, no session, no per-account
scoping. Every helper below that historically took an http_request/user_id purely to
support multi-user mode now has a fixed, trivial answer (no owner, always "admin"-level
access) -- kept as tiny functions rather than deleted so the endpoint bodies below stay
identical in shape to the always-been-single-user parts of the app.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import base64
import binascii
import json
import mimetypes
import os
import shutil
import uuid

import requests

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from .agent import Agent
from .i18n_strings import normalize_lang
from .config import DEFAULT_SYSTEM_PROMPT, settings
from .skills import SKILLS_DIRNAME, list_skills
from .llm_client import LLMClient
from .mailer import send_email
from .scheduler_runner import SchedulerRunner

PACKAGE_DIR = Path(__file__).resolve().parent


def _read_app_version() -> str:
    try:
        return (PACKAGE_DIR / "VERSION").read_text(encoding="utf-8").strip() or "0.0.0"
    except Exception:
        return "0.0.0"


APP_VERSION = _read_app_version()

app = FastAPI(title="LiteAgent", version=APP_VERSION)
agent = Agent()
scheduler_runner = SchedulerRunner(agent, agent.tools.schedules, settings)
INDEX = Path(__file__).resolve().parent / "static" / "index.html"


@app.on_event("startup")
def _start_scheduler() -> None:
    scheduler_runner.start()


# Single-user app: no accounts, no sessions. These stubs exist only so the endpoint bodies
# below (shared in spirit with the earlier multi-user codebase this was trimmed from) don't
# need per-call special-casing -- "no owner" / "always allowed" everywhere.
def _current_user_id(http_request: Request) -> str | None:
    return None


def _is_admin(http_request: Request) -> bool:
    return True


def _assisted_by(http_request: Request) -> str | None:
    return None


def _require_conversation_access(conversation_id: str, http_request: Request) -> None:
    return None


class ChatRequest(BaseModel):
    message: str = Field(min_length=1)
    conversation_id: str | None = None
    include_debug: bool = False
    images: list[str] = Field(default_factory=list)


class UploadRequest(BaseModel):
    filename: str = Field(min_length=1)
    data_b64: str = Field(min_length=1)


class LLMSettingsRequest(BaseModel):
    base_url: str | None = Field(default=None, min_length=1)
    model: str | None = Field(default=None, min_length=1)
    system_prompt: str | None = Field(default=None, min_length=1)
    context_window_tokens: int | None = Field(default=None, gt=0)
    api_key: str | None = Field(default=None, min_length=1)
    request_timeout: int | None = Field(default=None, gt=0, le=3600)


class BehaviorSettingsRequest(BaseModel):
    max_iterations: int | None = Field(default=None, gt=0, le=500)
    shell_timeout: int | None = Field(default=None, gt=0, le=3600)
    web_search_results: int | None = Field(default=None, gt=0, le=20)
    fetch_max_chars: int | None = Field(default=None, gt=0, le=1000000)
    memory_char_budget: int | None = Field(default=None, gt=0, le=100000)
    compact_trigger_ratio: float | None = Field(default=None, gt=0, le=1)
    compact_keep_recent_turns: int | None = Field(default=None, ge=0, le=50)


class SubagentSettingsRequest(BaseModel):
    max_iterations: int | None = Field(default=None, gt=0, le=50)
    max_seconds: int | None = Field(default=None, gt=0, le=1800)
    max_per_turn: int | None = Field(default=None, gt=0, le=20)
    enabled: bool | None = None
    concurrency: str | None = Field(default=None, pattern=r"^(sequential|parallel)$")


class EmbeddingSettingsRequest(BaseModel):
    base_url: str | None = None
    model: str | None = None
    api_key: str | None = Field(default=None, min_length=1)


class KbSettingsRequest(BaseModel):
    chunk_size: int | None = Field(default=None, gt=0, le=8000)
    chunk_overlap: int | None = Field(default=None, ge=0, le=4000)


class KbUploadRequest(BaseModel):
    filename: str = Field(min_length=1)
    data_b64: str = Field(min_length=1)
    title: str | None = None


class KbSearchRequest(BaseModel):
    query: str = Field(min_length=1)
    top_k: int = Field(default=5, gt=0, le=20)


class ConversationTitleRequest(BaseModel):
    title: str = Field(min_length=1)


class ConversationTruncateRequest(BaseModel):
    turn_index: int = Field(ge=0)


class MyInstructionsRequest(BaseModel):
    content: str = Field(default="", max_length=4000)


class MCPServerRequest(BaseModel):
    name: str = Field(min_length=1, max_length=40, pattern=r"^[a-zA-Z0-9_-]+$")
    command: str = Field(min_length=1)
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True


class ResumeRequest(BaseModel):
    tool_call_id: str = Field(min_length=1)
    approved: bool
    include_debug: bool = False


class SmtpSettingsRequest(BaseModel):
    host: str | None = None
    port: int | None = Field(default=None, gt=0, lt=65536)
    user: str | None = None
    password: str | None = None
    from_addr: str | None = None
    default_notify_email: str | None = None


class SmtpTestRequest(BaseModel):
    to: str = Field(min_length=1)


class ScheduleRequest(BaseModel):
    message: str = Field(min_length=1)
    time: str = Field(min_length=1)
    date: str | None = None
    notify_email: str | None = None


MAX_UPLOAD_BYTES = 100 * 1024 * 1024


@app.get("/")
def index() -> FileResponse:
    return FileResponse(INDEX, headers={"Cache-Control": "no-store"})


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"ok": True, "model": settings.model, "api_base": settings.base_url}


@app.get("/api/version")
def get_version() -> dict[str, str]:
    return {"version": APP_VERSION}


@app.get("/api/settings")
def get_settings() -> dict[str, Any]:
    return {
        "base_url": settings.base_url,
        "model": settings.model,
        "system_prompt": settings.system_prompt,
        "default_system_prompt": DEFAULT_SYSTEM_PROMPT,
        "context_window_tokens": settings.context_window_tokens,
        "api_key_set": bool(settings.api_key and settings.api_key != "not-required"),
        "request_timeout": settings.request_timeout,
    }


@app.post("/api/settings")
def update_settings(request: LLMSettingsRequest) -> dict[str, Any]:
    if not request.base_url and not request.model and not request.system_prompt and not request.context_window_tokens and not request.api_key and not request.request_timeout:
        raise HTTPException(status_code=400, detail="至少要提供一項要更新的設定")
    settings.update_llm(
        base_url=request.base_url,
        model=request.model,
        system_prompt=request.system_prompt,
        context_window_tokens=request.context_window_tokens,
        api_key=request.api_key,
        request_timeout=request.request_timeout,
    )
    return {
        "ok": True,
        "base_url": settings.base_url,
        "model": settings.model,
        "system_prompt": settings.system_prompt,
        "context_window_tokens": settings.context_window_tokens,
        "api_key_set": bool(settings.api_key and settings.api_key != "not-required"),
        "request_timeout": settings.request_timeout,
    }


@app.get("/api/behavior-settings")
def get_behavior_settings() -> dict[str, Any]:
    return {
        "max_iterations": settings.max_iterations,
        "shell_timeout": settings.shell_timeout,
        "web_search_results": settings.web_search_results,
        "fetch_max_chars": settings.fetch_max_chars,
        "memory_char_budget": settings.memory_char_budget,
        "compact_trigger_ratio": settings.compact_trigger_ratio,
        "compact_keep_recent_turns": settings.compact_keep_recent_turns,
    }


@app.post("/api/behavior-settings")
def update_behavior_settings(request: BehaviorSettingsRequest) -> dict[str, Any]:
    settings.update_behavior(
        max_iterations=request.max_iterations,
        shell_timeout=request.shell_timeout,
        web_search_results=request.web_search_results,
        fetch_max_chars=request.fetch_max_chars,
        memory_char_budget=request.memory_char_budget,
        compact_trigger_ratio=request.compact_trigger_ratio,
        compact_keep_recent_turns=request.compact_keep_recent_turns,
    )
    return get_behavior_settings()


@app.get("/api/subagent-settings")
def get_subagent_settings() -> dict[str, Any]:
    return {
        "max_iterations": settings.subagent_max_iterations,
        "max_seconds": settings.subagent_max_seconds,
        "max_per_turn": settings.subagent_max_per_turn,
        "enabled": settings.subagent_enabled,
        "concurrency": settings.subagent_concurrency,
    }


@app.post("/api/subagent-settings")
def update_subagent_settings(request: SubagentSettingsRequest) -> dict[str, Any]:
    settings.update_subagent(
        max_iterations=request.max_iterations,
        max_seconds=request.max_seconds,
        max_per_turn=request.max_per_turn,
        enabled=request.enabled,
        concurrency=request.concurrency,
    )
    return get_subagent_settings()


@app.get("/api/embedding-settings")
def get_embedding_settings() -> dict[str, Any]:
    return {
        "base_url": settings.embedding_base_url,
        "model": settings.embedding_model,
        "api_key_set": bool(settings.embedding_api_key),
    }


@app.post("/api/embedding-settings")
def update_embedding_settings(request: EmbeddingSettingsRequest) -> dict[str, Any]:
    settings.update_embedding(
        base_url=request.base_url,
        model=request.model,
        api_key=request.api_key,
    )
    return get_embedding_settings()


@app.get("/api/kb-settings")
def get_kb_settings() -> dict[str, Any]:
    return {"chunk_size": settings.kb_chunk_size, "chunk_overlap": settings.kb_chunk_overlap}


@app.post("/api/kb-settings")
def update_kb_settings(request: KbSettingsRequest) -> dict[str, Any]:
    settings.update_kb(chunk_size=request.chunk_size, chunk_overlap=request.chunk_overlap)
    return get_kb_settings()


@app.get("/api/kb/documents")
def kb_list_documents() -> list[dict[str, Any]]:
    return agent.tools.kb.list_documents()


@app.post("/api/kb/documents")
def kb_upload_document(request: KbUploadRequest, http_request: Request) -> dict[str, Any]:
    user_id = _current_user_id(http_request)
    if len(request.data_b64) > ((MAX_UPLOAD_BYTES + 2) // 3) * 4 + 4:
        raise HTTPException(status_code=413, detail="檔案過大")
    data = _decode_upload(request.data_b64)
    directory = agent.tools.workspace_root(user_id) / "kb_sources"
    directory.mkdir(parents=True, exist_ok=True)
    filename = os.path.basename(request.filename.replace("\\", "/")).replace("..", "").strip()
    if not filename or filename in {".", ".."}:
        raise HTTPException(status_code=400, detail="無效的檔名")
    filename = _dedupe_name(directory, filename)
    target = directory / filename
    target.write_bytes(data)
    try:
        return agent.tools.kb_add_document(
            agent.tools.workspace_display_rel(target, user_id), title=request.title, user_id=user_id,
        )
    except ValueError as exc:
        target.unlink(missing_ok=True)
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        target.unlink(missing_ok=True)
        raise HTTPException(status_code=502, detail=f"{type(exc).__name__}: {exc}") from exc


@app.delete("/api/kb/documents/{document_id}")
def kb_remove_document(document_id: int) -> dict[str, bool]:
    if not agent.tools.kb.remove_document(document_id):
        raise HTTPException(status_code=404, detail="找不到這份文件")
    return {"ok": True}


@app.post("/api/kb/search")
def kb_search(request: KbSearchRequest, http_request: Request) -> dict[str, Any]:
    user_id = _current_user_id(http_request)
    try:
        return agent.tools.kb_search(request.query, top_k=request.top_k, user_id=user_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"{type(exc).__name__}: {exc}") from exc


@app.get("/api/skills")
def skills() -> list[dict[str, str]]:
    return list_skills(settings.allowed_root / SKILLS_DIRNAME)


@app.get("/api/memory")
def list_memory(http_request: Request, target: str | None = None) -> dict[str, Any]:
    user_id = _current_user_id(http_request)
    notes = agent.tools.memory.list(target, user_id)
    return {
        "notes": notes,
        "budget": settings.memory_char_budget,
        "totals": {t: agent.tools.memory.total_chars(t, user_id) for t in ("memory", "user")},
    }


@app.delete("/api/memory/{note_id}")
def delete_memory(note_id: int, http_request: Request) -> dict[str, bool]:
    agent.tools.memory.remove(note_id)
    return {"ok": True}


@app.get("/api/my-instructions")
def get_my_instructions(http_request: Request) -> dict[str, Any]:
    """Self-managed personal system-prompt addendum, separate from the base persona/
    system prompt in config.py -- see user_prefs_store.py."""
    return {"content": agent.user_prefs.get(_current_user_id(http_request)), "max_chars": 4000}


@app.post("/api/my-instructions")
def set_my_instructions(request: MyInstructionsRequest, http_request: Request) -> dict[str, Any]:
    try:
        content = agent.user_prefs.set(_current_user_id(http_request), request.content)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "content": content}


@app.get("/api/models")
def list_models(base_url: str, for_embedding: bool = False) -> list[str]:
    api_key = settings.embedding_api_key if for_embedding and settings.embedding_api_key else settings.api_key
    try:
        return LLMClient.fetch_models(base_url, api_key)
    except requests.exceptions.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"無法取得模型清單：{type(exc).__name__}: {exc}") from exc
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=502, detail=f"模型清單格式無法解析：{exc}") from exc


@app.get("/api/context-window")
def get_context_window(base_url: str, model: str) -> dict[str, Any]:
    """Best-effort auto-detect for the Settings UI -- see LLMClient.fetch_context_length()
    for why this only ever works against an Ollama backend and silently returns None (never
    a 502) for anything else, so the frontend can just no-op instead of showing an error."""
    return {"context_length": LLMClient.fetch_context_length(base_url, model)}


@app.get("/api/mcp/servers")
def mcp_servers() -> list[dict[str, Any]]:
    return agent.tools.mcp.list_servers()


@app.post("/api/mcp/servers")
def add_mcp_server(request: MCPServerRequest) -> dict[str, Any]:
    agent.tools.mcp.add_server(request.name, request.command, request.args, request.env, request.enabled)
    return {"ok": True}


@app.delete("/api/mcp/servers/{name}")
def remove_mcp_server(name: str) -> dict[str, bool]:
    agent.tools.mcp.remove_server(name)
    return {"ok": True}


@app.post("/api/mcp/servers/{name}/reconnect")
def reconnect_mcp_server(name: str) -> dict[str, Any]:
    try:
        agent.tools.mcp.reconnect(name)
    except KeyError:
        raise HTTPException(status_code=404, detail="找不到這個 MCP server")
    return {"ok": True}


@app.get("/api/smtp-settings")
def get_smtp_settings() -> dict[str, Any]:
    return {
        "host": settings.smtp_host,
        "port": settings.smtp_port,
        "user": settings.smtp_user,
        "from_addr": settings.smtp_from,
        "default_notify_email": settings.default_notify_email,
        "password_set": bool(settings.smtp_password),
    }


@app.post("/api/smtp-settings")
def update_smtp_settings(request: SmtpSettingsRequest) -> dict[str, Any]:
    settings.update_smtp(
        host=request.host,
        port=request.port,
        user=request.user,
        password=request.password,
        from_addr=request.from_addr,
        default_notify_email=request.default_notify_email,
    )
    return get_smtp_settings()


@app.post("/api/smtp-settings/test")
def test_smtp_settings(request: SmtpTestRequest) -> dict[str, Any]:
    try:
        send_email(settings, request.to, "[LiteAgent] SMTP 測試信", "這是一封測試信，如果你收到了，代表 LiteAgent 的 Email 通知設定正確。")
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"{type(exc).__name__}: {exc}") from exc
    return {"ok": True}


@app.get("/api/schedules")
def list_schedules(http_request: Request) -> list[dict[str, Any]]:
    return agent.tools.schedules.list_all(owner_id=_current_user_id(http_request))


@app.post("/api/schedules")
def create_schedule(request: ScheduleRequest, http_request: Request) -> dict[str, Any]:
    try:
        return agent.tools.schedules.create(
            message=request.message, time=request.time, date=request.date, notify_email=request.notify_email,
            owner_id=_current_user_id(http_request), assisted_by_email=_assisted_by(http_request),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.delete("/api/schedules/{schedule_id}")
def cancel_schedule(schedule_id: str, http_request: Request) -> dict[str, bool]:
    if not agent.tools.schedules.cancel(schedule_id):
        raise HTTPException(status_code=404, detail="找不到這個排程，或已經被取消")
    return {"ok": True}


@app.get("/api/conversations")
def conversations(http_request: Request) -> list[dict[str, Any]]:
    return agent.store.list_conversations(_current_user_id(http_request))


@app.get("/api/conversations/{conversation_id}")
def history(conversation_id: str, http_request: Request) -> list[dict[str, Any]]:
    _require_conversation_access(conversation_id, http_request)
    return agent.store.display_history(conversation_id)


@app.get("/api/conversations/{conversation_id}/plan")
def get_plan(conversation_id: str, http_request: Request) -> dict[str, Any]:
    _require_conversation_access(conversation_id, http_request)
    return {"steps": agent.store.get_plan(conversation_id) or []}


@app.patch("/api/conversations/{conversation_id}/title")
def set_conversation_title(conversation_id: str, request: ConversationTitleRequest, http_request: Request) -> dict[str, Any]:
    _require_conversation_access(conversation_id, http_request)
    title = request.title.strip()[:60]
    if not title:
        raise HTTPException(status_code=422, detail="標題不可為空白")
    agent.store.set_title(conversation_id, title)
    return {"ok": True, "title": title}


@app.delete("/api/conversations/{conversation_id}")
def delete_conversation(conversation_id: str, http_request: Request) -> dict[str, bool]:
    _require_conversation_access(conversation_id, http_request)
    agent.store.delete_conversation(conversation_id)
    return {"ok": True}


@app.post("/api/conversations/{conversation_id}/truncate")
def truncate_conversation(conversation_id: str, request: ConversationTruncateRequest, http_request: Request) -> dict[str, bool]:
    """Used by the "edit and resend" UI feature -- physically deletes the turn_index-th user
    message and everything after it, so the pre-edit exchange doesn't linger in storage and
    reappear after a reload."""
    _require_conversation_access(conversation_id, http_request)
    agent.store.delete_from_user_turn(conversation_id, request.turn_index)
    return {"ok": True}


@app.get("/api/files")
def files(http_request: Request) -> list[dict[str, Any]]:
    user_id = _current_user_id(http_request)
    uploads = agent.tools.safe_path("uploads", user_id)
    if not uploads.is_dir():
        return []
    items = []
    for candidate in uploads.rglob("*"):
        if not candidate.is_file():
            continue
        rel = agent.tools.workspace_display_rel(candidate, user_id)
        stat = candidate.stat()
        items.append({
            "name": candidate.name,
            "rel": rel,
            "size": stat.st_size,
            "mtime": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
        })
    return sorted(items, key=lambda item: item["mtime"], reverse=True)


@app.get("/api/files/download/{rel:path}")
def download_file(rel: str, http_request: Request) -> FileResponse:
    requested = rel.lstrip("/")
    try:
        target = agent.tools.safe_path(requested, _current_user_id(http_request))
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail="只允許讀取工作目錄內的檔案") from exc
    if not target.is_file():
        raise HTTPException(status_code=404, detail="檔案不存在")
    return FileResponse(target, filename=target.name)


def _fs_safe_dir(rel: str, user_id: str | None) -> Path:
    """Resolve+validate a relative directory path within the workspace sandbox."""
    try:
        target = agent.tools.safe_path(rel, user_id) if rel else agent.tools.workspace_root(user_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail="路徑超出工作目錄範圍") from exc
    return target


def _decode_upload(data_b64: str) -> bytes:
    if len(data_b64) > ((MAX_UPLOAD_BYTES + 2) // 3) * 4 + 4:
        raise HTTPException(status_code=400, detail="檔案過大(上限 100MB)")
    try:
        data = base64.b64decode(data_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=400, detail="無效的 base64 檔案內容") from exc
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=400, detail="檔案過大(上限 100MB)")
    return data


def _dedupe_name(directory: Path, filename: str) -> str:
    """Avoid silently overwriting an existing file: foo.txt -> foo (2).txt, etc."""
    if not (directory / filename).exists():
        return filename
    stem, suffix = Path(filename).stem, Path(filename).suffix
    n = 2
    while (directory / f"{stem} ({n}){suffix}").exists():
        n += 1
    return f"{stem} ({n}){suffix}"


@app.get("/api/fs/list")
def fs_list(http_request: Request, path: str = "") -> list[dict[str, Any]]:
    user_id = _current_user_id(http_request)
    directory = _fs_safe_dir(path, user_id)
    if not directory.is_dir():
        raise HTTPException(status_code=404, detail="資料夾不存在")
    items = []
    for entry in directory.iterdir():
        rel = agent.tools.workspace_display_rel(entry, user_id)
        stat = entry.stat()
        items.append({
            "name": entry.name,
            "rel": rel,
            "type": "dir" if entry.is_dir() else "file",
            "size": stat.st_size if entry.is_file() else None,
            "mtime": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
        })
    items.sort(key=lambda item: (item["type"] != "dir", item["name"].lower()))
    return items


class MkdirRequest(BaseModel):
    path: str = Field(min_length=1)


@app.post("/api/fs/mkdir")
def fs_mkdir(request: MkdirRequest, http_request: Request) -> dict[str, str]:
    user_id = _current_user_id(http_request)
    try:
        target = agent.tools.safe_path(request.path, user_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail="路徑超出工作目錄範圍") from exc
    if target.exists():
        raise HTTPException(status_code=400, detail="已存在同名項目")
    target.mkdir(parents=True)
    return {"rel": agent.tools.workspace_display_rel(target, user_id)}


class FsUploadRequest(BaseModel):
    dir: str = ""
    filename: str = Field(min_length=1)
    data_b64: str = Field(min_length=1)


@app.post("/api/fs/upload")
def fs_upload(request: FsUploadRequest, http_request: Request) -> dict[str, str]:
    user_id = _current_user_id(http_request)
    directory = _fs_safe_dir(request.dir, user_id)
    directory.mkdir(parents=True, exist_ok=True)
    filename = os.path.basename(request.filename.replace("\\", "/")).replace("..", "").strip()
    if not filename or filename in {".", ".."}:
        raise HTTPException(status_code=400, detail="無效的檔名")
    data = _decode_upload(request.data_b64)
    filename = _dedupe_name(directory, filename)
    target = directory / filename
    target.write_bytes(data)
    return {"rel": agent.tools.workspace_display_rel(target, user_id)}


@app.delete("/api/fs/{rel:path}")
def fs_delete(rel: str, http_request: Request) -> dict[str, bool]:
    requested = rel.lstrip("/")
    if not requested:
        raise HTTPException(status_code=400, detail="不可刪除工作目錄根目錄")
    user_id = _current_user_id(http_request)
    try:
        target = agent.tools.safe_path(requested, user_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail="路徑超出工作目錄範圍") from exc
    if not target.exists():
        raise HTTPException(status_code=404, detail="項目不存在")
    if target.is_dir():
        shutil.rmtree(target)
    else:
        target.unlink()
    return {"ok": True}


@app.post("/api/upload")
def upload(request: UploadRequest, http_request: Request) -> dict[str, str]:
    user_id = _current_user_id(http_request)
    # Treat both slash styles as separators before basename on POSIX.
    filename = os.path.basename(request.filename.replace("\\", "/")).replace("..", "").strip()
    if not filename or filename in {".", ".."}:
        raise HTTPException(status_code=400, detail="無效的檔名")
    # Reject clearly oversized base64 before allocating the decoded buffer.
    if len(request.data_b64) > ((MAX_UPLOAD_BYTES + 2) // 3) * 4 + 4:
        raise HTTPException(status_code=400, detail="檔案過大(上限 100MB)")
    try:
        data = base64.b64decode(request.data_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=400, detail="無效的 base64 檔案內容") from exc
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=400, detail="檔案過大(上限 100MB)")
    rel = f"uploads/{uuid.uuid4().hex[:8]}_{filename}"
    target = agent.tools.safe_path(rel, user_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return {"rel": rel}


def _vision_content(message: str, image_rels: list[str], user_id: str | None = None) -> Any:
    """Build OpenAI-style multimodal content (text + inline base64 images) for
    the current turn only; images are not persisted back into conversation
    history (see Agent.chat/run_stream's llm_content parameter)."""
    if not image_rels:
        return None
    uploads = agent.tools.safe_path("uploads", user_id)
    parts: list[dict[str, Any]] = [{"type": "text", "text": message}]
    for rel in image_rels:
        try:
            target = agent.tools.safe_path(rel, user_id)
        except PermissionError:
            continue
        if uploads != target and uploads not in target.parents:
            continue
        if not target.is_file():
            continue
        mime = mimetypes.guess_type(target.name)[0] or ""
        if not mime.startswith("image/"):
            continue
        b64 = base64.b64encode(target.read_bytes()).decode("ascii")
        parts.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
    return parts if len(parts) > 1 else None


@app.post("/api/chat")
def chat(request: ChatRequest, http_request: Request) -> dict[str, Any]:
    if request.conversation_id:
        _require_conversation_access(request.conversation_id, http_request)
    user_id = _current_user_id(http_request)
    try:
        llm_content = _vision_content(request.message, request.images, user_id)
        result = agent.run(
            request.message, request.conversation_id, llm_content=llm_content,
            user_id=user_id, assisted_by=_assisted_by(http_request),
            ui_lang=normalize_lang(http_request.headers.get("X-UI-Lang")),
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Agent request failed: {type(exc).__name__}: {exc}") from exc
    response: dict[str, Any] = {
        "conversation_id": result.conversation_id,
        "content": result.content,
        "max_iterations_reached": result.max_iterations_reached,
        "duration_s": result.duration_s,
        "usage": result.usage,
    }
    if request.include_debug:
        response.update(events=result.events, reasoning=result.reasoning)
    return response


@app.post("/api/chat/stream")
def chat_stream(request: ChatRequest, http_request: Request) -> StreamingResponse:
    if request.conversation_id:
        _require_conversation_access(request.conversation_id, http_request)
    user_id = _current_user_id(http_request)
    assisted_by = _assisted_by(http_request)
    ui_lang = normalize_lang(http_request.headers.get("X-UI-Lang"))

    def event_source():
        try:
            llm_content = _vision_content(request.message, request.images, user_id)
            for event in agent.run_stream(request.message, request.conversation_id, llm_content=llm_content, user_id=user_id, assisted_by=assisted_by, ui_lang=ui_lang):
                if event["type"] in {"subagent_start", "subagent_end"}:
                    pass  # always forwarded regardless of the debug toggle
                elif not request.include_debug and event["type"] in {
                    "reasoning_delta", "tool_call_delta", "tool_start", "tool_result", "usage"
                }:
                    continue
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        except Exception as exc:
            error = {"type": "error", "error": f"Agent request failed: {type(exc).__name__}: {exc}"}
            yield f"data: {json.dumps(error, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/conversations/{conversation_id}/resume")
def resume_conversation(conversation_id: str, request: ResumeRequest, http_request: Request) -> StreamingResponse:
    """Continue a run that paused on a `confirm_required` event (a risky tool call awaiting
    human approval), with the user's approve/deny decision for that call."""
    _require_conversation_access(conversation_id, http_request)
    user_id = _current_user_id(http_request)
    assisted_by = _assisted_by(http_request)
    ui_lang = normalize_lang(http_request.headers.get("X-UI-Lang"))

    def event_source():
        try:
            decisions = {request.tool_call_id: request.approved}
            for event in agent.resume_stream(conversation_id, decisions, user_id=user_id, assisted_by=assisted_by, ui_lang=ui_lang):
                if event["type"] in {"subagent_start", "subagent_end"}:
                    pass  # always forwarded regardless of the debug toggle
                elif not request.include_debug and event["type"] in {
                    "reasoning_delta", "tool_call_delta", "tool_start", "tool_result", "usage"
                }:
                    continue
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        except Exception as exc:
            error = {"type": "error", "error": f"Agent request failed: {type(exc).__name__}: {exc}"}
            yield f"data: {json.dumps(error, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
