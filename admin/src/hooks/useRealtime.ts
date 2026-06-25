import { useEffect, useRef } from "react";
import { tokenStore } from "../api/client";

type Handlers = { onQueue?: () => void; onSession?: (msg: any) => void };

/** Live admin events over WebSocket (reuses the backend Redis pub/sub bus at /ws/admin).
 *  Auto-reconnects with backoff. Call the returned `watch(sessionId)` to also receive that
 *  session's live messages (customer / operator) during takeover. */
export function useRealtime(handlers: Handlers) {
  const hRef = useRef(handlers);
  hRef.current = handlers;
  const wsRef = useRef<WebSocket | null>(null);
  const watchRef = useRef<string>("");

  useEffect(() => {
    let stopped = false;
    let retry = 0;
    let reconnectTimer: ReturnType<typeof setTimeout>;
    let pingTimer: ReturnType<typeof setInterval>;

    const connect = () => {
      if (stopped) return;
      const token = tokenStore.access;
      if (!token) {
        reconnectTimer = setTimeout(connect, 2000);
        return;
      }
      const proto = location.protocol === "https:" ? "wss" : "ws";
      const ws = new WebSocket(`${proto}://${location.host}/ws/admin?token=${encodeURIComponent(token)}`);
      wsRef.current = ws;

      ws.onopen = () => {
        retry = 0;
        if (watchRef.current) ws.send(JSON.stringify({ type: "watch", session_id: watchRef.current }));
        pingTimer = setInterval(() => {
          if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: "ping" }));
        }, 25000);
      };
      ws.onmessage = (e) => {
        let msg: any;
        try { msg = JSON.parse(e.data); } catch { return; }
        if (msg.type === "queue") hRef.current.onQueue?.();
        else if (["customer_message", "human_message", "human_takeover", "ai_resumed", "human_ended"].includes(msg.type)) {
          hRef.current.onSession?.(msg);
        }
      };
      ws.onclose = () => {
        clearInterval(pingTimer);
        wsRef.current = null;
        if (!stopped) {
          retry += 1;
          reconnectTimer = setTimeout(connect, Math.min(1000 * 2 ** retry, 15000));
        }
      };
      ws.onerror = () => { try { ws.close(); } catch { /* ignore */ } };
    };

    connect();
    return () => {
      stopped = true;
      clearTimeout(reconnectTimer);
      clearInterval(pingTimer);
      wsRef.current?.close();
    };
  }, []);

  const watch = (sessionId: string) => {
    watchRef.current = sessionId || "";
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: "watch", session_id: sessionId || "" }));
    }
  };

  return { watch };
}
