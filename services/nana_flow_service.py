from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page, Response, async_playwright
from playwright.async_api import Request


BASE_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = BASE_DIR / "data"
NANA_IMAGES_ROOT = DATA_DIR / "images"
NANA_REFERENCE_CACHE_PATH = DATA_DIR / "nana_reference_cache.json"
DEFAULT_CDP_URL = "http://127.0.0.1:9223"
DEFAULT_PROJECT_URL = "https://labs.google/fx/zh/tools/flow/project/aae032c7-27b7-4ee2-85b1-944b48351403"

NANA_MODEL_LABELS = {
    "nano-banana-pro": "🍌 Nano Banana Pro",
    "nano-banana": "🍌 Nano Banana 2",
    "nano-banana-2": "🍌 Nano Banana 2",
    "nana": "🍌 Nano Banana 2",
    "nana-banana": "🍌 Nano Banana 2",
    "nano-banana-lite": "🍌 Nano Banana 2 Lite",
    "nano-banana-2-lite": "🍌 Nano Banana 2 Lite",
}
NANA_MODEL_ALIASES = set(NANA_MODEL_LABELS)
NANA_ASPECTS = {
    "16:9": 16 / 9,
    "4:3": 4 / 3,
    "1:1": 1.0,
    "3:4": 3 / 4,
    "9:16": 9 / 16,
}
NANA_ASPECT_CROPS = {
    "16:9": ("crop_16_9",),
    "4:3": ("crop_4_3",),
    "1:1": ("crop_square", "crop_1_1"),
    "3:4": ("crop_3_4",),
    "9:16": ("crop_9_16",),
}

_NANA_FLOW_LOCK = asyncio.Lock()
DEFAULT_SESSION_ID = "default"
DEFAULT_REFERENCE_CACHE_SCOPE = "global"


@dataclass(slots=True)
class NanaGeneratedImage:
    relative_path: str
    media_name: str
    width: int
    height: int
    content_type: str
    source: str
    session_id: str = DEFAULT_SESSION_ID


@dataclass(slots=True)
class NanaGenerateResult:
    images: list[NanaGeneratedImage]
    model_label: str
    aspect_ratio: str | None
    requested_n: int
    actual_new_media_count: int
    elapsed_seconds: float
    reference_cache_hits: int = 0
    reference_upload_count: int = 0
    reference_cache_stale: int = 0
    reference_media_names: list[str] = field(default_factory=list)
    reference_inputs: list[dict[str, object]] = field(default_factory=list)
    session_id: str = DEFAULT_SESSION_ID
    reference_cache_scope: str = DEFAULT_REFERENCE_CACHE_SCOPE


@dataclass(slots=True)
class NanaReferenceImage:
    data: bytes
    filename: str
    upload_filename: str
    upload_token: str
    content_type: str
    sha256: str


@dataclass(slots=True)
class NanaReferenceBinding:
    media_names: list[str]
    cache_hits: int
    upload_count: int
    stale_count: int
    inputs: list[dict[str, object]] = field(default_factory=list)


class NanaFlowError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 502, retryable: bool = True) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())


def _image_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _normalize_session_id(value: object) -> str:
    text = _clean(value).lower()
    text = re.sub(r"[^a-z0-9._-]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-._")
    return text[:64] or DEFAULT_SESSION_ID


def _normalize_reference_cache_scope(value: object) -> str:
    text = _clean(value).lower()
    text = re.sub(r"[^a-z0-9._-]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-._")
    return text[:96] or DEFAULT_REFERENCE_CACHE_SCOPE


def _reference_cache_key(reference_cache_scope: str, sha256: str) -> str:
    return f"v2:{_normalize_reference_cache_scope(reference_cache_scope)}:{sha256}"


def _session_filename(session_id: str, index: int, sha256: str, filename: str, nonce: str) -> str:
    safe_session = _normalize_session_id(session_id)[:32]
    safe_name = Path(filename or f"image_{index + 1}.png").name
    safe_name = re.sub(r"[^A-Za-z0-9._ -]+", "_", safe_name).strip(" ._") or f"image_{index + 1}.png"
    return f"{safe_session}__{index + 1:02d}_{sha256[:8]}_{nonce}__{safe_name}"


def _load_reference_cache() -> dict[str, Any]:
    try:
        raw = json.loads(NANA_REFERENCE_CACHE_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"version": 1, "items": {}}
    except Exception:
        return {"version": 1, "items": {}}
    if not isinstance(raw, dict):
        return {"version": 1, "items": {}}
    items = raw.get("items")
    if not isinstance(items, dict):
        raw["items"] = {}
    raw["version"] = 1
    return raw


def _save_reference_cache(cache: dict[str, Any]) -> None:
    NANA_REFERENCE_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp_path = NANA_REFERENCE_CACHE_PATH.with_suffix(".tmp")
    temp_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    temp_path.replace(NANA_REFERENCE_CACHE_PATH)


def _reference_cache_scope_from_key(key: str) -> str:
    match = re.match(r"^v2:([^:]+):", key)
    return _normalize_reference_cache_scope(match.group(1) if match else "")


def _reference_cache_seen_value(entry: dict[str, Any]) -> str:
    return (
        _clean(entry.get("last_used_at"))
        or _clean(entry.get("last_seen_at"))
        or _clean(entry.get("first_seen_at"))
    )


def _reference_cache_lookup(reference_cache_scope: str, sha256: str) -> dict[str, Any] | None:
    cache = _load_reference_cache()
    items = cache.get("items")
    if not isinstance(items, dict):
        return None
    reference_cache_scope = _normalize_reference_cache_scope(reference_cache_scope)
    cache_key = _reference_cache_key(reference_cache_scope, sha256)
    candidates: list[tuple[int, str, dict[str, Any]]] = []
    exact_entry = items.get(cache_key)
    if isinstance(exact_entry, dict):
        candidates.append((0, cache_key, exact_entry))
    cross_scope = [
        (key, item)
        for key, item in items.items()
        if key != cache_key
        and isinstance(item, dict)
        and _clean(item.get("sha256")) == sha256
        and _clean(item.get("media_name"))
    ]
    cross_scope.sort(key=lambda item: _reference_cache_seen_value(item[1]), reverse=True)
    candidates.extend((1, key, item) for key, item in cross_scope)

    changed = False
    for _, key, entry in candidates:
        media_name = _clean(entry.get("media_name"))
        if not media_name:
            continue
        conflicts = [
            conflict_key
            for conflict_key, item in items.items()
            if isinstance(item, dict)
            and _clean(item.get("media_name")) == media_name
            and _clean(item.get("sha256")) != sha256
        ]
        if conflicts:
            for stale_key in [key, *conflicts]:
                if stale_key in items:
                    items.pop(stale_key, None)
                    changed = True
            continue
        if changed:
            _save_reference_cache(cache)
        return {
            **entry,
            "cache_key": key,
            "reference_cache_scope": _clean(entry.get("reference_cache_scope")) or _reference_cache_scope_from_key(key),
        }
    if changed:
        _save_reference_cache(cache)
    return None


def _drop_reference_cache_media_names(media_names: list[str]) -> None:
    names = {_clean(name) for name in media_names if _clean(name)}
    if not names:
        return
    cache = _load_reference_cache()
    items = cache.get("items")
    if not isinstance(items, dict):
        return
    changed = False
    for key, item in list(items.items()):
        if isinstance(item, dict) and _clean(item.get("media_name")) in names:
            items.pop(key, None)
            changed = True
    if changed:
        _save_reference_cache(cache)


def _drop_media_name_cache_conflicts(
    items: dict[str, Any],
    *,
    sha256: str,
    media_name: str,
) -> None:
    for key, item in list(items.items()):
        if not isinstance(item, dict):
            continue
        if _clean(item.get("media_name")) == media_name and _clean(item.get("sha256")) != sha256:
            items.pop(key, None)


def _record_reference_cache_item(
    *,
    data: bytes,
    filename: str,
    content_type: str,
    media_name: str,
    origin: str,
    session_id: str = DEFAULT_SESSION_ID,
    reference_cache_scope: str = DEFAULT_REFERENCE_CACHE_SCOPE,
    local_path: str = "",
    mark_used: bool = False,
) -> None:
    media_name = _clean(media_name)
    if not data or not media_name:
        return
    sha256 = _image_sha256(data)
    session_id = _normalize_session_id(session_id)
    reference_cache_scope = _normalize_reference_cache_scope(reference_cache_scope)
    cache_key = _reference_cache_key(reference_cache_scope, sha256)
    cache = _load_reference_cache()
    items = cache.setdefault("items", {})
    if not isinstance(items, dict):
        items = {}
        cache["items"] = items
    existing = items.get(cache_key)
    if not isinstance(existing, dict):
        existing = {}
    now = _now_iso()
    use_count = int(existing.get("use_count") or 0)
    if mark_used:
        use_count += 1
    _drop_media_name_cache_conflicts(
        items,
        sha256=sha256,
        media_name=media_name,
    )
    entry = {
        **existing,
        "cache_key": cache_key,
        "sha256": sha256,
        "session_id": session_id,
        "reference_cache_scope": reference_cache_scope,
        "media_name": media_name,
        "filename": filename,
        "content_type": content_type or "image/png",
        "bytes": len(data),
        "origin": origin,
        "first_seen_at": existing.get("first_seen_at") or now,
        "last_seen_at": now,
        "use_count": use_count,
    }
    if local_path:
        entry["local_path"] = local_path
    if mark_used:
        entry["last_used_at"] = now
    items[cache_key] = entry
    _save_reference_cache(cache)


def _mark_reference_cache_used(reference_cache_scope: str, session_id: str, sha256: str, media_name: str) -> None:
    cache = _load_reference_cache()
    items = cache.get("items")
    if not isinstance(items, dict):
        return
    reference_cache_scope = _normalize_reference_cache_scope(reference_cache_scope)
    entry = items.get(_reference_cache_key(reference_cache_scope, sha256))
    if not isinstance(entry, dict):
        return
    now = _now_iso()
    entry["session_id"] = _normalize_session_id(session_id)
    entry["reference_cache_scope"] = reference_cache_scope
    entry["media_name"] = media_name
    entry["last_used_at"] = now
    entry["last_seen_at"] = now
    entry["use_count"] = int(entry.get("use_count") or 0) + 1
    _save_reference_cache(cache)


