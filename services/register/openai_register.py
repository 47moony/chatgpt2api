from __future__ import annotations

import base64
import hashlib
import json
import random
import secrets
import string
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import requests
import urllib3
from curl_cffi import requests as curl_requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from services.proxy_pool_service import load_available_proxies, proxy_url
from services.register import mail_provider

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
base_dir = Path(__file__).resolve().parent
config = {
    "mail": {
        "request_timeout": 30,
        "wait_timeout": 30,
        "wait_interval": 2,
        "providers": [],
    },
    "proxy": "",
    "total": 10,
    "threads": 3,
}
register_config_file = base_dir.parents[1] / "data" / "register.json"
try:
    saved_config = json.loads(register_config_file.read_text(encoding="utf-8"))
    config.update({key: saved_config[key] for key in ("mail", "proxy", "total", "threads") if key in saved_config})
except Exception:
    pass

auth_base = "https://auth.openai.com"
platform_base = "https://platform.openai.com"
platform_oauth_client_id = "app_2SKx67EdpoN0G6j64rFvigXD"
platform_oauth_redirect_uri = f"{platform_base}/auth/callback"
platform_oauth_audience = "https://api.openai.com/v1"
platform_auth0_client = "eyJuYW1lIjoiYXV0aDAtc3BhLWpzIiwidmVyc2lvbiI6IjEuMjEuMCJ9"
user_agent = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/145.0.0.0 Safari/537.36"
)
sec_ch_ua = '"Google Chrome";v="145", "Not?A_Brand";v="8", "Chromium";v="145"'
sec_ch_ua_full_version_list = '"Chromium";v="145.0.0.0", "Not:A-Brand";v="99.0.0.0", "Google Chrome";v="145.0.0.0"'
default_timeout = 30
print_lock = threading.Lock()
stats_lock = threading.Lock()
stats = {"done": 0, "success": 0, "fail": 0, "start_time": 0.0}
register_log_sink = None
thread_proxy_state = threading.local()
thread_proxy_lock = threading.Lock()
thread_proxy_pool: list[dict] = []
thread_proxy_index = 0
domain_failure_lock = threading.Lock()
domain_failure_counts: dict[str, int] = {}

common_headers = {
    "accept": "application/json",
    "accept-language": "en-US,en;q=0.9",
    "content-type": "application/json",
    "origin": auth_base,
    "priority": "u=1, i",
    "user-agent": user_agent,
    "sec-ch-ua": sec_ch_ua,
    "sec-ch-ua-arch": '"x86_64"',
    "sec-ch-ua-bitness": '"64"',
    "sec-ch-ua-full-version-list": sec_ch_ua_full_version_list,
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-model": '""',
    "sec-ch-ua-platform": '"Windows"',
    "sec-ch-ua-platform-version": '"10.0.0"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
}

navigate_headers = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "accept-language": "en-US,en;q=0.9",
    "user-agent": user_agent,
    "sec-ch-ua": sec_ch_ua,
    "sec-ch-ua-arch": '"x86_64"',
    "sec-ch-ua-bitness": '"64"',
    "sec-ch-ua-full-version-list": sec_ch_ua_full_version_list,
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-model": '""',
    "sec-ch-ua-platform": '"Windows"',
    "sec-ch-ua-platform-version": '"10.0.0"',
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "same-origin",
    "sec-fetch-user": "?1",
    "upgrade-insecure-requests": "1",
}


class RegisteredAccountAuthError(RuntimeError):
    def __init__(self, email: str, password: str, mailbox: dict, reason: str):
        super().__init__(reason)
        self.email = email
        self.password = password
        self.mailbox = dict(mailbox)
        self.reason = reason


def log(text: str, color: str = "") -> None:
    colors = {"red": "\033[31m", "green": "\033[32m", "yellow": "\033[33m"}
    if register_log_sink:
        try:
            register_log_sink(text, color)
        except Exception:
            pass
    with print_lock:
        prefix = colors.get(color, "")
        suffix = "\033[0m" if prefix else ""
        print(f"{prefix}{datetime.now().strftime('%H:%M:%S')} {text}{suffix}")


def step(index: int, text: str, color: str = "") -> None:
    log(f"[任务{index}] {text}", color)


def _make_trace_headers() -> dict[str, str]:
    trace_id = str(random.getrandbits(64))
    parent_id = str(random.getrandbits(64))
    return {
        "traceparent": f"00-{uuid.uuid4().hex}-{format(int(parent_id), '016x')}-01",
        "tracestate": "dd=s:1;o:rum",
        "x-datadog-origin": "rum",
        "x-datadog-parent-id": parent_id,
        "x-datadog-sampling-priority": "1",
        "x-datadog-trace-id": trace_id,
    }


def _generate_pkce() -> tuple[str, str]:
    code_verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode("ascii")
    code_challenge = base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    return code_verifier, code_challenge


def _random_password(length: int = 16) -> str:
    chars = string.ascii_letters + string.digits + "!@#$%"
    value = list(
        secrets.choice(string.ascii_uppercase)
        + secrets.choice(string.ascii_lowercase)
        + secrets.choice(string.digits)
        + secrets.choice("!@#$%")
        + "".join(secrets.choice(chars) for _ in range(max(0, length - 4)))
    )
    random.shuffle(value)
    return "".join(value)


def _random_name() -> tuple[str, str]:
    return random.choice(["James", "Robert", "John", "Michael", "David", "Mary", "Emma", "Olivia"]), random.choice(
        ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller"]
    )


def _random_birthdate() -> str:
    return f"{random.randint(1996, 2006):04d}-{random.randint(1, 12):02d}-{random.randint(1, 28):02d}"


def _response_json(resp) -> dict:
    try:
        data = resp.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _response_text(resp) -> str:
    try:
        return str(resp.text or "")
    except Exception:
        return ""


def _password_verify_requires_email_otp(payload: dict, raw_text: str = "") -> bool:
    if not isinstance(payload, dict):
        payload = {}
    page = payload.get("page") if isinstance(payload.get("page"), dict) else {}
    page_type = str(page.get("type") or payload.get("page_type") or "").strip().lower()
    continue_url = str(payload.get("continue_url") or "").strip().lower()
    text = str(raw_text or "").lower()
    return (
        page_type == "email_otp_verification"
        or "email-verification" in continue_url
        or "email-otp" in continue_url
        or "email_otp_verification" in text
        or "email-verification" in text
        or "email-otp" in text
    )


