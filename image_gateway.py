from __future__ import annotations

import base64
import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import asyncio
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote, unquote, urlparse

from curl_cffi import CurlMime
from curl_cffi import requests as curl_requests
from fastapi import File, Form, Header, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from fastapi import FastAPI
from pydantic import BaseModel, Field

from services.nana_flow_service import NanaFlowError
from services.nana_flow_service import edit_nana_images
from services.nana_flow_service import generate_nana_images
from services.nana_flow_service import is_nana_model

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
CONFIG_FILE = BASE_DIR / "config.json"
ENV_FILE = BASE_DIR / ".env"
GATEWAY_KEY_FILE = DATA_DIR / "image_gateway.key"
GATEWAY_LOG_FILE = DATA_DIR / "image_gateway.log"
DEFAULT_UPSTREAM_URL = "http://127.0.0.1:3300"
_LOG_LOCK = threading.Lock()


class GatewayImageRequest(BaseModel):
    prompt: str = Field(..., min_length=1)
    model: str = "gpt-image-2"
    n: int = Field(default=1, ge=1, le=4)
    size: str | None = None
    quality: str = "high"
    response_format: Literal["url", "b64_json"] = "url"
    max_attempts: int = Field(default=0, ge=0, le=10000)
    retry_delay_seconds: float = Field(default=5.0, ge=1.0, le=300.0)
    session_id: str = "default"
    reference_cache_scope: str = "global"


@dataclass(slots=True)
class ImageJob:
    job_id: str
    kind: Literal["generate", "edit"]
    public_base_url: str
    payload: dict[str, Any]
    queue_name: str = "gpt"
    status: Literal["queued", "running", "succeeded", "failed", "canceled"] = "queued"
    stage: str = "queued"
    provider: str = ""
    model: str = ""
    requested_model: str = ""
    session_id: str = "default"
    reference_cache_scope: str = "global"
    requested_count: int = 1
    ready_count: int = 0
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    updated_at: float = field(default_factory=time.time)
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None


def _clean(value: object) -> str:
    return str(value or "").strip().lstrip("\ufeff")


def _normalize_session_id(value: object) -> str:
    text = _clean(value).lower()
    text = re.sub(r"[^a-z0-9._-]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-._")
    return text[:64] or "default"


def _normalize_reference_cache_scope(value: object) -> str:
    text = _clean(value).lower()
    text = re.sub(r"[^a-z0-9._-]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-._")
    return text[:96] or "global"


def _log_gateway_event(request_id: str, stage: str, **fields: object) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    item = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "request_id": request_id,
        "stage": stage,
        **fields,
    }
    with _LOG_LOCK:
        with GATEWAY_LOG_FILE.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n")


def _read_config_auth_key() -> str:
    if not CONFIG_FILE.exists():
        return ""
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return ""
    if not isinstance(data, dict):
        return ""
    return _clean(data.get("auth-key"))


def _read_env_file_value(name: str) -> str:
    if not ENV_FILE.exists():
        return ""
    try:
        lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
    except Exception:
        return ""
    prefix = name.upper()
    for line in lines:
        item = line.strip()
        if not item or item.startswith("#") or "=" not in item:
            continue
        key, value = item.split("=", 1)
        if key.strip().upper() == prefix:
            return _clean(value)
    return ""


def _upstream_url() -> str:
    configured = _clean(os.getenv("IMAGE_GATEWAY_UPSTREAM_URL"))
    if configured:
        return configured
    host_port = _clean(os.getenv("CHATGPT2API_HOST_PORT")) or _read_env_file_value("CHATGPT2API_HOST_PORT")
    if host_port:
        return f"http://127.0.0.1:{host_port}"
    return DEFAULT_UPSTREAM_URL


def _upstream_auth_key() -> str:
    return (
        _clean(os.getenv("IMAGE_GATEWAY_UPSTREAM_AUTH_KEY"))
        or _clean(os.getenv("CHATGPT2API_AUTH_KEY"))
        or _read_config_auth_key()
    )


def _load_or_create_gateway_key() -> str:
    configured = _clean(os.getenv("IMAGE_GATEWAY_API_KEY"))
    if configured:
        return configured
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if GATEWAY_KEY_FILE.exists():
        existing = _clean(GATEWAY_KEY_FILE.read_text(encoding="utf-8"))
        if existing:
            return existing
    raw_key = "igw-" + secrets.token_urlsafe(32)
    GATEWAY_KEY_FILE.write_text(raw_key + "\n", encoding="utf-8")
    return raw_key


GATEWAY_API_KEY = _load_or_create_gateway_key()


def _gateway_key() -> str:
    return GATEWAY_API_KEY


def _image_secret() -> str:
    return _clean(os.getenv("IMAGE_GATEWAY_IMAGE_SECRET")) or _gateway_key()


def _public_base_url(request: Request | str) -> str:
    if isinstance(request, str):
        fallback = request
    else:
        fallback = str(request.base_url)
    return (_clean(os.getenv("IMAGE_GATEWAY_PUBLIC_BASE_URL")) or fallback).rstrip("/")


def _request_timeout() -> float | None:
    raw = _clean(os.getenv("IMAGE_GATEWAY_REQUEST_TIMEOUT_SECONDS"))
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def _nana_cdp_url() -> str:
    env_url = _clean(os.getenv("NANA_CDP_URL"))
    if env_url:
        return env_url
    cdp_url_file = Path(_clean(os.getenv("NANA_CDP_URL_FILE")) or DATA_DIR / "nana_cdp_url.txt")
    if cdp_url_file.exists():
        file_url = _clean(cdp_url_file.read_text(encoding="utf-8"))
        if file_url:
            return file_url
    return "http://127.0.0.1:9223"


def _nana_project_url() -> str:
    return (
        _clean(os.getenv("NANA_PROJECT_URL"))
        or "https://labs.google/fx/zh/tools/flow/project/aae032c7-27b7-4ee2-85b1-944b48351403"
    )


def _nana_timeout_seconds() -> float:
    raw = _clean(os.getenv("NANA_GENERATE_TIMEOUT_SECONDS"))
    if not raw:
        return 120.0
    try:
        value = float(raw)
    except ValueError:
        return 120.0
    return max(30.0, min(value, 300.0))


def _nana_default_max_attempts() -> int:
    raw = _clean(os.getenv("IMAGE_GATEWAY_NANA_DEFAULT_MAX_ATTEMPTS"))
    if not raw:
        return 2
    try:
        value = int(raw)
    except ValueError:
        return 2
    return max(1, min(value, 5))


def _nana_transient_min_attempts() -> int:
    raw = _clean(os.getenv("IMAGE_GATEWAY_NANA_TRANSIENT_MIN_ATTEMPTS"))
    if not raw:
        return 2
    try:
        value = int(raw)
    except ValueError:
        return 2
    return max(1, min(value, 5))


def _nana_fallback_enabled() -> bool:
    raw = _clean(os.getenv("IMAGE_GATEWAY_NANA_FALLBACK")).lower()
    if not raw:
        return True
    return raw not in {"0", "false", "no", "off"}


def _nana_fallback_model() -> str:
    return _clean(os.getenv("IMAGE_GATEWAY_NANA_FALLBACK_MODEL")) or "nano-banana-pro"


def _normalize_gateway_model(model: object) -> str:
    value = _clean(model)
    key = value.lower().replace("_", "-").replace(" ", "-")
    key = re.sub(r"-+", "-", key)
    aliases = {
        "": "gpt-image-2",
        "image2": "gpt-image-2",
        "gpt": "gpt-image-2",
        "gpt-image": "gpt-image-2",
        "chatgpt-image": "gpt-image-2",
        "codex-image2": "codex-gpt-image-2",
        "codex-gpt-image": "codex-gpt-image-2",
    }
    return aliases.get(key, value or "gpt-image-2")


def _is_gpt_image_model(model: object) -> bool:
    normalized = _normalize_gateway_model(model).lower()
    return normalized in {"gpt-image-2", "codex-gpt-image-2"}


