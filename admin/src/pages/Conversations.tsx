import { useEffect, useState } from "react";
import {
  Card, Table, Tag, Space, Select, Input, Drawer, Timeline, Descriptions, Button,
  Collapse, Empty, Tooltip, App as AntApp, Modal, Form, DatePicker, theme,
} from "antd";
import { RobotOutlined, UserOutlined, ToolOutlined, BookOutlined, DownloadOutlined, PaperClipOutlined } from "@ant-design/icons";
import { conversationApi } from "../api";
import { apiError } from "../api/client";
import { useAuth, canEdit } from "../auth";
import { useDebounce } from "../hooks/useDebounce";

export function Conversations() {
  const { user } = useAuth();
  const { message } = AntApp.useApp();
  const editable = canEdit(user?.role);
  const [data, setData] = useState<any>({ items: [], total: 0 });
  const [loading, setLoading] = useState(false);
  const [params, setParams] = useState<any>({ page: 1, page_size: 10, channel_type: "", pending_human: false, q: "", date_from: "", date_to: "", feedback: "", degraded: false });
  const [detail, setDetail] = useState<any>(null);
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

  const load = () => {
    setLoading(true);
    conversationApi.list(params).then(setData).finally(() => setLoading(false));
  };
  useEffect(load, [JSON.stringify(params)]);

  // Deep-link from a handoff notification (?session=ID) → auto-open that conversation.
  useEffect(() => {
    const sid = new URLSearchParams(window.location.search).get("session");
    if (sid) conversationApi.detail(sid).then(setDetail).catch(() => {});
  }, []);

  const columns = [
    { title: "标题", dataIndex: "title", render: (t: string) => t || "—" },
    { title: "渠道", dataIndex: "channel_type", width: 110, render: (c: string) => <Tag>{c}</Tag> },
    { title: "用户", dataIndex: "end_user_id", width: 160, ellipsis: true, render: (u: string, r: any) => r.end_user_display || u },
    { title: "轮数", dataIndex: "message_count", width: 70 },
    { title: "状态", dataIndex: "status", width: 110, render: (s: string, r: any) => r.escalated ? <Tag color="red">待人工</Tag> : <Tag color="blue">{s}</Tag> },
    { title: "最近活动", dataIndex: "last_activity_at", width: 170, render: (t: string) => t?.replace("T", " ").slice(0, 19) },
    { title: "", width: 70, render: (_: any, r: any) => <a onClick={() => conversationApi.detail(r.id).then(setDetail)}>详情</a> },
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
            options={[{ value: "web", label: "Web" }, { value: "wechat_work", label: "企业微信" }]}
            onChange={(v) => setParams((p: any) => ({ ...p, channel_type: v || "", page: 1 }))} />
          <Select style={{ width: 120 }} value={params.pending_human ? "pending" : "all"}
            options={[{ value: "all", label: "全部状态" }, { value: "pending", label: "待人工" }]}
            onChange={(v) => setParams((p: any) => ({ ...p, pending_human: v === "pending", page: 1 }))} />
          <Select allowClear placeholder="满意度" style={{ width: 120 }}
            options={[{ value: "up", label: "👍 好评" }, { value: "down", label: "👎 差评" }]}
            onChange={(v) => setParams((p: any) => ({ ...p, feedback: v || "", page: 1 }))} />
          <Select allowClear placeholder="降级" style={{ width: 110 }}
            options={[{ value: "1", label: "仅降级" }]}
            onChange={(v) => setParams((p: any) => ({ ...p, degraded: !!v, page: 1 }))} />
          <Button icon={<DownloadOutlined />} loading={exporting} onClick={doExport}>导出 CSV</Button>
        </Space>
        <Table
          rowKey="id" loading={loading} columns={columns as any} dataSource={data.items} size="small"
          pagination={{ current: params.page, pageSize: params.page_size, total: data.total, showTotal: (t) => `共 ${t} 条`,
            onChange: (page, page_size) => setParams((p: any) => ({ ...p, page, page_size })) }}
        />
      </Card>
      <ConversationDetail detail={detail} onClose={() => setDetail(null)} editable={editable} onChanged={load} />
    </div>
  );
}

