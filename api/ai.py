from __future__ import annotations

import asyncio
from typing import Literal

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field

from api.image_inputs import parse_image_edit_request, read_image_sources
from api.support import require_identity, resolve_image_base_url
from services.content_filter import check_request, request_text
from services.log_service import LoggedCall
from services.protocol import (
    anthropic_v1_messages,
    openai_v1_chat_complete,
    openai_v1_image_edit,
    openai_v1_image_generations,
    openai_v1_models,
    openai_v1_response,
)
from services.protocol.conversation import ImageGenerationError
from utils.helper import IMAGE_MODELS


class ImageGenerationRequest(BaseModel):
    prompt: str = Field(..., min_length=1)
    model: str = "gpt-image-2"
    n: int = Field(default=1, ge=1, le=4)
    size: str | None = None
    response_format: str = "b64_json"
    history_disabled: bool = True
    stream: bool | None = None


class CodexImageGenerationRequest(BaseModel):
    prompt: str = Field(..., min_length=1)
    model: str = "gpt-image-2"
    n: int = Field(default=1, ge=1, le=4)
    size: str | None = None
    response_format: Literal["url", "b64_json"] = "url"
    max_attempts: int = Field(default=0, ge=0, le=10000)
    retry_delay_seconds: float = Field(default=5.0, ge=1.0, le=300.0)


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    model: str | None = None
    prompt: str | None = None
    n: int | None = None
    stream: bool | None = None
    modalities: list[str] | None = None
    messages: list[dict[str, object]] | None = None


class ResponseCreateRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    model: str | None = None
    input: object | None = None
    tools: list[dict[str, object]] | None = None
    tool_choice: object | None = None
    stream: bool | None = None


class AnthropicMessageRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    model: str | None = None
    messages: list[dict[str, object]] | None = None
    system: object | None = None
    stream: bool | None = None


async def filter_or_log(call: LoggedCall, text: str) -> None:
    try:
        await run_in_threadpool(check_request, text)
    except HTTPException as exc:
        call.log("调用失败", status="failed", error=str(exc.detail))
        raise


def _collect_image_urls(data: object) -> list[str]:
    urls: list[str] = []
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and isinstance(item.get("url"), str) and item.get("url"):
                urls.append(str(item["url"]))
    return urls


def _is_retryable_image_exception(exc: Exception) -> bool:
    if isinstance(exc, ImageGenerationError):
        code = str(exc.code or "").lower()
        message = str(exc).lower()
        if int(exc.status_code) < 500 and code not in {"insufficient_quota", "upstream_error"}:
            return False
        if code == "content_policy_violation":
            return False
        if "unsupported image model" in message:
            return False
    if isinstance(exc, HTTPException) and int(exc.status_code) < 500 and int(exc.status_code) != 429:
        return False
    return True


def _image_error_detail(message: str, attempts: int, *, retryable: bool) -> dict[str, object]:
    return {
        "error": message or "image generation failed",
        "attempts": attempts,
        "retryable": retryable,
    }


