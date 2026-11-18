const ESCAPED: Record<string, string> = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };

function escapeHtml(text: string): string {
  return text.replace(/[&<>"']/g, (character) => ESCAPED[character]);
}

function safeUrl(url: string): string | null {
  const trimmed = url.trim();
  return /^https?:\/\//i.test(trimmed) ? trimmed : null;
}

function inlineHtml(escaped: string): string {
  return escaped
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>")
    .replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (match, text: string, url: string) => {
      const href = safeUrl(url.replace(/&amp;/g, "&"));
      return href ? `<a href="${href}" target="_blank" rel="noreferrer">${text}</a>` : match;
    });
}

/** Renders the small markdown subset models produce. Input is escaped first; links keep only http(s). */
export function renderMarkdown(source: string): string {
  const lines = escapeHtml(source).split("\n");
  const blocks: string[] = [];
  let list: { ordered: boolean; items: string[] } | null = null;
  let code: string[] | null = null;

  function flushList() {
    if (list) {
      const tag = list.ordered ? "ol" : "ul";
      blocks.push(`<${tag}>${list.items.map((item) => `<li>${inlineHtml(item)}</li>`).join("")}</${tag}>`);
      list = null;
    }
  }

  for (const line of lines) {
    const fence = line.match(/^```/);
    if (fence) {
      if (code) { blocks.push(`<pre><code>${code.join("\n")}</code></pre>`); code = null; }
      else { flushList(); code = []; }
      continue;
    }
    if (code) { code.push(line); continue; }
    const bullet = line.match(/^\s*[-*]\s+(.*)$/);
    const numbered = line.match(/^\s*\d+[.)]\s+(.*)$/);
    if (bullet) {
      if (list && !list.ordered) list.items.push(bullet[1]);
      else { flushList(); list = { ordered: false, items: [bullet[1]] }; }
    } else if (numbered) {
      if (list && list.ordered) list.items.push(numbered[1]);
      else { flushList(); list = { ordered: true, items: [numbered[1]] }; }
    } else {
      flushList();
      const heading = line.match(/^(#{1,4})\s+(.*)$/);
      if (heading) blocks.push(`<p><strong>${inlineHtml(heading[2])}</strong></p>`);
      else if (line.trim()) blocks.push(`<p>${inlineHtml(line)}</p>`);
    }
  }
  if (code) blocks.push(`<pre><code>${code.join("\n")}</code></pre>`);
  flushList();
  return blocks.join("");
}
