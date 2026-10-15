from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from typing import Any, Iterator, Optional

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

load_dotenv()
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("duckai_proxy")

try:
    from duckduckgo_search import DDGS
except ImportError:  # package name changed in newer releases
    from ddgs import DDGS  # type: ignore[no-redef]

APP_TITLE = "DuckDuckGo AI OpenAI-compatible API"
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "").strip()
DB_PATH = os.getenv("SQLITE_PATH", "api_keys.db")
BYPASS_API_KEYS = os.getenv("IGNPRE_API_KEYS", "False").strip().lower() in {"true", "1", "yes"}
REQUEST_TIMEOUT = max(1.0, min(float(os.getenv("UPSTREAM_TIMEOUT_SECONDS", "45")), 180.0))
MAX_RETRIES = max(0, min(int(os.getenv("UPSTREAM_MAX_RETRIES", "1")), 3))
RETRY_BACKOFF = max(0.05, min(float(os.getenv("UPSTREAM_RETRY_BACKOFF_SECONDS", "0.5")), 5.0))
MAX_CONCURRENT_UPSTREAM = max(1, min(int(os.getenv("MAX_CONCURRENT_UPSTREAM", "8")), 64))
DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "gpt-4o-mini")
# The legacy DDGS chat adapter supports a fixed set of model selectors. This registry
# must not advertise newer Duck.ai models until the upstream transport supports them.
MODEL_IDS = list(dict.fromkeys(
    item.strip() for item in os.getenv(
        "DUCKAI_MODELS",
        "gpt-4o-mini,llama-3.3-70b,claude-3-haiku,o3-mini,mistral-small-3",
    ).split(",") if item.strip()
))
if DEFAULT_MODEL not in MODEL_IDS:
    MODEL_IDS.insert(0, DEFAULT_MODEL)

upstream_slots = asyncio.Semaphore(MAX_CONCURRENT_UPSTREAM)
app = FastAPI(title=APP_TITLE, version="2.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[item.strip() for item in os.getenv("CORS_ORIGINS", "*").split(",") if item.strip()],
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

def _write_request_log(method: str, path: str, status_code: int, latency_ms: int, api_key_prefix: Optional[str]) -> None:
    if BYPASS_API_KEYS:
        return
    conn = _db_connection()
    try:
        cur = conn.cursor()
        placeholder = "%s" if DATABASE_URL else "?"
        cur.execute(
            f"INSERT INTO request_logs (method, path, status_code, latency_ms, api_key_prefix) VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder})",
            (method, path, status_code, latency_ms, api_key_prefix),
        )
        conn.commit()
    finally:
        conn.close()


@app.middleware("http")
async def capture_request_metrics(request: Request, call_next):
    started = time.monotonic()
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    prefix = token[:8] if token else None
    try:
        response = await call_next(request)
    except Exception:
        if request.url.path.startswith("/v1/"):
            try:
                await run_in_threadpool(_write_request_log, request.method, request.url.path, 500,
                                        int((time.monotonic() - started) * 1000), prefix)
            except Exception:
                logger.exception("Could not record failed request")
        raise
    if request.url.path.startswith("/v1/") or request.url.path.startswith("/chat/completions"):
        try:
            await run_in_threadpool(_write_request_log, request.method, request.url.path, response.status_code,
                                    int((time.monotonic() - started) * 1000), prefix)
        except Exception:
            logger.exception("Could not record API request")
    return response


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="ignore")
    role: str
    content: str


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    model: str = Field(default=DEFAULT_MODEL, min_length=1, max_length=200)
    messages: list[ChatMessage] = Field(min_length=1, max_length=200)
    stream: bool = False
    temperature: Optional[float] = Field(default=None, ge=0, le=2)
    max_tokens: Optional[int] = Field(default=None, ge=1, le=32768)
    user: Optional[str] = None


class ApiKeyCreate(BaseModel):
    description: Optional[str] = Field(default=None, max_length=500)


def _db_connection():
    if DATABASE_URL:
        import psycopg2
        return psycopg2.connect(DATABASE_URL, connect_timeout=5, sslmode="require")
    conn = sqlite3.connect(DB_PATH, timeout=5)
    return conn


