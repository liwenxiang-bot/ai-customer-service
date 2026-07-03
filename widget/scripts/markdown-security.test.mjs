#!/usr/bin/env node
// widget Markdown 渲染器安全与正确性测试（pnpm test）。
//
// 直接以 node 运行：用已有的 typescript devDependency 把 src/markdown.ts 就地转译后
// 经 data: URL 导入，零额外依赖、零构建产物。
//
// 覆盖三块：
// 1. 既有 16 个攻击向量（e7ac154 XSS 修复的防护范围：原始 HTML 透传、事件处理器、
//    javascript:/data:/vbscript:/实体编码 URL、script/iframe/object/embed/link/meta）；
// 2. 新增 14 个攻击向量（属性逃逸 / URL 混淆 / 表格与代码块边界 / 嵌套重组）；
// 3. 各支持语法的渲染正确性断言。

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import ts from "typescript";

const srcPath = fileURLToPath(new URL("../src/markdown.ts", import.meta.url));
const compiled = ts.transpileModule(readFileSync(srcPath, "utf8"), {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2020 },
}).outputText;
const { renderMarkdown, escapeHtml } = await import(
  "data:text/javascript;base64," + Buffer.from(compiled).toString("base64")
);

// ---------- 断言工具 ----------

let passed = 0;
const failures = [];

function check(name, fn) {
  try {
    fn();
    passed++;
  } catch (e) {
    failures.push({ name, msg: e.message });
  }
}

function ok(cond, msg) {
  if (!cond) throw new Error(msg);
}

// 渲染器唯一允许产出的标签与属性（与 src/markdown.ts 的拼装集合一一对应）
const ALLOWED_TAGS = new Set([
  "p", "br", "h1", "h2", "h3", "h4", "h5", "h6", "strong", "em", "code", "pre",
  "a", "img", "ul", "ol", "li", "blockquote", "hr",
  "table", "thead", "tbody", "tr", "th", "td",
]);
const ALLOWED_ATTRS = new Set(["href", "title", "target", "rel", "src", "alt", "loading", "class", "start", "style"]);

/** 白名单式安全断言：输出里的每一个标签都必须在固定集合内、不带事件处理器、
 *  属性名在白名单内、href/src 过协议白名单、class/style 只能是固定枚举形态。 */
function assertSafe(html, label) {
  for (const t of html.match(/<[^>]*>/g) || []) {
    const m = /^<\/?([a-zA-Z0-9]+)/.exec(t);
    ok(m && ALLOWED_TAGS.has(m[1].toLowerCase()), `${label}：非白名单标签 ${t}`);
    // 掏空引号内的属性值再查属性名，避免 alt/title 文本里的「x=」误报
    const skeleton = t.replace(/"[^"]*"/g, '""');
    ok(!/\son\w+\s*=/i.test(skeleton), `${label}：事件处理器 ${t}`);
    ok(!/(javascript|vbscript|data)\s*:/i.test(skeleton), `${label}：标签内危险协议 ${t}`);
    for (const am of skeleton.matchAll(/\s([a-zA-Z-]+)\s*=/g)) {
      ok(ALLOWED_ATTRS.has(am[1].toLowerCase()), `${label}：非白名单属性 ${am[1]}（${t}）`);
    }
    const href = /\shref="([^"]*)"/.exec(t);
    if (href) ok(/^(https?:\/\/|mailto:)/i.test(href[1]), `${label}：href 协议越界 ${href[1]}`);
    const src = /\ssrc="([^"]*)"/.exec(t);
    if (src) ok(/^https?:\/\//i.test(src[1]), `${label}：src 协议越界 ${src[1]}`);
    const cls = /\sclass="([^"]*)"/.exec(t);
    if (cls) ok(/^language-[A-Za-z0-9_+.#-]{1,24}$/.test(cls[1]), `${label}：class 越界 ${cls[1]}`);
    const style = /\sstyle="([^"]*)"/.exec(t);
    if (style) ok(/^text-align:(left|center|right)$/.test(style[1]), `${label}：style 越界 ${style[1]}`);
  }
  ok(!html.replace(/<[^>]*>/g, "").includes("<"), `${label}：标签外残留未转义的 <`);
}

/** 渲染 + 全量安全断言（所有用例统一走这里，正确性用例同样不许越界）。 */
function md(input, label = "render") {
  const html = renderMarkdown(input);
  assertSafe(html, label);
  return html;
}

// ---------- 一、既有 16 个攻击向量 ----------

