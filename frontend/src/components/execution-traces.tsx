import type { ToolCall } from "@/lib/chat-api";

const labels = { running: "Executando", completed: "Concluída", failed: "Falhou" };

export function ExecutionTraces({ calls }: { calls: ToolCall[] }) {
  if (!calls.length) return null;
  return <section className="execution-traces" aria-label="Execuções de ferramentas"><h3>Ferramentas</h3><ol>{calls.map((call) => <li key={call.id} className={`trace ${call.status}`}><details open={call.status === "running"}><summary><span className="status-dot" aria-hidden="true" /><strong>{call.name}</strong><span>{labels[call.status]}</span></summary><div className="trace-body">{call.arguments !== undefined && <div><h4>Parâmetros</h4><pre>{JSON.stringify(call.arguments, null, 2)}</pre></div>}{call.result !== undefined && <div><h4>Resultado</h4><pre>{typeof call.result === "string" ? call.result : JSON.stringify(call.result, null, 2)}</pre></div>}{call.error && <p className="error-text">{call.error}</p>}</div></details></li>)}</ol></section>;
}
