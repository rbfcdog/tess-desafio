import assert from "node:assert/strict";
import test from "node:test";
import { consumeStream } from "./stream.ts";

const start = { type: "start", run_id: "run", user_message_id: "user", assistant_message_id: "assistant" };
const done = { type: "done", run_id: "run", status: "completed", input_tokens: 9, output_tokens: 3 };

function bytesStream(bytes, chunkSize = bytes.length) {
  return new ReadableStream({
    start(controller) {
      for (let i = 0; i < bytes.length; i += chunkSize) controller.enqueue(bytes.slice(i, i + chunkSize));
      controller.close();
    },
  });
}

function eventStream(events) {
  return bytesStream(new TextEncoder().encode(events.map((event) => JSON.stringify(event)).join("\n") + "\n"));
}

test("arbitrary byte boundaries preserve Portuguese text, emoji, CRLF, and final line", async () => {
  const events = [
    start,
    { type: "status", stage: "compacting", text: "Resumindo contexto…" },
    { type: "compaction", summary_id: "summary", through_message_id: null },
    { type: "status", stage: "tool", text: "Consultando fonte…" },
    { type: "tool", id: "tool", name: "read_webpage", status: "running", arguments: { url: "https://example.com" } },
    { type: "tool", id: "tool", name: "read_webpage", status: "completed", result: { text: "Informação 👋" } },
    { type: "status", stage: "thinking", text: "Preparando resposta…" },
    { type: "delta", text: "Olá 👋\nComo vai?" },
    done,
  ];
  const bytes = new TextEncoder().encode(events.map((event) => JSON.stringify(event)).join("\r\n"));
  for (const size of [1, 2, 7, bytes.length]) {
    const received = [];
    assert.deepEqual(await consumeStream(bytesStream(bytes, size), (event) => received.push(event)), done);
    assert.deepEqual(received, events);
  }
});

test("terminal errors retain server detail and are not reported as completion", async () => {
  const failure = { type: "error", run_id: "run", status: "interrupted", error: "O provedor interrompeu a resposta." };
  assert.deepEqual(await consumeStream(eventStream([start, { type: "delta", text: "Parcial" }, failure]), () => {}), failure);
});

test("disconnect without terminal confirmation fails instead of claiming saved completion", async () => {
  await assert.rejects(consumeStream(eventStream([start, { type: "delta", text: "Parcial" }]), () => {}), /sem confirmar/);
  await assert.rejects(consumeStream(eventStream([start, { type: "status", stage: "thinking", text: "Aguarde" }, { type: "compaction", summary_id: "summary" }]), () => {}), /sem confirmar/);
});

test("progress cannot precede start or arrive after terminal confirmation", async () => {
  for (const progress of [
    { type: "status", stage: "thinking", text: "Preparando resposta" },
    { type: "tool", id: "tool", name: "read_webpage", status: "failed", error: "Sem acesso" },
    { type: "compaction", summary_id: "summary" },
  ]) {
    await assert.rejects(consumeStream(eventStream([progress, start, done]), () => {}), /antes de iniciar/);
    await assert.rejects(consumeStream(eventStream([start, done, progress]), () => {}), /após encerrar/);
  }
});

test("oversized tool-result lines are rejected even when a network chunk includes the newline", async () => {
  const bytes = new TextEncoder().encode([start, { type: "tool", id: "tool", name: "read_webpage", status: "completed", result: "x".repeat(1_048_577) }, done].map((event) => JSON.stringify(event)).join("\n") + "\n");
  for (const size of [65_536, bytes.length]) await assert.rejects(consumeStream(bytesStream(bytes, size), () => {}), /grande demais/);
});

test("malformed or out-of-order events and wrong run identifiers fail visibly", async () => {
  for (const events of [
    [{ type: "delta", text: "antes" }],
    [start, start],
    [start, { ...done, run_id: "another" }],
    [start, { ...done, input_tokens: -1 }],
    [start, done, { type: "delta", text: "depois" }],
    [start, { type: "unknown" }],
    [start, { type: "status", stage: "unknown", text: "Aguarde" }],
    [start, { type: "tool", id: "tool", name: "read_webpage", status: "completed", arguments: [] }],
    [start, { type: "compaction", summary_id: "" }],
    [start, done, { type: "status", stage: "thinking", text: "depois" }],
  ]) await assert.rejects(consumeStream(eventStream(events), () => {}));
  await assert.rejects(consumeStream(bytesStream(new TextEncoder().encode("{broken}\n")), () => {}), /inválida/);
});

test("read failures retain partial deltas and cancel the reader", async () => {
  const received = [];
  let reads = 0;
  const body = new ReadableStream({
    pull(controller) {
      if (reads++ === 0) controller.enqueue(new TextEncoder().encode(`${JSON.stringify(start)}\n${JSON.stringify({ type: "delta", text: "Parcial" })}\n`));
      else controller.error(new Error("network lost"));
    },
  });
  await assert.rejects(consumeStream(body, (event) => received.push(event)), /network lost/);
  assert.equal(received.at(-1).text, "Parcial");
  assert.equal(body.locked, false);
});

test("done carries followup suggestions when present", async () => {
  const withFollowups = { ...done, followups: ["Como funciona a compactacao?", "Quanto custa em tokens?"] };
  const bytes = new TextEncoder().encode([start, withFollowups].map((event) => JSON.stringify(event)).join("\n") + "\n");
  const received = [];
  assert.deepEqual(await consumeStream(bytesStream(bytes), (event) => received.push(event)), withFollowups);
  await assert.rejects(consumeStream(eventStream([start, { ...done, followups: [""] }]), () => {}), /desconhecido/);
});