def _password_verify_error(status_code: object, payload: dict, fallback: str = "") -> str:
    reason = ""
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            reason = str(error.get("message") or error.get("code") or error.get("type") or "").strip()
        reason = reason or str(payload.get("message") or payload.get("detail") or "").strip()
    base = f"password_verify_http_{status_code}"
    if reason:
        return f"{base}: {reason[:220]}"
    if str(status_code) == "403":
        return f"{base}: 远端拒绝密码校验且未返回邮箱验证码流程，通常是代理/IP 风控或登录挑战"
    if fallback:
        return fallback
    return base


def _decode_jwt_payload(token: str) -> dict:
    try:
        payload = token.split(".")[1]
        padding = 4 - len(payload) % 4
        if padding != 4:
            payload += "=" * padding
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def _iso_from_unix(timestamp: int | float, tz: timezone = timezone.utc) -> str:
    return datetime.fromtimestamp(int(timestamp), tz=timezone.utc).astimezone(tz).isoformat(timespec="seconds")


def _organization_id_from_payload(payload: dict) -> str:
    auth = payload.get("https://api.openai.com/auth") if isinstance(payload, dict) else {}
    auth = auth if isinstance(auth, dict) else {}
    organizations = auth.get("organizations")
    if isinstance(organizations, list):
        for organization in organizations:
            if isinstance(organization, dict) and organization.get("id"):
                return str(organization.get("id") or "").strip()
    return ""


def _chatgpt_account_id_from_auth(auth: dict) -> str:
    account_id = str(auth.get("chatgpt_account_id") or "").strip()
    if account_id:
        return account_id
    account_user_id = str(auth.get("chatgpt_account_user_id") or "").strip()
    if "__" in account_user_id:
        return account_user_id.rsplit("__", 1)[1].strip()
    return ""


def _oauth_overrides(result: dict) -> dict:
    oauth = result.get("oauth") if isinstance(result.get("oauth"), dict) else {}
    return oauth


def _mailbox_public_metadata(mailbox: dict) -> dict:
    if not isinstance(mailbox, dict):
        return {}
    metadata: dict[str, str] = {}
    for key in (
        "provider",
        "provider_ref",
        "address",
        "email_id",
        "account_id",
        "base_domain",
        "mailbox_name",
        "domain",
        "api_base",
        "created_at",
        "expires_at",
    ):
        value = str(mailbox.get(key) or "").strip()
        if value:
            metadata[key] = value
    seen_refs = mailbox.get("seen_code_message_refs") or mailbox.get("_seen_code_message_refs")
    if isinstance(seen_refs, list):
        values = [str(item).strip() for item in seen_refs if str(item).strip()]
        if values:
            metadata["seen_code_message_refs"] = values[-20:]
    return metadata


def _result_mail_metadata(mailbox: dict) -> dict:
    metadata = _mailbox_public_metadata(mailbox)
    return {
        "mail_provider": metadata.get("provider", ""),
        "mail_provider_ref": metadata.get("provider_ref", ""),
        "mailbox": metadata,
    }


def _email_domain(email: str) -> str:
    _, _, domain = str(email or "").strip().lower().partition("@")
    return domain


def _mailbox_api_base(mailbox: dict) -> str:
    if not isinstance(mailbox, dict):
        return ""
    return str(mailbox.get("api_base") or "").strip()


def _record_domain_failure(email: str, reason: str, index: int) -> None:
    domain = _email_domain(email)
    if not domain:
        return
    text = str(reason or "").lower()
    hard_domain_markers = (
        "user_register_http_400",
        "create_account_http_400",
        "failed to create account",
    )
    otp_markers = (
        "等待注册验证码超时",
        "邮箱服务",
        "moemail 请求失败",
        "tempmail",
        "mail 请求失败",
    )
    if any(marker in text for marker in hard_domain_markers):
        threshold = 2
        ttl = 6 * 3600
    elif any(marker in text for marker in otp_markers):
        threshold = 3
        ttl = 1800
    else:
        return
    with domain_failure_lock:
        count = domain_failure_counts.get(domain, 0) + 1
        domain_failure_counts[domain] = count
    if count >= threshold:
        mail_provider.suppress_domain(domain, reason, ttl_seconds=ttl)
        step(index, f"邮箱域名 {domain} 连续失败 {count} 次，本轮临时跳过: {reason}", "yellow")