def _drop_reference_cache_items(reference_cache_scope: str, sha256_values: list[str]) -> None:
    if not sha256_values:
        return
    cache = _load_reference_cache()
    items = cache.get("items")
    if not isinstance(items, dict):
        return
    changed = False
    reference_cache_scope = _normalize_reference_cache_scope(reference_cache_scope)
    for sha256 in sha256_values:
        key = _reference_cache_key(reference_cache_scope, sha256)
        if key in items:
            del items[key]
            changed = True
    if changed:
        _save_reference_cache(cache)


def _clean(value: object) -> str:
    return str(value or "").strip()


def _compact_label(value: object) -> str:
    return re.sub(r"\s+", " ", _clean(value).replace("\n", " ")).strip()


def _label_parts(value: object) -> list[str]:
    return [part.strip() for part in re.split(r"[\n|/]+", _clean(value)) if part.strip()]


def _label_part_candidates(value: object) -> list[str]:
    parts = _label_parts(value)
    compact = _compact_label(value)
    return [*parts, compact] if compact not in parts else parts


def _label_has_aspect(value: object) -> bool:
    text = _compact_label(value).lower()
    return bool(
        re.search(r"\bcrop[_-]\d+[_-]\d+\b", text)
        or "crop_square" in text
        or re.search(r"(^|[^0-9])(?:16:9|4:3|1:1|3:4|9:16)($|[^0-9])", text)
    )


def _label_matches_aspect(value: object, aspect: str) -> bool:
    aspect = _clean(aspect)
    if not aspect:
        return False
    crops = NANA_ASPECT_CROPS.get(aspect, (f"crop_{aspect.replace(':', '_')}",))
    for part in _label_part_candidates(value):
        text = part.lower()
        if text == aspect or text.endswith(f" {aspect}") or any(text == crop or crop in text for crop in crops):
            return True
    return False


def _label_has_count(value: object) -> bool:
    text = _compact_label(value).lower()
    return bool(
        re.search(r"(^|[^a-z0-9])(?:x|×)\s*[1-4]($|[^a-z0-9])", text)
        or re.search(r"(^|[^a-z0-9])[1-4]\s*(?:x|×)($|[^a-z0-9])", text)
    )


def _label_matches_count(value: object, count_label: str) -> bool:
    target = _compact_label(count_label).lower()
    if target in {"1x", "x1"}:
        patterns = (r"^1\s*(?:x|×)$", r"^(?:x|×)\s*1$")
    else:
        match = re.search(r"[1-4]", target)
        if not match:
            return False
        number = re.escape(match.group(0))
        patterns = (rf"^(?:x|×)\s*{number}$", rf"^{number}\s*(?:x|×)$")
    return any(re.search(pattern, part.lower()) for part in _label_part_candidates(value) for pattern in patterns)


def _label_has_nano_model(value: object) -> bool:
    text = _compact_label(value).lower()
    return (
        "nano banana" in text
        or "banana" in text
        or "nano" in text
        or " pro" in f" {text} "
        or " lite" in f" {text} "
    )


def _label_matches_nano_model(value: object, model_label: str) -> bool:
    labels = [part.lower() for part in _label_part_candidates(value)]
    target = _compact_label(model_label).lower()
    if target in labels:
        return True
    if "lite" in target:
        return any("lite" in label for label in labels)
    if "pro" in target:
        return any("pro" in label and "lite" not in label for label in labels)
    if "banana 2" in target:
        return any(("banana 2" in label or label in {"2", "nano banana"}) and "pro" not in label and "lite" not in label for label in labels)
    return False


def _label_is_generation_summary(value: object) -> bool:
    return _label_has_count(value) and _label_has_aspect(value) and (_label_has_nano_model(value) or "crop_" in _compact_label(value))


def is_nana_model(model: object) -> bool:
    return _normalize_model_key(model) in NANA_MODEL_ALIASES


def _normalize_model_key(model: object) -> str:
    value = _clean(model).lower().replace("_", "-").replace(" ", "-")
    value = re.sub(r"-+", "-", value)
    return value or "nano-banana"


def nana_model_label(model: object) -> str:
    key = _normalize_model_key(model)
    if key not in NANA_MODEL_LABELS:
        raise NanaFlowError(f"unsupported Nana model: {model}", status_code=400, retryable=False)
    return NANA_MODEL_LABELS[key]


def normalize_nana_aspect(size: object) -> str | None:
    value = _clean(size).lower()
    if not value or value in {"auto", "default"}:
        return None
    value = value.replace(" ", "")
    if value in NANA_ASPECTS:
        return value
    match = re.match(r"^(\d{2,5})x(\d{2,5})$", value)
    if not match:
        return None
    width = int(match.group(1))
    height = int(match.group(2))
    if width <= 0 or height <= 0:
        return None
    ratio = width / height
    return min(NANA_ASPECTS, key=lambda item: abs(NANA_ASPECTS[item] - ratio))


def _media_name_from_url(url: str) -> str:
    query = parse_qs(urlparse(url).query)
    return _clean((query.get("name") or [""])[0])


def _extension_for_content_type(content_type: str) -> str:
    normalized = _clean(content_type).split(";", 1)[0].lower()
    if normalized in {"image/jpeg", "image/jpg"}:
        return "jpg"
    if normalized == "image/webp":
        return "webp"
    if normalized == "image/gif":
        return "gif"
    return "png"


def _image_size_from_bytes(data: bytes, content_type: str) -> tuple[int, int]:
    normalized = _clean(content_type).split(";", 1)[0].lower()
    if normalized == "image/png" and len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n":
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    return 0, 0


def _is_google_login_url(url: str) -> bool:
    return "accounts.google." in _clean(url).lower()


def _is_flow_project_url(url: str) -> bool:
    normalized = _clean(url).lower()
    return "labs.google" in normalized and "/tools/flow/project/" in normalized


async def _page_text(page: Page, limit: int = 4000) -> str:
    try:
        text = await page.locator("body").inner_text(timeout=3000)
    except Exception:
        return ""
    return text[-limit:]


async def _find_flow_page(pages: list[Page], project_url: str) -> Page | None:
    target = _clean(project_url).split("#", 1)[0]
    for page in pages:
        if _clean(page.url).split("#", 1)[0] == target:
            return page
    for page in pages:
        if _is_flow_project_url(page.url):
            return page
    return None


async def _detect_workspace(page: Page) -> str:
    if _is_google_login_url(page.url):
        return "login_required"
    text = (await _page_text(page, 2000)).lower()
    if "application error" in text and "client-side exception" in text:
        return "client_error"
    if "create with google flow" in text and "your ai creative studio" in text:
        return "landing"
    if "智能体设置" in text or "图片生成默认设置" in text:
        return "workspace"
    if "所有媒体内容" in text and ("图片" in text or "角色" in text or "场景" in text):
        return "workspace"
    try:
        count = await page.locator("textarea, [contenteditable='true'], [role='textbox']").count()
        for index in range(count):
            item = page.locator("textarea, [contenteditable='true'], [role='textbox']").nth(index)
            try:
                if await item.is_visible(timeout=300):
                    return "workspace"
            except Exception:
                pass
    except Exception:
        pass
    return "unknown"


async def _click_flow_landing_cta(page: Page) -> bool:
    for locator in [
        page.get_by_role("button", name="Create with Google Flow").first,
        page.locator("button").filter(has_text="Create with Google Flow").first,
        page.get_by_text("Create with Google Flow", exact=True).first,
    ]:
        try:
            await locator.wait_for(state="visible", timeout=2500)
            await locator.scroll_into_view_if_needed(timeout=2500)
            await locator.click(timeout=10000)
            return True
        except Exception:
            pass
    return False


async def _ensure_flow_workspace(context: Any, project_url: str) -> Page:
    page = await _find_flow_page(list(context.pages), project_url)
    if page is None:
        page = await context.new_page()
        await page.goto(project_url, wait_until="domcontentloaded", timeout=60000)
    await page.bring_to_front()
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=15000)
    except Exception:
        pass
    state = await _detect_workspace(page)
    if state == "landing":
        clicked = await _click_flow_landing_cta(page)
        if clicked:
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=30000)
            except Exception:
                pass
            await page.wait_for_timeout(3000)
        state = await _detect_workspace(page)
    if state == "client_error":
        await _reload_flow_project(page, project_url)
        state = await _detect_workspace(page)
    if state == "login_required":
        raise NanaFlowError("Nana Chrome needs Google login", status_code=409, retryable=False)
    if state != "workspace":
        raise NanaFlowError(f"Nana Flow workspace is not ready: {state}", status_code=409, retryable=True)
    return page


async def _reload_flow_project(page: Page, project_url: str) -> None:
    await page.goto(project_url, wait_until="domcontentloaded", timeout=60000)
    try:
        await page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    await page.wait_for_timeout(1500)


async def _visible_overlay_count(page: Page) -> int:
    result = await page.evaluate(
        """() => {
          const selectors = [
            '[data-radix-popper-content-wrapper]',
            '[data-radix-dialog-content]',
            '[role="dialog"]',
            '[role="menu"]',
            '[role="listbox"]'
          ];
          return selectors.flatMap((selector) => Array.from(document.querySelectorAll(selector)))
            .filter((node) => {
              const box = node.getBoundingClientRect();
              const style = window.getComputedStyle(node);
              return box.width > 2 && box.height > 2 && style.visibility !== 'hidden' && style.display !== 'none';
            }).length;
        }"""
    )
    return int(result or 0)


async def _dismiss_flow_overlays(page: Page) -> None:
    for _ in range(4):
        if await _visible_overlay_count(page) <= 0:
            return
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(300)


async def _prepare_flow_page(page: Page, project_url: str) -> None:
    if await _detect_workspace(page) == "client_error":
        await _reload_flow_project(page, project_url)
    await _dismiss_flow_overlays(page)
    if await _detect_workspace(page) == "client_error":
        await _reload_flow_project(page, project_url)