def create_router() -> APIRouter:
    router = APIRouter()

    @router.get("/v1/models")
    async def list_models(authorization: str | None = Header(default=None)):
        require_identity(authorization)
        try:
            return await run_in_threadpool(openai_v1_models.list_models)
        except Exception as exc:
            raise HTTPException(status_code=502, detail={"error": str(exc)}) from exc

    @router.post("/v1/images/generations")
    async def generate_images(
            body: ImageGenerationRequest,
            request: Request,
            authorization: str | None = Header(default=None),
    ):
        identity = require_identity(authorization)
        payload = body.model_dump(mode="python")
        payload["base_url"] = resolve_image_base_url(request)
        call = LoggedCall(identity, "/v1/images/generations", body.model, "文生图", request_text=body.prompt)
        await filter_or_log(call, body.prompt)
        return await call.run(openai_v1_image_generations.handle, payload)

    @router.post("/api/codex/images/generate")
    async def generate_codex_image(
            body: CodexImageGenerationRequest,
            request: Request,
            authorization: str | None = Header(default=None),
            x_api_key: str | None = Header(default=None, alias="x-api-key"),
    ):
        identity = require_identity(authorization or (f"Bearer {x_api_key}" if x_api_key else None))
        if body.model not in IMAGE_MODELS:
            raise HTTPException(
                status_code=400,
                detail={"error": "unsupported image model,supported models: " + ", ".join(sorted(IMAGE_MODELS))},
            )

        call = LoggedCall(identity, "/api/codex/images/generate", body.model, "Codex文生图", request_text=body.prompt)
        await filter_or_log(call, body.prompt)

        payload = {
            "prompt": body.prompt,
            "model": body.model,
            "n": body.n,
            "size": body.size,
            "response_format": body.response_format,
            "base_url": resolve_image_base_url(request),
        }
        attempts = 0
        last_error = ""

        while body.max_attempts == 0 or attempts < body.max_attempts:
            attempts += 1
            try:
                result = await run_in_threadpool(openai_v1_image_generations.handle, payload)
                if not isinstance(result, dict):
                    raise RuntimeError("image generation returned streaming result unexpectedly")
                data = result.get("data")
                if not isinstance(data, list) or not data:
                    message = str(result.get("message") or "").strip()
                    raise ImageGenerationError(message or "image generation returned no image data")

                urls = _collect_image_urls(data)
                response = {
                    "ok": True,
                    "attempts": attempts,
                    "created": result.get("created"),
                    "model": body.model,
                    "size": body.size,
                    "data": data,
                    "urls": urls,
                    "image_urls": urls,
                }
                call.log("调用完成", response)
                return response
            except Exception as exc:
                last_error = str(exc) or exc.__class__.__name__
                retryable = _is_retryable_image_exception(exc)
                if not retryable:
                    call.log("调用失败", status="failed", error=last_error)
                    raise HTTPException(
                        status_code=getattr(exc, "status_code", 400),
                        detail=_image_error_detail(last_error, attempts, retryable=False),
                    ) from exc
                if body.max_attempts and attempts >= body.max_attempts:
                    call.log("调用失败", status="failed", error=last_error)
                    raise HTTPException(
                        status_code=getattr(exc, "status_code", 502),
                        detail=_image_error_detail(last_error, attempts, retryable=True),
                    ) from exc
                await asyncio.sleep(body.retry_delay_seconds)

        call.log("调用失败", status="failed", error=last_error)
        raise HTTPException(status_code=502, detail=_image_error_detail(last_error, attempts, retryable=True))

    @router.post("/v1/images/edits")
    async def edit_images(
            request: Request,
            authorization: str | None = Header(default=None),
    ):
        identity = require_identity(authorization)
        payload, image_sources = await parse_image_edit_request(request)
        prompt = str(payload["prompt"])
        model = str(payload["model"])
        call = LoggedCall(identity, "/v1/images/edits", model, "图生图", request_text=prompt)
        await filter_or_log(call, prompt)
        payload["images"] = await read_image_sources(image_sources)
        payload["base_url"] = resolve_image_base_url(request)
        return await call.run(openai_v1_image_edit.handle, payload)

    @router.post("/v1/chat/completions")
    async def create_chat_completion(body: ChatCompletionRequest, authorization: str | None = Header(default=None)):
        identity = require_identity(authorization)
        payload = body.model_dump(mode="python")
        model = str(payload.get("model") or "auto")
        request_preview = request_text(payload.get("prompt"), payload.get("messages"))
        call = LoggedCall(identity, "/v1/chat/completions", model, "文本生成", request_text=request_preview)
        await filter_or_log(call, request_preview)
        return await call.run(openai_v1_chat_complete.handle, payload)

    @router.post("/v1/responses")
    async def create_response(body: ResponseCreateRequest, authorization: str | None = Header(default=None)):
        identity = require_identity(authorization)
        payload = body.model_dump(mode="python")
        model = str(payload.get("model") or "auto")
        request_preview = request_text(payload.get("input"), payload.get("instructions"))
        call = LoggedCall(identity, "/v1/responses", model, "Responses", request_text=request_preview)
        await filter_or_log(call, request_preview)
        return await call.run(openai_v1_response.handle, payload)

    @router.post("/v1/messages")
    async def create_message(
            body: AnthropicMessageRequest,
            authorization: str | None = Header(default=None),
            x_api_key: str | None = Header(default=None, alias="x-api-key"),
            anthropic_version: str | None = Header(default=None, alias="anthropic-version"),
    ):
        identity = require_identity(authorization or (f"Bearer {x_api_key}" if x_api_key else None))
        payload = body.model_dump(mode="python")
        model = str(payload.get("model") or "auto")
        request_preview = request_text(payload.get("system"), payload.get("messages"), payload.get("tools"))
        call = LoggedCall(identity, "/v1/messages", model, "Messages", request_text=request_preview)
        await filter_or_log(call, request_preview)
        return await call.run(anthropic_v1_messages.handle, payload, sse="anthropic")

    return router
