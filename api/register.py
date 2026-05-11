from __future__ import annotations

import asyncio
import base64
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Header
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel

from api.support import require_admin
from services.register import openai_register
from services.register_service import register_service
from services.sub2api_export_service import OPENAI_PLATFORM_CLIENT_ID, build_sub2api_export, json_bytes




def _json_download(payload: dict, filename: str) -> Response:
    return Response(
        content=json_bytes(payload),
        media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _registered_account_to_pool_record(item: dict, job_id: str) -> dict:
    record = openai_register.build_account_pool_record(item, job_id)
    oauth = item.get("oauth") if isinstance(item.get("oauth"), dict) else {}
    if oauth:
        record["oauth"] = {**(record.get("oauth") if isinstance(record.get("oauth"), dict) else {}), **oauth}
    return record


def _registered_account_exportable(item: dict) -> bool:
    if not isinstance(item, dict) or item.get("auth_failed"):
        return False
    oauth = item.get("oauth") if isinstance(item.get("oauth"), dict) else {}
    return bool(
        str(item.get("access_token") or "").strip()
        and str(item.get("refresh_token") or oauth.get("refresh_token") or "").strip()
        and str(item.get("id_token") or oauth.get("id_token") or "").strip()
        and str(oauth.get("chatgpt_account_id") or "").strip()
    )


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
    if "__" in account_user_id:
        return account_user_id.rsplit("__", 1)[1].strip()
    return ""


def _local_oauth_metadata(item: dict, access_payload: dict[str, Any] | None = None) -> dict[str, str]:
    oauth = item.get("oauth") if isinstance(item.get("oauth"), dict) else {}
    access_payload = access_payload if isinstance(access_payload, dict) else _decode_jwt_payload(str(item.get("access_token") or ""))
    id_payload = _decode_jwt_payload(str(item.get("id_token") or oauth.get("id_token") or ""))
    access_auth = _auth_from_payload(access_payload)
    id_auth = _auth_from_payload(id_payload)
    profile = _profile_from_payload(access_payload)
    metadata = {
        "chatgpt_account_id": (
            str(oauth.get("chatgpt_account_id") or "").strip()
            or _chatgpt_account_id_from_auth(access_auth)
            or _chatgpt_account_id_from_auth(id_auth)
        ),
        "chatgpt_user_id": (
            str(oauth.get("chatgpt_user_id") or "").strip()
            or str(access_auth.get("chatgpt_user_id") or access_auth.get("user_id") or "").strip()
            or str(id_auth.get("chatgpt_user_id") or id_auth.get("user_id") or "").strip()
        ),
        "organization_id": (
            str(oauth.get("organization_id") or "").strip()
            or _organization_id_from_auth(id_auth)
            or _organization_id_from_auth(access_auth)
        ),
        "email": (
            str(oauth.get("email") or "").strip()
            or str(item.get("email") or "").strip()
            or str(profile.get("email") or id_payload.get("email") or access_payload.get("email") or "").strip()
        ),
        "client_id": str(oauth.get("client_id") or access_payload.get("client_id") or OPENAI_PLATFORM_CLIENT_ID).strip(),
    }
    return {key: value for key, value in metadata.items() if value}


def _selected_registered_accounts(emails: str = "") -> list[dict]:
    selected_emails = {item.strip().lower() for item in emails.split(",") if item.strip()}
    if selected_emails:
        return [
            item
            for item in register_service.registered_accounts()
            if str(item.get("email") or "").strip().lower() in selected_emails
        ]
    current = register_service.get()
    job_id = str(((current.get("stats") or {}).get("job_id")) or "").strip()
    registered = register_service.registered_accounts(job_id) if job_id else []
    return registered or register_service.registered_accounts()


def _check_registered_account(item: dict, *, remote: bool) -> dict:
    email = str(item.get("email") or "").strip()
    oauth = item.get("oauth") if isinstance(item.get("oauth"), dict) else {}
    access_token = str(item.get("access_token") or "").strip()
    access_payload = _decode_jwt_payload(access_token)
    local_oauth = _local_oauth_metadata(item, access_payload)
    if local_oauth:
        oauth = {**oauth, **local_oauth}
    exp = int(access_payload.get("exp") or 0)
    expires_at = datetime.fromtimestamp(exp, tz=timezone.utc).isoformat() if exp else None
    now_ts = int(datetime.now(timezone.utc).timestamp())
    missing_fields = []
    if not access_token:
        missing_fields.append("access_token")
    if not str(item.get("refresh_token") or oauth.get("refresh_token") or "").strip():
        missing_fields.append("refresh_token")
    if not str(item.get("id_token") or oauth.get("id_token") or "").strip():
        missing_fields.append("id_token")
    if not str(oauth.get("chatgpt_account_id") or "").strip():
        missing_fields.append("chatgpt_account_id")
    if not str(oauth.get("client_id") or OPENAI_PLATFORM_CLIENT_ID).strip():
        missing_fields.append("client_id")

    errors = [f"缺少 {field}" for field in missing_fields]
    warnings = []
    if exp and exp <= now_ts:
        warnings.append("access token 已过期，导入 sub2api 后需要由 sub2api 使用 refresh token 续期")
    elif exp and exp - now_ts < 3600:
        warnings.append("access token 将在 1 小时内过期，建议尽快导入 sub2api")
    if item.get("auth_failed"):
        errors.append("注册后独立鉴权失败，需要重新登录后再导出")
    if not access_payload and access_token:
        warnings.append("access token 无法解析过期时间")

    remote_status = "skipped"
    remote_error = ""
    needs_remote_metadata = bool(access_token and not item.get("auth_failed") and not str(oauth.get("chatgpt_account_id") or "").strip())
    if remote and needs_remote_metadata:
        try:
            from services.openai_backend_api import InvalidAccessTokenError, OpenAIBackendAPI

            account_hint = {
                "access_token": access_token,
                "proxy_key": item.get("proxy_key"),
                "proxy": item.get("proxy") if isinstance(item.get("proxy"), dict) else None,
            }
            remote = OpenAIBackendAPI(access_token, account=account_hint).get_user_info()
            remote_oauth = remote.get("oauth") if isinstance(remote.get("oauth"), dict) else {}
            account_id = str(remote_oauth.get("chatgpt_account_id") or "").strip()
            if account_id:
                oauth = {**oauth, **remote_oauth, "client_id": str(oauth.get("client_id") or OPENAI_PLATFORM_CLIENT_ID).strip()}
                missing_fields = [field for field in missing_fields if field != "chatgpt_account_id"]
                errors = [error for error in errors if error != "缺少 chatgpt_account_id"]
                register_service.update_registered_account(email, {"oauth": oauth})
            remote_status = "ok"
        except InvalidAccessTokenError as exc:
            remote_status = "invalid"
            remote_error = str(exc)
            warnings.append("当前 access token 已 401；如果 refresh token 仍有效，导入 sub2api 后可由 sub2api 续期")
        except Exception as exc:
            remote_status = "error"
            remote_error = str(exc) or exc.__class__.__name__
            warnings.append(f"远端补齐 chatgpt_account_id 失败：{remote_error}")
    elif remote and access_token and not item.get("auth_failed"):
        remote_status = "skipped"

    return {
        "email": email,
        "ok": not errors,
        "exportable": not errors,
        "missing_fields": missing_fields,
        "errors": errors,
        "warnings": warnings,
        "access_token_expired": bool(exp and exp <= now_ts),
        "access_token_expires_at": expires_at,
        "remote_status": remote_status,
        "remote_error": remote_error,
    }


def _check_registered_accounts(items: list[dict], *, remote: bool) -> dict:
    checks: list[dict] = []
    if remote and len(items) > 1:
        with ThreadPoolExecutor(max_workers=min(4, len(items))) as executor:
            futures = [executor.submit(_check_registered_account, item, remote=remote) for item in items]
            for future in as_completed(futures):
                checks.append(future.result())
        order = {str(item.get("email") or "").strip(): index for index, item in enumerate(items)}
        checks.sort(key=lambda item: order.get(str(item.get("email") or "").strip(), 0))
    else:
        checks = [_check_registered_account(item, remote=remote) for item in items]
    blocking = [item for item in checks if not item.get("exportable")]
    warnings = [item for item in checks if item.get("warnings")]
    return {
        "total": len(checks),
        "ok": not blocking,
        "blocking_count": len(blocking),
        "warning_count": len(warnings),
        "items": checks,
    }

class RegisterConfigRequest(BaseModel):
    mail: dict | None = None
    proxy: str | None = None
    total: int | None = None
    threads: int | None = None
    mode: str | None = None
    target_quota: int | None = None
    target_available: int | None = None
    check_interval: int | None = None
    add_to_local_pool: bool | None = None


class RegisterAccountsDeleteRequest(BaseModel):
    emails: list[str]


class RegisterAccountsRecoverRequest(BaseModel):
    emails: list[str]


class RegisterAccountsManualRecoverStartRequest(BaseModel):
    email: str


class RegisterAccountsManualRecoverCompleteRequest(BaseModel):
    session_id: str
    code: str


class RegisterAccountsImportRequest(BaseModel):
    emails: list[str] = []


def create_router() -> APIRouter:
    router = APIRouter()

    @router.get("/api/register")
    async def get_register_config(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"register": register_service.get()}

    @router.post("/api/register")
    async def update_register_config(body: RegisterConfigRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"register": register_service.update(body.model_dump(exclude_none=True))}

    @router.post("/api/register/start")
    async def start_register(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"register": register_service.start()}

    @router.post("/api/register/stop")
    async def stop_register(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"register": register_service.stop()}

    @router.post("/api/register/reset")
    async def reset_register(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"register": register_service.reset()}

    @router.delete("/api/register/accounts")
    async def delete_registered_accounts(
        body: RegisterAccountsDeleteRequest,
        authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        return register_service.delete_registered_accounts(body.emails)

    @router.post("/api/register/accounts/recover")
    async def recover_registered_accounts(
        body: RegisterAccountsRecoverRequest,
        authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        return register_service.recover_registered_accounts(body.emails)

    @router.post("/api/register/accounts/backfill-mail-metadata")
    async def backfill_registered_accounts_mail_metadata(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return register_service.backfill_registered_mail_metadata()

    @router.post("/api/register/accounts/recover/manual/start")
    async def start_manual_recover_registered_account(
        body: RegisterAccountsManualRecoverStartRequest,
        authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        return register_service.begin_manual_registered_account_recovery(body.email)

    @router.post("/api/register/accounts/recover/manual/complete")
    async def complete_manual_recover_registered_account(
        body: RegisterAccountsManualRecoverCompleteRequest,
        authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        return register_service.complete_manual_registered_account_recovery(body.session_id, body.code)

    @router.post("/api/register/accounts/import-local-pool")
    async def import_registered_accounts_to_pool(
        body: RegisterAccountsImportRequest,
        authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        return register_service.import_registered_accounts_to_pool(body.emails)

    @router.get("/api/register/check/sub2api")
    async def check_register_sub2api(
        emails: str = "",
        remote: bool = True,
        authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        registered = _selected_registered_accounts(emails)
        return {"check": _check_registered_accounts(registered, remote=remote)}

    @router.get("/api/register/export/sub2api")
    async def export_register_sub2api(
        proxy: bool = False,
        emails: str = "",
        authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        registered = _selected_registered_accounts(emails)
        current = register_service.get()
        job_id = str(((current.get("stats") or {}).get("job_id")) or "").strip()
        accounts = [
            _registered_account_to_pool_record(item, job_id)
            for item in registered
            if _registered_account_exportable(item)
        ]
        payload = build_sub2api_export(accounts, include_proxy=proxy)
        suffix = "with-proxy" if proxy else "no-proxy"
        return _json_download(payload, f"sub2api-register-{suffix}.json")

    @router.get("/api/register/events")
    async def register_events(token: str = ""):
        require_admin(f"Bearer {token}")

        async def stream():
            last = ""
            while True:
                payload = json.dumps(register_service.get(), ensure_ascii=False)
                if payload != last:
                    last = payload
                    yield f"data: {payload}\n\n"
                await asyncio.sleep(0.5)

        return StreamingResponse(stream(), media_type="text/event-stream")

    return router
