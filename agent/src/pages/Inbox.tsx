import { MouseEvent, useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { apiError, conversationApi, handoffApi } from "../api";
import { useAuth } from "../auth";
import { usePolling } from "../usePolling";
import { useRealtime } from "../realtime";
import { currentTheme, toggleTheme, ThemeMode } from "../theme";
import { alertActivity, notifyEnabled, setNotifyEnabled } from "../notify";
import { toast } from "../toast";
import { channelLabel, fmtShort, sessionStatusMeta } from "../utils";
import { IconBell, IconBellOff, IconInbox, IconMoon, IconSun } from "../icons";
import { Logo } from "../components/Logo";
import { Status } from "../components/Status";

type Tab = "waiting" | "mine" | "all";

const APP_TITLE = "坐席工作台";

/** 各 tab 的空态文案：一句现状 + 一句去向。 */
const EMPTY_COPY: Record<Tab, { title: string; sub: string }> = {
  waiting: { title: "没有等待中的客户", sub: "客户转人工后会出现在这里。" },
  mine: { title: "你还没有认领会话", sub: "到「待认领」认领需要人工的客户。" },
  all: { title: "没有需要人工的会话", sub: "待人工和接管中的会话会显示在这里。" },
};

/** 工单认领信息（纯文字）：待认领红 / 我认领的主色 / 他人跟进中性。
 *  列表项未附 ticket 字段（如「全部」tab 老数据）时不显示。 */
function ClaimText({ ticket, myId, status }: { ticket: any; myId?: string; status?: string }) {
  if (ticket === undefined) return null;
  if (!ticket || ticket.status === "open") {
    // 接管中的会话没有存活工单时不标「待认领」——旁边的状态已说明有人在接待。
    if (!ticket && status === "human_takeover") return null;
    return <span className="meta-text meta-open">待认领</span>;
  }
  if (ticket.assignee_id && ticket.assignee_id === myId)
    return <span className="meta-text meta-mine">我认领的</span>;
  return <span className="meta-text">{ticket.assignee_name || "同事"}跟进中</span>;
}

const FROM_LABEL: Record<string, string> = { customer: "", ai: "AI：", agent: "我方：" };

const newer = (a?: string | null, b?: string | null) =>
  !!a && !!b && Date.parse(a) > Date.parse(b);

/** 收件箱：待认领 / 我的 / 全部 三个队列，15s 轮询 + queue 事件即时刷新。
 *  桌面端作为左栏常驻（activeId 高亮当前会话）；后台有新动态的会话在名字前打未读点并闪动标题。 */
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
  const [notifyOn, setNotifyOn] = useState<boolean>(() => notifyEnabled());

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
  useRealtime({
    onQueue: scheduleLoad,
    onActivity: (msg: any) => {
      const sid = msg?.session_id;
      if (!sid) return;
      // Mark unread across ALL tabs at once (not only the list currently shown), then refresh.
      if (sid !== activeIdRef.current) {
        setUnread((prev) => (prev.has(sid) ? prev : new Set(prev).add(sid)));
      }
      alertActivity("客户有新消息", "有待处理的客服会话收到新消息", () => nav(`/s/${sid}`));
      scheduleLoad();
    },
  });
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
        <Logo size={22} />
        <div className="app-header-title">{APP_TITLE}</div>
        <div className="app-header-right">
          <span className="muted app-header-user">{user?.name || user?.email}</span>
          <button
            className="btn btn-ghost btn-icon"
            onClick={() => setThemeMode(toggleTheme())}
            title={themeMode === "dark" ? "切换为浅色" : "切换为深色"}
            aria-label="切换主题"
          >
            {themeMode === "dark" ? <IconSun /> : <IconMoon />}
          </button>
          <button
            className={`btn btn-ghost btn-icon${notifyOn ? " btn-on" : ""}`}
            onClick={async () => {
              const next = await setNotifyEnabled(!notifyOn);
              if (!notifyOn && !next) toast("浏览器未允许通知，请在地址栏左侧的站点设置中开启", "error");
              setNotifyOn(next);
            }}
            title={notifyOn ? "新消息提醒已开启（页面在后台时弹通知并响提示音），点击关闭" : "新消息提醒已关闭，点击开启"}
            aria-label="新消息提醒"
            aria-pressed={notifyOn}
          >
            {notifyOn ? <IconBell /> : <IconBellOff />}
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
                <div className="conv-main">
                  <div className="sk sk-line" style={{ width: `${52 - i * 4}%` }} />
                  <div className="sk sk-line sk-line-dim" style={{ width: `${84 - i * 6}%` }} />
                </div>
              </div>
            ))}
          </div>
        ) : list.length === 0 && !error ? (
          <div className="empty">
            <IconInbox className="empty-icon" size={36} />
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
                <div className="conv-main">
                  <div className="conv-row">
                    <span className="conv-name">
                      {unread.has(it.id) && <i className="unread-dot" aria-label="有新消息" />}
                      {name}
                    </span>
                    <span className="conv-time">{fmtShort(it.last_activity_at)}</span>
                  </div>
                  <div className="conv-summary">
                    {it.last_message ? (
                      <>
                        <span className="conv-from">{FROM_LABEL[it.last_message.from] || ""}</span>
                        {it.last_message.text || "（无内容）"}
                      </>
                    ) : (
                      it.title || "（无内容）"
                    )}
                  </div>
                  <div className="conv-meta">
                    <Status tone={st.cls} label={st.label} />
                    <ClaimText ticket={it.ticket} myId={user?.id} status={it.status} />
                    <span className="meta-text">{channelLabel(it.channel_type)}</span>
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
