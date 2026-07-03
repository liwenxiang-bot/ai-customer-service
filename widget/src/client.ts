// WebSocket chat client: streaming, heartbeat, auto-reconnect with backoff,
// and history backfill on reconnect.

export type ServerEvent = {
  type: string;
  [k: string]: any;
};

type Handler = (ev: ServerEvent) => void;

// 连续重连失败达到该次数后进入离线模式（UI 提示离线 + HTTP 降级发送）
const MAX_BACKOFF_ATTEMPTS = 5;
// 离线模式下的低频后台重试间隔；连上后无缝切回 WS
const OFFLINE_RETRY_MS = 60000;

export class ChatClient {
  private ws: WebSocket | null = null;
  private url: string;
  private origin: string;
  private handler: Handler;
  private heartbeat: number | null = null;
  private awaitingPong = false; // a ping is outstanding → if still true next tick, socket is dead
  private hiddenAt = 0; // epoch ms the tab was last backgrounded (0 = currently visible)
  private reconnectAttempts = 0;
  private reconnectTimer: number | null = null;
  private closedByUser = false;
  private blocked = false; // 服务端策略性拒绝（4xxx 关闭码：域名未白名单/渠道停用），不再重连
  private onOpenCb: (() => void) | null = null;
  private pendingSends: Record<string, unknown>[] = []; // queued while offline, flushed once on open

  sessionId: string;
  uid: string;
  channelKey: string;

  // Mobile browsers drop the socket when backgrounded/locked, but a resumed socket often
  // still reports OPEN while silently dead ("zombie") — so it delivers nothing yet never
  // triggers a reconnect. 回前台/网络恢复时不再无条件强杀重建（那会打断正在流式的健康连接，
  // 叠加服务端轮末 commit 甚至丢整轮），改为 probe() 探活：真僵尸由心跳看门狗在下个 tick
  // 重建（onopen 拉 history 回填错过的消息），健康连接不受影响。socket 非 OPEN 时仍立即重连。
  // 命名引用而非匿名函数，destroy() 时才能 removeEventListener，避免实例泄漏。
  private onVisibility = () => {
    if (document.visibilityState === "hidden") {
      this.hiddenAt = Date.now();
      return;
    }
    if (this.closedByUser || this.blocked) return;
    const staleGap = this.hiddenAt && Date.now() - this.hiddenAt > 8000;
    this.hiddenAt = 0;
    if (!this.isOpen()) this.connect();
    else if (staleGap && this.sessionId) this.probe();
  };

  private onOnline = () => {
    if (this.closedByUser || this.blocked) return;
    if (!this.isOpen()) this.connect();
    else if (this.sessionId) this.probe(); // 网络切换可能留下半开连接：探活而非强杀
  };

  constructor(baseUrl: string, uid: string, sessionId: string, channelKey: string, handler: Handler) {
    this.uid = uid;
    this.sessionId = sessionId;
    this.channelKey = channelKey;
    this.handler = handler;
    const wsBase = baseUrl.replace(/^http/, "ws");
    this.url = `${wsBase}/ws/chat?uid=${encodeURIComponent(uid)}&channel_key=${encodeURIComponent(
      channelKey
    )}`;
    this.origin = baseUrl;

    if (typeof document !== "undefined") {
      document.addEventListener("visibilitychange", this.onVisibility);
    }
    if (typeof window !== "undefined") {
      window.addEventListener("online", this.onOnline);
    }
  }