async def _visible_button_entries(page: Page) -> list[tuple[float, float, int, str]]:
    entries: list[tuple[float, float, int, str]] = []
    count = await page.locator("button").count()
    for index in range(count):
        button = page.locator("button").nth(index)
        try:
            if not await button.is_visible(timeout=200):
                continue
            box = await button.bounding_box(timeout=300)
            if not box:
                continue
            text = (await button.inner_text(timeout=300)).strip()
            entries.append((float(box.get("y", 0)), float(box.get("x", 0)), index, text))
        except Exception:
            pass
    return entries


async def _click_last_visible_button_containing(page: Page, marker: str) -> bool:
    entries = [entry for entry in await _visible_button_entries(page) if marker in entry[3]]
    if not entries:
        return False
    entries.sort()
    await page.locator("button").nth(entries[-1][2]).click(timeout=10000)
    return True


async def _generation_settings_open(page: Page) -> bool:
    text = await _page_text(page, 2500)
    if "图片生成默认设置" in text:
        return True
    entries = await _visible_button_entries(page)
    labels = [_compact_label(entry[3]) for entry in entries]
    has_model_dropdown = any("arrow_drop_down" in label and _label_has_nano_model(label) for label in labels)
    has_count_options = any(_label_matches_count(label, "1x") for label in labels) and any(_label_matches_count(label, "x4") for label in labels)
    has_aspect_options = any(_label_matches_aspect(label, "1:1") for label in labels) and any(_label_matches_aspect(label, "16:9") for label in labels)
    return has_model_dropdown and has_count_options and has_aspect_options


async def _click_prompt_generation_summary(page: Page) -> bool:
    entries = [
        entry
        for entry in await _visible_button_entries(page)
        if "arrow_drop_down" not in entry[3]
        and _label_is_generation_summary(entry[3])
    ]
    if not entries:
        return False
    entries.sort()
    await page.locator("button").nth(entries[-1][2]).click(timeout=10000)
    return True


async def _click_retry_if_present(page: Page) -> bool:
    text = await _page_text(page, 3000)
    if "出了点问题" not in text and "请重试" not in text and "try again" not in text.lower():
        return False
    for marker in ("重试", "try again", "retry"):
        entries = [entry for entry in await _visible_button_entries(page) if marker.lower() in entry[3].lower()]
        if entries:
            entries.sort()
            await page.locator("button").nth(entries[-1][2]).click(timeout=10000)
            await page.wait_for_timeout(2500)
            return True
    # Some Flow error cards are themselves clickable.
    entries = [entry for entry in await _visible_button_entries(page) if "出了点问题" in entry[3] or "请重试" in entry[3]]
    if entries:
        entries.sort()
        await page.locator("button").nth(entries[-1][2]).click(timeout=10000)
        await page.wait_for_timeout(2500)
        return True
    return False


async def _ensure_project_root(page: Page, project_url: str) -> None:
    target = _clean(project_url).split("#", 1)[0]
    current = _clean(page.url).split("#", 1)[0]
    if current == target:
        return
    if "/tools/flow/project/" not in current:
        return
    await page.goto(project_url, wait_until="domcontentloaded", timeout=60000)
    try:
        await page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    await page.wait_for_timeout(1000)


async def _close_settings_if_open(page: Page) -> None:
    await page.keyboard.press("Escape")
    await page.wait_for_timeout(300)
    text = await _page_text(page, 2500)
    if "智能体设置" not in text and "图片生成默认设置" not in text:
        return
    entries = [
        entry
        for entry in await _visible_button_entries(page)
        if "close" in entry[3] or "关闭" in entry[3]
    ]
    if entries:
        entries.sort()
        await page.locator("button").nth(entries[-1][2]).click(timeout=10000)
        await page.wait_for_timeout(800)


async def _open_settings(page: Page) -> None:
    await _dismiss_flow_overlays(page)
    if await _generation_settings_open(page):
        return
    if not await _click_prompt_generation_summary(page):
        raise NanaFlowError("Nana generation settings summary button not found", status_code=502, retryable=True)
    await page.wait_for_timeout(700)
    if await _generation_settings_open(page):
        return
    await page.keyboard.press("Escape")
    await page.wait_for_timeout(300)
    if await _click_prompt_generation_summary(page):
        await page.wait_for_timeout(700)
    if not await _generation_settings_open(page):
        raise NanaFlowError("Nana settings panel did not open", status_code=502, retryable=True)


async def _click_first_image_setting_tab(page: Page, label: str) -> None:
    entries = []
    for entry in await _visible_button_entries(page):
        text = entry[3]
        if _label_is_generation_summary(text):
            continue
        last_line = text.splitlines()[-1].strip()
        if label == last_line or label == _compact_label(text):
            entries.append(entry)
            continue
        if label in NANA_ASPECTS and _label_matches_aspect(text, label):
            entries.append(entry)
            continue
        if _label_matches_count(text, label):
            entries.append(entry)
    if not entries:
        raise NanaFlowError(f"Nana setting option not found: {label}", status_code=502, retryable=True)
    entries.sort()
    await page.locator("button").nth(entries[0][2]).click(timeout=10000)
    await page.wait_for_timeout(300)


async def _select_model(page: Page, model_label: str) -> None:
    entries = [
        entry
        for entry in await _visible_button_entries(page)
        if "arrow_drop_down" in entry[3] and _label_has_nano_model(entry[3])
    ]
    if not entries:
        raise NanaFlowError("Nana model dropdown not found", status_code=502, retryable=True)
    entries.sort()
    await page.locator("button").nth(entries[0][2]).click(timeout=10000)
    await page.wait_for_timeout(600)
    options = [
        entry
        for entry in await _visible_button_entries(page)
        if _label_matches_nano_model(entry[3], model_label)
    ]
    if not options:
        raise NanaFlowError(f"Nana model option not found: {model_label}", status_code=502, retryable=True)
    options.sort()
    await page.locator("button").nth(options[0][2]).click(timeout=10000)
    await page.wait_for_timeout(500)


async def _configure_generation_defaults(page: Page, *, model_label: str, aspect_ratio: str | None, n: int, project_url: str | None = None) -> None:
    last_error: NanaFlowError | None = None
    for attempt in range(2):
        try:
            await _open_settings(page)
            if aspect_ratio:
                await _click_first_image_setting_tab(page, aspect_ratio)
            await _click_first_image_setting_tab(page, "1x" if n <= 1 else f"x{n}")
            await _select_model(page, model_label)
            entries = [entry for entry in await _visible_button_entries(page) if entry[3].strip() == "保存"]
            if entries:
                entries.sort()
                await page.locator("button").nth(entries[-1][2]).click(timeout=10000)
                await page.wait_for_timeout(1000)
            await _close_settings_if_open(page)
            return
        except NanaFlowError as exc:
            last_error = exc
            if attempt == 0 and exc.retryable and project_url:
                await _reload_flow_project(page, project_url)
                await _dismiss_flow_overlays(page)
                continue
            raise
    if last_error is not None:
        raise last_error


async def _click_new_session(page: Page) -> bool:
    await _close_settings_if_open(page)
    if await _click_last_visible_button_containing(page, "新建会话"):
        await page.wait_for_timeout(1500)
        return True
    if await _click_last_visible_button_containing(page, "edit_square"):
        await page.wait_for_timeout(1500)
        return True
    return False


async def _prompt_box(page: Page) -> Any:
    candidates: list[tuple[float, float, int]] = []
    count = await page.locator("[role='textbox']").count()
    for index in range(count):
        item = page.locator("[role='textbox']").nth(index)
        try:
            if not await item.is_visible(timeout=300):
                continue
            box = await item.bounding_box(timeout=500)
            if not box:
                continue
            candidates.append((float(box.get("y", 0)), float(box.get("x", 0)), index))
        except Exception:
            pass
    if not candidates:
        raise NanaFlowError("Nana prompt textbox not found", status_code=502, retryable=True)
    candidates.sort()
    return page.locator("[role='textbox']").nth(candidates[-1][2])


async def _click_prompt_box(page: Page) -> Any:
    box = await _prompt_box(page)
    try:
        await box.click(timeout=10000)
        return box
    except PlaywrightError as exc:
        await _dismiss_flow_overlays(page)
        try:
            await box.click(timeout=10000)
            return box
        except PlaywrightError as retry_exc:
            raise NanaFlowError(f"Nana prompt textbox click failed: {retry_exc}", status_code=502, retryable=True) from exc


async def _submit_button(page: Page) -> Any:
    entries = [entry for entry in await _visible_button_entries(page) if entry[3].startswith("arrow_forward")]
    if not entries:
        await _dismiss_flow_overlays(page)
        entries = [entry for entry in await _visible_button_entries(page) if entry[3].startswith("arrow_forward")]
    if not entries:
        raise NanaFlowError("Nana submit button not found", status_code=502, retryable=True)
    entries.sort()
    return page.locator("button").nth(entries[-1][2])


async def _button_disabled(button: Any) -> bool:
    return bool(await button.evaluate("(b) => !!b.disabled || b.getAttribute('aria-disabled') === 'true'"))


def _unique_names(names: list[str]) -> list[str]:
    unique: list[str] = []
    for name in names:
        if name and name not in unique:
            unique.append(name)
    return unique


async def _media_names(page: Page) -> list[str]:
    srcs = await page.locator("img[src*='media.getMediaUrlRedirect']").evaluate_all(
        "(els) => els.map((e) => e.getAttribute('src'))"
    )
    names: list[str] = []
    for src in srcs:
        name = _media_name_from_url(_clean(src))
        if name and name not in names:
            names.append(name)
    return names