def _should_fallback_to_nana(model: object, status_code: int, error: object) -> bool:
    if not _nana_fallback_enabled() or not _is_gpt_image_model(model):
        return False
    text = str(error or "").lower()
    if status_code in {401, 403}:
        return False
    markers = (
        "no available image quota",
        "insufficient_quota",
        "no available account",
        "no available chat",
        "no account",
        "quota",
        "账号池为空",
        "无可用",
        "无号",
        "额度不足",
    )
    return any(marker in text for marker in markers)


def _is_nana_transient_retry_error(error: object) -> bool:
    text = str(error or "").lower()
    return (
        "recaptcha" in text
        or "public_error_unusual_activity" in text
        or "unusual_activity" in text
        or "异常活动" in text
        or "帮助中心" in text
    )


def _nana_retry_delay_seconds(error: object, retry_delay_seconds: float) -> float:
    if _is_nana_transient_retry_error(error):
        return max(float(retry_delay_seconds or 5.0), 20.0)
    return float(retry_delay_seconds or 5.0)


def _safe_edit_prompt_enabled() -> bool:
    raw = _clean(os.getenv("IMAGE_GATEWAY_SAFE_EDIT_PROMPT")).lower()
    if not raw:
        return False
    return raw not in {"0", "false", "no", "off"}


def _max_concurrent_requests() -> int:
    raw = _clean(os.getenv("IMAGE_GATEWAY_MAX_CONCURRENT_REQUESTS"))
    if not raw:
        return 2
    try:
        value = int(raw)
    except ValueError:
        return 2
    return max(1, min(value, 32))


_EDIT_PROMPT_REPLACEMENTS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"\bremove\s+head\s*,\s*legs?\s*,?\s*(?:and\s+)?tail\b", re.IGNORECASE),
        "show only the central body sprite component",
    ),
    (
        re.compile(r"\binclude\s+only\s+the\s+main\s+body\s+mass\s*:\s*[^.。\n]+", re.IGNORECASE),
        "show only the central body sprite component with a clean outer silhouette",
    ),
    (
        re.compile(r"\bonly\s+the\s+main\s+body\s+mass\s*:\s*[^.。\n]+", re.IGNORECASE),
        "central body sprite component with a clean outer silhouette",
    ),
    (re.compile(r"\bwolf\s+torso\b", re.IGNORECASE), "central body sprite component"),
    (re.compile(r"\btorso\b", re.IGNORECASE), "central body sprite component"),
    (re.compile(r"\bhind\s+leg\b", re.IGNORECASE), "rear limb sprite component"),
    (re.compile(r"\bfore\s*leg\b|\bfront\s+leg\b", re.IGNORECASE), "front limb sprite component"),
    (re.compile(r"\blegs\b", re.IGNORECASE), "limb components"),
    (re.compile(r"\bleg\b", re.IGNORECASE), "limb component"),
    (re.compile(r"\bhead\b", re.IGNORECASE), "head component"),
    (re.compile(r"\btail\b", re.IGNORECASE), "tail component"),
    (re.compile(r"\bchest\b|\bribcage\b|\bbelly\b|\bhip\b|\bstifle\b|\bhock\b", re.IGNORECASE), "outer silhouette"),
    (re.compile(r"\bremove\s+([^.。,\n]+)", re.IGNORECASE), r"omit separate \1"),
    (re.compile(r"\bdo\s+not\s+include\s+([^.。,\n]+)", re.IGNORECASE), r"omit separate \1"),
)


def _rewrite_edit_prompt_for_safety(prompt: str) -> tuple[str, list[str]]:
    if not _safe_edit_prompt_enabled():
        return prompt, []
    rewritten = prompt
    changed_terms: list[str] = []
    for pattern, replacement in _EDIT_PROMPT_REPLACEMENTS:
        rewritten, count = pattern.subn(replacement, rewritten)
        if count:
            changed_terms.append(pattern.pattern)
    if not changed_terms:
        return prompt, []
    rewritten, species_count = re.subn(
        r"\b(wolf|canine|dog)\b",
        "quadruped game character",
        rewritten,
        flags=re.IGNORECASE,
    )
    if species_count:
        changed_terms.append("species_neutralized")
    prefix = (
        "Clean stylized pixel-art game asset edit. Treat the uploaded image only as "
        "style, palette, and silhouette reference for a standalone animation-rigging "
        "sprite component. Use neutral component labels and keep the asset non-realistic, "
        "with no text.\n\n"
    )
    return prefix + rewritten.strip(), changed_terms


def _require_gateway_key(authorization: str | None, x_api_key: str | None) -> None:
    expected = _gateway_key()
    scheme, _, bearer = _clean(authorization).partition(" ")
    provided = bearer if scheme.lower() == "bearer" else ""
    provided = provided or _clean(x_api_key)
    if not expected or not provided or not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail={"error": "invalid gateway key"})


GATEWAY_MAX_CONCURRENT_REQUESTS = _max_concurrent_requests()
_GATEWAY_REQUEST_SEMAPHORE = asyncio.Semaphore(GATEWAY_MAX_CONCURRENT_REQUESTS)
_IMAGE_JOBS: dict[str, ImageJob] = {}
_IMAGE_JOB_QUEUES: dict[str, asyncio.Queue[str]] = {
    "gpt": asyncio.Queue(),
    "nano": asyncio.Queue(),
}
_IMAGE_JOB_WORKER_TASKS: dict[str, list[asyncio.Task[None]]] = {
    "gpt": [],
    "nano": [],
}
_IMAGE_JOB_LOCK = asyncio.Lock()
_MAX_IMAGE_JOBS = 300


def _gpt_job_workers() -> int:
    raw = _clean(os.getenv("IMAGE_GATEWAY_GPT_JOB_WORKERS"))
    if not raw:
        return max(1, GATEWAY_MAX_CONCURRENT_REQUESTS)
    try:
        value = int(raw)
    except ValueError:
        return max(1, GATEWAY_MAX_CONCURRENT_REQUESTS)
    return max(1, min(value, 8))


def _nana_job_workers() -> int:
    raw = _clean(os.getenv("IMAGE_GATEWAY_NANA_JOB_WORKERS"))
    if not raw:
        return 2
    try:
        value = int(raw)
    except ValueError:
        return 2
    return max(1, min(value, 4))


def _job_queue_name(provider: str) -> str:
    return "nano" if _clean(provider).lower() == "nano" else "gpt"


async def _run_upstream_with_slot(
    request_id: str,
    operation: str,
    attempt: int,
    func,
    *args,
):
    queued = _GATEWAY_REQUEST_SEMAPHORE.locked()
    if queued:
        _log_gateway_event(
            request_id,
            f"{operation}_queued",
            attempt=attempt,
            max_concurrent=GATEWAY_MAX_CONCURRENT_REQUESTS,
        )
    wait_started = time.perf_counter()
    async with _GATEWAY_REQUEST_SEMAPHORE:
        queue_wait_ms = round((time.perf_counter() - wait_started) * 1000, 1)
        if queued or queue_wait_ms >= 1:
            _log_gateway_event(
                request_id,
                f"{operation}_slot_acquired",
                attempt=attempt,
                queue_wait_ms=queue_wait_ms,
                max_concurrent=GATEWAY_MAX_CONCURRENT_REQUESTS,
            )
        return await run_in_threadpool(func, *args)


def _job_timestamp(value: float | None) -> str | None:
    if value is None:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(value))


def _queued_job_ids(queue_name: str | None = None) -> list[str]:
    queues = [queue_name] if queue_name else list(_IMAGE_JOB_QUEUES)
    ids: list[str] = []
    for name in queues:
        queue = _IMAGE_JOB_QUEUES.get(name)
        if queue is None:
            continue
        for job_id in list(getattr(queue, "_queue", [])):
            job = _IMAGE_JOBS.get(job_id)
            if job is not None and job.status == "queued":
                ids.append(job_id)
    return ids


def _job_queue_meta(job: ImageJob) -> tuple[int | None, int]:
    queued = _queued_job_ids(job.queue_name)
    job_id = job.job_id
    if job_id not in queued:
        return None, 0
    ahead_count = queued.index(job_id)
    return ahead_count + 1, ahead_count