  connect(onOpen?: () => void) {
    if (onOpen) this.onOpenCb = onOpen;
    if (this.blocked) return; // 站点被策略拒绝，重试无意义
    this.closedByUser = false;
    // Don't open a second socket if one is already live or in progress — otherwise a
    // send-triggered connect() during a reconnect would spawn duplicate connections.
    if (this.ws && (this.ws.readyState === WebSocket.CONNECTING || this.ws.readyState === WebSocket.OPEN)) {
      if (this.ws.readyState === WebSocket.OPEN) this.flushOnOpen();
      return;
    }
    const full = this.sessionId ? `${this.url}&session_id=${encodeURIComponent(this.sessionId)}` : this.url;
    try {
      this.ws = new WebSocket(full);
    } catch {
      this.scheduleReconnect();
      return;
    }

    this.ws.onopen = () => {
      this.reconnectAttempts = 0;
      this.startHeartbeat();
      // Restore transcript after a (re)connect.
      if (this.sessionId) this.send({ type: "history", limit: 50 });
      this.flushOnOpen();
    };

    this.ws.onmessage = (e) => {
      let ev: ServerEvent;
      try {
        ev = JSON.parse(e.data);
      } catch {
        return;
      }
      // 任何成功解析的入站帧都证明链路存活，不只认 pong——否则长回答流式期间
      // 服务端来不及应答 ping，心跳看门狗会误杀正在传输的连接。
      this.awaitingPong = false;
      if (ev.type === "pong") return;
      if (ev.type === "connected" && ev.session_id) this.sessionId = ev.session_id;
      if (ev.type === "message_end" && ev.session_id) this.sessionId = ev.session_id;
      this.handler(ev);
    };

    this.ws.onclose = (e: CloseEvent) => {
      this.stopHeartbeat();
      if (this.closedByUser) return;
      // 4xxx 为服务端策略性关闭（4403：域名未白名单/渠道停用/租户暂停），
      // 重连必然再被拒，直接进入 blocked 终态；仅异常断开走退避重连。
      if (e.code >= 4000 && e.code < 5000) {
        this.blocked = true;
        this.handler({ type: "_blocked", code: e.code });
        return;
      }
      this.scheduleReconnect();
    };

    this.ws.onerror = () => {
      this.ws?.close();
    };
  }

  /** Drain queued sends (once) and fire the one-shot open callback. */
  private flushOnOpen() {
    const queued = this.pendingSends;
    this.pendingSends = [];
    queued.forEach((p) => this.send(p));
    const cb = this.onOpenCb;
    this.onOpenCb = null;
    cb?.();
  }

  private scheduleReconnect() {
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    this.reconnectAttempts += 1;
    // 连续失败太多次：进入离线模式（UI 显示离线 + 发送走 HTTP 降级），
    // 改为每 60s 低频后台重试，连上后 onopen 复位、无缝切回 WS。
    if (this.reconnectAttempts >= MAX_BACKOFF_ATTEMPTS) {
      this.handler({ type: "_offline", attempt: this.reconnectAttempts });
      this.reconnectTimer = window.setTimeout(() => {
        this.reconnectTimer = null;
        if (!this.closedByUser) this.connect();
      }, OFFLINE_RETRY_MS);
      return;
    }
    const delay = Math.min(1000 * 2 ** this.reconnectAttempts, 15000);
    this.handler({ type: "_reconnecting", attempt: this.reconnectAttempts });
    this.reconnectTimer = window.setTimeout(() => {
      this.reconnectTimer = null;
      // Reconnect only restores the transcript; never replays queued sends here, or a
      // flaky connection would post the same message multiple times.
      if (!this.closedByUser) this.connect();
    }, delay);
  }

  /** 手动重试（离线态的「重试」按钮）：清掉退避计时器立即重连。 */
  retryNow() {
    if (this.closedByUser || this.blocked) return;
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    this.reconnectAttempts = 0;
    this.handler({ type: "_reconnecting", attempt: 0 });
    this.connect();
  }

  private startHeartbeat() {
    this.stopHeartbeat();
    this.awaitingPong = false;
    this.heartbeat = window.setInterval(() => {
      // Watchdog: if the previous ping was never answered, the socket is a zombie (resumed
      // from background, reports OPEN but dead) — rebuild it so history backfills what we missed.
      if (this.awaitingPong) {
        this.forceReconnect();
        return;
      }
      this.awaitingPong = true;
      this.send({ type: "ping" });
    }, 15000);
  }

  private stopHeartbeat() {
    if (this.heartbeat) {
      clearInterval(this.heartbeat);
      this.heartbeat = null;
    }
    this.awaitingPong = false;
  }

  /** 疑似僵尸连接的探活（回前台 staleGap / 'online' 事件触发）：置 awaitingPong 并立即发 ping，
   *  交给 15s 心跳看门狗裁决——真僵尸在下个 tick 前收不到任何入站帧，被 forceReconnect 重建；
   *  健康连接的 pong（或流式期间的任意 chunk）会清掉 awaitingPong，活流不被打断。
   *  先 startHeartbeat 重置周期，保证完整等满一个 tick，避免探测撞上即将触发的旧 tick 被误杀。 */
  private probe() {
    if (!this.isOpen()) return;
    this.startHeartbeat();
    this.awaitingPong = true;
    this.send({ type: "ping" });
  }