async def _media_names_by_upload_token(page: Page, tokens: list[str]) -> dict[str, str]:
    result = await page.evaluate(
        """(tokens) => {
          function mediaName(src) {
            try { return new URL(src || '', location.href).searchParams.get('name') || ''; }
            catch (e) { return ''; }
          }
          function clean(value) { return String(value || '').replace(/\\s+/g, ' ').trim(); }
          function visible(node) {
            const box = node.getBoundingClientRect();
            const style = window.getComputedStyle(node);
            return box.width > 2 && box.height > 2 && style.visibility !== 'hidden' && style.display !== 'none';
          }
          const output = {};
          const imgs = Array.from(document.querySelectorAll("img[src*='media.getMediaUrlRedirect']"));
          for (const token of tokens) {
            let best = null;
            for (const img of imgs) {
              const name = mediaName(img.getAttribute('src') || img.src || '');
              if (!name || !visible(img)) continue;
              for (let depth = 0, node = img; node && depth < 10; depth += 1, node = node.parentElement) {
                const text = clean(node.innerText || node.textContent);
                if (!text.includes(token)) continue;
                const box = node.getBoundingClientRect();
                const otherTokenCount = tokens.filter((item) => item !== token && text.includes(item)).length;
                const area = Math.max(1, box.width * box.height);
                const candidate = { name, otherTokenCount, area, depth };
                if (
                  !best ||
                  candidate.otherTokenCount < best.otherTokenCount ||
                  (candidate.otherTokenCount === best.otherTokenCount && candidate.area < best.area) ||
                  (candidate.otherTokenCount === best.otherTokenCount && candidate.area === best.area && candidate.depth < best.depth)
                ) {
                  best = candidate;
                }
                break;
              }
            }
            if (best) output[token] = best.name;
          }
          return output;
        }""",
        tokens,
    )
    if not isinstance(result, dict):
        return {}
    return {_clean(key): _clean(value) for key, value in result.items() if _clean(key) and _clean(value)}


async def _prompt_panel_media_names(page: Page) -> list[str]:
    box = await _prompt_box(page)
    handle = await box.element_handle()
    if handle is None:
        return []
    names = await page.evaluate(
        """(textbox) => {
          function mediaName(src) {
            try { return new URL(src || '', location.href).searchParams.get('name') || ''; }
            catch (e) { return ''; }
          }
          function clean(value) { return String(value || '').replace(/\\s+/g, ' ').trim(); }
          let node = textbox;
          for (let depth = 0; node && depth < 8; depth++, node = node.parentElement) {
            const rect = node.getBoundingClientRect();
            const text = clean(node.innerText || node.textContent);
            const promptLike =
              rect.width <= 700 &&
              rect.height <= 500 &&
              (text.includes('arrow_forward') || text.includes('add_2') || text.includes('创建'));
            if (!promptLike) continue;
            return Array.from(node.querySelectorAll('img'))
              .map((img) => {
                const box = img.getBoundingClientRect();
                return { name: mediaName(img.getAttribute('src') || ''), visible: box.width > 2 && box.height > 2 };
              })
              .filter((item) => item.visible && item.name)
              .map((item) => item.name);
          }
          return [];
        }""",
        handle,
    )
    if not isinstance(names, list):
        return []
    return _unique_names([_clean(name) for name in names])


async def _click_prompt_add_button(page: Page) -> None:
    box = await _prompt_box(page)
    handle = await box.element_handle()
    if handle is None:
        raise NanaFlowError("Nana prompt add button not found", status_code=502, retryable=True)
    index = await page.evaluate(
        """(textbox) => {
          const tr = textbox.getBoundingClientRect();
          const buttons = Array.from(document.querySelectorAll('button')).map((button, index) => {
            const rect = button.getBoundingClientRect();
            const text = (button.innerText || button.textContent || '').replace(/\\s+/g, ' ').trim();
            return {
              index,
              text,
              x: rect.x,
              y: rect.y,
              visible: rect.width > 2 && rect.height > 2,
              near: Math.abs(rect.y - tr.y) < 250 && Math.abs(rect.x - tr.x) < Math.max(450, tr.width + 100),
            };
          }).filter((item) => item.visible && item.near && item.text.includes('add_2'));
          buttons.sort((a, b) => b.y - a.y || a.x - b.x);
          return buttons.length ? buttons[0].index : -1;
        }""",
        handle,
    )
    if not isinstance(index, int) or index < 0:
        raise NanaFlowError("Nana prompt add button not found", status_code=502, retryable=True)
    await page.locator("button").nth(index).click(timeout=10000)
    await page.wait_for_timeout(1000)


async def _click_add_to_prompt_button(page: Page) -> bool:
    entries = [entry for entry in await _visible_button_entries(page) if "添加到提示" in entry[3]]
    if not entries:
        return False
    entries.sort()
    await page.locator("button").nth(entries[-1][2]).click(timeout=10000)
    await page.wait_for_timeout(1000)
    return True


async def _click_prompt_panel_button(page: Page, markers: list[str]) -> bool:
    box = await _prompt_box(page)
    handle = await box.element_handle()
    if handle is None:
        return False
    clicked = await handle.evaluate(
        """(textbox, markers) => {
          function clean(value) { return String(value || '').replace(/\\s+/g, ' ').trim(); }
          let node = textbox;
          for (let depth = 0; node && depth < 8; depth += 1, node = node.parentElement) {
            const rect = node.getBoundingClientRect();
            const text = clean(node.innerText || node.textContent);
            const promptLike =
              rect.width <= 700 &&
              rect.height <= 500 &&
              (text.includes('arrow_forward') || text.includes('add_2') || text.includes('创建'));
            if (!promptLike) continue;
            const buttons = Array.from(node.querySelectorAll('button')).map((button) => {
              const box = button.getBoundingClientRect();
              return {
                button,
                text: clean(button.innerText || button.textContent),
                visible: box.width > 2 && box.height > 2,
              };
            }).filter((item) => item.visible);
            for (const marker of markers) {
              const target = buttons.find((item) => item.text.includes(marker));
              if (target) {
                target.button.click();
                return true;
              }
            }
          }
          return false;
        }""",
        markers,
    )
    if bool(clicked):
        await page.wait_for_timeout(700)
        return True
    return False


async def _clear_prompt_references(page: Page) -> None:
    for _ in range(8):
        if not await _prompt_panel_media_names(page):
            return
        if await _click_prompt_panel_button(page, ["清除提示", "cancel", "close"]):
            continue
        break
    if await _prompt_panel_media_names(page):
        raise NanaFlowError("Nana prompt still has old reference images", status_code=502, retryable=True)


async def _click_resource_option_by_media_name(page: Page, media_name: str) -> bool:
    option_index = await page.evaluate(
        """(name) => {
          const options = Array.from(document.querySelectorAll('[role="option"]'));
          for (let index = 0; index < options.length; index += 1) {
            const imgs = Array.from(options[index].querySelectorAll('img'));
            if (imgs.some((img) => (img.getAttribute('src') || '').includes(name))) return index;
          }
          return -1;
        }""",
        media_name,
    )
    if not isinstance(option_index, int) or option_index < 0:
        return False
    await page.locator('[role="option"]').nth(option_index).click(timeout=10000)
    await page.wait_for_timeout(700)
    return True


async def _wait_for_uploaded_media_names(page: Page, before: list[str], expected_count: int, timeout_seconds: float = 75.0) -> list[str]:
    deadline = time.perf_counter() + max(15.0, timeout_seconds)
    last_diff: list[str] = []
    while time.perf_counter() < deadline:
        current = await _media_names(page)
        diff = [name for name in current if name not in before]
        if diff:
            last_diff = diff
        if len(diff) >= expected_count:
            return diff[:expected_count]
        await page.wait_for_timeout(1000)
    raise NanaFlowError(
        f"Nana upload did not finish attaching media: expected {expected_count}, got {len(last_diff)}",
        status_code=504,
        retryable=True,
    )


async def _wait_for_uploaded_media_by_token(
    page: Page,
    tokens: list[str],
    timeout_seconds: float = 75.0,
) -> dict[str, str]:
    deadline = time.perf_counter() + max(15.0, timeout_seconds)
    last_seen: dict[str, str] = {}
    while time.perf_counter() < deadline:
        current = await _media_names_by_upload_token(page, tokens)
        if current:
            last_seen = current
        if all(token in current for token in tokens):
            return current
        await page.wait_for_timeout(1000)
    missing = [token for token in tokens if token not in last_seen]
    raise NanaFlowError(
        f"Nana upload did not expose expected file tokens: missing={missing[:4]}",
        status_code=504,
        retryable=True,
    )


async def _media_upload_statuses(page: Page, media_names: list[str]) -> dict[str, dict[str, object]]:
    statuses = await page.evaluate(
        """(names) => {
          function progressTextFor(img) {
            let node = img;
            for (let depth = 0; node && depth < 8; depth += 1, node = node.parentElement) {
              const text = String(node.innerText || node.textContent || '').replace(/\\s+/g, ' ').trim();
              const match = text.match(/\\b(?:100|\\d{1,2})%\\b/);
              if (match) return match[0];
            }
            return '';
          }
          const result = {};
          for (const name of names) {
            const imgs = Array.from(document.querySelectorAll('img'))
              .filter((img) => (img.getAttribute('src') || '').includes(name));
            result[name] = {
              found: imgs.length > 0,
              ready: imgs.some((img) => img.complete && img.naturalWidth > 0 && img.naturalHeight > 0 && !progressTextFor(img)),
              progress: imgs.map((img) => progressTextFor(img)).filter(Boolean)[0] || '',
              natural: imgs.map((img) => [img.naturalWidth || 0, img.naturalHeight || 0]),
            };
          }
          return result;
        }""",
        media_names,
    )
    if not isinstance(statuses, dict):
        return {}
    return {str(key): value if isinstance(value, dict) else {} for key, value in statuses.items()}