def _image_job_response(job: ImageJob, request: Request | None = None) -> dict[str, Any]:
    queue_position, ahead_count = _job_queue_meta(job) if job.status == "queued" else (None, 0)
    status_url = f"/v1/image-jobs/{job.job_id}"
    if request is not None:
        status_url = f"{_public_base_url(request)}{status_url}"
    response: dict[str, Any] = {
        "ok": job.status not in {"failed", "canceled"},
        "job_id": job.job_id,
        "status": job.status,
        "stage": job.stage,
        "provider": job.provider,
        "queue": job.queue_name,
        "model": job.model,
        "requested_model": job.requested_model,
        "session_id": job.session_id,
        "reference_cache_scope": job.reference_cache_scope,
        "requested_count": job.requested_count,
        "ready_count": job.ready_count,
        "queue_position": 0 if job.status == "running" else queue_position,
        "ahead_count": ahead_count,
        "created_at": _job_timestamp(job.created_at),
        "started_at": _job_timestamp(job.started_at),
        "finished_at": _job_timestamp(job.finished_at),
        "updated_at": _job_timestamp(job.updated_at),
        "status_url": status_url,
    }
    if job.result is not None:
        response["result"] = job.result
        response["image_urls"] = job.result.get("image_urls") or job.result.get("urls") or []
        response["data"] = job.result.get("data")
    if job.error is not None:
        response["error"] = job.error
        response["ok"] = False
    return response


def _touch_job(job: ImageJob, stage: str | None = None, **fields: Any) -> None:
    if stage:
        job.stage = stage
    for key, value in fields.items():
        if hasattr(job, key):
            setattr(job, key, value)
    job.updated_at = time.time()


def _cleanup_old_jobs() -> None:
    if len(_IMAGE_JOBS) <= _MAX_IMAGE_JOBS:
        return
    protected = set(_queued_job_ids())
    old_ids = sorted(
        (
            job_id
            for job_id, job in _IMAGE_JOBS.items()
            if job.status in {"succeeded", "failed", "canceled"} and job_id not in protected
        ),
        key=lambda item: _IMAGE_JOBS[item].finished_at or _IMAGE_JOBS[item].updated_at,
    )
    for job_id in old_ids[: max(0, len(_IMAGE_JOBS) - _MAX_IMAGE_JOBS)]:
        _IMAGE_JOBS.pop(job_id, None)


async def _enqueue_image_job(job: ImageJob) -> None:
    job.queue_name = _job_queue_name(job.provider)
    async with _IMAGE_JOB_LOCK:
        _cleanup_old_jobs()
        _IMAGE_JOBS[job.job_id] = job
        await _IMAGE_JOB_QUEUES[job.queue_name].put(job.job_id)
        _log_gateway_event(
            job.job_id,
            "job_queued",
            kind=job.kind,
            queue=job.queue_name,
            model=job.model,
            session_id=job.session_id,
            requested_count=job.requested_count,
        )


def _image_signature(image_path: str) -> str:
    return hmac.new(_image_secret().encode("utf-8"), image_path.encode("utf-8"), hashlib.sha256).hexdigest()


def _normalize_image_path(image_path: str) -> str:
    normalized = _clean(image_path).replace("\\", "/").strip("/")
    if not normalized or any(part in {"", ".", ".."} for part in normalized.split("/")):
        raise HTTPException(status_code=400, detail={"error": "invalid image path"})
    return normalized


def _verify_image_signature(image_path: str, sig: str) -> None:
    expected = _image_signature(image_path)
    if not sig or not hmac.compare_digest(sig, expected):
        raise HTTPException(status_code=403, detail={"error": "invalid image signature"})


def _gateway_image_url(request: Request | str, image_path: str) -> str:
    encoded_path = quote(image_path, safe="/")
    sig = _image_signature(image_path)
    return f"{_public_base_url(request)}/images/{encoded_path}?sig={sig}"


def _rewrite_url(request: Request | str, value: str) -> str:
    parsed = urlparse(value)
    path = parsed.path or value
    match = re.match(r"^/images/(.+)$", path)
    if not match:
        return value
    return _gateway_image_url(request, _normalize_image_path(match.group(1)))


def _rewrite_result_urls(request: Request | str, value: object) -> object:
    if isinstance(value, dict):
        rewritten = {}
        for key, item in value.items():
            if key == "url" and isinstance(item, str):
                rewritten[key] = _rewrite_url(request, item)
            elif key in {"urls", "image_urls"} and isinstance(item, list):
                rewritten[key] = [_rewrite_url(request, url) if isinstance(url, str) else url for url in item]
            else:
                rewritten[key] = _rewrite_result_urls(request, item)
        return rewritten
    if isinstance(value, list):
        return [_rewrite_result_urls(request, item) for item in value]
    return value


def _collect_image_urls(data: object) -> list[str]:
    urls: list[str] = []
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and isinstance(item.get("url"), str) and item.get("url"):
                urls.append(str(item["url"]))
    return urls


def _sanitize_error_text(value: object) -> str:
    text = _clean(value)
    auth_key = _upstream_auth_key()
    if auth_key:
        text = text.replace(auth_key, "<hidden>")
    upstream = _upstream_url().rstrip("/")
    if upstream:
        text = text.replace(upstream, "<upstream>")
    return text or "image gateway request failed"


def _max_input_image_bytes() -> int:
    raw = _clean(os.getenv("IMAGE_GATEWAY_MAX_INPUT_IMAGE_BYTES"))
    if not raw:
        return 50 * 1024 * 1024
    try:
        value = int(raw)
    except ValueError:
        return 50 * 1024 * 1024
    return max(1 * 1024 * 1024, min(value, 200 * 1024 * 1024))


def _flatten_form_values(*groups: object) -> list[str]:
    values: list[str] = []
    for group in groups:
        if group is None:
            items: list[object] = []
        elif isinstance(group, str):
            items = [group]
        elif isinstance(group, list | tuple):
            items = list(group)
        else:
            items = [group]
        for raw in items:
            item = _clean(raw)
            if not item:
                continue
            if item.startswith("["):
                try:
                    parsed = json.loads(item)
                except Exception:
                    parsed = None
                if isinstance(parsed, list):
                    values.extend(_clean(value) for value in parsed if _clean(value))
                    continue
            values.append(item)
    return values


def _filename_from_url(url: str, fallback: str) -> str:
    parsed = urlparse(url)
    name = Path(unquote(parsed.path or "")).name
    if name:
        return name
    return fallback


def _ensure_input_image_size(data: bytes, label: str) -> None:
    max_bytes = _max_input_image_bytes()
    if len(data) > max_bytes:
        raise HTTPException(
            status_code=413,
            detail={"error": f"{label} exceeds max input image size {max_bytes} bytes"},
        )


def _image_tuple_from_url(url: str, index: int) -> tuple[bytes, str, str]:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise HTTPException(status_code=400, detail={"error": "image_url must be http or https"})
    response = curl_requests.get(url, timeout=_request_timeout() or 120)
    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail={"error": f"image_url fetch failed: {response.status_code}"})
    data = response.content or b""
    if not data:
        raise HTTPException(status_code=400, detail={"error": "image_url returned empty body"})
    _ensure_input_image_size(data, f"image_url[{index}]")
    content_type = _clean(response.headers.get("content-type")).split(";", 1)[0] or mimetypes.guess_type(parsed.path)[0] or "image/png"
    if not content_type.lower().startswith("image/"):
        content_type = mimetypes.guess_type(parsed.path)[0] or "image/png"
    return data, _filename_from_url(url, f"image_url_{index}.{content_type.rsplit('/', 1)[-1] or 'png'}"), content_type


def _image_tuple_from_base64(value: str, index: int) -> tuple[bytes, str, str]:
    content_type = "image/png"
    payload = value
    if value.startswith("data:"):
        header, _, payload = value.partition(",")
        match = re.match(r"^data:([^;,]+)", header)
        if match:
            content_type = match.group(1)
    try:
        data = base64.b64decode(payload, validate=True)
    except Exception as exc:
        raise HTTPException(status_code=400, detail={"error": f"invalid base64 image at index {index}"}) from exc
    if not data:
        raise HTTPException(status_code=400, detail={"error": "base64 image is empty"})
    _ensure_input_image_size(data, f"base64 image[{index}]")
    if not content_type.lower().startswith("image/"):
        content_type = "image/png"
    extension = content_type.rsplit("/", 1)[-1] or "png"
    return data, f"image_base64_{index}.{extension}", content_type