def build_sub2api_account(result: dict) -> dict:
    now = datetime.now(timezone.utc)
    access_token = str(result.get("access_token") or "").strip()
    id_token = str(result.get("id_token") or "").strip()
    access_payload = _decode_jwt_payload(access_token)
    id_payload = _decode_jwt_payload(id_token)
    access_auth = access_payload.get("https://api.openai.com/auth") if isinstance(access_payload, dict) else {}
    access_auth = access_auth if isinstance(access_auth, dict) else {}
    id_auth = id_payload.get("https://api.openai.com/auth") if isinstance(id_payload, dict) else {}
    id_auth = id_auth if isinstance(id_auth, dict) else {}
    profile = access_payload.get("https://api.openai.com/profile") if isinstance(access_payload, dict) else {}
    profile = profile if isinstance(profile, dict) else {}
    email = str(result.get("email") or profile.get("email") or id_payload.get("email") or "").strip()
    issued_at = int(access_payload.get("iat") or now.timestamp())
    expires_at = int(access_payload.get("exp") or issued_at)
    usage_updated_at = now.astimezone(timezone(timedelta(hours=8)))
    organization_id = _organization_id_from_payload(id_payload) or _organization_id_from_payload(access_payload)
    chatgpt_account_id = _chatgpt_account_id_from_auth(access_auth) or _chatgpt_account_id_from_auth(id_auth)
    oauth = _oauth_overrides(result)
    chatgpt_account_id = str(oauth.get("chatgpt_account_id") or chatgpt_account_id).strip()
    chatgpt_user_id = str(
        oauth.get("chatgpt_user_id")
        or access_auth.get("chatgpt_user_id")
        or access_auth.get("user_id")
        or id_auth.get("chatgpt_user_id")
        or id_auth.get("user_id")
        or ""
    ).strip()
    organization_id = str(oauth.get("organization_id") or organization_id).strip()
    client_id = str(oauth.get("client_id") or platform_oauth_client_id).strip()
    return {
        "name": email,
        "platform": "openai",
        "type": "oauth",
        "credentials": {
            "_token_version": issued_at * 1000,
            "access_token": access_token,
            "chatgpt_account_id": chatgpt_account_id,
            "chatgpt_user_id": chatgpt_user_id,
            "email": email,
            "expires_at": _iso_from_unix(expires_at, timezone(timedelta(hours=8))),
            "expires_in": max(0, expires_at - int(now.timestamp())),
            "id_token": id_token,
            "organization_id": organization_id,
            "refresh_token": str(result.get("refresh_token") or "").strip(),
            "client_id": client_id,
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


def build_account_pool_record(result: dict, register_job_id: str = "") -> dict:
    account = build_sub2api_account(result)
    credentials = account["credentials"]
    proxy = result.get("proxy") if isinstance(result.get("proxy"), dict) else {}
    record = {
        "access_token": credentials["access_token"],
        "email": credentials["email"] or None,
        "register_job_id": str(register_job_id or "").strip() or None,
        "login": {
            "email": credentials["email"],
            "password": str(result.get("password") or "").strip(),
        },
        "oauth": {
            "_token_version": credentials["_token_version"],
            "refresh_token": credentials["refresh_token"],
            "id_token": credentials["id_token"],
            "client_id": credentials["client_id"],
            "organization_id": credentials["organization_id"],
            "chatgpt_account_id": credentials["chatgpt_account_id"],
            "chatgpt_user_id": credentials["chatgpt_user_id"],
            "email": credentials["email"],
            "expires_at": credentials["expires_at"],
            "expires_in": credentials["expires_in"],
        },
    }
    if proxy.get("proxy_key"):
        record["proxy_key"] = str(proxy.get("proxy_key") or "").strip()
        record["proxy"] = dict(proxy)
    return record


def _mail_config_with_proxy(proxy: str = "") -> dict:
    mail_config = dict(config["mail"])
    mail_config["proxy"] = str(proxy or config.get("proxy") or "").strip()
    return mail_config


def create_mailbox(username: str | None = None, proxy: str = "") -> dict:
    return mail_provider.create_mailbox(_mail_config_with_proxy(proxy), username)


def wait_for_code(mailbox: dict, proxy: str = "") -> str | None:
    return mail_provider.wait_for_code(_mail_config_with_proxy(proxy), mailbox)


class SentinelTokenGenerator:
    MAX_ATTEMPTS = 500000
    ERROR_PREFIX = "wQ8Lk5FbGpA2NcR9dShT6gYjU7VxZ4D"

    def __init__(self, device_id: str, ua: str):
        self.device_id = device_id
        self.user_agent = ua
        self.sid = str(uuid.uuid4())

    @staticmethod
    def _fnv1a_32(text: str) -> str:
        h = 2166136261
        for ch in text:
            h ^= ord(ch)
            h = (h * 16777619) & 0xFFFFFFFF
        h ^= h >> 16
        h = (h * 2246822507) & 0xFFFFFFFF
        h ^= h >> 13
        h = (h * 3266489909) & 0xFFFFFFFF
        h ^= h >> 16
        return format(h & 0xFFFFFFFF, "08x")

    def _get_config(self) -> list:
        perf_now = random.uniform(1000, 50000)
        return [
            "1920x1080",
            time.strftime("%a %b %d %Y %H:%M:%S GMT+0000 (Coordinated Universal Time)", time.gmtime()),
            4294705152,
            random.random(),
            self.user_agent,
            "https://sentinel.openai.com/sentinel/20260124ceb8/sdk.js",
            None,
            None,
            "en-US",
            random.random(),
            random.choice(["vendorSub-undefined", "plugins-undefined", "mimeTypes-undefined", "hardwareConcurrency-undefined"]),
            random.choice(["location", "implementation", "URL", "documentURI", "compatMode"]),
            random.choice(["Object", "Function", "Array", "Number", "parseFloat", "undefined"]),
            perf_now,
            self.sid,
            "",
            random.choice([4, 8, 12, 16]),
            time.time() * 1000 - perf_now,
        ]

    @staticmethod
    def _b64(data) -> str:
        return base64.b64encode(json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).decode("ascii")

    def generate_requirements_token(self) -> str:
        data = self._get_config()
        data[3] = 1
        data[9] = round(random.uniform(5, 50))
        return "gAAAAAC" + self._b64(data)

    def generate_token(self, seed: str, difficulty: str) -> str:
        start = time.time()
        data = self._get_config()
        difficulty = str(difficulty or "0")
        for i in range(self.MAX_ATTEMPTS):
            data[3] = i
            data[9] = round((time.time() - start) * 1000)
            payload = self._b64(data)
            if self._fnv1a_32(seed + payload)[: len(difficulty)] <= difficulty:
                return "gAAAAAB" + payload + "~S"
        return "gAAAAAB" + self.ERROR_PREFIX + self._b64(str(None))


def build_sentinel_token(session: requests.Session, device_id: str, flow: str) -> str:
    generator = SentinelTokenGenerator(device_id, user_agent)
    resp = session.post(
        "https://sentinel.openai.com/backend-api/sentinel/req",
        data=json.dumps({"p": generator.generate_requirements_token(), "id": device_id, "flow": flow}),
        headers={
            "Content-Type": "text/plain;charset=UTF-8",
            "Referer": "https://sentinel.openai.com/backend-api/sentinel/frame.html",
            "Origin": "https://sentinel.openai.com",
            "User-Agent": user_agent,
            "sec-ch-ua": sec_ch_ua,
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
        },
        timeout=20,
        verify=False,
    )
    data = _response_json(resp)
    token = str(data.get("token") or "").strip()
    if resp.status_code != 200 or not token:
        raise RuntimeError(f"sentinel_req_failed_{resp.status_code}")
    pow_data = data.get("proofofwork") or {}
    p_value = (
        generator.generate_token(str(pow_data.get("seed") or ""), str(pow_data.get("difficulty") or "0"))
        if pow_data.get("required") and pow_data.get("seed")
        else generator.generate_requirements_token()
    )
    return json.dumps({"p": p_value, "t": "", "c": token, "id": device_id, "flow": flow}, separators=(",", ":"))


def _is_socks_proxy(proxy: str) -> bool:
    candidate = str(proxy or "").strip().lower()
    return candidate.startswith("socks5://") or candidate.startswith("socks5h://")


def create_session(proxy: str = "") -> Any:
    if _is_socks_proxy(proxy):
        return curl_requests.Session(impersonate="chrome", verify=False, proxy=proxy)
    session = requests.Session()
    retry = Retry(total=2, connect=2, read=2, backoff_factor=0.5, status_forcelist=(429, 500, 502, 503, 504))
    adapter = HTTPAdapter(max_retries=retry, pool_connections=50, pool_maxsize=50)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.verify = False
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})
    return session


