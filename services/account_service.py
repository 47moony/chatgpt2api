from __future__ import annotations

import base64
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from threading import Condition, Lock
from typing import Any

from curl_cffi.requests import Session

from services.config import config
from services.log_service import (
    LOG_TYPE_ACCOUNT,
    log_service,
)
from services.proxy_pool_service import proxy_by_key, proxy_url
from services.storage.base import StorageBackend
from utils.helper import anonymize_token


OPENAI_AUTH_BASE = "https://auth.openai.com"
OPENAI_REFRESH_SCOPE = "openid profile email"


class AccountService:
    """账号池服务，使用 token -> account 的 dict 保存账号。"""

    def __init__(self, storage_backend: StorageBackend):
        self.storage = storage_backend
        self._lock = Lock()
        self._image_slot_condition = Condition(self._lock)
        self._index = 0
        self._accounts = self._load_accounts()
        self._image_inflight: dict[str, int] = {}

    def _load_accounts(self) -> dict[str, dict]:
        accounts = self.storage.load_accounts()
        return {
            normalized["access_token"]: normalized
            for item in accounts
            if (normalized := self._normalize_account(item)) is not None
        }

    def _save_accounts(self) -> None:
        self.storage.save_accounts(list(self._accounts.values()))

    @staticmethod
    def _is_image_account_available(account: dict) -> bool:
        if not isinstance(account, dict):
            return False
        if account.get("status") in {"禁用", "限流", "异常"}:
            return False
        if bool(account.get("image_quota_unknown")):
            return True
        return int(account.get("quota") or 0) > 0

    def _normalize_account(self, item: dict) -> dict | None:
        if not isinstance(item, dict):
            return None
        access_token = item.get("access_token") or ""
        if not access_token:
            return None
        normalized = dict(item)
        normalized["access_token"] = access_token
        normalized["type"] = normalized.get("type") or "free"
        normalized["status"] = normalized.get("status") or "正常"
        normalized["quota"] = max(0, int(normalized.get("quota") if normalized.get("quota") is not None else 0))
        normalized["image_quota_unknown"] = bool(normalized.get("image_quota_unknown"))
        normalized["email"] = normalized.get("email") or None
        normalized["user_id"] = normalized.get("user_id") or None
        limits_progress = normalized.get("limits_progress")
        normalized["limits_progress"] = limits_progress if isinstance(limits_progress, list) else []
        normalized["default_model_slug"] = normalized.get("default_model_slug") or None
        normalized["restore_at"] = normalized.get("restore_at") or None
        normalized["success"] = int(normalized.get("success") or 0)
        normalized["fail"] = int(normalized.get("fail") or 0)
        normalized["last_used_at"] = normalized.get("last_used_at")
        return normalized

    @staticmethod
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

    @staticmethod
    def _auth_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
        auth = payload.get("https://api.openai.com/auth") if isinstance(payload, dict) else {}
        return auth if isinstance(auth, dict) else {}

    @staticmethod
    def _profile_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
        profile = payload.get("https://api.openai.com/profile") if isinstance(payload, dict) else {}
        return profile if isinstance(profile, dict) else {}

    @staticmethod
    def _chatgpt_account_id_from_auth(auth: dict[str, Any]) -> str:
        account_id = str(auth.get("chatgpt_account_id") or "").strip()
        if account_id:
            return account_id
        account_user_id = str(auth.get("chatgpt_account_user_id") or "").strip()
        if "__" in account_user_id:
            suffix = account_user_id.rsplit("__", 1)[1].strip()
            if suffix:
                return suffix
        return ""

    @staticmethod
    def _organization_id_from_auth(auth: dict[str, Any]) -> str:
        organization_id = str(auth.get("poid") or auth.get("organization_id") or "").strip()
        if organization_id:
            return organization_id
        organizations = auth.get("organizations")
        if isinstance(organizations, list):
            default_id = ""
            first_id = ""
            for organization in organizations:
                if not isinstance(organization, dict):
                    continue
                current = str(organization.get("id") or "").strip()
                if not current:
                    continue
                first_id = first_id or current
                if bool(organization.get("is_default")):
                    default_id = current
                    break
            return default_id or first_id
        return ""

    def _local_oauth_metadata(self, account: dict[str, Any]) -> dict[str, Any]:
        oauth = account.get("oauth") if isinstance(account.get("oauth"), dict) else {}
        access_token = str(account.get("access_token") or "").strip()
        access_payload = self._decode_jwt_payload(access_token)
        id_payload = self._decode_jwt_payload(str(oauth.get("id_token") or "").strip())
        access_auth = self._auth_from_payload(access_payload)
        id_auth = self._auth_from_payload(id_payload)
        profile = self._profile_from_payload(access_payload)

        metadata = {
            "chatgpt_account_id": (
                str(oauth.get("chatgpt_account_id") or "").strip()
                or self._chatgpt_account_id_from_auth(access_auth)
                or self._chatgpt_account_id_from_auth(id_auth)
            ),
            "chatgpt_user_id": (
                str(oauth.get("chatgpt_user_id") or "").strip()
                or str(access_auth.get("chatgpt_user_id") or access_auth.get("user_id") or "").strip()
                or str(id_auth.get("chatgpt_user_id") or id_auth.get("user_id") or "").strip()
                or str(account.get("user_id") or "").strip()
            ),
            "organization_id": (
                str(oauth.get("organization_id") or "").strip()
                or self._organization_id_from_auth(id_auth)
                or self._organization_id_from_auth(access_auth)
            ),
            "email": (
                str(oauth.get("email") or "").strip()
                or str(account.get("email") or "").strip()
                or str(profile.get("email") or id_payload.get("email") or access_payload.get("email") or "").strip()
            ),
            "plan_type": (
                str(oauth.get("plan_type") or "").strip()
                or str(access_auth.get("chatgpt_plan_type") or id_auth.get("chatgpt_plan_type") or "").strip()
                or str(account.get("type") or "").strip()
            ),
        }
        return {key: value for key, value in metadata.items() if value}

    @staticmethod
    def _proxy_url_for_account(account: dict[str, Any]) -> str:
        account_proxy = account.get("proxy") if isinstance(account.get("proxy"), dict) else None
        resolved = proxy_url(account_proxy) if account_proxy else ""
        if resolved:
            return resolved
        proxy_ref = proxy_by_key(str(account.get("proxy_key") or ""))
        resolved = proxy_url(proxy_ref) if proxy_ref else ""
        return resolved or config.get_proxy_settings()

    @staticmethod
    def _iso_from_unix(timestamp: int | float) -> str:
        return datetime.fromtimestamp(int(timestamp), tz=timezone.utc).astimezone(timezone(timedelta(hours=8))).isoformat(timespec="seconds")

    def _refresh_oauth_token(self, account: dict[str, Any]) -> dict[str, Any] | None:
        if str(account.get("credential_owner") or "").strip() == "sub2api" or str(account.get("sub2api_account_id") or "").strip():
            return None
        oauth = account.get("oauth") if isinstance(account.get("oauth"), dict) else {}
        refresh_token = str(oauth.get("refresh_token") or "").strip()
        if not refresh_token:
            return None

        client_id = str(oauth.get("client_id") or "").strip()
        if not client_id:
            raise RuntimeError("refresh_token_missing_client_id")
        proxy = self._proxy_url_for_account(account)
        last_error = ""
        session_kwargs: dict[str, Any] = {"impersonate": "chrome", "verify": True}
        if proxy:
            session_kwargs["proxy"] = proxy
        session = Session(**session_kwargs)
        try:
            response = session.post(
                f"{OPENAI_AUTH_BASE}/oauth/token",
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/x-www-form-urlencoded",
                    "User-Agent": "codex-cli/0.91.0",
                },
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": client_id,
                    "scope": OPENAI_REFRESH_SCOPE,
                },
                timeout=60,
            )
            if response.status_code != 200:
                last_error = f"refresh_token_http_{response.status_code}: {response.text[:200]}"
            else:
                data = response.json()
                access_token = str(data.get("access_token") or "").strip()
                if not access_token:
                    last_error = "refresh_token_missing_access_token"
                else:
                    id_token = str(data.get("id_token") or oauth.get("id_token") or "").strip()
                    new_refresh_token = str(data.get("refresh_token") or refresh_token).strip()
                    access_payload = self._decode_jwt_payload(access_token)
                    issued_at = int(access_payload.get("iat") or time.time())
                    expires_at = int(access_payload.get("exp") or (time.time() + int(data.get("expires_in") or 0)))
                    refreshed_account = {
                        **account,
                        "access_token": access_token,
                        "email": account.get("email"),
                        "oauth": {
                            **oauth,
                            "_token_version": issued_at * 1000,
                            "access_token": access_token,
                            "refresh_token": new_refresh_token,
                            "id_token": id_token,
                            "client_id": client_id,
                            "expires_at": self._iso_from_unix(expires_at),
                            "expires_in": max(0, expires_at - int(time.time())),
                        },
                    }
                    refreshed_account["oauth"].update(self._local_oauth_metadata(refreshed_account))
                    return refreshed_account
        except Exception as exc:
            last_error = str(exc) or exc.__class__.__name__
        finally:
            session.close()

        if last_error:
            raise RuntimeError(last_error)
        return None

    def _replace_account(self, old_access_token: str, next_item: dict[str, Any]) -> dict | None:
        old_access_token = str(old_access_token or "").strip()
        new_access_token = str(next_item.get("access_token") or "").strip()
        if not old_access_token or not new_access_token:
            return None
        with self._lock:
            current = self._accounts.get(old_access_token, {})
            account = self._normalize_account({**current, **next_item, "access_token": new_access_token})
            if account is None:
                return None
            if old_access_token != new_access_token:
                self._accounts.pop(old_access_token, None)
                inflight = self._image_inflight.pop(old_access_token, None)
                if inflight is not None:
                    self._image_inflight[new_access_token] = inflight
            self._accounts[new_access_token] = account
            self._save_accounts()
            log_service.add(LOG_TYPE_ACCOUNT, "刷新 OAuth token",
                            {"old_token": anonymize_token(old_access_token), "new_token": anonymize_token(new_access_token)})
            return dict(account)

    def ensure_oauth_metadata(
        self,
        access_token: str,
        *,
        allow_remote: bool = True,
        allow_token_refresh: bool = False,
        event: str = "ensure_oauth_metadata",
    ) -> dict[str, Any]:
        """Best-effort fill of OAuth metadata required by sub2api exports.

        Local JWT parsing is cheap and always tried first. If chatgpt_account_id is
        still missing, the ChatGPT backend account check is used as the source of
        truth when allow_remote is enabled.
        """
        access_token = str(access_token or "").strip()
        if not access_token:
            return {"ok": False, "updated": False, "chatgpt_account_id": "", "error": "access_token is required"}

        account = self.get_account(access_token)
        if account is None:
            return {"ok": False, "updated": False, "chatgpt_account_id": "", "error": "account not found"}

        local_metadata = self._local_oauth_metadata(account)
        if local_metadata:
            before = account.get("oauth") if isinstance(account.get("oauth"), dict) else {}
            changed = any(str(before.get(key) or "").strip() != str(value or "").strip() for key, value in local_metadata.items())
            if changed:
                account = self.update_account(access_token, {"oauth": local_metadata}) or self.get_account(access_token) or account

        oauth = account.get("oauth") if isinstance(account.get("oauth"), dict) else {}
        account_id = str(oauth.get("chatgpt_account_id") or "").strip()
        if account_id or not allow_remote:
            return {"ok": bool(account_id), "updated": bool(account_id), "chatgpt_account_id": account_id, "source": "local"}

        token_for_remote = access_token
        try:
            refreshed = self.fetch_remote_info(access_token, event, invalidate_on_401=False)
        except Exception as exc:
            if not allow_token_refresh:
                return {
                    "ok": False,
                    "updated": False,
                    "chatgpt_account_id": "",
                    "source": "remote",
                    "error": str(exc),
                }
            account = self.get_account(access_token) or account
            try:
                refreshed_token_account = self._refresh_oauth_token(account)
            except Exception as refresh_exc:
                return {
                    "ok": False,
                    "updated": False,
                    "chatgpt_account_id": "",
                    "source": "refresh_token",
                    "error": f"{exc}; refresh failed: {refresh_exc}",
                }
            if refreshed_token_account is None:
                return {"ok": False, "updated": False, "chatgpt_account_id": "", "source": "remote", "error": str(exc)}
            refreshed_token_account = self._replace_account(access_token, refreshed_token_account) or refreshed_token_account
            token_for_remote = str(refreshed_token_account.get("access_token") or access_token)
            try:
                refreshed = self.fetch_remote_info(token_for_remote, event, invalidate_on_401=False)
            except Exception as remote_exc:
                refreshed = refreshed_token_account
                remote_error = str(remote_exc)
            else:
                remote_error = ""
        else:
            remote_error = ""

        refreshed = refreshed or self.get_account(token_for_remote) or {}
        oauth = refreshed.get("oauth") if isinstance(refreshed.get("oauth"), dict) else {}
        account_id = str(oauth.get("chatgpt_account_id") or "").strip()
        result = {
            "ok": bool(account_id),
            "updated": bool(account_id),
            "chatgpt_account_id": account_id,
            "source": "remote",
            "access_token": token_for_remote,
        }
        if remote_error and not account_id:
            result["error"] = remote_error
        return result

    def backfill_oauth_metadata(
        self,
        access_tokens: list[str] | None = None,
        *,
        allow_remote: bool = True,
        allow_token_refresh: bool = False,
        event: str = "backfill_oauth_metadata",
    ) -> dict[str, Any]:
        tokens = list(dict.fromkeys(str(token or "").strip() for token in (access_tokens or self.list_tokens()) if str(token or "").strip()))
        if not tokens:
            return {"total": 0, "updated": 0, "missing": 0, "errors": [], "items": self.list_accounts()}

        updated = 0
        missing = 0
        errors = []
        token_map = {}
        max_workers = min(8, len(tokens))

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(
                    self.ensure_oauth_metadata,
                    token,
                    allow_remote=allow_remote,
                    allow_token_refresh=allow_token_refresh,
                    event=event,
                ): token
                for token in tokens
            }
            for future in as_completed(futures):
                token = futures[future]
                try:
                    result = future.result()
                except Exception as exc:
                    missing += 1
                    errors.append({"token": anonymize_token(token), "error": str(exc)})
                    continue
                if result.get("chatgpt_account_id"):
                    updated += 1
                    new_token = str(result.get("access_token") or token).strip()
                    if new_token and new_token != token:
                        token_map[token] = new_token
                else:
                    missing += 1
                    if result.get("error"):
                        errors.append({"token": anonymize_token(token), "error": result.get("error")})

        return {
            "total": len(tokens),
            "updated": updated,
            "missing": missing,
            "errors": errors,
            "token_map": token_map,
            "items": self.list_accounts(),
        }

    def list_tokens(self) -> list[str]:
        with self._lock:
            return list(self._accounts)

    def _list_ready_candidate_tokens(self, excluded_tokens: set[str] | None = None) -> list[str]:
        excluded = set(excluded_tokens or set())
        return [
            token
            for item in self._accounts.values()
            if self._is_image_account_available(item)
               and (token := item.get("access_token") or "")
               and token not in excluded
        ]

    def _list_available_candidate_tokens(self, excluded_tokens: set[str] | None = None) -> list[str]:
        max_concurrency = max(1, int(config.image_account_concurrency or 1))
        return [
            token
            for token in self._list_ready_candidate_tokens(excluded_tokens)
            if int(self._image_inflight.get(token, 0)) < max_concurrency
        ]

    def _acquire_next_candidate_token(self, excluded_tokens: set[str] | None = None) -> str:
        with self._image_slot_condition:
            while True:
                if not self._list_ready_candidate_tokens(excluded_tokens):
                    raise RuntimeError("no available image quota")
                tokens = self._list_available_candidate_tokens(excluded_tokens)
                if tokens:
                    access_token = tokens[self._index % len(tokens)]
                    self._index += 1
                    self._image_inflight[access_token] = int(self._image_inflight.get(access_token, 0)) + 1
                    return access_token
                self._image_slot_condition.wait(timeout=1.0)

    def release_image_slot(self, access_token: str) -> None:
        if not access_token:
            return
        with self._image_slot_condition:
            current_inflight = int(self._image_inflight.get(access_token, 0))
            if current_inflight <= 1:
                self._image_inflight.pop(access_token, None)
            else:
                self._image_inflight[access_token] = current_inflight - 1
            self._image_slot_condition.notify_all()

    def get_available_access_token(self) -> str:
        attempted_tokens: set[str] = set()
        while True:
            access_token = self._acquire_next_candidate_token(excluded_tokens=attempted_tokens)
            attempted_tokens.add(access_token)
            try:
                account = self.fetch_remote_info(access_token, "get_available_access_token", invalidate_on_401=False)
            except Exception as exc:
                last_error = str(exc) or exc.__class__.__name__
                updates = {"last_error": last_error}
                if "401" in last_error or "unauthorized" in last_error.lower():
                    updates.update({"status": "异常", "quota": 0})
                self.update_account(access_token, updates)
                self.release_image_slot(access_token)
                continue
            if self._is_image_account_available(account or {}):
                return access_token
            self.release_image_slot(access_token)

    def get_text_access_token(self, excluded_tokens: set[str] | None = None) -> str:
        excluded = set(excluded_tokens or set())
        with self._lock:
            candidates = [
                token
                for account in self._accounts.values()
                if account.get("status") not in {"禁用", "异常"}
                   and (token := account.get("access_token") or "")
                   and token not in excluded
            ]
            if not candidates:
                return ""
            access_token = candidates[self._index % len(candidates)]
            self._index += 1
            return access_token

    def mark_text_used(self, access_token: str) -> None:
        if not access_token:
            return
        with self._lock:
            current = self._accounts.get(access_token)
            if current is None:
                return
            next_item = dict(current)
            next_item["last_used_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            account = self._normalize_account(next_item)
            if account is None:
                return
            self._accounts[access_token] = account
            self._save_accounts()

    def remove_invalid_token(self, access_token: str, event: str) -> bool:
        account = self.get_account(access_token)
        protected = False
        if account:
            credential_owner = str(account.get("credential_owner") or "").strip()
            protected = bool(
                credential_owner
                or str(account.get("register_job_id") or "").strip()
                or str(account.get("sub2api_account_id") or "").strip()
            )
        if protected or not config.auto_remove_invalid_accounts:
            updates = {"status": "异常", "quota": 0, "last_error": f"invalid token during {event}"}
            self.update_account(access_token, updates)
            if protected:
                log_service.add(
                    LOG_TYPE_ACCOUNT,
                    "账号凭据失效，已标记异常",
                    {"source": event, "token": anonymize_token(access_token)},
                )
            return False
        removed = bool(self.delete_accounts([access_token])["removed"])
        if removed:
            log_service.add(LOG_TYPE_ACCOUNT, "自动移除异常账号",
                            {"source": event, "token": anonymize_token(access_token)})
        elif access_token:
            self.update_account(access_token, {"status": "异常", "quota": 0})
        return removed

    def get_account(self, access_token: str) -> dict | None:
        if not access_token:
            return None
        with self._lock:
            account = self._accounts.get(access_token)
            return dict(account) if account else None

    def list_accounts(self) -> list[dict]:
        with self._lock:
            return [dict(item) for item in self._accounts.values()]

    def list_limited_tokens(self) -> list[str]:
        with self._lock:
            return [
                token
                for item in self._accounts.values()
                if item.get("status") == "限流"
                   and (token := item.get("access_token") or "")
            ]

    def add_account_records(self, records: list[dict[str, Any]]) -> dict:
        records = [record for record in records if isinstance(record, dict) and record.get("access_token")]
        if not records:
            return {"added": 0, "skipped": 0, "items": self.list_accounts()}

        with self._lock:
            added = 0
            skipped = 0
            for record in records:
                access_token = str(record.get("access_token") or "").strip()
                current_key = access_token
                current = self._accounts.get(access_token)
                sub2api_account_id = str(record.get("sub2api_account_id") or "").strip()
                if current is None and sub2api_account_id:
                    for token, account in self._accounts.items():
                        if str(account.get("sub2api_account_id") or "").strip() == sub2api_account_id:
                            current_key = token
                            current = account
                            break
                record_login = record.get("login") if isinstance(record.get("login"), dict) else {}
                record_oauth = record.get("oauth") if isinstance(record.get("oauth"), dict) else {}
                record_email = str(
                    record.get("email") or record_login.get("email") or record_oauth.get("email") or ""
                ).strip().lower()
                record_owner = str(record.get("credential_owner") or "").strip()
                if current is None and record_email and record_owner == "chatgpt2api":
                    for token, account in self._accounts.items():
                        account_owner = str(account.get("credential_owner") or "").strip()
                        if account_owner != "chatgpt2api":
                            continue
                        account_login = account.get("login") if isinstance(account.get("login"), dict) else {}
                        account_oauth = account.get("oauth") if isinstance(account.get("oauth"), dict) else {}
                        account_email = str(
                            account.get("email") or account_login.get("email") or account_oauth.get("email") or ""
                        ).strip().lower()
                        if account_email == record_email:
                            current_key = token
                            current = account
                            break
                if current is None:
                    added += 1
                    current = {}
                else:
                    skipped += 1
                account = self._normalize_account(
                    {
                        **current,
                        **record,
                        "access_token": access_token,
                        "type": str(record.get("type") or current.get("type") or "free"),
                    }
                )
                if account is not None:
                    if current_key != access_token:
                        self._accounts.pop(current_key, None)
                        inflight = self._image_inflight.pop(current_key, None)
                        if inflight is not None:
                            self._image_inflight[access_token] = inflight
                    self._accounts[access_token] = account
            self._save_accounts()
            items = [dict(item) for item in self._accounts.values()]
            log_service.add(LOG_TYPE_ACCOUNT, f"新增 {added} 个账号，跳过 {skipped} 个",
                            {"added": added, "skipped": skipped})
        return {"added": added, "skipped": skipped, "items": items}

    def add_accounts(self, tokens: list[str]) -> dict:
        return self.add_account_records([{"access_token": token} for token in list(dict.fromkeys(token for token in tokens if token))])

    def delete_accounts(self, tokens: list[str]) -> dict:
        target_set = set(token for token in tokens if token)
        if not target_set:
            return {"removed": 0, "items": self.list_accounts()}
        with self._lock:
            removed = sum(self._accounts.pop(token, None) is not None for token in target_set)
            for token in target_set:
                self._image_inflight.pop(token, None)
            if removed:
                if self._accounts:
                    self._index %= len(self._accounts)
                else:
                    self._index = 0
                self._save_accounts()
                log_service.add(LOG_TYPE_ACCOUNT, f"删除 {removed} 个账号", {"removed": removed})
            items = [dict(item) for item in self._accounts.values()]
        return {"removed": removed, "items": items}

    def update_account(self, access_token: str, updates: dict) -> dict | None:
        if not access_token:
            return None
        with self._lock:
            current = self._accounts.get(access_token)
            if current is None:
                return None
            next_item = {**current, **updates, "access_token": access_token}
            if isinstance(current.get("oauth"), dict) or isinstance(updates.get("oauth"), dict):
                next_item["oauth"] = {
                    **(current.get("oauth") if isinstance(current.get("oauth"), dict) else {}),
                    **(updates.get("oauth") if isinstance(updates.get("oauth"), dict) else {}),
                }
            account = self._normalize_account(next_item)
            if account is None:
                return None
            if account.get("status") == "限流" and config.auto_remove_rate_limited_accounts:
                self._accounts.pop(access_token, None)
                self._save_accounts()
                log_service.add(LOG_TYPE_ACCOUNT, "自动移除限流账号", {"token": anonymize_token(access_token)})
                return None
            self._accounts[access_token] = account
            self._save_accounts()
            log_service.add(LOG_TYPE_ACCOUNT, "更新账号",
                            {"token": anonymize_token(access_token), "status": account.get("status")})
            return dict(account)
        return None

    def mark_image_result(self, access_token: str, success: bool) -> dict | None:
        if not access_token:
            return None
        self.release_image_slot(access_token)
        with self._lock:
            current = self._accounts.get(access_token)
            if current is None:
                return None
            next_item = dict(current)
            next_item["last_used_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            image_quota_unknown = bool(next_item.get("image_quota_unknown"))
            if success:
                next_item["success"] = int(next_item.get("success") or 0) + 1
                if not image_quota_unknown:
                    next_item["quota"] = max(0, int(next_item.get("quota") or 0) - 1)
                if not image_quota_unknown and next_item["quota"] == 0:
                    next_item["status"] = "限流"
                    next_item["restore_at"] = next_item.get("restore_at") or None
                elif next_item.get("status") == "限流":
                    next_item["status"] = "正常"
            else:
                next_item["fail"] = int(next_item.get("fail") or 0) + 1
            account = self._normalize_account(next_item)
            if account is None:
                return None
            if account.get("status") == "限流" and config.auto_remove_rate_limited_accounts:
                self._accounts.pop(access_token, None)
                self._save_accounts()
                log_service.add(LOG_TYPE_ACCOUNT, "自动移除限流账号", {"token": anonymize_token(access_token)})
                return None
            self._accounts[access_token] = account
            self._save_accounts()
            return dict(account)
        return None

    def fetch_remote_info(self, access_token: str, event: str = "fetch_remote_info", *, invalidate_on_401: bool = True) -> dict[str, Any] | None:
        if not access_token:
            raise ValueError("access_token is required")

        try:
            from services.openai_backend_api import InvalidAccessTokenError, OpenAIBackendAPI
            result = OpenAIBackendAPI(access_token).get_user_info()
        except InvalidAccessTokenError:
            if invalidate_on_401:
                self.remove_invalid_token(access_token, event)
            raise
        return self.update_account(access_token, result)

    @staticmethod
    def _is_retryable_remote_error(error: Exception) -> bool:
        text = str(error or "").lower()
        markers = (
            "http 408",
            "http 409",
            "http 425",
            "http 429",
            "http 500",
            "http 502",
            "http 503",
            "http 504",
            "status=408",
            "status=409",
            "status=425",
            "status=429",
            "status=500",
            "status=502",
            "status=503",
            "status=504",
            "timed out",
            "timeout",
            "connection",
            "temporarily unavailable",
            "remote end closed",
            "proxy",
            "tls",
            "ssl",
        )
        return any(marker in text for marker in markers)

    @staticmethod
    def _is_auth_failure_error(message: str) -> bool:
        text = str(message or "").lower()
        markers = (
            "401",
            "unauthorized",
            "token_invalidated",
            "authentication token has been invalidated",
            "invalid access token",
            "invalid_access_token",
        )
        return any(marker in text for marker in markers)

    def refresh_account_safely(
        self,
        access_token: str,
        event: str = "refresh_accounts",
        *,
        allow_token_refresh: bool = False,
    ) -> tuple[dict[str, Any] | None, str]:
        access_token = str(access_token or "").strip()
        if not access_token:
            return None, "access_token is required"

        last_error = ""
        for attempt in range(1, 4):
            try:
                return self.fetch_remote_info(access_token, event, invalidate_on_401=False), ""
            except Exception as exc:
                last_error = str(exc) or exc.__class__.__name__
                if not self._is_retryable_remote_error(exc) or attempt >= 3:
                    break
                time.sleep(1.5 * attempt)

        account = self.get_account(access_token)
        if account is None:
            return None, last_error or "account not found"

        auth_failed = self._is_auth_failure_error(last_error)
        if not allow_token_refresh or not auth_failed:
            status_updates = {"last_error": last_error}
            if auth_failed:
                status_updates.update({"status": "异常", "quota": 0})
            self.update_account(access_token, status_updates)
            return None, last_error

        try:
            refreshed = self._refresh_oauth_token(account)
        except Exception as exc:
            self.update_account(access_token, {"status": "异常", "quota": 0, "last_error": f"{last_error}; refresh failed: {exc}"})
            return None, f"{last_error}; refresh failed: {exc}" if last_error else f"refresh failed: {exc}"

        if refreshed is None:
            self.update_account(access_token, {"status": "异常", "quota": 0, "last_error": last_error or "refresh token unavailable"})
            return None, last_error or "refresh token unavailable"

        refreshed = self._replace_account(access_token, refreshed) or refreshed
        new_token = str(refreshed.get("access_token") or access_token).strip()
        for attempt in range(1, 3):
            try:
                return self.fetch_remote_info(new_token, event, invalidate_on_401=False), ""
            except Exception as exc:
                last_error = str(exc) or exc.__class__.__name__
                if not self._is_retryable_remote_error(exc) or attempt >= 2:
                    break
                time.sleep(1.5 * attempt)

        self.update_account(new_token, {"status": "异常", "quota": 0, "last_error": last_error})
        return None, last_error

    def refresh_accounts(self, access_tokens: list[str], *, allow_token_refresh: bool = False) -> dict[str, Any]:
        access_tokens = list(dict.fromkeys(token for token in access_tokens if token))
        if not access_tokens:
            return {"refreshed": 0, "errors": [], "items": self.list_accounts()}

        refreshed = 0
        errors = []
        max_workers = min(10, len(access_tokens))

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(
                    self.refresh_account_safely,
                    token,
                    "refresh_accounts",
                    allow_token_refresh=allow_token_refresh,
                ): token
                for token in access_tokens
            }
            for future in as_completed(futures):
                try:
                    account, error = future.result()
                except Exception as exc:
                    errors.append({
                        "token": anonymize_token(futures[future]),
                        "access_token": futures[future],
                        "error": str(exc),
                    })
                    continue
                if error:
                    errors.append({
                        "token": anonymize_token(futures[future]),
                        "access_token": futures[future],
                        "error": error,
                    })
                    continue
                if account is not None:
                    refreshed += 1

        return {
            "refreshed": refreshed,
            "errors": errors,
            "items": self.list_accounts(),
        }


account_service = AccountService(config.get_storage_backend())
