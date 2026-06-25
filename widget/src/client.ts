// WebSocket chat client: streaming, heartbeat, auto-reconnect with backoff,
// and history backfill on reconnect.

export type ServerEvent = {
  type: string;
  [k: string]: any;
};

type Handler = (ev: ServerEvent) => void;

export class ChatClient {
  private ws: WebSocket | null = null;
  private url: string;
  private origin: string;
  private handler: Handler;
  private heartbeat: number | null = null;
  private awaitingPong = false; // a ping is outstanding → if still true next tick, socket is dead
  private hiddenAt = 0; // epoch ms the tab was last backgrounded (0 = currently visible)
  private reconnectAttempts = 0;
  private closedByUser = false;
  private onOpenCb: (() => void) | null = null;
  private pendingSends: Record<string, unknown>[] = []; // queued while offline, flushed once on open

  sessionId: string;
  uid: string;
  channelKey: string;

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

    // Mobile browsers drop the socket when backgrounded/locked, but a resumed socket often
    // still reports OPEN while silently dead ("zombie") — so it delivers nothing yet never
    // triggers a reconnect. On returning to the foreground (or network recovery) we rebuild
    // the socket rather than trust isOpen(); the fresh connection re-pulls history, so any
    // operator replies / takeover notices missed while dead are backfilled — no manual refresh.
    if (typeof document !== "undefined") {
      document.addEventListener("visibilitychange", () => {
        if (document.visibilityState === "hidden") {
          this.hiddenAt = Date.now();
          return;
        }
        if (this.closedByUser) return;
        const staleGap = this.hiddenAt && Date.now() - this.hiddenAt > 8000;
        this.hiddenAt = 0;
        if (!this.isOpen()) this.connect();
        else if (staleGap && this.sessionId) this.forceReconnect();
      });
    }
    if (typeof window !== "undefined") {
      window.addEventListener("online", () => {
        if (this.closedByUser) return;
        if (!this.isOpen()) this.connect();
        else if (this.sessionId) this.forceReconnect(); // a network bounce can leave a half-open socket
      });
    }
  }

  connect(onOpen?: () => void) {
    if (onOpen) this.onOpenCb = onOpen;
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
      if (ev.type === "pong") {
        this.awaitingPong = false;
        return;
      }
      if (ev.type === "connected" && ev.session_id) this.sessionId = ev.session_id;
      if (ev.type === "message_end" && ev.session_id) this.sessionId = ev.session_id;
      this.handler(ev);
    };

    this.ws.onclose = () => {
      this.stopHeartbeat();
      if (!this.closedByUser) this.scheduleReconnect();
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
    this.reconnectAttempts += 1;
    const delay = Math.min(1000 * 2 ** this.reconnectAttempts, 15000);
    this.handler({ type: "_reconnecting", attempt: this.reconnectAttempts });
    setTimeout(() => {
      // Reconnect only restores the transcript; never replays queued sends here, or a
      // flaky connection would post the same message multiple times.
      if (!this.closedByUser) this.connect();
    }, delay);
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

  sendMessage(text: string, attachments?: any[]) {
    const payload = { type: "user_message", text, attachments: attachments || [] };
    if (this.isOpen()) this.send(payload);
    else this.pendingSends.push(payload); // flushed exactly once on next open
  }

  sendFeedback(messageId: string, kind: "up" | "down") {
    this.send({ type: "feedback", message_id: messageId, kind });
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
    this.ws?.close();
  }
}
