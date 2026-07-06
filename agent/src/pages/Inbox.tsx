import { MouseEvent, useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { apiError, conversationApi, handoffApi } from "../api";
import { useAuth } from "../auth";
import { usePolling } from "../usePolling";
import { useRealtime } from "../realtime";
import { currentTheme, toggleTheme, ThemeMode } from "../theme";
import { toast } from "../toast";
import { channelLabel, fmtShort, HANDOFF_STATUS, sessionStatusMeta } from "../utils";

type Tab = "waiting" | "mine" | "all";

const APP_TITLE = "坐席工作台";

/** 各 tab 的空态文案（插画共用一个耳机线稿）。 */
const EMPTY_COPY: Record<Tab, { title: string; sub: string }> = {
  waiting: { title: "现在没有客户在等待", sub: "客户转人工后会第一时间出现在这里，安心喝口水吧" },
  mine: { title: "还没有认领的会话", sub: "去「待认领」把需要帮助的客户接过来吧" },
  all: { title: "暂无需要关注的会话", sub: "待人工与接管中的会话都会汇总在这里" },
};

/** 工单认领徽标：待认领红（=admin open）/ 他人跟进橙（=admin in_progress）/ 我认领的蓝。
 *  列表项未附 ticket 字段（如「全部」tab 老数据）时不显示。 */
function ClaimBadge({ ticket, myId, status }: { ticket: any; myId?: string; status?: string }) {
  if (ticket === undefined) return null;
  if (!ticket || ticket.status === "open") {
    // 接管中的会话没有存活工单时不标「待认领」——旁边的状态徽标已说明有人在接待。
    if (!ticket && status === "human_takeover") return null;
    return <span className={`badge badge-${HANDOFF_STATUS.open.cls}`}>待认领</span>;
  }
  if (ticket.assignee_id && ticket.assignee_id === myId)
    return <span className="badge badge-blue">我认领的</span>;
  return (
    <span className={`badge badge-${HANDOFF_STATUS.in_progress.cls}`}>
      {ticket.assignee_name || "同事"}跟进中
    </span>
  );
}

/** 头像取名字首字符（Array.from 兼容 emoji/生僻字），带未读红点。 */
function Avatar({ name, unread }: { name: string; unread: boolean }) {
  const ch = Array.from((name || "客").trim())[0] || "客";
  return (
    <span className="conv-avatar" aria-hidden="true">
      {ch}
      {unread && <i className="unread-dot" />}
    </span>
  );
}

const newer = (a?: string | null, b?: string | null) =>
  !!a && !!b && Date.parse(a) > Date.parse(b);

/** 收件箱：待认领 / 我的 / 全部 三个队列，15s 轮询 + queue 事件即时刷新。
 *  桌面端作为左栏常驻（activeId 高亮当前会话）；后台有新动态的会话打未读点并闪动标题。 */
export function Inbox({ activeId = "" }: { activeId?: string }) {
  const { user, logout } = useAuth();
  const nav = useNavigate();
  const [tab, setTab] = useState<Tab>("waiting");
  const [list, setList] = useState<any[]>([]);
  const [loaded, setLoaded] = useState(false); // 当前 tab 是否已拿到首屏（否则显示骨架）
  const [error, setError] = useState<string | null>(null);
  const [counts, setCounts] = useState<any>({ waiting: 0, mine: 0, takeover: 0, all: 0 });
  const [claiming, setClaiming] = useState<string>(""); // 认领中的工单 id（按钮防连点）
  const [themeMode, setThemeMode] = useState<ThemeMode>(() => currentTheme());
  const [unread, setUnread] = useState<Set<string>>(new Set());

  const tabRef = useRef(tab);
  tabRef.current = tab;
  const seqRef = useRef(0); // 过期响应保护：只允许最新一次请求落地
  const activeIdRef = useRef(activeId);
  activeIdRef.current = activeId;
  // 每个会话最近一次已知活跃时间（跨 tab 累积、仅本次打开期间有效）：
  // 下次刷新时活跃时间变新且不是当前打开的会话 → 未读点。
  const lastActRef = useRef<Map<string, string>>(new Map());

  const markUnread = (items: any[]) => {
    setUnread((prev) => {
      let next: Set<string> | null = null;
      for (const it of items) {
        const seen = lastActRef.current.get(it.id);
        if (seen && newer(it.last_activity_at, seen) && it.id !== activeIdRef.current) {
          if (!next) next = new Set(prev);
          next.add(it.id);
        }
        if (it.last_activity_at) lastActRef.current.set(it.id, it.last_activity_at);
      }
      if (prev.has(activeIdRef.current)) {
        if (!next) next = new Set(prev);
        next.delete(activeIdRef.current);
      }
      return next || prev;
    });
  };

  const loadList = () => {
    const t = tabRef.current;
    const seq = ++seqRef.current;
    const apply = (items: any[]) => {
      if (seq !== seqRef.current || tabRef.current !== t) return;
      markUnread(items);
      setList(items);
      setLoaded(true);
      setError(null);
    };
    const fail = (e: unknown) => {
      if (seq === seqRef.current && tabRef.current === t) {
        setError(apiError(e));
        setLoaded(true);
      }
    };
    if (t === "mine") {
      // 我的 = 我认领的 in_progress 工单对应的会话（认领后工单仍挂我名下）
      //      ∪ 我正在接管（human_takeover 且 takeover_by_id === 我）的会话。
      // 与 queue-counts 的 mine 口径对齐：接管中的会话工单已 resolved，仅靠 takeover_by_id 归属。
      Promise.all([
        handoffApi.tickets({ mine: true, status: "in_progress", page_size: 100 }),
        conversationApi.list({ attention: true, page_size: 100 }),
      ])
        .then(([tk, cv]) => {
          const mineTickets = new Map<string, any>();
          (tk.items || []).forEach((x: any) => mineTickets.set(x.session_id, x));
          const mine = (it: any) =>
            mineTickets.has(it.id) ||
            it.ticket?.assignee_id === user?.id ||
            (it.status === "human_takeover" && it.takeover_by_id === user?.id);
          const items = (cv.items || [])
            .filter(mine)
            .map((it: any) => {
              if (it.ticket === undefined && mineTickets.has(it.id)) {
                const x = mineTickets.get(it.id);
                return {
                  ...it,
                  ticket: {
                    id: x.id,
                    status: x.status,
                    assignee_id: user?.id,
                    assignee_name: x.assignee_name ?? x.assignee_email ?? user?.name,
                  },
                };
              }
              return it;
            });
          apply(items);
        })
        .catch(fail);
    } else if (t === "waiting") {
      // 待认领 = escalated 且未被认领（无 in_progress 工单）。已认领的会话归属某个坐席，
      // 只应出现在「我的」，从待认领列表滤除，与 queue-counts 的 waiting 口径一致。
      conversationApi
        .list({ pending_human: true, page_size: 50 })
        .then((d) => apply((d.items || []).filter((it: any) => it.ticket?.status !== "in_progress")))
        .catch(fail);
    } else {
      conversationApi
        .list({ attention: true, page_size: 50 })
        .then((d) => apply(d.items || []))
        .catch(fail);
    }
    conversationApi.queueCounts().then((d) => setCounts(d)).catch(() => {});
  };
  usePolling(loadList, 15000, { deps: [tab] });

  // tab 切换先回骨架，避免旧列表闪现
  useEffect(() => {
    setLoaded(false);
    setList([]);
    setError(null);
  }, [tab]);

  // 打开某会话即视为已读
  useEffect(() => {
    if (!activeId) return;
    setUnread((prev) => {
      if (!prev.has(activeId)) return prev;
      const next = new Set(prev);
      next.delete(activeId);
      return next;
    });
  }, [activeId]);

  // 未读时 document.title 闪动计数；清零即还原
  useEffect(() => {
    if (unread.size === 0) {
      document.title = APP_TITLE;
      return;
    }
    const alert = `【${unread.size} 条新动态】${APP_TITLE}`;
    document.title = alert;
    let flip = false;
    const timer = setInterval(() => {
      flip = !flip;
      document.title = flip ? APP_TITLE : alert;
    }, 1200);
    return () => {
      clearInterval(timer);
      document.title = APP_TITLE;
    };
  }, [unread.size]);

  // queue 事件突发时合并刷新（400ms 尾随），避免瞬间打出多轮请求
  const queueTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const scheduleLoad = () => {
    if (queueTimerRef.current) return;
    queueTimerRef.current = setTimeout(() => {
      queueTimerRef.current = null;
      loadList();
    }, 400);
  };
  useRealtime({ onQueue: scheduleLoad });
  useEffect(() => () => {
    if (queueTimerRef.current) clearTimeout(queueTimerRef.current);
  }, []);

  const doClaim = async (e: MouseEvent, it: any) => {
    e.stopPropagation(); // 不触发进会话
    if (!it.ticket?.id || claiming) return;
    setClaiming(it.ticket.id);
    try {
      await handoffApi.claim(it.ticket.id);
      toast("认领成功", "success");
    } catch (err) {
      toast(apiError(err), "error"); // 409 → “已被 XX 认领”
    } finally {
      setClaiming("");
      loadList();
    }
  };

  const doLogout = async () => {
    await logout();
    nav("/login", { replace: true });
  };

  const tabs: Array<{ key: Tab; label: string; count: number }> = [
    { key: "waiting", label: "待认领", count: counts.waiting ?? 0 },
    { key: "mine", label: "我的", count: counts.mine ?? 0 },
    { key: "all", label: "全部", count: counts.all ?? 0 },
  ];
  const empty = EMPTY_COPY[tab];

  return (
    <div className="app-shell">
      <header className="app-header">
        <div className="app-header-title">{APP_TITLE}</div>
        <div className="app-header-right">
          <span className="muted app-header-user">{user?.name || user?.email}</span>
          <button
            className="btn btn-ghost btn-icon"
            onClick={() => setThemeMode(toggleTheme())}
            title={themeMode === "dark" ? "切换为浅色" : "切换为深色"}
            aria-label="切换主题"
          >
            {themeMode === "dark" ? (
              <svg viewBox="0 0 20 20" width="16" height="16" fill="none" aria-hidden="true">
                <circle cx="10" cy="10" r="4" fill="currentColor" />
                <g stroke="currentColor" strokeWidth="1.6" strokeLinecap="round">
                  <path d="M10 1.6v2M10 16.4v2M1.6 10h2M16.4 10h2M4.1 4.1l1.4 1.4M14.5 14.5l1.4 1.4M15.9 4.1l-1.4 1.4M5.5 14.5l-1.4 1.4" />
                </g>
              </svg>
            ) : (
              <svg viewBox="0 0 20 20" width="16" height="16" fill="currentColor" aria-hidden="true">
                <path d="M17.4 12.3A7.5 7.5 0 0 1 7.7 2.6a.5.5 0 0 0-.65-.62 8.3 8.3 0 1 0 10.97 10.97.5.5 0 0 0-.62-.65z" />
              </svg>
            )}
          </button>
          <button className="btn btn-ghost btn-sm" onClick={doLogout}>退出</button>
        </div>
      </header>

      <div className="tabs">
        {tabs.map((t) => (
          <button
            key={t.key}
            className={`tab${tab === t.key ? " tab-active" : ""}`}
            onClick={() => setTab(t.key)}
          >
            {t.label}
            <span className="tab-count">{t.count}</span>
          </button>
        ))}
      </div>

      <main className="inbox-list">
        {error && (
          <div className="banner-error">
            <span>队列刷新失败：{error}</span>
            <button className="banner-retry" onClick={loadList}>重试</button>
          </div>
        )}
        {!loaded && !error ? (
          <div aria-hidden="true">
            {[0, 1, 2, 3].map((i) => (
              <div key={i} className="conv-item conv-item-sk">
                <span className="sk sk-avatar" />
                <div className="conv-main">
                  <div className="sk sk-line" style={{ width: `${52 - i * 4}%` }} />
                  <div className="sk sk-line sk-line-dim" style={{ width: `${84 - i * 6}%` }} />
                </div>
              </div>
            ))}
          </div>
        ) : list.length === 0 && !error ? (
          <div className="empty">
            <svg className="empty-icon" viewBox="0 0 64 64" fill="none" aria-hidden="true">
              <path d="M20 38v-5c0-7 5.4-12 12-12s12 5 12 12v5" stroke="currentColor" strokeWidth="3.4" strokeLinecap="round" />
              <rect x="16.6" y="36.4" width="7" height="11" rx="3.2" fill="currentColor" />
              <rect x="40.4" y="36.4" width="7" height="11" rx="3.2" fill="currentColor" />
              <path d="M43.6 47.4c-.6 3.2-3.8 5-7.6 5h-2.4" stroke="currentColor" strokeWidth="2.6" strokeLinecap="round" />
              <circle cx="32.4" cy="52.4" r="2.2" fill="currentColor" />
              <path d="M49 14l1 2.6 2.6 1-2.6 1-1 2.6-1-2.6-2.6-1 2.6-1zM14 18l.8 2 2 .8-2 .8-.8 2-.8-2-2-.8 2-.8z" fill="currentColor" opacity="0.5" />
            </svg>
            <div className="empty-title">{empty.title}</div>
            <div className="empty-sub">{empty.sub}</div>
          </div>
        ) : (
          list.map((it) => {
            const st = sessionStatusMeta(it.status);
            const claimable = it.ticket && it.ticket.status === "open";
            const name = it.end_user_display || it.end_user_id || "匿名用户";
            return (
              <div
                key={it.id}
                className={`conv-item${it.id === activeId ? " conv-item-active" : ""}`}
                onClick={() => nav(`/s/${it.id}`)}
              >
                <Avatar name={name} unread={unread.has(it.id)} />
                <div className="conv-main">
                  <div className="conv-row">
                    <span className="conv-name">{name}</span>
                    <span className="conv-time">{fmtShort(it.last_activity_at)}</span>
                  </div>
                  <div className="conv-summary">{it.title || "（无内容）"}</div>
                  <div className="conv-meta">
                    <span className="badge badge-plain">{channelLabel(it.channel_type)}</span>
                    <span className={`badge badge-${st.cls}`}>{st.label}</span>
                    <ClaimBadge ticket={it.ticket} myId={user?.id} status={it.status} />
                  </div>
                </div>
                {claimable && (
                  <button
                    className="btn btn-primary btn-sm conv-claim"
                    disabled={claiming === it.ticket.id}
                    onClick={(e) => doClaim(e, it)}
                  >
                    {claiming === it.ticket.id ? "认领中…" : "认领"}
                  </button>
                )}
              </div>
            );
          })
        )}
      </main>
    </div>
  );
}
