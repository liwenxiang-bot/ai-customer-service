// Styles injected into the widget's shadow root so host-page CSS can't leak in or out.
// Design language (see CLAUDE.md · 聊天 widget): light, friendly, single-focus, host-
// overridable. Brand color + radius are exposed as CSS custom properties so each
// merchant can re-skin it. Bubbles 18px, container 14px, soft shadows, generous breathing
// room, gentle motion (reduced-motion respected).
export function buildStyles(themeColor: string): string {
  return `
:host { all: initial; }
* { box-sizing: border-box; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif; }

.acs-root {
  --acs-primary: ${themeColor};
  --acs-radius: 14px;        /* container radius */
  --acs-bubble-radius: 18px; /* message bubble radius */
  /* tints derived from the merchant's brand color */
  --acs-primary-soft: color-mix(in srgb, var(--acs-primary) 12%, transparent);
  --acs-on-primary: #fff;
}

/* ----------------------------------------------------------------- launcher */
.acs-launcher {
  position: fixed; right: 24px; bottom: 24px; width: 60px; height: 60px;
  border-radius: 50%; border: none; cursor: pointer; z-index: 2147483000;
  background: var(--acs-primary); color: var(--acs-on-primary);
  box-shadow: 0 10px 28px -6px color-mix(in srgb, var(--acs-primary) 55%, transparent),
              0 4px 10px rgba(17,24,39,.16);
  display: flex; align-items: center; justify-content: center;
  transition: transform .18s cubic-bezier(.2,.7,.2,1), box-shadow .18s ease;
}
.acs-launcher:hover { transform: translateY(-2px) scale(1.04); }
.acs-launcher:active { transform: scale(.97); }
.acs-launcher:focus-visible { outline: 3px solid var(--acs-primary-soft); outline-offset: 3px; }
.acs-launcher svg { width: 27px; height: 27px; }
/* 未读角标：面板收起期间收到消息时点亮，打开即清零 */
.acs-badge { position:absolute; top:-3px; right:-3px; min-width:19px; height:19px; padding:0 5px; border-radius:99px;
  background:#dc2626; color:#fff; font-size:11px; font-weight:650; line-height:19px; text-align:center;
  box-shadow:0 0 0 2px #fff; }
/* one gentle attention pulse ring */
.acs-launcher::after {
  content: ""; position: absolute; inset: 0; border-radius: 50%;
  box-shadow: 0 0 0 0 color-mix(in srgb, var(--acs-primary) 45%, transparent);
  animation: acs-pulse 2.6s ease-out infinite;
}
@keyframes acs-pulse {
  0% { box-shadow: 0 0 0 0 color-mix(in srgb, var(--acs-primary) 40%, transparent); }
  70%,100% { box-shadow: 0 0 0 14px transparent; }
}

/* ----------------------------------------------------------------- panel */
.acs-panel {
  position: fixed; right: 24px; bottom: 96px; width: 388px; height: 620px;
  max-height: calc(100vh - 120px);
  background: var(--acs-bg); border-radius: var(--acs-radius); overflow: hidden;
  display: flex; flex-direction: column; z-index: 2147483000;
  border: 1px solid var(--acs-border);
  box-shadow: 0 24px 60px -12px rgba(17,24,39,.28), 0 8px 20px -8px rgba(17,24,39,.16);
  animation: acs-panel-in .26s cubic-bezier(.2,.7,.2,1) both;
}
@keyframes acs-panel-in { from { opacity:0; transform: translateY(14px) scale(.985); } to { opacity:1; transform:none; } }
.acs-fullscreen .acs-panel {
  /* Center a max-860 column reliably (left+transform avoids the inset/margin over-
     constraint that pins fixed elements to one side). Full-width on phones. */
  position: fixed; top: 0; bottom: 0; left: 50%; transform: translateX(-50%);
  width: 100%; max-width: 860px; height: 100%; max-height: none;
  border-radius: 0; box-shadow: none; border: none; margin: 0; animation: none;
}

/* theme tokens（--acs-sub 需在白底与气泡底 #f4f5f8 上均 ≥4.5:1，满足 WCAG AA） */
.acs-light { --acs-bg:#fff; --acs-border:#ebedf1; --acs-text:#1b1f27; --acs-sub:#667082;
  --acs-bot-bubble:#f4f5f8; --acs-user-bubble:var(--acs-primary); --acs-input-bg:#fff; --acs-composer-bg:#fff; }
.acs-dark { --acs-bg:#14161c; --acs-border:#2a2f3b; --acs-text:#e7e9ee; --acs-sub:#9aa3b2;
  --acs-bot-bubble:#222732; --acs-user-bubble:var(--acs-primary); --acs-input-bg:#1c1f28; --acs-composer-bg:#171a21; }

/* ----------------------------------------------------------------- header */
.acs-header {
  display:flex; align-items:center; gap:11px; padding:14px 16px; color:var(--acs-on-primary);
  background: linear-gradient(135deg,
    color-mix(in srgb, var(--acs-primary) 94%, white),
    color-mix(in srgb, var(--acs-primary) 82%, black));
}
.acs-header img { width:30px; height:30px; border-radius:9px; object-fit:cover; background:rgba(255,255,255,.22); flex-shrink:0; }
.acs-header .acs-title { font-weight:650; font-size:15px; line-height:1.2; flex:1; min-width:0; letter-spacing:.2px; }
.acs-header .acs-status { font-size:11px; font-weight:450; opacity:.92; display:flex; align-items:center; gap:5px; margin-top:3px; letter-spacing:.3px; }
.acs-header .acs-status::before {
  content:""; width:6px; height:6px; border-radius:50%; background:#3ddc84;
  box-shadow:0 0 0 0 rgba(61,220,132,.6); animation: acs-presence 2s ease-out infinite;
}
.acs-status.offline::before { background:#cbd2dc; animation:none; }
.acs-status .acs-status-retry { background:none; border:none; padding:0; width:auto; height:auto; display:inline;
  color:inherit; font:inherit; opacity:1; cursor:pointer; text-decoration:underline; text-underline-offset:2px; font-weight:550; }
.acs-status .acs-status-retry:hover { background:none; }
@keyframes acs-presence { 0%{box-shadow:0 0 0 0 rgba(61,220,132,.55);} 70%,100%{box-shadow:0 0 0 6px transparent;} }
.acs-header button { background:rgba(255,255,255,.0); border:none; color:var(--acs-on-primary); cursor:pointer; opacity:.85;
  width:30px; height:30px; border-radius:8px; font-size:15px; display:flex; align-items:center; justify-content:center; transition:background .15s, opacity .15s; }
.acs-header button:hover { opacity:1; background:rgba(255,255,255,.16); }
.acs-header button:focus-visible { outline:2px solid rgba(255,255,255,.7); outline-offset:1px; }
.acs-header button svg { width:17px; height:17px; }

/* ----------------------------------------------------------------- messages */
/* 消息区外层：给「回到底部」悬浮钮与反馈回执提供定位锚点 */
.acs-body { flex:1; min-height:0; position:relative; display:flex; flex-direction:column; }
/* 不用 scroll-behavior:smooth——流式期间高频程序化滚动叠加平滑动画会追不上内容 */
.acs-messages { flex:1; min-height:0; overflow-y:auto; padding:18px 16px 8px; background:var(--acs-bg);
  display:flex; flex-direction:column; gap:14px; }
.acs-messages::-webkit-scrollbar { width:7px; }
.acs-messages::-webkit-scrollbar-thumb { background:color-mix(in srgb, var(--acs-sub) 35%, transparent); border-radius:99px; }
.acs-msg { display:flex; flex-direction:column; max-width:86%; animation: acs-rise .3s cubic-bezier(.2,.7,.2,1) both; }
.acs-msg.user { align-self:flex-end; align-items:flex-end; }
.acs-msg.assistant { align-self:flex-start; align-items:flex-start; }
@keyframes acs-rise { from { opacity:0; transform: translateY(7px); } to { opacity:1; transform:none; } }

.acs-bubble { padding:11px 14px; border-radius:var(--acs-bubble-radius); font-size:14.5px; line-height:1.62; word-break:break-word; }
.acs-msg.assistant .acs-bubble { background:var(--acs-bot-bubble); color:var(--acs-text); border-bottom-left-radius:6px; }
.acs-msg.user .acs-bubble { background:var(--acs-user-bubble); color:var(--acs-on-primary); border-bottom-right-radius:6px;
  box-shadow:0 4px 12px -4px color-mix(in srgb, var(--acs-primary) 50%, transparent); }
.acs-bubble p { margin:0 0 8px; } .acs-bubble p:last-child { margin:0; }
.acs-bubble ul,.acs-bubble ol { margin:6px 0; padding-left:20px; } .acs-bubble li { margin:2px 0; }
.acs-bubble pre { background:rgba(0,0,0,.07); padding:10px 12px; border-radius:10px; overflow-x:auto; font-size:12.5px; margin:6px 0; }
.acs-bubble code { font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace; font-size:12.5px; }
.acs-bubble pre code { background:none; padding:0; }
.acs-bubble a { color:var(--acs-primary); font-weight:500; }
.acs-msg.user .acs-bubble a { color:#fff; text-decoration:underline; }
.acs-bubble strong { font-weight:650; }

/* tool status / typing */
.acs-tool { font-size:12.5px; color:var(--acs-sub); display:flex; align-items:center; gap:7px; padding:6px 4px 2px; }
/* "客服正在输入" bubble — bot bubble whose only content is the animated dots */
.acs-bubble.acs-typing { display:inline-flex; align-items:center; padding:13px 15px; }
.acs-dots { display:inline-flex; gap:3px; }
.acs-dots span { width:6px; height:6px; background:var(--acs-sub); border-radius:50%; opacity:.5; animation:acs-bounce 1.3s infinite ease-in-out; }
.acs-dots span:nth-child(2){animation-delay:.18s} .acs-dots span:nth-child(3){animation-delay:.36s}
@keyframes acs-bounce { 0%,80%,100%{transform:translateY(0);opacity:.4} 40%{transform:translateY(-4px);opacity:1} }

/* citations */
.acs-citations { margin-top:7px; display:flex; flex-wrap:wrap; gap:6px; }
.acs-cite { font-size:11px; color:var(--acs-sub); background:var(--acs-bot-bubble); border:1px solid var(--acs-border);
  border-radius:99px; padding:3px 9px; line-height:1.4; max-width:100%; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }

/* feedback — quiet, reveals on hover/keyboard focus (desktop); subtle but tappable on touch */
.acs-fb { display:flex; gap:4px; margin-top:6px; opacity:0; transition:opacity .18s ease; }
.acs-msg.assistant:hover .acs-fb,
.acs-msg.assistant:focus-within .acs-fb { opacity:1; }
.acs-fb button { background:transparent; border:none; cursor:pointer; color:var(--acs-sub);
  width:26px; height:26px; border-radius:7px; display:flex; align-items:center; justify-content:center; transition:background .15s, color .15s, transform .12s; }
.acs-fb button svg { width:15px; height:15px; }
.acs-fb button:hover { background:var(--acs-bot-bubble); color:var(--acs-text); }
.acs-fb button:active { transform:scale(.88); }
.acs-fb button.active { color:var(--acs-primary); background:var(--acs-primary-soft); }
.acs-fb button:focus-visible { outline:2px solid var(--acs-primary-soft); outline-offset:1px; }
@media (hover:none) { .acs-fb { opacity:.55; } }  /* touch: keep visible but understated */

/* 👎 后的可选原因输入 */
.acs-fb-note { display:flex; gap:6px; margin-top:6px; align-items:center; }
.acs-fb-note input { width:190px; max-width:100%; border:1px solid var(--acs-border); border-radius:9px; padding:5px 9px;
  font-size:12px; background:var(--acs-input-bg); color:var(--acs-text); outline:none; }
.acs-fb-note input::placeholder { color:var(--acs-sub); }
.acs-fb-note input:focus { border-color:var(--acs-primary); }
.acs-fb-note button { background:none; border:none; padding:2px 4px; border-radius:6px; font-size:12px;
  color:var(--acs-primary); cursor:pointer; font-weight:550; }
.acs-fb-note button:focus-visible { outline:2px solid var(--acs-primary-soft); outline-offset:1px; }
.acs-fb-note .acs-fb-note-skip { color:var(--acs-sub); }
/* 反馈回执轻提示 */
.acs-fb-ack { position:absolute; bottom:52px; left:50%; transform:translateX(-50%); pointer-events:none;
  background:color-mix(in srgb, #0b0e13 78%, transparent); color:#fff; font-size:12px; padding:6px 12px;
  border-radius:99px; animation: acs-fade .2s ease both; }

/* human-agent handoff identity（浅色用深绿保证白底 ≥4.5:1；深色底亮绿即达标） */
.acs-human-label { font-size:11.5px; color:#0b7a56; font-weight:600; margin-bottom:4px; display:flex; align-items:center; gap:5px; letter-spacing:.2px; }
.acs-dark .acs-human-label { color:#0f9d6e; }
.acs-human-label svg { width:13px; height:13px; }
.acs-escalation { font-size:12.5px; background:color-mix(in srgb, #f59e0b 12%, var(--acs-bg)); color:#9a6207;
  border:1px solid color-mix(in srgb, #f59e0b 28%, transparent); border-radius:12px; padding:9px 12px; line-height:1.5; }
.acs-dark .acs-escalation { color:#fbbf24; }

/* sent-status / retry */
.acs-msg-meta { font-size:10.5px; color:var(--acs-sub); margin-top:4px; }
.acs-retry { background:none; border:none; padding:0; font:inherit; color:#dc2626; cursor:pointer; font-weight:500; }
.acs-retry:disabled { opacity:.45; cursor:not-allowed; }  /* busy 期间禁用，等当前回复结束 */
.acs-retry:focus-visible { outline:2px solid var(--acs-primary-soft); outline-offset:1px; border-radius:4px; }
.acs-dark .acs-retry { color:#f87171; }  /* #dc2626 在深色底不足 4.5:1 */

/* per-message time (mono, tabular) + day separator */
.acs-time { margin-top:3px; padding:0 3px; font-size:10.5px; color:var(--acs-sub); letter-spacing:.2px;
  font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace; font-variant-numeric: tabular-nums; }
.acs-date-sep { align-self:center; margin:6px 0 2px; }
.acs-date-sep span { font-size:11px; color:var(--acs-sub); background:var(--acs-bot-bubble);
  padding:3px 11px; border-radius:99px; letter-spacing:.3px; }

/* centered system status line:  ——— 人工客服已接入 ——— */
.acs-system { align-self:stretch; display:flex; align-items:center; gap:10px; margin:8px 6px; }
.acs-system::before, .acs-system::after { content:""; flex:1; height:1px; background:var(--acs-border); }
.acs-system span { font-size:11.5px; color:var(--acs-text); opacity:.7; white-space:nowrap; letter-spacing:.2px; }

/* 「回到底部」悬浮钮（用户上翻时出现；上翻期间有新消息则点亮提示） */
.acs-jump { position:absolute; bottom:12px; left:50%; transform:translateX(-50%); display:flex; align-items:center; gap:6px;
  background:var(--acs-bg); color:var(--acs-text); border:1px solid var(--acs-border); border-radius:99px;
  padding:6px 12px; font-size:12px; cursor:pointer; box-shadow:0 8px 20px -8px rgba(17,24,39,.35); }
.acs-jump:hover { background:var(--acs-bot-bubble); }
.acs-jump:focus-visible { outline:2px solid var(--acs-primary-soft); outline-offset:1px; }
.acs-jump svg { width:14px; height:14px; }
.acs-jump-new { color:var(--acs-primary); font-weight:600; }

/* ----------------------------------------------------------------- composer */
.acs-input { border-top:1px solid var(--acs-border); padding:11px 12px; background:var(--acs-composer-bg);
  display:flex; gap:9px; align-items:flex-end; }
.acs-input textarea { flex:1; resize:none; border:1.5px solid var(--acs-border); border-radius:13px; padding:10px 13px;
  font-size:16px; line-height:1.5; background:var(--acs-input-bg); color:var(--acs-text); outline:none; max-height:120px;
  transition:border-color .15s ease, box-shadow .15s ease; }
.acs-input textarea::placeholder { color:var(--acs-sub); }
.acs-input textarea:focus { border-color:var(--acs-primary); box-shadow:0 0 0 3px var(--acs-primary-soft); }
.acs-send { background:var(--acs-primary); border:none; color:var(--acs-on-primary); width:40px; height:40px; border-radius:50%;
  cursor:pointer; flex-shrink:0; display:flex; align-items:center; justify-content:center; transition:transform .14s, opacity .15s, box-shadow .15s;
  box-shadow:0 4px 12px -3px color-mix(in srgb, var(--acs-primary) 50%, transparent); }
.acs-send:hover:not(:disabled) { transform:scale(1.06); }
.acs-send:active:not(:disabled) { transform:scale(.94); }
.acs-send:disabled { opacity:.4; cursor:default; box-shadow:none; }
.acs-send:focus-visible { outline:3px solid var(--acs-primary-soft); outline-offset:2px; }
.acs-powered { text-align:center; font-size:10.5px; color:var(--acs-sub); padding:0 0 9px; background:var(--acs-composer-bg); letter-spacing:.2px; }

/* ----------------------------------------------------------------- suggested questions */
.acs-suggest { display:flex; flex-direction:column; gap:7px; align-items:flex-start; margin-top:2px; }
.acs-suggest-btn { text-align:left; background:var(--acs-bg); color:var(--acs-primary); cursor:pointer; line-height:1.4; max-width:100%;
  border:1px solid color-mix(in srgb, var(--acs-primary) 32%, var(--acs-border)); border-radius:13px; padding:8px 13px; font-size:13px;
  transition:background .15s, transform .12s; }
.acs-suggest-btn:hover { background:var(--acs-primary-soft); }
.acs-suggest-btn:active { transform:scale(.98); }

/* ----------------------------------------------------------------- attachments in bubbles */
.acs-atts { display:flex; flex-wrap:wrap; gap:8px; margin-top:7px; }
.acs-msg.user .acs-atts { justify-content:flex-end; }
/* 图片附件放大入口用原生 button 包裹，保证键盘可达 */
.acs-att-imgbtn { background:none; border:none; padding:0; cursor:pointer; width:150px; max-width:62%; border-radius:12px; display:block; }
.acs-att-imgbtn:focus-visible { outline:2px solid var(--acs-primary-soft); outline-offset:2px; }
.acs-att-img { width:100%; border-radius:12px; border:1px solid var(--acs-border); display:block; object-fit:cover; }
.acs-att-file { display:flex; align-items:center; gap:8px; text-decoration:none; background:var(--acs-bot-bubble); color:var(--acs-text);
  border:1px solid var(--acs-border); border-radius:12px; padding:9px 12px; font-size:13px; max-width:240px; }
.acs-att-file svg { width:18px; height:18px; flex-shrink:0; color:var(--acs-primary); }
.acs-att-name { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }

/* ----------------------------------------------------------------- quick actions (transfer / end) */
.acs-quick { display:flex; gap:8px; padding:8px 12px 8px; background:var(--acs-composer-bg); }
.acs-quick-btn { background:transparent; border:1px solid var(--acs-border); color:var(--acs-sub); border-radius:99px;
  padding:5px 11px; font-size:12px; cursor:pointer; display:flex; align-items:center; gap:5px;
  transition:background .15s, color .15s, border-color .15s; }
.acs-quick-btn svg { width:13px; height:13px; }
.acs-quick-btn:hover { color:var(--acs-text); background:var(--acs-bot-bubble); }
.acs-quick-btn.danger:hover { color:#dc2626; border-color:color-mix(in srgb,#dc2626 40%, var(--acs-border)); }
.acs-quick-btn:focus-visible { outline:2px solid var(--acs-primary-soft); outline-offset:1px; }

/* ----------------------------------------------------------------- composer attach + pending */
.acs-attach { background:transparent; border:none; color:var(--acs-sub); width:40px; height:40px; border-radius:50%; cursor:pointer;
  flex-shrink:0; display:flex; align-items:center; justify-content:center; transition:background .15s, color .15s; }
.acs-attach:hover { background:var(--acs-bot-bubble); color:var(--acs-primary); }
.acs-attach svg { width:20px; height:20px; }
.acs-attach:focus-visible { outline:2px solid var(--acs-primary-soft); outline-offset:1px; }
.acs-emoji-btn.on { color:var(--acs-primary); background:var(--acs-bot-bubble); }
.acs-emoji { background:var(--acs-composer-bg); border-top:1px solid var(--acs-border); padding:6px 10px 8px; }
.acs-emoji-tabs { display:flex; gap:4px; margin-bottom:6px; overflow-x:auto; }
.acs-emoji-tab { background:none; border:none; padding:4px 10px; border-radius:999px; font-size:12px; color:var(--acs-sub);
  cursor:pointer; white-space:nowrap; }
.acs-emoji-tab.on { background:var(--acs-primary-soft); color:var(--acs-primary); font-weight:600; }
.acs-emoji-tab:focus-visible, .acs-emoji-item:focus-visible { outline:2px solid var(--acs-primary-soft); outline-offset:1px; }
.acs-emoji-grid { display:grid; grid-template-columns:repeat(8, 1fr); gap:2px; max-height:172px; overflow-y:auto;
  overscroll-behavior:contain; }
.acs-emoji-item { background:none; border:none; border-radius:8px; font-size:22px; line-height:1; padding:6px 0; cursor:pointer;
  font-family:"Apple Color Emoji","Segoe UI Emoji","Noto Color Emoji",sans-serif; }
.acs-emoji-item:hover { background:var(--acs-bot-bubble); }
.acs-pending { display:flex; flex-wrap:wrap; gap:8px; padding:10px 12px 8px; background:var(--acs-composer-bg); }
.acs-pend { position:relative; display:flex; align-items:center; gap:7px; background:var(--acs-bot-bubble); border:1px solid var(--acs-border);
  border-radius:10px; padding:5px 8px; font-size:12px; color:var(--acs-text); max-width:180px; }
.acs-pend img { width:30px; height:30px; border-radius:6px; object-fit:cover; flex-shrink:0; }
.acs-pend > svg { width:22px; height:22px; flex-shrink:0; color:var(--acs-sub); }  /* file placeholder icon */
.acs-pend-name { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.acs-pend-x { background:none; border:none; padding:0; cursor:pointer; color:var(--acs-sub); display:flex; border-radius:4px; }
.acs-pend-x:hover { color:#dc2626; }
.acs-pend-x:focus-visible { outline:2px solid var(--acs-primary-soft); outline-offset:1px; }
.acs-pend-x svg { width:14px; height:14px; }
.acs-pend.uploading { opacity:.55; }

/* stop-generation button (replaces send while streaming) */
.acs-send.acs-stop { background:var(--acs-bot-bubble); color:var(--acs-text); box-shadow:none; }
.acs-send.acs-stop:hover { background:var(--acs-border); transform:none; }

/* ----------------------------------------------------------------- rating / end session */
.acs-rate-overlay { position:absolute; inset:0; background:color-mix(in srgb, #0b0e13 45%, transparent); display:flex;
  align-items:center; justify-content:center; padding:20px; z-index:5; animation: acs-fade .2s ease both; }
@keyframes acs-fade { from{opacity:0} to{opacity:1} }
.acs-rate-card { background:var(--acs-bg); color:var(--acs-text); border-radius:16px; padding:20px 18px; width:100%; max-width:300px;
  box-shadow:0 20px 50px -12px rgba(0,0,0,.4); text-align:center; }
.acs-rate-card h4 { margin:0 0 4px; font-size:15px; font-weight:650; }
.acs-rate-card p { margin:0 0 14px; font-size:12.5px; color:var(--acs-sub); }
.acs-stars { display:flex; justify-content:center; gap:6px; margin-bottom:14px; }
.acs-star { cursor:pointer; color:var(--acs-border); transition:transform .12s, color .12s; background:none; border:none; padding:0; }
.acs-star svg { width:30px; height:30px; }
.acs-star.on { color:#f5b301; }
.acs-star:hover { transform:scale(1.12); }
.acs-rate-note { width:100%; border:1.5px solid var(--acs-border); border-radius:10px; padding:8px 10px; font-size:13px; resize:none;
  background:var(--acs-input-bg); color:var(--acs-text); outline:none; margin-bottom:12px; font-family:inherit; }
.acs-rate-note:focus { border-color:var(--acs-primary); }
.acs-rate-actions { display:flex; gap:8px; }
.acs-rate-actions button { flex:1; border-radius:10px; padding:9px; font-size:13px; cursor:pointer; border:none; transition:opacity .15s; }
.acs-rate-skip { background:var(--acs-bot-bubble); color:var(--acs-sub); }
.acs-rate-submit { background:var(--acs-primary); color:var(--acs-on-primary); font-weight:600; }
.acs-rate-actions button:hover { opacity:.88; }
.acs-ended { text-align:center; font-size:12.5px; color:var(--acs-sub); padding:12px; }
.acs-ended button { background:none; border:none; padding:0; font:inherit; color:var(--acs-primary); cursor:pointer; font-weight:500; }
.acs-ended button:focus-visible { outline:2px solid var(--acs-primary-soft); outline-offset:1px; border-radius:4px; }

/* ----------------------------------------------------------------- responsive */
@media (max-width:480px) {
  .acs-panel { right:0; bottom:0; width:100vw; height:100dvh; max-height:none; border-radius:0; border:none; }
  .acs-launcher { right:16px; bottom:16px; }
  .acs-msg { max-width:90%; }
}

/* ----------------------------------------------------------------- a11y */
@media (prefers-reduced-motion: reduce) {
  .acs-msg, .acs-panel, .acs-rate-overlay, .acs-fb-ack { animation: none; }
  .acs-launcher::after, .acs-header .acs-status::before, .acs-dots span { animation: none; }
  .acs-launcher, .acs-send, .acs-fb button { transition: none; }
}
`;
}
