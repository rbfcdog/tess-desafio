import asyncio
from typing import Any
from uuid import UUID

import httpx2
from fastapi import HTTPException

from core.config import Config


class Supabase:
    def __init__(self, config: Config, client: httpx2.AsyncClient):
        self.config = config
        self.client = client

    def require_database(self) -> None:
        if not self.config.database_configured:
            raise HTTPException(503, "Persistência não configurada no servidor.")

    async def authenticate(self, authorization: str | None) -> str:
        if not self.config.auth_configured:
            raise HTTPException(503, "Autenticação não configurada no servidor.")
        parts = (authorization or "").split()
        if len(parts) != 2 or parts[0].lower() != "bearer" or len(parts[1]) > 16384:
            raise HTTPException(401, "Entre na sua conta para continuar.", headers={"WWW-Authenticate": "Bearer"})
        try:
            response = await self.client.get(
                f"{self.config.supabase_url}/auth/v1/user",
                headers={"apikey": self.config.public_key, "Authorization": f"Bearer {parts[1]}"},
            )
        except httpx2.HTTPError:
            raise HTTPException(503, "Serviço de autenticação indisponível.") from None
        if response.status_code in {400, 401, 403}:
            await self.audit(None, "auth.denied")
            raise HTTPException(401, "Sessão inválida ou expirada. Entre novamente.", headers={"WWW-Authenticate": "Bearer"})
        if response.status_code != 200:
            raise HTTPException(503, "Serviço de autenticação indisponível.")
        try:
            user_id = str(UUID(response.json()["id"]))
        except (ValueError, KeyError, TypeError):
            raise HTTPException(503, "Resposta inválida do serviço de autenticação.") from None
        return user_id

    async def request(self, method: str, resource: str, **kwargs: Any) -> Any:
        self.require_database()
        headers = {"apikey": self.config.secret_key}
        if not self.config.secret_key.startswith("sb_secret_"):
            headers["Authorization"] = f"Bearer {self.config.secret_key}"
        try:
            response = await self.client.request(
                method, f"{self.config.supabase_url}/rest/v1/{resource}",
                headers=headers, **kwargs,
            )
        except httpx2.HTTPError:
            raise HTTPException(503, "Persistência indisponível. Tente novamente mais tarde.") from None
        if response.is_error:
            try:
                code = response.json().get("message", "")
            except (ValueError, AttributeError):
                code = ""
            errors = {
                "CONVERSATION_NOT_FOUND": (404, "Conversa não encontrada."),
                "BUDGET_EXCEEDED": (429, "O limite global de tokens foi atingido."),
                "RUN_RATE_LIMITED": (429, "Muitas mensagens. Aguarde um minuto e tente novamente."),
                "RUN_ALREADY_ACTIVE": (409, "Esta conversa já tem uma resposta em andamento."),
                "REQUEST_ALREADY_PROCESSED": (409, "Esta mensagem já foi enviada. Recarregue a conversa."),
                "RUN_NOT_ACTIVE": (409, "A execução não está mais ativa."),
                "SUMMARY_OUTDATED": (409, "O contexto mudou; atualize a conversa."),
                "ATTACHMENT_NOT_FOUND": (404, "Anexo não encontrado."),
                "INVALID_ATTACHMENT": (422, "Seleção de anexos inválida."),
                "TOOL_NOT_FOUND": (404, "Ferramenta não encontrada."),
                "INVALID_TOOL": (422, "Configuração de ferramenta inválida."),
                "INVALID_TOOL_STATUS": (422, "Estado de ferramenta inválido."),
                "INVALID_SCOPES": (422, "Permissões do conector inválidas."),
                "TOOL_LIMIT": (429, "Limite de ferramentas atingido."),
                "TOOL_NAME_EXISTS": (409, "Já existe uma ferramenta com esse nome."),
            }
            status, message = errors.get(code, (503, "Não foi possível salvar ou consultar os dados."))
            raise HTTPException(status, message)
        try:
            return response.json()
        except ValueError:
            raise HTTPException(503, "Resposta inválida da persistência.") from None

    async def rpc(self, name: str, **parameters: Any) -> dict[str, Any]:
        result = await self.request("POST", f"rpc/{name}", json=parameters)
        if not isinstance(result, dict):
            raise HTTPException(503, "Resposta inválida da persistência.")
        return result

    async def rows(self, table: str, **parameters: str) -> list[dict[str, Any]]:
        # PostgREST caps each page; never silently omit old messages or usage rows.
        rows: list[dict[str, Any]] = []
        offset = 0
        while True:
            page = await self.request("GET", table, params={**parameters, "limit": "1000", "offset": str(offset)})
            if not isinstance(page, list) or any(not isinstance(row, dict) for row in page):
                raise HTTPException(503, "Resposta inválida da persistência.")
            rows.extend(page)
            if not page:
                return rows
            offset += len(page)

    async def owned_conversation(self, user_id: str, conversation_id: str) -> dict[str, Any]:
        rows = await self.rows(
            "conversations", select="id,title,created_at,updated_at",
            user_id=f"eq.{user_id}", id=f"eq.{conversation_id}",
        )
        if not rows:
            await self.audit(user_id, "conversation.access_denied")
            raise HTTPException(404, "Conversa não encontrada.")
        return rows[0]

    async def history(self, user_id: str, conversation_id: str) -> dict[str, Any]:
        await self.owned_conversation(user_id, conversation_id)
        await self.rpc("recover_stale_runs", p_user_id=user_id)
        messages, runs, files, links, tool_calls, summaries, model_steps = await asyncio.gather(
            self.rows(
                "messages", select="id,role,content,status,run_id,created_at",
                user_id=f"eq.{user_id}", conversation_id=f"eq.{conversation_id}", order="created_at.asc,id.asc",
            ),
            self.rows(
                "runs", select="id,status,model,input_tokens,output_tokens,reserved_tokens,error_code,created_at,finished_at",
                user_id=f"eq.{user_id}", conversation_id=f"eq.{conversation_id}", order="created_at.asc,id.asc",
            ),
            self.rows(
                "attachments", select="id,conversation_id,filename,media_type,size_bytes,page_count,created_at",
                user_id=f"eq.{user_id}", conversation_id=f"eq.{conversation_id}", order="created_at.asc,id.asc",
            ),
            self.rows(
                "message_attachments", select="message_id,attachment_id",
                user_id=f"eq.{user_id}", conversation_id=f"eq.{conversation_id}", order="message_id.asc,attachment_id.asc",
            ),
            self.rows(
                "tool_executions", select="id,run_id,name,status,arguments,result,error,created_at,finished_at",
                user_id=f"eq.{user_id}", conversation_id=f"eq.{conversation_id}", order="created_at.asc,id.asc",
            ),
            self.rows(
                "context_summaries", select="id,run_id,through_message_id,scope,created_at",
                user_id=f"eq.{user_id}", conversation_id=f"eq.{conversation_id}", order="created_at.asc,id.asc",
            ),
            self.rows(
                "model_steps", select="id,run_id,kind,status,reserved_tokens,input_tokens,output_tokens,created_at,finished_at",
                user_id=f"eq.{user_id}", conversation_id=f"eq.{conversation_id}", order="created_at.asc,id.asc",
            ),
        )
        by_id = {file["id"]: file for file in files}
        by_message: dict[str, list[dict[str, Any]]] = {}
        for link in links:
            if link["attachment_id"] in by_id:
                by_message.setdefault(link["message_id"], []).append(by_id[link["attachment_id"]])
        for message in messages:
            message["attachments"] = by_message.get(message["id"], [])
        return {"messages": messages, "runs": runs, "tool_calls": tool_calls, "summaries": summaries, "model_steps": model_steps}

    async def settings(self, user_id: str) -> dict[str, str]:
        rows = await self.rows("user_settings", select="system_prompt", user_id=f"eq.{user_id}")
        if len(rows) != 1 or not isinstance(rows[0].get("system_prompt"), str):
            raise HTTPException(503, "As configurações da conta não estão disponíveis.")
        return {"system_prompt": rows[0]["system_prompt"]}

    async def audit(self, user_id: str | None, event: str, **metadata: Any) -> None:
        if not self.config.database_configured:
            return
        try:
            await self.rpc("record_audit", p_user_id=user_id, p_event=event, p_metadata=metadata)
        except HTTPException:
            # Audit denial logging is best effort; run/accounting transactions are not.
            pass
