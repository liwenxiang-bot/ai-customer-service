import { Fragment } from "preact";
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
    file_upload_enabled?: boolean;
    suggested_questions?: string[];
  };
}

interface Attachment {
  url: string;
  name: string;
  content_type: string;
  size: number;
  kind: "image" | "file";
}

interface Pending extends Partial<Attachment> {
  id: string;
  name: string;
  uploading: boolean;
  error?: boolean;
  preview?: string; // object URL for image preview while uploading
}

interface Msg {
  id: string;
  role: "user" | "assistant";
  content: string;
  citations?: any[];
  attachments?: Attachment[];
  feedback?: "up" | "down" | null;
  status?: "sending" | "sent" | "failed";
  streaming?: boolean;
  toolLabel?: string;
  escalated?: boolean;
  fromHuman?: boolean;
  system?: boolean; // status notices (takeover joined / AI resumed / ended) — rendered centered
  ts?: number; // epoch ms; absent for the greeting
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

function isImage(a: { kind?: string; content_type?: string }): boolean {
  return a.kind === "image" || (a.content_type || "").startsWith("image/");
}

const pad2 = (n: number) => (n < 10 ? "0" + n : "" + n);
function fmtTime(ts?: number): string {
  if (!ts) return "";
  const d = new Date(ts);
  return `${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
}
function dayKey(ts: number): string {
  const d = new Date(ts);
  return `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`;
}
function dayLabel(ts: number): string {
  const d = new Date(ts);
  const now = new Date();
  const days = Math.round(
    (new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime() -
      new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime()) / 86400000
  );
  if (days === 0) return "今天";
  if (days === 1) return "昨天";
  if (d.getFullYear() === now.getFullYear()) return `${d.getMonth() + 1}月${d.getDate()}日`;
  return `${d.getFullYear()}年${d.getMonth() + 1}月${d.getDate()}日`;
}

export function App({ config }: { config: WidgetConfig }) {
  const fullscreen = config.mode === "fullscreen";
  const b = config.branding;
  const [open, setOpen] = useState(fullscreen);
  const [theme, setTheme] = useState(config.branding.default_theme || "light");
  const [messages, setMessages] = useState<Msg[]>([]);
  const [input, setInput] = useState("");
  const [status, setStatus] = useState<"connecting" | "online" | "offline">("connecting");
  const [busy, setBusy] = useState(false);
  const [pending, setPending] = useState<Pending[]>([]);
  const [escalated, setEscalated] = useState(false);
  const [ended, setEnded] = useState(false);
  const [showRate, setShowRate] = useState(false);
  const [rating, setRating] = useState(0);
  const [rateNote, setRateNote] = useState("");
  const [copiedId, setCopiedId] = useState<string | null>(null);

  const clientRef = useRef<ChatClient | null>(null);
  const channelKey = config.channelKey;
  const uid = useRef(uidFor(channelKey));
  const sessionKey = `acs_session_${channelKey}`;
  const listRef = useRef<HTMLDivElement>(null);
  const taRef = useRef<HTMLTextAreaElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const stoppedRef = useRef<Set<string>>(new Set());

  const autoGrow = (el: HTMLTextAreaElement | null) => {
    if (!el) return;
    el.style.height = "auto";
    el.style.height = Math.min(el.scrollHeight, 120) + "px";
  };

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
    if (open && messages.length === 0 && config.branding.welcome_message) {
      setMessages([{ id: "welcome", role: "assistant", content: config.branding.welcome_message }]);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  useEffect(() => {
    const el = listRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages, pending]);

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
              attachments: m.attachments,
              feedback: m.feedback,
              fromHuman: m.from_human,
              ts: m.created_at ? Date.parse(m.created_at) : undefined,
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
      case "escalated":
        setEscalated(true);
        if (ev.message) appendSystem(ev.message);
        break;
      case "session_ended":
        setEnded(true);
        break;
      case "human_message":
        setMessages((prev) => [
          ...prev,
          { id: ev.message_id || "h-" + Date.now(), role: "assistant", content: ev.content, fromHuman: true, ts: Date.now() },
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
          { id: ev.turn_id, role: "assistant", content: "", streaming: true, ts: Date.now() },
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
        setEscalated(true);
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
    if (stoppedRef.current.has(turnId)) return; // user stopped this turn
    setMessages((prev) =>
      prev.map((m) => (m.id === turnId ? { ...m, content: m.content + (delta || "") } : m))
    );
  }
  function updateStreaming(turnId: string, fn: (m: Msg) => Msg) {
    if (stoppedRef.current.has(turnId)) return;
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
    setMessages((prev) => [...prev, { id: "sys-" + Date.now(), role: "assistant", content: text, system: true, ts: Date.now() }]);
  }

  // ---- attachments ----
  function onPickFiles(e: Event) {
    const files = Array.from((e.target as HTMLInputElement).files || []);
    if (fileRef.current) fileRef.current.value = "";
    for (const file of files) {
      const id = "p-" + Math.random().toString(36).slice(2);
      const preview = file.type.startsWith("image/") ? URL.createObjectURL(file) : undefined;
      setPending((prev) => [...prev, { id, name: file.name, uploading: true, preview }]);
      clientRef.current!
        .uploadFile(file)
        .then((d: Attachment) =>
          setPending((prev) => prev.map((p) => (p.id === id ? { ...p, ...d, uploading: false } : p)))
        )
        .catch(() =>
          setPending((prev) => prev.map((p) => (p.id === id ? { ...p, uploading: false, error: true } : p)))
        );
    }
  }
  function removePending(id: string) {
    setPending((prev) => prev.filter((p) => p.id !== id));
  }

  // ---- sending ----
  function doSend(text: string, atts: Attachment[]) {
    const client = clientRef.current!;
    setMessages((prev) => [
      ...prev,
      { id: "u-" + Date.now(), role: "user", content: text, attachments: atts.length ? atts : undefined, status: "sending", ts: Date.now() },
    ]);
    if (!client.isOpen()) client.connect(() => client.sendMessage(text, atts));
    else client.sendMessage(text, atts);
  }

  function send() {
    if (busy) return;
    const text = input.trim();
    const ready = pending.filter((p) => p.url && !p.error);
    if (!text && ready.length === 0) return;
    if (pending.some((p) => p.uploading)) return; // wait for uploads
    const atts: Attachment[] = ready.map((p) => ({
      url: p.url!, name: p.name, content_type: p.content_type!, size: p.size!, kind: p.kind!,
    }));
    doSend(text, atts);
    setInput("");
    setPending([]);
    if (taRef.current) taRef.current.style.height = "auto";
  }

  function askSuggested(q: string) {
    if (busy) return;
    doSend(q, []);
  }

  function stop() {
    setMessages((prev) =>
      prev.map((m) => {
        if (m.streaming) {
          stoppedRef.current.add(m.id);
          return { ...m, streaming: false, toolLabel: undefined };
        }
        return m;
      })
    );
    setBusy(false);
  }

  function retry(text: string) {
    setInput(text);
  }

  function feedback(m: Msg, kind: "up" | "down") {
    if (!m.id || m.id.startsWith("welcome") || m.id.startsWith("sys-")) return;
    clientRef.current?.sendFeedback(m.id, kind);
    setMessages((prev) => prev.map((x) => (x.id === m.id ? { ...x, feedback: kind } : x)));
  }

  function copyMsg(m: Msg) {
    navigator.clipboard?.writeText(m.content);
    setCopiedId(m.id);
    setTimeout(() => setCopiedId((c) => (c === m.id ? null : c)), 1500);
  }

  function requestHuman() {
    clientRef.current?.requestHuman();
  }

  function finishEnd() {
    setShowRate(false);
    setEnded(true);
    localStorage.removeItem(sessionKey);
  }
  function submitRate() {
    clientRef.current?.endSession(rating, rateNote.trim());
    finishEnd();
  }
  function skipRate() {
    clientRef.current?.endSession(0, "");
    finishEnd();
  }
  function restart() {
    setEnded(false);
    setEscalated(false);
    setRating(0);
    setRateNote("");
    setPending([]);
    if (clientRef.current) clientRef.current.sessionId = "";
    setMessages(config.branding.welcome_message
      ? [{ id: "welcome", role: "assistant", content: config.branding.welcome_message }]
      : []);
  }

  function onKey(e: KeyboardEvent) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  }

  const rootClass = `acs-root acs-${theme}` + (fullscreen ? " acs-fullscreen" : "");
  const started = messages.some((m) => m.role === "user");
  const suggestions = b.suggested_questions || [];
  const showSuggest = !ended && !started && suggestions.length > 0;
  const canSend = (!!input.trim() || pending.some((p) => p.url && !p.error)) && !pending.some((p) => p.uploading);

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
              {theme === "light" ? (
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z" /></svg>
              ) : (
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4" /><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41" /></svg>
              )}
            </button>
            {!fullscreen && (
              <button onClick={() => setOpen(false)} aria-label="关闭" title="收起">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6 6 18M6 6l12 12" /></svg>
              </button>
            )}
          </div>

          <div class="acs-messages" ref={listRef} role="log" aria-live="polite" aria-label="对话消息">
            {messages.map((m, i) => {
              const prev = messages[i - 1];
              const sep =
                m.ts && (i === 0 ? dayKey(m.ts) !== dayKey(Date.now()) : prev?.ts && dayKey(m.ts) !== dayKey(prev.ts));
              return (
              <Fragment key={m.id}>
                {sep && <div class="acs-date-sep"><span>{dayLabel(m.ts!)}</span></div>}
                {m.system ? (
                  <div class="acs-system"><span>{m.content}</span></div>
                ) : (
                <div class={`acs-msg ${m.role}`}>
                {m.escalated && <div class="acs-escalation">🔔 已为你转接人工，稍后会有同事跟进。</div>}
                {m.fromHuman && (
                  <div class="acs-human-label">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2" /><circle cx="12" cy="7" r="4" /></svg>
                    人工客服
                  </div>
                )}
                {(m.role === "assistant" || m.content) && (
                  <div
                    class="acs-bubble"
                    dangerouslySetInnerHTML={{
                      __html: m.role === "assistant" ? renderMarkdown(m.content || "") : escapeText(m.content),
                    }}
                  />
                )}
                {m.attachments && m.attachments.length > 0 && (
                  <div class="acs-atts">
                    {m.attachments.map((a, i) =>
                      isImage(a) ? (
                        <img key={i} class="acs-att-img" src={a.url} alt={a.name || ""} loading="lazy" onClick={() => window.open(a.url, "_blank")} />
                      ) : (
                        <a key={i} class="acs-att-file" href={a.url} target="_blank" rel="noopener noreferrer">
                          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" /><path d="M14 2v6h6" /></svg>
                          <span class="acs-att-name">{a.name || "文件"}</span>
                        </a>
                      )
                    )}
                  </div>
                )}
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
                    <button onClick={() => copyMsg(m)} title="复制" aria-label="复制">
                      {copiedId === m.id ? (
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5" /></svg>
                      ) : (
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2" /><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" /></svg>
                      )}
                    </button>
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
                {m.ts && !m.streaming && <div class="acs-time">{fmtTime(m.ts)}</div>}
                </div>
                )}
              </Fragment>
              );
            })}

            {showSuggest && (
              <div class="acs-suggest">
                {suggestions.map((q) => (
                  <button class="acs-suggest-btn" onClick={() => askSuggested(q)}>{q}</button>
                ))}
              </div>
            )}
          </div>

          {ended ? (
            <div class="acs-ended">
              本次会话已结束，感谢你的咨询。<a onClick={restart}>开始新会话</a>
            </div>
          ) : (
            <>
              {started && (
                <div class="acs-quick">
                  {!escalated && (
                    <button class="acs-quick-btn" onClick={requestHuman} title="转接人工客服">
                      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2" /><circle cx="12" cy="7" r="4" /></svg>
                      转人工
                    </button>
                  )}
                  <button class="acs-quick-btn danger" onClick={() => setShowRate(true)} title="结束会话">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" /><path d="m16 17 5-5-5-5" /><path d="M21 12H9" /></svg>
                    结束会话
                  </button>
                </div>
              )}

              {pending.length > 0 && (
                <div class="acs-pending">
                  {pending.map((p) => (
                    <div class={"acs-pend" + (p.uploading ? " uploading" : "")} key={p.id}>
                      {p.preview ? <img src={p.preview} alt="" /> : (
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" /><path d="M14 2v6h6" /></svg>
                      )}
                      <span class="acs-pend-name">{p.error ? "上传失败" : p.name}</span>
                      <span class="acs-pend-x" onClick={() => removePending(p.id)} aria-label="移除">
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6 6 18M6 6l12 12" /></svg>
                      </span>
                    </div>
                  ))}
                </div>
              )}

              <div class="acs-input">
                {b.file_upload_enabled !== false && (
                  <>
                    <input
                      ref={fileRef}
                      type="file"
                      multiple
                      accept="image/png,image/jpeg,image/webp,image/gif,.pdf,.csv,.txt,.doc,.docx,.xls,.xlsx"
                      style="display:none"
                      onChange={onPickFiles}
                    />
                    <button class="acs-attach" onClick={() => fileRef.current?.click()} aria-label="上传文件" title="上传图片或文件">
                      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m21.44 11.05-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48" /></svg>
                    </button>
                  </>
                )}
                <textarea
                  ref={taRef}
                  rows={1}
                  aria-label="输入消息"
                  placeholder={b.placeholder}
                  value={input}
                  onInput={(e) => { setInput((e.target as HTMLTextAreaElement).value); autoGrow(e.target as HTMLTextAreaElement); }}
                  onKeyDown={onKey}
                />
                {busy ? (
                  <button class="acs-send acs-stop" onClick={stop} aria-label="停止生成" title="停止生成">
                    <svg viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="6" width="12" height="12" rx="2.5" /></svg>
                  </button>
                ) : (
                  <button class="acs-send" disabled={!canSend} onClick={send} aria-label="发送">
                    <svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor"><path d="M2 21l21-9L2 3v7l15 2-15 2v7z" /></svg>
                  </button>
                )}
              </div>
              {b.show_powered_by && <div class="acs-powered">AI 智能客服 · 由大模型驱动</div>}
            </>
          )}

          {showRate && (
            <div class="acs-rate-overlay" onClick={(e) => { if (e.target === e.currentTarget) setShowRate(false); }}>
              <div class="acs-rate-card">
                <h4>结束会话</h4>
                <p>这次的服务体验如何？</p>
                <div class="acs-stars">
                  {[1, 2, 3, 4, 5].map((n) => (
                    <button class={"acs-star" + (n <= rating ? " on" : "")} onClick={() => setRating(n)} aria-label={`${n} 星`}>
                      <svg viewBox="0 0 24 24" fill={n <= rating ? "currentColor" : "none"} stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01L12 2z" /></svg>
                    </button>
                  ))}
                </div>
                <textarea class="acs-rate-note" rows={2} placeholder="留下你的建议（可选）" value={rateNote} onInput={(e) => setRateNote((e.target as HTMLTextAreaElement).value)} />
                <div class="acs-rate-actions">
                  <button class="acs-rate-skip" onClick={skipRate}>直接结束</button>
                  <button class="acs-rate-submit" onClick={submitRate}>提交评价</button>
                </div>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function escapeText(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/\n/g, "<br/>");
}
