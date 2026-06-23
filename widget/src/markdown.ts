import { marked } from "marked";

marked.setOptions({ gfm: true, breaks: true });

// Minimal sanitization: strip <script>/<iframe>/event handlers. Assistant output is
// trusted-ish (our own model), but we render it as HTML, so we defend in depth.
function sanitize(html: string): string {
  return html
    .replace(/<\s*(script|iframe|object|embed|link|meta)[^>]*>[\s\S]*?<\s*\/\s*\1\s*>/gi, "")
    .replace(/<\s*(script|iframe|object|embed|link|meta)[^>]*\/?>/gi, "")
    .replace(/\son\w+\s*=\s*"[^"]*"/gi, "")
    .replace(/\son\w+\s*=\s*'[^']*'/gi, "")
    .replace(/javascript:/gi, "");
}

export function renderMarkdown(text: string): string {
  try {
    return sanitize(marked.parse(text, { async: false }) as string);
  } catch {
    return escapeHtml(text);
  }
}

export function escapeHtml(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}
