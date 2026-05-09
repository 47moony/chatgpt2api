from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Header
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel

from api.support import require_admin
from services.account_service import account_service
from services.register_service import register_service
from services.sub2api_export_service import build_sub2api_export, json_bytes




def _json_download(payload: dict, filename: str) -> Response:
    return Response(
        content=json_bytes(payload),
        media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )

class RegisterConfigRequest(BaseModel):
    mail: dict | None = None
    proxy: str | None = None
    total: int | None = None
    threads: int | None = None
    mode: str | None = None
    target_quota: int | None = None
    target_available: int | None = None
    check_interval: int | None = None


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

    @router.get("/api/register/export/sub2api")
    async def export_register_sub2api(proxy: bool = False, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        current = register_service.get()
        job_id = str(((current.get("stats") or {}).get("job_id")) or "").strip()
        accounts = [item for item in account_service.list_accounts() if job_id and str(item.get("register_job_id") or "") == job_id]
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
