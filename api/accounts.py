from __future__ import annotations

import io
import json
import re
import zipfile
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Header, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response
from pydantic import BaseModel, Field

from services.auth_service import auth_service

from api.support import (
    require_admin,
    sanitize_cpa_pool,
    sanitize_cpa_pools,
    sanitize_sub2api_server,
    sanitize_sub2api_servers,
)
from services.account_service import account_service
from services.cpa_service import cpa_config, cpa_import_service, list_remote_files
from services.register_service import register_service
from services.sub2api_service import (
    list_remote_accounts as sub2api_list_remote_accounts,
    list_remote_groups as sub2api_list_remote_groups,
    sub2api_config,
    sub2api_import_service,
)



def _sanitize_account(item: dict) -> dict:
    sanitized = {key: value for key, value in item.items() if key != "oauth"}
    login = sanitized.get("login")
    if isinstance(login, dict):
        sanitized["login"] = {key: value for key, value in login.items() if key != "password"}
    return sanitized


def _sanitize_accounts(items: list[dict]) -> list[dict]:
    return [_sanitize_account(item) for item in items]


def _sanitize_account_errors(items: list[dict]) -> list[dict]:
    sanitized: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        next_item = dict(item)
        delete_token = str(next_item.get("delete_token") or "").strip()
        token = str(next_item.pop("access_token", "") or next_item.get("token") or "").strip()
        if token:
            from utils.helper import anonymize_token

            next_item["token"] = anonymize_token(token)
        if delete_token:
            next_item["delete_token"] = delete_token
        sanitized.append(next_item)
    return sanitized


def _account_email(item: dict) -> str:
    login = item.get("login") if isinstance(item.get("login"), dict) else {}
    oauth = item.get("oauth") if isinstance(item.get("oauth"), dict) else {}
    return str(item.get("email") or login.get("email") or oauth.get("email") or "").strip().lower()


def _registered_records_by_email_and_token() -> tuple[dict[str, dict], dict[str, dict]]:
    registered_records = register_service.registered_accounts()
    records_by_email = {
        str(item.get("email") or "").strip().lower(): item
        for item in registered_records
        if str(item.get("email") or "").strip()
    }
    records_by_token = {
        str(item.get("access_token") or "").strip(): item
        for item in registered_records
        if str(item.get("access_token") or "").strip()
    }
    return records_by_email, records_by_token


def _is_sub2api_owned_account(account: dict) -> bool:
    return (
        str(account.get("credential_owner") or "").strip() == "sub2api"
        or bool(str(account.get("sub2api_account_id") or "").strip())
    )


def _terminal_recovery_error(token: str, email: str, error: str) -> dict:
    return {
        "token": token,
        "access_token": token,
        "delete_token": token,
        "email": email,
        "error": error,
        "terminal": True,
        "terminal_action": "delete_local_pool",
    }


def _is_recoverable_auth_error(message: str) -> bool:
    text = str(message or "").lower()
    if register_service.is_terminal_account_error(text):
        return False
    network_markers = (
        "tls",
        "ssl",
        "timed out",
        "timeout",
        "connection",
        "proxy",
        "temporarily unavailable",
        "remote end closed",
        "curl",
    )
    if any(marker in text for marker in network_markers):
        return False
    auth_markers = (
        "401",
        "unauthorized",
        "token_invalidated",
        "authentication token has been invalidated",
        "invalid access token",
        "invalid_access_token",
        "refresh failed",
        "refresh token unavailable",
        "refresh_token_http_400",
        "refresh_token_http_401",
        "refresh_token_http_403",
        "refresh_token_missing_access_token",
        "refresh_token_missing_client_id",
    )
    return any(marker in text for marker in auth_markers)


class UserKeyCreateRequest(BaseModel):
    name: str = ""


class UserKeyUpdateRequest(BaseModel):
    name: str | None = None
    enabled: bool | None = None
    key: str | None = None


class AccountCreateRequest(BaseModel):
    tokens: list[str] = Field(default_factory=list)
    accounts: list[dict[str, Any]] = Field(default_factory=list)


