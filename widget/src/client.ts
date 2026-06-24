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
  }

  connect(onOpen?: () => void) {
    this.onOpenCb = onOpen || null;
    this.closedByUser = false;
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
      this.onOpenCb?.();
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

  private scheduleReconnect() {
    this.reconnectAttempts += 1;
    const delay = Math.min(1000 * 2 ** this.reconnectAttempts, 15000);
    this.handler({ type: "_reconnecting", attempt: this.reconnectAttempts });
    setTimeout(() => {
      if (!this.closedByUser) this.connect(this.onOpenCb || undefined);
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
    this.send({ type: "user_message", text, attachments: attachments || [] });
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