def init_db() -> None:
    conn = _db_connection()
    try:
        cur = conn.cursor()
        if DATABASE_URL:
            cur.execute("""CREATE TABLE IF NOT EXISTS openai_api_keys (
                id SERIAL PRIMARY KEY, key TEXT UNIQUE NOT NULL,
                description TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        else:
            cur.execute("""CREATE TABLE IF NOT EXISTS openai_api_keys (
                id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT UNIQUE NOT NULL,
                description TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        if DATABASE_URL:
            cur.execute("""CREATE TABLE IF NOT EXISTS request_logs (
                id BIGSERIAL PRIMARY KEY, method TEXT NOT NULL, path TEXT NOT NULL,
                status_code INTEGER NOT NULL, latency_ms INTEGER NOT NULL,
                api_key_prefix TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
            cur.execute("""CREATE TABLE IF NOT EXISTS model_registry (
                public_id TEXT PRIMARY KEY, upstream_id TEXT NOT NULL,
                label TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1)""")
        else:
            cur.execute("""CREATE TABLE IF NOT EXISTS request_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT, method TEXT NOT NULL, path TEXT NOT NULL,
                status_code INTEGER NOT NULL, latency_ms INTEGER NOT NULL,
                api_key_prefix TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
            cur.execute("""CREATE TABLE IF NOT EXISTS model_registry (
                public_id TEXT PRIMARY KEY, upstream_id TEXT NOT NULL,
                label TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1)""")
        cur.execute("SELECT COUNT(*) FROM model_registry")
        if cur.fetchone()[0] == 0:
            placeholder = "%s" if DATABASE_URL else "?"
            for model_id in MODEL_IDS:
                cur.execute(
                    f"INSERT INTO model_registry (public_id, upstream_id, label, enabled) VALUES ({placeholder}, {placeholder}, {placeholder}, 1)",
                    (model_id, model_id, model_id),
                )
        conn.commit()
    finally:
        conn.close()


@app.on_event("startup")
async def startup() -> None:
    if not BYPASS_API_KEYS:
        await run_in_threadpool(init_db)


def _bearer_token(authorization: Optional[str]) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Bearer authorization")
    token = authorization[7:].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Missing or invalid Bearer authorization")
    return token


async def get_admin_key(authorization: Optional[str] = Header(default=None)) -> str:
    if BYPASS_API_KEYS:
        return ""
    if not ADMIN_TOKEN:
        raise HTTPException(status_code=503, detail="Admin endpoints are disabled: ADMIN_TOKEN is not configured")
    token = _bearer_token(authorization)
    if not secrets.compare_digest(token, ADMIN_TOKEN):
        raise HTTPException(status_code=403, detail="Invalid admin token")
    return token


async def get_api_key(authorization: Optional[str] = Header(default=None)) -> str:
    if BYPASS_API_KEYS:
        return ""
    token = _bearer_token(authorization)
    def check_key() -> bool:
        conn = _db_connection()
        try:
            cur = conn.cursor()
            placeholder = "%s" if DATABASE_URL else "?"
            cur.execute(f"SELECT id FROM openai_api_keys WHERE key = {placeholder}", (token,))
            return cur.fetchone() is not None
        finally:
            conn.close()
    if not await run_in_threadpool(check_key):
        raise HTTPException(status_code=401, detail="Invalid API key")
    return token


def _error(message: str, status: int, error_type: str = "upstream_error", code: Optional[str] = None) -> HTTPException:
    body: dict[str, Any] = {"error": {"message": message, "type": error_type}}
    if code:
        body["error"]["code"] = code
    return HTTPException(status_code=status, detail=body["error"])


def _is_challenge_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    markers = (
        "captcha", "human verification", "verify you are human", "challenge page",
        "unusual traffic", "bot detection", "security check", "automated requests",
    )
    return any(marker in text for marker in markers)


def _is_retryable(exc: BaseException) -> bool:
    if _is_challenge_error(exc):
        return False
    text = str(exc).lower()
    transient = ("timeout", "timed out", "temporarily unavailable", "connection reset",
                 "connection aborted", "502", "503", "504", "429", "rate limit")
    return isinstance(exc, (TimeoutError, ConnectionError, OSError)) or any(s in text for s in transient)


def _conversation_prompt(messages: list[ChatMessage]) -> str:
    # DDGS.chat accepts a single prompt, so preserve prior turns as plain text.
    # System/developer instructions are included as context, not passed as API controls.
    parts = []
    for message in messages:
        content = message.content.strip()
        if not content:
            continue
        role = message.role.lower()
        if role not in {"system", "developer", "user", "assistant"}:
            raise HTTPException(status_code=400, detail=f"Unsupported message role: {message.role}")
        label = {"system": "System", "developer": "Developer", "user": "User", "assistant": "Assistant"}[role]
        parts.append(f"{label}: {content}")
    if not parts:
        raise HTTPException(status_code=400, detail="messages must contain non-empty content")
    parts.append("Assistant:")
    return "\n\n".join(parts)


def _call_chat(prompt: str, model: str) -> str:
    last_exc: Optional[BaseException] = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            result = DDGS().chat(prompt, model=model)
            if not isinstance(result, str) or not result.strip():
                raise RuntimeError("Duck.ai returned an empty response")
            return result
        except Exception as exc:
            last_exc = exc
            if _is_challenge_error(exc):
                raise RuntimeError("Duck.ai returned a CAPTCHA or security challenge. Complete it through Duck.ai directly; this proxy does not bypass challenges.") from exc
            if attempt >= MAX_RETRIES or not _is_retryable(exc):
                break
            time.sleep(RETRY_BACKOFF * (2 ** attempt))
    assert last_exc is not None
    raise last_exc


async def _chat_with_limits(prompt: str, model: str) -> str:
    async with upstream_slots:
        try:
            return await asyncio.wait_for(
                run_in_threadpool(_call_chat, prompt, model),
                timeout=REQUEST_TIMEOUT,
            )
        except asyncio.TimeoutError as exc:
            raise _error(
                f"Duck.ai request exceeded the {REQUEST_TIMEOUT:g}-second timeout.",
                504, "upstream_timeout", "upstream_timeout",
            ) from exc
        except RuntimeError as exc:
            if _is_challenge_error(exc):
                raise _error(str(exc), 503, "upstream_challenge", "captcha_or_challenge") from exc
            raise _error(str(exc), 502) from exc
        except Exception as exc:
            logger.warning("Duck.ai request failed (%s)", type(exc).__name__)
            if _is_challenge_error(exc):
                raise _error(
                    "Duck.ai requires a CAPTCHA or security challenge. Use Duck.ai directly; this proxy will not bypass it.",
                    503, "upstream_challenge", "captcha_or_challenge",
                ) from exc
            raise _error("Duck.ai request failed. Retry later if the service recovers.", 502) from exc


def _next_or_sentinel(iterator: Iterator[str]) -> Any:
    try:
        return next(iterator)
    except StopIteration:
        return _STREAM_END


_STREAM_END = object()


def _completion_id() -> str:
    return "chatcmpl-" + uuid.uuid4().hex


def _completion(model: str, content: str) -> dict[str, Any]:
    return {
        "id": _completion_id(),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
    }


@app.get("/", response_class=HTMLResponse)
async def serve_html():
    try:
        with open("website.html", "r", encoding="utf-8") as file:
            return HTMLResponse(content=file.read(), headers={"Cache-Control": "no-store"})
    except FileNotFoundError:
        return HTMLResponse("<h1>Duck.ai OpenAI-compatible API</h1><p>Use /docs, /health, or /v1/models.</p>")


@app.get("/health")
@app.get("/healthz")
async def health():
    return {"status": "ok", "service": "duckai-proxy"}


def _model_rows() -> list[dict[str, Any]]:
    if BYPASS_API_KEYS:
        return [{"public_id": item, "upstream_id": item, "label": item, "enabled": 1} for item in MODEL_IDS]
    conn = _db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT public_id, upstream_id, label, enabled FROM model_registry ORDER BY public_id")
        return [{"public_id": r[0], "upstream_id": r[1], "label": r[2], "enabled": int(r[3])} for r in cur.fetchall()]
    finally:
        conn.close()


def _resolve_model(public_id: str) -> Optional[str]:
    for row in _model_rows():
        if row["public_id"] == public_id and row["enabled"]:
            return row["upstream_id"]
    return None


@app.get("/v1/models")
@app.get("/models")
async def list_models(api_key: str = Depends(get_api_key)):
    now = int(time.time())
    return {"object": "list", "data": [
        {"id": row["public_id"], "object": "model", "created": now, "owned_by": "duckduckgo"}
        for row in _model_rows() if row["enabled"]
    ]}


@app.post("/v1/chat/completions")
@app.post("/chat/completions")
async def chat_completions(request: ChatCompletionRequest, api_key: str = Depends(get_api_key)):
    model = request.model.strip()
    upstream_model = _resolve_model(model)
    if upstream_model is None:
        raise _error(
            f"Unknown or disabled model '{model}'. List configured model IDs at GET /v1/models.",
            400, "invalid_request_error", "model_not_found",
        )
    prompt = _conversation_prompt(request.messages)
    if not request.stream:
        content = await _chat_with_limits(prompt, upstream_model)
        return JSONResponse(_completion(model, content))

    async def stream_events() -> Iterator[str]:
        # DDGS currently exposes a synchronous iterator. Run it in a worker and
        # enforce an overall deadline; do not retry once response bytes are sent.
        deadline = time.monotonic() + REQUEST_TIMEOUT
        try:
            iterator = await asyncio.wait_for(
                run_in_threadpool(lambda: iter(DDGS().chat_yield(prompt, model=upstream_model))),
                timeout=min(REQUEST_TIMEOUT, 10.0),
            )
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    error_chunk = {"error": {"message": "Duck.ai stream timed out.", "type": "upstream_timeout", "code": "upstream_timeout"}}
                    yield f"data: {json.dumps(error_chunk)}\n\n"
                    yield "data: [DONE]\n\n"
                    return
                token = await asyncio.wait_for(run_in_threadpool(_next_or_sentinel, iterator), timeout=remaining)
                if token is _STREAM_END:
                    break
                chunk = {
                    "id": "chatcmpl-" + request_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [{"index": 0, "delta": {"content": token}, "finish_reason": None}],
                }
                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
            final_chunk = {
                "id": "chatcmpl-" + request_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            }
            yield f"data: {json.dumps(final_chunk)}\n\n"
            yield "data: [DONE]\n\n"
        except asyncio.TimeoutError:
            yield f"data: {json.dumps({'error': {'message': 'Duck.ai stream timed out.', 'type': 'upstream_timeout', 'code': 'upstream_timeout'}})}\n\n"
            yield "data: [DONE]\n\n"
        except Exception as exc:
            if _is_challenge_error(exc):
                message = "Duck.ai requires a CAPTCHA or security challenge. This proxy will not bypass it."
                code = "captcha_or_challenge"
            else:
                message = "Duck.ai stream failed."
                code = "upstream_error"
            yield f"data: {json.dumps({'error': {'message': message, 'type': 'upstream_error', 'code': code}})}\n\n"
            yield "data: [DONE]\n\n"

    request_id = uuid.uuid4().hex
    created = int(time.time())
    return StreamingResponse(stream_events(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
    })


@app.get("/api-keys", dependencies=[Depends(get_admin_key)])
async def list_api_keys():
    if BYPASS_API_KEYS:
        return {"api_keys": []}
    def query_keys():
        conn = _db_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT id, key, description, created_at FROM openai_api_keys")
            rows = cur.fetchall()
            return [{"id": r[0], "key": r[1], "description": r[2], "created_at": str(r[3])} for r in rows]
        finally:
            conn.close()
    return {"api_keys": await run_in_threadpool(query_keys)}


@app.post("/api-keys", dependencies=[Depends(get_admin_key)])
async def create_api_key_endpoint(payload: ApiKeyCreate):
    if BYPASS_API_KEYS:
        raise HTTPException(status_code=409, detail="API key management is unavailable while IGNPRE_API_KEYS is enabled")
    new_key = secrets.token_urlsafe(32)
    def insert_key():
        conn = _db_connection()
        try:
            cur = conn.cursor()
            placeholder = "%s" if DATABASE_URL else "?"
            cur.execute(f"INSERT INTO openai_api_keys (key, description) VALUES ({placeholder}, {placeholder})",
                        (new_key, payload.description))
            conn.commit()
            return cur.lastrowid if not DATABASE_URL else None
        finally:
            conn.close()
    try:
        key_id = await run_in_threadpool(insert_key)
    except Exception as exc:
        logger.exception("Failed to create API key")
        raise HTTPException(status_code=500, detail="Could not create API key") from exc
    return {"id": key_id, "key": new_key, "description": payload.description}


@app.delete("/api-keys/{key}", dependencies=[Depends(get_admin_key)])
async def delete_api_key(key: str):
    if BYPASS_API_KEYS:
        raise HTTPException(status_code=409, detail="API key management is unavailable while IGNPRE_API_KEYS is enabled")
    def remove_key():
        conn = _db_connection()
        try:
            cur = conn.cursor()
            placeholder = "%s" if DATABASE_URL else "?"
            cur.execute(f"DELETE FROM openai_api_keys WHERE key = {placeholder}", (key,))
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()
    if not await run_in_threadpool(remove_key):
        raise HTTPException(status_code=404, detail="API key not found")
    return {"detail": "API key deleted"}


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    detail = exc.detail
    if isinstance(detail, dict) and "type" in detail:
        return JSONResponse(status_code=exc.status_code, content={"error": detail}, headers=exc.headers)
    return JSONResponse(status_code=exc.status_code, content={"detail": detail}, headers=exc.headers)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