def _is_retryable_upstream_error(status_code: int, detail: object) -> bool:
    if status_code == 429:
        return True
    if status_code < 500:
        return False
    text = str(detail or "").lower()
    if "content_policy" in text or "policy violation" in text:
        return False
    return True


def _post_upstream_generate(body: GatewayImageRequest, request_id: str = "") -> dict:
    auth_key = _upstream_auth_key()
    if not auth_key:
        raise RuntimeError("upstream auth key is not configured")
    upstream = _upstream_url().rstrip("/")
    request_kwargs = {
        "headers": {
            "Authorization": f"Bearer {auth_key}",
            "Content-Type": "application/json",
        },
        "json": {
            "prompt": body.prompt,
            "model": body.model,
            "n": body.n,
            "size": body.size,
            "quality": body.quality,
            "response_format": body.response_format,
        },
    }
    request_kwargs["timeout"] = _request_timeout()
    started = time.perf_counter()
    response = curl_requests.post(f"{upstream}/v1/images/generations", **request_kwargs)
    upstream_ms = round((time.perf_counter() - started) * 1000, 1)
    try:
        payload = response.json()
    except Exception:
        payload = {"error": response.text}
    if request_id:
        _log_gateway_event(
            request_id,
            "upstream_generate_returned",
            status_code=response.status_code,
            upstream_ms=upstream_ms,
            data_count=len(payload.get("data") or []) if isinstance(payload, dict) and isinstance(payload.get("data"), list) else 0,
        )
    if response.status_code >= 400:
        detail = payload.get("detail") if isinstance(payload, dict) else payload
        if detail is None and isinstance(payload, dict):
            detail = payload.get("error") or payload
        raise HTTPException(
            status_code=response.status_code,
            detail={
                "error": _sanitize_error_text(detail),
                "retryable": _is_retryable_upstream_error(response.status_code, detail),
            },
        )
    if not isinstance(payload, dict):
        raise RuntimeError("upstream returned invalid response")
    return payload


def _post_upstream_edit(
    form: dict[str, object],
    images: list[tuple[bytes, str, str]],
    request_id: str = "",
    masks: list[tuple[bytes, str, str]] | None = None,
) -> dict:
    auth_key = _upstream_auth_key()
    if not auth_key:
        raise RuntimeError("upstream auth key is not configured")
    upstream = _upstream_url().rstrip("/")
    multipart = CurlMime()
    for data, filename, content_type in images:
        multipart.addpart(
            name="image",
            filename=filename or "image.png",
            content_type=content_type or "image/png",
            data=data,
        )
    for data, filename, content_type in masks or []:
        multipart.addpart(
            name="mask",
            filename=filename or "mask.png",
            content_type=content_type or "image/png",
            data=data,
        )
    request_kwargs = {
        "headers": {"Authorization": f"Bearer {auth_key}"},
        "data": {key: value for key, value in form.items() if value is not None},
        "multipart": multipart,
    }
    request_kwargs["timeout"] = _request_timeout()
    started = time.perf_counter()
    try:
        response = curl_requests.post(f"{upstream}/v1/images/edits", **request_kwargs)
    finally:
        multipart.close()
    upstream_ms = round((time.perf_counter() - started) * 1000, 1)
    try:
        payload = response.json()
    except Exception:
        payload = {"error": response.text}
    if request_id:
        _log_gateway_event(
            request_id,
            "upstream_edit_returned",
            status_code=response.status_code,
            upstream_ms=upstream_ms,
            image_count=len(images),
            mask_count=len(masks or []),
            upload_bytes=sum(len(item[0]) for item in images),
            data_count=len(payload.get("data") or []) if isinstance(payload, dict) and isinstance(payload.get("data"), list) else 0,
        )
    if response.status_code >= 400:
        detail = payload.get("detail") if isinstance(payload, dict) else payload
        if detail is None and isinstance(payload, dict):
            detail = payload.get("error") or payload
        raise HTTPException(
            status_code=response.status_code,
            detail={
                "error": _sanitize_error_text(detail),
                "retryable": _is_retryable_upstream_error(response.status_code, detail),
            },
        )
    if not isinstance(payload, dict):
        raise RuntimeError("upstream returned invalid response")
    return payload


def _fetch_upstream_image(image_path: str) -> tuple[bytes, str]:
    upstream = _upstream_url().rstrip("/")
    encoded_path = quote(image_path, safe="/")
    response = curl_requests.get(f"{upstream}/images/{encoded_path}", timeout=_request_timeout() or 60)
    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail={"error": "image not found"})
    content_type = _clean(response.headers.get("content-type")) or "image/png"
    return response.content, content_type


def _fetch_local_image(image_path: str) -> tuple[bytes, str] | None:
    normalized_path = _normalize_image_path(image_path)
    root = (DATA_DIR / "images").resolve()
    path = (root / normalized_path).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        return None
    if not path.is_file():
        return None
    content_type = mimetypes.guess_type(path.name)[0] or "image/png"
    return path.read_bytes(), content_type


def _fetch_gateway_image(image_path: str) -> tuple[bytes, str]:
    local = _fetch_local_image(image_path)
    if local is not None:
        return local
    return _fetch_upstream_image(image_path)


async def _generate_nana_response(body: GatewayImageRequest, request: Request | str, request_id: str, request_started: float) -> dict:
    attempts = 0
    max_attempts = body.max_attempts if body.max_attempts > 0 else _nana_default_max_attempts()
    last_error = ""
    session_id = _normalize_session_id(body.session_id)
    reference_cache_scope = _normalize_reference_cache_scope(body.reference_cache_scope)
    while attempts < max_attempts:
        attempts += 1
        try:
            attempt_started = time.perf_counter()
            _log_gateway_event(
                request_id,
                "nano_generate_attempt_start",
                attempt=attempts,
                max_attempts=max_attempts,
                model=body.model,
                size=body.size,
                n=body.n,
                session_id=session_id,
                reference_cache_scope=reference_cache_scope,
            )
            result = await generate_nana_images(
                prompt=body.prompt,
                model=body.model,
                n=body.n,
                size=body.size,
                cdp_url=_nana_cdp_url(),
                project_url=_nana_project_url(),
                timeout_seconds=_nana_timeout_seconds(),
                session_id=session_id,
                reference_cache_scope=reference_cache_scope,
            )
            urls = [_gateway_image_url(request, image.relative_path) for image in result.images]
            if body.response_format == "b64_json":
                data = [
                    {"b64_json": base64.b64encode((DATA_DIR / "images" / image.relative_path).read_bytes()).decode("ascii")}
                    for image in result.images
                ]
                urls = []
            else:
                data = [{"url": url} for url in urls]
            response = {
                "created": int(time.time()),
                "data": data,
                "ok": True,
                "attempts": attempts,
                "model": body.model,
                "requested_model": body.model,
                "provider": "nano",
                "session_id": session_id,
                "reference_cache_scope": reference_cache_scope,
                "size": body.size,
                "quality": body.quality,
                "urls": urls,
                "image_urls": urls,
                "nano": {
                    "model_label": result.model_label,
                    "session_id": result.session_id,
                    "reference_cache_scope": result.reference_cache_scope,
                    "aspect_ratio": result.aspect_ratio,
                    "requested_n": result.requested_n,
                    "actual_new_media_count": result.actual_new_media_count,
                    "elapsed_seconds": result.elapsed_seconds,
                    "reference_cache": {
                        "hits": result.reference_cache_hits,
                        "uploads": result.reference_upload_count,
                        "stale": result.reference_cache_stale,
                        "media_names": result.reference_media_names,
                    },
                    "reference_inputs": result.reference_inputs,
                    "images": [
                        {
                            "path": image.relative_path,
                            "media_name": image.media_name,
                            "width": image.width,
                            "height": image.height,
                            "content_type": image.content_type,
                            "source": image.source,
                            "session_id": image.session_id,
                        }
                        for image in result.images
                    ],
                },
            }
            _log_gateway_event(
                request_id,
                "nano_generate_return",
                attempts=attempts,
                attempt_ms=round((time.perf_counter() - attempt_started) * 1000, 1),
                total_ms=round((time.perf_counter() - request_started) * 1000, 1),
                url_count=len(urls),
                image_count=len(result.images),
                sources=[image.source for image in result.images],
                session_id=session_id,
                reference_cache_scope=reference_cache_scope,
                reference_cache_hits=result.reference_cache_hits,
                reference_upload_count=result.reference_upload_count,
                reference_cache_stale=result.reference_cache_stale,
            )
            return response
        except NanaFlowError as exc:
            last_error = _sanitize_error_text(exc)
            _log_gateway_event(
                request_id,
                "nano_generate_attempt_error",
                attempt=attempts,
                status_code=exc.status_code,
                retryable=exc.retryable,
                max_attempts=max_attempts,
                error=last_error[:300],
            )
            if exc.retryable and _is_nana_transient_retry_error(last_error):
                max_attempts = max(max_attempts, _nana_transient_min_attempts())
            if not exc.retryable or attempts >= max_attempts:
                raise HTTPException(
                    status_code=exc.status_code,
                    detail={"error": last_error, "attempts": attempts, "retryable": exc.retryable},
                ) from exc
        except Exception as exc:
            last_error = _sanitize_error_text(exc)
            _log_gateway_event(
                request_id,
                "nano_generate_attempt_error",
                attempt=attempts,
                status_code=502,
                retryable=True,
                max_attempts=max_attempts,
                error=last_error[:300],
            )
            if _is_nana_transient_retry_error(last_error):
                max_attempts = max(max_attempts, _nana_transient_min_attempts())
            if attempts >= max_attempts:
                raise HTTPException(
                    status_code=502,
                    detail={"error": last_error, "attempts": attempts, "retryable": True},
                ) from exc
        await asyncio.sleep(_nana_retry_delay_seconds(last_error, body.retry_delay_seconds))

    raise HTTPException(status_code=502, detail={"error": last_error, "attempts": attempts, "retryable": True})