  /** Tear down a (possibly zombie) socket and open a fresh one; onopen re-pulls history. */
  private forceReconnect() {
    this.stopHeartbeat();
    const old = this.ws;
    this.ws = null;
    if (old) {
      old.onclose = null; // detach so it can't schedule a competing reconnect
      old.onerror = null;
      old.onmessage = null;
      try {
        old.close();
      } catch {
        /* ignore */
      }
    }
    this.handler({ type: "_reconnecting", attempt: 1 });
    this.connect();
  }

  isOpen() {
    return this.ws?.readyState === WebSocket.OPEN;
  }

  send(obj: Record<string, any>) {
    if (this.isOpen()) this.ws!.send(JSON.stringify(obj));
  }

  sendMessage(text: string, attachments?: any[], clientMsgId?: string) {
    const payload: Record<string, unknown> = { type: "user_message", text, attachments: attachments || [] };
    // 幂等键：服务端持久化到消息 meta 并在 received/history 中回传（重试复用同一 id 去重），
    // 也供 ACK 超时后从本地队列撤回排队消息。
    if (clientMsgId) payload.client_msg_id = clientMsgId;
    if (this.isOpen()) this.send(payload);
    else this.pendingSends.push(payload); // flushed exactly once on next open
  }

  /** ACK 超时后撤掉仍在排队的消息，避免用户重试时旧队列再发一遍造成重复。 */
  cancelQueued(clientMsgId: string) {
    this.pendingSends = this.pendingSends.filter((p) => p.client_msg_id !== clientMsgId);
  }

  /** 请求服务端取消当前生成；调用方本地立即收尾，不等服务端确认。 */
  stopGeneration() {
    this.send({ type: "stop" });
  }

  /** HTTP 降级通道：WS 连不上时走非流式 REST 接口，整段返回 {reply, session_id, citations…}。
   *  text 允许为空（纯附件消息）：与 WS 分支对齐，只要 attachments 非空服务端即接受。 */
  async sendMessageHttp(text: string, attachments?: any[]): Promise<any> {
    const res = await fetch(`${this.origin}/api/chat/message`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        text,
        uid: this.uid,
        session_id: this.sessionId || null,
        channel_key: this.channelKey,
        attachments: attachments || [],
      }),
    });
    if (!res.ok) throw new Error(`发送失败（${res.status}）`);
    const data = await res.json();
    if (data.session_id) this.sessionId = data.session_id;
    return data;
  }

  sendFeedback(messageId: string, kind: "up" | "down", note = "") {
    this.send({ type: "feedback", message_id: messageId, kind, note });
  }

  requestHuman() {
    this.send({ type: "request_human" });
  }

  /** Notify a watching operator that the customer is typing (throttled by the caller). */
  sendTyping() {
    this.send({ type: "typing" });
  }

  endSession(rating?: number, note?: string) {
    this.send({ type: "end_session", rating: rating || 0, note: note || "" });
  }

  /** Upload one attachment over HTTP; returns the stored descriptor. */
  async uploadFile(file: File): Promise<any> {
    const fd = new FormData();
    fd.append("file", file);
    fd.append("channel_key", this.channelKey);
    fd.append("uid", this.uid);
    const res = await fetch(`${this.origin}/api/chat/upload`, { method: "POST", body: fd });
    if (!res.ok) {
      let detail = "上传失败";
      try {
        detail = (await res.json()).detail || detail;
      } catch {
        /* ignore */
      }
      throw new Error(detail);
    }
    return res.json();
  }

  close() {
    this.closedByUser = true;
    this.stopHeartbeat();
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    this.ws?.close();
  }

  /** 彻底销毁：断开 socket 并移除全局监听器/计时器，供宿主卸载 widget 时调用。 */
  destroy() {
    this.close();
    if (typeof document !== "undefined") document.removeEventListener("visibilitychange", this.onVisibility);
    if (typeof window !== "undefined") window.removeEventListener("online", this.onOnline);
  }
}
