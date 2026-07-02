from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from services.config import DATA_DIR


IMAGE_LOG_ENDPOINTS = {"/v1/images/generations", "/v1/images/edits"}
IMAGE_TASK_FILE = DATA_DIR / "image_tasks.json"
LOG_FILE = DATA_DIR / "logs.jsonl"
IMAGE_DIR = DATA_DIR / "images"
OLD_LOCALHOST_3000_EXPORT_FILE = DATA_DIR / "image_conversations_localhost_3000_export.json"


def _clean(value: object, default: str = "") -> str:
    text = str(value or "").strip()
    return text or default


def _stable_id(prefix: str, *parts: object) -> str:
    payload = "\n".join(_clean(part) for part in parts).encode("utf-8", errors="ignore")
    digest = hashlib.sha1(payload).hexdigest()[:24]
    return f"{prefix}-{digest}"


def _parse_time(value: object) -> datetime | None:
    text = _clean(value)
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text[:26], fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None


def _iso(value: object) -> str:
    parsed = _parse_time(value)
    if parsed is None:
        return datetime.now().isoformat()
    return parsed.isoformat()


def _title(prompt: str, fallback: str = "服务端图片记录") -> str:
    text = " ".join(prompt.split()).strip()
    if not text:
        return fallback
    return text[:12] + ("..." if len(text) > 12 else "")


def _mode_from_endpoint(endpoint: object) -> str:
    return "edit" if _clean(endpoint).endswith("/edits") else "generate"


def _url_image_rel(url: object) -> str:
    text = _clean(url)
    if not text:
        return ""
    parsed = urlparse(text)
    path = parsed.path if parsed.scheme else text
    marker = "/images/"
    if marker not in path:
        return ""
    rel = unquote(path.split(marker, 1)[1]).replace("\\", "/").lstrip("/")
    if ".." in Path(rel).parts:
        return ""
    return rel


def _image_url(base_url: str, url: object) -> str:
    rel = _url_image_rel(url)
    if rel:
        return f"{base_url.rstrip('/')}/images/{rel}"
    return _clean(url)


def _image_exists(rel: str) -> bool:
    if not rel:
        return False
    try:
        (IMAGE_DIR / rel).resolve().relative_to(IMAGE_DIR.resolve())
    except ValueError:
        return False
    return (IMAGE_DIR / rel).is_file()


def _rewrite_image_urls(value: object, base_url: str) -> object:
    if isinstance(value, dict):
        result: dict[str, object] = {}
        for key, item in value.items():
            if key == "url" and isinstance(item, str) and _url_image_rel(item):
                result[key] = _image_url(base_url, item)
            else:
                result[key] = _rewrite_image_urls(item, base_url)
        return result
    if isinstance(value, list):
        return [_rewrite_image_urls(item, base_url) for item in value]
    return value


def _turn_group_id(task_id: str) -> str:
    match = re.match(r"^(.+)-(\d+)$", task_id)
    return match.group(1) if match else task_id


def _load_logs() -> list[dict[str, Any]]:
    if not LOG_FILE.exists():
        return []
    items: list[dict[str, Any]] = []
    for index, raw_line in enumerate(LOG_FILE.read_text(encoding="utf-8", errors="ignore").splitlines()):
        try:
            item = json.loads(raw_line)
        except Exception:
            continue
        if not isinstance(item, dict):
            continue
        detail = item.get("detail")
        if not isinstance(detail, dict):
            continue
        endpoint = _clean(detail.get("endpoint"))
        if endpoint not in IMAGE_LOG_ENDPOINTS:
            continue
        item.setdefault("id", _stable_id("legacy-log", index, raw_line))
        items.append(item)
    return items


def _load_tasks() -> list[dict[str, Any]]:
    if not IMAGE_TASK_FILE.exists():
        return []
    try:
        raw = json.loads(IMAGE_TASK_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []
    tasks = raw.get("tasks") if isinstance(raw, dict) else raw
    if not isinstance(tasks, list):
        return []
    return [item for item in tasks if isinstance(item, dict)]


def _log_urls(log: dict[str, Any]) -> list[str]:
    detail = log.get("detail") if isinstance(log.get("detail"), dict) else {}
    urls = detail.get("urls")
    if not isinstance(urls, list):
        return []
    return [_clean(url) for url in urls if _clean(url)]


def _build_log_indexes(logs: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], set[str]]:
    by_rel: dict[str, dict[str, Any]] = {}
    success_rels: set[str] = set()
    for log in logs:
        for url in _log_urls(log):
            rel = _url_image_rel(url)
            if not rel:
                continue
            by_rel.setdefault(rel, log)
            success_rels.add(rel)
    return by_rel, success_rels