class AccountDeleteRequest(BaseModel):
    tokens: list[str] = Field(default_factory=list)


class AccountRefreshRequest(BaseModel):
    access_tokens: list[str] = Field(default_factory=list)


class AccountRecoverRequest(BaseModel):
    access_tokens: list[str] = Field(default_factory=list)


class AccountExportRequest(BaseModel):
    access_tokens: list[str] = Field(default_factory=list)
    format: Literal["json", "zip"] = "json"


class AccountUpdateRequest(BaseModel):
    access_token: str = ""
    type: str | None = None
    status: str | None = None
    quota: int | None = None


class CPAPoolCreateRequest(BaseModel):
    name: str = ""
    base_url: str = ""
    secret_key: str = ""


class CPAPoolUpdateRequest(BaseModel):
    name: str | None = None
    base_url: str | None = None
    secret_key: str | None = None


class CPAImportRequest(BaseModel):
    names: list[str] = Field(default_factory=list)


class Sub2APIServerCreateRequest(BaseModel):
    name: str = ""
    base_url: str = ""
    email: str = ""
    password: str = ""
    api_key: str = ""
    group_id: str = ""


class Sub2APIServerUpdateRequest(BaseModel):
    name: str | None = None
    base_url: str | None = None
    email: str | None = None
    password: str | None = None
    api_key: str | None = None
    group_id: str | None = None


class Sub2APIImportRequest(BaseModel):
    account_ids: list[str] = Field(default_factory=list)


def _account_payload_token(item: dict[str, Any]) -> str:
    return str(item.get("access_token") or item.get("accessToken") or "").strip()


def _unique_tokens(tokens: list[str]) -> list[str]:
    return list(dict.fromkeys(str(token or "").strip() for token in tokens if str(token or "").strip()))


