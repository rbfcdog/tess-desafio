"use client";

import type { SupabaseClient } from "@supabase/supabase-js";
import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { apiFetch, apiJson, checkedResponse, errorMessage, type Attachment, type Conversation, type History, type Message, type ServerConfig, type Usage } from "@/lib/chat-api";
import { consumeStream } from "@/lib/stream";
import { AttachmentPicker, AttachmentView } from "@/components/attachments";
import { ToolsPanel } from "@/components/tools-panel";
import { SharingPanel } from "@/components/sharing-panel";
import { ExecutionTraces } from "@/components/execution-traces";
import { CopyMessage, ExportConversation } from "@/components/conversation-actions";
import { filterConversations } from "@/lib/conversations";
import { retainUnsentAttachments } from "@/lib/attachments";

const numbers = new Intl.NumberFormat("pt-BR");
const dates = new Intl.DateTimeFormat("pt-BR", { dateStyle: "short", timeStyle: "short" });
const statuses = { running: "Em execução", completed: "Concluída", failed: "Falhou", interrupted: "Interrompida" };

export function ChatWorkspace({ client, userId, email }: { client: SupabaseClient; userId: string; email: string }) {
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [conversationQuery, setConversationQuery] = useState("");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [history, setHistory] = useState<History | null>(null);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [needsSync, setNeedsSync] = useState(false);
  const [usage, setUsage] = useState<Usage | null>(null);
  const [config, setConfig] = useState<ServerConfig | null>(null);
  const [systemPrompt, setSystemPrompt] = useState("");
  const [savedPrompt, setSavedPrompt] = useState("");
  const [settingsReady, setSettingsReady] = useState(false);
  const [settingsNotice, setSettingsNotice] = useState("");
  const [draft, setDraft] = useState("");
  const [renameTitle, setRenameTitle] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [booting, setBooting] = useState(true);
  const [reload, setReload] = useState(0);
  const [historyReload, setHistoryReload] = useState(0);
  const [action, setAction] = useState<string | null>(null);
  const [streamPhase, setStreamPhase] = useState<"sending" | "streaming" | "syncing" | null>(null);
  const [streamNotice, setStreamNotice] = useState("");
  const [pendingAttachments, setPendingAttachments] = useState<Attachment[]>([]);
  const [attachmentBusy, setAttachmentBusy] = useState(false);
  const [toolsBusy, setToolsBusy] = useState(false);
  const [sharingBusy, setSharingBusy] = useState(false);
  const [progress, setProgress] = useState("");
  const lifetime = useRef<AbortController | null>(null);
  const streamController = useRef<AbortController | null>(null);
  const actionLock = useRef(false);
  const transcript = useRef<HTMLDivElement>(null);
  const followTranscript = useRef(true);
  const composer = useRef<HTMLTextAreaElement>(null);
  const selectionKey = `tess:conversation:${userId}`;
  const selected = conversations.find((conversation) => conversation.id === selectedId);
  const visibleConversations = useMemo(() => filterConversations(conversations, conversationQuery), [conversations, conversationQuery]);
  const busy = action !== null || streamPhase !== null || attachmentBusy || toolsBusy || sharingBusy;
  const running = !!history && (history.runs.some((run) => run.status === "running") || history.messages.some((message) => message.status === "running"));

  useEffect(() => {
    const controller = new AbortController();
    lifetime.current = controller;
    void (async () => {
      try {
        const response = await checkedResponse(await fetch("/api/config", { signal: controller.signal, cache: "no-store" }));
        const serverConfig = await response.json() as ServerConfig;
        if (controller.signal.aborted) return;
        setConfig(serverConfig);
        if (!serverConfig.auth_configured) throw new Error("Configure o Supabase no servidor.");
        const [list, account, settings] = await Promise.all([
          apiJson<{ conversations: Conversation[] }>(client, "/conversations", controller.signal),
          apiJson<Usage>(client, "/usage", controller.signal),
          apiJson<{ system_prompt: string }>(client, "/settings", controller.signal),
        ]);
        if (controller.signal.aborted) return;
        setConversations(list.conversations);
        setUsage(account);
        setSystemPrompt(settings.system_prompt);
        setSavedPrompt(settings.system_prompt);
        setSettingsReady(true);
        let previous: string | null = null;
        try { previous = localStorage.getItem(selectionKey); } catch { /* Storage may be unavailable in private browsing. */ }
        setHistory(null);
        setPendingAttachments([]);
        setHistoryLoading(list.conversations.length > 0);
        setSelectedId((current) => list.conversations.find((conversation) => conversation.id === (current ?? previous))?.id ?? list.conversations[0]?.id ?? null);
        setHistoryReload((value) => value + 1);
      } catch (error) {
        if (!controller.signal.aborted) setError(errorMessage(error));
      } finally { if (!controller.signal.aborted) setBooting(false); }
    })();
    return () => { controller.abort(); streamController.current?.abort(); };
  }, [client, selectionKey, reload]);

  useEffect(() => {
    if (!selectedId) return;
    try { localStorage.setItem(selectionKey, selectedId); } catch { /* History remains persistent on the server. */ }
    const controller = new AbortController();
    // Selection handlers clear the previous transcript before this request begins.
    followTranscript.current = true;
    void apiJson<History>(client, `/conversations/${selectedId}/messages`, controller.signal).then((result) => {
      if (!controller.signal.aborted) { setHistory(result); setNeedsSync(false); setPendingAttachments((current) => retainUnsentAttachments(current, result.messages)); }
    }).catch((error: unknown) => {
      if (!controller.signal.aborted) setError(errorMessage(error));
    }).finally(() => { if (!controller.signal.aborted) setHistoryLoading(false); });
    return () => controller.abort();
  }, [client, selectedId, selectionKey, historyReload]);

  // A disconnected worker can finish after the first refresh. Reads also recover stale runs server-side.
  useEffect(() => {
    if (!selectedId || (!running && !needsSync) || streamPhase || historyLoading || booting) return;
    const controller = new AbortController();
    let timeout: number;
    const poll = async () => {
      try {
        const [result, account] = await Promise.all([
          apiJson<History>(client, `/conversations/${selectedId}/messages`, controller.signal),
          apiJson<Usage>(client, "/usage", controller.signal),
        ]);
        if (!controller.signal.aborted) { setHistory(result); setUsage(account); setNeedsSync(false); setPendingAttachments((current) => retainUnsentAttachments(current, result.messages)); }
      } catch (error) {
        if (!controller.signal.aborted) setError(errorMessage(error));
      } finally { if (!controller.signal.aborted) timeout = window.setTimeout(poll, 5000); }
    };
    timeout = window.setTimeout(poll, 3000);
    return () => { controller.abort(); clearTimeout(timeout); };
  }, [client, selectedId, running, needsSync, streamPhase, historyLoading, booting]);

  useEffect(() => {
    if (followTranscript.current && transcript.current) transcript.current.scrollTop = transcript.current.scrollHeight;
  }, [history, streamPhase]);

  async function createConversation() {
    if (actionLock.current || !lifetime.current) return;
    const signal = lifetime.current.signal;
    actionLock.current = true;
    setAction("create");
    setError("");
    try {
      const { conversation } = await apiJson<{ conversation: Conversation }>(client, "/conversations", signal, {});
      if (signal.aborted) return;
      setConversations((current) => [conversation, ...current]);
      setConversationQuery("");
      setHistory(null);
      setHistoryLoading(true);
      setSelectedId(conversation.id);
      setDraft("");
      setPendingAttachments([]);
      setRenameTitle(null);
      setStreamNotice("");
      composer.current?.focus();
    } catch (error) { if (!signal.aborted) setError(errorMessage(error)); }
    finally { if (!signal.aborted) { actionLock.current = false; setAction(null); } }
  }

  async function renameConversation(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (actionLock.current || booting || !selectedId || !renameTitle?.trim() || !lifetime.current) return;
    const signal = lifetime.current.signal;
    actionLock.current = true;
    setAction("rename");
    setError("");
    try {
      const { conversation } = await apiJson<{ conversation: Conversation }>(client, `/conversations/${selectedId}`, signal, { title: renameTitle.trim() }, "PATCH");
      if (signal.aborted) return;
      setConversations((current) => current.map((item) => item.id === conversation.id ? conversation : item));
      setRenameTitle(null);
    } catch (error) { if (!signal.aborted) setError(errorMessage(error)); }
    finally { if (!signal.aborted) { actionLock.current = false; setAction(null); } }
  }

  async function saveSettings(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (actionLock.current || booting || !settingsReady || !lifetime.current) return;
    const signal = lifetime.current.signal;
    actionLock.current = true;
    setAction("settings");
    setError("");
    setSettingsNotice("");
    try {
      const settings = await apiJson<{ system_prompt: string }>(client, "/settings", signal, { system_prompt: systemPrompt }, "PATCH");
      if (signal.aborted) return;
      setSystemPrompt(settings.system_prompt);
      setSavedPrompt(settings.system_prompt);
      setSettingsNotice("Salvo.");
    } catch (error) { if (!signal.aborted) setError(errorMessage(error)); }
    finally { if (!signal.aborted) { actionLock.current = false; setAction(null); } }
  }

  async function signOut() {
    if (actionLock.current || !lifetime.current) return;
    const signal = lifetime.current.signal;
    actionLock.current = true;
    setAction("signout");
    setError("");
    try {
      const { error } = await client.auth.signOut({ scope: "local" });
      if (error) throw error;
    } catch (error) { if (!signal.aborted) setError(errorMessage(error)); }
    finally { if (!signal.aborted) { actionLock.current = false; setAction(null); } }
  }

  async function sendMessage(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const content = draft.trim();
    if (actionLock.current || busy || !selectedId || !history || (!content && !pendingAttachments.length) || content.length > 16000 || running || needsSync || booting || historyLoading || !config?.chat_configured || !lifetime.current) return;
    const conversationId = selectedId;
    const attachments = pendingAttachments;
    const signal = lifetime.current.signal;
    const controller = new AbortController();
    const abortStream = () => controller.abort();
    signal.addEventListener("abort", abortStream, { once: true });
    streamController.current = controller;
    actionLock.current = true;
    setStreamPhase("sending");
    setNeedsSync(true);
    setStreamNotice("");
    setProgress("");
    setError("");
    let assistantId: string | undefined;
    let runId: string | undefined;
    try {
      const response = await apiFetch(client, `/conversations/${conversationId}/messages`, controller.signal, { content, request_id: crypto.randomUUID(), attachment_ids: attachments.map((attachment) => attachment.id) });
      if (!response.headers.get("content-type")?.toLowerCase().includes("application/x-ndjson") || !response.body) {
        await response.body?.cancel();
        throw new Error("Resposta inválida. Atualize a conversa.");
      }
      await consumeStream(response.body, (event) => {
        if (signal.aborted) return;
        if (event.type === "start") {
          assistantId = event.assistant_message_id;
          runId = event.run_id;
          // Database timestamps arrive only in the authoritative history, not the start event.
          const messages: Message[] = [
            { id: event.user_message_id, role: "user", content, status: "completed", run_id: event.run_id, created_at: "", attachments },
            { id: event.assistant_message_id, role: "assistant", content: "", status: "running", run_id: event.run_id, created_at: "" },
          ];
          setHistory((current) => current ? { ...current, messages: [...current.messages, ...messages] } : current);
          setDraft("");
          setPendingAttachments([]);
          setStreamPhase("streaming");
        } else if (event.type === "delta") {
          setProgress("");
          setHistory((current) => current ? { ...current, messages: current.messages.map((message) => message.id === assistantId ? { ...message, content: message.content + event.text } : message) } : current);
        } else if (event.type === "status") {
          setProgress(event.text);
        } else if (event.type === "tool") {
          setHistory((current) => current ? { ...current, tool_calls: [...(current.tool_calls ?? []).filter((call) => call.id !== event.id), { ...(current.tool_calls ?? []).find((call) => call.id === event.id), ...event, run_id: runId }] } : current);
        } else if (event.type === "compaction") {
          setProgress("Resumindo contexto…");
          setHistory((current) => current ? { ...current, summaries: [...(current.summaries ?? []).filter((summary) => summary.id !== event.summary_id), { id: event.summary_id, through_message_id: event.through_message_id }] } : current);
        } else {
          setProgress("");
          if (event.type === "error") setError(event.error);
          else setStreamNotice(event.truncated ? "Resposta incompleta: limite de saída." : "");
          // Keep the assistant unconfirmed until the authoritative history is reloaded below.
          if (event.type === "error") setHistory((current) => current ? { ...current, messages: current.messages.map((message) => message.id === assistantId ? { ...message, status: event.status } : message) } : current);
        }
      });
    } catch (error) {
      if (!signal.aborted) {
        setStreamNotice("");
        setError(controller.signal.aborted ? "Interrompido. Atualizando…" : errorMessage(error));
      }
    } finally {
      signal.removeEventListener("abort", abortStream);
      streamController.current = null;
      if (!signal.aborted) {
        setStreamPhase("syncing");
        const results = await Promise.allSettled([
          apiJson<History>(client, `/conversations/${conversationId}/messages`, signal),
          apiJson<Usage>(client, "/usage", signal),
          apiJson<{ conversations: Conversation[] }>(client, "/conversations", signal),
        ]);
        if (!signal.aborted) {
          const [messagesResult, usageResult, conversationsResult] = results;
          if (messagesResult.status === "fulfilled") { setHistory(messagesResult.value); setNeedsSync(false); setPendingAttachments((current) => retainUnsentAttachments(current, messagesResult.value.messages)); }
          if (usageResult.status === "fulfilled") setUsage(usageResult.value);
          if (conversationsResult.status === "fulfilled") setConversations(conversationsResult.value.conversations);
          const failures = results.filter((result) => result.status === "rejected");
          if (failures.length) {
            setStreamNotice("");
            setError((current) => current || errorMessage(failures[0].reason));
          }
          actionLock.current = false;
          setStreamPhase(null);
          setProgress("");
          composer.current?.focus();
        }
      }
    }
  }

  const canSend = !busy && !booting && !historyLoading && !needsSync && !!selectedId && !!history && !running && !!config?.chat_configured && (!!draft.trim() || pendingAttachments.length > 0) && draft.trim().length <= 16000 && (usage === null || usage.remaining_tokens > 0);

  return <main className="workspace">
    <aside className="sidebar" aria-label="Navegação das conversas">
      <div className="brand">Tess</div>
      <details className="conversation-nav" open>
        <summary>Conversas</summary>
        <div className="conversation-nav-body">
          <button className="button primary w-full" type="button" onClick={createConversation} disabled={busy || booting || !config?.auth_configured}>{action === "create" ? "Criando…" : "Nova conversa"}</button>
          <label className="conversation-search"><span className="sr-only">Buscar conversas</span><input type="search" value={conversationQuery} maxLength={120} placeholder="Buscar" disabled={booting} aria-controls="conversation-list" onChange={(event) => setConversationQuery(event.target.value)} /></label>
          <nav id="conversation-list" aria-label="Histórico de conversas" className="conversation-list">
            {booting && <p role="status" className="muted text-sm">Carregando…</p>}
            {!booting && conversations.length > 0 && visibleConversations.length === 0 && <p role="status" className="muted text-sm">Sem resultados.</p>}
            {visibleConversations.map((conversation) => <button key={conversation.id} type="button" aria-current={selectedId === conversation.id ? "page" : undefined} disabled={busy || booting} onClick={() => {
              if (actionLock.current || selectedId === conversation.id) return;
              setHistory(null); setHistoryLoading(true); setSelectedId(conversation.id); setRenameTitle(null); setDraft(""); setPendingAttachments([]); setError(""); setStreamNotice(""); setProgress("");
            }}><span className="conversation-title" title={conversation.title}>{conversation.title}</span></button>)}
          </nav>
        </div>
      </details>
      <details className="usage-panel">
        <summary>Saldo <span>{usage ? `${numbers.format(usage.remaining_tokens)} tokens` : "—"}</span></summary>
        {usage && <dl className="usage-details"><div><dt>Usados</dt><dd>{numbers.format(usage.used_tokens)}</dd></div><div><dt>Reservados</dt><dd>{numbers.format(usage.reserved_tokens)}</dd></div><div><dt>Limite global</dt><dd>{numbers.format(usage.limit_tokens)}</dd></div><div><dt>Seu uso</dt><dd>{numbers.format(usage.user_tokens)}</dd></div></dl>}
      </details>
      <footer className="account"><span title={email}>{email}</span><button type="button" className="text-button" onClick={signOut} disabled={busy}>{action === "signout" ? "Saindo…" : "Sair"}</button></footer>
    </aside>

    <section className="chat-pane" aria-labelledby="conversation-heading">
      <header className="chat-header"><div className="min-w-0"><h1 id="conversation-heading">{selected?.title ?? "Conversas"}</h1></div>
        <div className="header-actions"><button className="button secondary" type="button" disabled={busy || booting} onClick={() => { setBooting(true); setError(""); setReload((value) => value + 1); setStreamNotice(""); }}>Atualizar</button>
          {selected && <ExportConversation key={selected.id} conversation={selected} history={history} disabled={busy || booting || historyLoading || running || needsSync} />}
          {selected && <button className="button secondary" type="button" disabled={busy || booting} onClick={() => setRenameTitle(selected.title)}>Renomear</button>}
        </div>
      </header>
      {renameTitle !== null && <form className="rename-form" onSubmit={renameConversation}><label htmlFor="conversation-title">Nome<input id="conversation-title" autoFocus maxLength={120} required value={renameTitle} disabled={busy || booting} onChange={(event) => setRenameTitle(event.target.value)} /></label><button className="button primary" type="submit" disabled={busy || booting || !renameTitle.trim()}>Salvar</button><button className="button secondary" type="button" disabled={busy} onClick={() => setRenameTitle(null)}>Cancelar</button></form>}
      <div className="chat-controls">
      <details className="settings"><summary>Instruções</summary>
        <form onSubmit={saveSettings} className="form-stack"><label htmlFor="system-prompt">Instruções<textarea id="system-prompt" rows={4} maxLength={8000} value={systemPrompt} disabled={busy || booting || !settingsReady} onChange={(event) => { setSystemPrompt(event.target.value); setSettingsNotice(""); }} /></label><button className="button secondary self-start" type="submit" disabled={busy || booting || !settingsReady || systemPrompt === savedPrompt}>{action === "settings" ? "Salvando…" : "Salvar"}</button>{settingsNotice && <p role="status" className="notice">{settingsNotice}</p>}</form>
      </details>
      <ToolsPanel client={client} disabled={busy || booting} onBusy={setToolsBusy} />
      {selectedId && <SharingPanel key={selectedId} client={client} conversationId={selectedId} disabled={busy || booting || historyLoading || running || needsSync} onBusy={setSharingBusy} />}
      </div>
      <div className="workspace-notices">
        {error && <div role="alert" className="notice error"><p className="whitespace-pre-wrap">{error}</p><button type="button" className="text-button" onClick={() => setError("")}>Fechar</button></div>}
        {config && !config.chat_configured && <p role="status" className="notice">Configure a OpenAI no servidor.</p>}
        {usage?.remaining_tokens === 0 && <p role="status" className="notice">Sem tokens disponíveis.</p>}
        {streamNotice && <p role="status" className="notice">{streamNotice}</p>}
      </div>
      <div className="transcript" ref={transcript} onScroll={() => {
        const element = transcript.current;
        if (element) followTranscript.current = element.scrollHeight - element.scrollTop - element.clientHeight < 100;
      }} role="region" tabIndex={0} aria-label="Mensagens da conversa" aria-busy={historyLoading}>
        {historyLoading ? <p role="status" className="empty-state">Carregando…</p> : !selectedId ? null : !history ? <div className="empty-state"><p>Histórico indisponível.</p><button type="button" className="button secondary" disabled={busy} onClick={() => { setHistoryLoading(true); setHistoryReload((value) => value + 1); }}>Tentar novamente</button></div> : history.messages.length === 0 ? null : <ol className="message-list">{history.messages.map((message) => {
          const run = history.runs.find((item) => item.id === message.run_id);
          return <li key={message.id} className={`message ${message.role}`}><article aria-label={message.role === "user" ? "Sua mensagem" : "Resposta do assistente"}>
            <div className="message-heading"><h2 title={message.created_at ? dates.format(new Date(message.created_at)) : undefined}>{message.role === "user" ? "Você" : "Assistente"}</h2>{message.content && <CopyMessage text={message.content} disabled={message.status === "running"} />}</div>
            <div className="message-content">{message.content}</div>
            {!!message.attachments?.length && <div className="message-attachments">{message.attachments.map((attachment) => <AttachmentView key={attachment.id} attachment={attachment} client={client} />)}</div>}
            {message.role === "assistant" && <details className="run-details">
              <summary>{message.status === "completed" ? "Detalhes" : message.status === "running" && streamPhase === "syncing" ? "Confirmando…" : statuses[message.status]}{message.status !== "running" && (!run || run.input_tokens === null || run.output_tokens === null) ? " · uso desconhecido" : ""}</summary>
              <footer className={`execution ${message.status}`}><span>{statuses[message.status]}</span>{run && <><span>{run.model}</span>{run.input_tokens !== null && run.output_tokens !== null && <span>{numbers.format(run.input_tokens)} entrada · {numbers.format(run.output_tokens)} saída</span>}{run.error_code && <span>{run.error_code}</span>}</>}</footer>
              <ExecutionTraces calls={(history.tool_calls ?? []).filter((call) => call.run_id === message.run_id)} />
              {!!history.model_steps?.some((step) => step.run_id === message.run_id) && <details className="model-steps"><summary>Uso por etapa</summary><ul>{history.model_steps.filter((step) => step.run_id === message.run_id).map((step) => <li key={step.id}><strong>{step.kind === "compaction" ? "Resumo" : "Resposta"}</strong> · {statuses[step.status]} · {step.input_tokens !== null && step.output_tokens !== null ? `${numbers.format(step.input_tokens)} entrada · ${numbers.format(step.output_tokens)} saída` : `${numbers.format(step.reserved_tokens)} reservados; uso desconhecido`}</li>)}</ul></details>}
            </details>}
          </article></li>;
        })}</ol>}
      </div>
      <div className="composer-area">
        {running && !streamPhase && <p role="status" className="notice">Execução em andamento.</p>}
        {needsSync && !streamPhase && !running && <p role="status" className="notice">Sincronizando…</p>}
        <p role="status" aria-live="polite" className="stream-status">{streamPhase === "syncing" ? "Sincronizando…" : progress || (streamPhase === "sending" ? "Enviando…" : streamPhase === "streaming" ? "Respondendo…" : "")}</p>
        <form className="composer" onSubmit={sendMessage}><label className="sr-only" htmlFor="message">Sua mensagem</label><textarea ref={composer} id="message" rows={3} maxLength={16000} placeholder="Mensagem" value={draft} disabled={busy || booting || needsSync || !selectedId || !history || historyLoading || running || !config?.chat_configured} onChange={(event) => setDraft(event.target.value)} aria-keyshortcuts="Control+Enter Meta+Enter" onKeyDown={(event) => {
          if (event.key === "Enter" && (event.ctrlKey || event.metaKey) && !event.nativeEvent.isComposing) {
            event.preventDefault();
            if (canSend) event.currentTarget.form?.requestSubmit();
          }
        }} />
          <div className="composer-bottom">{draft.length > 14000 && <p>{numbers.format(draft.length)}/16.000</p>}{streamPhase === "sending" || streamPhase === "streaming" ? <button type="button" className="button secondary" onClick={() => streamController.current?.abort()}>Parar</button> : <button type="submit" className="button primary" disabled={!canSend} title="Ctrl/Cmd+Enter">Enviar</button>}</div>
        </form>
        {selectedId && history && !historyLoading && <details className="composer-attachments"><summary>Anexos{pendingAttachments.length > 0 && <span className="font-mono">{pendingAttachments.length}/5</span>}</summary><AttachmentPicker key={selectedId} client={client} conversationId={selectedId} usedIds={history.messages.flatMap((message) => (message.attachments ?? []).map((attachment) => attachment.id))} selected={pendingAttachments} onChange={setPendingAttachments} disabled={busy || booting || running || needsSync} onBusy={setAttachmentBusy} /></details>}
      </div>
    </section>
  </main>;
}
