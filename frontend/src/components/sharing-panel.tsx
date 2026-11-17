"use client";

import type { SupabaseClient } from "@supabase/supabase-js";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { apiJson, errorMessage, type Share } from "@/lib/chat-api";

const dates = new Intl.DateTimeFormat("pt-BR", { dateStyle: "short", timeStyle: "short" });

export function SharingPanel({ client, conversationId, disabled, onBusy }: { client: SupabaseClient; conversationId: string; disabled: boolean; onBusy: (value: boolean) => void }) {
  const [shares, setShares] = useState<Share[]>([]);
  const [loading, setLoading] = useState(true);
  const [working, setWorking] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [createdLink, setCreatedLink] = useState<{ id: string; url: string } | null>(null);
  const [days, setDays] = useState(7);
  const [accepted, setAccepted] = useState(false);
  const [reload, setReload] = useState(0);
  const lifetime = useRef<AbortController | null>(null);
  const lock = useRef(false);

  useEffect(() => {
    const controller = new AbortController();
    lifetime.current = controller;
    void apiJson<{ shares: Share[] }>(client, `/conversations/${conversationId}/shares`, controller.signal).then((result) => {
      if (!controller.signal.aborted) { setShares(result.shares); setError(""); }
    }).catch((error: unknown) => { if (!controller.signal.aborted) setError(errorMessage(error)); }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [client, conversationId, reload]);

  async function mutate(work: (signal: AbortSignal) => Promise<void>) {
    if (lock.current || disabled || loading || !lifetime.current) return;
    const signal = lifetime.current.signal;
    lock.current = true;
    setWorking(true);
    onBusy(true);
    setError("");
    setNotice("");
    try { await work(signal); }
    catch (error) { if (!signal.aborted) setError(errorMessage(error)); }
    finally { if (!signal.aborted) { lock.current = false; setWorking(false); onBusy(false); } }
  }

  function create(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!accepted) return;
    void mutate(async (signal) => {
      const result = await apiJson<{ share: Share; path: string }>(client, `/conversations/${conversationId}/shares`, signal, { expires_in_days: days });
      if (signal.aborted) return;
      const url = new URL(result.path, window.location.origin);
      if (url.origin !== window.location.origin || !url.pathname.startsWith("/share/")) throw new Error("Link inválido. Contate o administrador.");
      setShares((current) => [result.share, ...current]);
      setCreatedLink({ id: result.share.id, url: url.href });
      setAccepted(false);
      setNotice("Link criado.");
    });
  }

  async function copy() {
    const signal = lifetime.current?.signal;
    if (!createdLink || !signal || signal.aborted) return;
    try { await navigator.clipboard.writeText(createdLink.url); if (!signal.aborted) setNotice("Link copiado."); }
    catch { if (!signal.aborted) setError("Não foi possível copiar. Selecione e copie o link manualmente."); }
  }

  return <details className="settings sharing-settings"><summary>Compartilhar</summary><div className="feature-body">
    <p className="notice">Não inclui novas mensagens. Revogar não apaga cópias. O link não é recuperável.</p>
    {error && <p className="notice error" role="alert">{error}</p>}
    {notice && <p className="notice" role="status">{notice}</p>}
    <form onSubmit={create} className="form-stack"><label>Validade<select value={days} disabled={disabled || working} onChange={(event) => setDays(Number(event.target.value))}>{[1, 2, 3, 4, 5, 6, 7].map((day) => <option key={day} value={day}>{day} {day === 1 ? "dia" : "dias"}</option>)}</select></label><label className="checkbox-label"><input type="checkbox" required checked={accepted} disabled={disabled || working} onChange={(event) => setAccepted(event.target.checked)} />Qualquer pessoa com o link poderá ler mensagens, anexos e resultados de ferramentas, sem login.</label><button className="button primary self-start" type="submit" disabled={disabled || working || loading || !accepted}>Criar link</button></form>
    {createdLink && <div className="created-share"><label>Link<input readOnly value={createdLink.url} onFocus={(event) => event.target.select()} /></label><button type="button" className="button secondary" onClick={() => void copy()}>Copiar</button><a className="text-button" href={createdLink.url} target="_blank" rel="noreferrer">Abrir</a></div>}
    <div className="feature-actions"><h2>Links</h2><button type="button" className="text-button" disabled={disabled || working || loading} onClick={() => { setLoading(true); setReload((value) => value + 1); }}>Atualizar</button></div>
    {loading ? <p role="status">Carregando…</p> : shares.length === 0 ? <p className="muted text-sm">Nenhum link.</p> : <ul className="share-list">{shares.map((share) => <li key={share.id}><div><p>Criado <time dateTime={share.created_at}>{dates.format(new Date(share.created_at))}</time></p><p className="muted text-xs">{share.revoked_at ? `Revogado ${dates.format(new Date(share.revoked_at))}` : `Expira ${dates.format(new Date(share.expires_at))}`}</p></div>{!share.revoked_at && <button type="button" className="button secondary" disabled={disabled || working} onClick={() => {
      if (!window.confirm("Revogar o link e bloquear o acesso à conversa e aos arquivos?")) return;
      void mutate(async (signal) => {
        await apiJson(client, `/shares/${share.id}`, signal, undefined, "DELETE");
        if (signal.aborted) return;
        setShares((current) => current.filter((item) => item.id !== share.id));
        if (createdLink?.id === share.id) setCreatedLink(null);
        setNotice("Link revogado.");
        const result = await apiJson<{ shares: Share[] }>(client, `/conversations/${conversationId}/shares`, signal);
        if (signal.aborted) return;
        setShares(result.shares);
      });
    }}>Revogar</button>}</li>)}</ul>}
  </div></details>;
}
