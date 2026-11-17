"use client";

import type { SupabaseClient } from "@supabase/supabase-js";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { apiJson, errorMessage, type GoogleStatus, type ToolConfig, type ToolDefinition } from "@/lib/chat-api";

const emptySchema = JSON.stringify({ type: "object", properties: {}, additionalProperties: false }, null, 2);
const emptyForm = { id: "", name: "", description: "", kind: "http" as "http" | "mcp", url: "", schema: emptySchema, secret: "", replaceSecret: false, enabled: false, allowed: [] as string[], discovered: [] as ToolDefinition[] };

export function ToolsPanel({ client, disabled, onBusy }: { client: SupabaseClient; disabled: boolean; onBusy: (value: boolean) => void }) {
  const [tools, setTools] = useState<ToolConfig[]>([]);
  const [google, setGoogle] = useState<GoogleStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [working, setWorking] = useState(false);
  const [reload, setReload] = useState(0);
  const [editing, setEditing] = useState(false);
  const [form, setForm] = useState(emptyForm);
  const lifetime = useRef<AbortController | null>(null);
  const lock = useRef(false);
  const panel = useRef<HTMLDetailsElement>(null);

  useEffect(() => {
    const controller = new AbortController();
    lifetime.current = controller;
    void Promise.allSettled([
      apiJson<{ tools: ToolConfig[] }>(client, "/tools", controller.signal),
      apiJson<{ google: GoogleStatus }>(client, "/connectors", controller.signal),
    ]).then(([toolResult, connectorResult]) => {
      if (controller.signal.aborted) return;
      if (toolResult.status === "fulfilled") setTools(toolResult.value.tools);
      if (connectorResult.status === "fulfilled") setGoogle(connectorResult.value.google);
      const errors = [toolResult, connectorResult].filter((result) => result.status === "rejected").map((result) => errorMessage(result.reason));
      const url = new URL(window.location.href);
      if (url.searchParams.get("connector") === "google") {
        if (panel.current) panel.current.open = true;
        if (url.searchParams.get("status") === "connected") setNotice("Google conectado.");
        else if (url.searchParams.get("status") === "error") errors.push(url.searchParams.get("reason") === "access_denied" ? "Conexão não autorizada. Reconecte o Google para revisar as permissões." : "Falha na conexão Google. Confira a configuração com o administrador e tente conectar novamente.");
        url.searchParams.delete("connector");
        url.searchParams.delete("status");
        url.searchParams.delete("reason");
        window.history.replaceState(window.history.state, "", `${url.pathname}${url.search}${url.hash}`);
      }
      setError(errors.join("\n"));
      setLoading(false);
    });
    return () => controller.abort();
  }, [client, reload]);

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

  function edit(tool: ToolConfig) {
    if (tool.kind === "builtin") return;
    setForm({ id: tool.id, name: tool.name, description: tool.description, kind: tool.kind, url: tool.config.url ?? "", schema: JSON.stringify(tool.config.input_schema ?? JSON.parse(emptySchema), null, 2), secret: "", replaceSecret: false, enabled: tool.enabled, allowed: tool.config.allowed_tools ?? [], discovered: tool.config.discovered_tools ?? [] });
    setEditing(true);
    setNotice("");
  }

  function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    void mutate(async (signal) => {
      let inputSchema: Record<string, unknown> | undefined;
      if (form.kind === "http") {
        try {
          const value: unknown = JSON.parse(form.schema);
          if (!value || typeof value !== "object" || Array.isArray(value) || !("type" in value) || value.type !== "object") throw new Error();
          inputSchema = value as Record<string, unknown>;
        } catch { throw new Error('Use JSON válido com "type": "object".'); }
      }
      const body = { name: form.name.trim(), description: form.description.trim(), kind: form.kind, enabled: form.enabled, config: form.kind === "http" ? { url: form.url.trim(), input_schema: inputSchema } : { url: form.url.trim(), allowed_tools: form.allowed }, ...(form.replaceSecret ? { secret_token: form.secret } : {}) };
      const { tool } = await apiJson<{ tool: ToolConfig }>(client, form.id ? `/tools/${form.id}` : "/tools", signal, body, form.id ? "PATCH" : "POST");
      if (signal.aborted) return;
      setTools((current) => form.id ? current.map((item) => item.id === tool.id ? tool : item) : [...current, tool]);
      setForm(emptyForm);
      setEditing(false);
      setNotice(tool.kind === "mcp" ? "Servidor salvo. Descubra, permita e salve os nomes antes de ativar." : "Ferramenta salva.");
    });
  }

  function discover(tool: ToolConfig) {
    void mutate(async (signal) => {
      const { tools: discovered } = await apiJson<{ tools: ToolDefinition[] }>(client, `/tools/${tool.id}/discover`, signal);
      if (signal.aborted) return;
      const updated = { ...tool, config: { ...tool.config, discovered_tools: discovered } };
      setTools((current) => current.map((item) => item.id === tool.id ? updated : item));
      edit(updated);
      setNotice(discovered.length ? "Selecione e salve os nomes permitidos." : "Nenhuma ferramenta encontrada. Confira a URL e as permissões MCP.");
    });
  }

  return <details ref={panel} className="settings tools-settings"><summary>Ferramentas</summary>
    <div className="feature-body">
      {loading && <p role="status">Carregando…</p>}
      {error && <p role="alert" className="notice error whitespace-pre-wrap">{error}</p>}
      {notice && <p role="status" className="notice">{notice}</p>}
      <div className="feature-actions"><button type="button" className="button secondary" disabled={disabled || working || loading} onClick={() => { setForm(emptyForm); setEditing(true); }}>Adicionar ferramenta</button><button type="button" className="text-button" disabled={disabled || working || loading} onClick={() => { setLoading(true); setReload((value) => value + 1); }}>Atualizar</button></div>
      <ul className="tool-list">{tools.map((tool) => <li key={tool.id}>
        <div className="tool-title"><strong>{tool.name}</strong>{tool.kind !== "builtin" && <span className="tool-kind">{tool.kind.toUpperCase()}</span>}<label className="checkbox-label"><input type="checkbox" aria-label={`${tool.name} ativada`} checked={tool.enabled} disabled={disabled || working || loading} onChange={(event) => { const enabled = event.target.checked; void mutate(async (signal) => {
          const { tool: updated } = await apiJson<{ tool: ToolConfig }>(client, `/tools/${tool.id}`, signal, { enabled }, "PATCH");
          if (!signal.aborted) setTools((current) => current.map((item) => item.id === updated.id ? updated : item));
        }); }} /></label></div>
        {tool.config.url && <p className="tool-url">{tool.config.url}</p>}
        {tool.has_secret && <p className="muted text-xs">Token salvo.</p>}
        {tool.kind === "mcp" && <p className="muted text-xs">Permitidos: {(tool.config.allowed_tools ?? []).join(", ") || "nenhum"}</p>}
        {tool.kind !== "builtin" && <div className="feature-actions"><button type="button" className="text-button" aria-label={`Editar ${tool.name}`} disabled={disabled || working} onClick={() => edit(tool)}>Editar</button>{tool.kind === "mcp" && <button type="button" className="text-button" aria-label={`Descobrir ferramentas de ${tool.name}`} disabled={disabled || working} onClick={() => discover(tool)}>Descobrir</button>}<button type="button" className="text-button" aria-label={`Excluir ${tool.name}`} disabled={disabled || working} onClick={() => {
          if (!window.confirm(`Excluir a ferramenta ${tool.name}?`)) return;
          void mutate(async (signal) => { await apiJson(client, `/tools/${tool.id}`, signal, undefined, "DELETE"); if (!signal.aborted) { setTools((current) => current.filter((item) => item.id !== tool.id)); if (form.id === tool.id) setEditing(false); } });
        }}>Excluir</button></div>}
      </li>)}</ul>
      {editing && <form className="form-stack tool-form" onSubmit={save}>
        <fieldset disabled={disabled || working}><div className="form-stack">
          <label>Tipo<select value={form.kind} disabled={!!form.id} onChange={(event) => setForm({ ...form, kind: event.target.value as "http" | "mcp", enabled: false })}><option value="http">HTTP GET</option><option value="mcp">MCP</option></select></label>
          <label>Nome<input required maxLength={64} pattern={"[A-Za-z][A-Za-z0-9_\\-]*"} title="Inicie com letra; use letras, números, _ ou -." value={form.name} onChange={(event) => setForm({ ...form, name: event.target.value })} /></label>
          <label>Descrição<textarea required rows={2} maxLength={1000} value={form.description} onChange={(event) => setForm({ ...form, description: event.target.value })} /></label>
          <label>URL HTTPS pública<input type="url" pattern="https://.*" required value={form.url} onChange={(event) => setForm({ ...form, url: event.target.value, ...(form.kind === "mcp" ? { allowed: [], discovered: [], enabled: false } : {}) })} /></label>
          <p className="muted text-xs">HTTPS público, sem credenciais na URL.{form.kind === "mcp" && " MCP: host autorizado, só leitura. Trocar a URL remove permissões."}</p>
          {form.kind === "http" ? <label>Parâmetros (JSON Schema)<textarea rows={7} className="json-input" required spellCheck={false} value={form.schema} onChange={(event) => setForm({ ...form, schema: event.target.value })} /></label> : <fieldset className="allowed-tools">
            <legend>Ferramentas permitidas (até 20)</legend>
            {form.discovered.length ? form.discovered.map((tool) => <div key={tool.name}>
              <label className="checkbox-label"><input type="checkbox" checked={form.allowed.includes(tool.name)} disabled={!form.allowed.includes(tool.name) && form.allowed.length >= 20} onChange={(event) => setForm({ ...form, allowed: event.target.checked ? [...form.allowed, tool.name] : form.allowed.filter((name) => name !== tool.name) })} /><strong>{tool.name}</strong></label>
              <details><summary>Esquema de {tool.name}</summary><pre>{JSON.stringify(tool.input_schema, null, 2)}</pre></details>
            </div>) : <p className="muted text-sm">Salve desativado e use “Descobrir”. Só nomes permitidos serão ativados.</p>}
            {form.allowed.filter((name) => !form.discovered.some((tool) => tool.name === name)).map((name) => <label className="checkbox-label" key={name}><input type="checkbox" checked onChange={() => setForm({ ...form, allowed: form.allowed.filter((item) => item !== name) })} />{name} (não encontrado; desmarque para remover)</label>)}
          </fieldset>}
          <label>Token Bearer (opcional)<input type="password" maxLength={8192} autoComplete="new-password" value={form.secret} placeholder={form.id ? "Vazio mantém o atual" : undefined} onChange={(event) => setForm({ ...form, secret: event.target.value, replaceSecret: true })} /></label>
          {form.id && <button type="button" className="text-button" onClick={() => setForm({ ...form, secret: "", replaceSecret: true })}>Remover token ao salvar</button>}
          {form.replaceSecret && !form.secret && <p role="status" className="muted text-xs">Token será removido ao salvar.</p>}
          <label className="checkbox-label"><input type="checkbox" checked={form.enabled} onChange={(event) => setForm({ ...form, enabled: event.target.checked })} />Ativar</label>
          <div className="feature-actions"><button type="submit" className="button primary">Salvar</button><button type="button" className="button secondary" onClick={() => { setEditing(false); setForm(emptyForm); }}>Cancelar</button></div>
        </div></fieldset>
      </form>}
      <section className="connector-section" aria-labelledby="google-heading"><h2 id="google-heading">Google Drive e Gmail</h2>
        <p className="muted text-sm">Somente leitura.</p>
        {google && <>{google.connected && <p>{google.email || "Conectado"}</p>}{google.scopes.length > 0 && <details><summary>Permissões</summary><ul>{google.scopes.map((scope) => <li key={scope}><code>{scope}</code></li>)}</ul></details>}
          {!google.configured ? <div className="notice"><p>Configure o Google no servidor.</p><a className="text-button" href="https://console.cloud.google.com/apis/credentials" target="_blank" rel="noreferrer">Configurar</a></div> : !google.connected && <button type="button" className="button secondary" disabled={disabled || working} onClick={() => void mutate(async (signal) => {
            const { url } = await apiJson<{ url: string }>(client, "/connectors/google/connect", signal, {});
            const destination = new URL(url);
            if (destination.protocol !== "https:" || destination.hostname !== "accounts.google.com") throw new Error("Endereço de autorização Google inválido. Contate o administrador.");
            if (!signal.aborted) window.location.assign(destination.href);
          })}>Conectar Google</button>}
          {google.connected && <button type="button" className="button secondary" disabled={disabled || working} onClick={() => {
            if (!window.confirm("Desconectar Google e apagar as credenciais salvas?")) return;
            void mutate(async (signal) => { await apiJson(client, "/connectors/google", signal, undefined, "DELETE"); if (!signal.aborted) { setGoogle({ ...google, connected: false, scopes: [], email: undefined }); setNotice("Google desconectado."); } });
          }}>Desconectar Google</button>}
        </>}
      </section>
    </div>
  </details>;
}
