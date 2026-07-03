import { useEffect, useRef, useState } from "react";
import { Alert, App as AntApp, Button, Dropdown, Empty, Grid, Input, Popconfirm, Segmented, Spin, Tag, Tooltip, theme } from "antd";
import {
  CheckOutlined,
  CustomerServiceOutlined,
  PaperClipOutlined,
  ReloadOutlined,
  RobotOutlined,
  SendOutlined,
  ThunderboltOutlined,
  UserOutlined,
} from "@ant-design/icons";
import { cannedApi, conversationApi } from "../api";
import { apiError } from "../api/client";
import { useAuth, canEdit } from "../auth";
import { fmtShort } from "../utils/time";
import { useRealtime } from "../hooks/useRealtime";
import { usePolling } from "../hooks/usePolling";
import { isImeComposing } from "../utils/ime";
import { CHANNEL_TYPE, SESSION_STATUS, statusMeta } from "../utils/status";

/** 回复输入区 —— 草稿文本放在本地 state，敲字只重渲染这一小块，
 *  不再带动整个工作台（50 项队列 + 全部消息气泡）重渲染；提交成功后自行清空。 */
function Composer({ canned, sending, onSend, onTyping }: {
  canned: any[];
  sending: boolean;
  onSend: (content: string) => Promise<boolean>;
  onTyping: () => void;
}) {
  const [text, setText] = useState("");
  const submit = async () => {
    const c = text.trim();
    if (!c) return;
    if (await onSend(c)) setText("");
  };
  return (
    <div style={{ display: "flex", gap: 8, alignItems: "flex-end" }}>
      <Input.TextArea
        value={text}
        onChange={(e) => { setText(e.target.value); onTyping(); }}
        autoSize={{ minRows: 1, maxRows: 4 }}
        placeholder="输入回复，回车发送给客户（Shift+Enter 换行）"
        onPressEnter={(e) => {
          if (isImeComposing(e)) return; // 输入法选词上屏的回车不发送
          if (!e.shiftKey) { e.preventDefault(); submit(); }
        }}
      />
      <Dropdown
        trigger={["click"]}
        menu={{
          items: canned.length
            ? canned.map((c) => ({ key: c.id, label: c.title || c.content.slice(0, 24) }))
            : [{ key: "none", label: "暂无模板", disabled: true }],
          onClick: ({ key }) => {
            const c = canned.find((x) => x.id === key);
            if (c) setText((t) => (t ? t + "\n" + c.content : c.content));
          },
        }}
      >
        <Button icon={<ThunderboltOutlined />}>快捷回复</Button>
      </Dropdown>
      <Button type="primary" icon={<SendOutlined />} loading={sending} onClick={submit}>发送</Button>
    </div>
  );
}

const AGENT_NOTICE_KEY = "acs_agent_console_notice_closed"; // 顶部坐席站上线提示的关闭状态

/** 坐席工作台 — a two-pane live console for handling conversations that need a human:
 *  left = auto-refreshing queue (待人工 / 接管中), right = chat + takeover controls. */
