"use client";

import type { SupabaseClient } from "@supabase/supabase-js";
import { useEffect, useRef, useState } from "react";
import { apiFetch, apiJson, checkedResponse, errorMessage, type Attachment } from "@/lib/chat-api";
import { attachmentTypes, validateUploads } from "@/lib/attachments";

export function AttachmentView({ attachment, client, shareToken }: { attachment: Attachment; client?: SupabaseClient; shareToken?: string }) {
  const [url, setUrl] = useState("");
  const [error, setError] = useState("");
  const [retry, setRetry] = useState(0);
  const image = attachment.media_type.startsWith("image/");
  useEffect(() => {
    const controller = new AbortController();
    let objectUrl = "";
    void (async () => {
      try {
        const path = shareToken ? `/shared/${encodeURIComponent(shareToken)}/attachments/${attachment.id}` : `/attachments/${attachment.id}`;
        const response = client
          ? await apiFetch(client, path, controller.signal)
          : await checkedResponse(await fetch(`/api${path}`, { signal: controller.signal, cache: "no-store", referrerPolicy: "no-referrer" }));
        const blob = await response.blob();
        if (controller.signal.aborted) return;
        objectUrl = URL.createObjectURL(blob);
        setUrl(objectUrl);
      } catch (error) { if (!controller.signal.aborted) setError(errorMessage(error)); }
    })();
    return () => { controller.abort(); if (objectUrl) URL.revokeObjectURL(objectUrl); };
  }, [attachment.id, client, shareToken, retry]);

  return <div className="attachment-card">
    {/* Authenticated object URLs are intentionally not routed through the image optimizer. */}
    {/* eslint-disable-next-line @next/next/no-img-element */}
    {image && url && <a href={url} target="_blank" rel="noreferrer" aria-label={`Ampliar ${attachment.filename}`}><img className="attachment-preview" src={url} alt={attachment.filename} /></a>}
    <div className="attachment-meta">{!image && url ? <a className="text-button" href={url} target="_blank" rel="noreferrer" aria-label={`Ver PDF ${attachment.filename} em nova aba`}>{attachment.filename}</a> : <strong>{attachment.filename}</strong>}<span>{new Intl.NumberFormat("pt-BR", { maximumFractionDigits: 1 }).format(attachment.size_bytes / 1024)} KB{attachment.page_count ? ` · ${attachment.page_count} pág.` : ""}</span></div>
    {error ? <div><p role="alert" className="error-text">{error}</p><button type="button" className="text-button" aria-label={`Recarregar ${attachment.filename}`} onClick={() => { setError(""); setUrl(""); setRetry((value) => value + 1); }}>Recarregar</button></div> : url ? <a className="text-button" href={url} download={attachment.filename} aria-label={`Baixar ${attachment.filename}`}>Baixar</a> : <span role="status" className="muted text-xs">Carregando…</span>}
  </div>;
}

export function AttachmentPicker({ client, conversationId, usedIds, selected, onChange, disabled, onBusy }: { client: SupabaseClient; conversationId: string; usedIds: string[]; selected: Attachment[]; onChange: (attachments: Attachment[]) => void; disabled: boolean; onBusy: (busy: boolean) => void }) {
  const [uploads, setUploads] = useState<Attachment[]>([]);
  const [loading, setLoading] = useState(true);
  const [working, setWorking] = useState(false);
  const [error, setError] = useState("");
  const [reload, setReload] = useState(0);
  const lifetime = useRef<AbortController | null>(null);
  const lock = useRef(false);
  useEffect(() => {
    const controller = new AbortController();
    lifetime.current = controller;
    void apiJson<{ attachments: Attachment[] }>(client, `/conversations/${conversationId}/attachments`, controller.signal).then((data) => {
      if (!controller.signal.aborted) { setUploads(data.attachments); setError(""); }
    }).catch((error: unknown) => { if (!controller.signal.aborted) setError(errorMessage(error)); }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [client, conversationId, reload]);

  async function upload(files: File[]) {
    if (!files.length || lock.current || disabled || loading || !lifetime.current) return;
    const validation = validateUploads(files, selected.length);
    if (validation) { setError(validation); return; }
    const signal = lifetime.current.signal;
    lock.current = true;
    setWorking(true);
    onBusy(true);
    setError("");
    const added: Attachment[] = [];
    try {
      for (const file of files) {
        const data = new FormData();
        data.append("file", file);
        const { attachment } = await apiJson<{ attachment: Attachment }>(client, `/conversations/${conversationId}/attachments`, signal, data);
        if (signal.aborted) return;
        added.push(attachment);
        setUploads((current) => [...current, attachment]);
      }
    } catch (error) { if (!signal.aborted) setError(errorMessage(error)); }
    finally {
      if (!signal.aborted) {
        onChange([...selected, ...added]);
        lock.current = false;
        setWorking(false);
        onBusy(false);
      }
    }
  }

  async function remove(attachment: Attachment) {
    if (lock.current || disabled || loading || !lifetime.current) return;
    const signal = lifetime.current.signal;
    lock.current = true;
    setWorking(true);
    onBusy(true);
    setError("");
    try {
      await apiJson(client, `/attachments/${attachment.id}`, signal, undefined, "DELETE");
      if (signal.aborted) return;
      setUploads((current) => current.filter((item) => item.id !== attachment.id));
      onChange(selected.filter((item) => item.id !== attachment.id));
    } catch (error) { if (!signal.aborted) setError(errorMessage(error)); }
    finally { if (!signal.aborted) { lock.current = false; setWorking(false); onBusy(false); } }
  }

  const pending = uploads.filter((attachment) => !usedIds.includes(attachment.id));
  return <section className="attachment-picker" aria-label="Arquivos da próxima mensagem" aria-busy={working || loading}>
    <label className="file-label button secondary">Anexar<input className="sr-only" type="file" accept={attachmentTypes.join(",")} multiple disabled={disabled || working || loading} onChange={(event) => { const files = Array.from(event.target.files ?? []); event.target.value = ""; void upload(files); }} /></label>
    <p className="muted text-xs">Imagens/PDF · até 5 por mensagem · 5 MB/arquivo · 20 páginas/PDF · 50 MB/conta</p>
    {working && <p role="status" className="muted text-xs">Salvando…</p>}
    {error && <div className="notice error"><p role="alert">{error}</p><button type="button" className="text-button" disabled={working || disabled || loading} onClick={() => { setLoading(true); setReload((value) => value + 1); }}>Atualizar arquivos</button></div>}
    {pending.length > 0 && <ul className="pending-files">{pending.map((attachment) => <li key={attachment.id}><label className="checkbox-label"><input type="checkbox" aria-label={`Selecionar ${attachment.filename} para o próximo envio`} checked={selected.some((item) => item.id === attachment.id)} disabled={disabled || working} onChange={(event) => {
      if (event.target.checked && selected.length >= 5) { setError("Máximo de 5 arquivos por mensagem."); return; }
      onChange(event.target.checked ? [...selected, attachment] : selected.filter((item) => item.id !== attachment.id));
    }} />Selecionar</label><AttachmentView attachment={attachment} client={client} /><button type="button" className="text-button" aria-label={`Excluir ${attachment.filename}`} disabled={disabled || working} onClick={() => void remove(attachment)}>Excluir</button></li>)}</ul>}
  </section>;
}