def _task_data(task: dict[str, Any]) -> list[dict[str, Any]]:
    data = task.get("data")
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def _task_to_image(task: dict[str, Any], data: dict[str, Any], index: int, base_url: str) -> dict[str, Any]:
    task_id = _clean(task.get("id"), _stable_id("task-image", task, index))
    url = _clean(data.get("url"))
    image: dict[str, Any] = {
        "id": f"server-image-{task_id}-{index}",
        "taskId": task_id,
        "status": "success",
        "url": _image_url(base_url, url),
    }
    revised_prompt = _clean(data.get("revised_prompt"))
    if revised_prompt:
        image["revised_prompt"] = revised_prompt
    if task.get("duration_ms") is not None:
        image["durationMs"] = task.get("duration_ms")
    return image


def _error_task_to_image(task: dict[str, Any], index: int = 0) -> dict[str, Any]:
    task_id = _clean(task.get("id"), _stable_id("task-error", task, index))
    return {
        "id": f"server-image-{task_id}-{index}",
        "taskId": task_id,
        "status": "error",
        "error": _clean(task.get("error"), "生成失败"),
    }


def _find_prompt_for_task(task: dict[str, Any], log_by_rel: dict[str, dict[str, Any]]) -> str:
    for item in _task_data(task):
        rel = _url_image_rel(item.get("url"))
        log = log_by_rel.get(rel)
        detail = log.get("detail") if isinstance(log, dict) and isinstance(log.get("detail"), dict) else {}
        prompt = _clean(detail.get("request_text"))
        if prompt:
            return prompt
    for item in _task_data(task):
        prompt = _clean(item.get("revised_prompt"))
        if prompt:
            return prompt
    return ""


