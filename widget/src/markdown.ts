// 自研迷你 Markdown 渲染器：白名单式「先转义后渲染」，零依赖替代 marked（源码约 72KB）。
//
// 安全架构（顺序即防线）：
// 1. 第一步对全文做 HTML 转义（& < > " ' 全转），之后的所有解析都在转义后的文本上进行；
// 2. 输出只可能来自本文件拼装的固定标签集（p/br/h1-h6/strong/em/code/pre/a/img/ul/ol/
//    li/blockquote/hr/table/thead/tbody/tr/th/td），属性值全部来自已转义文本或固定枚举；
// 3. 链接/图片 URL 过协议白名单（链接 http/https/mailto，图片 http/https），实体编码
//    （javascript&#58;）经第一步转义后必然不匹配白名单，一律降级为纯文本；
// 4. 不支持的语法不做任何透传，自然保持为已转义的纯文本；
// 5. sanitize() 作为第二道正则防线保留（纵深防御，与 marked 时代一致）。
//
// 支持子集（对客服回答够用）：段落/换行、#-###### 标题、**粗体**/*斜体*、`行内代码`、
// ```围栏代码块、[链接](url)、![图片](url)、无序/有序列表（嵌套一层）、> 引用、
// --- 分割线、GFM 表格、裸 http(s) 链接自动识别。
// 测试：scripts/markdown-security.test.mjs（pnpm test）。

// URL 协议白名单：链接只放行 http/https/mailto，图片只放行 http/https。
// javascript:、data:、vbscript: 及实体编码（javascript&#58;）等一律降级为纯文本。
const SAFE_LINK = /^(https?:\/\/|mailto:)/i;
const SAFE_IMG = /^https?:\/\//i;

