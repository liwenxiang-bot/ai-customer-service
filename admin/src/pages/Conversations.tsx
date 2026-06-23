import { useEffect, useState } from "react";
import {
  Card, Table, Tag, Space, Select, Input, Drawer, Timeline, Descriptions, Button,
  Collapse, Empty, Tooltip, App as AntApp, Modal, Form,
} from "antd";
import { RobotOutlined, UserOutlined, ToolOutlined, BookOutlined } from "@ant-design/icons";
import { conversationApi } from "../api";
import { apiError } from "../api/client";
import { useAuth, canEdit } from "../auth";

export function Conversations() {
  const { user } = useAuth();
  const editable = canEdit(user?.role);
  const [data, setData] = useState<any>({ items: [], total: 0 });
  const [loading, setLoading] = useState(false);
  const [params, setParams] = useState<any>({ page: 1, page_size: 10, channel_type: "", pending_human: false, q: "" });
  const [detail, setDetail] = useState<any>(null);

  const load = () => {
    setLoading(true);
    conversationApi.list(params).then(setData).finally(() => setLoading(false));
  };
  useEffect(load, [JSON.stringify(params)]);

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
      <Card>
        <Space style={{ marginBottom: 16 }} wrap>
          <Input allowClear placeholder="搜索标题" style={{ width: 200 }} onChange={(e) => setParams((p: any) => ({ ...p, q: e.target.value, page: 1 }))} />
          <Select allowClear placeholder="渠道" style={{ width: 140 }}
            options={[{ value: "web", label: "Web" }, { value: "wechat_work", label: "企业微信" }]}
            onChange={(v) => setParams((p: any) => ({ ...p, channel_type: v || "", page: 1 }))} />
          <Select style={{ width: 140 }} value={params.pending_human ? "pending" : "all"}
            options={[{ value: "all", label: "全部" }, { value: "pending", label: "待人工" }]}
            onChange={(v) => setParams((p: any) => ({ ...p, pending_human: v === "pending", page: 1 }))} />
        </Space>
        <Table
          rowKey="id" loading={loading} columns={columns as any} dataSource={data.items}
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
  const [kModal, setKModal] = useState<any>(null);
  const [form] = Form.useForm();
  if (!detail) return null;
  const s = detail.session;

  return (
    <Drawer title="对话详情" width={720} open={!!detail} onClose={onClose}
      extra={editable && s.escalated && <Button onClick={async () => { await conversationApi.markHandled(s.id); message.success("已标记完成"); onChanged(); onClose(); }}>标记已处理</Button>}
    >
      <Descriptions size="small" column={2} bordered style={{ marginBottom: 16 }}>
        <Descriptions.Item label="渠道">{s.channel_type}</Descriptions.Item>
        <Descriptions.Item label="用户">{s.end_user_display || s.end_user_id}</Descriptions.Item>
        <Descriptions.Item label="状态">{s.escalated ? <Tag color="red">待人工</Tag> : s.status}</Descriptions.Item>
        <Descriptions.Item label="创建时间">{s.created_at?.replace("T", " ").slice(0, 19)}</Descriptions.Item>
        {s.summary && <Descriptions.Item label="摘要" span={2}>{s.summary}</Descriptions.Item>}
      </Descriptions>

      <Timeline
        items={detail.messages.map((m: any) => ({
          dot: m.role === "user" ? <UserOutlined /> : <RobotOutlined style={{ color: "#4f46e5" }} />,
          children: (
            <div>
              <div style={{ fontWeight: 600, marginBottom: 4 }}>
                {m.role === "user" ? "用户" : "客服"}
                {m.degraded && <Tag color="orange" style={{ marginLeft: 8 }}>降级</Tag>}
                {m.model && <Tag style={{ marginLeft: 8 }}>{m.model}</Tag>}
                {m.latency_ms > 0 && <span style={{ color: "#999", fontSize: 12, marginLeft: 8 }}>{m.latency_ms}ms · {m.prompt_tokens + m.completion_tokens} tokens · ${m.cost_usd?.toFixed(5)}</span>}
              </div>
              <div style={{ whiteSpace: "pre-wrap", marginBottom: 6 }}>{m.content}</div>
              {m.tool_calls?.length > 0 && (
                <Collapse size="small" ghost items={[{
                  key: "t", label: <span><ToolOutlined /> 工具调用 ({m.tool_calls.length})</span>,
                  children: m.tool_calls.map((tc: any, i: number) => (
                    <div key={i} style={{ fontSize: 12, marginBottom: 8, background: "#fafafa", padding: 8, borderRadius: 6 }}>
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
              {editable && m.role === "assistant" && m.content && (
                <a style={{ fontSize: 12 }} onClick={() => { form.setFieldsValue({ content: m.content, title: "" }); setKModal(m); }}>加入知识库</a>
              )}
            </div>
          ),
        }))}
      />

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