async def _edit_nana_response(
    *,
    prompt: str,
    model: str,
    n: int,
    size: str | None,
    response_format: Literal["url", "b64_json"],
    max_attempts: int,
    retry_delay_seconds: float,
    images: list[tuple[bytes, str, str]],
    request: Request | str,
    request_id: str,
    request_started: float,
    session_id: str = "default",
    reference_cache_scope: str = "global",
) -> dict:
    attempts = 0
    max_attempts = max_attempts if max_attempts > 0 else _nana_default_max_attempts()
    last_error = ""
    session_id = _normalize_session_id(session_id)
    reference_cache_scope = _normalize_reference_cache_scope(reference_cache_scope)
    while attempts < max_attempts:
        attempts += 1
        try:
            attempt_started = time.perf_counter()
            _log_gateway_event(
                request_id,
                "nano_edit_attempt_start",
                attempt=attempts,
                max_attempts=max_attempts,
                model=model,
                size=size,
                n=n,
                session_id=session_id,
                reference_cache_scope=reference_cache_scope,
                image_count=len(images),
                upload_bytes=sum(len(item[0]) for item in images),
            )
            result = await edit_nana_images(
                prompt=prompt,
                images=images,
                model=model,
                n=n,
                size=size,
                cdp_url=_nana_cdp_url(),
                project_url=_nana_project_url(),
                timeout_seconds=_nana_timeout_seconds(),
                session_id=session_id,
                reference_cache_scope=reference_cache_scope,
            )
            urls = [_gateway_image_url(request, image.relative_path) for image in result.images]
            if response_format == "b64_json":
                data = [
                    {"b64_json": base64.b64encode((DATA_DIR / "images" / image.relative_path).read_bytes()).decode("ascii")}
                    for image in result.images
                ]
                urls = []
            else:
                data = [{"url": url} for url in urls]
            response = {
                "created": int(time.time()),
                "data": data,
                "ok": True,
                "attempts": attempts,
                "model": model,
                "requested_model": model,
                "provider": "nano",
                "session_id": session_id,
                "reference_cache_scope": reference_cache_scope,
                "size": size,
                "urls": urls,
                "image_urls": urls,
                "nano": {
                    "model_label": result.model_label,
                    "session_id": result.session_id,
                    "reference_cache_scope": result.reference_cache_scope,
                    "aspect_ratio": result.aspect_ratio,
                    "requested_n": result.requested_n,
                    "actual_new_media_count": result.actual_new_media_count,
                    "elapsed_seconds": result.elapsed_seconds,
                    "reference_cache": {
                        "hits": result.reference_cache_hits,
                        "uploads": result.reference_upload_count,
                        "stale": result.reference_cache_stale,
                        "media_names": result.reference_media_names,
                    },
                    "reference_inputs": result.reference_inputs,
                    "images": [
                        {
                            "path": image.relative_path,
                            "media_name": image.media_name,
                            "width": image.width,
                            "height": image.height,
                            "content_type": image.content_type,
                            "source": image.source,
                            "session_id": image.session_id,
                        }
                        for image in result.images
                    ],
                },
            }
            _log_gateway_event(
                request_id,
                "nano_edit_return",
                attempts=attempts,
                attempt_ms=round((time.perf_counter() - attempt_started) * 1000, 1),
                total_ms=round((time.perf_counter() - request_started) * 1000, 1),
                url_count=len(urls),
                image_count=len(result.images),
                sources=[image.source for image in result.images],
                session_id=session_id,
                reference_cache_scope=reference_cache_scope,
                reference_cache_hits=result.reference_cache_hits,
                reference_upload_count=result.reference_upload_count,
                reference_cache_stale=result.reference_cache_stale,
            )
            return response
        except NanaFlowError as exc:
            last_error = _sanitize_error_text(exc)
            _log_gateway_event(
                request_id,
                "nano_edit_attempt_error",
                attempt=attempts,
                status_code=exc.status_code,
                retryable=exc.retryable,
                max_attempts=max_attempts,
                error=last_error[:300],
            )
            if exc.retryable and _is_nana_transient_retry_error(last_error):
                max_attempts = max(max_attempts, _nana_transient_min_attempts())
            if not exc.retryable or attempts >= max_attempts:
                raise HTTPException(
                    status_code=exc.status_code,
                    detail={"error": last_error, "attempts": attempts, "retryable": exc.retryable},
                ) from exc
        except Exception as exc:
            last_error = _sanitize_error_text(exc)
            _log_gateway_event(
                request_id,
                "nano_edit_attempt_error",
                attempt=attempts,
                status_code=502,
                retryable=True,
                max_attempts=max_attempts,
                error=last_error[:300],
            )
            if _is_nana_transient_retry_error(last_error):
                max_attempts = max(max_attempts, _nana_transient_min_attempts())
            if attempts >= max_attempts:
                raise HTTPException(
                    status_code=502,
                    detail={"error": last_error, "attempts": attempts, "retryable": True},
                ) from exc
        await asyncio.sleep(_nana_retry_delay_seconds(last_error, retry_delay_seconds))

    raise HTTPException(status_code=502, detail={"error": last_error, "attempts": attempts, "retryable": True})