async def _generated_media_statuses(page: Page, media_names: list[str]) -> dict[str, dict[str, object]]:
    statuses = await page.evaluate(
        """(names) => {
          function clean(value) { return String(value || '').replace(/\\s+/g, ' ').trim(); }
          function visible(node) {
            const box = node.getBoundingClientRect();
            const style = window.getComputedStyle(node);
            return box.width > 2 && box.height > 2 && style.visibility !== 'hidden' && style.display !== 'none';
          }
          function cardFor(img) {
            let best = img;
            for (let depth = 0, node = img; node && depth < 10; depth += 1, node = node.parentElement) {
              const box = node.getBoundingClientRect();
              if (box.width >= 48 && box.height >= 48 && box.width <= 1200 && box.height <= 1200) best = node;
              const text = clean(node.innerText || node.textContent);
              if (/\\b(?:100|\\d{1,2})%\\b/.test(text)) return node;
            }
            return best;
          }
          function hasNonZeroBlurValue(value) {
            const text = String(value || '').toLowerCase();
            const matches = Array.from(text.matchAll(/blur\\(([^)]*)\\)/g));
            if (!matches.length) return false;
            return matches.some((match) => {
              const raw = String(match[1] || '').trim();
              const numeric = Number.parseFloat(raw);
              return !Number.isFinite(numeric) || Math.abs(numeric) > 0.01;
            });
          }
          function customBlurAmount(node) {
            const value = window.getComputedStyle(node).getPropertyValue('--blur-amount');
            const numeric = Number.parseFloat(value || '0');
            return Number.isFinite(numeric) ? numeric : 0;
          }
          function inlineBlurAmount(node) {
            const style = String(node.getAttribute('style') || '');
            const match = style.match(/--blur-amount\\s*:\\s*([^;]+)/i);
            if (!match) return 0;
            const numeric = Number.parseFloat(match[1] || '0');
            return Number.isFinite(numeric) ? numeric : 0;
          }
          function hasBlur(node) {
            for (let depth = 0; node && depth < 8; depth += 1, node = node.parentElement) {
              const style = window.getComputedStyle(node);
              const text = `${node.getAttribute('class') || ''} ${node.getAttribute('style') || ''}`.toLowerCase();
              if (hasNonZeroBlurValue(style.filter) || hasNonZeroBlurValue(style.backdropFilter)) return true;
              if (Math.abs(customBlurAmount(node)) > 0.01 || Math.abs(inlineBlurAmount(node)) > 0.01) return true;
              if (text.includes('skeleton') || text.includes('loading')) return true;
            }
            return false;
          }
          function progressText(card) {
            const text = clean(card.innerText || card.textContent);
            const match = text.match(/\\b(?:100|\\d{1,2})%\\b/);
            if (match) return match[0];
            const progress = card.querySelector('[role="progressbar"], progress');
            if (!progress) return '';
            return clean(progress.getAttribute('aria-valuenow') || progress.textContent || 'progress');
          }
          function hasDownloadControl(card) {
            return Array.from(card.querySelectorAll('button, a')).some((node) => {
              if (!visible(node)) return false;
              const text = clean(node.innerText || node.textContent || node.getAttribute('aria-label') || node.getAttribute('title'));
              return /download|下载/i.test(text);
            });
          }
          const result = {};
          for (const name of names) {
            const imgs = Array.from(document.querySelectorAll('img'))
              .filter((img) => (img.getAttribute('src') || '').includes(name) && visible(img));
            const items = imgs.map((img) => {
              const card = cardFor(img);
              const progress = progressText(card);
              const blur = hasBlur(img) || hasBlur(card);
              const loaded = !!(img.complete && img.naturalWidth > 0 && img.naturalHeight > 0);
              const downloadable = hasDownloadControl(card);
              return {
                loaded,
                progress,
                blur,
                downloadable,
                text: clean(card.innerText || card.textContent),
                natural: [img.naturalWidth || 0, img.naturalHeight || 0],
              };
            });
            result[name] = {
              found: items.length > 0,
              ready: items.some((item) => item.loaded && !item.progress && !item.blur),
              downloadable: items.some((item) => item.downloadable),
              progress: items.map((item) => item.progress).filter(Boolean)[0] || '',
              blurred: items.some((item) => item.blur),
              text: items.map((item) => item.text).filter(Boolean).join(' | ').slice(-1000),
              natural: items.map((item) => item.natural),
            };
          }
          return result;
        }""",
        media_names,
    )
    if not isinstance(statuses, dict):
        return {}
    return {str(key): value if isinstance(value, dict) else {} for key, value in statuses.items()}


async def _wait_for_uploaded_media_ready(page: Page, media_names: list[str], timeout_seconds: float = 120.0) -> None:
    deadline = time.perf_counter() + max(15.0, timeout_seconds)
    last_statuses: dict[str, dict[str, object]] = {}
    while time.perf_counter() < deadline:
        statuses = await _media_upload_statuses(page, media_names)
        last_statuses = statuses
        if media_names and all(bool(statuses.get(name, {}).get("ready")) for name in media_names):
            return
        await page.wait_for_timeout(1000)
    pending = {
        name: last_statuses.get(name, {})
        for name in media_names
        if not bool(last_statuses.get(name, {}).get("ready"))
    }
    raise NanaFlowError(f"Nana uploaded media did not become ready: {pending}", status_code=504, retryable=True)


def _reference_images_from_input(images: list[tuple[bytes, str, str]], session_id: str) -> list[NanaReferenceImage]:
    references: list[NanaReferenceImage] = []
    for index, (data, filename, content_type) in enumerate(images):
        if not data:
            continue
        safe_filename = filename or f"image_{index + 1}.png"
        safe_content_type = content_type or "image/png"
        sha256 = _image_sha256(data)
        nonce = uuid.uuid4().hex[:8]
        upload_filename = _session_filename(session_id, index, sha256, safe_filename, nonce)
        upload_token = upload_filename.split("__", 2)[0] + "__" + upload_filename.split("__", 2)[1]
        references.append(
            NanaReferenceImage(
                data=data,
                filename=safe_filename,
                upload_filename=upload_filename,
                upload_token=upload_token,
                content_type=safe_content_type,
                sha256=sha256,
            )
        )
    return references


async def _upload_reference_images(
    page: Page,
    references: list[NanaReferenceImage],
    indexes: list[int],
) -> dict[int, str]:
    if not indexes:
        return {}
    uploaded: dict[int, str] = {}
    for index in indexes:
        reference = references[index]
        file_payload = {
            "name": reference.upload_filename,
            "mimeType": reference.content_type,
            "buffer": reference.data,
        }
        await page.locator("input[type='file']").set_input_files([file_payload], timeout=15000)
        uploaded_by_token = await _wait_for_uploaded_media_by_token(page, [reference.upload_token])
        media_name = uploaded_by_token[reference.upload_token]
        if media_name in uploaded.values():
            raise NanaFlowError(
                "Nana upload token mapping collision",
                status_code=502,
                retryable=True,
            )
        await _wait_for_uploaded_media_ready(page, [media_name])
        uploaded[index] = media_name
    return uploaded


def _index_references_by_hash(references: list[NanaReferenceImage]) -> dict[str, list[int]]:
    grouped: dict[str, list[int]] = {}
    for index, reference in enumerate(references):
        grouped.setdefault(reference.sha256, []).append(index)
    return grouped


def _reference_binding_inputs(
    references: list[NanaReferenceImage],
    media_by_index: dict[int, str],
    source_by_index: dict[int, str],
    reference_cache_scope: str,
) -> list[dict[str, object]]:
    return [
        {
            "index": index + 1,
            "filename": reference.filename,
            "upload_token": reference.upload_token,
            "sha256": reference.sha256,
            "reference_cache_scope": reference_cache_scope,
            "media_name": media_by_index.get(index, ""),
            "source": source_by_index.get(index, ""),
        }
        for index, reference in enumerate(references)
    ]


def _record_uploaded_reference_cache_items(
    references: list[NanaReferenceImage],
    uploaded: dict[int, str],
    *,
    session_id: str,
    reference_cache_scope: str,
) -> None:
    for index, media_name in uploaded.items():
        reference = references[index]
        _record_reference_cache_item(
            data=reference.data,
            filename=reference.filename,
            content_type=reference.content_type,
            media_name=media_name,
            origin="upload",
            session_id=session_id,
            reference_cache_scope=reference_cache_scope,
            mark_used=True,
        )


async def _bind_edit_references(
    page: Page,
    images: list[tuple[bytes, str, str]],
    session_id: str,
    reference_cache_scope: str,
) -> NanaReferenceBinding:
    session_id = _normalize_session_id(session_id)
    reference_cache_scope = _normalize_reference_cache_scope(reference_cache_scope)
    references = _reference_images_from_input(images, session_id)
    if not references:
        raise NanaFlowError("image file is required", status_code=400, retryable=False)

    indexes_by_hash = _index_references_by_hash(references)
    media_by_index: dict[int, str] = {}
    source_by_index: dict[int, str] = {}
    cached_representatives: list[int] = []
    missing_representatives: list[int] = []

    for sha256, indexes in indexes_by_hash.items():
        entry = _reference_cache_lookup(reference_cache_scope, sha256)
        media_name = _clean(entry.get("media_name") if entry else "")
        if media_name:
            entry_scope = _normalize_reference_cache_scope(entry.get("reference_cache_scope") if entry else "")
            source = "cache" if entry_scope == reference_cache_scope else "cache_cross_scope"
            for index in indexes:
                media_by_index[index] = media_name
                source_by_index[index] = source
            cached_representatives.append(indexes[0])
        else:
            missing_representatives.append(indexes[0])

    uploaded = await _upload_reference_images(page, references, missing_representatives)
    for index, media_name in uploaded.items():
        reference = references[index]
        for sibling_index in indexes_by_hash.get(reference.sha256, [index]):
            media_by_index[sibling_index] = media_name
            source_by_index[sibling_index] = "upload"

    reference_media_names = _unique_names([media_by_index[index] for index in range(len(references)) if media_by_index.get(index)])
    try:
        await _add_exact_uploaded_media_to_prompt(page, reference_media_names)
    except NanaFlowError:
        if not cached_representatives:
            raise
        _drop_reference_cache_media_names([media_by_index.get(index, "") for index in cached_representatives])
        _drop_reference_cache_items(reference_cache_scope, [references[index].sha256 for index in cached_representatives])
        await _clear_prompt_references(page)
        uploaded_stale = await _upload_reference_images(page, references, cached_representatives)
        for index, media_name in uploaded_stale.items():
            reference = references[index]
            for sibling_index in indexes_by_hash.get(reference.sha256, [index]):
                media_by_index[sibling_index] = media_name
                source_by_index[sibling_index] = "stale_upload"
        reference_media_names = _unique_names([media_by_index[index] for index in range(len(references)) if media_by_index.get(index)])
        await _add_exact_uploaded_media_to_prompt(page, reference_media_names)
        _record_uploaded_reference_cache_items(
            references,
            uploaded,
            session_id=session_id,
            reference_cache_scope=reference_cache_scope,
        )
        _record_uploaded_reference_cache_items(
            references,
            uploaded_stale,
            session_id=session_id,
            reference_cache_scope=reference_cache_scope,
        )
        return NanaReferenceBinding(
            media_names=reference_media_names,
            cache_hits=0,
            upload_count=len(uploaded) + len(uploaded_stale),
            stale_count=len(cached_representatives),
            inputs=_reference_binding_inputs(references, media_by_index, source_by_index, reference_cache_scope),
        )

    _record_uploaded_reference_cache_items(
        references,
        uploaded,
        session_id=session_id,
        reference_cache_scope=reference_cache_scope,
    )
    for index in cached_representatives:
        reference = references[index]
        if source_by_index.get(index) == "cache_cross_scope":
            _record_reference_cache_item(
                data=reference.data,
                filename=reference.filename,
                content_type=reference.content_type,
                media_name=media_by_index[index],
                origin="cache_cross_scope",
                session_id=session_id,
                reference_cache_scope=reference_cache_scope,
                mark_used=True,
            )
        else:
            _mark_reference_cache_used(reference_cache_scope, session_id, reference.sha256, media_by_index[index])
    return NanaReferenceBinding(
        media_names=reference_media_names,
        cache_hits=len(cached_representatives),
        upload_count=len(uploaded),
        stale_count=0,
        inputs=_reference_binding_inputs(references, media_by_index, source_by_index, reference_cache_scope),
    )


