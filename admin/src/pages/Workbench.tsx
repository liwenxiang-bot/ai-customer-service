import { useEffect, useRef, useState } from "react";
import { App as AntApp, Button, Dropdown, Empty, Input, Segmented, Tag, Tooltip, theme } from "antd";
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

  const [filter, setFilter] = useState<string>("pending");
  const [list, setList] = useState<any[]>([]);
  const [counts, setCounts] = useState<any>({ waiting: 0, takeover: 0, all: 0 });
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<any>(null);
  const [replyText, setReplyText] = useState("");
  const [sending, setSending] = useState(false);
  const [customerTyping, setCustomerTyping] = useState(false);
  const bodyRef = useRef<HTMLDivElement>(null);
  const typingHideRef = useRef<ReturnType<typeof setTimeout> | null>(null); // auto-hide "客户正在输入"
  const lastTypingRef = useRef(0); // throttle outbound operator typing pings

  // Deep-link from a handoff ticket (?session=ID) → preselect that conversation.
  useEffect(() => {
    const sid = new URLSearchParams(window.location.search).get("session");
    if (sid) setSelectedId(sid);
  }, []);

  // Quick-reply templates (inserted into the composer during takeover).
  const [canned, setCanned] = useState<any[]>([]);
  useEffect(() => { cannedApi.list().then((d) => setCanned(d.items)).catch(() => {}); }, []);

  // ---- poll the queue ----
  const loadList = () => {
    const params =
      filter === "takeover" ? { status: "human_takeover" } :
      filter === "all" ? { attention: true } :
      { pending_human: true };  // 待接待 = waiting (status=escalated)
    conversationApi.list({ ...params, page_size: 50 }).then((d) => setList(d.items)).catch(() => {});
    conversationApi.queueCounts().then(setCounts).catch(() => {});
  };
  useEffect(() => {
    loadList();
    const t = setInterval(loadList, 15000);
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filter]);

  // ---- poll the selected conversation ----
  const loadDetail = () => {
    if (selectedId) conversationApi.detail(selectedId).then(setDetail).catch(() => {});
  };
  useEffect(() => {
    setDetail(null);
    if (!selectedId) return;
    loadDetail();
    const t = setInterval(loadDetail, 15000);
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedId]);

  useEffect(() => {
    if (bodyRef.current) bodyRef.current.scrollTop = bodyRef.current.scrollHeight;
  }, [detail?.messages?.length]);

  useEffect(() => () => { if (typingHideRef.current) clearTimeout(typingHideRef.current); }, []);

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
    onQueue: () => loadList(),
    onSession: (msg: any) => {
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
  const onComposerChange = (v: string) => {
    setReplyText(v);
    const now = Date.now();
    if (selectedId && now - lastTypingRef.current > 2500) { lastTypingRef.current = now; sendTyping(selectedId); }
  };

  const s = detail?.session;
  const inTakeover = s?.status === "human_takeover";

  const act = async (fn: () => Promise<any>, ok: string) => {
    try { await fn(); message.success(ok); loadDetail(); loadList(); }
    catch (e) { message.error(apiError(e)); }
  };
  const sendReply = async () => {
    const c = replyText.trim();
    if (!c) return;
    setSending(true);
    try {
      const res = await conversationApi.reply(selectedId!, c);
      setReplyText("");
      // Show our own reply immediately (no full reload); the realtime echo dedups by id.
      appendMsg({ id: res?.message_id, role: "assistant", model: "human", content: c });
    } catch (e) { message.error(apiError(e)); }
    finally { setSending(false); }
  };

  const statusTag = (it: any) =>
    it.status === "human_takeover" ? <Tag color="green">接管中</Tag>
    : it.status === "escalated" ? <Tag color="orange">待接待</Tag>
    : <Tag>{it.status}</Tag>;

  return (
    <div>
      <div className="acs-page-title">坐席工作台</div>
      <div className="acs-page-sub">实时接待需要人工处理的会话：接管后可直接与客户对话，AI 自动让位。</div>

      <div style={{ display: "flex", gap: 14, height: "calc(100vh - 150px)" }}>
        {/* ---- queue ---- */}
        <div style={{ width: 300, display: "flex", flexDirection: "column", background: panel, border: `1px solid ${line}`, borderRadius: 8, overflow: "hidden" }}>
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
            {list.length === 0 ? (
              <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="当前没有待接待的会话" style={{ marginTop: 60 }} />
            ) : (
              list.map((it) => (
                <div
                  key={it.id}
                  onClick={() => setSelectedId(it.id)}
                  style={{
                    padding: "11px 12px", borderBottom: `1px solid ${line}`, cursor: "pointer",
                    background: selectedId === it.id ? token.colorPrimaryBg : "transparent",
                  }}
                >
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 6 }}>
                    <span style={{ fontWeight: 600, fontSize: 13, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
                      {it.end_user_display || it.end_user_id || "匿名用户"}
                    </span>
                    {statusTag(it)}
                  </div>
                  <div style={{ color: muted, fontSize: 12, marginTop: 3, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
                    {it.title || "（无内容）"}
                  </div>
                  <div style={{ color: token.colorTextTertiary, fontSize: 11, marginTop: 3 }}>
                    <Tag bordered={false} style={{ fontSize: 10, lineHeight: "16px", padding: "0 5px", marginRight: 4 }}>{it.channel_type}</Tag>
                    {fmtShort(it.last_activity_at)}
                  </div>
                </div>
              ))
            )}
          </div>
        </div>

        {/* ---- chat panel ---- */}
        <div style={{ flex: 1, display: "flex", flexDirection: "column", background: panel, border: `1px solid ${line}`, borderRadius: 8, overflow: "hidden" }}>
          {!s ? (
            <div style={{ flex: 1, display: "grid", placeItems: "center" }}>
              <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="从左侧选择一个会话开始接待" />
            </div>
          ) : (
            <>
              {/* top bar */}
              <div style={{ padding: "10px 16px", borderBottom: `1px solid ${line}`, display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8 }}>
                <div style={{ minWidth: 0 }}>
                  <span style={{ fontWeight: 600 }}>{s.end_user_display || s.end_user_id || "匿名用户"}</span>
                  <Tag bordered={false} style={{ marginLeft: 8 }}>{s.channel_type}</Tag>
                  {inTakeover ? <Tag color="green">接管中</Tag> : s.escalated ? <Tag color="red">待人工</Tag> : null}
                </div>
                {editable && (
                  <div style={{ display: "flex", gap: 8, flexShrink: 0 }}>
                    {!inTakeover && <Button type="primary" icon={<CustomerServiceOutlined />} onClick={() => act(() => conversationApi.takeover(selectedId!), "已接管，可直接回复客户")}>接管对话</Button>}
                    {inTakeover && <Button onClick={() => act(() => conversationApi.release(selectedId!, true), "已结束接管，AI 恢复")}>结束接管</Button>}
                    {inTakeover && <Button danger icon={<CheckOutlined />} onClick={() => act(() => conversationApi.release(selectedId!, false), "已结束并完成")}>结束并完成</Button>}
                    {!inTakeover && s.escalated && <Button icon={<CheckOutlined />} onClick={() => act(() => conversationApi.markHandled(selectedId!), "已标记完成")}>标记完成</Button>}
                  </div>
                )}
              </div>

              {/* messages */}
              <div ref={bodyRef} style={{ flex: 1, overflowY: "auto", padding: 16, background: token.colorBgLayout, display: "flex", flexDirection: "column", gap: 10 }}>
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
                  <div style={{ display: "flex", gap: 8, alignItems: "flex-end" }}>
                    <Input.TextArea
                      value={replyText}
                      onChange={(e) => onComposerChange(e.target.value)}
                      autoSize={{ minRows: 1, maxRows: 4 }}
                      placeholder="输入回复，回车发送给客户（Shift+Enter 换行）"
                      onPressEnter={(e) => { if (!e.shiftKey) { e.preventDefault(); sendReply(); } }}
                    />
                    <Dropdown
                      trigger={["click"]}
                      menu={{
                        items: canned.length
                          ? canned.map((c) => ({ key: c.id, label: c.title || c.content.slice(0, 24) }))
                          : [{ key: "none", label: "暂无模板", disabled: true }],
                        onClick: ({ key }) => {
                          const c = canned.find((x) => x.id === key);
                          if (c) setReplyText((t) => (t ? t + "\n" + c.content : c.content));
                        },
                      }}
                    >
                      <Button icon={<ThunderboltOutlined />}>快捷回复</Button>
                    </Dropdown>
                    <Button type="primary" icon={<SendOutlined />} loading={sending} onClick={sendReply}>发送</Button>
                  </div>
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