async def _execute_generate_job(job: ImageJob) -> dict[str, Any]:
    body = job.payload["body"]
    requested_model = job.payload["requested_model"]
    session_id = _normalize_session_id(job.payload.get("session_id") or body.session_id)
    reference_cache_scope = _normalize_reference_cache_scope(job.payload.get("reference_cache_scope") or body.reference_cache_scope)
    request_id = job.job_id
    request_started = time.perf_counter()
    _touch_job(job, "routing", provider="nano" if is_nana_model(body.model) else "gpt", model=body.model, requested_model=requested_model, session_id=session_id, reference_cache_scope=reference_cache_scope)
    if is_nana_model(body.model):
        _touch_job(job, "nano_generating", provider="nano")
        nana_body = body.model_copy(update={"session_id": session_id, "reference_cache_scope": reference_cache_scope})
        result = await _generate_nana_response(nana_body, job.public_base_url, request_id, request_started)
        result["requested_model"] = requested_model
        result["session_id"] = session_id
        result["reference_cache_scope"] = reference_cache_scope
        return result

    attempts = 0
    last_error = ""
    while body.max_attempts == 0 or attempts < body.max_attempts:
        attempts += 1
        try:
            attempt_started = time.perf_counter()
            _touch_job(job, "gpt_generating", provider="gpt")
            _log_gateway_event(request_id, "generate_attempt_start", attempt=attempts)
            result = await _run_upstream_with_slot(
                request_id,
                "generate",
                attempts,
                _post_upstream_generate,
                body,
                request_id,
            )
            data = result.get("data")
            if not isinstance(data, list) or not data:
                raise RuntimeError(str(result.get("message") or "image generation returned no image data"))
            rewritten = _rewrite_result_urls(job.public_base_url, result)
            if not isinstance(rewritten, dict):
                _log_gateway_event(
                    request_id,
                    "generate_return",
                    attempts=attempts,
                    attempt_ms=round((time.perf_counter() - attempt_started) * 1000, 1),
                    total_ms=round((time.perf_counter() - request_started) * 1000, 1),
                )
                return {"ok": True, "attempts": attempts, "result": rewritten}
            urls = _collect_image_urls(rewritten.get("data"))
            rewritten["ok"] = True
            rewritten["attempts"] = attempts
            rewritten["model"] = body.model
            rewritten["requested_model"] = requested_model
            rewritten["provider"] = "gpt"
            rewritten["session_id"] = session_id
            rewritten["reference_cache_scope"] = reference_cache_scope
            rewritten["size"] = body.size
            rewritten["urls"] = urls
            rewritten["image_urls"] = urls
            _log_gateway_event(
                request_id,
                "generate_return",
                attempts=attempts,
                attempt_ms=round((time.perf_counter() - attempt_started) * 1000, 1),
                total_ms=round((time.perf_counter() - request_started) * 1000, 1),
                url_count=len(urls),
            )
            return rewritten
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, dict) else {"error": str(exc.detail)}
            last_error = str(detail.get("error") or exc.detail or "")
            retryable = bool(detail.get("retryable", _is_retryable_upstream_error(int(exc.status_code), detail)))
            _log_gateway_event(request_id, "generate_attempt_error", attempt=attempts, status_code=exc.status_code, retryable=retryable, error=last_error[:300])
            if _should_fallback_to_nana(body.model, int(exc.status_code), last_error):
                fallback_model = _nana_fallback_model()
                if is_nana_model(fallback_model):
                    _touch_job(job, "fallback_to_nano", provider="nano", model=fallback_model)
                    _log_gateway_event(
                        request_id,
                        "generate_fallback_to_nano",
                        from_model=body.model,
                        to_model=fallback_model,
                        reason=last_error[:300],
                    )
                    fallback_body = body.model_copy(update={"model": fallback_model, "max_attempts": max(1, int(body.max_attempts or 1)), "session_id": session_id, "reference_cache_scope": reference_cache_scope})
                    fallback_result = await _generate_nana_response(fallback_body, job.public_base_url, request_id, request_started)
                    fallback_result["requested_model"] = requested_model
                    fallback_result["session_id"] = session_id
                    fallback_result["reference_cache_scope"] = reference_cache_scope
                    fallback_result["fallback"] = {
                        "from_model": requested_model,
                        "routed_from_model": body.model,
                        "to_model": fallback_model,
                        "reason": last_error,
                    }
                    return fallback_result
            if not retryable or (body.max_attempts and attempts >= body.max_attempts):
                raise HTTPException(
                    status_code=exc.status_code,
                    detail={"error": last_error, "attempts": attempts, "retryable": retryable},
                ) from exc
        except Exception as exc:
            last_error = _sanitize_error_text(exc)
            _log_gateway_event(request_id, "generate_attempt_error", attempt=attempts, status_code=502, retryable=True, error=last_error[:300])
            if body.max_attempts and attempts >= body.max_attempts:
                raise HTTPException(
                    status_code=502,
                    detail={"error": last_error, "attempts": attempts, "retryable": True},
                ) from exc
        await asyncio.sleep(body.retry_delay_seconds)

    raise HTTPException(status_code=502, detail={"error": last_error, "attempts": attempts, "retryable": True})


async def _execute_edit_job(job: ImageJob) -> dict[str, Any]:
    payload = job.payload
    request_id = job.job_id
    request_started = time.perf_counter()
    requested_model = payload["requested_model"]
    session_id = _normalize_session_id(payload.get("session_id"))
    reference_cache_scope = _normalize_reference_cache_scope(payload.get("reference_cache_scope"))
    model = payload["model"]
    n = payload["n"]
    size = payload["size"]
    response_format = payload["response_format"]
    max_attempts = payload["max_attempts"]
    retry_delay_seconds = payload["retry_delay_seconds"]
    images = payload["images"]
    masks = payload["masks"]
    prompt = payload["prompt"]
    upstream_prompt = payload["upstream_prompt"]
    quality = payload["quality"]

    use_nana_edit = is_nana_model(model)
    _touch_job(job, "routing", provider="nano" if use_nana_edit else "gpt", model=model, requested_model=requested_model, session_id=session_id, reference_cache_scope=reference_cache_scope)
    if use_nana_edit:
        _touch_job(job, "nano_editing", provider="nano")
        result = await _edit_nana_response(
            prompt=prompt,
            model=_clean(model) or "nano-banana",
            n=n,
            size=_clean(size),
            response_format=response_format,
            max_attempts=max_attempts,
            retry_delay_seconds=retry_delay_seconds,
            images=images,
            request=job.public_base_url,
            request_id=request_id,
            request_started=request_started,
            session_id=session_id,
            reference_cache_scope=reference_cache_scope,
        )
        result["requested_model"] = requested_model
        result["session_id"] = session_id
        result["reference_cache_scope"] = reference_cache_scope
        return result

    form = {
        "prompt": upstream_prompt,
        "model": _clean(model) or "gpt-image-2",
        "n": n,
        "size": _clean(size),
        "quality": _clean(quality) or "high",
        "response_format": response_format,
    }
    attempts = 0
    last_error = ""
    while max_attempts == 0 or attempts < max_attempts:
        attempts += 1
        try:
            attempt_started = time.perf_counter()
            _touch_job(job, "gpt_editing", provider="gpt")
            _log_gateway_event(request_id, "edit_attempt_start", attempt=attempts)
            result = await _run_upstream_with_slot(
                request_id,
                "edit",
                attempts,
                _post_upstream_edit,
                form,
                images,
                request_id,
                masks,
            )
            data = result.get("data")
            if not isinstance(data, list) or not data:
                raise RuntimeError(str(result.get("message") or "image edit returned no image data"))
            rewritten = _rewrite_result_urls(job.public_base_url, result)
            if not isinstance(rewritten, dict):
                _log_gateway_event(
                    request_id,
                    "edit_return",
                    attempts=attempts,
                    attempt_ms=round((time.perf_counter() - attempt_started) * 1000, 1),
                    total_ms=round((time.perf_counter() - request_started) * 1000, 1),
                )
                return {"ok": True, "attempts": attempts, "result": rewritten}
            urls = _collect_image_urls(rewritten.get("data"))
            rewritten["ok"] = True
            rewritten["attempts"] = attempts
            rewritten["model"] = model
            rewritten["requested_model"] = requested_model
            rewritten["provider"] = "gpt"
            rewritten["session_id"] = session_id
            rewritten["reference_cache_scope"] = reference_cache_scope
            rewritten["urls"] = urls
            rewritten["image_urls"] = urls
            _log_gateway_event(
                request_id,
                "edit_return",
                attempts=attempts,
                attempt_ms=round((time.perf_counter() - attempt_started) * 1000, 1),
                total_ms=round((time.perf_counter() - request_started) * 1000, 1),
                url_count=len(urls),
            )
            return rewritten
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, dict) else {"error": str(exc.detail)}
            last_error = str(detail.get("error") or exc.detail or "")
            retryable = bool(detail.get("retryable", _is_retryable_upstream_error(int(exc.status_code), detail)))
            _log_gateway_event(request_id, "edit_attempt_error", attempt=attempts, status_code=exc.status_code, retryable=retryable, error=last_error[:300])
            if not masks and _should_fallback_to_nana(model, int(exc.status_code), last_error):
                fallback_model = _nana_fallback_model()
                if is_nana_model(fallback_model):
                    _touch_job(job, "fallback_to_nano", provider="nano", model=fallback_model)
                    _log_gateway_event(
                        request_id,
                        "edit_fallback_to_nano",
                        from_model=requested_model,
                        routed_from_model=model,
                        to_model=fallback_model,
                        reason=last_error[:300],
                    )
                    fallback_result = await _edit_nana_response(
                        prompt=prompt,
                        model=fallback_model,
                        n=n,
                        size=_clean(size),
                        response_format=response_format,
                        max_attempts=max(1, int(max_attempts or 1)),
                        retry_delay_seconds=retry_delay_seconds,
                        images=images,
                        request=job.public_base_url,
                        request_id=request_id,
                        request_started=request_started,
                        session_id=session_id,
                        reference_cache_scope=reference_cache_scope,
                    )
                    fallback_result["requested_model"] = requested_model
                    fallback_result["session_id"] = session_id
                    fallback_result["reference_cache_scope"] = reference_cache_scope
                    fallback_result["fallback"] = {
                        "from_model": requested_model,
                        "routed_from_model": model,
                        "to_model": fallback_model,
                        "reason": last_error,
                    }
                    return fallback_result
            if not retryable or (max_attempts and attempts >= max_attempts):
                raise HTTPException(
                    status_code=exc.status_code,
                    detail={"error": last_error, "attempts": attempts, "retryable": retryable},
                ) from exc
        except Exception as exc:
            last_error = _sanitize_error_text(exc)
            _log_gateway_event(request_id, "edit_attempt_error", attempt=attempts, status_code=502, retryable=True, error=last_error[:300])
            if max_attempts and attempts >= max_attempts:
                raise HTTPException(
                    status_code=502,
                    detail={"error": last_error, "attempts": attempts, "retryable": True},
                ) from exc
        await asyncio.sleep(retry_delay_seconds)

    raise HTTPException(status_code=502, detail={"error": last_error, "attempts": attempts, "retryable": True})


