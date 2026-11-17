"use client";

import { createClient, type Session, type SupabaseClient } from "@supabase/supabase-js";
import { useEffect, useState, type FormEvent } from "react";
import { ChatWorkspace } from "@/components/chat-workspace";
import { errorMessage } from "@/lib/chat-api";

const publicUrl = process.env.NEXT_PUBLIC_SUPABASE_URL;
const publicKey = process.env.NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY;
let browserClient: SupabaseClient | undefined;

function SetupRequired({ detail }: { detail?: string }) {
  return <main className="entry-shell"><section className="entry-card" aria-labelledby="setup-title">
    <h1 id="setup-title">Configuração necessária</h1>
    <p>Configure <code>frontend/.env.local</code> conforme o README.</p>
    {detail && <p role="alert" className="notice error">{detail}</p>}
    <a className="button secondary" href="/api/config" target="_blank" rel="noreferrer">Ver configuração da API</a>
  </section></main>;
}

function AuthForm({ client, sessionError, clearSessionError }: { client: SupabaseClient; sessionError: string; clearSessionError: () => void }) {
  const [mode, setMode] = useState<"signin" | "signup">("signin");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [confirmation, setConfirmation] = useState("");

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    setError("");
    clearSessionError();
    setConfirmation("");
    try {
      if (mode === "signin") {
        const { error } = await client.auth.signInWithPassword({ email: email.trim(), password });
        if (error) throw error;
      } else {
        const { data, error } = await client.auth.signUp({ email: email.trim(), password, options: { emailRedirectTo: window.location.origin } });
        if (error) throw error;
        if (!data.session) {
          setConfirmation(`Confira ${email.trim()}. Se houver confirmação, abra o link recebido e entre.`);
          setPassword("");
        }
      }
    } catch (error) {
      const code = error && typeof error === "object" && "code" in error ? error.code : "";
      setError(code === "invalid_credentials" ? "E-mail ou senha incorretos." : code === "email_not_confirmed" ? "Confirme seu e-mail pelo link recebido." : errorMessage(error));
    } finally { setBusy(false); }
  }

  return <main className="entry-shell"><section className="entry-card" aria-labelledby="auth-title">
    <h1 id="auth-title">Tess</h1>
    <div className="auth-modes" aria-label="Acesso à conta">
      <button type="button" className={mode === "signin" ? "selected" : ""} aria-pressed={mode === "signin"} disabled={busy} onClick={() => { setMode("signin"); setError(""); clearSessionError(); }}>Entrar</button>
      <button type="button" className={mode === "signup" ? "selected" : ""} aria-pressed={mode === "signup"} disabled={busy} onClick={() => { setMode("signup"); setError(""); clearSessionError(); }}>Criar conta</button>
    </div>
    {(error || sessionError) && <p role="alert" className="notice error">{error || sessionError}</p>}
    {confirmation && <p role="status" className="notice">{confirmation}</p>}
    <form onSubmit={submit} className="form-stack">
      <label htmlFor="email">E-mail<input id="email" type="email" autoComplete="email" value={email} required disabled={busy} onChange={(event) => setEmail(event.target.value)} /></label>
      <label htmlFor="password">Senha<input id="password" type="password" autoComplete={mode === "signin" ? "current-password" : "new-password"} minLength={mode === "signup" ? 8 : undefined} aria-describedby={mode === "signup" ? "password-hint" : undefined} value={password} required disabled={busy} onChange={(event) => setPassword(event.target.value)} /></label>
      {mode === "signup" && <p id="password-hint" className="muted text-sm">Mínimo de 8 caracteres.</p>}
      <button className="button primary" disabled={busy} type="submit">{busy ? "Aguarde…" : mode === "signin" ? "Entrar" : "Criar conta"}</button>
    </form>
  </section></main>;
}

export default function Home() {
  const [client, setClient] = useState<SupabaseClient | null>(null);
  const [session, setSession] = useState<Session | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [configError, setConfigError] = useState("");

  useEffect(() => {
    if (!publicUrl || !publicKey) return;
    let active = true;
    let authEventReceived = false;
    let subscription: { unsubscribe(): void } | undefined;
    void (async () => {
      try {
        if (publicKey.startsWith("sb_secret_")) throw new Error("Chave secreta no frontend. Remova e revogue-a; use uma chave pública.");
        const payload = publicKey.split(".")[1];
        if (payload) {
          let role: unknown;
          try { role = JSON.parse(atob(payload.replace(/-/g, "+").replace(/_/g, "/"))).role; } catch { /* The SDK validates non-JWT publishable keys. */ }
          if (role === "service_role") throw new Error("Chave service_role no frontend. Remova e revogue-a; use a chave pública anon.");
        }
        const supabase = browserClient ??= createClient(publicUrl, publicKey, { auth: { persistSession: true, autoRefreshToken: true, detectSessionInUrl: true } });
        subscription = supabase.auth.onAuthStateChange((_event, nextSession) => {
          if (!active) return;
          authEventReceived = true;
          setClient(supabase);
          setSession(nextSession);
          setLoading(false);
        }).data.subscription;
        try {
          const { data, error } = await supabase.auth.getSession();
          if (!active) return;
          if (error) setError(error.message);
          setClient(supabase);
          if (!authEventReceived) setSession(data.session);
          setLoading(false);
        } catch (error) {
          if (active) { setClient(supabase); setError(errorMessage(error)); setLoading(false); }
        }
      } catch (error) { if (active) setConfigError(errorMessage(error)); }
    })();
    return () => { active = false; subscription?.unsubscribe(); };
  }, []);

  if (!publicUrl || !publicKey || configError) return <SetupRequired detail={configError} />;
  if (loading || !client) return <main className="entry-shell"><p role="status">Carregando…</p></main>;
  if (!session) return <AuthForm client={client} sessionError={error} clearSessionError={() => setError("")} />;
  return <ChatWorkspace key={session.user.id} client={client} userId={session.user.id} email={session.user.email ?? "Conta autenticada"} />;
}
