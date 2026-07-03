import { useEffect, useRef, useState } from "react";
import {
  Card, Table, Tag, Space, Select, Input, Drawer, Timeline, Descriptions, Button,
  Collapse, Empty, Tooltip, App as AntApp, Modal, Form, DatePicker, Popconfirm, theme,
} from "antd";
import { RobotOutlined, UserOutlined, ToolOutlined, BookOutlined, DownloadOutlined, PaperClipOutlined } from "@ant-design/icons";
import { conversationApi } from "../api";
import { apiError } from "../api/client";
import { useAuth, canEdit } from "../auth";
import { useDebounce } from "../hooks/useDebounce";
import { usePolling } from "../hooks/usePolling";
import { fmtTime } from "../utils/time";
import { isImeComposing } from "../utils/ime";
import { CHANNEL_TYPE, SESSION_STATUS, statusMeta } from "../utils/status";

/** 会话状态 Tag：接管中优先，其次待人工旗标，其余按状态映射文案与颜色。 */
const sessionTag = (status: string, escalated?: boolean) => {
  const m = statusMeta(SESSION_STATUS, status === "human_takeover" ? status : escalated ? "escalated" : status);
  return <Tag color={m.color}>{m.label}</Tag>;
};

export function Conversations() {
  const { user } = useAuth();
  const { message } = AntApp.useApp();
  const editable = canEdit(user?.role);
  const [data, setData] = useState<any>({ items: [], total: 0 });
  const [loading, setLoading] = useState(false);
  const [params, setParams] = useState<any>({ page: 1, page_size: 10, channel_type: "", pending_human: false, status: "", q: "", date_from: "", date_to: "", feedback: "", degraded: false });
  const [detailSid, setDetailSid] = useState("");
  const [exporting, setExporting] = useState(false);
  const [search, setSearch] = useState("");
  const dq = useDebounce(search, 400);
  useEffect(() => { setParams((p: any) => ({ ...p, q: dq, page: 1 })); }, [dq]);

  const doExport = async () => {
    setExporting(true);
    try { await conversationApi.exportCsv({ ...params, page: undefined, page_size: undefined }); }
    catch (e) { message.error(apiError(e)); }
    finally { setExporting(false); }
  };

  // 递增序号防过期响应：快速切换筛选/翻页时，只有最新一次请求的结果才落地。
  const loadSeq = useRef(0);
  const load = () => {
    const seq = ++loadSeq.current;
    setLoading(true);
    conversationApi.list(params)
      .then((d) => { if (seq === loadSeq.current) setData(d); })
      .catch((e) => { if (seq === loadSeq.current) message.error(apiError(e)); })
      .finally(() => { if (seq === loadSeq.current) setLoading(false); });
  };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { load(); }, [params.page, params.page_size, params.channel_type, params.pending_human, params.status, params.q, params.date_from, params.date_to, params.feedback, params.degraded]);

  // 抽屉只记录会话 id：首拉与轮询统一由 ConversationDetail 内的 usePolling 负责
  // （挂载/换 sid 立即执行一次），避免打开时「预取 + 轮询首拉」重复请求两次详情。
  const openDetail = (id: string) => setDetailSid(id);

  // Deep-link from a handoff notification (?session=ID) → auto-open that conversation.
  useEffect(() => {
    const sid = new URLSearchParams(window.location.search).get("session");
    if (sid) openDetail(sid);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const columns = [
    { title: "标题", dataIndex: "title", render: (t: string) => t || "—" },
    { title: "渠道", dataIndex: "channel_type", width: 110, render: (c: string) => <Tag>{statusMeta(CHANNEL_TYPE, c).label}</Tag> },
    { title: "用户", dataIndex: "end_user_id", width: 160, ellipsis: true, render: (u: string, r: any) => r.end_user_display || u },
    { title: "轮数", dataIndex: "message_count", width: 70 },
    { title: "状态", dataIndex: "status", width: 110, render: (s: string, r: any) => sessionTag(s, r.escalated) },
    { title: "最近活动", dataIndex: "last_activity_at", width: 170, render: (t: string) => fmtTime(t) },
    { title: "", width: 70, render: (_: any, r: any) => <Button type="link" size="small" style={{ padding: 0 }} onClick={() => openDetail(r.id)}>详情</Button> },
  ];

  return (
    <div>
      <div className="acs-page-title">对话记录</div>
      <div className="acs-page-sub">查看全部历史会话与详情（工具调用、引用来源、调用链路），可一键加入知识库。实时接待请用「坐席工作台」。</div>
      <Card>
        <Space style={{ marginBottom: 12 }} wrap>
          <Input allowClear placeholder="搜索标题/内容/用户" style={{ width: 200 }} value={search} onChange={(e) => setSearch(e.target.value)} />
          <DatePicker.RangePicker
            onChange={(d: any) => setParams((p: any) => ({ ...p, date_from: d?.[0] ? d[0].format("YYYY-MM-DD") : "", date_to: d?.[1] ? d[1].format("YYYY-MM-DD") : "", page: 1 }))}
          />
          <Select allowClear placeholder="渠道" style={{ width: 130 }}
            options={Object.entries(CHANNEL_TYPE).map(([k, v]) => ({ value: k, label: v.label }))}
            onChange={(v) => setParams((p: any) => ({ ...p, channel_type: v || "", page: 1 }))} />
          <Select style={{ width: 130 }} value={params.pending_human ? "pending" : params.status || "all"}
            options={[
              { value: "all", label: "全部状态" },
              // 「待人工」沿用 pending_human 语义（后端等价于 status=escalated 队列）。
              { value: "pending", label: SESSION_STATUS.escalated.label },
              ...Object.entries(SESSION_STATUS)
                .filter(([k]) => k !== "escalated")
                .map(([k, v]) => ({ value: k, label: v.label })),
            ]}
            onChange={(v) => setParams((p: any) => ({ ...p, pending_human: v === "pending", status: v === "all" || v === "pending" ? "" : v, page: 1 }))} />
          <Select allowClear placeholder="回答反馈" style={{ width: 120 }}
            options={[{ value: "up", label: "👍 好评" }, { value: "down", label: "👎 差评" }]}
            onChange={(v) => setParams((p: any) => ({ ...p, feedback: v || "", page: 1 }))} />
          <Select allowClear placeholder="降级" style={{ width: 110 }}
            options={[{ value: "1", label: "仅降级" }]}
            onChange={(v) => setParams((p: any) => ({ ...p, degraded: !!v, page: 1 }))} />
          <Button icon={<DownloadOutlined />} loading={exporting} onClick={doExport}>导出 CSV</Button>
        </Space>
        <Table
          rowKey="id" loading={loading} columns={columns as any} dataSource={data.items} size="small"
          scroll={{ x: "max-content" }}
          pagination={{ current: params.page, pageSize: params.page_size, total: data.total, showTotal: (t) => `共 ${t} 条`,
            onChange: (page, page_size) => setParams((p: any) => ({ ...p, page, page_size })) }}
        />
      </Card>
      <ConversationDetail sid={detailSid} onClose={() => setDetailSid("")} editable={editable} onChanged={load} />
    </div>
  );
}

function ConversationDetail({ sid, onClose, editable, onChanged }: any) {
  const { message } = AntApp.useApp();
  const { token } = theme.useToken();
  const [kModal, setKModal] = useState<any>(null);
  const [form] = Form.useForm();
  const [live, setLive] = useState<any>(null);
  const [replyText, setReplyText] = useState("");
  const [sending, setSending] = useState(false);

  const status = live?.session?.status;
  const inTakeover = status === "human_takeover";
  const ended = status === "closed" || status === "human_handled";

  // 换会话时重置本地状态；首拉由下方 usePolling 立即执行一次完成。
  useEffect(() => { setLive(null); setReplyText(""); }, [sid]);

  // 换会话后旧请求的在途响应不再回写（sid 过期保护）。
  const sidRef = useRef(sid);
  sidRef.current = sid;
  // 返回请求 Promise：sendReply 等调用点 await 后时间线已真正刷新；轮询失败保持静默。
  const refresh = () => {
    const cur = sid;
    if (!cur) return Promise.resolve();
    return conversationApi.detail(cur).then((d) => { if (sidRef.current === cur) setLive(d); }).catch(() => {});
  };

  // Poll while the drawer is open so the operator sees the customer's replies live.
  // 已结束会话（closed/human_handled）不再轮询；页面切后台自动暂停。
  usePolling(refresh, 3000, { enabled: !!sid && !ended, deps: [sid] });

  if (!sid || !live) return null;
  const s = live.session;

  const doTakeover = async () => {
    try { await conversationApi.takeover(sid); message.success("已接管，AI 已暂停，可直接回复客户"); refresh(); }
    catch (e) { message.error(apiError(e)); }
  };
  const doRelease = async (resumeAi: boolean) => {
    try {
      await conversationApi.release(sid, resumeAi);
      message.success(resumeAi ? "已结束接管，AI 恢复" : "已结束并标记完成");
      refresh(); onChanged();
    } catch (e) { message.error(apiError(e)); }
  };
  const sendReply = async () => {
    const c = replyText.trim();
    if (!c) return;
    setSending(true);
    try { await conversationApi.reply(sid, c); setReplyText(""); await refresh(); }
    catch (e) { message.error(apiError(e)); }
    finally { setSending(false); }
  };

  return (
    <Drawer title="对话详情" width={720} open={!!sid} onClose={onClose}
      extra={editable && (
        <Space>
          {!inTakeover && <Button type="primary" onClick={doTakeover}>接管对话</Button>}
          {inTakeover && <Button onClick={() => doRelease(true)}>结束接管 (AI 恢复)</Button>}
          {inTakeover && (
            <Popconfirm title="结束接管并关闭该会话？" description="客户将收到「本次人工服务已结束」，无法继续此对话。" okText="结束" okButtonProps={{ danger: true }}
              onConfirm={() => doRelease(false)}>
              <Button danger>结束并完成</Button>
            </Popconfirm>
          )}
          {s.escalated && !inTakeover && (
            <Button onClick={async () => { await conversationApi.markHandled(s.id); message.success("已标记完成"); onChanged(); onClose(); }}>标记已处理</Button>
          )}
        </Space>
      )}
    >
      <Descriptions size="small" column={2} bordered style={{ marginBottom: 16 }}>
        <Descriptions.Item label="渠道">{statusMeta(CHANNEL_TYPE, s.channel_type).label}</Descriptions.Item>
        <Descriptions.Item label="用户">{s.end_user_display || s.end_user_id}</Descriptions.Item>
        <Descriptions.Item label="状态">
          {sessionTag(s.status, s.escalated)}
        </Descriptions.Item>
        <Descriptions.Item label="创建时间">{fmtTime(s.created_at)}</Descriptions.Item>
        {s.satisfaction_rating ? (
          <Descriptions.Item label="满意度评分" span={2}>
            {"★".repeat(s.satisfaction_rating)}{"☆".repeat(5 - s.satisfaction_rating)} {s.satisfaction_note && `· ${s.satisfaction_note}`}
          </Descriptions.Item>
        ) : null}
        {s.summary && <Descriptions.Item label="摘要" span={2}>{s.summary}</Descriptions.Item>}
      </Descriptions>

      <Timeline
        items={live.messages.map((m: any) => {
          const isHuman = m.model === "human";
          return {
            dot: m.role === "user" ? <UserOutlined /> : <RobotOutlined style={{ color: isHuman ? "#10b981" : "#0F766E" }} />,
            children: (
              <div>
                <div style={{ fontWeight: 600, marginBottom: 4 }}>
                  {m.role === "user" ? "用户" : isHuman ? "人工客服" : "客服"}
                  {isHuman && <Tag color="green" style={{ marginLeft: 8 }}>人工</Tag>}
                  {m.degraded && <Tag color="orange" style={{ marginLeft: 8 }}>降级</Tag>}
                  {!isHuman && m.model && <Tag style={{ marginLeft: 8 }}>{m.model}</Tag>}
                  {m.latency_ms > 0 && <span style={{ color: token.colorTextSecondary, fontSize: 12, marginLeft: 8 }}>{m.latency_ms}ms · {m.prompt_tokens + m.completion_tokens} tokens · ${m.cost_usd?.toFixed(5)}</span>}
                </div>
                <div style={{ whiteSpace: "pre-wrap", marginBottom: 6 }}>{m.content}</div>
                {m.attachments?.length > 0 && (
                  <div style={{ display: "flex", flexWrap: "wrap", gap: 8, marginBottom: 6 }}>
                    {m.attachments.map((a: any, i: number) =>
                      a.kind === "image" || (a.content_type || "").startsWith("image/") ? (
                        <a key={i} href={a.url} target="_blank" rel="noreferrer">
                          <img src={a.url} alt={a.name} style={{ width: 96, height: 96, objectFit: "cover", borderRadius: 6, border: `1px solid ${token.colorBorderSecondary}` }} />
                        </a>
                      ) : (
                        <a key={i} href={a.url} target="_blank" rel="noreferrer" style={{ fontSize: 13 }}>
                          <PaperClipOutlined /> {a.name || "文件"}
                        </a>
                      )
                    )}
                  </div>
                )}
                {m.tool_calls?.length > 0 && (
                  <Collapse size="small" ghost items={[{
                    key: "t", label: <span><ToolOutlined /> 工具调用 ({m.tool_calls.length})</span>,
                    children: m.tool_calls.map((tc: any, i: number) => (
                      <div key={i} style={{ fontSize: 12, marginBottom: 8, background: token.colorFillTertiary, padding: 8, borderRadius: 6 }}>
                        <b>{tc.name}</b> <Tag color={tc.status === "ok" ? "green" : "red"}>{tc.status}</Tag> {tc.duration_ms}ms
                        <div style={{ color: token.colorTextSecondary }}>参数：{JSON.stringify(tc.arguments)}</div>
                        <div style={{ color: token.colorTextSecondary }}>结果：{String(tc.result).slice(0, 200)}</div>
                      </div>
                    )),
                  }]} />
                )}
                {m.citations?.length > 0 && (
                  <div style={{ marginTop: 4 }}>
                    {m.citations.map((c: any) => <Tooltip key={c.ref} title={c.snippet}><Tag icon={<BookOutlined />} color="blue">[{c.ref}] {c.title}</Tag></Tooltip>)}
                  </div>
                )}
                {m.trace_id && <div style={{ color: token.colorTextTertiary, fontSize: 11, marginTop: 4 }}>trace: {m.trace_id}</div>}
                {editable && m.role === "assistant" && !isHuman && m.content && (
                  <Button type="link" size="small" style={{ padding: 0, fontSize: 12 }} onClick={() => { form.setFieldsValue({ content: m.content, title: "" }); setKModal(m); }}>加入知识库</Button>
                )}
              </div>
            ),
          };
        })}
      />

      {/* Live reply box — only while actively taken over. */}
      {editable && inTakeover && (
        <div style={{ position: "sticky", bottom: 0, background: token.colorBgElevated, paddingTop: 12, borderTop: `1px solid ${token.colorBorderSecondary}` }}>
          <Space.Compact style={{ width: "100%" }}>
            <Input.TextArea
              value={replyText}
              onChange={(e) => setReplyText(e.target.value)}
              autoSize={{ minRows: 1, maxRows: 4 }}
              placeholder="输入回复，回车直接发给客户（Shift+Enter 换行）…"
              onPressEnter={(e) => { if (isImeComposing(e)) return; if (!e.shiftKey) { e.preventDefault(); sendReply(); } }}
            />
            <Button type="primary" loading={sending} onClick={sendReply}>发送</Button>
          </Space.Compact>
        </div>
      )}

      <Modal title="加入知识库" open={!!kModal} onCancel={() => setKModal(null)} onOk={async () => {
        const v = await form.validateFields();
        try { await conversationApi.toKnowledge(s.id, v); message.success("已加入知识库（向量后台生成）"); setKModal(null); }
        catch (e) { message.error(apiError(e)); }
      }}>
        <Form form={form} layout="vertical">
          <Form.Item name="title" label="标题" rules={[{ required: true }]}><Input /></Form.Item>
          <Form.Item name="content" label="内容" rules={[{ required: true }]}><Input.TextArea rows={6} /></Form.Item>
          <Form.Item name="category" label="分类"><Input /></Form.Item>
        </Form>
      </Modal>
    </Drawer>
  );
}