async def _image_job_worker(queue_name: str) -> None:
    queue = _IMAGE_JOB_QUEUES[queue_name]
    while True:
        job_id = await queue.get()
        job = _IMAGE_JOBS.get(job_id)
        if job is None:
            queue.task_done()
            continue
        if job.status == "canceled":
            queue.task_done()
            continue
        job.status = "running"
        job.stage = "starting"
        job.started_at = time.time()
        job.updated_at = job.started_at
        _log_gateway_event(job.job_id, "job_started", kind=job.kind, queue=queue_name, model=job.model, session_id=job.session_id)
        try:
            result = await (_execute_generate_job(job) if job.kind == "generate" else _execute_edit_job(job))
            job.result = result
            job.status = "succeeded"
            job.stage = "succeeded"
            job.ready_count = len(result.get("image_urls") or result.get("urls") or [])
            job.finished_at = time.time()
            job.updated_at = job.finished_at
            _log_gateway_event(job.job_id, "job_succeeded", provider=result.get("provider"), image_count=job.ready_count, session_id=job.session_id)
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, dict) else {"error": str(exc.detail)}
            job.error = {"status_code": exc.status_code, **detail}
            job.status = "failed"
            job.stage = "failed"
            job.finished_at = time.time()
            job.updated_at = job.finished_at
            _log_gateway_event(job.job_id, "job_failed", status_code=exc.status_code, error=str(detail.get("error") or detail)[:300], session_id=job.session_id)
        except Exception as exc:
            error = _sanitize_error_text(exc)
            job.error = {"status_code": 502, "error": error, "retryable": True}
            job.status = "failed"
            job.stage = "failed"
            job.finished_at = time.time()
            job.updated_at = job.finished_at
            _log_gateway_event(job.job_id, "job_failed", status_code=502, error=error[:300], session_id=job.session_id)
        finally:
            queue.task_done()


def _ensure_image_job_worker() -> None:
    desired = {"gpt": _gpt_job_workers(), "nano": _nana_job_workers()}
    for queue_name, count in desired.items():
        tasks = [task for task in _IMAGE_JOB_WORKER_TASKS.get(queue_name, []) if not task.done()]
        while len(tasks) < count:
            tasks.append(asyncio.create_task(_image_job_worker(queue_name)))
        _IMAGE_JOB_WORKER_TASKS[queue_name] = tasks


app = FastAPI(
    title="chatgpt2api image gateway",
    version="1.0.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["POST", "GET"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def _startup_image_jobs() -> None:
    _ensure_image_job_worker()


@app.get("/health")
async def health() -> dict:
    return {
        "ok": True,
        "upstream_url": _upstream_url(),
        "routes": {
            "generate_jobs": ["/v1/image-jobs/generations"],
            "edit_jobs": ["/v1/image-jobs/edits"],
            "job_status": "/v1/image-jobs/{job_id}",
            "deprecated_sync": ["/generate", "/edit", "/v1/images/generations", "/v1/images/edits"],
            "images": "/images/{path}?sig=...",
        },
        "queues": {
            "gpt": {
                "workers": _gpt_job_workers(),
                "queued": len(_queued_job_ids("gpt")),
                "upstream_max_concurrent": GATEWAY_MAX_CONCURRENT_REQUESTS,
            },
            "nano": {
                "workers": _nana_job_workers(),
                "queued": len(_queued_job_ids("nano")),
            },
        },
        "models": {
            "default": "gpt-image-2",
            "gpt": ["gpt-image-2", "codex-gpt-image-2", "image2"],
            "nano": ["nano-banana", "nano-banana-lite", "nano-banana-pro"],
        },
        "edit_inputs": [
            "multipart image",
            "image[]",
            "image_url",
            "image_urls",
            "image_url[]",
            "image_b64",
            "image_base64",
            "b64_json",
            "mask (GPT only)",
        ],
        "nano": {
            "fallback_enabled": _nana_fallback_enabled(),
            "fallback_model": _nana_fallback_model(),
            "default_max_attempts": _nana_default_max_attempts(),
            "transient_min_attempts": _nana_transient_min_attempts(),
            "cdp_url": _nana_cdp_url(),
            "project_url": _nana_project_url(),
            "supports_mask": False,
        },
    }


@app.post("/v1/image-jobs/generations")
async def create_generate_job(
    body: GatewayImageRequest,
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="x-api-key"),
) -> dict:
    _require_gateway_key(authorization, x_api_key)
    requested_model = body.model
    session_id = _normalize_session_id(body.session_id)
    reference_cache_scope = _normalize_reference_cache_scope(body.reference_cache_scope)
    normalized_model = _normalize_gateway_model(body.model)
    if normalized_model != body.model:
        body = body.model_copy(update={"model": normalized_model})
    if session_id != body.session_id or reference_cache_scope != body.reference_cache_scope:
        body = body.model_copy(update={"session_id": session_id, "reference_cache_scope": reference_cache_scope})
    _ensure_image_job_worker()
    job = ImageJob(
        job_id="imgjob_" + secrets.token_hex(10),
        kind="generate",
        public_base_url=_public_base_url(request),
        payload={"body": body, "requested_model": requested_model, "session_id": session_id, "reference_cache_scope": reference_cache_scope},
        provider="nano" if is_nana_model(body.model) else "gpt",
        model=body.model,
        requested_model=requested_model,
        session_id=session_id,
        reference_cache_scope=reference_cache_scope,
        requested_count=body.n,
    )
    await _enqueue_image_job(job)
    return _image_job_response(job, request)