def request_with_local_retry(session: requests.Session, method: str, url: str, retry_attempts: int = 3, **kwargs):
    last_error = ""
    for _ in range(max(1, retry_attempts)):
        try:
            return session.request(method.upper(), url, timeout=default_timeout, **kwargs), ""
        except Exception as error:
            last_error = str(error)
            time.sleep(1)
    return None, last_error


def validate_otp(session: requests.Session, device_id: str, code: str):
    headers = dict(common_headers)
    headers["referer"] = f"{auth_base}/email-verification"
    headers["oai-device-id"] = device_id
    headers.update(_make_trace_headers())
    resp, error = request_with_local_retry(session, "post", f"{auth_base}/api/accounts/email-otp/validate", json={"code": code}, headers=headers, verify=False)
    if resp is not None and resp.status_code == 200:
        return resp, ""
    headers["openai-sentinel-token"] = build_sentinel_token(session, device_id, "authorize_continue")
    resp, error = request_with_local_retry(session, "post", f"{auth_base}/api/accounts/email-otp/validate", json={"code": code}, headers=headers, verify=False)
    return resp, error


def extract_oauth_callback_params_from_url(url: str) -> dict[str, str] | None:
    if not url:
        return None
    try:
        params = parse_qs(urlparse(url).query)
    except Exception:
        return None
    code = str((params.get("code") or [""])[0]).strip()
    if not code:
        return None
    return {"code": code, "state": str((params.get("state") or [""])[0]).strip(), "scope": str((params.get("scope") or [""])[0]).strip()}


def extract_oauth_callback_params_from_consent_session(session: requests.Session, consent_url: str, device_id: str) -> dict[str, str] | None:
    if consent_url.startswith("/"):
        consent_url = f"{auth_base}{consent_url}"
    current_url = consent_url
    for _ in range(10):
        response = session.get(current_url, headers=navigate_headers, verify=False, timeout=30, allow_redirects=False)
        callback_params = extract_oauth_callback_params_from_url(str(response.url)) or extract_oauth_callback_params_from_url(str(response.headers.get("Location") or "").strip())
        if callback_params:
            return callback_params
        location = str(response.headers.get("Location") or "").strip()
        if response.status_code not in (301, 302, 303, 307, 308) or not location:
            break
        current_url = f"{auth_base}{location}" if location.startswith("/") else location
    raw = session.cookies.get("oai-client-auth-session", domain=".auth.openai.com") or session.cookies.get("oai-client-auth-session")
    if not raw:
        return None
    try:
        first_part = raw.split(".")[0]
        padding = 4 - len(first_part) % 4
        if padding != 4:
            first_part += "=" * padding
        payload = json.loads(base64.urlsafe_b64decode(first_part))
        workspace_id = payload["workspaces"][0]["id"]
    except Exception:
        return None
    headers = dict(common_headers)
    headers["referer"] = consent_url
    headers["oai-device-id"] = device_id
    headers.update(_make_trace_headers())
    ws_resp = session.post(f"{auth_base}/api/accounts/workspace/select", json={"workspace_id": workspace_id}, headers=headers, verify=False, timeout=30, allow_redirects=False)
    callback_params = extract_oauth_callback_params_from_url(str(ws_resp.headers.get("Location") or "").strip())
    if callback_params:
        return callback_params
    ws_data = _response_json(ws_resp)
    orgs = ((ws_data.get("data") or {}).get("orgs") or []) if isinstance(ws_data, dict) else []
    if not orgs:
        return None
    org_id = str((orgs[0] or {}).get("id") or "").strip()
    project_id = str(((orgs[0] or {}).get("projects") or [{}])[0].get("id") or "").strip()
    if not org_id:
        return None
    org_headers = dict(common_headers)
    org_headers["referer"] = str(ws_data.get("continue_url") or consent_url)
    org_headers["oai-device-id"] = device_id
    org_headers.update(_make_trace_headers())
    body = {"org_id": org_id}
    if project_id:
        body["project_id"] = project_id
    org_resp = session.post(f"{auth_base}/api/accounts/organization/select", json=body, headers=org_headers, verify=False, timeout=30, allow_redirects=False)
    return extract_oauth_callback_params_from_url(str(org_resp.headers.get("Location") or "").strip())


def exchange_platform_tokens(session: requests.Session, device_id: str, code_verifier: str, consent_url: str) -> dict | None:
    callback_params = extract_oauth_callback_params_from_consent_session(session, consent_url, device_id)
    if not callback_params:
        # 回退方案：直接导航 consent URL（allow_redirects=True），从最终 URL 提取 code
        print(f"[exchange_platform_tokens] 主方案失败，尝试回退方案, continue_url={consent_url[:120]}")
        try:
            r = session.get(consent_url, headers=navigate_headers, allow_redirects=True, verify=False, timeout=30)
            final_url = str(r.url)
            print(f"[exchange_platform_tokens] 回退 final_url={final_url[:120]}")
            callback_params = extract_oauth_callback_params_from_url(final_url)
            if not callback_params:
                for hist in getattr(r, "history", []) or []:
                    loc = str(hist.headers.get("Location") or "")
                    callback_params = extract_oauth_callback_params_from_url(loc)
                    if callback_params:
                        break
        except Exception as e:
            print(f"[exchange_platform_tokens] 回退方案异常: {e}")
    if not callback_params:
        print("[exchange_platform_tokens] 所有方案均无法提取 OAuth code")
        return None
    code = str(callback_params.get("code") or "").strip()
    if not code:
        return None
    resp = session.post(
        f"{auth_base}/oauth/token",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": platform_oauth_redirect_uri,
            "client_id": platform_oauth_client_id,
            "code_verifier": code_verifier,
        },
        verify=False,
        timeout=60,
    )
    data = _response_json(resp)
    if resp.status_code != 200 or not data.get("access_token") or not data.get("refresh_token") or not data.get("id_token"):
        detail = f", detail={json.dumps(data, ensure_ascii=False)}" if data else ""
        raise RuntimeError(f"token_exchange_http_{resp.status_code}{detail}")
    payload = _decode_jwt_payload(str(data.get("id_token") or "")) or _decode_jwt_payload(str(data.get("access_token") or ""))
    return {
        "email": str(payload.get("email") or "").strip(),
        "access_token": str(data.get("access_token") or "").strip(),
        "refresh_token": str(data.get("refresh_token") or "").strip(),
        "id_token": str(data.get("id_token") or "").strip(),
    }