const legacyVectors = [
  ["script 标签", "<script>alert(1)</script>"],
  ["img onerror", '<img src=x onerror="alert(1)">'],
  ["svg onload", "<svg onload=alert(1)>"],
  ["iframe 注入", '<iframe src="https://evil.example"></iframe>'],
  ["object/embed", '<object data="x"></object><embed src="x">'],
  ["link 标签", '<link rel="stylesheet" href="https://evil.example/x.css">'],
  ["meta refresh", '<meta http-equiv="refresh" content="0;url=https://evil.example">'],
  ["javascript: 链接", "[点我](javascript:alert(1))"],
  ["大小写混淆协议", "[点我](JaVaScRiPt:alert(document.cookie))"],
  ["实体编码协议", "[点我](javascript&#58;alert(1))"],
  ["vbscript: 链接", "[点我](vbscript:msgbox(1))"],
  ["data: 链接", "[点我](data:text/html,<script>alert(1)</script>)"],
  ["javascript: 图片", "![x](javascript:alert(1))"],
  ["data: 图片", "![x](data:image/svg+xml,<svg onload=alert(1)>)"],
  ["行内事件处理器", '<div onclick="alert(1)">点击</div>'],
  ["原始 HTML 链接", '<a href="javascript:alert(1)">raw link</a>'],
];
for (const [name, input] of legacyVectors) {
  check(`攻击向量（既有）：${name}`, () => {
    const html = md(input, name);
    ok(!/<a\b/i.test(html) || !/href="(javascript|vbscript|data)/i.test(html), `${name}：产出危险链接`);
  });
}

// 抽查关键向量的具体降级形态
check("既有向量降级形态：script 转义为文本", () => {
  ok(md("<script>alert(1)</script>").includes("&lt;script&gt;"), "script 应以转义文本可见");
});
check("既有向量降级形态：javascript: 链接只留文字", () => {
  const html = md("[点我](javascript:alert(1))");
  ok(html.includes("点我") && !html.includes("<a"), "应只保留链接文字");
});
check("既有向量降级形态：javascript: 图片降级为 alt 文本", () => {
  const html = md("![商品图](javascript:alert(1))");
  ok(html.includes("商品图") && !html.includes("<img"), "应只保留 alt 文本");
});

// ---------- 二、新增攻击向量（属性逃逸 / URL 混淆 / 边界 / 嵌套重组） ----------

const newVectors = [
  ["属性逃逸：图片 alt 带引号", '![" onerror="alert(1)](https://a.example/x.png)'],
  ["属性逃逸：链接 title 夹带属性", '[x](https://a.example "y" onfocus="alert(1)")'],
  ["属性逃逸：URL 内引号", '[x](https://a.example/"><img src=x onerror=alert(1)>)'],
  ["URL 混淆：制表符拆分协议", "[x](java\tscript:alert(1))"],
  ["URL 混淆：换行拆分协议", "[x](java\nscript:alert(1))"],
  ["URL 混淆：控制字符前缀", "[x](\u0001javascript:alert(1))"],
  ["URL 混淆：前导空格", "[x](   javascript:alert(1))"],
  ["URL 混淆：加粗标记拆分协议", "[x](java**script**:alert(1))"],
  ["嵌套重组：链接里套危险图片", "[![alt](javascript:x)](javascript:y)"],
  ["嵌套重组：强调切开的标签", "**<scr**ipt>alert(1)</scr**ipt>**"],
  ["嵌套重组：强调包裹的标签", "*<u>*abc*</u>*"],
  ["表格边界：单元格注入", "| 名称 | 值 |\n| --- | --- |\n| <img src=x onerror=alert(1)> | [x](javascript:alert(1)) |"],
  ["代码块边界：围栏内标签", "```html\n<script>alert(1)</script>\n```"],
  ["代码块边界：未闭合围栏", "```\n<img src=x onerror=alert(1)>"],
];
for (const [name, input] of newVectors) {
  check(`攻击向量（新增）：${name}`, () => {
    const html = md(input, name);
    ok(!/href="\s*(javascript|vbscript|data)/i.test(html), `${name}：危险 href 泄漏`);
    ok(!/src="\s*(javascript|vbscript|data)/i.test(html), `${name}：危险 src 泄漏`);
  });
}

check("新增向量降级形态：实体不被二次解码", () => {
  // 用户输入的实体文本必须原样可见（&lt; → &amp;lt;），绝不能被重组成标签
  const html = md("&lt;script&gt;alert(1)&lt;/script&gt;");
  ok(html.includes("&amp;lt;script&amp;gt;"), "实体应被再转义为文本");
  ok(!html.includes("<script"), "实体不得重组为标签");
});
check("新增向量降级形态：代码块内容保持转义", () => {
  const html = md("```html\n<script>alert(1)</script>\n```");
  ok(html.includes("&lt;script&gt;alert(1)&lt;/script&gt;"), "围栏内应为转义文本");
});
check("新增向量降级形态：autolink 不吞出危险协议", () => {
  const html = md("详见 https://a.example?next=javascript:alert(1) 页面");
  ok(!/href="(javascript|vbscript|data)/i.test(html), "href 不得以危险协议开头");
});
check("新增向量降级形态：表格单元格转义", () => {
  const html = md("| 名称 | 值 |\n| --- | --- |\n| <b>x</b> | y |");
  ok(html.includes("&lt;b&gt;x&lt;/b&gt;"), "单元格内原始 HTML 应转义");
});

// ---------- 三、正确性用例 ----------

check("标题 1-6 级", () => {
  ok(md("# 一级").includes("<h1>一级</h1>"), "h1");
  ok(md("## 退款政策").includes("<h2>退款政策</h2>"), "h2");
  ok(md("###### 六级").includes("<h6>六级</h6>"), "h6");
  ok(md("####### 七个井号").includes("####### 七个井号"), "7 个 # 应降级为文本");
});

check("粗体 / 斜体 / 粗斜体", () => {
  const html = md("**加粗**和*斜体*和***都要***");
  ok(html.includes("<strong>加粗</strong>"), "粗体");
  ok(html.includes("<em>斜体</em>"), "斜体");
  ok(html.includes("<strong><em>都要</em></strong>"), "粗斜体");
});

check("下划线强调与 snake_case", () => {
  ok(md("__加粗__ 与 _斜体_").includes("<strong>加粗</strong>"), "下划线粗体");
  ok(md("__加粗__ 与 _斜体_").includes("<em>斜体</em>"), "下划线斜体");
  ok(md("变量 order_item_id 不变").includes("order_item_id"), "词内下划线不强调");
});

check("行内代码", () => {
  const html = md("运行 `pnpm install` 即可");
  ok(html.includes("<code>pnpm install</code>"), "行内代码");
});

check("围栏代码块与语言标注", () => {
  const html = md("```python\nprint('你好')\n```");
  ok(html.includes('<pre><code class="language-python">'), "语言 class");
  ok(html.includes("print(&#39;你好&#39;)"), "内容转义展示");
  const bad = md("```<bad lang>\nx\n```");
  ok(bad.includes("<pre><code>"), "非法语言名应丢弃 class");
});

check("代码块内的 Markdown 语法不生效", () => {
  const html = md("```\n**不加粗** [不是链接](https://a.example)\n```");
  ok(html.includes("**不加粗**"), "围栏内星号原样");
  ok(!html.includes("<a "), "围栏内不产生链接");
});

check("链接", () => {
  const html = md("[官网](https://example.com)");
  ok(html.includes('<a href="https://example.com"'), "href");
  ok(html.includes('target="_blank"'), "新窗口");
  ok(html.includes('rel="noopener noreferrer nofollow"'), "rel 防护");
  ok(html.includes(">官网</a>"), "链接文字");
});

check("mailto 链接与带 title 链接", () => {
  ok(md("[联系我们](mailto:cs@example.com)").includes('href="mailto:cs@example.com"'), "mailto");
  const html = md('[官网](https://example.com "公司首页")');
  ok(html.includes('title="公司首页"'), "title 属性");
});

check("链接文字内的强调", () => {
  const html = md("[**加粗文字**](https://example.com)");
  ok(html.includes("<a "), "仍是链接");
  ok(html.includes("<strong>加粗文字</strong>"), "内部强调生效");
});

check("图片", () => {
  const html = md("![商品图](https://cdn.example.com/a.png)");
  ok(html.includes('<img src="https://cdn.example.com/a.png"'), "src");
  ok(html.includes('alt="商品图"'), "alt");
  ok(html.includes('loading="lazy"'), "懒加载");
});

check("无序列表与一层嵌套", () => {
  const html = md("- 甲\n- 乙\n  - 乙一\n  - 乙二\n- 丙");
  ok(
    html.includes("<ul><li>甲</li><li>乙<ul><li>乙一</li><li>乙二</li></ul></li><li>丙</li></ul>"),
    "嵌套结构"
  );
});

check("有序列表与 start", () => {
  ok(md("1. 一\n2. 二").includes("<ol><li>一</li><li>二</li></ol>"), "有序列表");
  ok(md("3. 三\n4. 四").includes('<ol start="3">'), "start 属性");
  const mixed = md("1. 一\n   1. 一点一\n2. 二");
  ok(mixed.includes("<li>一<ol><li>一点一</li></ol></li>"), "有序嵌套");
});

check("引用", () => {
  ok(md("> 请先登录账号").includes("<blockquote><p>请先登录账号</p></blockquote>"), "单行引用");
  const html = md("> 第一行\n> 第二行");
  ok(html.includes("第一行<br/>第二行"), "多行引用");
});

check("分割线", () => {
  const html = md("上文\n\n---\n\n下文");
  ok(html.includes("<hr/>"), "hr");
  ok(html.includes("<p>上文</p>") && html.includes("<p>下文</p>"), "前后段落完整");
});

check("GFM 表格与对齐", () => {
  const html = md("| 商品 | 价格 |\n| --- | ---: |\n| 苹果 | 5 元 |\n| 香蕉 | 3 元 |");
  ok(html.includes("<table><thead><tr><th>商品</th>"), "表头");
  ok(html.includes('<th style="text-align:right">价格</th>'), "右对齐表头");
  ok(html.includes("<td>苹果</td>"), "单元格");
  ok(html.includes('<td style="text-align:right">5 元</td>'), "对齐跟随列");
  ok(md("| a |\n| :-: |\n| b |").includes('style="text-align:center"'), "居中对齐");
});

check("表格单元格内联语法", () => {
  const html = md("| 项目 | 说明 |\n| --- | --- |\n| **重点** | [文档](https://example.com) |");
  ok(html.includes("<td><strong>重点</strong></td>"), "单元格粗体");
  ok(html.includes('<a href="https://example.com"'), "单元格链接");
});

check("段落与软换行", () => {
  const html = md("第一行\n第二行\n\n第二段");
  ok(html.includes("<p>第一行<br/>第二行</p>"), "单个换行 → <br/>（breaks 行为）");
  ok(html.includes("<p>第二段</p>"), "空行分段");
});

check("裸链接自动识别", () => {
  const html = md("详见 https://example.com/faq。");
  ok(html.includes('<a href="https://example.com/faq"'), "autolink");
  ok(!html.includes("。</a>"), "不吞全角句号");
  ok(md("见 https://example.com/a, 谢谢").includes('href="https://example.com/a"'), "尾随英文逗号剔除");
});

check("不支持语法安全降级为纯文本", () => {
  const html = md("~~删除线~~ 和 <b>原始HTML</b> 和 [ref][1]");
  ok(html.includes("~~删除线~~"), "删除线降级");
  ok(html.includes("&lt;b&gt;原始HTML&lt;/b&gt;"), "原始 HTML 转义");
  ok(html.includes("[ref][1]"), "引用式链接降级");
});

check("反斜杠转义", () => {
  ok(md("\\*不是斜体\\*").includes("*不是斜体*"), "转义星号");
  ok(!md("\\*不是斜体\\*").includes("<em>"), "不产生 em");
});

check("空输入与纯文本", () => {
  ok(renderMarkdown("") === "", "空串");
  ok(md("普通一句话") === "<p>普通一句话</p>", "纯文本段落");
});

check("escapeHtml 导出保持不变", () => {
  ok(escapeHtml('<a b="c">\'&') === "&lt;a b=&quot;c&quot;&gt;&#39;&amp;", "五字符全转");
});

check("综合：RAG 风格长答案", () => {
  const input = [
    "## 退换货政策",
    "",
    "满足以下条件可申请退货：",
    "",
    "- 签收后 **7 天内**",
    "- 商品完好，不影响二次销售",
    "  - 吊牌、包装齐全",
    "",
    "| 类型 | 时限 | 运费 |",
    "| --- | :-: | ---: |",
    "| 质量问题 | 30 天 | 商家承担 |",
    "| 七天无理由 | 7 天 | 买家承担 |",
    "",
    "> 生鲜类商品不支持七天无理由退货。",
    "",
    "操作路径：`我的订单 -> 售后`，详情见 [帮助中心](https://example.com/help)。",
  ].join("\n");
  const html = md(input, "综合");
  ok(html.includes("<h2>退换货政策</h2>"), "标题");
  ok(html.includes("<strong>7 天内</strong>"), "列表内粗体");
  ok(html.includes("<ul><li>签收后"), "列表");
  ok(html.includes('<td style="text-align:center">30 天</td>'), "表格对齐");
  ok(html.includes("<blockquote>"), "引用");
  ok(html.includes("<code>我的订单 -&gt; 售后</code>"), "行内代码转义");
  ok(html.includes('<a href="https://example.com/help"'), "链接");
});

// ---------- 汇总 ----------

const total = passed + failures.length;
if (failures.length) {
  console.error(`\n✗ ${failures.length}/${total} 个用例失败：\n`);
  for (const f of failures) console.error(`  ✗ ${f.name}\n    ${f.msg}`);
  process.exit(1);
}
console.log(`✓ markdown 渲染器测试全部通过（${total} 个用例）`);
