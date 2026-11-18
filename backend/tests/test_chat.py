import asyncio
import copy
import json
import unittest
from time import monotonic
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from services.chat import Run, chat_response
from services.context import Context
from test_context import history_with_turns


def message(text):
    return {"type": "message", "id": "message", "role": "assistant", "status": "completed", "content": [{"type": "output_text", "text": text, "annotations": []}]}


def response(output=None, *, usage=(11, 7), status="completed", reason=None):
    return SimpleNamespace(
        output=output if output is not None else [message("answer")], status=status,
        usage=SimpleNamespace(input_tokens=usage[0], output_tokens=usage[1]) if usage is not None else None,
        incomplete_details=SimpleNamespace(reason=reason) if reason else None,
    )


class Stream:
    def __init__(self, *events):
        self.events = events
        self.close = AsyncMock()

    def __aiter__(self):
        return self.iterate()

    async def iterate(self):
        for event in self.events:
            if isinstance(event, Exception):
                raise event
            yield event


def terminal(result=None, kind="response.completed"):
    return SimpleNamespace(type=kind, response=result or response())


class EngineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.history = {"messages": [], "runs": [], "summaries": []}
        self.reservations = []
        self.records = []
        self.summaries = []
        self.finishes = []
        self.fail_reserve = False
        self.fail_finish = False
        self.renames = []
        self.conversation_rows = [{"title": "Nova conversa"}]

        async def rpc(name, **parameters):
            if name == "begin_run":
                return {"run_id": "run", "user_message_id": "user-message", "assistant_message_id": "assistant-message"}
            if name == "begin_regenerate_run":
                return {"run_id": "run", "user_message_id": "user-1", "assistant_message_id": "assistant-message"}
            if name == "rename_conversation":
                self.renames.append(parameters["p_title"])
            if name == "reserve_model_step":
                if self.fail_reserve:
                    raise HTTPException(429, "budget exhausted")
                self.reservations.append(parameters)
                return {"id": f"step-{len(self.reservations)}", "reserved_tokens": 999}
            if name == "record_model_step":
                self.records.append(parameters)
            if name == "save_summary":
                self.summaries.append(parameters)
                return {"id": f"summary-{len(self.summaries)}"}
            if name in {"finish_run", "finish_unattempted_run"}:
                self.finishes.append(parameters)
                if self.fail_finish:
                    raise HTTPException(503, "persistence failed")
            return {"ok": True}

        def rows_side_effect(table, **parameters):
            if table == "conversations":
                return self.conversation_rows
            return self.history.get("summaries", [])

        self.db = SimpleNamespace(
            config=SimpleNamespace(model="gpt-4.1-mini", max_output_tokens=2048),
            history=AsyncMock(side_effect=lambda *_args: self.history),
            rows=AsyncMock(side_effect=rows_side_effect),
            settings=AsyncMock(return_value={"system_prompt": ""}),
            rpc=AsyncMock(side_effect=rpc), audit=AsyncMock(),
        )
        self.registry = SimpleNamespace(definitions=[], execute=AsyncMock(return_value={"facts": "real result"}), record_native=AsyncMock())
        self.provider = SimpleNamespace(responses=SimpleNamespace(
            input_tokens=SimpleNamespace(count=AsyncMock(return_value=SimpleNamespace(input_tokens=100))),
            create=AsyncMock(return_value=Stream(terminal())),
        ))
        self.request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
        self.loader = patch("services.chat.ToolRegistry.load", AsyncMock(return_value=self.registry))
        self.loader.start()
        self.addCleanup(self.loader.stop)
        self.environment = patch.dict("os.environ", {
            "CONTEXT_INPUT_TOKENS": "16000", "CONTEXT_WINDOW_TOKENS": "128000",
            "MAX_MODEL_STEPS": "8", "MAX_TOOL_CALLS": "8", "SUMMARY_OUTPUT_TOKENS": "1500",
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)

    async def events(self, attachment_ids=None):
        result = await chat_response(self.request, self.db, self.provider, "owner", "conversation", "Olá", "request", attachment_ids)
        return [json.loads(chunk) async for chunk in result.body_iterator]

    async def test_regenerate_replays_the_last_user_prompt_without_duplicating_it(self):
        self.history = {"messages": [
            {"id": "user-1", "role": "user", "content": "Pergunta original", "status": "completed", "created_at": "t1"},
        ], "runs": [], "summaries": []}
        result = await chat_response(self.request, self.db, self.provider, "owner", "conversation", "", "request", regenerate=True)
        events = [json.loads(chunk) async for chunk in result.body_iterator]
        self.assertEqual(events[-1]["type"], "done")
        begin = self.db.rpc.await_args_list[0]
        self.assertEqual(begin.args[0], "begin_regenerate_run")
        self.assertNotIn("p_content", begin.kwargs)
        start = next(event for event in events if event["type"] == "start")
        self.assertEqual(start["user_message_id"], "user-1")
        prompt = self.provider.responses.create.await_args.kwargs["input"]
        self.assertIn("Pergunta original", str(prompt))
        self.assertEqual(str(prompt).count("Pergunta original"), 1)

    async def test_first_message_is_named_by_the_model(self):
        self.history = {"messages": [
            {"id": "user-message", "role": "user", "content": "Olá", "status": "completed", "created_at": "t1"},
            {"id": "assistant-message", "role": "assistant", "content": "", "status": "running", "created_at": "t1"},
        ], "runs": [], "summaries": []}
        async def create(**kwargs):
            if kwargs.get("stream"):
                return Stream(terminal())
            return response([message("Fotossintese explicada")], usage=(30, 6))
        self.provider.responses.create.side_effect = create
        events = await self.events()
        self.assertEqual(events[-1]["type"], "done")
        self.assertEqual(self.renames, ["Fotossintese explicada"])
        title_step = next(step for step in self.reservations if step["p_max_output_tokens"] == 200)
        self.assertEqual(title_step["p_kind"], "reply")
        completed = [record for record in self.records if record["p_input_tokens"] == 30]
        self.assertEqual(completed[0]["p_output_tokens"], 6)

    async def test_renamed_conversation_is_never_overwritten(self):
        self.history = {"messages": [
            {"id": "user-message", "role": "user", "content": "Olá", "status": "completed", "created_at": "t1"},
            {"id": "assistant-message", "role": "assistant", "content": "", "status": "running", "created_at": "t1"},
        ], "runs": [], "summaries": []}
        self.conversation_rows = [{"title": "Meu nome escolhido"}]
        events = await self.events()
        self.assertEqual(events[-1]["type"], "done")
        self.assertEqual(self.renames, [])
        self.assertEqual(len(self.reservations), 1)

    async def test_failed_naming_never_blocks_the_chat(self):
        self.history = {"messages": [
            {"id": "user-message", "role": "user", "content": "Olá", "status": "completed", "created_at": "t1"},
            {"id": "assistant-message", "role": "assistant", "content": "", "status": "running", "created_at": "t1"},
        ], "runs": [], "summaries": []}
        calls = []
        async def create(**kwargs):
            calls.append(kwargs)
            if not kwargs.get("stream"):
                raise ConnectionError("naming upstream failed")
            return Stream(terminal())
        self.provider.responses.create.side_effect = create
        events = await self.events()
        self.assertEqual(events[-1]["type"], "done")
        self.assertEqual(self.renames, [])
        self.assertEqual(self.records[0]["p_status"], "failed")
        self.assertEqual(self.finishes[-1]["p_input_tokens"], 11)
        self.assertEqual(self.finishes[-1]["p_output_tokens"], 7)

    async def test_partial_failure_is_never_completed_and_unknown_usage_remains_reserved(self):
        self.provider.responses.create.return_value = Stream(
            SimpleNamespace(type="response.output_text.delta", delta="Texto parcial"),
            ConnectionError("private upstream diagnostics"),
        )
        events = await self.events()
        self.assertEqual([event["type"] for event in events], ["start", "status", "delta", "error"])
        self.assertNotIn("private upstream diagnostics", str(events))
        self.assertEqual(self.finishes[-1]["p_status"], "failed")
        self.assertEqual(self.finishes[-1]["p_content"], "Texto parcial")
        self.assertIsNone(self.finishes[-1]["p_input_tokens"])
        self.assertIsNone(self.finishes[-1]["p_output_tokens"])
        self.assertEqual(self.records[-1]["p_status"], "failed")
        self.assertIsNone(self.records[-1]["p_input_tokens"])

    async def test_all_function_calls_and_native_output_are_replayed_statelessly(self):
        self.registry.definitions = [{"type": "web_search"}, {"type": "function", "name": "lookup", "parameters": {"type": "object"}}]
        calls = [
            {"type": "reasoning", "id": "reasoning", "summary": [], "encrypted_content": "opaque"},
            {"type": "web_search_call", "id": "native", "status": "completed", "action": {"type": "search", "query": "facts"}},
            {"type": "function_call", "id": "fn-1", "call_id": "call-1", "name": "lookup", "arguments": '{"x":1}'},
            {"type": "function_call", "id": "fn-2", "call_id": "call-2", "name": "lookup", "arguments": '{"x":2}'},
        ]
        self.provider.responses.create.side_effect = [Stream(terminal(response(calls))), Stream(terminal())]
        events = await self.events()
        self.assertEqual(events[-1]["type"], "done")
        self.assertEqual((events[-1]["input_tokens"], events[-1]["output_tokens"]), (22, 14))
        self.assertEqual(self.registry.execute.await_count, 2)
        self.registry.record_native.assert_awaited_once_with(calls[1])
        second = self.provider.responses.create.await_args_list[1].kwargs
        replay = second["input"]
        self.assertEqual(replay[1:5], calls)
        self.assertEqual([item["call_id"] for item in replay if item.get("type") == "function_call_output"], ["call-1", "call-2"])
        self.assertEqual(second["max_tool_calls"], 5)
        self.assertEqual(second["max_output_tokens"], 2041)
        self.assertFalse(second["store"])
        self.assertEqual(second["truncation"], "disabled")
        self.assertNotIn("previous_response_id", second)
        self.assertEqual([record["p_status"] for record in self.records], ["completed", "completed"])
        names = [call.args[0] for call in self.db.rpc.await_args_list]
        self.assertLess(names.index("reserve_model_step"), names.index("record_model_step"))

    async def test_tool_failure_feeds_the_error_and_the_turn_survives(self):
        self.registry.definitions = [{"type": "function", "name": "lookup", "parameters": {"type": "object"}}]
        self.registry.execute.side_effect = [RuntimeError("boom upstream"), {"facts": "second try"}]
        calls = [{"type": "function_call", "id": "fn-1", "call_id": "call-1", "name": "lookup", "arguments": '{"x":1}'},
                 {"type": "function_call", "id": "fn-2", "call_id": "call-2", "name": "lookup", "arguments": '{"x":2}'}]
        self.provider.responses.create.side_effect = [Stream(terminal(response(calls))), Stream(terminal())]
        events = await self.events()
        self.assertEqual(events[-1]["type"], "done")
        failed = [event for event in events if event["type"] == "tool"]
        self.assertEqual([event["status"] for event in failed], ["running", "failed", "running", "completed"])
        self.assertEqual(failed[1]["error"], "Não foi possível concluir a ferramenta.")
        second = self.provider.responses.create.await_args_list[1].kwargs
        replay = [item for item in second["input"] if item.get("type") == "function_call_output"]
        self.assertEqual(replay[0]["call_id"], "call-1")
        self.assertIn("Não foi possível concluir", replay[0]["output"])
        self.assertEqual([record["p_status"] for record in self.records], ["completed", "completed"])

    async def test_budget_denial_never_calls_model_or_tools(self):
        self.fail_reserve = True
        events = await self.events()
        self.assertEqual(events[-1]["type"], "error")
        self.provider.responses.create.assert_not_awaited()
        self.registry.execute.assert_not_awaited()

    async def test_later_budget_denial_does_not_start_another_generation_or_tool(self):
        call = {"type": "function_call", "id": "fn", "call_id": "call", "name": "lookup", "arguments": "{}"}
        self.provider.responses.create.return_value = Stream(terminal(response([call])))
        async def execute(*_args):
            self.fail_reserve = True
            return {"facts": "saved"}
        self.registry.execute.side_effect = execute
        events = await self.events()
        self.assertEqual(events[-1]["type"], "error")
        self.assertEqual(self.provider.responses.create.await_count, 1)
        self.assertEqual(self.registry.execute.await_count, 1)

    async def test_failed_terminal_retains_known_usage_without_success(self):
        self.provider.responses.create.return_value = Stream(terminal(response(usage=(5000, 9), status="failed"), "response.failed"))
        events = await self.events()
        self.assertEqual(events[-1]["type"], "error")
        self.assertEqual(self.finishes[-1]["p_status"], "failed")
        self.assertEqual(self.finishes[-1]["p_input_tokens"], 5000)
        self.assertEqual(self.records[-1]["p_input_tokens"], 5000)

    async def test_missing_usage_is_not_saved_as_completed(self):
        self.provider.responses.create.return_value = Stream(terminal(response(usage=None)))
        events = await self.events()
        self.assertEqual(events[-1]["type"], "error")
        self.assertIsNone(self.finishes[-1]["p_input_tokens"])
        self.assertEqual(self.finishes[-1]["p_status"], "failed")

    async def test_truncation_preserves_partial_text_and_never_executes_incomplete_calls(self):
        call = {"type": "function_call", "id": "fn", "call_id": "call", "name": "lookup", "arguments": '{"unfinished":'}
        self.provider.responses.create.return_value = Stream(
            SimpleNamespace(type="response.output_text.delta", delta="partial"),
            terminal(response([call], status="incomplete", reason="max_output_tokens"), "response.incomplete"),
        )
        events = await self.events()
        self.assertTrue(events[-1]["truncated"])
        self.assertTrue(self.finishes[-1]["p_content"].startswith("partial"))
        self.registry.execute.assert_not_awaited()

    async def test_persistence_failure_never_emits_done(self):
        self.fail_finish = True
        events = await self.events()
        self.assertEqual(events[-1]["type"], "error")
        self.assertNotIn("done", [event["type"] for event in events])
        self.assertIn("reserva permanece", events[-1]["error"])

    async def test_multimodal_count_uses_actual_parts_before_generation(self):
        image = {"type": "input_image", "image_url": "data:image/png;base64,real-bytes"}
        with patch("services.assets.model_parts", AsyncMock(return_value=[image])):
            events = await self.events(["image-id"])
        self.assertEqual(events[-1]["type"], "done")
        counted = self.provider.responses.input_tokens.count.await_args.kwargs
        generated = self.provider.responses.create.await_args.kwargs
        self.assertEqual(counted["input"], generated["input"])
        self.assertIn(image, counted["input"][-1]["content"])
        begin = self.db.rpc.await_args_list[0].kwargs
        self.assertEqual(begin["p_attachment_ids"], ["image-id"])
        self.assertEqual(begin["p_reserved_tokens"], 1)

    async def test_history_compaction_is_persisted_accounted_and_never_deletes_raw_messages(self):
        self.history = history_with_turns(4)
        raw = copy.deepcopy(self.history)
        async def create(**kwargs):
            return response([message("factual summary")]) if not kwargs["stream"] else Stream(terminal())
        self.provider.responses.create.side_effect = create
        with patch.dict("os.environ", {"CONTEXT_INPUT_TOKENS": "50"}):
            events = await self.events()
        self.assertEqual(events[-1]["type"], "done")
        self.assertEqual(self.history, raw)
        self.assertEqual([summary["p_through_message_id"] for summary in self.summaries], [f"assistant-{index}" for index in range(4)])
        self.assertEqual([step["p_kind"] for step in self.reservations], ["compaction"] * 4 + ["reply"])
        self.assertEqual((events[-1]["input_tokens"], events[-1]["output_tokens"]), (55, 35))
        self.assertEqual(len([event for event in events if event["type"] == "compaction"]), 4)
        self.assertEqual(self.finishes[-1]["p_input_tokens"], 55)
        final_prompt = self.provider.responses.create.await_args.kwargs["input"]
        self.assertIn("factual summary", str(final_prompt))
        self.assertNotIn("user text 0", str(final_prompt))

    async def test_loop_compaction_retains_complete_recent_call_result_pair(self):
        context = Context(self.history, "", "current", [])
        groups = [[
            {"type": "function_call", "id": f"fn-{index}", "call_id": f"call-{index}", "name": "lookup", "arguments": "{}"},
            {"type": "function_call_output", "call_id": f"call-{index}", "output": "facts"},
        ] for index in range(3)]
        context.loop_groups = copy.deepcopy(groups)
        self.provider.responses.create.return_value = response([message("loop summary")])
        with patch.dict("os.environ", {"CONTEXT_INPUT_TOKENS": "50"}):
            run = Run(self.request, self.db, self.provider, "owner", "conversation", "run")
            events = [event async for event in run.compact(context, [], 2048)]
        self.assertEqual(context.loop_groups, groups[-1:])
        self.assertEqual(context.loop_summary, "loop summary")
        self.assertEqual([summary["p_scope"] for summary in self.summaries], ["loop", "loop"])
        self.assertEqual(len([event for event in events if event["type"] == "compaction"]), 2)
        for call in self.provider.responses.create.await_args_list:
            payload = str(call.kwargs["input"])
            self.assertIn("function_call_output", payload)
            self.assertIn("function_call", payload)

    async def test_heartbeat_checkpoint_runs_during_nonstream_external_wait(self):
        run = Run(self.request, self.db, self.provider, "owner", "conversation", "run")
        run.checkpoint_at = monotonic() - 16
        async def external():
            await asyncio.sleep(0)
            return "complete"
        self.assertEqual(await run.wait(external()), "complete")
        checkpoints = [call for call in self.db.rpc.await_args_list if call.args[0] == "checkpoint_run"]
        self.assertEqual(len(checkpoints), 1)
        self.assertEqual(checkpoints[0].kwargs["p_content"], "")

    async def test_disconnect_after_delta_preserves_partial_content_and_unknown_usage(self):
        self.provider.responses.create.return_value = Stream(
            SimpleNamespace(type="response.output_text.delta", delta="saved partial"),
            terminal(),
        )
        result = await chat_response(self.request, self.db, self.provider, "owner", "conversation", "Olá", "request")
        while True:
            event = json.loads(await anext(result.body_iterator))
            if event["type"] == "delta":
                break
        await result.body_iterator.aclose()
        self.assertEqual(self.finishes[-1]["p_status"], "interrupted")
        self.assertEqual(self.finishes[-1]["p_content"], "saved partial")
        self.assertIsNone(self.finishes[-1]["p_input_tokens"])
        self.assertEqual(self.records[-1]["p_status"], "failed")

    async def test_current_file_above_soft_threshold_is_allowed_below_hard_window(self):
        self.provider.responses.input_tokens.count.return_value = SimpleNamespace(input_tokens=20000)
        image = {"type": "input_image", "image_url": "data:image/png;base64,actual"}
        with patch("services.assets.model_parts", AsyncMock(return_value=[image])):
            events = await self.events(["image-id"])
        self.assertEqual(events[-1]["type"], "done")
        self.assertEqual(self.reservations[0]["p_input_tokens"], 20000)
        self.assertEqual(self.summaries, [])

    async def test_current_file_above_hard_window_never_generates_or_drops_file(self):
        self.provider.responses.input_tokens.count.return_value = SimpleNamespace(input_tokens=128000)
        image = {"type": "input_image", "image_url": "data:image/png;base64,actual"}
        with patch("services.assets.model_parts", AsyncMock(return_value=[image])):
            events = await self.events(["image-id"])
        self.assertEqual(events[-1]["type"], "error")
        self.assertIn("arquivo atual", events[-1]["error"])
        self.provider.responses.create.assert_not_awaited()
        self.assertEqual(self.finishes[-1]["p_input_tokens"], 0)

    async def test_summary_usage_is_retained_when_following_reply_usage_is_unknown(self):
        self.history = history_with_turns(1)
        async def create(**kwargs):
            if not kwargs["stream"]:
                return response([message("facts")], usage=(300, 10))
            return Stream(ConnectionError("upstream"))
        self.provider.responses.create.side_effect = create
        with patch.dict("os.environ", {"CONTEXT_INPUT_TOKENS": "50"}):
            events = await self.events()
        self.assertEqual(events[-1]["type"], "error")
        self.assertEqual([record["p_status"] for record in self.records], ["completed", "failed"])
        self.assertEqual(self.records[0]["p_input_tokens"], 300)
        self.assertIsNone(self.finishes[-1]["p_input_tokens"])
        self.assertEqual(len(self.summaries), 1)

    async def test_model_step_bound_stops_a_tool_only_loop(self):
        call = {"type": "function_call", "id": "fn", "call_id": "call", "name": "lookup", "arguments": "{}"}
        self.provider.responses.create.return_value = Stream(terminal(response([call])))
        with patch.dict("os.environ", {"MAX_MODEL_STEPS": "1"}):
            events = await self.events()
        self.assertEqual(events[-1]["type"], "error")
        self.assertEqual(self.provider.responses.create.await_count, 1)
        self.assertEqual(self.registry.execute.await_count, 1)
        self.assertEqual(self.finishes[-1]["p_error_code"], "model_step_limit")


if __name__ == "__main__":
    unittest.main()
