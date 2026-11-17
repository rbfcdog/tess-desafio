"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { ApiError, checkedResponse, errorMessage, type SharedSnapshot } from "@/lib/chat-api";
import { AttachmentView } from "@/components/attachments";
import { ExecutionTraces } from "@/components/execution-traces";
import { CopyMessage } from "@/components/conversation-actions";

const dates = new Intl.DateTimeFormat("pt-BR", { dateStyle: "short", timeStyle: "short" });
const statuses = { running: "Em execução ao compartilhar", completed: "Concluída", failed: "Falhou", interrupted: "Interrompida" };

export function SharedConversation({ token }: { token: string }) {
  const [snapshot, setSnapshot] = useState<SharedSnapshot | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [reload, setReload] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    let inFlight = false;
    const refresh = async () => {
      if (inFlight) return;
      inFlight = true;
      try {
        const response = await checkedResponse(await fetch(`/api/shared/${encodeURIComponent(token)}`, { cache: "no-store", signal: controller.signal, referrerPolicy: "no-referrer" }));
        const data = await response.json() as SharedSnapshot;
        if (!controller.signal.aborted) { setSnapshot(data); setError(""); }
      } catch (error) {
        if (!controller.signal.aborted) {
          if (error instanceof ApiError && error.status === 404) {
            setSnapshot(null);
            setError("Link inválido, expirado ou revogado. Peça um novo link.");
          } else setError(errorMessage(error));
        }
      } finally { inFlight = false; if (!controller.signal.aborted) setLoading(false); }
    };
    void refresh();
    const timer = window.setInterval(() => void refresh(), 60_000);
    const onFocus = () => void refresh();
    window.addEventListener("focus", onFocus);
    return () => { controller.abort(); clearInterval(timer); window.removeEventListener("focus", onFocus); };
  }, [token, reload]);

  return <main className="shared-shell"><header className="shared-header"><Link href="/" className="brand" aria-label="Abrir Tess">Tess</Link><span className="tool-kind">Somente leitura</span></header>
    {error && <div className="notice error"><p role="alert">{error}</p><button className="text-button" type="button" onClick={() => { setLoading(true); setReload((value) => value + 1); }}>Tentar novamente</button></div>}
    {loading && !snapshot && <p className="empty-state" role="status">Carregando…</p>}
    {snapshot && <><section className="shared-intro"><h1>{snapshot.title}</h1><p className="muted">Expira em <time dateTime={snapshot.expires_at}>{dates.format(new Date(snapshot.expires_at))}</time></p></section>
      {snapshot.messages.length === 0 && <p className="empty-state">Sem mensagens.</p>}
      <ol className="message-list">{snapshot.messages.map((message) => <li key={message.id} className={`message ${message.role}`}><article><div className="message-heading"><h2 title={message.created_at ? dates.format(new Date(message.created_at)) : undefined}>{message.role === "user" ? "Usuário" : "Assistente"}</h2>{message.content && <CopyMessage text={message.content} disabled={message.status === "running"} />}</div><div className="message-content">{message.content}</div>{!!message.attachments?.length && <div className="message-attachments">{message.attachments.map((attachment) => <AttachmentView key={attachment.id} attachment={attachment} shareToken={token} />)}</div>}{message.role === "assistant" && <details className="run-details"><summary>{message.status === "completed" ? "Detalhes" : statuses[message.status]}</summary><footer className={`execution ${message.status}`}>{statuses[message.status]}</footer><ExecutionTraces calls={snapshot.tool_calls.filter((call) => !!message.run_id && call.run_id === message.run_id)} /></details>}</article></li>)}</ol>
      {!!snapshot.tool_calls.filter((call) => !snapshot.messages.some((message) => message.role === "assistant" && message.run_id && message.run_id === call.run_id)).length && <ExecutionTraces calls={snapshot.tool_calls.filter((call) => !snapshot.messages.some((message) => message.role === "assistant" && message.run_id && message.run_id === call.run_id))} />}
    </>}
  </main>;
}
