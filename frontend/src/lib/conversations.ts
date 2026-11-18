import type { Conversation, History } from "./chat-api";

const searchable = (text: string) => text.normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLocaleLowerCase("pt-BR");

export function filterConversations(conversations: Conversation[], query: string): Conversation[] {
  const term = searchable(query.trim());
  return term ? conversations.filter((conversation) => searchable(conversation.title).includes(term)) : conversations;
}

export function serializeConversation(conversation: Conversation, history: History): string {
  // Export only transcript fields, never unrelated account/settings state from API responses.
  return JSON.stringify({
    conversation_id: conversation.id,
    title: conversation.title,
    exported_at: new Date().toISOString(),
    messages: history.messages.map((message) => ({
      id: message.id, run_id: message.run_id, role: message.role, content: message.content,
      status: message.status, created_at: message.created_at,
      attachments: (message.attachments ?? []).map((attachment) => ({
        id: attachment.id, filename: attachment.filename, media_type: attachment.media_type,
        size_bytes: attachment.size_bytes, page_count: attachment.page_count ?? null,
      })),
    })),
    runs: history.runs.map((run) => ({
      id: run.id, status: run.status, model: run.model,
      input_tokens: run.input_tokens, output_tokens: run.output_tokens, error_code: run.error_code,
      created_at: run.created_at, finished_at: run.finished_at,
    })),
    tool_calls: (history.tool_calls ?? []).map((call) => ({
      id: call.id, run_id: call.run_id, name: call.name, status: call.status,
      arguments: call.arguments, result: call.result, error: call.error,
    })),
  }, null, 2);
}

export function serializeConversationMarkdown(conversation: Conversation, history: History): string {
  const heading = (role: string) => role === "user" ? "Você" : "Assistente";
  const lines = [`# ${conversation.title}`, "", `_Exportado em ${new Date().toLocaleString("pt-BR")}_`, ""];
  for (const message of history.messages) {
    lines.push(`## ${heading(message.role)}`, "");
    lines.push(message.content || "_(sem texto)_", "");
    for (const attachment of message.attachments ?? []) {
      lines.push(`- Anexo: ${attachment.filename} (${Math.max(1, Math.round(attachment.size_bytes / 1024))} KB)`);
    }
    if (message.attachments?.length) lines.push("");
  }
  const runs = history.runs.filter((run) => run.input_tokens !== null && run.output_tokens !== null);
  if (runs.length) {
    lines.push("## Consumo", "");
    for (const run of runs) {
      lines.push(`- ${run.model}: ${run.input_tokens} entrada · ${run.output_tokens} saída`);
    }
  }
  return lines.join("\n") + "\n";
}