async def _wait_for_prompt_references(page: Page, required_names: list[str], timeout_seconds: float = 20.0) -> None:
    required = set(required_names)
    deadline = time.perf_counter() + max(5.0, timeout_seconds)
    while time.perf_counter() < deadline:
        attached = set(await _prompt_panel_media_names(page))
        if required.issubset(attached):
            return
        await page.wait_for_timeout(500)
    attached = await _prompt_panel_media_names(page)
    missing = [name for name in required_names if name not in attached]
    raise NanaFlowError(
        f"Nana uploaded references were not added to prompt: missing {len(missing)}",
        status_code=502,
        retryable=True,
    )


async def _wait_for_exact_prompt_references(page: Page, required_names: list[str], timeout_seconds: float = 20.0) -> None:
    required = set(required_names)
    deadline = time.perf_counter() + max(5.0, timeout_seconds)
    last_attached: list[str] = []
    while time.perf_counter() < deadline:
        attached = await _prompt_panel_media_names(page)
        last_attached = attached
        if set(attached) == required:
            return
        await page.wait_for_timeout(500)
    extra = [name for name in last_attached if name not in required]
    missing = [name for name in required_names if name not in last_attached]
    raise NanaFlowError(
        f"Nana prompt references mismatch: expected {len(required_names)}, got {len(last_attached)}, "
        f"extra={len(extra)}, missing={len(missing)}",
        status_code=502,
        retryable=True,
    )


async def _add_uploaded_media_to_prompt(page: Page, media_names: list[str]) -> None:
    for media_name in media_names:
        if media_name in await _prompt_panel_media_names(page):
            continue
        await _click_prompt_add_button(page)
        selected = await _click_resource_option_by_media_name(page, media_name)
        if not selected:
            raise NanaFlowError("Nana uploaded media not found in resource picker", status_code=502, retryable=True)
        if media_name not in await _prompt_panel_media_names(page):
            await _click_add_to_prompt_button(page)
        await _wait_for_prompt_references(page, [media_name])


async def _add_exact_uploaded_media_to_prompt(page: Page, media_names: list[str]) -> None:
    for attempt in range(2):
        await _clear_prompt_references(page)
        await _add_uploaded_media_to_prompt(page, media_names)
        try:
            await _wait_for_exact_prompt_references(page, media_names)
            return
        except NanaFlowError:
            if attempt >= 1:
                raise
            await _clear_prompt_references(page)


def _nana_edit_prompt(prompt: str, reference_count: int) -> str:
    return prompt


async def _wait_for_generated_media_names(
    page: Page,
    *,
    before: list[str],
    stream_media_names: list[str],
    captured_media: dict[str, tuple[bytes, str]] | None = None,
    n: int,
    timeout_seconds: float,
    previous_error: str,
    blocked_message: str,
    timeout_message: str,
) -> list[str]:
    per_image_timeout_seconds = max(30.0, min(300.0, float(timeout_seconds or 120.0)))
    started = time.perf_counter()
    hard_deadline = started + max(per_image_timeout_seconds, per_image_timeout_seconds * max(1, n) + 60.0)
    next_image_deadline = started + per_image_timeout_seconds
    new_names: list[str] = []
    ready_names: list[str] = []
    ui_retry_count = 0
    before_set = set(before)
    while time.perf_counter() < min(next_image_deadline, hard_deadline):
        if await _detect_workspace(page) == "client_error":
            raise NanaFlowError("Nana Flow client-side exception", status_code=502, retryable=True)
        current = await _media_names(page)
        dom_diff = [name for name in current if name not in before_set]
        stream_diff = [name for name in stream_media_names if name not in before_set]
        captured_diff = [name for name in (captured_media or {}) if name not in before_set]
        candidates = list(dict.fromkeys([*dom_diff, *stream_diff, *captured_diff]))
        if candidates:
            statuses = await _generated_media_statuses(page, candidates)
            current_ready = [
                name
                for name in candidates
                if name in (captured_media or {})
                or bool(statuses.get(name, {}).get("ready"))
                or bool(statuses.get(name, {}).get("downloadable"))
            ]
            has_activity = bool(dom_diff or stream_diff or captured_diff)
            if not has_activity:
                has_activity = any(
                    bool(status.get("progress")) or bool(status.get("blurred")) or bool(status.get("loaded"))
                    for status in statuses.values()
                )
            if len(current_ready) > len(ready_names):
                ready_names = current_ready
                new_names = current_ready
                next_image_deadline = min(time.perf_counter() + per_image_timeout_seconds, hard_deadline)
            elif current_ready != ready_names:
                ready_names = current_ready
                new_names = current_ready
            elif len(candidates) > len(new_names) and not ready_names:
                new_names = candidates
            if has_activity and len(ready_names) < n:
                next_image_deadline = min(time.perf_counter() + per_image_timeout_seconds, hard_deadline)
            if len(ready_names) >= n:
                break
        transient_error = await _transient_flow_error_text(
            page,
            statuses if candidates else None,
            include_page_fallback=False,
        )
        if transient_error and transient_error != previous_error:
            raise NanaFlowError(f"Nana Flow unusual activity guardrail: {transient_error}", status_code=403, retryable=True)
        error_text = await _policy_error_text(
            page,
            statuses if candidates else None,
            include_page_fallback=False,
        )
        if error_text and error_text != previous_error and not dom_diff:
            raise NanaFlowError(blocked_message.format(error=error_text), status_code=400, retryable=False)
        if ui_retry_count < 2 and await _click_retry_if_present(page):
            ui_retry_count += 1
            new_names = []
            ready_names = []
            before = await _media_names(page)
            before_set = set(before)
            stream_media_names.clear()
            if captured_media is not None:
                captured_media.clear()
            next_image_deadline = min(time.perf_counter() + per_image_timeout_seconds, hard_deadline)
            continue
        await page.wait_for_timeout(750)
    if not ready_names:
        raise NanaFlowError(timeout_message, status_code=504, retryable=True)
    return ready_names[:n]


def _is_batch_generate_request(request: Request) -> bool:
    return "flowMedia:batchGenerateImages" in request.url and request.method.upper() == "POST"


def _batch_generate_media_names_from_payload(payload: object) -> list[str]:
    if not isinstance(payload, dict):
        return []
    media = payload.get("media")
    if not isinstance(media, list):
        return []
    names: list[str] = []
    for item in media:
        if not isinstance(item, dict):
            continue
        name = _clean(item.get("name")).lower()
        if name and name not in names:
            names.append(name)
    return names


async def _fill_prompt_and_get_submit_button(page: Page, prompt: str) -> Any:
    box = await _click_prompt_box(page)
    await page.keyboard.press("Control+A")
    await page.keyboard.press("Backspace")
    await page.keyboard.insert_text(prompt)
    button = await _submit_button(page)
    for _ in range(40):
        if not await _button_disabled(button):
            break
        await page.wait_for_timeout(250)
    if await _button_disabled(button):
        raise NanaFlowError("Nana submit button stayed disabled", status_code=502, retryable=True)
    return button


async def _click_submit_button_and_capture_batch_request(page: Page, button: Any) -> Request:
    try:
        async with page.expect_request(_is_batch_generate_request, timeout=30000) as request_info:
            await button.click(timeout=10000)
        return await request_info.value
    except PlaywrightError as exc:
        raise NanaFlowError(f"Nana batch generation request was not captured: {exc}", status_code=504, retryable=True) from exc


async def _submit_prompt_and_capture_batch_request(page: Page, prompt: str) -> Request:
    button = await _fill_prompt_and_get_submit_button(page, prompt)
    return await _click_submit_button_and_capture_batch_request(page, button)


async def _media_names_from_batch_request(
    request: Request,
    *,
    expected_count: int,
    timeout_seconds: float,
) -> list[str]:
    per_image_timeout_seconds = max(30.0, min(300.0, float(timeout_seconds or 120.0)))
    hard_timeout = max(per_image_timeout_seconds, per_image_timeout_seconds * max(1, expected_count) + 60.0)
    try:
        response = await asyncio.wait_for(request.response(), timeout=hard_timeout)
    except asyncio.TimeoutError as exc:
        raise NanaFlowError("Nana generation timed out waiting for batch response", status_code=504, retryable=True) from exc
    if response is None:
        raise NanaFlowError("Nana batch generation returned no response", status_code=502, retryable=True)
    try:
        text = await asyncio.wait_for(response.text(), timeout=30.0)
    except asyncio.TimeoutError as exc:
        raise NanaFlowError("Nana generation timed out reading batch response", status_code=504, retryable=True) from exc
    except Exception as exc:
        raise NanaFlowError(f"Nana batch response read failed: {exc}", status_code=502, retryable=True) from exc
    if response.status >= 400:
        retryable = response.status in {403, 408, 409, 429} or response.status >= 500
        raise NanaFlowError(
            f"Nana batch generation HTTP {response.status}: {text[:800]}",
            status_code=response.status,
            retryable=retryable,
        )
    try:
        payload = json.loads(text)
    except Exception as exc:
        raise NanaFlowError("Nana batch generation returned invalid JSON", status_code=502, retryable=True) from exc
    names = _batch_generate_media_names_from_payload(payload)
    if len(names) < expected_count:
        raise NanaFlowError(
            f"Nana batch generation returned {len(names)} media item(s), expected {expected_count}",
            status_code=502,
            retryable=True,
        )
    return names[:expected_count]


