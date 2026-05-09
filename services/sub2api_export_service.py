from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from services.config import DATA_DIR


CHINA_TZ = timezone(timedelta(hours=8))
SUB2API_PROXY_FILE = DATA_DIR / "sub2api-proxy.json"


def _decode_jwt_payload(token: str) -> dict[str, Any]:
    parts = str(token or "").split(".")
    if len(parts) < 2:
        return {}
    try:
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")).decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _iso_from_unix(timestamp: int | float) -> str:
    return datetime.fromtimestamp(int(timestamp), tz=timezone.utc).astimezone(CHINA_TZ).isoformat(timespec="seconds")


def _proxy_key(proxy: dict[str, Any]) -> str:
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


def load_available_proxies(path: Path = SUB2API_PROXY_FILE) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    raw = data.get("proxies") if isinstance(data, dict) else []
    proxies: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        if item.get("status", "active") != "active":
            continue
        key = _proxy_key(item)
        if not key or key in seen:
            continue
        proxy = dict(item)
        proxy["proxy_key"] = key
        proxies.append(proxy)
        seen.add(key)
    return proxies


def _auth_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    auth = payload.get("https://api.openai.com/auth") if isinstance(payload, dict) else {}
    return auth if isinstance(auth, dict) else {}


def _profile_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    profile = payload.get("https://api.openai.com/profile") if isinstance(payload, dict) else {}
    return profile if isinstance(profile, dict) else {}


def _organization_id_from_auth(auth: dict[str, Any]) -> str:
    organizations = auth.get("organizations")
    if isinstance(organizations, list):
        for organization in organizations:
            if isinstance(organization, dict) and organization.get("id"):
                return str(organization.get("id") or "").strip()
    return ""


def account_to_sub2api(account: dict[str, Any], now: datetime | None = None) -> dict[str, Any] | None:
    now = now or datetime.now(timezone.utc)
    access_token = str(account.get("access_token") or "").strip()
    if not access_token:
        return None
    oauth = account.get("oauth") if isinstance(account.get("oauth"), dict) else {}
    access_payload = _decode_jwt_payload(access_token)
    id_token = str(oauth.get("id_token") or "").strip()
    id_payload = _decode_jwt_payload(id_token)
    access_auth = _auth_from_payload(access_payload)
    id_auth = _auth_from_payload(id_payload)
    profile = _profile_from_payload(access_payload)
    email = str(oauth.get("email") or account.get("email") or profile.get("email") or id_payload.get("email") or access_payload.get("sub") or "unknown").strip()
    issued_at = int(access_payload.get("iat") or now.timestamp())
    expires_at = int(access_payload.get("exp") or now.timestamp())
    usage_updated_at = now.astimezone(CHINA_TZ)
    chatgpt_account_id = str(oauth.get("chatgpt_account_id") or access_auth.get("chatgpt_account_id") or id_auth.get("chatgpt_account_id") or "").strip()
    chatgpt_user_id = str(oauth.get("chatgpt_user_id") or access_auth.get("chatgpt_user_id") or access_auth.get("user_id") or id_auth.get("chatgpt_user_id") or id_auth.get("user_id") or "").strip()
    organization_id = str(oauth.get("organization_id") or _organization_id_from_auth(id_auth) or _organization_id_from_auth(access_auth) or "").strip()
    return {
        "name": email,
        "platform": "openai",
        "type": "oauth",
        "credentials": {
            "_token_version": oauth.get("_token_version") or issued_at * 1000,
            "access_token": access_token,
            "chatgpt_account_id": chatgpt_account_id,
            "chatgpt_user_id": chatgpt_user_id,
            "email": email,
            "expires_at": str(oauth.get("expires_at") or _iso_from_unix(expires_at)),
            "expires_in": max(0, expires_at - int(now.timestamp())),
            "id_token": id_token,
            "organization_id": organization_id,
            "refresh_token": str(oauth.get("refresh_token") or "").strip(),
        },
        "extra": {
            "codex_5h_reset_after_seconds": 0,
            "codex_5h_reset_at": usage_updated_at.isoformat(timespec="seconds"),
            "codex_5h_used_percent": 0,
            "codex_5h_window_minutes": 0,
            "codex_7d_reset_after_seconds": 604800,
            "codex_7d_reset_at": (usage_updated_at + timedelta(days=7)).isoformat(timespec="seconds"),
            "codex_7d_used_percent": 0,
            "codex_7d_window_minutes": 10080,
            "codex_primary_over_secondary_percent": 0,
            "codex_primary_reset_after_seconds": 604800,
            "codex_primary_used_percent": 0,
            "codex_primary_window_minutes": 10080,
            "codex_secondary_reset_after_seconds": 0,
            "codex_secondary_used_percent": 0,
            "codex_secondary_window_minutes": 0,
            "codex_usage_updated_at": usage_updated_at.isoformat(timespec="seconds"),
            "email": email,
            "privacy_mode": "training_off",
        },
        "concurrency": 10,
        "priority": 1,
        "rate_multiplier": 1,
        "auto_pause_on_expired": True,
    }


def build_sub2api_export(accounts: list[dict[str, Any]], include_proxy: bool = False) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    exported_accounts = [item for account in accounts if (item := account_to_sub2api(account, now)) is not None]
    proxies = load_available_proxies() if include_proxy else []
    if proxies:
        proxy_keys = [proxy["proxy_key"] for proxy in proxies]
        for index, account in enumerate(exported_accounts):
            account["proxy_key"] = proxy_keys[index % len(proxy_keys)]
    return {
        "exported_at": now.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "proxies": proxies,
        "accounts": exported_accounts,
    }


def json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
