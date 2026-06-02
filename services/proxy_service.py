"""Global outbound proxy helpers for upstream ChatGPT and CPA requests."""

from __future__ import annotations

import time
from urllib.parse import urlparse

from curl_cffi.requests import Session

from services.config import config
from services.proxy_pool_service import proxy_by_key, proxy_url


class ProxySettingsStore:
    def build_session_kwargs(self, account: dict | None = None, proxy: str = "", **session_kwargs) -> dict[str, object]:
        selected_proxy = str(proxy or "").strip()
        if not selected_proxy and isinstance(account, dict):
            account_proxy = account.get("proxy")
            if isinstance(account_proxy, dict):
                selected_proxy = proxy_url(account_proxy)
            else:
                selected_proxy = str(account_proxy or "").strip()
            if not selected_proxy:
                proxy_ref = proxy_by_key(str(account.get("proxy_key") or ""))
                selected_proxy = proxy_url(proxy_ref) if proxy_ref else ""
        selected_proxy = selected_proxy or config.get_proxy_settings()
        if selected_proxy:
            session_kwargs["proxy"] = selected_proxy
        return session_kwargs


def _clean(value: object) -> str:
    return str(value or "").strip()


def _is_valid_proxy_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https", "socks5", "socks5h"} and bool(parsed.netloc)


def test_proxy(url: str, *, timeout: float = 15.0) -> dict:
    candidate = _clean(url)
    if not candidate:
        return {"ok": False, "status": 0, "latency_ms": 0, "error": "proxy url is required"}
    if not _is_valid_proxy_url(candidate):
        return {"ok": False, "status": 0, "latency_ms": 0, "error": "invalid proxy url"}
    session = Session(impersonate="edge101", verify=True, proxy=candidate)
    started = time.perf_counter()
    try:
        response = session.get(
            "https://chatgpt.com/api/auth/csrf",
            headers={"user-agent": "Mozilla/5.0 (chatgpt2api proxy test)"},
            timeout=timeout,
        )
        latency_ms = int((time.perf_counter() - started) * 1000)
        return {
            "ok": response.status_code < 500,
            "status": int(response.status_code),
            "latency_ms": latency_ms,
            "error": None if response.status_code < 500 else f"HTTP {response.status_code}",
        }
    except Exception as exc:
        latency_ms = int((time.perf_counter() - started) * 1000)
        return {
            "ok": False,
            "status": 0,
            "latency_ms": latency_ms,
            "error": str(exc) or exc.__class__.__name__,
        }
    finally:
        session.close()

proxy_settings = ProxySettingsStore()

