import assert from "node:assert/strict";
import test from "node:test";
import { filterConversations, serializeConversation } from "./conversations.ts";

const conversations = [
  { id: "one", title: "Reunião da equipe" },
  { id: "two", title: "Orçamento" },
  { id: "three", title: "REUNIÃO com clientes" },
];

test("title search ignores accents/case, preserves order and distinguishes an empty query from no match", () => {
  assert.deepEqual(filterConversations(conversations, "  REUNIAO ").map((item) => item.id), ["one", "three"]);
  assert.deepEqual(filterConversations(conversations, "orça").map((item) => item.id), ["two"]);
  assert.deepEqual(filterConversations(conversations, "  ").map((item) => item.id), ["one", "two", "three"]);
  assert.deepEqual(filterConversations(conversations, "inexistente"), []);
});

test("transcript export retains message content but excludes unrelated private state and file bytes", () => {
  const text = 'Reunião: "amanhã"\n<script>não executar</script>';
  const serialized = serializeConversation({ id: "one", title: "Reunião", private_key: "ACCOUNT_SECRET" }, {
    system_prompt: "PRIVATE_INSTRUCTION",
    messages: [{
      id: "message", run_id: "run", role: "user", content: text, status: "completed", created_at: "2026-11-16T00:00:00Z",
      access_token: "SESSION_SECRET",
      attachments: [{ id: "file", filename: "documento.pdf", media_type: "application/pdf", size_bytes: 100, data: "BINARY_BYTES", private_url: "PRIVATE_DOWNLOAD" }],
    }],
    runs: [{ id: "run", status: "failed", model: "configured-model", input_tokens: null, output_tokens: null, error_code: "provider_failed", private_key: "PROVIDER_SECRET" }],
    tool_calls: [],
  });
  const transcript = JSON.parse(serialized);
  assert.equal(transcript.messages[0].content, text);
  assert.equal(transcript.runs[0].input_tokens, null);
  for (const secret of ["ACCOUNT_SECRET", "PRIVATE_INSTRUCTION", "SESSION_SECRET", "BINARY_BYTES", "PRIVATE_DOWNLOAD", "PROVIDER_SECRET"]) {
    assert.equal(serialized.includes(secret), false, secret);
  }
});
