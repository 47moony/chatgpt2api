from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

from services.config import DATA_DIR


SUB2API_PROXY_FILE = DATA_DIR / "sub2api-proxy.json"


def proxy_key(proxy: dict[str, Any]) -> str:
    key = str(proxy.get("proxy_key") or "").strip()
    if key:
        return key
    protocol = str(proxy.get("protocol") or "").strip()
    host = str(proxy.get("host") or "").strip()
    port = proxy.get("port")
    username = str(proxy.get("username") or "").strip()
    password = str(proxy.get("password") or "").strip()
    if not protocol or not host or port in ("", None):
        return ""
    return f"{protocol}|{host}|{port}|{username}|{password}"


def proxy_url(proxy: dict[str, Any]) -> str:
    protocol = str(proxy.get("protocol") or "").strip() or "http"
    host = str(proxy.get("host") or "").strip()
    port = proxy.get("port")
    username = str(proxy.get("username") or "").strip()
    password = str(proxy.get("password") or "").strip()
    if not host or port in ("", None):
        return ""
    auth = f"{username}:{password}@" if username or password else ""
    return f"{protocol}://{auth}{host}:{port}"


def load_proxy_config(path: Path = SUB2API_PROXY_FILE) -> dict[str, Any]:
    if not path.exists():
        return {"proxies": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"proxies": []}
    return data if isinstance(data, dict) else {"proxies": []}


def load_available_proxies(path: Path = SUB2API_PROXY_FILE) -> list[dict[str, Any]]:
    data = load_proxy_config(path)
    raw = data.get("proxies") if isinstance(data, dict) else []
    proxies: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        if item.get("status", "active") != "active":
            continue
        key = proxy_key(item)
        if not key or key in seen:
            continue
        proxy = dict(item)
        proxy["proxy_key"] = key
        url = proxy_url(proxy)
        if url:
            proxy["url"] = url
        proxies.append(proxy)
        seen.add(key)
    return proxies


def pick_random_proxy() -> dict[str, Any] | None:
    proxies = load_available_proxies()
    return dict(random.choice(proxies)) if proxies else None


def proxy_by_key(key: str) -> dict[str, Any] | None:
    target = str(key or "").strip()
    if not target:
        return None
    for proxy in load_available_proxies():
        if str(proxy.get("proxy_key") or "") == target:
            return proxy
    return None
