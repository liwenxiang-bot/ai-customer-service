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

    // Mobile browsers drop the socket when backgrounded/locked; reconnect (and backfill
    // history) as soon as the tab is visible again or the network returns — so operator
    // replies / takeover state appear without a manual refresh.
    if (typeof document !== "undefined") {
      document.addEventListener("visibilitychange", () => {
        if (document.visibilityState === "visible" && !this.closedByUser && !this.isOpen()) this.connect();
      });
    }
    if (typeof window !== "undefined") {
      window.addEventListener("online", () => {
        if (!this.closedByUser && !this.isOpen()) this.connect();
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
      if (ev.type === "pong") return;
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
    this.heartbeat = window.setInterval(() => this.send({ type: "ping" }), 25000);
  }

  private stopHeartbeat() {
    if (this.heartbeat) {
      clearInterval(this.heartbeat);
      this.heartbeat = null;
    }
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