def _download_timestamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _safe_export_name(value: str, fallback: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-._")
    return (clean or fallback)[:80]


def _account_zip_bytes(items: list[dict[str, str]]) -> bytes:
    buf = io.BytesIO()
    used_names: set[str] = set()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        for index, item in enumerate(items, start=1):
            raw_name = item.get("email") or item.get("account_id") or f"account-{index:03d}"
            base_name = _safe_export_name(raw_name, f"account-{index:03d}")
            name = base_name
            suffix = 2
            while name in used_names:
                name = f"{base_name}-{suffix}"
                suffix += 1
            used_names.add(name)
            archive.writestr(
                f"{name}.json",
                json.dumps(item, ensure_ascii=False, indent=2) + "\n",
            )
    return buf.getvalue()


def create_router() -> APIRouter:
    router = APIRouter()

    @router.get("/api/auth/users")
    async def list_user_keys(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"items": auth_service.list_keys(role="user")}

    @router.post("/api/auth/users")
    async def create_user_key(body: UserKeyCreateRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        try:
            item, raw_key = auth_service.create_key(role="user", name=body.name)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc
        return {"item": item, "key": raw_key, "items": auth_service.list_keys(role="user")}

    @router.post("/api/auth/users/{key_id}")
    async def update_user_key(
            key_id: str,
            body: UserKeyUpdateRequest,
            authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        updates = {
            key: value
            for key, value in {
                "name": body.name,
                "enabled": body.enabled,
                "key": body.key,
            }.items()
            if value is not None
        }
        if not updates:
            raise HTTPException(status_code=400, detail={"error": "还没有检测到改动，请修改后再保存"})
        try:
            item = auth_service.update_key(key_id, updates, role="user")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc
        if item is None:
            raise HTTPException(status_code=404, detail={"error": "这条用户密钥不存在，可能已经被删除"})
        return {"item": item, "items": auth_service.list_keys(role="user")}

    @router.delete("/api/auth/users/{key_id}")
    async def delete_user_key(key_id: str, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        if not auth_service.delete_key(key_id, role="user"):
            raise HTTPException(status_code=404, detail={"error": "这条用户密钥不存在，可能已经被删除"})
        return {"items": auth_service.list_keys(role="user")}

    @router.get("/api/accounts")
    async def get_accounts(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"items": _sanitize_accounts(account_service.list_accounts())}

    @router.post("/api/accounts")
    async def create_accounts(body: AccountCreateRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        account_payloads = [item for item in body.accounts if isinstance(item, dict)]
        payload_tokens = [_account_payload_token(item) for item in account_payloads]
        tokens = _unique_tokens([*body.tokens, *payload_tokens])
        if not tokens:
            raise HTTPException(status_code=400, detail={"error": "tokens is required"})
        if account_payloads:
            result = account_service.add_account_items(account_payloads)
            payload_token_set = set(_unique_tokens(payload_tokens))
            extra_tokens = [token for token in tokens if token not in payload_token_set]
            if extra_tokens:
                extra_result = account_service.add_accounts(extra_tokens)
                result["added"] = int(result.get("added") or 0) + int(extra_result.get("added") or 0)
                result["skipped"] = int(result.get("skipped") or 0) + int(extra_result.get("skipped") or 0)
        else:
            result = account_service.add_accounts(tokens)
        refresh_result = account_service.refresh_accounts(tokens)
        return {
            **result,
            "refreshed": refresh_result.get("refreshed", 0),
            "errors": _sanitize_account_errors(refresh_result.get("errors", [])),
            "items": _sanitize_accounts(refresh_result.get("items", result.get("items", []))),
        }

    @router.delete("/api/accounts")
    async def delete_accounts(body: AccountDeleteRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        tokens = [str(token or "").strip() for token in body.tokens if str(token or "").strip()]
        if not tokens:
            raise HTTPException(status_code=400, detail={"error": "tokens is required"})
        result = account_service.delete_accounts(tokens)
        return {**result, "items": _sanitize_accounts(result.get("items", []))}

    @router.post("/api/accounts/refresh")
    async def refresh_accounts(body: AccountRefreshRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        access_tokens = [str(token or "").strip() for token in body.access_tokens if str(token or "").strip()]
        if not access_tokens:
            access_tokens = account_service.list_tokens()
        if not access_tokens:
            raise HTTPException(status_code=400, detail={"error": "access_tokens is required"})
        result = await run_in_threadpool(lambda: account_service.refresh_accounts(access_tokens, allow_token_refresh=True))
        errors = list(result.get("errors") or [])

        removed = 0
        failed_tokens = [
            str(item.get("access_token") or item.get("token") or "").strip()
            for item in errors
            if isinstance(item, dict)
        ]
        failed_tokens = [token for token in failed_tokens if token]

        if failed_tokens:
            current_accounts = {
                str(item.get("access_token") or "").strip(): item
                for item in account_service.list_accounts()
                if str(item.get("access_token") or "").strip()
            }
            records_by_email, records_by_token = _registered_records_by_email_and_token()
            terminal_tokens: list[str] = []

            for item in errors:
                if not isinstance(item, dict):
                    continue
                token = str(item.get("access_token") or item.get("token") or "").strip()
                error = str(item.get("error") or "").strip()
                if not token:
                    continue
                account = current_accounts.get(token) or {}
                email = _account_email(account)
                record = records_by_token.get(token) or records_by_email.get(email)
                if register_service.is_terminal_account_error(error):
                    terminal_tokens.append(token)
                    continue
                if record and str(record.get("password") or "").strip():
                    item["email"] = email or str(record.get("email") or "").strip().lower()
                    item["recovery_skipped_reason"] = "一键刷新不会自动重新登录，请在确认后手动点击恢复凭据"

            if terminal_tokens:
                delete_result = await run_in_threadpool(lambda: account_service.delete_accounts(terminal_tokens))
                removed = int(delete_result.get("removed") or 0)
                terminal_set = set(terminal_tokens)
                errors = [
                    item
                    for item in errors
                    if str(item.get("access_token") or item.get("token") or "").strip() not in terminal_set
                ]

        return {
            **result,
            "removed": removed,
            "errors": _sanitize_account_errors(errors),
            "items": _sanitize_accounts(account_service.list_accounts()),
        }

    @router.post("/api/accounts/recover")
    async def recover_accounts(body: AccountRecoverRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        access_tokens = [str(token or "").strip() for token in body.access_tokens if str(token or "").strip()]
        if not access_tokens:
            raise HTTPException(status_code=400, detail={"error": "access_tokens is required"})

        accounts_by_token = {
            str(item.get("access_token") or "").strip(): item
            for item in account_service.list_accounts()
            if str(item.get("access_token") or "").strip()
        }
        registered_records = register_service.registered_accounts()
        records_by_email = {
            str(item.get("email") or "").strip().lower(): item
            for item in registered_records
            if str(item.get("email") or "").strip()
        }
        records_by_token = {
            str(item.get("access_token") or "").strip(): item
            for item in registered_records
            if str(item.get("access_token") or "").strip()
        }

        target_emails: list[str] = []
        errors: list[dict] = []
        seen: set[str] = set()
        for access_token in access_tokens:
            account = accounts_by_token.get(access_token)
            if account is None:
                errors.append({"token": access_token, "error": "账号不存在"})
                continue
            login = account.get("login") if isinstance(account.get("login"), dict) else {}
            email = str(account.get("email") or login.get("email") or "").strip().lower()
            last_error = str(account.get("last_error") or "").strip()
            if register_service.is_terminal_account_error(last_error):
                errors.append(_terminal_recovery_error(
                    access_token,
                    email,
                    "账号已被远端删除或停用，无法通过验证码或重新登录恢复",
                ))
                continue
            if _is_sub2api_owned_account(account):
                errors.append({"token": access_token, "email": email, "error": "sub2api 来源账号不由 chatgpt2api 重新登录恢复"})
                continue
            if str(account.get("status") or "").strip() != "异常":
                errors.append({"token": access_token, "email": email, "error": "账号未标记异常，先刷新确认失败后再恢复"})
                continue
            if not _is_recoverable_auth_error(last_error):
                errors.append({"token": access_token, "email": email, "error": "当前异常不是认证失败或 refresh 失败，不触发重新登录恢复"})
                continue
            record = records_by_token.get(access_token) or records_by_email.get(email)
            if record is None:
                errors.append({"token": access_token, "email": email, "error": "没有匹配的注册记录，无法自动重新登录"})
                continue
            record_email = str(record.get("email") or "").strip().lower()
            if not record_email:
                errors.append({"token": access_token, "email": email, "error": "注册记录缺少邮箱"})
                continue
            if not str(record.get("password") or "").strip():
                errors.append({"token": access_token, "email": record_email, "error": "注册记录缺少密码，无法自动重新登录"})
                continue
            if record_email not in seen:
                seen.add(record_email)
                target_emails.append(record_email)

        recovered = 0
        if target_emails:
            result = await run_in_threadpool(
                lambda: register_service.recover_registered_accounts(target_emails, force=True)
            )
            recovered = int(result.get("recovered") or 0)
            errors.extend(result.get("errors") or [])
            for email in target_emails:
                await run_in_threadpool(lambda value=email: register_service.sync_registered_account_to_local_pool(value))
            terminal_emails = {
                str(item.get("email") or "").strip().lower()
                for item in errors
                if isinstance(item, dict) and register_service.is_terminal_account_error(str(item.get("error") or ""))
            }
            token_by_email = {
                _account_email(account): token
                for token, account in accounts_by_token.items()
                if _account_email(account)
            }
            for item in errors:
                if not isinstance(item, dict):
                    continue
                if not item.get("manual_code_required"):
                    continue
                email = str(item.get("email") or "").strip().lower()
                token = token_by_email.get(email, "")
                if token:
                    item["delete_token"] = token
            if terminal_emails:
                for token, account in accounts_by_token.items():
                    if _account_email(account) in terminal_emails:
                        account_service.update_account(
                            token,
                            {
                                "status": "异常",
                                "quota": 0,
                                "last_error": "账号已被远端删除或停用，无法通过验证码或重新登录恢复",
                            },
                        )
                for item in errors:
                    if not isinstance(item, dict):
                        continue
                    email = str(item.get("email") or "").strip().lower()
                    if email not in terminal_emails:
                        continue
                    for token, account in accounts_by_token.items():
                        if _account_email(account) == email:
                            item["access_token"] = token
                            item["delete_token"] = token
                            item["terminal"] = True
                            item["terminal_action"] = "delete_local_pool"
                            break
        return {
            "recovered": recovered,
            "errors": _sanitize_account_errors(errors),
            "items": _sanitize_accounts(account_service.list_accounts()),
        }

    @router.post("/api/accounts/backfill-oauth")
    async def backfill_oauth_metadata(body: AccountRefreshRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        access_tokens = [str(token or "").strip() for token in body.access_tokens if str(token or "").strip()]
        result = account_service.backfill_oauth_metadata(access_tokens or None, allow_remote=True)
        return {**result, "items": _sanitize_accounts(result.get("items", []))}

    @router.post("/api/accounts/export")
    async def export_accounts(body: AccountExportRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        access_tokens = _unique_tokens(body.access_tokens)
        items = account_service.build_export_items(access_tokens)
        if not items:
            raise HTTPException(
                status_code=400,
                detail={"error": "没有可导出的完整账号，需要同时有 access_token、refresh_token 和 id_token"},
            )

        timestamp = _download_timestamp()
        if body.format == "zip":
            content = _account_zip_bytes(items)
            return Response(
                content,
                media_type="application/zip",
                headers={"Content-Disposition": f'attachment; filename="codex-accounts-{timestamp}.zip"'},
            )

        payload: dict[str, str] | list[dict[str, str]] = items[0] if len(items) == 1 else items
        return Response(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="codex-accounts-{timestamp}.json"'},
        )

    @router.post("/api/accounts/update")
    async def update_account(body: AccountUpdateRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        access_token = str(body.access_token or "").strip()
        if not access_token:
            raise HTTPException(status_code=400, detail={"error": "access_token is required"})
        updates = {key: value for key, value in {"type": body.type, "status": body.status, "quota": body.quota}.items() if value is not None}
        if not updates:
            raise HTTPException(status_code=400, detail={"error": "还没有检测到改动，请修改后再保存"})
        account = account_service.update_account(access_token, updates)
        if account is None:
            raise HTTPException(status_code=404, detail={"error": "account not found"})
        return {"item": _sanitize_account(account), "items": _sanitize_accounts(account_service.list_accounts())}

    @router.get("/api/cpa/pools")
    async def list_cpa_pools(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"pools": sanitize_cpa_pools(cpa_config.list_pools())}

    @router.post("/api/cpa/pools")
    async def create_cpa_pool(body: CPAPoolCreateRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        if not body.base_url.strip():
            raise HTTPException(status_code=400, detail={"error": "base_url is required"})
        if not body.secret_key.strip():
            raise HTTPException(status_code=400, detail={"error": "secret_key is required"})
        pool = cpa_config.add_pool(name=body.name, base_url=body.base_url, secret_key=body.secret_key)
        return {"pool": sanitize_cpa_pool(pool), "pools": sanitize_cpa_pools(cpa_config.list_pools())}

    @router.post("/api/cpa/pools/{pool_id}")
    async def update_cpa_pool(pool_id: str, body: CPAPoolUpdateRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        pool = cpa_config.update_pool(pool_id, body.model_dump(exclude_none=True))
        if pool is None:
            raise HTTPException(status_code=404, detail={"error": "pool not found"})
        return {"pool": sanitize_cpa_pool(pool), "pools": sanitize_cpa_pools(cpa_config.list_pools())}

    @router.delete("/api/cpa/pools/{pool_id}")
    async def delete_cpa_pool(pool_id: str, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        if not cpa_config.delete_pool(pool_id):
            raise HTTPException(status_code=404, detail={"error": "pool not found"})
        return {"pools": sanitize_cpa_pools(cpa_config.list_pools())}

    @router.get("/api/cpa/pools/{pool_id}/files")
    async def cpa_pool_files(pool_id: str, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        pool = cpa_config.get_pool(pool_id)
        if pool is None:
            raise HTTPException(status_code=404, detail={"error": "pool not found"})
        return {"pool_id": pool_id, "files": await run_in_threadpool(list_remote_files, pool)}

    @router.post("/api/cpa/pools/{pool_id}/import")
    async def cpa_pool_import(pool_id: str, body: CPAImportRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        pool = cpa_config.get_pool(pool_id)
        if pool is None:
            raise HTTPException(status_code=404, detail={"error": "pool not found"})
        try:
            job = cpa_import_service.start_import(pool, body.names)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc
        return {"import_job": job}

    @router.get("/api/cpa/pools/{pool_id}/import")
    async def cpa_pool_import_progress(pool_id: str, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        pool = cpa_config.get_pool(pool_id)
        if pool is None:
            raise HTTPException(status_code=404, detail={"error": "pool not found"})
        return {"import_job": pool.get("import_job")}

    @router.get("/api/sub2api/servers")
    async def list_sub2api_servers(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"servers": sanitize_sub2api_servers(sub2api_config.list_servers())}

    @router.post("/api/sub2api/servers")
    async def create_sub2api_server(body: Sub2APIServerCreateRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        if not body.base_url.strip():
            raise HTTPException(status_code=400, detail={"error": "base_url is required"})
        has_login = body.email.strip() and body.password.strip()
        has_api_key = bool(body.api_key.strip())
        if not has_login and not has_api_key:
            raise HTTPException(status_code=400, detail={"error": "email+password or api_key is required"})
        server = sub2api_config.add_server(
            name=body.name,
            base_url=body.base_url,
            email=body.email,
            password=body.password,
            api_key=body.api_key,
            group_id=body.group_id,
        )
        return {"server": sanitize_sub2api_server(server), "servers": sanitize_sub2api_servers(sub2api_config.list_servers())}

    @router.post("/api/sub2api/servers/{server_id}")
    async def update_sub2api_server(server_id: str, body: Sub2APIServerUpdateRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        server = sub2api_config.update_server(server_id, body.model_dump(exclude_none=True))
        if server is None:
            raise HTTPException(status_code=404, detail={"error": "server not found"})
        return {"server": sanitize_sub2api_server(server), "servers": sanitize_sub2api_servers(sub2api_config.list_servers())}

    @router.delete("/api/sub2api/servers/{server_id}")
    async def delete_sub2api_server(server_id: str, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        if not sub2api_config.delete_server(server_id):
            raise HTTPException(status_code=404, detail={"error": "server not found"})
        return {"servers": sanitize_sub2api_servers(sub2api_config.list_servers())}

    @router.get("/api/sub2api/servers/{server_id}/groups")
    async def sub2api_server_groups(server_id: str, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        server = sub2api_config.get_server(server_id)
        if server is None:
            raise HTTPException(status_code=404, detail={"error": "server not found"})
        try:
            groups = await run_in_threadpool(sub2api_list_remote_groups, server)
        except Exception as exc:
            raise HTTPException(status_code=502, detail={"error": str(exc)}) from exc
        return {"server_id": server_id, "groups": groups}

    @router.get("/api/sub2api/servers/{server_id}/accounts")
    async def sub2api_server_accounts(server_id: str, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        server = sub2api_config.get_server(server_id)
        if server is None:
            raise HTTPException(status_code=404, detail={"error": "server not found"})
        try:
            accounts = await run_in_threadpool(sub2api_list_remote_accounts, server)
        except Exception as exc:
            raise HTTPException(status_code=502, detail={"error": str(exc)}) from exc
        return {"server_id": server_id, "accounts": accounts}

    @router.post("/api/sub2api/servers/{server_id}/import")
    async def sub2api_server_import(server_id: str, body: Sub2APIImportRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        server = sub2api_config.get_server(server_id)
        if server is None:
            raise HTTPException(status_code=404, detail={"error": "server not found"})
        try:
            job = sub2api_import_service.start_import(server, body.account_ids)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc
        return {"import_job": job}

    @router.get("/api/sub2api/servers/{server_id}/import")
    async def sub2api_server_import_progress(server_id: str, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        server = sub2api_config.get_server(server_id)
        if server is None:
            raise HTTPException(status_code=404, detail={"error": "server not found"})
        return {"import_job": server.get("import_job")}

    return router
