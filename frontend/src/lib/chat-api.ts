import type { SupabaseClient } from "@supabase/supabase-js";

export type Conversation = { id: string; title: string; created_at: string; updated_at: string };
export type ExecutionStatus = "running" | "completed" | "failed" | "interrupted";
export type Attachment = { id: string; conversation_id: string; filename: string; media_type: string; size_bytes: number; page_count?: number | null; created_at: string };
export type Message = { id: string; role: "user" | "assistant"; content: string; status: ExecutionStatus; run_id: string; created_at: string; attachments?: Attachment[] };
export type Run = { id: string; status: ExecutionStatus; model: string; input_tokens: number | null; output_tokens: number | null; error_code: string | null; created_at: string; finished_at: string | null };
export type ToolCall = { id: string; run_id?: string; name: string; status: "running" | "completed" | "failed"; arguments?: unknown; result?: unknown; error?: string | null; created_at?: string; finished_at?: string | null };
export type ModelStep = { id: string; run_id: string; kind: "reply" | "compaction"; status: "running" | "completed" | "failed"; reserved_tokens: number; input_tokens: number | null; output_tokens: number | null };
export type ContextSummary = { id: string; through_message_id?: string | null; scope?: "history" | "loop"; created_at?: string };
export type History = { messages: Message[]; runs: Run[]; tool_calls?: ToolCall[]; summaries?: ContextSummary[]; model_steps?: ModelStep[] };
export type ToolDefinition = { name: string; description: string; input_schema: Record<string, unknown> };
export type ToolConfig = { id: string; name: string; description: string; kind: "builtin" | "http" | "mcp"; enabled: boolean; config: { url?: string; input_schema?: Record<string, unknown>; allowed_tools?: string[]; discovered_tools?: ToolDefinition[] }; has_secret: boolean; created_at: string };
export type GoogleStatus = { configured: boolean; connected: boolean; scopes: string[]; email?: string };
export type Share = { id: string; created_at: string; expires_at: string; revoked_at: string | null };
export type SharedSnapshot = { title: string; created_at: string; expires_at: string; messages: Message[]; tool_calls: ToolCall[] };
export type Usage = { limit_tokens: number; used_tokens: number; reserved_tokens: number; remaining_tokens: number; user_tokens: number };
export type ServerConfig = { auth_configured: boolean; chat_configured: boolean; model: string | null };

export class ApiError extends Error {
  public readonly status: number;
  constructor(message: string, status: number) {
    super(message);
    this.status = status;
    this.name = "ApiError";
  }
}

export function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "Falha na solicitação.";
}

export async function checkedResponse(response: Response): Promise<Response> {
  if (response.ok) return response;
  let detail = "";
  try {
    const body: unknown = await response.json();
    if (body && typeof body === "object" && "error" in body && typeof body.error === "string") detail = body.error;
  } catch { /* Proxies may return a non-JSON error page. */ }
  const fallback: Record<number, string> = {
    401: "Sessão expirada. Entre novamente.",
    404: "Conversa indisponível.",
    409: "Atualize a conversa antes de reenviar.",
    429: "Limite de uso atingido.",
    503: "Serviço indisponível.",
  };
  throw new ApiError(detail || fallback[response.status] || `Erro HTTP ${response.status}.`, response.status);
}

export async function apiFetch(client: SupabaseClient, path: string, signal: AbortSignal, body?: unknown, method = body === undefined ? "GET" : "POST"): Promise<Response> {
  const { data, error } = await client.auth.getSession();
  signal.throwIfAborted();
  if (error) throw error;
  if (!data.session) throw new ApiError("Sessão expirada. Entre novamente.", 401);
  try {
    const multipart = body instanceof FormData;
    return await checkedResponse(await fetch(`/api${path}`, {
      method,
      headers: { Authorization: `Bearer ${data.session.access_token}`, ...(body === undefined || multipart ? {} : { "Content-Type": "application/json" }) },
      ...(body === undefined ? {} : { body: multipart ? body : JSON.stringify(body) }),
      cache: "no-store",
      signal,
    }));
  } catch (error) {
    if (signal.aborted || error instanceof ApiError) throw error;
    throw new Error("Sem conexão com a API. Tente novamente.");
  }
}

export async function apiJson<T>(client: SupabaseClient, path: string, signal: AbortSignal, body?: unknown, method?: string): Promise<T> {
  const response = await apiFetch(client, path, signal, body, method);
  try { return await response.json() as T; }
  catch { throw new Error("Resposta inválida. Atualize a página."); }
}
