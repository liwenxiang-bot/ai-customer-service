import { useEffect, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { conversationApi } from "../api";
import { channelLabel, fmtShort, HANDOFF_STATUS, sessionStatusMeta } from "../utils";

/** Rough device / browser label from a User-Agent (enough for an agent's at-a-glance context). */
export function describeUA(ua?: string): string {
  if (!ua) return "";
  const os = /iPhone|iPad/.test(ua)
    ? "iOS"
    : /Android/.test(ua)
      ? "Android"
      : /Windows/.test(ua)
        ? "Windows"
        : /Mac OS X|Macintosh/.test(ua)
          ? "macOS"
          : /Linux/.test(ua)
            ? "Linux"
            : "";
  const browser = /MicroMessenger/.test(ua)
    ? "微信"
    : /Edg\//.test(ua)
      ? "Edge"
      : /Chrome\//.test(ua)
        ? "Chrome"
        : /Firefox\//.test(ua)
          ? "Firefox"
          : /Safari\//.test(ua)
            ? "Safari"
            : "";
  const mobile = /Mobile|iPhone|Android/.test(ua) ? "手机" : "电脑";
  return [mobile, os, browser].filter(Boolean).join(" · ");
}

function Row({ k, v }: { k: string; v?: ReactNode }) {
  if (v === undefined || v === null || v === "") return null;
  return (
    <div className="cp-row">
      <span className="cp-key">{k}</span>
      <span className="cp-val">{v}</span>
    </div>
  );
}

/** Right-hand context column on wide screens (Chatwoot-style): who the customer is, this
 *  conversation's state, and their previous conversations. */
export function CustomerPanel({ detail, ticket }: { detail: any; ticket: any }) {
  const s = detail?.session;
  const [history, setHistory] = useState<any[] | null>(null);

  useEffect(() => {
    setHistory(null);
    if (!s?.end_user_id) return;
    let alive = true;
    conversationApi
      .list({ end_user_id: s.end_user_id, page_size: 11 })
      .then((d) => alive && setHistory((d.items || []).filter((x: any) => x.id !== s.id).slice(0, 10)))
      .catch(() => alive && setHistory([]));
    return () => {
      alive = false;
    };
  }, [s?.id, s?.end_user_id]);

  if (!s) return <aside className="customer-panel" aria-label="客户信息" />;
  const meta: { ip?: string; ua?: string } = s.meta || {};
  const name: string = s.end_user_display || s.end_user_id || "匿名用户";
  const st = sessionStatusMeta(s.status);
  const tk = ticket ? HANDOFF_STATUS[ticket.status as keyof typeof HANDOFF_STATUS] : null;

  return (
    <aside className="customer-panel" aria-label="客户信息">
      <section className="cp-section cp-head">
        <span className="cp-avatar" aria-hidden="true">{Array.from(name.trim())[0] || "客"}</span>
        <div className="cp-head-main">
          <div className="cp-name" title={name}>{name}</div>
          <span className="badge badge-plain">{channelLabel(s.channel_type)}</span>
        </div>
      </section>

      <section className="cp-section">
        <div className="cp-title">客户</div>
        <Row k="用户 ID" v={<code className="cp-code">{s.end_user_id || "—"}</code>} />
        <Row k="设备" v={describeUA(meta.ua)} />
        <Row k="IP" v={typeof meta.ip === "string" ? meta.ip : ""} />
        <Row k="进线时间" v={fmtShort(s.created_at)} />
      </section>

      <section className="cp-section">
        <div className="cp-title">本次会话</div>
        <Row k="状态" v={<span className={`badge badge-${st.cls}`}>{st.label}</span>} />
        <Row k="消息数" v={String(detail?.messages?.length ?? 0)} />
        <Row
          k="工单"
          v={tk && <span className={`badge badge-${tk.cls}`}>{tk.label}{ticket?.assignee_name ? ` · ${ticket.assignee_name}` : ""}</span>}
        />
        <Row k="满意度" v={s.satisfaction_rating ? "★".repeat(s.satisfaction_rating) : ""} />
      </section>

      <section className="cp-section">
        <div className="cp-title">历史会话{history && history.length > 0 ? `（${history.length}）` : ""}</div>
        {history === null ? (
          <div className="muted cp-empty">加载中…</div>
        ) : history.length === 0 ? (
          <div className="muted cp-empty">这是该客户的第一次咨询</div>
        ) : (
          history.map((h) => {
            const hs = sessionStatusMeta(h.status);
            return (
              <Link key={h.id} to={`/s/${h.id}`} className="cp-hist">
                <div className="cp-hist-row">
                  <span className="cp-hist-time">{fmtShort(h.last_activity_at || h.created_at)}</span>
                  <span className={`badge badge-${hs.cls}`}>{hs.label}</span>
                </div>
                <div className="cp-hist-text">{h.last_message?.text || h.title || "（无内容）"}</div>
              </Link>
            );
          })
        )}
      </section>
    </aside>
  );
}
