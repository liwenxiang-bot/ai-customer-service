// Styles injected into the widget's shadow root so host-page CSS can't leak in or out.
export function buildStyles(themeColor: string): string {
  return `
:host { all: initial; }
* { box-sizing: border-box; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif; }

.acs-root { --acs-primary: ${themeColor}; --acs-radius: 14px; }

/* Launcher button */
.acs-launcher {
  position: fixed; right: 24px; bottom: 24px; width: 60px; height: 60px;
  border-radius: 50%; background: var(--acs-primary); color: #fff; border: none;
  cursor: pointer; box-shadow: 0 6px 20px rgba(0,0,0,.22); z-index: 2147483000;
  display: flex; align-items: center; justify-content: center; transition: transform .15s ease;
}
.acs-launcher:hover { transform: scale(1.06); }
.acs-launcher svg { width: 28px; height: 28px; }

/* Panel */
.acs-panel {
  position: fixed; right: 24px; bottom: 96px; width: 384px; height: 600px; max-height: calc(100vh - 120px);
  background: var(--acs-bg); border-radius: 16px; box-shadow: 0 12px 48px rgba(0,0,0,.24);
  display: flex; flex-direction: column; overflow: hidden; z-index: 2147483000;
  border: 1px solid var(--acs-border);
}
.acs-fullscreen .acs-panel {
  position: fixed; inset: 0; width: 100%; height: 100%; max-height: none; border-radius: 0;
  box-shadow: none; border: none;   /* 全屏时去掉悬浮窗那圈 1px 边框 */
  margin: 0 auto; max-width: 820px;
}

/* Theme tokens */
.acs-light { --acs-bg:#fff; --acs-border:#eceef2; --acs-text:#1f2430; --acs-sub:#8a93a3;
  --acs-bot-bubble:#f3f4f7; --acs-user-bubble:var(--acs-primary); --acs-input-bg:#fff; }
.acs-dark { --acs-bg:#1b1e27; --acs-border:#2c313d; --acs-text:#e7e9ee; --acs-sub:#9aa3b2;
  --acs-bot-bubble:#262b36; --acs-user-bubble:var(--acs-primary); --acs-input-bg:#222631; }

/* Header */
.acs-header { display:flex; align-items:center; gap:10px; padding:14px 16px; background:var(--acs-primary); color:#fff; }
.acs-header img { width:28px; height:28px; border-radius:8px; object-fit:cover; background:rgba(255,255,255,.2); }
.acs-header .acs-title { font-weight:600; font-size:15px; flex:1; }
.acs-header .acs-status { font-size:11px; opacity:.85; }
.acs-header button { background:transparent; border:none; color:#fff; cursor:pointer; opacity:.85; padding:4px; }
.acs-header button:hover { opacity:1; }

/* Messages */
.acs-messages { flex:1; min-height:0; overflow-y:auto; padding:16px; background:var(--acs-bg); display:flex; flex-direction:column; gap:12px; }
.acs-msg { display:flex; flex-direction:column; max-width:84%; }
.acs-msg.user { align-self:flex-end; align-items:flex-end; }
.acs-msg.assistant { align-self:flex-start; align-items:flex-start; }
.acs-bubble { padding:10px 13px; border-radius:var(--acs-radius); font-size:14px; line-height:1.6; word-break:break-word; }
.acs-msg.assistant .acs-bubble { background:var(--acs-bot-bubble); color:var(--acs-text); border-top-left-radius:4px; }
.acs-msg.user .acs-bubble { background:var(--acs-user-bubble); color:#fff; border-top-right-radius:4px; }
.acs-bubble p { margin:0 0 8px; } .acs-bubble p:last-child { margin:0; }
.acs-bubble ul,.acs-bubble ol { margin:6px 0; padding-left:20px; }
.acs-bubble pre { background:rgba(0,0,0,.08); padding:10px; border-radius:8px; overflow-x:auto; font-size:12.5px; }
.acs-bubble code { font-family: ui-monospace, Menlo, Consolas, monospace; font-size:12.5px; }
.acs-bubble a { color:var(--acs-primary); }
.acs-msg.user .acs-bubble a { color:#fff; text-decoration:underline; }

/* Tool status / typing */
.acs-tool { font-size:12px; color:var(--acs-sub); display:flex; align-items:center; gap:6px; padding:2px 4px; }
.acs-dots span { display:inline-block; width:6px; height:6px; margin:0 1px; background:var(--acs-sub); border-radius:50%; animation:acs-bounce 1.2s infinite; }
.acs-dots span:nth-child(2){animation-delay:.2s} .acs-dots span:nth-child(3){animation-delay:.4s}
@keyframes acs-bounce { 0%,80%,100%{transform:scale(.6);opacity:.4} 40%{transform:scale(1);opacity:1} }

/* Citations */
.acs-citations { margin-top:6px; display:flex; flex-wrap:wrap; gap:6px; }
.acs-cite { font-size:11px; color:var(--acs-sub); background:var(--acs-bot-bubble); border:1px solid var(--acs-border); border-radius:10px; padding:2px 8px; }

/* Feedback */
.acs-fb { display:flex; gap:8px; margin-top:6px; }
.acs-fb button { background:transparent; border:none; cursor:pointer; color:var(--acs-sub); padding:2px; font-size:13px; border-radius:6px; }
.acs-fb button:hover { background:var(--acs-bot-bubble); }
.acs-fb button.active { color:var(--acs-primary); }

/* Escalation banner */
.acs-escalation { font-size:12.5px; background:#fff7ed; color:#9a3412; border:1px solid #fed7aa; border-radius:10px; padding:8px 12px; }

/* Input */
.acs-input { border-top:1px solid var(--acs-border); padding:10px 12px; background:var(--acs-bg); display:flex; gap:8px; align-items:flex-end; }
.acs-input textarea { flex:1; resize:none; border:1px solid var(--acs-border); border-radius:12px; padding:9px 12px; font-size:16px;
  background:var(--acs-input-bg); color:var(--acs-text); outline:none; max-height:120px; line-height:1.5; }
.acs-input textarea:focus { border-color:var(--acs-primary); }
.acs-send { background:var(--acs-primary); border:none; color:#fff; width:38px; height:38px; border-radius:50%; cursor:pointer; flex-shrink:0; display:flex; align-items:center; justify-content:center; }
.acs-send:disabled { opacity:.45; cursor:default; }
.acs-powered { text-align:center; font-size:10.5px; color:var(--acs-sub); padding:0 0 8px; background:var(--acs-bg); }
.acs-msg-meta { font-size:10.5px; color:var(--acs-sub); margin-top:3px; }
.acs-retry { color:#dc2626; cursor:pointer; }

@media (max-width:480px) {
  .acs-panel { right:0; bottom:0; width:100vw; height:100vh; max-height:none; border-radius:0; }
  .acs-launcher { right:16px; bottom:16px; }
}
`;
}
