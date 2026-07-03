import { Component, Fragment } from "preact";
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

// 消息数组软上限：超出后砍掉最旧的，顶部以系统行提示「更早消息已省略」，
// 防止长会话下内存与重渲染成本无限增长（更早内容仍可靠重连时的 history 回填找回）。
const MAX_MESSAGES = 300;
function capMessages(list: Msg[]): Msg[] {
  if (list.length <= MAX_MESSAGES) return list;
  return [
    { id: "truncated", role: "assistant", content: "更早消息已省略", system: true },
    ...list.slice(list.length - MAX_MESSAGES),
  ];
}

export function App({ config }: { config: WidgetConfig }) {
  const fullscreen = config.mode === "fullscreen";
  const b = config.branding;
  const [open, setOpen] = useState(fullscreen);
  const [theme, setTheme] = useState(config.branding.default_theme || "light");
  const [messages, setMessages] = useState<Msg[]>([]);
  const [input, setInput] = useState("");
  const [status, setStatus] = useState<"connecting" | "online" | "offline" | "blocked">("connecting");
  const [busy, setBusy] = useState(false);
  const [pending, setPending] = useState<Pending[]>([]);
  const [escalated, setEscalated] = useState(false);
  const [ended, setEnded] = useState(false);
  const [showRate, setShowRate] = useState(false);
  const [rating, setRating] = useState(0);
  const [rateNote, setRateNote] = useState("");
  const [copiedId, setCopiedId] = useState<string | null>(null);
  const [agentTyping, setAgentTyping] = useState(false);
  const [unread, setUnread] = useState(0); // 面板收起期间收到的消息数（launcher 角标）
  const [showJump, setShowJump] = useState(false); // 用户上翻后显示「回到底部」
  const [hasNew, setHasNew] = useState(false); // 上翻期间有新内容到达
  const [fbAck, setFbAck] = useState(false); // 「已收到反馈」轻提示

  const clientRef = useRef<ChatClient | null>(null);
  const typingHideRef = useRef<number | null>(null); // auto-hides the "客服正在输入" indicator
  const lastTypingSentRef = useRef(0); // throttles our own outbound typing pings
  const channelKey = config.channelKey;
  const uid = useRef(uidFor(channelKey));
  const sessionKey = `acs_session_${channelKey}`;
  const listRef = useRef<HTMLDivElement>(null);
  const taRef = useRef<HTMLTextAreaElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const stoppedRef = useRef<Set<string>>(new Set());
  const atBottomRef = useRef(true); // 用户是否停留在消息列表底部附近（±40px）
  const openRef = useRef(open); // handleEvent 只绑定一次，读面板开合状态走 ref
  const statusRef = useRef(status); // 同上：doSend 的离线降级判断走 ref，避免记忆化行拿到旧闭包
  const busyRef = useRef(busy); // retry 的 busy 门控走 ref（记忆化行的回调闭包可能过期）
  const httpTurnRef = useRef(0); // 进行中的 HTTP 降级轮数：>0 时 WS 生命周期事件不得抢先 setBusy(false)
  const pendingAckRef = useRef<Map<string, number>>(new Map()); // client_msg_id → ACK 超时定时器
  const lastFbRef = useRef<{ id: string; prev: Msg["feedback"] } | null>(null); // 最近一次反馈，供 ack 失败回滚
  const fbAckTimerRef = useRef<number | null>(null);
  const launcherRef = useRef<HTMLButtonElement>(null);
  const endBtnRef = useRef<HTMLButtonElement>(null);
  const rateCardRef = useRef<HTMLDivElement>(null);
  const wasOpenRef = useRef(false); // 焦点交接只在真实开合后发生，首次挂载不抢宿主页焦点
  const rateWasOpenRef = useRef(false);

  useEffect(() => { statusRef.current = status; }, [status]);
  useEffect(() => { busyRef.current = busy; }, [busy]);
  useEffect(() => {
    openRef.current = open;
    if (open) setUnread(0); // 打开面板即视为已读
  }, [open]);

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
    return () => {
      client.destroy(); // 移除全局监听器与计时器，避免宿主反复挂载时泄漏
      pendingAckRef.current.forEach((t) => clearTimeout(t));
      pendingAckRef.current.clear();
    };
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

  // 仅当用户位于底部附近时才跟随滚动；上翻阅读不被打断（behavior:auto 为瞬时，避免流式期间动画堆积）
  useEffect(() => {
    const el = listRef.current;
    if (el && atBottomRef.current) el.scrollTo({ top: el.scrollHeight, behavior: "auto" });
  }, [open, messages, pending, agentTyping]);

  // 上翻期间有新内容到达 → 点亮「回到底部」按钮上的提示
  useEffect(() => {
    if (!atBottomRef.current && messages.length > 0) setHasNew(true);
  }, [messages]);

  function onListScroll() {
    const el = listRef.current;
    if (!el) return;
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
    atBottomRef.current = nearBottom;
    setShowJump(!nearBottom);
    if (nearBottom) setHasNew(false);
  }
  function jumpToBottom() {
    const el = listRef.current;
    if (el) el.scrollTo({ top: el.scrollHeight, behavior: "auto" });
  }

  // 面板开合的焦点交接：打开聚焦输入框，收起归还给悬浮球
  useEffect(() => {
    if (open) {
      wasOpenRef.current = true;
      taRef.current?.focus();
    } else if (wasOpenRef.current) {
      wasOpenRef.current = false;
      launcherRef.current?.focus();
    }
  }, [open]);

  // 评价弹窗的焦点交接：打开移入第一个可聚焦按钮，关闭归还「结束会话」按钮
  useEffect(() => {
    if (showRate) {
      rateWasOpenRef.current = true;
      (rateCardRef.current?.querySelector("button") as HTMLButtonElement | null)?.focus();
    } else if (rateWasOpenRef.current) {
      rateWasOpenRef.current = false;
      endBtnRef.current?.focus();
    }
  }, [showRate]);

  function handleEvent(ev: ServerEvent) {
    switch (ev.type) {
      case "connected":
        setStatus("online");
        break;
      case "_reconnecting":
        setStatus("connecting");
        // 断流复位：message_end 已随旧连接消亡，不复位会让输入框永远卡在「停止生成」态。
        // 例外：HTTP 降级轮进行中（httpTurnRef>0）——它不走 WS，连接抖动不影响其在途请求，
        // busy 归 sendViaHttp 的 finally 收尾，这里抢先解锁会让用户在回复未到时重复发问。
        if (httpTurnRef.current === 0) setBusy(false);
        interruptStreaming();
        break;
      case "_offline":
        // 重连连续失败：进入离线态（头部显示离线 + 手动重试，发送走 HTTP 降级）
        setStatus("offline");
        if (httpTurnRef.current === 0) setBusy(false);
        interruptStreaming();
        break;
      case "_blocked":
        // 服务端策略性拒绝（4403 等）：停止重连，提示站点不可用
        setStatus("blocked");
        if (httpTurnRef.current === 0) setBusy(false);
        interruptStreaming();
        break;
      case "history": {
        // Status notices (转人工 / 人工接入 / 人工结束) are ephemeral real-time events that
        // aren't persisted, so a refresh/reconnect would lose them. Reconstruct the current
        // one from session.status and append it as the trailing system line.
        if (ev.messages?.length) {
          const msgs: Msg[] = ev.messages.map((m: any) => ({
            id: m.id,
            role: m.role,
            content: m.content,
            citations: m.citations,
            attachments: m.attachments,
            feedback: m.feedback,
            fromHuman: m.from_human,
            ts: m.created_at ? Date.parse(m.created_at) : undefined,
            status: "sent",
          }));
          // 服务端回传的 client_msg_id（发送时随 user_message 上送并持久化到消息 meta）：
          // 本地乐观气泡的 id 就是当初的 client_msg_id，据此识别「同一条消息」。
          // 已被服务端收录的，顺手清掉它的 ACK 超时定时器——消息已确认落库，不该再被标失败。
          const seenClient = new Set<string>();
          for (const m of ev.messages as any[]) {
            if (!m.client_msg_id) continue;
            seenClient.add(m.client_msg_id);
            const timer = pendingAckRef.current.get(m.client_msg_id);
            if (timer !== undefined) {
              clearTimeout(timer);
              pendingAckRef.current.delete(m.client_msg_id);
            }
          }
          const notice = statusNotice(ev.status);
          if (notice) msgs.push({ id: "sys-" + ev.status, role: "assistant", content: notice, system: true });
          // 合并而非整体替换：服务端消息为主体，欢迎语固定在头部，尾部保留三类本地气泡：
          // 1) sending —— 已发出但尚未收到 received ACK 的乐观气泡；
          // 2) failed  —— 发送失败等待重试的气泡，重试按钮不能因重连回填而丢失（文本未送达，
          //    丢了气泡用户输入就彻底没了）；
          // 3) streaming 的 assistant 气泡 —— WS 流式中或 HTTP 降级(rest-)占位，回复还在路上。
          // 其中 1)/2) 若已被服务端覆盖（同 DB id，或回传的 client_msg_id 等于本地 id），
          // 说明消息已落库：本地乐观气泡让位于服务端版本（替换，不并存也不丢弃）。
          setMessages((prev) => {
            const seen = new Set(msgs.map((m) => m.id));
            const head = prev.filter((m) => m.id === "welcome" && !seen.has(m.id));
            const tail = prev.filter((m) => {
              if (seen.has(m.id) || seenClient.has(m.id)) return false; // 已由服务端版本替换
              if (m.role === "user") return m.status === "sending" || m.status === "failed";
              return !!m.streaming; // 进行中的流式回复 / rest- 占位
            });
            return [...head, ...msgs, ...tail];
          });
        }
        // 重建即新一轮起点，残留的 WS 生成态作废；但 HTTP 降级轮不依赖 WS、可能正在途中，
        // 它的 busy 由 sendViaHttp 的 finally 收尾，这里不能抢先解锁。
        if (httpTurnRef.current === 0) setBusy(false);
        // Restore handoff UI state so the 转人工 button stays hidden while a human is engaged.
        setEscalated(ev.status === "escalated" || ev.status === "human_takeover");
        if (ev.status === "closed") setEnded(true);
        break;
      }
      case "human_takeover":
        setEscalated(true); // proactive operator takeover (no prior 转人工) must hide the button too
        if (ev.message) appendSystem(ev.message);
        break;
      case "ai_resumed":
      case "human_ended":
        setEscalated(false);
        clearAgentTyping();
        if (ev.message) appendSystem(ev.message);
        break;
      case "agent_typing":
        showAgentTyping();
        break;
      case "escalated":
        setEscalated(true);
        if (ev.message) appendSystem(ev.message);
        break;
      case "session_ended":
        setEnded(true);
        clearAgentTyping();
        break;
      case "human_message":
        clearAgentTyping(); // the message itself supersedes the "正在输入" hint
        if (!openRef.current) setUnread((n) => n + 1);
        setMessages((prev) => capMessages([
          ...prev,
          { id: ev.message_id || "h-" + Date.now(), role: "assistant", content: ev.content, fromHuman: true, ts: Date.now() },
        ]));
        break;
      case "received":
        // 入队即回的 ACK（AI 轮与人工接管分支都会发）：按 client_msg_id 精确置 sent
        ackUser(ev.client_msg_id);
        break;
      case "message_start":
        setBusy(true);
        if (!openRef.current) setUnread((n) => n + 1);
        setMessages((prev) => capMessages([
          ...prev,
          { id: ev.turn_id, role: "assistant", content: "", streaming: true, ts: Date.now() },
        ]));
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
      case "feedback_ack":
        // 后端确认反馈写入：失败回滚乐观更新，成功给一个「已收到反馈」轻提示
        if (ev.ok === false) {
          const last = lastFbRef.current;
          if (last) setMessages((prev) => prev.map((m) => (m.id === last.id ? { ...m, feedback: last.prev } : m)));
        } else {
          setFbAck(true);
          if (fbAckTimerRef.current) clearTimeout(fbAckTimerRef.current);
          fbAckTimerRef.current = window.setTimeout(() => setFbAck(false), 2000);
        }
        lastFbRef.current = null;
        break;
      case "rate_limited":
        failStreaming(); // 终结流式占位气泡，避免点点动画与错误提示同屏永驻
        appendSystem(ev.message || "请求过于频繁，请稍后再试。");
        setBusy(false);
        break;
      case "error":
        failStreaming();
        appendSystem(ev.message || "服务暂时不可用。");
        setBusy(false);
        break;
    }
  }

  /** received ACK 的精确匹配：按 client_msg_id（即本地气泡 id）置 sent，并清除这条消息
   *  自己的超时定时器——不再用「最后一条 sending」启发式，快速连发/重试插队都不会错标。
   *  晚到的 ACK 也能把已被超时误标 failed 的气泡改回已发出，避免用户重试造成重复。 */
  function ackUser(clientMsgId?: string) {
    if (!clientMsgId) return;
    const timer = pendingAckRef.current.get(clientMsgId);
    if (timer !== undefined) {
      clearTimeout(timer);
      pendingAckRef.current.delete(clientMsgId);
    }
    setMessages((prev) =>
      prev.map((m) =>
        m.id === clientMsgId && m.role === "user" && m.status !== "sent" ? { ...m, status: "sent" as const } : m
      )
    );
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
    setMessages((prev) => capMessages([...prev, { id: "sys-" + Date.now(), role: "assistant", content: text, system: true, ts: Date.now() }]));
  }
  /** 连接中断时收尾半截的流式气泡：停掉动画并标注「已中断」，后续 chunk 一律丢弃。
   *  rest- 占位豁免：HTTP 降级轮不走 WS，连接抖动不影响其在途请求，留给 sendViaHttp 收尾。 */
  function interruptStreaming() {
    setMessages((prev) =>
      prev.map((m) => {
        if (!m.streaming || m.id.startsWith("rest-")) return m;
        stoppedRef.current.add(m.id);
        const content = m.content ? m.content + "\n\n*（回答已中断）*" : "（回答已中断）";
        return { ...m, streaming: false, toolLabel: undefined, content };
      })
    );
  }
  /** error/rate_limited 时收尾流式占位：空气泡直接移除（错误提示紧随其后），有内容的停掉动画。
   *  rest- 占位同样豁免：HTTP 降级轮的失败由 sendViaHttp 的 catch 自己处理。 */
  function failStreaming() {
    setMessages((prev) =>
      prev
        .filter((m) => !(m.streaming && !m.content && !m.id.startsWith("rest-")))
        .map((m) => {
          if (!m.streaming || m.id.startsWith("rest-")) return m;
          stoppedRef.current.add(m.id);
          return { ...m, streaming: false, toolLabel: undefined };
        })
    );
  }

  // ---- typing indicators ----
  function clearAgentTyping() {
    if (typingHideRef.current) { clearTimeout(typingHideRef.current); typingHideRef.current = null; }
    setAgentTyping(false);
  }
  // "客服正在输入" pings arrive only while typing; no reliable "stopped" signal, so self-expire.
  function showAgentTyping() {
    setAgentTyping(true);
    if (typingHideRef.current) clearTimeout(typingHideRef.current);
    typingHideRef.current = window.setTimeout(() => setAgentTyping(false), 4000);
  }
  // Tell a watching operator we're typing — only once a human is engaged, throttled to ~2.5s.
  function maybeSendTyping() {
    if (!escalated) return;
    const now = Date.now();
    if (now - lastTypingSentRef.current < 2500) return;
    lastTypingSentRef.current = now;
    clientRef.current?.sendTyping();
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
  /** reuseId：重试时复用原 client_msg_id（消息 id 即 client_msg_id），服务端按其幂等去重。 */
  function doSend(text: string, atts: Attachment[], reuseId?: string) {
    const client = clientRef.current!;
    const id = reuseId || "u-" + Date.now() + "-" + Math.random().toString(36).slice(2, 6);
    atBottomRef.current = true; // 自己发的消息总是滚回底部并恢复跟随
    setMessages((prev) => capMessages([
      ...prev,
      { id, role: "user", content: text, attachments: atts.length ? atts : undefined, status: "sending", ts: Date.now() },
    ]));
    if (statusRef.current === "offline") {
      // WS 连不上时降级走 HTTP：无流式，整段返回（后台仍在低频重试 WS）
      void sendViaHttp(id, text, atts);
      return;
    }
    if (!client.isOpen()) client.connect();
    client.sendMessage(text, atts, id); // queues if still connecting, flushes exactly once on open
    // ACK 超时：约 10s 内没等到这条消息自己的 received 就标记失败，并撤掉可能还在排队的副本。
    // 定时器按 client_msg_id 入 Map，received/history 按 id 精确清除，多条并发互不串扰。
    const prevTimer = pendingAckRef.current.get(id);
    if (prevTimer !== undefined) clearTimeout(prevTimer);
    const timer = window.setTimeout(() => {
      pendingAckRef.current.delete(id);
      client.cancelQueued(id);
      setMessages((prev) => prev.map((m) => (m.id === id && m.status === "sending" ? { ...m, status: "failed" } : m)));
    }, 10000);
    pendingAckRef.current.set(id, timer);
  }

  /** HTTP 降级发送：占位气泡等待整段回复，成功即收尾，失败标记重试。
   *  httpTurnRef 标记「HTTP 轮进行中」：期间 WS 侧的 _reconnecting/_offline/history 等
   *  生命周期事件不得 setBusy(false)——这一轮的 busy 归属本函数，finally 里自己收尾，
   *  否则输入框会在回复未到时提前解锁，用户重复发问、回复错位。 */
  async function sendViaHttp(id: string, text: string, atts: Attachment[]) {
    const client = clientRef.current!;
    const holderId = "rest-" + Date.now();
    httpTurnRef.current += 1;
    setBusy(true);
    setMessages((prev) => capMessages([...prev, { id: holderId, role: "assistant", content: "", streaming: true, ts: Date.now() }]));
    try {
      const data = await client.sendMessageHttp(text, atts);
      if (data.session_id) localStorage.setItem(sessionKey, data.session_id);
      setMessages((prev) => {
        // 回复已先一步经 history 回填（同 DB id）：丢弃占位，避免同一条回复双份渲染
        if (data.message_id && prev.some((m) => m.id === data.message_id)) {
          return prev
            .filter((m) => m.id !== holderId)
            .map((m) => (m.id === id ? { ...m, status: "sent" as const } : m));
        }
        const holderAlive = prev.some((m) => m.id === holderId);
        const next = prev.map((m) => {
          if (m.id === id) return { ...m, status: "sent" as const };
          if (m.id === holderId)
            return { ...m, id: data.message_id || holderId, content: data.reply || "", streaming: false, citations: data.citations };
          return m;
        });
        if (holderAlive) return next;
        // 双保险：请求期间 WS 恢复拉 history（或软上限截断）可能移走了占位气泡——
        // 找不到 holderId 就改为追加，整段回复不能静默丢弃。
        return capMessages([
          ...next,
          { id: data.message_id || holderId, role: "assistant" as const, content: data.reply || "", citations: data.citations, ts: Date.now() },
        ]);
      });
      if (data.escalation) setEscalated(true);
    } catch {
      setMessages((prev) =>
        prev
          .filter((m) => m.id !== holderId)
          .map((m) => (m.id === id ? { ...m, status: "failed" as const } : m))
      );
    } finally {
      httpTurnRef.current -= 1;
      if (httpTurnRef.current === 0) setBusy(false);
    }
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
    // 通知服务端取消生成（回 message_end{stopped:true} 走正常收尾）；本地立即终结，不等确认
    clientRef.current?.stopGeneration();
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

  /** 重试失败消息：移除原气泡，携带原附件复用原 client_msg_id 重发（服务端幂等去重，
   *  若原消息其实已送达则不会被回答两次）。busy 期间按钮已禁用，这里再以 ref 兜底。 */
  function retry(m: Msg) {
    if (busyRef.current) return;
    setMessages((prev) => prev.filter((x) => x.id !== m.id));
    doSend(m.content, m.attachments || [], m.id);
  }

  function feedback(m: Msg, kind: "up" | "down", note = "") {
    if (!m.id || m.id.startsWith("welcome") || m.id.startsWith("sys-")) return;
    lastFbRef.current = { id: m.id, prev: m.feedback ?? null }; // 供 feedback_ack ok=false 时回滚
    clientRef.current?.sendFeedback(m.id, kind, note);
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
    if (e.isComposing || e.keyCode === 229) return; // 输入法选词回车不发送（keyCode 229 兜底旧 Safari）
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  }

  // Esc 逐层退出：先关评价弹窗，再收起面板（fullscreen 无面板可收）
  function onPanelKey(e: KeyboardEvent) {
    if (e.key !== "Escape") return;
    if (showRate) {
      setShowRate(false);
      e.stopPropagation();
    } else if (!fullscreen) {
      setOpen(false);
      e.stopPropagation();
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
        <button ref={launcherRef} class="acs-launcher" onClick={() => setOpen(true)} aria-label={unread > 0 ? `打开客服，${unread} 条未读消息` : "打开客服"}>
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M21 11.5a8.38 8.38 0 0 1-8.5 8.5 8.5 8.5 0 0 1-3.8-.9L3 21l1.9-5.7a8.5 8.5 0 1 1 16.1-3.8Z" />
          </svg>
          {unread > 0 && <span class="acs-badge">{unread > 99 ? "99+" : unread}</span>}
        </button>
      )}

      {open && (
        <div class="acs-panel" onKeyDown={onPanelKey}>
          <div class="acs-header">
            {b.logo_url ? <img src={b.logo_url} alt="" /> : null}
            <div class="acs-title">
              {b.brand_name}
              <div class={"acs-status" + (status === "online" ? "" : " offline")}>
                {status === "online" ? "在线"
                  : status === "connecting" ? "连接中…"
                  : status === "blocked" ? "当前站点暂不可用"
                  : <>离线 · <button type="button" class="acs-status-retry" onClick={() => clientRef.current?.retryNow()}>重试</button></>}
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

          <div class="acs-body">
            <div class="acs-messages" ref={listRef} onScroll={onListScroll} role="log" aria-live="polite" aria-label="对话消息">
              {messages.map((m, i) => {
                const prev = messages[i - 1];
                const sep =
                  m.ts && (i === 0 ? dayKey(m.ts) !== dayKey(Date.now()) : prev?.ts && dayKey(m.ts) !== dayKey(prev.ts))
                    ? dayLabel(m.ts!)
                    : null;
                return (
                  <MessageRow
                    key={m.id}
                    m={m}
                    sep={sep}
                    copied={copiedId === m.id}
                    retryDisabled={m.status === "failed" && busy}
                    onRetry={retry}
                    onFeedback={feedback}
                    onCopy={copyMsg}
                  />
                );
              })}

              {agentTyping && (
                <div class="acs-msg assistant">
                  <div class="acs-human-label">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2" /><circle cx="12" cy="7" r="4" /></svg>
                    人工客服
                  </div>
                  <div class="acs-bubble acs-typing"><span class="acs-dots"><span></span><span></span><span></span></span></div>
                </div>
              )}

              {showSuggest && (
                <div class="acs-suggest">
                  {suggestions.map((q) => (
                    <button class="acs-suggest-btn" onClick={() => askSuggested(q)}>{q}</button>
                  ))}
                </div>
              )}
            </div>

            {showJump && (
              <button type="button" class="acs-jump" onClick={jumpToBottom} aria-label="回到底部">
                {hasNew && <span class="acs-jump-new">有新消息</span>}
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 5v14" /><path d="m19 12-7 7-7-7" /></svg>
              </button>
            )}
            {fbAck && <div class="acs-fb-ack" role="status">已收到反馈</div>}
          </div>

          {ended ? (
            <div class="acs-ended">
              本次会话已结束，感谢你的咨询。<button type="button" onClick={restart}>开始新会话</button>
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
                  <button ref={endBtnRef} class="acs-quick-btn danger" onClick={() => setShowRate(true)} title="结束会话">
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
                      <button type="button" class="acs-pend-x" onClick={() => removePending(p.id)} aria-label="移除附件" title="移除">
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6 6 18M6 6l12 12" /></svg>
                      </button>
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
                  onInput={(e) => { setInput((e.target as HTMLTextAreaElement).value); autoGrow(e.target as HTMLTextAreaElement); maybeSendTyping(); }}
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
              <div class="acs-rate-card" ref={rateCardRef} role="dialog" aria-modal="true" aria-label="结束会话评价">
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

interface RowProps {
  m: Msg;
  sep: string | null; // 日期分隔条文案（无则不渲染）
  copied: boolean;
  retryDisabled: boolean; // busy（生成中/HTTP 轮进行中）时禁用重试，避免与进行中的回合抢队
  onRetry: (m: Msg) => void;
  onFeedback: (m: Msg, kind: "up" | "down", note?: string) => void;
  onCopy: (m: Msg) => void;
}

/** 按消息对象引用记忆化：消息更新走不可变替换，引用不变即跳过重渲染——
 *  流式期间只有正在增长的那条会重新解析 markdown，历史消息不再逐 chunk 重算。
 *  回调有意不参与比较（它们只依赖 ref/setter，行为始终最新）。 */
class MessageRow extends Component<RowProps> {
  shouldComponentUpdate(next: RowProps) {
    // retryDisabled 仅对 failed 气泡取 busy 值（其余恒为 false），busy 翻转不会重渲染全列表
    return (
      next.m !== this.props.m ||
      next.sep !== this.props.sep ||
      next.copied !== this.props.copied ||
      next.retryDisabled !== this.props.retryDisabled
    );
  }
  render() {
    return <MessageBody {...this.props} />;
  }
}

function MessageBody({ m, sep, copied, retryDisabled, onRetry, onFeedback, onCopy }: RowProps) {
  // 👎 后的可选原因输入（行内局部状态，消息定稿换 id 重挂载时自然丢弃）
  const [noteOpen, setNoteOpen] = useState(false);
  const [note, setNote] = useState("");
  const noteRef = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (noteOpen) noteRef.current?.focus();
  }, [noteOpen]);

  function submitNote() {
    const t = note.trim();
    if (t) onFeedback(m, "down", t); // 👎 已即时上报，这里补发带原因的一次
    setNoteOpen(false);
    setNote("");
  }

  return (
    <Fragment>
      {sep && <div class="acs-date-sep"><span>{sep}</span></div>}
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
                  <button key={i} type="button" class="acs-att-imgbtn" onClick={() => window.open(a.url, "_blank", "noopener,noreferrer")} aria-label={`查看图片 ${a.name || ""}`}>
                    <img class="acs-att-img" src={a.url} alt={a.name || ""} loading="lazy" />
                  </button>
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
              <button onClick={() => onCopy(m)} title="复制" aria-label="复制">
                {copied ? (
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5" /></svg>
                ) : (
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2" /><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" /></svg>
                )}
              </button>
              <button class={m.feedback === "up" ? "active" : ""} onClick={() => { onFeedback(m, "up"); setNoteOpen(false); }} title="有帮助" aria-label="有帮助">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M7 10v12" /><path d="M15 5.88 14 10h5.83a2 2 0 0 1 1.92 2.56l-2.33 8A2 2 0 0 1 17.5 22H4a2 2 0 0 1-2-2v-8a2 2 0 0 1 2-2h2.76a2 2 0 0 0 1.79-1.11L12 2a3.13 3.13 0 0 1 3 3.88Z" /></svg>
              </button>
              <button class={m.feedback === "down" ? "active" : ""} onClick={() => { onFeedback(m, "down"); setNoteOpen(true); }} title="没帮助" aria-label="没帮助">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 14V2" /><path d="M9 18.12 10 14H4.17a2 2 0 0 1-1.92-2.56l2.33-8A2 2 0 0 1 6.5 2H20a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2h-2.76a2 2 0 0 0-1.79 1.11L12 22a3.13 3.13 0 0 1-3-3.88Z" /></svg>
              </button>
            </div>
          )}
          {noteOpen && (
            <div class="acs-fb-note">
              <input
                ref={noteRef}
                value={note}
                placeholder="愿意说说原因吗？（可选）"
                aria-label="反馈原因"
                onInput={(e) => setNote((e.target as HTMLInputElement).value)}
                onKeyDown={(e) => {
                  if (e.isComposing || e.keyCode === 229) return;
                  if (e.key === "Enter") submitNote();
                }}
              />
              <button type="button" onClick={submitNote}>发送</button>
              <button type="button" class="acs-fb-note-skip" onClick={() => { setNoteOpen(false); setNote(""); }}>跳过</button>
            </div>
          )}
          {m.role === "user" && m.status === "failed" && (
            <div class="acs-msg-meta">
              发送失败 · <button type="button" class="acs-retry" disabled={retryDisabled} title={retryDisabled ? "请等当前回复结束后再重试" : undefined} onClick={() => onRetry(m)}>重试</button>
            </div>
          )}
          {m.ts && !m.streaming && <div class="acs-time">{fmtTime(m.ts)}</div>}
        </div>
      )}
    </Fragment>
  );
}

function escapeText(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/\n/g, "<br/>");
}

/** Current-state notice reconstructed from session.status on (re)connect — keeps the
 *  handoff banner alive across refresh, since these notices aren't persisted as messages. */
function statusNotice(status?: string): string | null {
  switch (status) {
    case "escalated": return "已为你转接人工，我们会尽快安排同事跟进。";
    case "human_takeover": return "人工客服已接入，正在为你服务~";
    case "human_handled": return "本次人工服务已结束，感谢咨询。";
    default: return null;
  }
}
