export type StreamEvent =
  | { type: "start"; run_id: string; user_message_id: string; assistant_message_id: string }
  | { type: "delta"; text: string }
  | { type: "status"; stage: "compacting" | "thinking" | "tool"; text: string }
  | { type: "tool"; id: string; name: string; status: "running" | "completed" | "failed"; arguments?: Record<string, unknown>; result?: unknown; error?: string }
  | { type: "compaction"; summary_id: string; through_message_id?: string | null }
  | { type: "done"; run_id: string; status: "completed"; input_tokens: number; output_tokens: number; truncated?: boolean; followups?: string[] }
  | { type: "error"; run_id: string; status: "failed" | "interrupted"; error: string };

type TerminalEvent = Extract<StreamEvent, { type: "done" | "error" }>;

function parseEvent(line: string): StreamEvent {
  let event: Record<string, unknown>;
  try {
    const value: unknown = JSON.parse(line);
    if (!value || typeof value !== "object") throw new Error();
    event = value as Record<string, unknown>;
  } catch { throw new Error("A API enviou uma linha de transmissão inválida."); }
  const text = (key: string) => typeof event[key] === "string" && (event[key] as string).length > 0;
  const tokens = (key: string) => typeof event[key] === "number" && Number.isSafeInteger(event[key]) && (event[key] as number) >= 0;
  if (
    (event.type === "start" && text("run_id") && text("user_message_id") && text("assistant_message_id")) ||
    (event.type === "delta" && typeof event.text === "string") ||
    (event.type === "status" && ["compacting", "thinking", "tool"].includes(String(event.stage)) && text("text")) ||
    (event.type === "tool" && text("id") && text("name") && ["running", "completed", "failed"].includes(String(event.status)) && (event.arguments === undefined || (event.arguments !== null && typeof event.arguments === "object" && !Array.isArray(event.arguments))) && (event.error === undefined || typeof event.error === "string")) ||
    (event.type === "compaction" && text("summary_id") && (event.through_message_id === undefined || event.through_message_id === null || text("through_message_id"))) ||
    (event.type === "done" && text("run_id") && event.status === "completed" && tokens("input_tokens") && tokens("output_tokens") && (event.truncated === undefined || typeof event.truncated === "boolean") && (event.followups === undefined || (Array.isArray(event.followups) && event.followups.every((item) => typeof item === "string" && item.length > 0)))) ||
    (event.type === "error" && text("run_id") && ["failed", "interrupted"].includes(String(event.status)) && text("error"))
  ) return event as StreamEvent;
  throw new Error("A API enviou um evento de transmissão desconhecido ou incompleto.");
}

/** Consume bytes, not network chunks as lines: UTF-8 characters and JSON may span reads. */
export async function consumeStream(body: ReadableStream<Uint8Array>, onEvent: (event: StreamEvent) => void): Promise<TerminalEvent> {
  const reader = body.getReader();
  const decoder = new TextDecoder("utf-8", { fatal: true });
  let pending = "";
  let runId: string | undefined;
  let terminal: TerminalEvent | undefined;
  const deliver = (line: string) => {
    if (!line.trim()) return;
    if (terminal) throw new Error("A API enviou dados após encerrar a execução.");
    if (line.length > 1_048_576) throw new Error("A API enviou uma linha de transmissão grande demais.");
    const event = parseEvent(line);
    if (event.type === "start") {
      if (runId) throw new Error("A API iniciou a mesma transmissão mais de uma vez.");
      runId = event.run_id;
    } else {
      if (!runId) throw new Error("A API enviou dados antes de iniciar a execução.");
      if (event.type === "done" || event.type === "error") {
        if (event.run_id !== runId) throw new Error("A API retornou uma execução diferente da iniciada.");
        terminal = event;
      }
    }
    onEvent(event);
  };
  try {
    while (true) {
      const { value, done } = await reader.read();
      pending += done ? decoder.decode() : decoder.decode(value, { stream: true });
      let newline: number;
      while ((newline = pending.indexOf("\n")) >= 0) {
        deliver(pending.slice(0, newline));
        pending = pending.slice(newline + 1);
      }
      if (pending.length > 1_048_576) throw new Error("A API enviou uma linha de transmissão grande demais.");
      if (done) break;
    }
    if (pending.trim()) deliver(pending);
    if (!terminal) throw new Error("A conexão terminou sem confirmar a execução. O histórico será atualizado; não reenvie antes de conferir o resultado.");
    return terminal;
  } finally {
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}