@app.post("/v1/image-jobs/edits")
async def create_edit_job(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="x-api-key"),
    image: list[UploadFile] | None = File(default=None),
    image_list: list[UploadFile] | None = File(default=None, alias="image[]"),
    mask: UploadFile | None = File(default=None),
    image_url: list[str] | None = Form(default=None),
    image_urls: list[str] | None = Form(default=None),
    image_url_list: list[str] | None = Form(default=None, alias="image_url[]"),
    image_b64: list[str] | None = Form(default=None),
    image_base64: list[str] | None = Form(default=None),
    b64_json: list[str] | None = Form(default=None),
    prompt: str = Form(...),
    model: str = Form(default="gpt-image-2"),
    n: int = Form(default=1),
    size: str | None = Form(default=None),
    response_format: Literal["url", "b64_json"] = Form(default="url"),
    quality: str = Form(default="high"),
    max_attempts: int = Form(default=0),
    retry_delay_seconds: float = Form(default=5.0),
    session_id: str = Form(default="default"),
    reference_cache_scope: str = Form(default="global"),
) -> dict:
    _require_gateway_key(authorization, x_api_key)
    request_id = secrets.token_hex(6)
    request_started = time.perf_counter()
    read_started = time.perf_counter()
    prompt = _clean(prompt)
    if not prompt:
        raise HTTPException(status_code=400, detail={"error": "prompt is required"})
    requested_model = model
    session_id = _normalize_session_id(session_id)
    reference_cache_scope = _normalize_reference_cache_scope(reference_cache_scope)
    model = _normalize_gateway_model(model)
    use_nana_edit = is_nana_model(model)
    upstream_prompt, prompt_rewrites = (prompt, []) if use_nana_edit else _rewrite_edit_prompt_for_safety(prompt)
    if n < 1 or n > 4:
        raise HTTPException(status_code=400, detail={"error": "n must be between 1 and 4"})
    max_attempts = max(0, min(10000, int(max_attempts or 0)))
    retry_delay_seconds = max(1.0, min(300.0, float(retry_delay_seconds or 5.0)))

    uploads = [*(image or []), *(image_list or [])]
    url_inputs = _flatten_form_values(image_url, image_urls, image_url_list)
    base64_inputs = _flatten_form_values(image_b64, image_base64, b64_json)
    if not uploads and not url_inputs and not base64_inputs:
        raise HTTPException(status_code=400, detail={"error": "image file is required"})
    images: list[tuple[bytes, str, str]] = []
    for upload in uploads:
        data = await upload.read()
        if not data:
            raise HTTPException(status_code=400, detail={"error": "image file is empty"})
        _ensure_input_image_size(data, upload.filename or "image")
        images.append((data, upload.filename or "image.png", upload.content_type or "image/png"))
    for index, url in enumerate(url_inputs, start=1):
        images.append(await run_in_threadpool(_image_tuple_from_url, url, index))
    for index, value in enumerate(base64_inputs, start=1):
        images.append(_image_tuple_from_base64(value, index))
    masks: list[tuple[bytes, str, str]] = []
    if mask is not None:
        data = await mask.read()
        if not data:
            raise HTTPException(status_code=400, detail={"error": "mask file is empty"})
        _ensure_input_image_size(data, mask.filename or "mask")
        masks.append((data, mask.filename or "mask.png", mask.content_type or "image/png"))
    if use_nana_edit and masks:
        raise HTTPException(status_code=400, detail={"error": "Nano Banana edit does not support mask/inpainting yet"})
    _log_gateway_event(
        request_id,
        "edit_received",
        requested_model=requested_model,
        model=_clean(model) or "gpt-image-2",
        size=_clean(size),
        quality=_clean(quality) or "high",
        n=n,
        max_attempts=max_attempts,
        retry_delay_seconds=retry_delay_seconds,
        session_id=session_id,
        reference_cache_scope=reference_cache_scope,
        image_count=len(images),
        mask_count=len(masks),
        image_url_count=len(url_inputs),
        image_b64_count=len(base64_inputs),
        upload_bytes=sum(len(item[0]) for item in images),
        read_upload_ms=round((time.perf_counter() - read_started) * 1000, 1),
        safe_prompt_rewritten=bool(prompt_rewrites),
        safe_prompt_rewrite_count=len(prompt_rewrites),
    )

    _ensure_image_job_worker()
    job = ImageJob(
        job_id="imgjob_" + secrets.token_hex(10),
        kind="edit",
        public_base_url=_public_base_url(request),
        payload={
            "prompt": prompt,
            "requested_model": requested_model,
            "session_id": session_id,
            "reference_cache_scope": reference_cache_scope,
            "model": model,
            "upstream_prompt": upstream_prompt,
            "prompt_rewrites": prompt_rewrites,
            "n": n,
            "size": _clean(size),
            "quality": _clean(quality) or "high",
            "response_format": response_format,
            "max_attempts": max_attempts,
            "retry_delay_seconds": retry_delay_seconds,
            "images": images,
            "masks": masks,
        },
        provider="nano" if use_nana_edit else "gpt",
        model=model,
        requested_model=requested_model,
        session_id=session_id,
        reference_cache_scope=reference_cache_scope,
        requested_count=n,
    )
    await _enqueue_image_job(job)
    return _image_job_response(job, request)


@app.get("/v1/image-jobs/{job_id}")
async def get_image_job(
    job_id: str,
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="x-api-key"),
) -> dict:
    _require_gateway_key(authorization, x_api_key)
    job = _IMAGE_JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail={"error": "image job not found"})
    return _image_job_response(job, request)


@app.post("/v1/image-jobs/{job_id}/cancel")
async def cancel_image_job(
    job_id: str,
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="x-api-key"),
) -> dict:
    _require_gateway_key(authorization, x_api_key)
    job = _IMAGE_JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail={"error": "image job not found"})
    if job.status == "queued":
        job.status = "canceled"
        job.stage = "canceled"
        job.finished_at = time.time()
        job.updated_at = job.finished_at
        job.error = {"status_code": 499, "error": "image job canceled"}
        _log_gateway_event(job.job_id, "job_canceled")
    elif job.status == "running":
        raise HTTPException(status_code=409, detail={"error": "running image job cannot be canceled safely yet"})
    return _image_job_response(job, request)


@app.post("/generate")
@app.post("/v1/images/generations")
async def deprecated_sync_generate(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="x-api-key"),
) -> dict:
    _require_gateway_key(authorization, x_api_key)
    raise HTTPException(
        status_code=410,
        detail={
            "error": "synchronous image endpoints have been removed; use POST /v1/image-jobs/generations and poll GET /v1/image-jobs/{job_id}",
            "job_endpoint": "/v1/image-jobs/generations",
        },
    )


@app.post("/edit")
@app.post("/v1/images/edits")
async def deprecated_sync_edit(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="x-api-key"),
) -> dict:
    _require_gateway_key(authorization, x_api_key)
    raise HTTPException(
        status_code=410,
        detail={
            "error": "synchronous image endpoints have been removed; use POST /v1/image-jobs/edits and poll GET /v1/image-jobs/{job_id}",
            "job_endpoint": "/v1/image-jobs/edits",
        },
    )


@app.get("/images/{image_path:path}")
async def get_image(image_path: str, sig: str = "") -> Response:
    normalized_path = _normalize_image_path(image_path)
    _verify_image_signature(normalized_path, sig)
    try:
        content, content_type = await run_in_threadpool(_fetch_gateway_image, normalized_path)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail={"error": _sanitize_error_text(exc)}) from exc
    return Response(content=content, media_type=content_type)


if __name__ == "__main__":
    import subprocess
    import uvicorn

    def _pause_before_exit() -> None:
        if os.name != "nt":
            return
        try:
            input("Press Enter to exit...")
        except EOFError:
            pass

    def _windows_excluded_tcp_range(port: int) -> str:
        if os.name != "nt":
            return ""
        try:
            result = subprocess.run(
                ["netsh", "interface", "ipv4", "show", "excludedportrange", "protocol=tcp"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore",
                timeout=10,
                check=False,
            )
        except Exception:
            return ""
        for line in result.stdout.splitlines():
            match = re.match(r"^\s*(\d+)\s+(\d+)", line)
            if not match:
                continue
            start = int(match.group(1))
            end = int(match.group(2))
            if start <= port <= end:
                return f"{start}-{end}"
        return ""

    port_raw = _clean(os.getenv("IMAGE_GATEWAY_PORT")) or "3200"
    try:
        port = int(port_raw)
    except ValueError:
        port = 3200
    excluded_range = _windows_excluded_tcp_range(port)
    if excluded_range:
        print(f"Windows has reserved TCP port {port} in excluded range {excluded_range}.", flush=True)
        print("Set IMAGE_GATEWAY_PORT to an available port, for example 3210, or remove the Windows port exclusion as Administrator.", flush=True)
        _pause_before_exit()
        raise SystemExit(1)
    print(f"Image gateway upstream: {_upstream_url()}", flush=True)
    print(f"Image gateway listen port: {port}", flush=True)
    print("For normal use, start-image-gateway.bat is still recommended because it sets LAN URLs and restarts old processes.", flush=True)
    uvicorn.run("image_gateway:app", host="0.0.0.0", port=port, access_log=True)