def _turn_from_task_group(
    group_id: str,
    tasks: list[dict[str, Any]],
    *,
    base_url: str,
    log_by_rel: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    tasks = sorted(tasks, key=lambda item: (_clean(item.get("created_at")), _clean(item.get("id"))))
    first = tasks[0]
    prompt = ""
    for task in tasks:
        prompt = _find_prompt_for_task(task, log_by_rel)
        if prompt:
            break
    images: list[dict[str, Any]] = []
    for task in tasks:
        if _clean(task.get("status")) == "success":
            for index, item in enumerate(_task_data(task)):
                images.append(_task_to_image(task, item, index, base_url))
        else:
            images.append(_error_task_to_image(task))
    status = "error" if images and all(image.get("status") == "error" for image in images) else "success"
    created_at = _iso(first.get("created_at"))
    return {
        "id": f"server-task-turn-{group_id}",
        "prompt": prompt,
        "model": _clean(first.get("model"), "gpt-image-2"),
        "mode": "edit" if first.get("mode") == "edit" else "generate",
        "referenceImages": [],
        "count": max(1, len(images)),
        "size": _clean(first.get("size")),
        "ratio": "1:1",
        "tier": "1k",
        "quality": _clean(first.get("quality"), "auto"),
        "images": images,
        "createdAt": created_at,
        "status": status,
        **({"error": _clean(first.get("error"))} if status == "error" and _clean(first.get("error")) else {}),
    }


def _conversation_from_turn(turn: dict[str, Any], source_key: str, updated_at: object) -> dict[str, Any]:
    created_at = _clean(turn.get("createdAt"), _iso(updated_at))
    updated = _iso(updated_at)
    prompt = _clean(turn.get("prompt"))
    return {
        "id": _stable_id("server-image-conversation", source_key),
        "title": _title(prompt),
        "createdAt": created_at,
        "updatedAt": updated,
        "turns": [turn],
    }


def _conversation_from_log(log: dict[str, Any], base_url: str) -> dict[str, Any] | None:
    detail = log.get("detail") if isinstance(log.get("detail"), dict) else {}
    prompt = _clean(detail.get("request_text"))
    urls = _log_urls(log)
    images = [
        {
            "id": f"server-log-image-{_stable_id('url', url)}-{index}",
            "status": "success",
            "url": _image_url(base_url, url),
        }
        for index, url in enumerate(urls)
        if _image_url(base_url, url)
    ]
    status = "success" if _clean(detail.get("status"), "success") == "success" and images else "error"
    if status == "error" and not prompt and not _clean(detail.get("error")):
        return None
    created_at = _iso(detail.get("started_at") or log.get("time"))
    turn = {
        "id": _stable_id("server-log-turn", log.get("id"), prompt, urls),
        "prompt": prompt,
        "model": _clean(detail.get("model"), "gpt-image-2"),
        "mode": _mode_from_endpoint(detail.get("endpoint")),
        "referenceImages": [],
        "count": max(1, len(images)),
        "size": "",
        "ratio": "1:1",
        "tier": "1k",
        "quality": "auto",
        "images": images or [{"id": _stable_id("server-log-error", log.get("id")), "status": "error", "error": _clean(detail.get("error"), "生成失败")}],
        "createdAt": created_at,
        "status": status,
        **({"error": _clean(detail.get("error"), "生成失败")} if status == "error" else {}),
    }
    return _conversation_from_turn(turn, _clean(log.get("id"), json.dumps(log, ensure_ascii=False)), detail.get("ended_at") or log.get("time"))


class ImageConversationRecoveryService:
    def recover_original_localhost_3000(self, base_url: str) -> dict[str, Any]:
        if not OLD_LOCALHOST_3000_EXPORT_FILE.exists():
            return {
                "items": [],
                "stats": {
                    "available": False,
                    "conversations": 0,
                    "turns": 0,
                    "images": 0,
                    "reference_images": 0,
                    "source": str(OLD_LOCALHOST_3000_EXPORT_FILE),
                },
            }
        try:
            raw = json.loads(OLD_LOCALHOST_3000_EXPORT_FILE.read_text(encoding="utf-8"))
        except Exception:
            raw = []
        items = raw if isinstance(raw, list) else []
        rewritten = [_rewrite_image_urls(item, base_url) for item in items if isinstance(item, dict)]
        turns = 0
        images = 0
        reference_images = 0
        for conversation in rewritten:
            if not isinstance(conversation, dict):
                continue
            for turn in conversation.get("turns", []):
                if not isinstance(turn, dict):
                    continue
                turns += 1
                turn_images = turn.get("images")
                turn_refs = turn.get("referenceImages")
                if isinstance(turn_images, list):
                    images += len(turn_images)
                if isinstance(turn_refs, list):
                    reference_images += len(turn_refs)
        return {
            "items": rewritten,
            "stats": {
                "available": True,
                "conversations": len(rewritten),
                "turns": turns,
                "images": images,
                "reference_images": reference_images,
                "source": str(OLD_LOCALHOST_3000_EXPORT_FILE),
            },
        }

    def recover(self, base_url: str) -> dict[str, Any]:
        logs = _load_logs()
        tasks = _load_tasks()
        log_by_rel, success_log_rels = _build_log_indexes(logs)

        conversations: dict[str, dict[str, Any]] = {}
        task_groups: dict[str, list[dict[str, Any]]] = {}
        task_rels: set[str] = set()
        for task in tasks:
            task_id = _clean(task.get("id"))
            if not task_id:
                continue
            task_groups.setdefault(_turn_group_id(task_id), []).append(task)
            for item in _task_data(task):
                rel = _url_image_rel(item.get("url"))
                if rel:
                    task_rels.add(rel)

        for group_id, group_tasks in task_groups.items():
            turn = _turn_from_task_group(group_id, group_tasks, base_url=base_url, log_by_rel=log_by_rel)
            updated_at = max((_clean(task.get("updated_at")) for task in group_tasks), default="")
            conversation = _conversation_from_turn(turn, f"task:{group_id}", updated_at or turn.get("createdAt"))
            conversations[conversation["id"]] = conversation

        for log in logs:
            rels = {_url_image_rel(url) for url in _log_urls(log)}
            if rels and rels.issubset(task_rels):
                continue
            conversation = _conversation_from_log(log, base_url)
            if conversation is not None:
                conversations.setdefault(conversation["id"], conversation)

        items = sorted(conversations.values(), key=lambda item: _clean(item.get("updatedAt")), reverse=True)
        restored_images = 0
        missing_images = 0
        for conversation in items:
            for turn in conversation.get("turns", []):
                if not isinstance(turn, dict):
                    continue
                for image in turn.get("images", []):
                    if not isinstance(image, dict) or image.get("status") != "success":
                        continue
                    restored_images += 1
                    rel = _url_image_rel(image.get("url"))
                    if rel and not _image_exists(rel):
                        missing_images += 1

        return {
            "items": items,
            "stats": {
                "logs": len(logs),
                "tasks": len(tasks),
                "conversations": len(items),
                "images": restored_images,
                "missing_images": missing_images,
                "success_log_images": len(success_log_rels),
            },
        }


image_conversation_recovery_service = ImageConversationRecoveryService()
