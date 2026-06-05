from __future__ import annotations

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
from pathlib import Path
from typing import Literal
from urllib.parse import quote, urlparse

from curl_cffi import CurlMime
from curl_cffi import requests as curl_requests
from fastapi import File, Form, Header, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from fastapi import FastAPI
from pydantic import BaseModel, Field

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
CONFIG_FILE = BASE_DIR / "config.json"
GATEWAY_KEY_FILE = DATA_DIR / "image_gateway.key"
GATEWAY_LOG_FILE = DATA_DIR / "image_gateway.log"
DEFAULT_UPSTREAM_URL = "http://127.0.0.1:3000"
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


def _clean(value: object) -> str:
    return str(value or "").strip()


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


def _upstream_url() -> str:
    return _clean(os.getenv("IMAGE_GATEWAY_UPSTREAM_URL")) or DEFAULT_UPSTREAM_URL


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


def _public_base_url(request: Request) -> str:
    return (_clean(os.getenv("IMAGE_GATEWAY_PUBLIC_BASE_URL")) or str(request.base_url)).rstrip("/")


def _request_timeout() -> float | None:
    raw = _clean(os.getenv("IMAGE_GATEWAY_REQUEST_TIMEOUT_SECONDS"))
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def _safe_edit_prompt_enabled() -> bool:
    raw = _clean(os.getenv("IMAGE_GATEWAY_SAFE_EDIT_PROMPT")).lower()
    if not raw:
        return True
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


def _gateway_image_url(request: Request, image_path: str) -> str:
    encoded_path = quote(image_path, safe="/")
    sig = _image_signature(image_path)
    return f"{_public_base_url(request)}/images/{encoded_path}?sig={sig}"


def _rewrite_url(request: Request, value: str) -> str:
    parsed = urlparse(value)
    path = parsed.path or value
    match = re.match(r"^/images/(.+)$", path)
    if not match:
        return value
    return _gateway_image_url(request, _normalize_image_path(match.group(1)))


def _rewrite_result_urls(request: Request, value: object) -> object:
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


def _post_upstream_edit(form: dict[str, object], images: list[tuple[bytes, str, str]], request_id: str = "") -> dict:
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


@app.get("/health")
async def health() -> dict[str, bool]:
    return {"ok": True}


@app.post("/generate")
async def generate(
    body: GatewayImageRequest,
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="x-api-key"),
) -> dict:
    _require_gateway_key(authorization, x_api_key)
    request_id = secrets.token_hex(6)
    request_started = time.perf_counter()
    _log_gateway_event(request_id, "generate_received", model=body.model, size=body.size, n=body.n)
    attempts = 0
    last_error = ""
    while body.max_attempts == 0 or attempts < body.max_attempts:
        attempts += 1
        try:
            attempt_started = time.perf_counter()
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
            rewritten = _rewrite_result_urls(request, result)
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


@app.post("/edit")
async def edit(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="x-api-key"),
    image: list[UploadFile] | None = File(default=None),
    image_list: list[UploadFile] | None = File(default=None, alias="image[]"),
    prompt: str = Form(...),
    model: str = Form(default="gpt-image-2"),
    n: int = Form(default=1),
    size: str | None = Form(default=None),
    response_format: Literal["url", "b64_json"] = Form(default="url"),
    quality: str = Form(default="high"),
    max_attempts: int = Form(default=0),
    retry_delay_seconds: float = Form(default=5.0),
) -> dict:
    _require_gateway_key(authorization, x_api_key)
    request_id = secrets.token_hex(6)
    request_started = time.perf_counter()
    read_started = time.perf_counter()
    prompt = _clean(prompt)
    if not prompt:
        raise HTTPException(status_code=400, detail={"error": "prompt is required"})
    upstream_prompt, prompt_rewrites = _rewrite_edit_prompt_for_safety(prompt)
    if n < 1 or n > 4:
        raise HTTPException(status_code=400, detail={"error": "n must be between 1 and 4"})
    max_attempts = max(0, min(10000, int(max_attempts or 0)))
    retry_delay_seconds = max(1.0, min(300.0, float(retry_delay_seconds or 5.0)))

    uploads = [*(image or []), *(image_list or [])]
    if not uploads:
        raise HTTPException(status_code=400, detail={"error": "image file is required"})
    images: list[tuple[bytes, str, str]] = []
    for upload in uploads:
        data = await upload.read()
        if not data:
            raise HTTPException(status_code=400, detail={"error": "image file is empty"})
        images.append((data, upload.filename or "image.png", upload.content_type or "image/png"))
    _log_gateway_event(
        request_id,
        "edit_received",
        model=_clean(model) or "gpt-image-2",
        size=_clean(size),
        quality=_clean(quality) or "high",
        n=n,
        image_count=len(images),
        upload_bytes=sum(len(item[0]) for item in images),
        read_upload_ms=round((time.perf_counter() - read_started) * 1000, 1),
        safe_prompt_rewritten=bool(prompt_rewrites),
        safe_prompt_rewrite_count=len(prompt_rewrites),
    )

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
            _log_gateway_event(request_id, "edit_attempt_start", attempt=attempts)
            result = await _run_upstream_with_slot(
                request_id,
                "edit",
                attempts,
                _post_upstream_edit,
                form,
                images,
                request_id,
            )
            data = result.get("data")
            if not isinstance(data, list) or not data:
                raise RuntimeError(str(result.get("message") or "image edit returned no image data"))
            rewritten = _rewrite_result_urls(request, result)
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
