import { useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import {
  apiError,
  cannedApi,
  conversationApi,
  handoffApi,
  MAX_ATTACHMENTS,
  MAX_UPLOAD_BYTES,
  UPLOAD_ACCEPT,
  type Attachment,
} from "../api";
import { useAuth } from "../auth";
import { usePolling } from "../usePolling";
import { useRealtime } from "../realtime";
import { toast } from "../toast";
import { channelLabel, fmtShort, HANDOFF_STATUS, isImeComposing, sessionStatusMeta } from "../utils";

type Ticket = {
  id: string;
  status: string;
  assignee_id?: string | null;
  assignee_name?: string | null;
} | null;

/** 找到该会话最新一张未解决的工单。
 *  首选契约端点：GET /conversations?pending_human=true 每项附 ticket；
 *  会话已不在待接待列表（如已接管）时，兜底扫工单列表按 session_id 匹配。 */
async function findTicket(sessionId: string, myId?: string): Promise<Ticket> {
  try {
    const d = await conversationApi.list({ pending_human: true, page_size: 100 });
    const hit = (d.items || []).find((x: any) => x.id === sessionId);
    if (hit && hit.ticket !== undefined) return hit.ticket;
  } catch {
    /* 忽略，走工单接口兜底 */
  }
  const probes: Array<{ params: any; mine: boolean }> = [
    { params: { mine: true, status: "in_progress", page_size: 100 }, mine: true },
    { params: { status: "open", page_size: 100 }, mine: false },
    { params: { status: "in_progress", page_size: 100 }, mine: false },
  ];
  for (const { params, mine } of probes) {
    try {
      const d = await handoffApi.tickets(params);
      const t = (d.items || []).find((x: any) => x.session_id === sessionId);
      if (t) {
        return {
          id: t.id,
          status: t.status,
          assignee_id: t.assignee_id ?? (mine ? myId ?? null : null),
          assignee_name: t.assignee_name ?? t.assignee_email ?? null,
        };
      }
    } catch {
      /* 下一个来源 */
    }
  }
  return null;
}

/** 附件 url 协议白名单：仅放行 http/https 与站内相对/媒体路径，拒绝 javascript:/data: 等注入向量。
 *  安全的返回原 url，否则返回 null（调用方不渲染）。 */
function safeAttachUrl(url: unknown): string | null {
  if (typeof url !== "string") return null;
  const u = url.trim();
  if (!u) return null;
  // 站内相对路径（/media/... 或 ./、无协议）一律放行
  if (u.startsWith("/") || u.startsWith("./") || u.startsWith("../")) return u;
  try {
    const proto = new URL(u, location.origin).protocol;
    return proto === "http:" || proto === "https:" ? u : null;
  } catch {
    return null;
  }
}

/** 是否按图片渲染：只信 url 后缀，不信可伪造的 kind/content_type。 */
function isImageUrl(url: string): boolean {
  const path = url.split(/[?#]/)[0].toLowerCase();
  return /\.(png|jpe?g|gif|webp|bmp|svg|avif)$/.test(path);
}

/** 会话页：消息流 + 认领/接管/结束操作 + 快捷回复。支持通知深链 /agent/#/s/{id} 直达。 */
export function SessionPage() {
  const { id = "" } = useParams();
  const { user } = useAuth();

  const [detail, setDetail] = useState<any>(null);
  const [loadError, setLoadError] = useState("");
  const [ticket, setTicket] = useState<Ticket | undefined>(undefined); // undefined=加载中
  const [canned, setCanned] = useState<any[]>([]);
  const [cannedOpen, setCannedOpen] = useState(false);
  const [text, setText] = useState("");
  const [sending, setSending] = useState(false);
  const [pending, setPending] = useState<Attachment[]>([]); // 已上传、待随下一条回复发送
  const [uploading, setUploading] = useState(0);
  const fileRef = useRef<HTMLInputElement>(null);
  const [acting, setActing] = useState(""); // 进行中的操作 key，各按钮独立显示 loading 文案
  const [confirmEnd, setConfirmEnd] = useState(false);
  const [customerTyping, setCustomerTyping] = useState(false);
  const [showJump, setShowJump] = useState(false); // 上翻阅读时的「回到底部」
  const [hasNew, setHasNew] = useState(false); // 上翻期间有新消息

  const idRef = useRef(id); // WS 回调 / await 之后读最新会话 id，避免闭包串会话
  idRef.current = id;
  const ticketSeqRef = useRef(0);
  const bodyRef = useRef<HTMLDivElement>(null);
  const taRef = useRef<HTMLTextAreaElement>(null);
  const atBottomRef = useRef(true); // 贴底才跟随滚动（同 widget 策略）
  const msgCountRef = useRef(0);
  const typingHideRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const lastTypingRef = useRef(0);

  // ---- 数据加载 ----
  const loadDetail = () => {
    const sid = idRef.current;
    if (!sid) return;
    conversationApi
      .detail(sid)
      .then((d) => {
        if (idRef.current !== sid) return;
        // 按 message.id 合并：快照在途期间实时消息可能已追加，旧快照不吞掉本地尾部
        setDetail((prev: any) => {
          if (!prev?.messages?.length) return d;
          const seen = new Set((d.messages || []).map((x: any) => x.id));
          const extra = prev.messages.filter((x: any) => x.id && !seen.has(x.id));
          return extra.length ? { ...d, messages: [...(d.messages || []), ...extra] } : d;
        });
        setLoadError("");
      })
      .catch((e) => {
        if (idRef.current === sid && !detail) setLoadError(apiError(e));
      });
  };
  const loadTicket = () => {
    const sid = idRef.current;
    if (!sid) return;
    const seq = ++ticketSeqRef.current;
    findTicket(sid, user?.id).then((t) => {
      if (seq === ticketSeqRef.current && idRef.current === sid) setTicket(t);
    });
  };

  useEffect(() => {
    setDetail(null);
    setLoadError("");
    setTicket(undefined);
    setText("");
    setPending([]);
    setConfirmEnd(false);
    setCustomerTyping(false);
    setCannedOpen(false);
    setShowJump(false);
    setHasNew(false);
    atBottomRef.current = true;
    msgCountRef.current = 0;
    loadTicket();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id]);
  usePolling(loadDetail, 15000, { enabled: !!id, deps: [id] });
  useEffect(() => {
    cannedApi.list().then((d) => setCanned(d.items || [])).catch(() => {});
  }, []);

  // 滚动跟随：仅当用户贴底时才跟随新消息；上翻阅读不被打断，改为点亮「回到底部」
  useEffect(() => {
    const el = bodyRef.current;
    const count = detail?.messages?.length || 0;
    const grew = count > msgCountRef.current;
    msgCountRef.current = count;
    if (!el) return;
    if (atBottomRef.current) el.scrollTop = el.scrollHeight;
    else if (grew) setHasNew(true);
  }, [detail?.messages?.length]);
  useEffect(() => () => {
    if (typingHideRef.current) clearTimeout(typingHideRef.current);
  }, []);

  const onBodyScroll = () => {
    const el = bodyRef.current;
    if (!el) return;
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
    atBottomRef.current = nearBottom;
    setShowJump(!nearBottom);
    if (nearBottom) setHasNew(false);
  };
  const jumpToBottom = () => {
    const el = bodyRef.current;
    if (el) el.scrollTop = el.scrollHeight;
    atBottomRef.current = true;
    setShowJump(false);
    setHasNew(false);
  };

  // ---- 实时 ----
  const appendMsg = (m: any) => {
    if (!m?.id) return;
    setDetail((d: any) =>
      !d || (d.messages || []).some((x: any) => x.id === m.id)
        ? d
        : { ...d, messages: [...(d.messages || []), m] }
    );
  };
  const showCustomerTyping = () => {
    setCustomerTyping(true);
    if (typingHideRef.current) clearTimeout(typingHideRef.current);
    typingHideRef.current = setTimeout(() => setCustomerTyping(false), 4000);
  };
  const { watch, sendTyping } = useRealtime({
    onQueue: () => loadTicket(), // 认领/退回等队列事件 → 刷新工单归属
    onSession: (msg: any) => {
      // 载荷带 session_id（后端契约）：不属于当前会话的消息忽略，避免串线
      if (msg?.session_id && msg.session_id !== idRef.current) return;
      setCustomerTyping(false);
      if (msg?.type === "human_message" && msg.message_id)
        appendMsg({
          id: msg.message_id,
          role: "assistant",
          model: "human",
          content: msg.content,
          attachments: msg.attachments || [],
        });
      else if (msg?.type === "customer_message" && msg.message_id)
        appendMsg({ id: msg.message_id, role: "user", content: msg.content, attachments: msg.attachments || [] });
      else {
        // takeover / resume / end → 状态变了，整体刷新
        loadDetail();
        loadTicket();
      }
    },
    onTyping: (msg: any) => {
      // 后端 customer_typing 现在带 session_id：非当前会话一律忽略，避免串线误显示。
      // （缺省 session_id 的旧格式兜底放行，仅本会话可能收到。）
      if (msg?.session_id && msg.session_id !== idRef.current) return;
      showCustomerTyping();
    },
  });
  useEffect(() => {
    watch(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id]);

  // ---- 操作 ----
  const act = async (key: string, fn: () => Promise<any>, ok: string) => {
    if (acting) return;
    setActing(key);
    try {
      await fn();
      toast(ok, "success");
    } catch (e) {
      toast(apiError(e), "error");
    } finally {
      setActing("");
      loadDetail();
      loadTicket();
    }
  };
  /** 操作按钮文案：进行中换 loading 文案，其余操作进行中时只置灰。 */
  const actLabel = (key: string, idle: string, busy: string) => (acting === key ? busy : idle);

  const pickFiles = async (files: FileList | null) => {
    const sid = idRef.current;
    if (!files || !files.length || !sid) return;
    const room = MAX_ATTACHMENTS - pending.length - uploading;
    const list = Array.from(files);
    if (list.length > room) toast(`一次最多发送 ${MAX_ATTACHMENTS} 个附件`, "error");
    for (const f of list.slice(0, Math.max(room, 0))) {
      if (f.size > MAX_UPLOAD_BYTES) {
        toast(`「${f.name}」超过 10MB`, "error");
        continue;
      }
      setUploading((n) => n + 1);
      try {
        const att = await conversationApi.upload(sid, f);
        if (idRef.current === sid) setPending((p) => [...p, att]);
      } catch (e) {
        toast(`「${f.name}」上传失败：${apiError(e)}`, "error");
      } finally {
        setUploading((n) => n - 1);
      }
    }
  };

  const submit = async () => {
    const c = text.trim();
    const sid = idRef.current;
    const atts = pending;
    if ((!c && !atts.length) || sending || uploading || !sid) return;
    setSending(true);
    try {
      const res = await conversationApi.reply(sid, c, atts);
      if (idRef.current === sid) {
        appendMsg({
          id: res?.message_id,
          role: "assistant",
          model: "human",
          content: c,
          attachments: res?.attachments || atts,
        });
        setText("");
        setPending([]);
        atBottomRef.current = true; // 自己发消息始终回到底部
        taRef.current?.focus();
      }
    } catch (e) {
      toast(apiError(e), "error");
    } finally {
      setSending(false);
    }
  };

  // 长文本输入自动撑高（上限 120px），清空后回落
  const autoGrow = (el: HTMLTextAreaElement | null) => {
    if (!el) return;
    el.style.height = "auto";
    el.style.height = Math.min(el.scrollHeight, 120) + "px";
  };
  useEffect(() => {
    if (!text) autoGrow(taRef.current);
  }, [text]);

  // 告诉客户坐席正在输入（节流 ~2.5s）
  const onComposerTyping = () => {
    const now = Date.now();
    if (idRef.current && now - lastTypingRef.current > 2500) {
      lastTypingRef.current = now;
      sendTyping(idRef.current);
    }
  };

  const s = detail?.session;
  const inTakeover = s?.status === "human_takeover";
  // 目前只有网页渠道能把坐席附件推给客户（企微推送尚未打通）
  const canAttach = s?.channel_type === "web";
  const ticketOpen = !!ticket && ticket.status === "open";
  const ticketMine = !!ticket && ticket.status === "in_progress" && !!ticket.assignee_id && ticket.assignee_id === user?.id;
  const ticketOther = !!ticket && ticket.status === "in_progress" && !ticketMine;
  const st = sessionStatusMeta(s?.status);

  // 接管中自动聚焦输入框，进来即可打字
  useEffect(() => {
    if (inTakeover) taRef.current?.focus();
  }, [inTakeover, id]);

  // 底部提示（未接管时）
  const composerHint = inTakeover
    ? ""
    : ticketOpen
      ? "认领后可接管对话"
      : ticketMine
        ? "点击上方「接管对话」后即可回复客户"
        : ticketOther
          ? `该会话已由${ticket?.assignee_name || "同事"}跟进`
          : s?.status === "escalated"
            ? "接管后即可回复客户"
            : "会话当前无需人工接管";

  return (
    <div className="app-shell chat-shell">
      <header className="app-header">
        <Link className="back-link" to="/" aria-label="返回收件箱">
          <svg viewBox="0 0 20 20" width="18" height="18" fill="none" aria-hidden="true">
            <path d="M12.5 4.5 7 10l5.5 5.5" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        </Link>
        <div className="app-header-main">
          <div className="app-header-title">
            {s ? s.end_user_display || s.end_user_id || "匿名用户" : "会话"}
          </div>
          {s && (
            <div className="app-header-sub">
              <span className="badge badge-plain">{channelLabel(s.channel_type)}</span>
              <span className={`badge badge-${st.cls}`}>{st.label}</span>
              {ticketMine && <span className="badge badge-blue">我认领的</span>}
              {ticketOther && (
                <span className={`badge badge-${HANDOFF_STATUS.in_progress.cls}`}>
                  {ticket?.assignee_name || "同事"}跟进中
                </span>
              )}
              {ticketOpen && <span className={`badge badge-${HANDOFF_STATUS.open.cls}`}>待认领</span>}
            </div>
          )}
        </div>
      </header>

      {/* 操作条 */}
      {s && (
        <div className="action-bar">
          {!inTakeover && ticketOpen && (
            <button className="btn btn-primary btn-sm" disabled={!!acting}
              onClick={() => act("claim", () => handoffApi.claim(ticket!.id), "认领成功")}>
              {actLabel("claim", "认领", "认领中…")}
            </button>
          )}
          {!inTakeover && (ticketMine || (!ticket && s.status === "escalated")) && (
            <button className="btn btn-primary btn-sm" disabled={!!acting}
              onClick={() => act("takeover", () => conversationApi.takeover(id), "已接管，可直接回复客户")}>
              {actLabel("takeover", "接管对话", "接管中…")}
            </button>
          )}
          {!inTakeover && ticketMine && (
            <button className="btn btn-ghost btn-sm" disabled={!!acting}
              onClick={() => act("unclaim", () => handoffApi.unclaim(ticket!.id), "已退回待认领")}>
              {actLabel("unclaim", "退回", "退回中…")}
            </button>
          )}
          {!inTakeover && s.status === "escalated" && (ticketMine || !ticket) && (
            <button className="btn btn-ghost btn-sm" disabled={!!acting}
              onClick={() => act("handled", () => conversationApi.markHandled(id), "已标记完成")}>
              {actLabel("handled", "标记完成", "标记中…")}
            </button>
          )}
          {inTakeover && (
            <button className="btn btn-ghost btn-sm" disabled={!!acting}
              onClick={() => act("release", () => conversationApi.release(id, true), "已结束接管：AI 接续服务，工单已解决")}>
              {actLabel("release", "结束接管", "结束中…")}
            </button>
          )}
          {inTakeover && (
            <button className="btn btn-danger btn-sm" disabled={!!acting} onClick={() => setConfirmEnd(true)}>
              结束并完成
            </button>
          )}
        </div>
      )}

      {/* 消息流 */}
      <div className="msgs-wrap">
        <div ref={bodyRef} className="msgs" onScroll={onBodyScroll}>
          {!s ? (
            loadError ? (
              <div className="page-center muted">
                <div>会话加载失败：{loadError}</div>
                <button className="btn btn-ghost btn-sm" style={{ marginTop: 10 }}
                  onClick={() => { setLoadError(""); loadDetail(); }}>
                  重试
                </button>
              </div>
            ) : (
              <div className="msgs-sk" aria-hidden="true">
                {[62, 40, 74, 34].map((w, i) => (
                  <div key={i} className={`msg ${i % 2 ? "msg-right" : "msg-left"}`}>
                    <div className="sk sk-line" style={{ width: 64, marginBottom: 4 }} />
                    <div className="sk sk-bubble" style={{ width: `${w}%` }} />
                  </div>
                ))}
              </div>
            )
          ) : (
            (detail.messages || [])
              .filter((m: any) => m.role === "user" || m.role === "assistant")
              .map((m: any) => {
                const isUser = m.role === "user";
                const isHuman = m.model === "human";
                return (
                  <div key={m.id} className={`msg ${isUser ? "msg-left" : "msg-right"}`}>
                    <div className="msg-who">
                      {isUser ? "客户" : isHuman ? "人工" : "AI"}
                      {m.created_at ? ` · ${fmtShort(m.created_at)}` : ""}
                    </div>
                    {m.content && (
                      <div className={`bubble ${isUser ? "bubble-user" : isHuman ? "bubble-human" : "bubble-ai"}`}>
                        {m.content}
                      </div>
                    )}
                    {Array.isArray(m.attachments) && m.attachments.length > 0 && (
                      <div className="attachments">
                        {m.attachments.map((a: any, i: number) => {
                          // 协议白名单过滤后再渲染；不安全的 url（javascript:/data: 等）整条丢弃
                          const url = safeAttachUrl(a?.url);
                          if (!url) return null;
                          return isImageUrl(url) ? (
                            <button
                              key={i}
                              type="button"
                              title="新窗口查看大图"
                              style={{ padding: 0, border: "none", background: "none", cursor: "pointer" }}
                              onClick={() => window.open(url, "_blank", "noopener,noreferrer")}
                            >
                              <img className="attach-img" src={url} alt={a?.name || "图片"} loading="lazy" />
                            </button>
                          ) : (
                            <a key={i} className="attach-file" href={url} target="_blank" rel="noopener noreferrer">
                              📎 {a?.name || "文件"}
                            </a>
                          );
                        })}
                      </div>
                    )}
                  </div>
                );
              })
          )}
        </div>
        {showJump && (
          <button className={`jump-btn${hasNew ? " jump-btn-new" : ""}`} onClick={jumpToBottom}>
            {hasNew ? "有新消息" : "回到底部"}
            <svg viewBox="0 0 12 12" width="11" height="11" fill="none" aria-hidden="true">
              <path d="M2 4.5 6 8.5l4-4" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          </button>
        )}
      </div>

      {/* 客户正在输入 */}
      <div className={`typing-hint${customerTyping ? " typing-hint-on" : ""}`}>
        {customerTyping ? "客户正在输入…" : ""}
      </div>

      {/* 快捷回复面板 */}
      {inTakeover && cannedOpen && (
        <div className="canned-panel">
          {canned.length === 0 ? (
            <div className="muted canned-empty">暂无快捷回复模板</div>
          ) : (
            canned.map((c: any) => (
              <button
                key={c.id}
                className="canned-item"
                onClick={() => {
                  setText((t) => (t ? t + "\n" + c.content : c.content));
                  setCannedOpen(false);
                  taRef.current?.focus();
                }}
              >
                <span className="canned-title">{c.title || c.content.slice(0, 20)}</span>
                <span className="canned-preview">{c.content}</span>
              </button>
            ))
          )}
        </div>
      )}

      {/* 待发送附件 */}
      {inTakeover && (pending.length > 0 || uploading > 0) && (
        <div className="pending-tray">
          {pending.map((a, i) => (
            <div key={a.key} className="pending-item" title={a.name}>
              {a.kind === "image" ? (
                <img className="pending-thumb" src={a.url} alt={a.name} />
              ) : (
                <span className="pending-name">{a.name}</span>
              )}
              <button
                className="pending-remove"
                aria-label={`移除 ${a.name}`}
                onClick={() => setPending((p) => p.filter((_, j) => j !== i))}
              >
                ×
              </button>
            </div>
          ))}
          {uploading > 0 && <div className="pending-item pending-loading muted">上传中…</div>}
        </div>
      )}

      {/* 输入区 */}
      <div className="composer">
        {inTakeover ? (
          <>
            <button
              className={`btn btn-ghost btn-icon composer-canned${cannedOpen ? " composer-canned-on" : ""}`}
              onClick={() => setCannedOpen((v) => !v)}
              title="快捷回复"
              aria-label="快捷回复"
            >
              <svg viewBox="0 0 20 20" width="16" height="16" fill="currentColor" aria-hidden="true">
                <path d="M11.3 1.6 4 11h4.4l-1 7.4L14.9 9h-4.5l.9-7.4z" />
              </svg>
            </button>
            {canAttach && (
              <>
                <button
                  className="btn btn-ghost btn-icon composer-attach"
                  onClick={() => fileRef.current?.click()}
                  disabled={pending.length + uploading >= MAX_ATTACHMENTS}
                  title="发送图片或文件"
                  aria-label="发送图片或文件"
                >
                  <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                    <path d="m21.44 11.05-9.19 9.19a6 6 0 0 1-8.49-8.49l8.57-8.57A4 4 0 1 1 18 8.84l-8.59 8.57a2 2 0 0 1-2.83-2.83l8.49-8.48" />
                  </svg>
                </button>
                <input
                  ref={fileRef}
                  type="file"
                  accept={UPLOAD_ACCEPT}
                  multiple
                  hidden
                  onChange={(e) => {
                    pickFiles(e.target.files);
                    e.target.value = ""; // 允许重复选择同一文件
                  }}
                />
              </>
            )}
            {/* 发送中不 disable：双发已由 submit 的 sending 守卫兜住，保持可输入且焦点不丢 */}
            <textarea
              ref={taRef}
              className="composer-input"
              rows={1}
              placeholder="输入回复，回车发送"
              title="回车发送，Shift+回车换行"
              value={text}
              onChange={(e) => {
                setText(e.target.value);
                autoGrow(e.target);
                onComposerTyping();
              }}
              onKeyDown={(e) => {
                if (e.key !== "Enter" || e.shiftKey) return;
                if (isImeComposing(e)) return; // 输入法选词上屏的回车不发送
                e.preventDefault();
                submit();
              }}
            />
            <button
              className="btn btn-primary btn-sm"
              disabled={sending || uploading > 0 || (!text.trim() && !pending.length)}
              onClick={submit}
            >
              {sending ? "发送中…" : "发送"}
            </button>
          </>
        ) : (
          <div className="composer-hint muted">{s ? composerHint : ""}</div>
        )}
      </div>

      {/* 结束并完成 确认弹层 */}
      {confirmEnd && (
        <div className="overlay" onClick={() => setConfirmEnd(false)}>
          <div className="dialog" onClick={(e) => e.stopPropagation()}>
            <div className="dialog-title">结束接管并关闭该会话？</div>
            <div className="dialog-body">
              客户将收到「本次人工服务已结束」，无法继续此对话；对应工单会自动标记为已解决。
            </div>
            <div className="dialog-actions">
              <button className="btn btn-ghost" onClick={() => setConfirmEnd(false)}>取消</button>
              <button
                className="btn btn-danger"
                disabled={!!acting}
                onClick={async () => {
                  setConfirmEnd(false);
                  await act("end", () => conversationApi.release(id, false), "已结束并完成");
                }}
              >
                {actLabel("end", "结束并完成", "处理中…")}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
