import asyncio
import json
import os
from collections.abc import AsyncIterator, Awaitable
from time import monotonic
from typing import Any

import anyio
from fastapi import HTTPException, Request
from openai import AsyncOpenAI
from starlette.responses import StreamingResponse

from services.context import Context, SUMMARY_INSTRUCTIONS
from repositories.supabase import Supabase
from services.tools import ToolRegistry

TRUNCATION_NOTICE = "\n\n[Resposta interrompida no limite de saída. Envie outra mensagem para continuar.]"
MAX_RESPONSE_BYTES = 262144
MAX_TOOL_RESULT_BYTES = 32768
MAX_TOOL_BYTES = 131072
RUN_SECONDS = 480


def line(event: dict[str, Any]) -> bytes:
    return (json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def value(item: Any, key: str, default: Any = None) -> Any:
    return item.get(key, default) if isinstance(item, dict) else getattr(item, key, default)


def output_items(response: Any) -> list[dict[str, Any]]:
    return [item if isinstance(item, dict) else item.model_dump(exclude_none=True) for item in value(response, "output", [])]


def response_text(response: Any) -> str:
    return "".join(
        part.get("text", part.get("refusal", ""))
        for item in output_items(response) if item.get("type") == "message"
        for part in item.get("content", []) if part.get("type") in {"output_text", "refusal"}
    )


class Interrupted(Exception):
    pass


async def settle(
    db: Supabase, user_id: str, run_id: str, status: str, content: str,
    input_tokens: int | None, output_tokens: int | None, error_code: str | None,
    *, pending_reservation: bool = False,
) -> None:
    parameters = dict(p_user_id=user_id, p_run_id=run_id, p_status=status, p_content=content, p_error_code=error_code)
    if not pending_reservation:
        parameters.update(p_input_tokens=input_tokens, p_output_tokens=output_tokens)
    result = await db.rpc("finish_unattempted_run" if pending_reservation else "finish_run", **parameters)
    if result.get("ok") is not True:
        raise HTTPException(503, "Não foi possível confirmar a persistência da resposta.")


class Run:
    """One concrete, bounded Responses run; all remote waits share the same heartbeat."""

    def __init__(self, request: Request, db: Supabase, provider: AsyncOpenAI, user_id: str, conversation_id: str, run_id: str):
        self.request, self.db, self.provider = request, db, provider
        self.user_id, self.conversation_id, self.run_id = user_id, conversation_id, run_id
        self.started = self.checkpoint_at = monotonic()
        self.text = ""
        self.saved_bytes = 0
        self.input_tokens = self.output_tokens = 0
        self.unknown_steps = 0
        self.steps = self.tool_calls = self.tool_bytes = self.reply_tokens = 0
        self.active_step: str | None = None
        self.active_usage: tuple[int, int] | None = None
        self.reservation_pending = False
        self.stream: Any = None
        self.error_code = "provider_stream_failed"
        self.soft = int(os.environ.get("CONTEXT_INPUT_TOKENS", "16000"))
        self.window = int(os.environ.get("CONTEXT_WINDOW_TOKENS", "128000"))
        self.max_steps = int(os.environ.get("MAX_MODEL_STEPS", "8"))
        self.max_tools = int(os.environ.get("MAX_TOOL_CALLS", "8"))
        self.summary_cap = int(os.environ.get("SUMMARY_OUTPUT_TOKENS", "1500"))

    async def check(self) -> None:
        if monotonic() - self.started >= RUN_SECONDS:
            self.error_code = "run_timeout"
            raise Interrupted()
        if await self.request.is_disconnected():
            self.error_code = "client_disconnected"
            raise Interrupted()
        now = monotonic()
        size = len(self.text.encode("utf-8"))
        if now - self.checkpoint_at >= 15 or size - self.saved_bytes >= 4096 or (size != self.saved_bytes and now - self.checkpoint_at >= 2):
            result = await self.db.rpc("checkpoint_run", p_user_id=self.user_id, p_run_id=self.run_id, p_content=self.text)
            if result.get("ok") is not True:
                raise HTTPException(503, "Não foi possível salvar a resposta parcial.")
            self.checkpoint_at, self.saved_bytes = monotonic(), size

    async def wait(self, operation: Awaitable[Any]) -> Any:
        pending = asyncio.ensure_future(operation)
        try:
            async with asyncio.timeout(max(0, self.started + RUN_SECONDS - monotonic())):
                while True:
                    await self.check()
                    ready, _ = await asyncio.wait({pending}, timeout=1)
                    if ready:
                        return pending.result()
        except TimeoutError:
            self.error_code = "run_timeout"
            raise Interrupted() from None
        finally:
            if not pending.done():
                pending.cancel()
                with anyio.move_on_after(4, shield=True):
                    try:
                        await pending
                    except (asyncio.CancelledError, Exception):
                        pass

    async def count(self, prompt: list[dict[str, Any]], tools: list[dict[str, Any]]) -> int:
        result = await self.wait(self.provider.responses.input_tokens.count(model=self.db.config.model, input=prompt, tools=tools))
        count = value(result, "input_tokens")
        if type(count) is not int or count < 0:
            raise RuntimeError("Invalid input token count")
        return count

    async def reserve(self, kind: str, input_tokens: int, output_cap: int) -> None:
        if self.steps >= self.max_steps:
            self.error_code = "model_step_limit"
            raise HTTPException(422, "Limite de etapas atingido. O contexto já resumido foi preservado; envie uma nova mensagem para continuar.")
        if input_tokens + output_cap > self.window:
            self.error_code = "context_window_exceeded"
            raise HTTPException(413, "O conteúdo ou arquivo atual excede a janela do modelo. Envie um arquivo menor; o histórico foi preservado.")
        self.reservation_pending = True
        result = await self.wait(self.db.rpc(
            "reserve_model_step", p_user_id=self.user_id, p_run_id=self.run_id,
            p_kind=kind, p_input_tokens=input_tokens, p_max_output_tokens=output_cap,
        ))
        self.active_step = result["id"]
        self.active_usage = None
        self.steps += 1
        # A transport failure may occur after the provider accepted the request. Unknown usage
        # remains reserved, even when there was no text and the client never saw a delta.
        self.unknown_steps += 1
        self.reservation_pending = False

    def observe_usage(self, response: Any) -> None:
        usage = value(response, "usage")
        counts = (value(usage, "input_tokens"), value(usage, "output_tokens"))
        if all(type(count) is int and count >= 0 for count in counts):
            self.active_usage = counts
            self.input_tokens += counts[0]
            self.output_tokens += counts[1]
            self.unknown_steps -= 1

    async def record(self, status: str, *, cleanup: bool = False) -> None:
        if self.active_step is None:
            return
        counts = self.active_usage or (None, None)
        operation = self.db.rpc(
            "record_model_step", p_user_id=self.user_id, p_step_id=self.active_step,
            p_status=status, p_input_tokens=counts[0], p_output_tokens=counts[1],
        )
        result = await operation if cleanup else await self.wait(operation)
        if result.get("ok") is not True:
            raise HTTPException(503, "Não foi possível salvar o uso do modelo.")
        self.active_step = None
        self.active_usage = None

    def append_text(self, delta: str) -> None:
        if len(self.text.encode("utf-8")) + len(delta.encode("utf-8")) > MAX_RESPONSE_BYTES:
            self.error_code = "response_size_limit"
            raise RuntimeError("Response size limit")
        self.text += delta

    async def summarize(self, prompt: list[dict[str, Any]], count: int) -> str:
        await self.reserve("compaction", count, self.summary_cap)
        response = await self.wait(self.provider.responses.create(
            model=self.db.config.model, input=prompt, tools=[], max_output_tokens=self.summary_cap,
            store=False, stream=False, truncation="disabled",
        ))
        self.observe_usage(response)
        summary = response_text(response)
        if value(response, "status") != "completed" or not self.active_usage or not summary.strip() or len(summary.encode("utf-8")) > 65536:
            self.error_code = "compaction_failed"
            await self.record("failed")
            raise RuntimeError("Incomplete summary")
        await self.record("completed")
        return summary

    async def compact(self, context: Context, tools: list[dict[str, Any]], output_cap: int) -> AsyncIterator[dict[str, Any]]:
        while True:
            prompt = await self.wait(context.prompt(self.db, self.user_id, self.conversation_id))
            count = await self.count(prompt, tools)
            if count <= min(self.soft, self.window - output_cap):
                return
            # Prefer old history, retaining the latest two complete turns whenever possible.
            # If those alone exceed the threshold, compact them too, never the current input.
            if context.turns:
                limit = max(1, len(context.turns) - 2)
                source: list[dict[str, Any]] = []
                if context.summary:
                    source.append(context.summary_item(context.summary, "Previous history summary"))
                selected = 0
                summary_prompt: list[dict[str, Any]] = []
                summary_count = 0
                for turn in context.turns[:limit]:
                    items = await self.wait(context.turn_items(self.db, self.user_id, self.conversation_id, [turn]))
                    candidate = [{"role": "system", "content": SUMMARY_INSTRUCTIONS}, *source, *items]
                    candidate_count = await self.count(candidate, [])
                    if selected and candidate_count > min(self.soft, self.window - self.summary_cap):
                        break
                    if candidate_count + self.summary_cap > self.window:
                        self.error_code = "context_window_exceeded"
                        raise HTTPException(413, "Um turno antigo excede a janela do modelo; o histórico original foi preservado.")
                    source.extend(items)
                    selected += 1
                    summary_prompt, summary_count = candidate, candidate_count
                through = context.turns[selected - 1][-1]["id"]
                scope = "history"
            elif len(context.loop_groups) > 1:
                # Retain the most recent complete group and never summarize a pending pair.
                source = [context.summary_item(context.loop_summary, "Previous tool-loop summary")] if context.loop_summary else []
                selected = 0
                for group in context.loop_groups[:-1]:
                    item = {"role": "user", "content": json.dumps(group, ensure_ascii=False)}
                    candidate = [{"role": "system", "content": SUMMARY_INSTRUCTIONS}, *source, item]
                    candidate_count = await self.count(candidate, [])
                    if selected and candidate_count > min(self.soft, self.window - self.summary_cap):
                        break
                    if candidate_count + self.summary_cap > self.window:
                        self.error_code = "context_window_exceeded"
                        raise HTTPException(413, "O resultado da ferramenta excede a janela do modelo; os dados salvos foram preservados.")
                    source.append(item)
                    selected += 1
                    summary_prompt, summary_count = candidate, candidate_count
                through, scope = None, "loop"
            else:
                # A soft threshold is not a hard rejection: an indivisible current file or
                # recent call/result group is allowed up to the real model window.
                if count + output_cap > self.window:
                    self.error_code = "context_window_exceeded"
                    raise HTTPException(413, "O conteúdo ou arquivo atual excede a janela do modelo; o histórico foi preservado.")
                return
            yield {"type": "status", "stage": "compacting", "text": "Resumindo contexto anterior sem apagar o histórico."}
            summary = await self.summarize(summary_prompt, summary_count)
            saved = await self.wait(self.db.rpc(
                "save_summary", p_user_id=self.user_id, p_conversation_id=self.conversation_id,
                p_run_id=self.run_id, p_through_message_id=through, p_summary=summary, p_scope=scope,
            ))
            if scope == "history":
                context.summary = summary
                context.turns = context.turns[selected:]
            else:
                context.loop_summary = summary
                context.loop_groups = context.loop_groups[selected:]
            yield {"type": "compaction", "summary_id": saved["id"], "through_message_id": through}

    async def reply(self, prompt: list[dict[str, Any]], tools: list[dict[str, Any]], output_cap: int) -> AsyncIterator[dict[str, Any]]:
        count = await self.count(prompt, tools)
        await self.reserve("reply", count, output_cap)
        self.stream = await self.wait(self.provider.responses.create(
            model=self.db.config.model, input=prompt, tools=tools, max_output_tokens=output_cap,
            max_tool_calls=max(1, self.max_tools - self.tool_calls),
            store=False, stream=True, truncation="disabled", include=["reasoning.encrypted_content"],
        ))
        iterator = self.stream.__aiter__()
        try:
            while True:
                try:
                    event = await self.wait(anext(iterator))
                except StopAsyncIteration:
                    self.error_code = "provider_stream_ended"
                    raise RuntimeError("Missing terminal event") from None
                event_type = value(event, "type")
                if event_type in {"response.output_text.delta", "response.refusal.delta"}:
                    delta = value(event, "delta", "")
                    self.append_text(delta)
                    yield {"type": "delta", "text": delta}
                elif event_type in {"response.completed", "response.incomplete", "response.failed"}:
                    response = value(event, "response")
                    self.observe_usage(response)
                    truncated = event_type == "response.incomplete" and value(value(response, "incomplete_details"), "reason") == "max_output_tokens"
                    if (event_type != "response.completed" and not truncated) or self.active_usage is None:
                        self.error_code = "provider_usage_missing" if self.active_usage is None else "provider_response_failed"
                        await self.record("failed")
                        raise RuntimeError("Provider response not complete")
                    self.reply_tokens += self.active_usage[1]
                    await self.record("completed")
                    yield {"type": "terminal", "response": response, "truncated": truncated}
                    return
                elif event_type in {"error", "response.error"}:
                    raise RuntimeError("Provider stream error")
        finally:
            with anyio.move_on_after(4, shield=True):
                try:
                    await self.stream.close()
                except Exception:
                    pass
            self.stream = None


async def chat_response(
    request: Request, db: Supabase, provider: AsyncOpenAI, user_id: str,
    conversation_id: str, content: str, request_id: str, attachment_ids: list[str] | None = None,
    *, regenerate: bool = False,
) -> StreamingResponse:
    try:
        if regenerate:
            identifiers = await db.rpc(
                "begin_regenerate_run", p_user_id=user_id, p_conversation_id=conversation_id,
                p_request_id=request_id, p_model=db.config.model, p_reserved_tokens=1,
            )
        else:
            identifiers = await db.rpc(
                "begin_run", p_user_id=user_id, p_conversation_id=conversation_id,
                p_request_id=request_id, p_content=content, p_model=db.config.model,
                p_reserved_tokens=1, p_attachment_ids=attachment_ids or [],
            )
    except HTTPException as exc:
        if exc.status_code in {409, 429}:
            await db.audit(user_id, "run.denied", status=exc.status_code)
        raise
    run = Run(request, db, provider, user_id, conversation_id, identifiers["run_id"])

    async def events() -> AsyncIterator[bytes]:
        saved = False
        status = "failed"
        error_message = "A resposta foi interrompida. Recarregue a conversa antes de continuar."
        try:
            yield line({"type": "start", **identifiers})
            # Snapshot only AFTER acquiring the conversation's active slot. Failed/running turns
            # remain in raw history, but never become model context or summary coverage.
            history = await run.wait(db.history(user_id, conversation_id))
            prompt_content, prompt_attachments = content, attachment_ids or []
            if regenerate:
                last_user = next((message for message in reversed(history["messages"])
                                  if message["role"] == "user" and message["status"] == "completed"), None)
                if last_user is None:
                    raise HTTPException(422, "Não há mensagem para regenerar.")
                prompt_content = last_user["content"]
                prompt_attachments = [attachment["id"] for attachment in last_user.get("attachments", [])]
            summaries = await run.wait(db.rows(
                "context_summaries", select="id,run_id,through_message_id,scope,summary,created_at",
                user_id=f"eq.{user_id}", conversation_id=f"eq.{conversation_id}",
                scope="eq.history", order="created_at.asc,id.asc",
            ))
            settings = await run.wait(db.settings(user_id))
            conversation_rows = await run.wait(db.rows(
                "conversations", select="title", user_id=f"eq.{user_id}", id=f"eq.{conversation_id}",
            ))
            # The first exchange names an unnamed conversation; the model writes the title.
            if (conversation_rows and conversation_rows[0].get("title") == "Nova conversa"
                    and any(message["role"] == "user" for message in history["messages"])
                    and not any(message["role"] == "assistant" and message["status"] == "completed"
                                for message in history["messages"])
                    and prompt_content):
                try:
                    title_prompt = ("Escolha um titulo curto em portugues que resume esta abertura de conversa. "
                                    "Responda somente com o titulo, sem aspas, com no maximo 40 caracteres.\n\n"
                                    f"Mensagem: {prompt_content[:600]}")
                    title_count = await run.count([{"role": "user", "content": title_prompt}], [])
                    await run.reserve("reply", title_count, 200)
                    title_response = await run.wait(provider.responses.create(
                        model=db.config.model, input=[{"role": "user", "content": title_prompt}],
                        tools=[], max_output_tokens=200, store=False, stream=False, truncation="disabled",
                    ))
                    run.observe_usage(title_response)
                    candidate = response_text(title_response).strip().strip('"').strip()[:60]
                    await run.record("completed")
                    if candidate:
                        await run.wait(db.rpc("rename_conversation", p_user_id=user_id,
                                              p_conversation_id=conversation_id, p_title=candidate))
                except Exception:
                    # A failed naming attempt never blocks the chat; the step is settled conservatively.
                    try:
                        await run.record("failed", cleanup=True)
                    except Exception:
                        pass
            context = Context({**history, "summaries": summaries}, settings["system_prompt"], prompt_content, prompt_attachments)
            registry = await run.wait(ToolRegistry.load(db, user_id, conversation_id, run.run_id))
            current = await run.wait(context.current_item(db, user_id, conversation_id))
            minimum = ([{"role": "system", "content": context.system_prompt}] if context.system_prompt else []) + [current]
            if await run.count(minimum, registry.definitions) + db.config.max_output_tokens > run.window:
                run.error_code = "context_window_exceeded"
                raise HTTPException(413, "O conteúdo ou arquivo atual excede a janela do modelo; o histórico foi preservado.")
            seen_calls: set[str] = set()
            while True:
                output_cap = db.config.max_output_tokens - run.reply_tokens
                if output_cap <= 0:
                    run.error_code = "output_limit"
                    raise RuntimeError("Output limit reached")
                tools = registry.definitions if run.tool_calls < run.max_tools else []
                async for event in run.compact(context, tools, output_cap):
                    yield line(event)
                yield line({"type": "status", "stage": "thinking", "text": "Preparando resposta."})
                prompt = await run.wait(context.prompt(db, user_id, conversation_id))
                response = None
                truncated = False
                before = len(run.text)
                async for event in run.reply(prompt, tools, output_cap):
                    if event["type"] == "terminal":
                        response, truncated = event["response"], event["truncated"]
                    else:
                        yield line(event)
                if response is None:
                    raise RuntimeError("Missing response")
                if len(run.text) == before:
                    fallback = response_text(response)
                    if fallback:
                        run.append_text(fallback)
                        yield line({"type": "delta", "text": fallback})
                items = output_items(response)
                calls = [item for item in items if item.get("type") == "function_call"]
                native = [item for item in items if item.get("type") == "web_search_call"]
                for item in native:
                    await run.wait(registry.record_native(item))
                    run.tool_calls += 1
                    yield line({"type": "tool", "id": item["id"], "name": "web_search", "status": "completed" if item.get("status") == "completed" else "failed"})
                if run.tool_calls + len(calls) > run.max_tools:
                    run.error_code = "tool_call_limit"
                    raise RuntimeError("Tool call limit reached")
                if truncated:
                    # Incomplete function arguments are never executed.
                    run.append_text(TRUNCATION_NOTICE)
                    yield line({"type": "delta", "text": TRUNCATION_NOTICE})
                elif calls:
                    group = list(items)
                    for call in calls:
                        call_id = call["call_id"]
                        if call_id in seen_calls:
                            raise RuntimeError("Repeated function call ID")
                        seen_calls.add(call_id)
                        run.tool_calls += 1
                        name, arguments = call["name"], call["arguments"]
                        yield line({"type": "status", "stage": "tool", "text": f"Consultando ferramenta {name}."})
                        yield line({"type": "tool", "id": call_id, "name": name, "status": "running"})
                        tool_failed = False
                        try:
                            result = await run.wait(registry.execute(name, arguments))
                            encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
                            size = len(encoded.encode("utf-8"))
                            if size > MAX_TOOL_RESULT_BYTES or run.tool_bytes + size > MAX_TOOL_BYTES:
                                raise RuntimeError("Tool result size limit")
                            run.tool_bytes += size
                        except (asyncio.CancelledError, Interrupted):
                            raise
                        except Exception:
                            # A falha da ferramenta não derruba o turno: o modelo recebe o erro e pode responder sem ela.
                            result = {"error": f"A ferramenta {name} falhou agora e pode funcionar se tentada novamente."}
                            encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
                            run.tool_bytes += len(encoded.encode("utf-8"))
                            tool_failed = True
                        if isinstance(result, dict) and isinstance(result.get("error"), str):
                            tool_failed = True
                        yield line({
                            "type": "tool", "id": call_id, "name": name,
                            "status": "failed" if tool_failed else "completed",
                            **({"error": result["error"]} if tool_failed else {"result": result}),
                        })
                        tool_output = {"type": "function_call_output", "call_id": call_id, "output": encoded}
                        if call.get("caller") is not None:
                            tool_output["caller"] = call["caller"]
                        group.append(tool_output)
                    context.loop_groups.append(group)
                    continue
                elif not any(item.get("type") == "message" for item in items):
                    # Native tools/reasoning can require another stateless generation.
                    context.loop_groups.append(items)
                    continue
                await run.check()
                await run.wait(settle(
                    db, user_id, run.run_id, "completed", run.text,
                    run.input_tokens, run.output_tokens, "output_truncated" if truncated else None,
                ))
                saved = True
                yield line({
                    "type": "done", "run_id": run.run_id, "status": "completed",
                    "input_tokens": run.input_tokens, "output_tokens": run.output_tokens,
                    **({"truncated": True} if truncated else {}),
                })
                return
        except (asyncio.CancelledError, GeneratorExit):
            status, run.error_code = "interrupted", "client_disconnected"
            raise
        except Interrupted:
            status = "interrupted"
        except Exception as exc:
            if isinstance(exc, HTTPException):
                error_message = str(exc.detail)
                if exc.status_code == 429:
                    run.error_code = "budget_exceeded"
                elif exc.status_code == 503:
                    run.error_code = "persistence_unavailable"
        finally:
            # Never turn a persistence failure into a saved completion. Unknown paid calls retain
            # their full reservation; stale-run recovery handles a failed bounded cleanup.
            with anyio.move_on_after(15, shield=True):
                if run.stream is not None:
                    try:
                        await run.stream.close()
                    except Exception:
                        pass
                if not saved:
                    try:
                        await run.record("failed", cleanup=True)
                        await settle(
                            db, user_id, run.run_id, status, run.text,
                            None if run.unknown_steps else run.input_tokens,
                            None if run.unknown_steps else run.output_tokens, run.error_code,
                            pending_reservation=run.reservation_pending,
                        )
                        saved = True
                    except Exception:
                        pass
        yield line({
            "type": "error", "run_id": run.run_id, "status": status,
            "error": error_message if saved else "Não foi possível confirmar o salvamento. A reserva permanece protegida; recarregue mais tarde para recuperar a conversa.",
        })

    return StreamingResponse(events(), media_type="application/x-ndjson", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
