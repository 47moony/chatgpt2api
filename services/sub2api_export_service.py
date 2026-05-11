from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from services.proxy_pool_service import load_available_proxies, proxy_key


CHINA_TZ = timezone(timedelta(hours=8))
OPENAI_PLATFORM_CLIENT_ID = "app_2SKx67EdpoN0G6j64rFvigXD"


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


def _chatgpt_account_id_from_auth(auth: dict[str, Any]) -> str:
    account_id = str(auth.get("chatgpt_account_id") or "").strip()
    if account_id:
        return account_id
    account_user_id = str(auth.get("chatgpt_account_user_id") or "").strip()
    marker = "__"
    if marker in account_user_id:
        suffix = account_user_id.rsplit(marker, 1)[1].strip()
        if suffix:
            return suffix
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
    chatgpt_account_id = str(oauth.get("chatgpt_account_id") or _chatgpt_account_id_from_auth(access_auth) or _chatgpt_account_id_from_auth(id_auth) or "").strip()
    if not chatgpt_account_id:
        return None
    chatgpt_user_id = str(oauth.get("chatgpt_user_id") or access_auth.get("chatgpt_user_id") or access_auth.get("user_id") or id_auth.get("chatgpt_user_id") or id_auth.get("user_id") or "").strip()
    organization_id = str(oauth.get("organization_id") or _organization_id_from_auth(id_auth) or _organization_id_from_auth(access_auth) or "").strip()
    exported = {
        "name": email,
        "platform": "openai",
        "type": "oauth",
        "credentials": {
            "_token_version": oauth.get("_token_version") or issued_at * 1000,
            "access_token": access_token,
            "chatgpt_account_id": chatgpt_account_id,
            "chatgpt_user_id": chatgpt_user_id,
            "client_id": str(oauth.get("client_id") or OPENAI_PLATFORM_CLIENT_ID).strip(),
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
    proxy_key = str(account.get("proxy_key") or "").strip()
    if proxy_key:
        exported["proxy_key"] = proxy_key
    return exported


def build_sub2api_export(accounts: list[dict[str, Any]], include_proxy: bool = False) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    exported_accounts = [item for account in accounts if (item := account_to_sub2api(account, now)) is not None]
    proxies = load_available_proxies() if include_proxy else []
    if include_proxy and proxies:
        proxy_map = {str(proxy.get("proxy_key") or proxy_key(proxy)): proxy for proxy in proxies}
        proxy_keys = list(proxy_map)
        assigned_index = 0
        for account in exported_accounts:
            current_key = str(account.get("proxy_key") or "").strip()
            if current_key and current_key in proxy_map:
                continue
            if proxy_keys:
                account["proxy_key"] = proxy_keys[assigned_index % len(proxy_keys)]
                assigned_index += 1
        used_keys = {str(account.get("proxy_key") or "") for account in exported_accounts if str(account.get("proxy_key") or "") in proxy_map}
        proxies = [proxy for key, proxy in proxy_map.items() if key in used_keys]
        proxies = [_sub2api_proxy_payload(proxy) for proxy in proxies]
    return {
        "exported_at": now.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "proxies": proxies,
        "accounts": exported_accounts,
    }


def _sub2api_proxy_payload(proxy: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "proxy_key": str(proxy.get("proxy_key") or proxy_key(proxy)).strip(),
        "name": str(proxy.get("name") or "").strip(),
        "protocol": str(proxy.get("protocol") or "http").strip(),
        "host": str(proxy.get("host") or "").strip(),
        "port": proxy.get("port"),
        "status": str(proxy.get("status") or "active").strip(),
    }
    username = str(proxy.get("username") or "").strip()
    password = str(proxy.get("password") or "").strip()
    if username:
        payload["username"] = username
    if password:
        payload["password"] = password
    return payload


def json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
