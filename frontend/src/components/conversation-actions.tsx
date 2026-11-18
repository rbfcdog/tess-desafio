"use client";

import { useState } from "react";
import type { Conversation, History } from "@/lib/chat-api";
import { serializeConversation, serializeConversationMarkdown } from "@/lib/conversations";

export function CopyMessage({ text, disabled = false }: { text: string; disabled?: boolean }) {
  const [copiedText, setCopiedText] = useState<string | null>(null);
  const [copying, setCopying] = useState(false);
  const [error, setError] = useState("");

  async function copy() {
    if (!text || disabled || copying) return;
    setCopying(true);
    setCopiedText(null);
    setError("");
    try {
      await navigator.clipboard.writeText(text);
      setCopiedText(text);
    } catch {
      setError("Cópia bloqueada. Copie o texto manualmente.");
    } finally { setCopying(false); }
  }

  return <>
    <button type="button" className="text-button copy-message" disabled={!text || disabled || copying} onClick={() => void copy()} aria-label="Copiar texto da mensagem">{copying ? "Copiando…" : copiedText === text ? "Copiado" : "Copiar"}</button>
    {copiedText === text && <span role="status" className="sr-only">Texto da mensagem copiado.</span>}
    {error && <span role="alert" className="action-error">{error}</span>}
  </>;
}

export function ExportConversation({ conversation, history, disabled }: { conversation: Conversation; history: History | null; disabled: boolean }) {
  const [error, setError] = useState("");

  function download(format: "json" | "markdown") {
    if (!history || disabled) return;
    setError("");
    let url = "";
    try {
      // ponytail: browser-only export; stream from the server if transcripts outgrow client memory.
      const body = format === "markdown"
        ? serializeConversationMarkdown(conversation, history)
        : serializeConversation(conversation, history);
      url = URL.createObjectURL(new Blob([body], { type: format === "markdown" ? "text/markdown;charset=utf-8" : "application/json;charset=utf-8" }));
      const link = document.createElement("a");
      link.href = url;
      link.download = `tess-conversa-${conversation.id}${format === "markdown" ? ".md" : ".json"}`;
      document.body.append(link);
      try { link.click(); } finally { link.remove(); }
      // Keep the Blob alive long enough for the browser to start the download.
      const downloadUrl = url;
      window.setTimeout(() => URL.revokeObjectURL(downloadUrl), 60_000);
    } catch {
      if (url) URL.revokeObjectURL(url);
      setError("Falha na exportação. Tente novamente.");
    }
  }

  return <>
    <button type="button" className="button secondary" disabled={disabled || !history} onClick={() => download("json")} title="JSON com mensagens e metadados, sem arquivos binários.">Exportar</button>
    <button type="button" className="button secondary" disabled={disabled || !history} onClick={() => download("markdown")} title="Markdown legível com o histórico e o consumo.">Exportar .md</button>
    {error && <span role="alert" className="action-error">{error}</span>}
  </>;
}