async def _wait_for_explicit_generated_media_ready(
    page: Page,
    media_names: list[str],
    *,
    timeout_seconds: float,
    previous_error: str,
    blocked_message: str,
    timeout_message: str,
) -> None:
    per_image_timeout_seconds = max(30.0, min(300.0, float(timeout_seconds or 120.0)))
    started = time.perf_counter()
    hard_deadline = started + max(per_image_timeout_seconds, per_image_timeout_seconds * max(1, len(media_names)) + 60.0)
    last_statuses: dict[str, dict[str, object]] = {}
    while time.perf_counter() < hard_deadline:
        async with _NANA_FLOW_LOCK:
            if await _detect_workspace(page) == "client_error":
                raise NanaFlowError("Nana Flow client-side exception", status_code=502, retryable=True)
            statuses = await _generated_media_statuses(page, media_names)
            last_statuses = statuses
            ready = [
                name
                for name in media_names
                if bool(statuses.get(name, {}).get("ready"))
                or bool(statuses.get(name, {}).get("downloadable"))
            ]
            if len(ready) >= len(media_names):
                return
            transient_error = await _transient_flow_error_text(page, statuses, include_page_fallback=False)
            if transient_error and transient_error != previous_error:
                raise NanaFlowError(f"Nana Flow unusual activity guardrail: {transient_error}", status_code=403, retryable=True)
            error_text = await _policy_error_text(page, statuses, include_page_fallback=False)
            if error_text and error_text != previous_error:
                raise NanaFlowError(blocked_message.format(error=error_text), status_code=400, retryable=False)
        await asyncio.sleep(0.75)
    raise NanaFlowError(f"{timeout_message}: {last_statuses}", status_code=504, retryable=True)


def _policy_error_from_text(text: object) -> str:
    value = _clean(text)
    lower = value.lower()
    explicit_markers = (
        "此生成内容可能违反了我们的政策",
        "此提示词可能违反了我们",
        "违反了我们的政策",
        "triggered our safety filters",
        "safety block",
        "safety filter",
        "policy violation",
    )
    if any(marker.lower() in lower for marker in explicit_markers):
        return value[-1000:]
    if "失败" in value and ("违反" in value or "未成年人" in value or "有害内容" in value):
        return value[-1000:]
    return ""


async def _policy_error_text(
    page: Page,
    statuses: dict[str, dict[str, object]] | None = None,
    *,
    include_page_fallback: bool = True,
) -> str:
    for status in (statuses or {}).values():
        if not isinstance(status, dict):
            continue
        text = _policy_error_from_text(status.get("text"))
        if text:
            return text
    if not include_page_fallback:
        return ""
    return _policy_error_from_text(await _page_text(page, 1800))


def _transient_flow_error_from_text(text: object) -> str:
    value = _clean(text)
    lower = value.lower()
    markers = (
        "我们发现了一些异常活动",
        "异常活动",
        "请访问帮助中心",
        "unusual activity",
        "recaptcha",
        "public_error_unusual_activity",
    )
    if any(marker.lower() in lower for marker in markers):
        return value[-1000:]
    return ""


async def _transient_flow_error_text(
    page: Page,
    statuses: dict[str, dict[str, object]] | None = None,
    *,
    include_page_fallback: bool = True,
) -> str:
    for status in (statuses or {}).values():
        if not isinstance(status, dict):
            continue
        text = _transient_flow_error_from_text(status.get("text"))
        if text:
            return text
    if not include_page_fallback:
        return ""
    return _transient_flow_error_from_text(await _page_text(page, 1800))


async def _visible_generation_error_text(page: Page) -> str:
    return await _transient_flow_error_text(page) or await _policy_error_text(page)


async def _canvas_export_image(page: Page, media_name: str) -> tuple[bytes, str, int, int]:
    result = await page.evaluate(
        """async (name) => {
          const imgs = Array.from(document.querySelectorAll('img'));
          const img = imgs.find((item) => (item.getAttribute('src') || '').includes(name));
          if (!img) return { ok: false, error: 'img_not_found' };
          if (!img.complete || img.naturalWidth === 0) {
            await new Promise((resolve, reject) => {
              img.addEventListener('load', resolve, { once: true });
              img.addEventListener('error', () => reject(new Error('image_load_error')), { once: true });
              setTimeout(() => reject(new Error('image_load_timeout')), 30000);
            });
          }
          const canvas = document.createElement('canvas');
          canvas.width = img.naturalWidth;
          canvas.height = img.naturalHeight;
          const ctx = canvas.getContext('2d');
          ctx.drawImage(img, 0, 0);
          return {
            ok: true,
            width: img.naturalWidth,
            height: img.naturalHeight,
            dataUrl: canvas.toDataURL('image/png'),
          };
        }""",
        media_name,
    )
    if not isinstance(result, dict) or not result.get("ok"):
        raise NanaFlowError(f"Nana image export failed: {result}", status_code=502, retryable=True)
    data_url = _clean(result.get("dataUrl"))
    _, _, payload = data_url.partition(",")
    if not payload:
        raise NanaFlowError("Nana canvas export returned empty data", status_code=502, retryable=True)
    return base64.b64decode(payload), "image/png", int(result.get("width") or 0), int(result.get("height") or 0)


async def _image_natural_size(page: Page, media_name: str) -> tuple[int, int]:
    result = await page.evaluate(
        """(name) => {
          const imgs = Array.from(document.querySelectorAll('img'));
          const img = imgs.find((item) => (item.getAttribute('src') || '').includes(name));
          if (!img) return { width: 0, height: 0 };
          return { width: img.naturalWidth || 0, height: img.naturalHeight || 0 };
        }""",
        media_name,
    )
    if not isinstance(result, dict):
        return 0, 0
    return int(result.get("width") or 0), int(result.get("height") or 0)


async def _image_src_for_media_name(page: Page, media_name: str) -> str:
    result = await page.evaluate(
        """(name) => {
          const imgs = Array.from(document.querySelectorAll('img'));
          const img = imgs.find((item) => (item.getAttribute('src') || '').includes(name));
          if (!img) return '';
          try { return new URL(img.getAttribute('src') || img.src || '', location.href).href; }
          catch (e) { return img.getAttribute('src') || img.src || ''; }
        }""",
        media_name,
    )
    return _clean(result)


async def _fetch_media_image_via_page_context(page: Page, media_name: str) -> tuple[bytes, str, int, int] | None:
    src = await _image_src_for_media_name(page, media_name)
    if not src:
        return None
    try:
        response = await page.context.request.get(src, timeout=8000)
        if response.status >= 400:
            return None
        content_type = _clean(response.headers.get("content-type")) or "image/png"
        if not content_type.lower().startswith("image/"):
            return None
        data = await response.body()
    except Exception:
        return None
    if not data:
        return None
    width, height = await _image_natural_size(page, media_name)
    return data, content_type, width, height


async def _fetch_media_image_via_page_fetch(page: Page, media_name: str) -> tuple[bytes, str, int, int] | None:
    try:
        result = await page.evaluate(
            """async (name) => {
              const imgs = Array.from(document.querySelectorAll('img'));
              const img = imgs.find((item) => (item.getAttribute('src') || '').includes(name));
              if (!img) return { ok: false, error: 'img_not_found' };
              const url = new URL(img.getAttribute('src') || img.src || '', location.href).href;
              const response = await fetch(url, { credentials: 'include', cache: 'force-cache' });
              if (!response.ok) return { ok: false, status: response.status };
              const buffer = await response.arrayBuffer();
              const bytes = new Uint8Array(buffer);
              let binary = '';
              const chunk = 0x8000;
              for (let offset = 0; offset < bytes.length; offset += chunk) {
                binary += String.fromCharCode(...bytes.subarray(offset, offset + chunk));
              }
              return {
                ok: true,
                contentType: response.headers.get('content-type') || 'image/png',
                data: btoa(binary),
                width: img.naturalWidth || 0,
                height: img.naturalHeight || 0,
              };
            }""",
            media_name,
        )
    except Exception:
        return None
    if not isinstance(result, dict) or not result.get("ok"):
        return None
    content_type = _clean(result.get("contentType")) or "image/png"
    if not content_type.lower().startswith("image/"):
        return None
    payload = _clean(result.get("data"))
    if not payload:
        return None
    try:
        data = base64.b64decode(payload, validate=True)
    except Exception:
        return None
    if not data:
        return None
    return data, content_type, int(result.get("width") or 0), int(result.get("height") or 0)


async def _fetch_media_image_via_redirect(page: Page, media_name: str) -> tuple[bytes, str, int, int] | None:
    url = f"https://labs.google/fx/api/trpc/media.getMediaUrlRedirect?name={quote(media_name, safe='')}"
    try:
        response = await page.context.request.get(url, timeout=30000)
        if response.status >= 400:
            return None
        content_type = _clean(response.headers.get("content-type")) or "image/png"
        if not content_type.lower().startswith("image/"):
            return None
        data = await response.body()
    except Exception:
        return None
    if not data:
        return None
    width, height = _image_size_from_bytes(data, content_type)
    return data, content_type, width, height


async def _save_generated_media_images(
    page: Page,
    media_names: list[str],
    *,
    session_id: str,
    reference_cache_scope: str,
    captured_media: dict[str, tuple[bytes, str]] | None = None,
) -> list[NanaGeneratedImage]:
    images: list[NanaGeneratedImage] = []
    for media_name in media_names:
        captured_item = (captured_media or {}).get(media_name)
        if captured_item is not None:
            data, content_type = captured_item
            width, height = _image_size_from_bytes(data, content_type)
            if not width or not height:
                async with _NANA_FLOW_LOCK:
                    width, height = await _image_natural_size(page, media_name)
            images.append(_save_image_bytes(media_name, data, content_type, width, height, "response", session_id, reference_cache_scope))
            continue
        fetched_item = await _fetch_media_image_via_redirect(page, media_name)
        if fetched_item is not None:
            data, content_type, width, height = fetched_item
            images.append(_save_image_bytes(media_name, data, content_type, width, height, "redirect", session_id, reference_cache_scope))
            continue
        async with _NANA_FLOW_LOCK:
            fetched_item = await _fetch_media_image_via_page_fetch(page, media_name)
            if fetched_item is not None:
                data, content_type, width, height = fetched_item
                images.append(_save_image_bytes(media_name, data, content_type, width, height, "page_fetch", session_id, reference_cache_scope))
                continue
            fetched_item = await _fetch_media_image_via_page_context(page, media_name)
            if fetched_item is not None:
                data, content_type, width, height = fetched_item
                images.append(_save_image_bytes(media_name, data, content_type, width, height, "page_request", session_id, reference_cache_scope))
                continue
            data, content_type, width, height = await _canvas_export_image(page, media_name)
            images.append(_save_image_bytes(media_name, data, content_type, width, height, "canvas", session_id, reference_cache_scope))
    return images