function ConversationDetail({ detail, onClose, editable, onChanged }: any) {
  const { message } = AntApp.useApp();
  const { token } = theme.useToken();
  const [kModal, setKModal] = useState<any>(null);
  const [form] = Form.useForm();
  const [live, setLive] = useState<any>(detail);
  const [replyText, setReplyText] = useState("");
  const [sending, setSending] = useState(false);

  const sid = detail?.session?.id;
  const status = live?.session?.status;
  const inTakeover = status === "human_takeover";

  // Reset local state when a different conversation is opened.
  useEffect(() => { setLive(detail); setReplyText(""); }, [sid]);

  // Poll while the drawer is open so the operator sees the customer's replies live.
  useEffect(() => {
    if (!sid) return;
    const t = setInterval(() => { conversationApi.detail(sid).then(setLive).catch(() => {}); }, 3000);
    return () => clearInterval(t);
  }, [sid]);

  if (!detail || !live) return null;
  const s = live.session;
  const refresh = () => conversationApi.detail(sid).then(setLive).catch(() => {});

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
    <Drawer title="对话详情" width={720} open={!!detail} onClose={onClose}
      extra={editable && (
        <Space>
          {!inTakeover && <Button type="primary" onClick={doTakeover}>接管对话</Button>}
          {inTakeover && <Button onClick={() => doRelease(true)}>结束接管 (AI 恢复)</Button>}
          {inTakeover && <Button danger onClick={() => doRelease(false)}>结束并完成</Button>}
          {s.escalated && !inTakeover && (
            <Button onClick={async () => { await conversationApi.markHandled(s.id); message.success("已标记完成"); onChanged(); onClose(); }}>标记已处理</Button>
          )}
        </Space>
      )}
    >
      <Descriptions size="small" column={2} bordered style={{ marginBottom: 16 }}>
        <Descriptions.Item label="渠道">{s.channel_type}</Descriptions.Item>
        <Descriptions.Item label="用户">{s.end_user_display || s.end_user_id}</Descriptions.Item>
        <Descriptions.Item label="状态">
          {inTakeover ? <Tag color="green">人工接管中</Tag> : s.escalated ? <Tag color="red">待人工</Tag> : s.status}
        </Descriptions.Item>
        <Descriptions.Item label="创建时间">{s.created_at?.replace("T", " ").slice(0, 19)}</Descriptions.Item>
        {s.satisfaction_rating ? (
          <Descriptions.Item label="满意度" span={2}>
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
                  {m.latency_ms > 0 && <span style={{ color: "#999", fontSize: 12, marginLeft: 8 }}>{m.latency_ms}ms · {m.prompt_tokens + m.completion_tokens} tokens · ${m.cost_usd?.toFixed(5)}</span>}
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
                        <div style={{ color: "#666" }}>参数：{JSON.stringify(tc.arguments)}</div>
                        <div style={{ color: "#666" }}>结果：{String(tc.result).slice(0, 200)}</div>
                      </div>
                    )),
                  }]} />
                )}
                {m.citations?.length > 0 && (
                  <div style={{ marginTop: 4 }}>
                    {m.citations.map((c: any) => <Tooltip key={c.ref} title={c.snippet}><Tag icon={<BookOutlined />} color="blue">[{c.ref}] {c.title}</Tag></Tooltip>)}
                  </div>
                )}
                {m.trace_id && <div style={{ color: "#bbb", fontSize: 11, marginTop: 4 }}>trace: {m.trace_id}</div>}
                {editable && m.role === "assistant" && !isHuman && m.content && (
                  <a style={{ fontSize: 12 }} onClick={() => { form.setFieldsValue({ content: m.content, title: "" }); setKModal(m); }}>加入知识库</a>
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
              onPressEnter={(e) => { if (!e.shiftKey) { e.preventDefault(); sendReply(); } }}
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