export function renderMarkdown(text: string): string {
  try {
    return sanitize(renderBlocks(escapeHtml(text), 0));
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

// 纵深防御（第二道防线，不是唯一防线）：即便上面的白名单拼装出现纰漏，也再剥一层
// 明显危险的标签/属性。
function sanitize(html: string): string {
  return html
    .replace(/<\s*(script|iframe|object|embed|link|meta)[^>]*>[\s\S]*?<\s*\/\s*\1\s*>/gi, "")
    .replace(/<\s*(script|iframe|object|embed|link|meta)[^>]*\/?>/gi, "")
    .replace(/\son\w+\s*=\s*"[^"]*"/gi, "")
    .replace(/\son\w+\s*=\s*'[^']*'/gi, "")
    .replace(/javascript:/gi, "");
}

// ---------- 块级解析（输入已整体转义） ----------

const RE_FENCE = /^ {0,3}(`{3,}|~{3,})(.*)$/;
const RE_HEADING = /^ {0,3}(#{1,6})[ \t]+(.*?)[ \t]*#*[ \t]*$/;
const RE_HR = /^ {0,3}(?:(?:-[ \t]*){3,}|(?:\*[ \t]*){3,}|(?:_[ \t]*){3,})$/;
const RE_QUOTE = /^ {0,3}&gt;/; // 「>」已被整体转义为 &gt;
const RE_LIST = /^([ \t]*)([-*+]|\d{1,9}[.)])[ \t]+(.*)$/;
const RE_LANG = /^[A-Za-z0-9_+.#-]{1,24}$/; // 代码块语言名白名单（进 class 属性）
const RE_SEP_CELL = /^:?-+:?$/;

function renderBlocks(src: string, depth: number): string {
  const lines = src.split(/\r?\n/);
  const out: string[] = [];
  let para: string[] = [];
  const flush = () => {
    if (para.length) {
      out.push(`<p>${para.map((l) => renderInline(l, 0, false)).join("<br/>")}</p>`);
      para = [];
    }
  };

  let i = 0;
  while (i < lines.length) {
    const line = lines[i];

    if (!line.trim()) {
      flush();
      i++;
      continue;
    }

    // 围栏代码块：内容原样（已转义）展示，未闭合则吃到结尾（与 marked 行为一致）
    const fence = RE_FENCE.exec(line);
    if (fence) {
      flush();
      const closeRe = new RegExp(`^ {0,3}\\${fence[1][0]}{${fence[1].length},}[ \\t]*$`);
      const info = fence[2].trim().split(/[ \t]/)[0] || "";
      const lang = RE_LANG.test(info) ? info : "";
      const buf: string[] = [];
      i++;
      while (i < lines.length && !closeRe.test(lines[i])) buf.push(lines[i++]);
      i++; // 跳过闭合围栏
      out.push(`<pre><code${lang ? ` class="language-${lang}"` : ""}>${buf.join("\n")}</code></pre>`);
      continue;
    }

    const h = RE_HEADING.exec(line);
    if (h) {
      flush();
      const n = h[1].length;
      out.push(`<h${n}>${renderInline(h[2], 0, false)}</h${n}>`);
      i++;
      continue;
    }

    if (RE_HR.test(line)) {
      flush();
      out.push("<hr/>");
      i++;
      continue;
    }

    // 引用：连续 &gt; 行；引用体递归按块解析（可含列表/嵌套引用），深度限 2 防递归失控
    if (RE_QUOTE.test(line)) {
      flush();
      const buf: string[] = [];
      while (i < lines.length && RE_QUOTE.test(lines[i])) {
        buf.push(lines[i].replace(/^ {0,3}&gt; ?/, ""));
        i++;
      }
      const inner =
        depth < 2
          ? renderBlocks(buf.join("\n"), depth + 1)
          : `<p>${buf.map((l) => renderInline(l, 0, false)).join("<br/>")}</p>`;
      out.push(`<blockquote>${inner}</blockquote>`);
      continue;
    }

    const li = RE_LIST.exec(line);
    if (li && indentOf(li[1]) < 2) {
      flush();
      const r = parseList(lines, i);
      out.push(r.html);
      i = r.next;
      continue;
    }

    const table = parseTable(lines, i);
    if (table) {
      flush();
      out.push(table.html);
      i = table.next;
      continue;
    }

    para.push(line.trim());
    i++;
  }
  flush();
  return out.join("");
}

function indentOf(ws: string): number {
  let w = 0;
  for (const c of ws) w += c === "\t" ? 2 : 1;
  return w;
}

interface ListItem {
  text: string[]; // 首行 + 缩进续行（渲染时以 <br/> 连接）
  sub: string[]; // 嵌套一层的子项（更深的缩进也折算到这一层）
  subTag: "" | "ul" | "ol";
  subStart: number;
}

function parseList(lines: string[], start: number): { html: string; next: number } {
  const head = RE_LIST.exec(lines[start])!;
  const ordered = /^\d/.test(head[2]);
  const startNum = ordered ? parseInt(head[2], 10) : 1;
  const items: ListItem[] = [];
  let i = start;

  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) {
      // 项间空行：下一非空行仍是列表项则跨过空行继续，否则列表结束
      let j = i + 1;
      while (j < lines.length && !lines[j].trim()) j++;
      if (j < lines.length && RE_LIST.test(lines[j])) {
        i = j;
        continue;
      }
      break;
    }
    const m = RE_LIST.exec(line);
    if (m) {
      const isOrd = /^\d/.test(m[2]);
      if (indentOf(m[1]) < 2) {
        if (isOrd !== ordered) break; // 顶层换类型 → 结束，交给下一轮开新列表
        items.push({ text: [m[3]], sub: [], subTag: "", subStart: 1 });
      } else {
        const last = items[items.length - 1];
        if (!last) break;
        if (!last.subTag) {
          last.subTag = isOrd ? "ol" : "ul";
          last.subStart = isOrd ? parseInt(m[2], 10) : 1;
        }
        last.sub.push(m[3]);
      }
      i++;
      continue;
    }
    // 缩进的普通行并入上一项作为续行；顶格行结束列表
    if (items.length && indentOf((/^[ \t]*/.exec(line) as RegExpExecArray)[0]) >= 2) {
      items[items.length - 1].text.push(line.trim());
      i++;
      continue;
    }
    break;
  }

  const tag = ordered ? "ol" : "ul";
  const startAttr = ordered && startNum !== 1 ? ` start="${startNum}"` : "";
  const body = items
    .map((it) => {
      let s = `<li>${it.text.map((t) => renderInline(t, 0, false)).join("<br/>")}`;
      if (it.sub.length) {
        const ss = it.subTag === "ol" && it.subStart !== 1 ? ` start="${it.subStart}"` : "";
        s += `<${it.subTag}${ss}>${it.sub.map((t) => `<li>${renderInline(t, 0, false)}</li>`).join("")}</${it.subTag}>`;
      }
      return s + "</li>";
    })
    .join("");
  return { html: `<${tag}${startAttr}>${body}</${tag}>`, next: i };
}

function parseTable(lines: string[], start: number): { html: string; next: number } | null {
  const header = lines[start];
  const sepLine = lines[start + 1];
  if (!header.includes("|") || !sepLine || !sepLine.includes("|") || !sepLine.includes("-")) return null;
  const heads = splitCells(header);
  const seps = splitCells(sepLine);
  if (!heads.length || heads.length !== seps.length || !seps.every((c) => RE_SEP_CELL.test(c))) return null;

  // 对齐方式来自固定枚举（用户输入只决定选哪一个，不进属性值）
  const aligns = seps.map((c) => {
    const l = c.startsWith(":");
    const r = c.endsWith(":");
    if (l && r) return ' style="text-align:center"';
    if (r) return ' style="text-align:right"';
    if (l) return ' style="text-align:left"';
    return "";
  });

  let i = start + 2;
  const rows: string[] = [];
  while (i < lines.length && lines[i].trim() && lines[i].includes("|")) {
    const cells = splitCells(lines[i]);
    while (cells.length < heads.length) cells.push("");
    rows.push(
      `<tr>${cells
        .slice(0, heads.length)
        .map((c, k) => `<td${aligns[k]}>${renderInline(c, 0, false)}</td>`)
        .join("")}</tr>`
    );
    i++;
  }
  const thead = `<thead><tr>${heads.map((c, k) => `<th${aligns[k]}>${renderInline(c, 0, false)}</th>`).join("")}</tr></thead>`;
  return {
    html: `<table>${thead}${rows.length ? `<tbody>${rows.join("")}</tbody>` : ""}</table>`,
    next: i,
  };
}

function splitCells(line: string): string[] {
  let s = line.replace(/\\\|/g, "\x00").trim(); // \| → 字面竖线（占位再还原）
  if (s.startsWith("|")) s = s.slice(1);
  if (s.endsWith("|")) s = s.slice(0, -1);
  return s.split("|").map((c) => c.replace(/\x00/g, "|").trim());
}

// ---------- 行内解析（输入已整体转义） ----------

// 可被反斜杠转义的标点（CommonMark 子集；原始 <>"'& 已被整体转义，不会在此出现）
const RE_ESCAPABLE = /[!"#$%&'()*+,\-./:;<=>?@[\]\\^_`{|}~]/;

function renderInline(text: string, depth: number, noLink: boolean): string {
  if (depth > 6) return text; // 嵌套过深：剩余部分按纯文本输出
  let out = "";
  let i = 0;
  const n = text.length;
  while (i < n) {
    const ch = text[i];

    if (ch === "\\" && i + 1 < n && RE_ESCAPABLE.test(text[i + 1])) {
      out += text[i + 1];
      i += 2;
      continue;
    }

    // 行内代码：内容原样（已转义）展示，不再做任何行内变换
    if (ch === "`") {
      let run = 1;
      while (text[i + run] === "`") run++;
      const opener = text.slice(i, i + run);
      const close = text.indexOf(opener, i + run);
      if (close !== -1) {
        out += `<code>${text.slice(i + run, close).trim()}</code>`;
        i = close + run;
        continue;
      }
      out += opener;
      i += run;
      continue;
    }

    if (ch === "!" && text[i + 1] === "[") {
      const t = parseLinkTarget(text, i + 1);
      if (t) {
        if (SAFE_IMG.test(t.href)) {
          out += `<img src="${t.href}" alt="${t.label}"${t.title ? ` title="${t.title}"` : ""} loading="lazy" />`;
        } else {
          out += t.label; // 非白名单协议：降级为纯文本（alt）
        }
        i = t.end;
        continue;
      }
    }

    if (ch === "[") {
      const t = parseLinkTarget(text, i);
      if (t) {
        const inner = renderInline(t.label, depth + 1, true);
        if (!noLink && SAFE_LINK.test(t.href)) {
          out += `<a href="${t.href}"${t.title ? ` title="${t.title}"` : ""} target="_blank" rel="noopener noreferrer nofollow">${inner}</a>`;
        } else {
          out += inner; // 非白名单协议：只保留链接文字
        }
        i = t.end;
        continue;
      }
    }

    if (ch === "*" || ch === "_") {
      const em = parseEmphasis(text, i, ch, depth, noLink);
      if (em) {
        out += em.html;
        i = em.end;
        continue;
      }
    }

    // GFM 裸链接自动识别（链接文字内不再嵌套）
    if (!noLink && (ch === "h" || ch === "H")) {
      const url = parseAutoLink(text, i);
      if (url) {
        out += `<a href="${url}" target="_blank" rel="noopener noreferrer nofollow">${url}</a>`;
        i += url.length;
        continue;
      }
    }

    out += ch;
    i++;
  }
  return out;
}

/** 解析 [label](dest "title") / ![label](dest)。返回的 label/href/title 均为已转义
 *  文本的原样切片（不含原始引号与尖括号），可直接拼进双引号属性。 */
function parseLinkTarget(
  text: string,
  start: number
): { label: string; href: string; title: string; end: number } | null {
  // [label]：支持方括号嵌套与反斜杠转义。扫描窗口设上限：客服回答里不存在
  // 上千字符的链接文字，却能把「[[[[…」这类病态输入从 O(n²) 压回 O(n)。
  const n = Math.min(text.length, start + 2000);
  let j = start + 1;
  let level = 1;
  while (j < n && level > 0) {
    const c = text[j];
    if (c === "\\") {
      j += 2;
      continue;
    }
    if (c === "[") level++;
    else if (c === "]") level--;
    j++;
  }
  if (level !== 0 || text[j] !== "(") return null;
  const label = text.slice(start + 1, j - 1);

  // (dest "title")：dest 到空白或配对右括号为止（不允许包含空白）
  let k = j + 1;
  while (k < n && (text[k] === " " || text[k] === "\t")) k++;
  const hrefStart = k;
  let paren = 0;
  while (k < n) {
    const c = text[k];
    if (c === "\\") {
      k += 2;
      continue;
    }
    if (/\s/.test(c)) break;
    if (c === "(") paren++;
    else if (c === ")") {
      if (paren === 0) break;
      paren--;
    }
    k++;
  }
  const href = text.slice(hrefStart, Math.min(k, n));
  let m = k;
  while (m < n && /\s/.test(text[m])) m++;
  if (text[m] === ")") return { label, href, title: "", end: m + 1 };
  // 可选 title：原始引号已被整体转义为 &quot; / &#39;，按实体形式配对；
  // 后面还有别的内容（如属性注入尝试）则整体不算链接，降级为纯文本
  const tm =
    /^&quot;([\s\S]*?)&quot;[ \t]*\)/.exec(text.slice(m)) ||
    /^&#39;([\s\S]*?)&#39;[ \t]*\)/.exec(text.slice(m));
  if (!tm) return null;
  return { label, href, title: tm[1], end: m + tm[0].length };
}

function parseEmphasis(
  text: string,
  i: number,
  ch: string,
  depth: number,
  noLink: boolean
): { html: string; end: number } | null {
  // 词内下划线不作强调（snake_case）
  if (ch === "_" && i > 0 && /[A-Za-z0-9_]/.test(text[i - 1])) return null;
  const two = ch + ch;
  const three = two + ch;

  if (text.startsWith(two, i + 1)) {
    // ***粗斜体***
    const close = text.indexOf(three, i + 3);
    if (close !== -1 && text.slice(i + 3, close).trim()) {
      return {
        html: `<strong><em>${renderInline(text.slice(i + 3, close), depth + 1, noLink)}</em></strong>`,
        end: close + 3,
      };
    }
  }
  if (text[i + 1] === ch) {
    // **粗体**
    const close = text.indexOf(two, i + 2);
    if (close !== -1 && text.slice(i + 2, close).trim()) {
      return {
        html: `<strong>${renderInline(text.slice(i + 2, close), depth + 1, noLink)}</strong>`,
        end: close + 2,
      };
    }
    return null;
  }
  // *斜体*：跳过被转义的闭合符与词内下划线；内容首尾不能是空白
  let close = text.indexOf(ch, i + 1);
  while (
    close !== -1 &&
    (text[close - 1] === "\\" || (ch === "_" && /[A-Za-z0-9_]/.test(text[close + 1] || "")))
  ) {
    close = text.indexOf(ch, close + 1);
  }
  if (close === -1) return null;
  const body = text.slice(i + 1, close);
  if (!body.trim() || /^\s/.test(body) || /\s$/.test(body)) return null;
  return { html: `<em>${renderInline(body, depth + 1, noLink)}</em>`, end: close + 1 };
}

/** 裸 http(s) 链接：不吞 CJK 与全角标点，去掉尾随英文标点及不成对的右括号。 */
function parseAutoLink(text: string, i: number): string | null {
  if (i > 0 && /[A-Za-z0-9]/.test(text[i - 1])) return null;
  const m = /^https?:\/\/[^\s\u3000-\u303f\u4e00-\u9fff\uff00-\uffef]+/i.exec(text.slice(i));
  if (!m) return null;
  let url = m[0];
  for (;;) {
    if (/[.,;:!?]$/.test(url)) {
      url = url.slice(0, -1);
      continue;
    }
    if (url.endsWith(")") && !url.includes("(")) {
      url = url.slice(0, -1);
      continue;
    }
    break;
  }
  return /^https?:\/\/[^\s/]/i.test(url) ? url : null;
}