def _save_image_bytes(
    media_name: str,
    data: bytes,
    content_type: str,
    width: int,
    height: int,
    source: str,
    session_id: str = DEFAULT_SESSION_ID,
    reference_cache_scope: str = DEFAULT_REFERENCE_CACHE_SCOPE,
) -> NanaGeneratedImage:
    now = time.localtime()
    session_id = _normalize_session_id(session_id)
    reference_cache_scope = _normalize_reference_cache_scope(reference_cache_scope)
    relative_dir = Path("nano") / session_id / time.strftime("%Y", now) / time.strftime("%m", now) / time.strftime("%d", now)
    extension = _extension_for_content_type(content_type)
    filename = f"{int(time.time())}_{uuid.uuid4().hex[:12]}_{media_name}.{extension}"
    relative_path = (relative_dir / filename).as_posix()
    path = NANA_IMAGES_ROOT / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    _record_reference_cache_item(
        data=data,
        filename=filename,
        content_type=content_type,
        media_name=media_name,
        origin=f"generated:{source}",
        session_id=session_id,
        reference_cache_scope=reference_cache_scope,
        local_path=relative_path,
    )
    return NanaGeneratedImage(
        relative_path=relative_path,
        media_name=media_name,
        width=width,
        height=height,
        content_type=content_type,
        source=source,
        session_id=session_id,
    )


async def _capture_media_response(response: Response, captured: dict[str, tuple[bytes, str]]) -> None:
    media_name = _media_name_from_response_chain(response)
    if not media_name or response.status >= 400:
        return
    try:
        body = await response.body()
    except Exception:
        return
    if not body:
        return
    content_type = _clean(response.headers.get("content-type")) or "image/png"
    if not content_type.lower().startswith("image/"):
        return
    captured[media_name] = (body, content_type)


async def _capture_flow_stream_media_names(response: Response, generated_names: list[str]) -> None:
    if "flowCreationAgent:streamChat" not in response.url or response.status >= 400:
        return
    try:
        body = await response.text()
    except Exception:
        return
    for name in re.findall(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", body, flags=re.I):
        normalized = name.lower()
        if normalized not in generated_names:
            generated_names.append(normalized)


def _media_name_from_response_chain(response: Response) -> str:
    candidates = [response.url]
    try:
        request = response.request
        while request is not None:
            candidates.append(request.url)
            request = request.redirected_from
    except Exception:
        pass
    for url in candidates:
        name = _media_name_from_url(_clean(url))
        if name:
            return name
    return ""


async def generate_nana_images(
    *,
    prompt: str,
    model: str,
    n: int,
    size: str | None,
    cdp_url: str = DEFAULT_CDP_URL,
    project_url: str = DEFAULT_PROJECT_URL,
    timeout_seconds: float = 300.0,
    session_id: str = DEFAULT_SESSION_ID,
    reference_cache_scope: str = DEFAULT_REFERENCE_CACHE_SCOPE,
) -> NanaGenerateResult:
    prompt = _clean(prompt)
    if not prompt:
        raise NanaFlowError("prompt is required", status_code=400, retryable=False)
    n = max(1, min(int(n or 1), 4))
    model_label = nana_model_label(model)
    aspect_ratio = normalize_nana_aspect(size)
    session_id = _normalize_session_id(session_id)
    reference_cache_scope = _normalize_reference_cache_scope(reference_cache_scope)
    started = time.perf_counter()

    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.connect_over_cdp(cdp_url)
        except PlaywrightError as exc:
            raise NanaFlowError(f"Nana Chrome CDP connect failed: {exc}", status_code=409, retryable=True) from exc
        try:
            captured: dict[str, tuple[bytes, str]] = {}
            capture_tasks: set[asyncio.Task[None]] = set()

            def on_response(response: Response) -> None:
                task = asyncio.create_task(_capture_media_response(response, captured))
                capture_tasks.add(task)
                task.add_done_callback(capture_tasks.discard)

            async with _NANA_FLOW_LOCK:
                context = browser.contexts[0] if browser.contexts else await browser.new_context()
                page = await _ensure_flow_workspace(context, project_url)
                page.on("response", on_response)
                await _ensure_project_root(page, project_url)
                await _prepare_flow_page(page, project_url)
                await _configure_generation_defaults(page, model_label=model_label, aspect_ratio=aspect_ratio, n=n, project_url=project_url)
                await _click_new_session(page)
                await _clear_prompt_references(page)
                previous_error = await _visible_generation_error_text(page)
                request = await _submit_prompt_and_capture_batch_request(page, prompt)

            try:
                new_names = await _media_names_from_batch_request(request, expected_count=n, timeout_seconds=timeout_seconds)
                await _wait_for_explicit_generated_media_ready(
                    page,
                    new_names,
                    timeout_seconds=timeout_seconds,
                    previous_error=previous_error,
                    blocked_message="Nana generation blocked by policy: {error}",
                    timeout_message="Nana generation timed out waiting for media",
                )
                if capture_tasks:
                    await asyncio.wait(capture_tasks, timeout=5)
                images = await _save_generated_media_images(
                    page,
                    new_names,
                    session_id=session_id,
                    reference_cache_scope=reference_cache_scope,
                    captured_media=captured,
                )
            finally:
                try:
                    page.remove_listener("response", on_response)
                except Exception:
                    pass
            return NanaGenerateResult(
                images=images,
                model_label=model_label,
                aspect_ratio=aspect_ratio,
                requested_n=n,
                actual_new_media_count=len(new_names),
                elapsed_seconds=round(time.perf_counter() - started, 1),
                session_id=session_id,
                reference_cache_scope=reference_cache_scope,
            )
        finally:
            await browser.close()


async def edit_nana_images(
    *,
    prompt: str,
    images: list[tuple[bytes, str, str]],
    model: str,
    n: int,
    size: str | None,
    cdp_url: str = DEFAULT_CDP_URL,
    project_url: str = DEFAULT_PROJECT_URL,
    timeout_seconds: float = 300.0,
    session_id: str = DEFAULT_SESSION_ID,
    reference_cache_scope: str = DEFAULT_REFERENCE_CACHE_SCOPE,
) -> NanaGenerateResult:
    prompt = _clean(prompt)
    if not prompt:
        raise NanaFlowError("prompt is required", status_code=400, retryable=False)
    if not images:
        raise NanaFlowError("image file is required", status_code=400, retryable=False)
    n = max(1, min(int(n or 1), 4))
    model_label = nana_model_label(model)
    aspect_ratio = normalize_nana_aspect(size)
    session_id = _normalize_session_id(session_id)
    reference_cache_scope = _normalize_reference_cache_scope(reference_cache_scope)
    started = time.perf_counter()

    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.connect_over_cdp(cdp_url)
        except PlaywrightError as exc:
            raise NanaFlowError(f"Nana Chrome CDP connect failed: {exc}", status_code=409, retryable=True) from exc
        try:
            captured: dict[str, tuple[bytes, str]] = {}
            capture_tasks: set[asyncio.Task[None]] = set()

            def on_response(response: Response) -> None:
                task = asyncio.create_task(_capture_media_response(response, captured))
                capture_tasks.add(task)
                task.add_done_callback(capture_tasks.discard)

            async with _NANA_FLOW_LOCK:
                context = browser.contexts[0] if browser.contexts else await browser.new_context()
                page = await _ensure_flow_workspace(context, project_url)
                page.on("response", on_response)
                await _ensure_project_root(page, project_url)
                await _prepare_flow_page(page, project_url)
                await _configure_generation_defaults(page, model_label=model_label, aspect_ratio=aspect_ratio, n=n, project_url=project_url)
                await _click_new_session(page)
                reference_binding = await _bind_edit_references(page, images, session_id, reference_cache_scope)
                reference_media_names = reference_binding.media_names
                edit_prompt = _nana_edit_prompt(prompt, len(reference_media_names))
                button = await _fill_prompt_and_get_submit_button(page, edit_prompt)
                await _wait_for_exact_prompt_references(page, reference_media_names)
                previous_error = await _visible_generation_error_text(page)
                request = await _click_submit_button_and_capture_batch_request(page, button)

            try:
                new_names = await _media_names_from_batch_request(request, expected_count=n, timeout_seconds=timeout_seconds)
                await _wait_for_explicit_generated_media_ready(
                    page,
                    new_names,
                    timeout_seconds=timeout_seconds,
                    previous_error=previous_error,
                    blocked_message="Nana edit blocked by policy: {error}",
                    timeout_message="Nana edit timed out waiting for media",
                )
                if capture_tasks:
                    await asyncio.wait(capture_tasks, timeout=5)
                generated = await _save_generated_media_images(
                    page,
                    new_names,
                    session_id=session_id,
                    reference_cache_scope=reference_cache_scope,
                    captured_media=captured,
                )
            finally:
                try:
                    page.remove_listener("response", on_response)
                except Exception:
                    pass
            return NanaGenerateResult(
                images=generated,
                model_label=model_label,
                aspect_ratio=aspect_ratio,
                requested_n=n,
                actual_new_media_count=len(new_names),
                elapsed_seconds=round(time.perf_counter() - started, 1),
                reference_cache_hits=reference_binding.cache_hits,
                reference_upload_count=reference_binding.upload_count,
                reference_cache_stale=reference_binding.stale_count,
                reference_media_names=reference_binding.media_names,
                reference_inputs=reference_binding.inputs,
                session_id=session_id,
                reference_cache_scope=reference_cache_scope,
            )
        finally:
            await browser.close()