class PlatformRegistrar:
    def __init__(self, proxy: str = "") -> None:
        self.proxy = str(proxy or "").strip()
        self.session = create_session(proxy)
        self.device_id = str(uuid.uuid4())

    def close(self) -> None:
        self.session.close()

    def _navigate_headers(self, referer: str = "") -> dict[str, str]:
        headers = dict(navigate_headers)
        if referer:
            headers["referer"] = referer
        return headers

    def _json_headers(self, referer: str) -> dict[str, str]:
        headers = dict(common_headers)
        headers["referer"] = referer
        headers["oai-device-id"] = self.device_id
        headers.update(_make_trace_headers())
        return headers

    def _platform_authorize(self, email: str, index: int) -> None:
        step(index, "开始 platform authorize")
        self.session.cookies.set("oai-did", self.device_id, domain=".auth.openai.com")
        self.session.cookies.set("oai-did", self.device_id, domain="auth.openai.com")
        _, code_challenge = _generate_pkce()
        params = {
            "issuer": auth_base,
            "client_id": platform_oauth_client_id,
            "audience": platform_oauth_audience,
            "redirect_uri": platform_oauth_redirect_uri,
            "device_id": self.device_id,
            "screen_hint": "login_or_signup",
            "max_age": "0",
            "login_hint": email,
            "scope": "openid profile email offline_access",
            "response_type": "code",
            "response_mode": "query",
            "state": secrets.token_urlsafe(32),
            "nonce": secrets.token_urlsafe(32),
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "auth0Client": platform_auth0_client,
        }
        resp, error = request_with_local_retry(self.session, "get", f"{auth_base}/api/accounts/authorize?{urlencode(params)}", headers=self._navigate_headers(f"{platform_base}/"), allow_redirects=True, verify=False)
        if resp is None or resp.status_code != 200:
            err = _response_json(resp).get("error", {}) if resp is not None else {}
            detail = f": {err.get('code', '')} - {err.get('message', '')}".strip(" -") if err else ""
            raise RuntimeError(error or f"platform_authorize_http_{getattr(resp, 'status_code', 'unknown')}{detail}")
        step(index, "platform authorize 完成")

    def _register_user(self, email: str, password: str, index: int) -> None:
        step(index, "开始提交注册密码")
        headers = self._json_headers(f"{auth_base}/create-account/password")
        headers["openai-sentinel-token"] = build_sentinel_token(self.session, self.device_id, "username_password_create")
        resp, error = request_with_local_retry(self.session, "post", f"{auth_base}/api/accounts/user/register", json={"username": email, "password": password}, headers=headers, verify=False)
        if resp is None or resp.status_code != 200:
            data = _response_json(resp) if resp is not None else {}
            if data.get("message") == "Failed to create account. Please try again.":
                step(index, "注册失败提示: 邮箱域名很可能因滥用被封禁，请更换邮箱域名", "yellow")
            detail = f", detail={json.dumps(data, ensure_ascii=False)}" if data else ""
            raise RuntimeError(error or f"user_register_http_{getattr(resp, 'status_code', 'unknown')}{detail}")
        step(index, "提交注册密码完成")

    def _send_otp(self, index: int) -> None:
        step(index, "开始发送验证码")
        resp, error = request_with_local_retry(self.session, "get", f"{auth_base}/api/accounts/email-otp/send", headers=self._navigate_headers(f"{auth_base}/create-account/password"), allow_redirects=True, verify=False)
        if resp is None or resp.status_code not in (200, 302):
            raise RuntimeError(error or f"send_otp_http_{getattr(resp, 'status_code', 'unknown')}")
        step(index, "发送验证码完成")

    def _validate_otp(self, code: str, index: int) -> None:
        step(index, f"开始校验验证码 {code}")
        resp, error = validate_otp(self.session, self.device_id, code)
        if resp is None or resp.status_code != 200:
            body = ""
            try:
                body = (resp.text or "")[:500] if resp is not None else ""
            except Exception:
                pass
            raise RuntimeError(error or f"validate_otp_http_{getattr(resp, 'status_code', 'unknown')}_body={body}")
        step(index, "验证码校验完成")

    def _create_account(self, name: str, birthdate: str, index: int) -> None:
        step(index, "开始创建账号资料")
        headers = self._json_headers(f"{auth_base}/about-you")
        headers["openai-sentinel-token"] = build_sentinel_token(self.session, self.device_id, "oauth_create_account")
        resp, error = request_with_local_retry(self.session, "post", f"{auth_base}/api/accounts/create_account", json={"name": name, "birthdate": birthdate}, headers=headers, verify=False)
        if resp is None or resp.status_code not in (200, 302):
            data = _response_json(resp) if resp is not None else {}
            if data.get("message") == "Failed to create account. Please try again.":
                step(index, "创建账号失败提示: 邮箱域名很可能因滥用被封禁，请更换邮箱域名", "yellow")
            detail = f", detail={json.dumps(data, ensure_ascii=False)}" if data else ""
            raise RuntimeError(error or f"create_account_http_{getattr(resp, 'status_code', 'unknown')}{detail}")
        step(index, "创建账号资料完成")

    def _login_authorize(self, email: str, index: int, code_verifier: str = "", code_challenge: str = "") -> tuple[str, str]:
        if not code_verifier or not code_challenge:
            code_verifier, code_challenge = _generate_pkce()
        params = {
            "issuer": auth_base,
            "client_id": platform_oauth_client_id,
            "audience": platform_oauth_audience,
            "redirect_uri": platform_oauth_redirect_uri,
            "device_id": self.device_id,
            "screen_hint": "login_or_signup",
            "max_age": "0",
            "login_hint": email,
            "scope": "openid profile email offline_access",
            "response_type": "code",
            "response_mode": "query",
            "state": secrets.token_urlsafe(32),
            "nonce": secrets.token_urlsafe(32),
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "auth0Client": platform_auth0_client,
        }
        resp, error = request_with_local_retry(self.session, "get", f"{auth_base}/api/accounts/authorize?{urlencode(params)}", headers=self._navigate_headers(f"{platform_base}/"), allow_redirects=True, verify=False)
        if resp is None or resp.status_code >= 400:
            raise RuntimeError(error or f"platform_login_authorize_http_{getattr(resp, 'status_code', 'unknown')}")
        step(index, "登录 authorize 完成")
        return code_verifier, str(resp.url or "")

    def _authorize_continue_login(self, email: str, index: int, code_verifier: str, code_challenge: str) -> None:
        def submit_email():
            headers = self._json_headers(f"{auth_base}/log-in?usernameKind=email")
            headers["openai-sentinel-token"] = build_sentinel_token(self.session, self.device_id, "authorize_continue")
            return request_with_local_retry(
                self.session,
                "post",
                f"{auth_base}/api/accounts/authorize/continue",
                json={"username": {"kind": "email", "value": email}},
                headers=headers,
                allow_redirects=False,
                verify=False,
            )

        step(index, "开始提交邮箱")
        resp, error = submit_email()
        if resp is not None and resp.status_code == 409:
            step(index, "邮箱提交 invalid_state，重新 authorize 后重试", "yellow")
            for cookie in list(self.session.cookies):
                if "auth.openai.com" in cookie.domain:
                    self.session.cookies.clear(domain=cookie.domain, path=cookie.path, name=cookie.name)
            self.session.cookies.set("oai-did", self.device_id, domain=".auth.openai.com")
            self.session.cookies.set("oai-did", self.device_id, domain="auth.openai.com")
            self._login_authorize(email, index, code_verifier, code_challenge)
            resp, error = submit_email()

        if resp is None or resp.status_code != 200:
            data = _response_json(resp) if resp is not None else {}
            detail = json.dumps(data, ensure_ascii=False) if data else ""
            raise RuntimeError(
                error
                or f"email_submit_http_{getattr(resp, 'status_code', 'unknown')}"
                + (f": {detail}" if detail else "")
            )
        step(index, "邮箱提交完成")

    def _verify_password_with_retry(self, password: str, index: int) -> dict:
        last_error = ""
        for attempt in range(1, 3):
            headers = self._json_headers(f"{auth_base}/log-in/password")
            headers["openai-sentinel-token"] = build_sentinel_token(self.session, self.device_id, "password_verify")
            resp, error = request_with_local_retry(self.session, "post", f"{auth_base}/api/accounts/password/verify", json={"password": password}, headers=headers, allow_redirects=False, verify=False)
            if resp is not None and resp.status_code == 200:
                step(index, "密码校验完成")
                return _response_json(resp)
            payload = _response_json(resp) if resp is not None else {}
            raw_text = f"{_response_text(resp)} {getattr(resp, 'url', '')}" if resp is not None else ""
            if resp is not None and _password_verify_requires_email_otp(payload, raw_text):
                step(index, f"密码校验返回邮箱验证码流程（HTTP {resp.status_code}），继续读取注册邮箱验证码", "yellow")
                if not isinstance(payload, dict):
                    payload = {}
                if not str(payload.get("continue_url") or "").strip():
                    payload["continue_url"] = f"{auth_base}/email-verification"
                if not isinstance(payload.get("page"), dict):
                    payload["page"] = {"type": "email_otp_verification"}
                return payload
            last_error = error or _password_verify_error(getattr(resp, "status_code", "unknown"), payload)
            if self._is_retryable_login_error(last_error) and attempt < 2:
                step(index, f"密码校验失败 {getattr(resp, 'status_code', 'network')}，等待后重试 {attempt}/2", "yellow")
                time.sleep(2 * attempt)
                continue
            raise RuntimeError(last_error)
        raise RuntimeError(last_error or "password_verify_failed")

    def _run_retryable_step(self, index: int, label: str, action, attempts: int = 3):
        last_error = ""
        for attempt in range(1, max(1, attempts) + 1):
            try:
                return action()
            except RuntimeError as exc:
                last_error = str(exc) or exc.__class__.__name__
                if self._is_retryable_register_error(last_error) and attempt < attempts:
                    step(index, f"{label}遇到临时错误，等待后重试 {attempt}/{attempts}: {last_error}", "yellow")
                    time.sleep(2 * attempt)
                    continue
                raise
        raise RuntimeError(last_error or f"{label}失败")

    @staticmethod
    def _is_retryable_register_error(message: str) -> bool:
        text = str(message or "").lower()
        retryable_markers = (
            "platform_authorize_http_408",
            "platform_authorize_http_409",
            "platform_authorize_http_425",
            "platform_authorize_http_429",
            "platform_authorize_http_500",
            "platform_authorize_http_502",
            "platform_authorize_http_503",
            "platform_authorize_http_504",
            "user_register_http_408",
            "user_register_http_425",
            "user_register_http_429",
            "user_register_http_500",
            "user_register_http_502",
            "user_register_http_503",
            "user_register_http_504",
            "send_otp_http_408",
            "send_otp_http_409",
            "send_otp_http_425",
            "send_otp_http_429",
            "send_otp_http_500",
            "send_otp_http_502",
            "send_otp_http_503",
            "send_otp_http_504",
            "validate_otp_http_408",
            "validate_otp_http_409",
            "validate_otp_http_425",
            "validate_otp_http_429",
            "validate_otp_http_500",
            "validate_otp_http_502",
            "validate_otp_http_503",
            "validate_otp_http_504",
            "create_account_http_408",
            "create_account_http_409",
            "create_account_http_425",
            "create_account_http_429",
            "create_account_http_500",
            "create_account_http_502",
            "create_account_http_503",
            "create_account_http_504",
        )
        return PlatformRegistrar._is_retryable_login_error(text) or any(marker in text for marker in retryable_markers)

    @staticmethod
    def _is_retryable_login_error(message: str) -> bool:
        text = str(message or "").lower()
        if "deleted or deactivated" in text or "account because it has been deleted" in text:
            return False
        retryable_markers = (
            "password_verify_http_409",
            "password_verify_http_408",
            "password_verify_http_425",
            "password_verify_http_429",
            "password_verify_http_500",
            "password_verify_http_502",
            "password_verify_http_503",
            "password_verify_http_504",
            "platform_login_authorize_failed",
            "platform_login_authorize_http_408",
            "platform_login_authorize_http_409",
            "platform_login_authorize_http_425",
            "platform_login_authorize_http_429",
            "platform_login_authorize_http_500",
            "platform_login_authorize_http_502",
            "platform_login_authorize_http_503",
            "platform_login_authorize_http_504",
            "token换取失败",
            "token_exchange_http_408",
            "token_exchange_http_409",
            "token_exchange_http_425",
            "token_exchange_http_429",
            "token_exchange_http_500",
            "token_exchange_http_502",
            "token_exchange_http_503",
            "token_exchange_http_504",
            "sentinel_req_failed_429",
            "sentinel_req_failed_500",
            "sentinel_req_failed_502",
            "sentinel_req_failed_503",
            "sentinel_req_failed_504",
            "timed out",
            "timeout",
            "connection",
            "temporarily unavailable",
            "remote end closed",
            "proxy",
            "tls",
            "ssl",
        )
        return any(marker in text for marker in retryable_markers)

    def _begin_login_for_tokens(self, email: str, password: str, index: int) -> tuple[str, str, bool]:
        step(index, "开始独立登录换 token")
        code_verifier = ""
        payload = {}
        for attempt in range(1, 3):
            try:
                code_verifier, code_challenge = _generate_pkce()
                code_verifier, _ = self._login_authorize(email, index, code_verifier, code_challenge)
                self._authorize_continue_login(email, index, code_verifier, code_challenge)
                payload = self._verify_password_with_retry(password, index)
                break
            except RuntimeError as exc:
                if self._is_retryable_login_error(str(exc)) and attempt < 2:
                    step(index, f"独立登录状态未稳定，重新 authorize 后重试 {attempt}/2: {exc}", "yellow")
                    time.sleep(3 * attempt)
                    continue
                raise
        continue_url = str(payload.get("continue_url") or "").strip()
        page_type = str(((payload.get("page") or {}).get("type")) or "")
        if not continue_url:
            continue_url = f"{auth_base}/sign-in-with-chatgpt/codex/consent"
        needs_email_otp = page_type == "email_otp_verification" or "email-verification" in continue_url or "email-otp" in continue_url
        return code_verifier, continue_url, needs_email_otp

    def _complete_email_otp_for_tokens(self, code: str, continue_url: str, index: int) -> str:
        resp, reason = validate_otp(self.session, self.device_id, code)
        if resp is None or resp.status_code != 200:
            print("独立登录验证码校验失败响应:", resp.text if resp is not None else "None")
            data = _response_json(resp) if resp is not None else {}
            message = str((data.get("error") or {}).get("message") or data.get("message") or "").strip()
            raise RuntimeError(reason or f"独立登录验证码校验失败{': ' + message if message else ''}")
        otp_payload = _response_json(resp)
        next_continue_url = str(otp_payload.get("continue_url") or continue_url).strip()
        step(index, "独立登录验证码校验完成")
        return next_continue_url

    def _exchange_tokens_after_login(self, code_verifier: str, continue_url: str, index: int) -> dict:
        tokens = None
        for attempt in range(1, 4):
            try:
                tokens = exchange_platform_tokens(self.session, self.device_id, code_verifier, continue_url)
            except RuntimeError as exc:
                if self._is_retryable_login_error(str(exc)) and attempt < 3:
                    step(index, f"token 换取失败，等待后重试 {attempt}/3: {exc}", "yellow")
                    time.sleep(2 * attempt)
                    continue
                raise
            if tokens:
                break
            if attempt < 3:
                step(index, f"token 换取未拿到回调参数，等待后重试 {attempt}/3", "yellow")
                time.sleep(2 * attempt)
        if not tokens:
            raise RuntimeError("token换取失败")
        step(index, "token 换取完成")
        return tokens

    def _login_and_exchange_tokens(self, email: str, password: str, mailbox: dict, index: int, manual_code: str = "") -> dict:
        code_verifier, continue_url, needs_email_otp = self._begin_login_for_tokens(email, password, index)
        if needs_email_otp:
            step(index, "独立登录需要邮箱验证码")
            code = str(manual_code or "").strip()
            if code:
                step(index, "使用手动输入的邮箱验证码")
            else:
                code = wait_for_code(mailbox, self.proxy) or ""
            if not code:
                wait_error = str(mailbox.get("_last_wait_error") or "").strip()
                raise RuntimeError(f"独立登录等待验证码超时{': ' + wait_error if wait_error else ''}")
            continue_url = self._complete_email_otp_for_tokens(code, continue_url, index)
        return self._exchange_tokens_after_login(code_verifier, continue_url, index)

    def begin_manual_existing_account_recovery(self, email: str, password: str, index: int) -> dict:
        code_verifier, continue_url, needs_email_otp = self._begin_login_for_tokens(email, password, index)
        if not needs_email_otp:
            tokens = self._exchange_tokens_after_login(code_verifier, continue_url, index)
            return {
                "status": "complete",
                "tokens": tokens,
            }
        step(index, "独立登录需要邮箱验证码，等待手动输入")
        return {
            "status": "manual_code_required",
            "code_verifier": code_verifier,
            "continue_url": continue_url,
        }

    def complete_manual_existing_account_recovery(self, code_verifier: str, continue_url: str, code: str, index: int) -> dict:
        continue_url = self._complete_email_otp_for_tokens(code, continue_url, index)
        return self._exchange_tokens_after_login(code_verifier, continue_url, index)

    def register(self, index: int) -> dict:
        step(index, "开始创建邮箱")
        mailbox = create_mailbox(proxy=self.proxy)
        email = str(mailbox.get("address") or "").strip()
        if not email:
            raise RuntimeError("邮箱服务未返回 address")
        label = str(mailbox.get("label") or "")
        step(index, f"邮箱创建完成[{label}]: {email}")
        password = _random_password()
        first_name, last_name = _random_name()
        try:
            self._run_retryable_step(index, "platform authorize", lambda: self._platform_authorize(email, index))
            self._run_retryable_step(index, "提交注册密码", lambda: self._register_user(email, password, index))
            self._run_retryable_step(index, "发送验证码", lambda: self._send_otp(index))
            step(index, "开始等待注册验证码")
            code = wait_for_code(mailbox, self.proxy)
            if not code:
                wait_error = str(mailbox.get("_last_wait_error") or "").strip()
                raise RuntimeError(f"等待注册验证码超时{': ' + wait_error if wait_error else ''}")
            step(index, f"收到注册验证码: {code}")
            self._run_retryable_step(index, "校验注册验证码", lambda: self._validate_otp(code, index), attempts=2)
            self._run_retryable_step(index, "创建账号资料", lambda: self._create_account(f"{first_name} {last_name}", _random_birthdate(), index))
            try:
                tokens = self._login_and_exchange_tokens(email, password, mailbox, index)
            except Exception as exc:
                raise RegisteredAccountAuthError(email, password, mailbox, str(exc) or exc.__class__.__name__) from exc
        except RegisteredAccountAuthError:
            raise
        except Exception as exc:
            _record_domain_failure(email, str(exc), index)
            raise
        return {
            "email": email,
            "password": password,
            **_result_mail_metadata(mailbox),
            "access_token": str(tokens.get("access_token") or "").strip(),
            "refresh_token": str(tokens.get("refresh_token") or "").strip(),
            "id_token": str(tokens.get("id_token") or "").strip(),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

    def recover_existing_account(self, email: str, password: str, mailbox: dict, index: int, manual_code: str = "") -> dict:
        tokens = self._login_and_exchange_tokens(email, password, mailbox, index, manual_code)
        return {
            "email": email,
            "password": password,
            **_result_mail_metadata(mailbox),
            "access_token": str(tokens.get("access_token") or "").strip(),
            "refresh_token": str(tokens.get("refresh_token") or "").strip(),
            "id_token": str(tokens.get("id_token") or "").strip(),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "recovered": True,
        }


def reset_thread_proxy_pool() -> None:
    global thread_proxy_pool, thread_proxy_index
    with thread_proxy_lock:
        proxies = load_available_proxies()
        random.shuffle(proxies)
        thread_proxy_pool = proxies
        thread_proxy_index = 0
    with domain_failure_lock:
        domain_failure_counts.clear()
    mail_provider.clear_suppressed_domains()


def _thread_proxy() -> dict | None:
    global thread_proxy_index
    if not hasattr(thread_proxy_state, "proxy"):
        with thread_proxy_lock:
            if not thread_proxy_pool:
                thread_proxy_state.proxy = None
            else:
                thread_proxy_state.proxy = dict(thread_proxy_pool[thread_proxy_index % len(thread_proxy_pool)])
                thread_proxy_index += 1
    proxy = getattr(thread_proxy_state, "proxy", None)
    return dict(proxy) if isinstance(proxy, dict) else None


def _proxy_label(proxy: dict | None) -> str:
    if not isinstance(proxy, dict):
        return "全局代理/直连"
    return str(proxy.get("name") or proxy.get("proxy_key") or proxy_url(proxy) or "未命名代理")


def _recovery_proxies(current_proxy: dict | None, limit: int = 3) -> list[dict | None]:
    current_key = str((current_proxy or {}).get("proxy_key") or "").strip()
    candidates: list[dict | None] = []
    seen = {current_key} if current_key else set()
    proxies = load_available_proxies()
    random.shuffle(proxies)
    for proxy in proxies:
        key = str(proxy.get("proxy_key") or "").strip()
        if key and key in seen:
            continue
        candidates.append(proxy)
        if key:
            seen.add(key)
        if len(candidates) >= limit:
            break
    if not candidates and current_proxy is None and config["proxy"]:
        candidates.append(None)
    return candidates


def _recover_registered_account(
    error: RegisteredAccountAuthError,
    current_proxy: dict | None,
    index: int,
) -> tuple[dict | None, dict | None, str]:
    last_error = error.reason
    if not PlatformRegistrar._is_retryable_login_error(last_error):
        return None, None, last_error
    for attempt, proxy in enumerate(_recovery_proxies(current_proxy), start=1):
        step(index, f"账号已创建但鉴权失败，尝试换代理恢复登录 {attempt}: {_proxy_label(proxy)}", "yellow")
        registrar = PlatformRegistrar(proxy_url(proxy) if proxy else config["proxy"])
        try:
            result = registrar.recover_existing_account(error.email, error.password, error.mailbox, index)
            if proxy:
                result["proxy"] = proxy
            step(index, "换代理恢复登录成功", "green")
            return result, proxy, ""
        except Exception as exc:
            last_error = str(exc) or exc.__class__.__name__
            step(index, f"换代理恢复登录失败: {last_error}", "yellow")
            if not PlatformRegistrar._is_retryable_login_error(last_error):
                break
            time.sleep(2 * attempt)
        finally:
            registrar.close()
    return None, None, last_error


def worker(index: int, register_job_id: str = "") -> dict:
    start = time.time()
    selected_proxy = _thread_proxy()
    registrar_proxy = proxy_url(selected_proxy) if selected_proxy else config["proxy"]
    registrar = PlatformRegistrar(registrar_proxy)
    try:
        step(index, "任务启动")
        if selected_proxy:
            step(index, f"使用注册代理: {selected_proxy.get('name') or selected_proxy.get('proxy_key')}")
        try:
            result = registrar.register(index)
        except RegisteredAccountAuthError as auth_error:
            step(index, f"账号已创建，独立登录/换 token 失败: {auth_error.reason}", "yellow")
            result, recovered_proxy, recovery_error = _recover_registered_account(auth_error, selected_proxy, index)
            if not result:
                partial = {
                    "email": auth_error.email,
                    "password": auth_error.password,
                    **_result_mail_metadata(auth_error.mailbox),
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "auth_failed": True,
                    "error": recovery_error,
                }
                if selected_proxy:
                    partial["proxy"] = selected_proxy
                api_base = _mailbox_api_base(auth_error.mailbox) or "unknown"
                step(index, f"账号已创建但鉴权失败，需清理邮箱: {auth_error.email}，api_base={api_base}，原因: {recovery_error}", "red")
                raise RuntimeError(f"账号已创建但鉴权失败，恢复失败: {recovery_error}") from auth_error
            selected_proxy = recovered_proxy
        if selected_proxy:
            result["proxy"] = selected_proxy
        cost = time.time() - start
        access_token = str(result["access_token"])
        with stats_lock:
            stats["done"] += 1
            stats["success"] += 1
            avg = (time.time() - stats["start_time"]) / stats["success"]
        log(f'{result["email"]} 注册成功，本次耗时{cost:.1f}s，全局平均每个号注册耗时{avg:.1f}s', "green")
        return {"ok": True, "index": index, "result": result}
    except Exception as e:
        cost = time.time() - start
        with stats_lock:
            stats["done"] += 1
            stats["fail"] += 1
        registered = locals().get("partial")
        failed_email = ""
        failed_api_base = ""
        if isinstance(registered, dict):
            failed_email = str(registered.get("email") or "").strip()
            failed_mailbox = registered.get("mailbox") if isinstance(registered.get("mailbox"), dict) else {}
            failed_api_base = _mailbox_api_base(failed_mailbox)
        if not failed_email:
            failed_email = str(locals().get("email") or "").strip()
        if failed_email:
            log(
                f"任务{index} 注册失败，本次耗时{cost:.1f}s，邮箱={failed_email}，api_base={failed_api_base or 'unknown'}，原因: {e}",
                "red",
            )
        else:
            log(f"任务{index} 注册失败，本次耗时{cost:.1f}s，原因: {e}", "red")
        _record_domain_failure(failed_email, str(e), index)
        return {
            "ok": False,
            "index": index,
            "error": str(e),
            "registered": registered if isinstance(registered, dict) else None,
        }
    finally:
        registrar.close()
