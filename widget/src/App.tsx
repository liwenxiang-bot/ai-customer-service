import { useEffect, useRef, useState } from "preact/hooks";
import { ChatClient, ServerEvent } from "./client";
import { renderMarkdown } from "./markdown";

export interface WidgetConfig {
  baseUrl: string;
  channelKey: string;
  mode: "popup" | "fullscreen";
  branding: {
    welcome_message: string;
    theme_color: string;
    logo_url: string;
    brand_name: string;
    placeholder: string;
    default_theme: "light" | "dark";
    show_powered_by: boolean;
  };
}

interface Msg {
  id: string;
  role: "user" | "assistant";
  content: string;
  citations?: any[];
  feedback?: "up" | "down" | null;
  status?: "sending" | "sent" | "failed";
  streaming?: boolean;
  toolLabel?: string;
  escalated?: boolean;
  fromHuman?: boolean;
}

function uidFor(channelKey: string): string {
  const key = `acs_uid_${channelKey}`;
  let v = localStorage.getItem(key);
  if (!v) {
    v = "u-" + (crypto.randomUUID?.() || Math.random().toString(36).slice(2) + Date.now());
    localStorage.setItem(key, v);
  }
  return v;
}

export function App({ config }: { config: WidgetConfig }) {
  const fullscreen = config.mode === "fullscreen";
  const [open, setOpen] = useState(fullscreen);
  const [theme, setTheme] = useState(config.branding.default_theme || "light");
  const [messages, setMessages] = useState<Msg[]>([]);
  const [input, setInput] = useState("");
  const [status, setStatus] = useState<"connecting" | "online" | "offline">("connecting");
  const [busy, setBusy] = useState(false);

  const clientRef = useRef<ChatClient | null>(null);
  const channelKey = config.channelKey;
  const uid = useRef(uidFor(channelKey));
  const sessionKey = `acs_session_${channelKey}`;
  const listRef = useRef<HTMLDivElement>(null);

  // ---- connection lifecycle ----
  useEffect(() => {
    const sessionId = localStorage.getItem(sessionKey) || "";
    const client = new ChatClient(config.baseUrl, uid.current, sessionId, channelKey, handleEvent);
    clientRef.current = client;
    if (open) client.connect();
    return () => client.close();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (open && clientRef.current && !clientRef.current.isOpen()) clientRef.current.connect();
  }, [open]);

  useEffect(() => {
    // Greet only when there is no restored transcript.
    if (open && messages.length === 0 && config.branding.welcome_message) {
      setMessages([{ id: "welcome", role: "assistant", content: config.branding.welcome_message }]);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  useEffect(() => {
    const el = listRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages]);

  function handleEvent(ev: ServerEvent) {
    switch (ev.type) {
      case "connected":
        setStatus("online");
        break;
      case "_reconnecting":
        setStatus("connecting");
        break;
      case "history":
        if (ev.messages?.length) {
          setMessages(
            ev.messages.map((m: any) => ({
              id: m.id,
              role: m.role,
              content: m.content,
              citations: m.citations,
              feedback: m.feedback,
              fromHuman: m.from_human,
              status: "sent",
            }))
          );
        }
        break;
      case "human_takeover":
      case "ai_resumed":
      case "human_ended":
        if (ev.message) appendSystem(ev.message);
        break;
      case "human_message":
        setMessages((prev) => [
          ...prev,
          { id: ev.message_id || "h-" + Date.now(), role: "assistant", content: ev.content, fromHuman: true },
        ]);
        break;
      case "received":
        markLastUserSent();
        break;
      case "message_start":
        setBusy(true);
        markLastUserSent();
        setMessages((prev) => [
          ...prev,
          { id: ev.turn_id, role: "assistant", content: "", streaming: true },
        ]);
        break;
      case "stream_chunk":
        appendToStreaming(ev.turn_id, ev.delta);
        break;
      case "tool_status":
        if (ev.status === "running")
          updateStreaming(ev.turn_id, (m) => ({ ...m, toolLabel: ev.label || "正在处理…" }));
        else updateStreaming(ev.turn_id, (m) => ({ ...m, toolLabel: undefined }));
        break;
      case "citations":
        updateStreaming(ev.turn_id, (m) => ({ ...m, citations: ev.citations }));
        break;
      case "escalation":
        updateStreaming(ev.turn_id, (m) => ({ ...m, escalated: true }));
        break;
      case "message_end":
        if (ev.session_id) localStorage.setItem(sessionKey, ev.session_id);
        finalizeStreaming(ev.turn_id, ev.message_id, ev.citations);
        setBusy(false);
        break;
      case "rate_limited":
        appendSystem(ev.message || "请求过于频繁，请稍后再试。");
        setBusy(false);
        break;
      case "error":
        appendSystem(ev.message || "服务暂时不可用。");
        setBusy(false);
        break;
    }
  }

  function markLastUserSent() {
    setMessages((prev) => {
      const copy = [...prev];
      for (let i = copy.length - 1; i >= 0; i--) {
        if (copy[i].role === "user" && copy[i].status === "sending") {
          copy[i] = { ...copy[i], status: "sent" };
          break;
        }
      }
      return copy;
    });
  }

  function appendToStreaming(turnId: string, delta: string) {
    setMessages((prev) =>
      prev.map((m) => (m.id === turnId ? { ...m, content: m.content + (delta || "") } : m))
    );
  }
  function updateStreaming(turnId: string, fn: (m: Msg) => Msg) {
    setMessages((prev) => prev.map((m) => (m.id === turnId ? fn(m) : m)));
  }
  function finalizeStreaming(turnId: string, messageId: string, citations: any[]) {
    setMessages((prev) =>
      prev.map((m) =>
        m.id === turnId
          ? { ...m, id: messageId || turnId, streaming: false, toolLabel: undefined, citations: citations || m.citations }
          : m
      )
    );
  }
  function appendSystem(text: string) {
    setMessages((prev) => [...prev, { id: "sys-" + Date.now(), role: "assistant", content: text }]);
  }

  function send() {
    const text = input.trim();
    if (!text || busy) return;
    const client = clientRef.current!;
    setMessages((prev) => [...prev, { id: "u-" + Date.now(), role: "user", content: text, status: "sending" }]);
    setInput("");
    if (!client.isOpen()) {
      client.connect(() => client.sendMessage(text));
    } else {
      client.sendMessage(text);
    }
  }

  function retry(text: string) {
    setInput(text);
  }

  function feedback(m: Msg, kind: "up" | "down") {
    if (!m.id || m.id.startsWith("welcome") || m.id.startsWith("sys-")) return;
    clientRef.current?.sendFeedback(m.id, kind);
    setMessages((prev) => prev.map((x) => (x.id === m.id ? { ...x, feedback: kind } : x)));
  }

  function onKey(e: KeyboardEvent) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  }

  const b = config.branding;
  const rootClass = `acs-root acs-${theme}` + (fullscreen ? " acs-fullscreen" : "");

  return (
    <div class={rootClass}>
      {!fullscreen && !open && (
        <button class="acs-launcher" onClick={() => setOpen(true)} aria-label="打开客服">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M21 11.5a8.38 8.38 0 0 1-8.5 8.5 8.5 8.5 0 0 1-3.8-.9L3 21l1.9-5.7a8.5 8.5 0 1 1 16.1-3.8Z" />
          </svg>
        </button>
      )}

      {open && (
        <div class="acs-panel">
          <div class="acs-header">
            {b.logo_url ? <img src={b.logo_url} alt="" /> : null}
            <div class="acs-title">
              {b.brand_name}
              <div class={"acs-status" + (status === "online" ? "" : " offline")}>
                {status === "online" ? "在线" : status === "connecting" ? "连接中…" : "离线"}
              </div>
            </div>
            <button onClick={() => setTheme(theme === "light" ? "dark" : "light")} aria-label="切换主题" title="切换主题">
              {theme === "light" ? "🌙" : "☀️"}
            </button>
            {!fullscreen && (
              <button onClick={() => setOpen(false)} aria-label="关闭" title="收起">
                ✕
              </button>
            )}
          </div>

          <div class="acs-messages" ref={listRef} role="log" aria-live="polite" aria-label="对话消息">
            {messages.map((m) => (
              <div class={`acs-msg ${m.role}`} key={m.id}>
                {m.escalated && <div class="acs-escalation">🔔 已为你转接人工，稍后会有同事跟进。</div>}
                {m.fromHuman && <div class="acs-human-label">👤 人工客服</div>}
                <div
                  class="acs-bubble"
                  dangerouslySetInnerHTML={{
                    __html: m.role === "assistant" ? renderMarkdown(m.content || "") : escapeText(m.content),
                  }}
                />
                {m.streaming && m.toolLabel && (
                  <div class="acs-tool">
                    <span class="acs-dots"><span></span><span></span><span></span></span>
                    {m.toolLabel}
                  </div>
                )}
                {m.streaming && !m.toolLabel && !m.content && (
                  <div class="acs-tool"><span class="acs-dots"><span></span><span></span><span></span></span></div>
                )}
                {m.citations && m.citations.length > 0 && (
                  <div class="acs-citations">
                    {m.citations.map((c: any) => (
                      <span class="acs-cite" title={c.snippet || ""}>[{c.ref}] {c.title || "来源"}</span>
                    ))}
                  </div>
                )}
                {m.role === "assistant" && !m.streaming && m.id && !m.id.startsWith("welcome") && !m.id.startsWith("sys-") && (
                  <div class="acs-fb">
                    <button class={m.feedback === "up" ? "active" : ""} onClick={() => feedback(m, "up")} title="有帮助" aria-label="有帮助">
                      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M7 10v12" /><path d="M15 5.88 14 10h5.83a2 2 0 0 1 1.92 2.56l-2.33 8A2 2 0 0 1 17.5 22H4a2 2 0 0 1-2-2v-8a2 2 0 0 1 2-2h2.76a2 2 0 0 0 1.79-1.11L12 2a3.13 3.13 0 0 1 3 3.88Z" /></svg>
                    </button>
                    <button class={m.feedback === "down" ? "active" : ""} onClick={() => feedback(m, "down")} title="没帮助" aria-label="没帮助">
                      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 14V2" /><path d="M9 18.12 10 14H4.17a2 2 0 0 1-1.92-2.56l2.33-8A2 2 0 0 1 6.5 2H20a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2h-2.76a2 2 0 0 0-1.79 1.11L12 22a3.13 3.13 0 0 1-3-3.88Z" /></svg>
                    </button>
                  </div>
                )}
                {m.role === "user" && m.status === "failed" && (
                  <div class="acs-msg-meta">
                    发送失败 · <span class="acs-retry" onClick={() => retry(m.content)}>重试</span>
                  </div>
                )}
              </div>
            ))}
          </div>

          <div class="acs-input">
            <textarea
              rows={1}
              aria-label="输入消息"
              placeholder={b.placeholder}
              value={input}
              onInput={(e) => setInput((e.target as HTMLTextAreaElement).value)}
              onKeyDown={onKey}
            />
            <button class="acs-send" disabled={busy || !input.trim()} onClick={send} aria-label="发送">
              <svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor"><path d="M2 21l21-9L2 3v7l15 2-15 2v7z" /></svg>
            </button>
          </div>
          {b.show_powered_by && <div class="acs-powered">AI 智能客服 · 由大模型驱动</div>}
        </div>
      )}
    </div>
  );
}

function escapeText(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/\n/g, "<br/>");
}
