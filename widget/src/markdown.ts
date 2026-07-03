import { marked, Tokens } from "marked";

marked.setOptions({ gfm: true, breaks: true });

// URL 协议白名单：链接只放行 http/https/mailto，图片只放行 http/https。
// javascript:、data:、vbscript: 及实体编码（javascript&#58;）等一律降级为纯文本。
const SAFE_LINK = /^(https?:\/\/|mailto:)/i;
const SAFE_IMG = /^https?:\/\//i;

// 关键防线：不让 marked 透传任何原始 HTML。html token（块级/行内）一律转义成
// 文本输出，链接/图片自行拼装并对属性值转义——最终 HTML 里只会出现 marked
// 自己生成的受控标签，<img onerror>/<svg onload> 之类的注入无从落地。
marked.use({
  renderer: {
    html(token: Tokens.HTML | Tokens.Tag) {
      return escapeHtml(token.text);
    },
    link(token: Tokens.Link) {
      const inner = this.parser.parseInline(token.tokens);
      const href = (token.href || "").trim();
      if (!SAFE_LINK.test(href)) return inner; // 非白名单协议：只保留链接文字
      const title = token.title ? ` title="${escapeHtml(token.title)}"` : "";
      return `<a href="${escapeHtml(href)}"${title} target="_blank" rel="noopener noreferrer nofollow">${inner}</a>`;
    },
    image(token: Tokens.Image) {
      const href = (token.href || "").trim();
      if (!SAFE_IMG.test(href)) return escapeHtml(token.text || href); // 非白名单协议：降级为纯文本
      const title = token.title ? ` title="${escapeHtml(token.title)}"` : "";
      return `<img src="${escapeHtml(href)}" alt="${escapeHtml(token.text || "")}"${title} loading="lazy" />`;
    },
  },
});

// 纵深防御（第二道防线，不再是唯一防线）：即便上面的转义被绕过，也再剥一层
// 明显危险的标签/属性。
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
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}