export function Workbench() {
  const { user } = useAuth();
  const editable = canEdit(user?.role);
  const { message } = AntApp.useApp();
  const { token } = theme.useToken();
  const line = token.colorBorderSecondary;
  const panel = token.colorBgContainer;
  const muted = token.colorTextSecondary;
  // 窄屏（<992px）降级：队列与聊天面板纵向堆叠。首帧 screens 为空对象时按宽屏渲染，避免桌面端闪一下堆叠布局。
  const screens = Grid.useBreakpoint();
  const narrow = screens.lg === false;

  const [filter, setFilter] = useState<string>("pending");
  const [agentNoticeClosed, setAgentNoticeClosed] = useState(() => localStorage.getItem(AGENT_NOTICE_KEY) === "1");
  const [list, setList] = useState<any[]>([]);
  const [listError, setListError] = useState<string | null>(null); // 队列拉取失败提示（保留旧数据）
  const [counts, setCounts] = useState<any>({ waiting: 0, takeover: 0, all: 0 });
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<any>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [sending, setSending] = useState(false);
  const [customerTyping, setCustomerTyping] = useState(false);
  const bodyRef = useRef<HTMLDivElement>(null);
  const typingHideRef = useRef<ReturnType<typeof setTimeout> | null>(null); // auto-hide "客户正在输入"
  const lastTypingRef = useRef(0); // throttle outbound operator typing pings
  const selectedIdRef = useRef<string | null>(null); // WS 回调 / await 之后读最新选中值，避免闭包串会话
  selectedIdRef.current = selectedId;

  // Deep-link from a handoff ticket (?session=ID) → preselect that conversation.
  useEffect(() => {
    const sid = new URLSearchParams(window.location.search).get("session");
    if (sid) setSelectedId(sid);
  }, []);

  // Quick-reply templates (inserted into the composer during takeover).
  const [canned, setCanned] = useState<any[]>([]);
  useEffect(() => { cannedApi.list().then((d) => setCanned(d.items)).catch(() => {}); }, []);

  // ---- poll the queue (usePolling：页面隐藏自动暂停，回前台立即补一次) ----
  const filterRef = useRef(filter); // scheduleLoadList 的 400ms 定时器闭包可能过期，始终经 ref 读最新 tab
  filterRef.current = filter;
  const listSeq = useRef(0); // 过期响应保护：只允许最新一次请求落地（同 Conversations 的 loadSeq 模式）
  const loadList = () => {
    const f = filterRef.current;
    const seq = ++listSeq.current;
    const params =
      f === "takeover" ? { status: "human_takeover" } :
      f === "all" ? { attention: true } :
      { pending_human: true };  // 待接待 = waiting (status=escalated)
    conversationApi.list({ ...params, page_size: 50 })
      .then((d) => {
        // 落地时校验：已切 tab 或已有更新的请求发出，旧响应直接丢弃，不覆盖当前列表
        if (seq !== listSeq.current || filterRef.current !== f) return;
        setList(d.items); setListError(null);
      })
      .catch((e) => { if (seq === listSeq.current && filterRef.current === f) setListError(apiError(e)); }); // 保留旧数据，列表顶部提示错误
    conversationApi.queueCounts().then(setCounts).catch(() => {});
  };
  usePolling(loadList, 15000, { deps: [filter] });

  // WS 队列事件突发时合并刷新（400ms 尾随），避免瞬间打出多轮 list+counts 请求
  const queueTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const scheduleLoadList = () => {
    if (queueTimerRef.current) return;
    queueTimerRef.current = setTimeout(() => { queueTimerRef.current = null; loadList(); }, 400);
  };

  // ---- poll the selected conversation ----
  const loadDetail = () => {
    const sid = selectedIdRef.current;
    if (!sid) return;
    conversationApi.detail(sid)
      .then((d) => {
        if (selectedIdRef.current !== sid) return;
        // 按 message.id 合并：快照请求在途期间 appendMsg 可能已追加实时消息（发送回执 / WS 推送），
        // 慢返回的旧快照整体替换会把它们吞掉——保留本地已有而快照缺失的尾部消息。
        setDetail((prev: any) => {
          if (!prev?.messages?.length) return d;
          const seen = new Set((d.messages || []).map((x: any) => x.id));
          const extra = prev.messages.filter((x: any) => x.id && !seen.has(x.id));
          return extra.length ? { ...d, messages: [...(d.messages || []), ...extra] } : d;
        });
        setDetailLoading(false);
      })
      .catch(() => { if (selectedIdRef.current === sid) setDetailLoading(false); });
  };
  useEffect(() => {
    setDetail(null);
    setDetailLoading(!!selectedId);
  }, [selectedId]);
  usePolling(loadDetail, 15000, { enabled: !!selectedId, deps: [selectedId] });

  useEffect(() => {
    if (bodyRef.current) bodyRef.current.scrollTop = bodyRef.current.scrollHeight;
  }, [detail?.messages?.length]);

  useEffect(() => () => {
    if (typingHideRef.current) clearTimeout(typingHideRef.current);
    if (queueTimerRef.current) clearTimeout(queueTimerRef.current);
  }, []);

  // Append one live message instead of refetching the whole thread — keeps the operator's
  // view as instant as the customer's (which appends incrementally). Dedup by id so the
  // realtime echo of our own reply doesn't double up with the optimistic append on send.
  const appendMsg = (m: any) => {
    if (!m?.id) return;
    setDetail((d: any) =>
      !d || (d.messages || []).some((x: any) => x.id === m.id)
        ? d
        : { ...d, messages: [...(d.messages || []), m] }
    );
  };

  // Realtime: refresh the queue on backend queue events; on a live message, append it in place
  // (instant) rather than refetching; only a status change re-pulls the thread. 15s poll = fallback.
  const showCustomerTyping = () => {
    setCustomerTyping(true);
    if (typingHideRef.current) clearTimeout(typingHideRef.current);
    typingHideRef.current = setTimeout(() => setCustomerTyping(false), 4000);
  };
  const { watch, sendTyping } = useRealtime({
    onQueue: scheduleLoadList,
    onSession: (msg: any) => {
      // 载荷带 session_id（后端契约）：不属于当前选中会话的消息不进面板，
      // 只安排一次队列刷新，避免快速切换会话时消息串线。
      if (msg?.session_id && msg.session_id !== selectedIdRef.current) { scheduleLoadList(); return; }
      setCustomerTyping(false); // a real message supersedes the typing hint
      if (msg?.type === "human_message" && msg.message_id)
        appendMsg({ id: msg.message_id, role: "assistant", model: "human", content: msg.content });
      else if (msg?.type === "customer_message" && msg.message_id)
        appendMsg({ id: msg.message_id, role: "user", content: msg.content, attachments: msg.attachments || [] });
      else loadDetail(); // takeover / resume / end → refresh session status + thread
    },
    onTyping: showCustomerTyping,
  });
  useEffect(() => { watch(selectedId || ""); setCustomerTyping(false); }, [selectedId]);

  // Tell the customer the operator is typing (throttled ~2.5s; backend relays to their chat).
  const onComposerTyping = () => {
    const now = Date.now();
    const sid = selectedIdRef.current;
    if (sid && now - lastTypingRef.current > 2500) { lastTypingRef.current = now; sendTyping(sid); }
  };

  const s = detail?.session;
  const inTakeover = s?.status === "human_takeover";

  const act = async (fn: () => Promise<any>, ok: string) => {
    try { await fn(); message.success(ok); loadDetail(); loadList(); }
    catch (e) { message.error(apiError(e)); }
  };
  const sendReply = async (content: string) => {
    const sid = selectedIdRef.current;
    if (!sid) return false;
    setSending(true);
    try {
      const res = await conversationApi.reply(sid, content);
      // await 期间可能已切走会话：仍停留在原会话时才做乐观追加，避免串进别的面板。
      if (selectedIdRef.current === sid)
        appendMsg({ id: res?.message_id, role: "assistant", model: "human", content });
      return true;
    } catch (e) { message.error(apiError(e)); return false; }
    finally { setSending(false); }
  };

  // 状态 Tag 统一取自 utils/status.ts，保证跨页文案/颜色一致
  const statusTag = (st?: string) => {
    const m = statusMeta(SESSION_STATUS, st);
    return <Tag color={m.color}>{m.label}</Tag>;
  };

  // 认领徽标：待人工队列每项附 ticket（无工单或已解决则不显示；其他队列无此字段自然跳过）
  const metaTagStyle = { fontSize: 10, lineHeight: "16px", padding: "0 5px", marginRight: 4 } as const;
  const ticketBadge = (t: any) => {
    if (!t || t.status === "resolved") return null;
    if (!t.assignee_id) return <Tag bordered={false} color="gold" style={metaTagStyle}>待认领</Tag>;
    if (t.assignee_id === user?.id) return <Tag bordered={false} color="green" style={metaTagStyle}>我认领的</Tag>;
    return <Tag bordered={false} color="default" style={metaTagStyle}>已被 {t.assignee_name || "同事"} 认领</Tag>;
  };

  return (
    // Fill the padded content area exactly (viewport − 64px header − 48px acs-content padding)
    // so the panels end on the 24px bottom gap like every other page, not at the edge.
    // 窄屏不锁死高度：面板纵向堆叠后随内容自适应，整页滚动。
    <div style={{ display: "flex", flexDirection: "column", height: narrow ? undefined : "calc(100vh - 112px)" }}>
      {/* 队列项键盘聚焦样式（index.css 无现成 class，就近声明 :focus-visible） */}
      <style>{`.acs-queue-item:focus-visible { outline: 2px solid ${token.colorPrimary}; outline-offset: -2px; }`}</style>
      <div className="acs-page-title">坐席工作台</div>
      <div className="acs-page-sub">实时接待需要人工处理的会话：接管后可直接与客户对话，AI 自动让位。</div>

      {!agentNoticeClosed && (
        <Alert
          type="info" showIcon closable
          style={{ marginBottom: 12, flexShrink: 0 }}
          message={<>全新独立坐席工作台已上线（手机可用）：<a href="/agent/" target="_blank" rel="noreferrer">/agent/</a></>}
          onClose={() => { localStorage.setItem(AGENT_NOTICE_KEY, "1"); setAgentNoticeClosed(true); }}
        />
      )}

      <div style={{ display: "flex", flexDirection: narrow ? "column" : "row", gap: 14, flex: 1, minHeight: 0 }}>
        {/* ---- queue ----（窄屏限高 240px 内部滚动，宽屏固定 300px 侧栏） */}
        <div style={{ ...(narrow ? { maxHeight: 240, flexShrink: 0 } : { width: 300 }), display: "flex", flexDirection: "column", background: panel, border: `1px solid ${line}`, borderRadius: 8, overflow: "hidden" }}>
          <div style={{ padding: "10px 12px", borderBottom: `1px solid ${line}`, display: "flex", alignItems: "center", justifyContent: "space-between" }}>
            <Segmented
              size="small"
              value={filter}
              onChange={(v) => setFilter(v as string)}
              options={[
                { label: `待接待 ${counts.waiting}`, value: "pending" },
                { label: `接管中 ${counts.takeover}`, value: "takeover" },
                { label: `全部 ${counts.all}`, value: "all" },
              ]}
            />
            <Tooltip title="刷新"><Button size="small" type="text" icon={<ReloadOutlined />} onClick={loadList} /></Tooltip>
          </div>
          <div style={{ flex: 1, overflowY: "auto" }}>
            {listError && (
              <Alert banner type="error" showIcon message={`队列刷新失败：${listError}`} style={{ fontSize: 12 }} />
            )}
            {list.length === 0 ? (
              !listError && <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="当前没有待接待的会话" style={{ marginTop: 60 }} />
            ) : (
              list.map((it) => (
                <div
                  key={it.id}
                  className="acs-queue-item"
                  role="button"
                  tabIndex={0}
                  onClick={() => setSelectedId(it.id)}
                  onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); setSelectedId(it.id); } }}
                  style={{
                    padding: "11px 12px", borderBottom: `1px solid ${line}`, cursor: "pointer",
                    background: selectedId === it.id ? token.colorPrimaryBg : "transparent",
                  }}
                >
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 6 }}>
                    <span style={{ fontWeight: 600, fontSize: 13, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
                      {it.end_user_display || it.end_user_id || "匿名用户"}
                    </span>
                    {statusTag(it.status)}
                  </div>
                  <div style={{ color: muted, fontSize: 12, marginTop: 3, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
                    {it.title || "（无内容）"}
                  </div>
                  <div style={{ color: token.colorTextTertiary, fontSize: 11, marginTop: 3 }}>
                    <Tag bordered={false} style={metaTagStyle}>{statusMeta(CHANNEL_TYPE, it.channel_type).label}</Tag>
                    {ticketBadge(it.ticket)}
                    {fmtShort(it.last_activity_at)}
                  </div>
                </div>
              ))
            )}
          </div>
        </div>

        {/* ---- chat panel ----（窄屏给最小高度撑起空态/加载态，消息区另行限高内部滚动） */}
        <div style={{ flex: 1, ...(narrow ? { minHeight: 360 } : {}), display: "flex", flexDirection: "column", background: panel, border: `1px solid ${line}`, borderRadius: 8, overflow: "hidden" }}>
          {!s ? (
            <div style={{ flex: 1, display: "grid", placeItems: "center" }}>
              {!selectedId ? (
                <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="从左侧选择一个会话开始接待" />
              ) : detailLoading ? (
                <div style={{ textAlign: "center", color: muted }}>
                  <Spin />
                  <div style={{ marginTop: 10, fontSize: 13 }}>会话加载中…</div>
                </div>
              ) : (
                <div style={{ textAlign: "center" }}>
                  <div style={{ color: muted, fontSize: 13, marginBottom: 12 }}>会话加载失败，请重试</div>
                  <Button icon={<ReloadOutlined />} onClick={() => { setDetailLoading(true); loadDetail(); }}>重试</Button>
                </div>
              )}
            </div>
          ) : (
            <>
              {/* top bar */}
              <div style={{ padding: "10px 16px", borderBottom: `1px solid ${line}`, display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8 }}>
                <div style={{ minWidth: 0 }}>
                  <span style={{ fontWeight: 600 }}>{s.end_user_display || s.end_user_id || "匿名用户"}</span>
                  <Tag bordered={false} style={{ marginLeft: 8 }}>{statusMeta(CHANNEL_TYPE, s.channel_type).label}</Tag>
                  {inTakeover ? statusTag("human_takeover") : s.escalated ? statusTag("escalated") : null}
                </div>
                {editable && (
                  <div style={{ display: "flex", gap: 8, flexShrink: 0 }}>
                    {!inTakeover && <Button type="primary" icon={<CustomerServiceOutlined />} onClick={() => act(() => conversationApi.takeover(selectedId!), "已接管，可直接回复客户")}>接管对话</Button>}
                    {inTakeover && <Button onClick={() => act(() => conversationApi.release(selectedId!, true), "已结束接管，AI 恢复")}>结束接管</Button>}
                    {inTakeover && (
                      <Popconfirm title="结束接管并关闭该会话？" description="客户将收到「本次人工服务已结束」，无法继续此对话。" okText="结束" okButtonProps={{ danger: true }}
                        onConfirm={() => act(() => conversationApi.release(selectedId!, false), "已结束并完成")}>
                        <Button danger icon={<CheckOutlined />}>结束并完成</Button>
                      </Popconfirm>
                    )}
                    {!inTakeover && s.escalated && <Button icon={<CheckOutlined />} onClick={() => act(() => conversationApi.markHandled(selectedId!), "已标记完成")}>标记完成</Button>}
                  </div>
                )}
              </div>

              {/* messages —— 窄屏外层不锁高，给消息区 55vh 上限保证内部滚动（自动滚底依赖它），输入框始终可见 */}
              <div ref={bodyRef} style={{ flex: 1, ...(narrow ? { maxHeight: "55vh" } : {}), overflowY: "auto", padding: 16, background: token.colorBgLayout, display: "flex", flexDirection: "column", gap: 10 }}>
                {(detail.messages || []).filter((m: any) => m.role === "user" || m.role === "assistant").map((m: any) => {
                  const isUser = m.role === "user";
                  const isHuman = m.model === "human";
                  return (
                    <div key={m.id} style={{ display: "flex", flexDirection: "column", alignItems: isUser ? "flex-end" : "flex-start", maxWidth: "76%", alignSelf: isUser ? "flex-end" : "flex-start" }}>
                      <div style={{ fontSize: 11, color: muted, marginBottom: 2 }}>
                        {isUser ? <><UserOutlined /> 客户</> : isHuman ? <><CustomerServiceOutlined style={{ color: "#0f9d6e" }} /> 人工</> : <><RobotOutlined style={{ color: "#0F766E" }} /> AI</>}
                      </div>
                      {m.content && (
                        <div style={{
                          padding: "8px 12px", borderRadius: 12, fontSize: 14, lineHeight: 1.55, whiteSpace: "pre-wrap", wordBreak: "break-word",
                          background: isUser ? "#0F766E" : panel, color: isUser ? "#fff" : token.colorText,
                          border: isUser ? "none" : `1px solid ${line}`,
                          borderBottomRightRadius: isUser ? 4 : 12, borderBottomLeftRadius: isUser ? 12 : 4,
                        }}>{m.content}</div>
                      )}
                      {m.attachments?.length > 0 && (
                        <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginTop: 4, justifyContent: isUser ? "flex-end" : "flex-start" }}>
                          {m.attachments.map((a: any, i: number) =>
                            a.kind === "image" || (a.content_type || "").startsWith("image/") ? (
                              <a key={i} href={a.url} target="_blank" rel="noreferrer">
                                <img src={a.url} alt={a.name} style={{ width: 90, height: 90, objectFit: "cover", borderRadius: 8, border: `1px solid ${line}` }} />
                              </a>
                            ) : (
                              <a key={i} href={a.url} target="_blank" rel="noreferrer" style={{ fontSize: 12 }}>
                                <PaperClipOutlined /> {a.name || "文件"}
                              </a>
                            )
                          )}
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>

              {/* live "customer is typing" hint */}
              <div style={{ height: 18, padding: "0 16px", fontSize: 12, color: muted, display: "flex", alignItems: "center", gap: 6, opacity: customerTyping ? 1 : 0, transition: "opacity .15s" }}>
                {customerTyping && <><UserOutlined /> 客户正在输入…</>}
              </div>

              {/* composer */}
              <div style={{ borderTop: `1px solid ${line}`, padding: 12 }}>
                {inTakeover ? (
                  <Composer canned={canned} sending={sending} onSend={sendReply} onTyping={onComposerTyping} />
                ) : (
                  <div style={{ textAlign: "center", color: muted, fontSize: 13, padding: "6px 0" }}>
                    点击右上角「接管对话」后即可直接回复客户
                  </div>
                )}
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
