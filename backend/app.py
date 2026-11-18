import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from time import monotonic
from typing import Annotated
from uuid import UUID

import httpx2
import uvicorn
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from openai import AsyncOpenAI, DefaultAsyncHttpx2Client
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from starlette.exceptions import HTTPException

from auth import User
from assets import router as assets_router
from connectors import router as connectors_router
from chat import chat_response
from service import Config, Supabase
from tools import router as tools_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    config = Config.from_env()
    async with httpx2.AsyncClient(timeout=httpx2.Timeout(10, connect=3), follow_redirects=False) as client:
        app.state.db = Supabase(config, client)
        app.state.provider = None
        if config.chat_configured:
            app.state.provider = AsyncOpenAI(
                api_key=config.openai_key, max_retries=0,
                base_url="https://api.openai.com/v1",
                http_client=DefaultAsyncHttpx2Client(
                    timeout=httpx2.Timeout(60, connect=10), follow_redirects=False,
                ),
            )
        try:
            yield
        finally:
            if app.state.provider is not None:
                await app.state.provider.close()


app = FastAPI(title="Tess API", lifespan=lifespan)
app.include_router(assets_router)
app.include_router(tools_router)
app.include_router(connectors_router)
_started_at = monotonic()
Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
Content = Annotated[str, StringConstraints(strip_whitespace=True, max_length=16000)]


class ConversationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: Title = "Nova conversa"


class ConversationRename(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: Title


class SettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    system_prompt: str = Field(max_length=8000)


class MessageCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: Content = ""
    request_id: UUID
    attachment_ids: list[UUID] = Field(default_factory=list, max_length=5)
    regenerate: bool = False

    @model_validator(mode="after")
    def require_message(self) -> "MessageCreate":
        if not self.regenerate and not self.content and not self.attachment_ids:
            raise ValueError("A message requires text or attachments")
        if len(set(self.attachment_ids)) != len(self.attachment_ids):
            raise ValueError("Duplicate attachments")
        return self


@app.api_route("/api/health", methods=["GET", "HEAD"], include_in_schema=False)
async def health() -> JSONResponse:
    return JSONResponse(
        {
            "status": "ok",
            "service": "tess-desafio",
            "timestamp": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "uptimeSeconds": int(monotonic() - _started_at),
        },
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/config")
async def configuration(request: Request) -> JSONResponse:
    config = request.app.state.db.config
    return JSONResponse({
        "auth_configured": config.auth_configured,
        "chat_configured": config.chat_configured,
        "model": config.model if config.chat_configured else None,
        "features": {"attachments": True, "sharing": True, "tools": True, "compaction": True, "mcp": True},
        "context_input_tokens": config.context_input_tokens,
    }, headers={"Cache-Control": "no-store"})


@app.get("/api/conversations")
async def conversations(request: Request, user_id: User) -> dict:
    db = request.app.state.db
    await db.rpc("recover_stale_runs", p_user_id=user_id)
    return await db.rpc("list_conversations", p_user_id=user_id)


@app.post("/api/conversations", status_code=201)
async def create_conversation(body: ConversationCreate, request: Request, user_id: User) -> dict:
    return {"conversation": await request.app.state.db.rpc("create_conversation", p_user_id=user_id, p_title=body.title)}


@app.patch("/api/conversations/{conversation_id}")
async def rename_conversation(conversation_id: UUID, body: ConversationRename, request: Request, user_id: User) -> dict:
    return {"conversation": await request.app.state.db.rpc(
        "rename_conversation", p_user_id=user_id, p_conversation_id=str(conversation_id), p_title=body.title,
    )}


@app.delete("/api/conversations/{conversation_id}")
async def delete_conversation(conversation_id: UUID, request: Request, user_id: User) -> dict:
    return await request.app.state.db.rpc("delete_conversation", p_user_id=user_id, p_conversation_id=str(conversation_id))


@app.get("/api/conversations/{conversation_id}/messages")
async def messages(conversation_id: UUID, request: Request, user_id: User) -> dict:
    return await request.app.state.db.history(user_id, str(conversation_id))


@app.get("/api/settings")
async def settings(request: Request, user_id: User) -> dict:
    return await request.app.state.db.settings(user_id)


@app.patch("/api/settings")
async def update_settings(body: SettingsUpdate, request: Request, user_id: User) -> dict:
    return await request.app.state.db.rpc("update_settings", p_user_id=user_id, p_system_prompt=body.system_prompt)


@app.get("/api/usage")
async def usage(request: Request, user_id: User) -> dict:
    db = request.app.state.db
    account = await db.rows("usage_accounts", select="limit_tokens,used_tokens,reserved_tokens", id="eq.global")
    if len(account) != 1:
        raise HTTPException(503, "A conta global de uso não está disponível.")
    ledger = await db.rows("usage_ledger", select="charged_tokens", user_id=f"eq.{user_id}", order="created_at.asc,id.asc")
    result = {key: account[0][key] for key in ("limit_tokens", "used_tokens", "reserved_tokens")}
    result["remaining_tokens"] = max(0, result["limit_tokens"] - result["used_tokens"] - result["reserved_tokens"])
    result["user_tokens"] = sum(row["charged_tokens"] for row in ledger)
    if any(type(value) is not int or value < 0 for value in result.values()):
        raise HTTPException(503, "O uso de tokens não está disponível.")
    return result


@app.post("/api/conversations/{conversation_id}/messages")
async def send_message(conversation_id: UUID, body: MessageCreate, request: Request, user_id: User) -> StreamingResponse:
    db = request.app.state.db
    await db.owned_conversation(user_id, str(conversation_id))
    if request.app.state.provider is None:
        raise HTTPException(503, "Chat não configurado. O servidor precisa das chaves Supabase e OpenAI e de um modelo/limite de saída válidos.")
    return await chat_response(
        request, db, request.app.state.provider, user_id,
        str(conversation_id), body.content, str(body.request_id), [str(item) for item in body.attachment_ids],
        regenerate=body.regenerate,
    )


@app.middleware("http")
async def prevent_private_caching(request: Request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.exception_handler(HTTPException)
async def http_error(_request: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse(
        {"error": {404: "Not found", 405: "Method not allowed"}.get(exc.status_code, str(exc.detail))},
        status_code=exc.status_code,
        headers={"Cache-Control": "no-store", **(exc.headers or {})},
    )


@app.exception_handler(RequestValidationError)
async def validation_error(_request: Request, _exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        {"error": "Dados inválidos. Confira os campos e limites de tamanho antes de enviar."},
        status_code=422, headers={"Cache-Control": "no-store"},
    )


@app.exception_handler(Exception)
async def unexpected_error(_request: Request, _exc: Exception) -> JSONResponse:
    return JSONResponse(
        {"error": "Serviço temporariamente indisponível."},
        status_code=503, headers={"Cache-Control": "no-store"},
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "3001"))
    if not 1 <= port <= 65535:
        raise ValueError("PORT must be an integer between 1 and 65535")
    # OAuth callbacks contain one-time codes; application events are audited in the database.
    uvicorn.run(app, host="0.0.0.0", port=port, access_log=False)
