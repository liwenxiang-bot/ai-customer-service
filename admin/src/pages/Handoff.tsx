import { useEffect, useState } from "react";
import { Card, Table, Tag, Select, Space, Popconfirm, Badge, App as AntApp } from "antd";
import { useNavigate } from "react-router-dom";
import { handoffApi } from "../api";
import { apiError } from "../api/client";
import { useAuth, canEdit } from "../auth";

const REASON_LABELS: any = {
  user_request: "用户要求", model_decision: "AI 判断", negative_feedback: "连续负反馈", error_fallback: "异常兜底",
};
const STATUS_COLORS: any = { open: "red", in_progress: "orange", resolved: "green" };
const PRIO: any = {
  urgent: { c: "red", t: "紧急" }, high: { c: "orange", t: "高" }, normal: { c: "blue", t: "普通" }, low: { c: "default", t: "低" },
};
const PRIO_OPTS = [
  { value: "urgent", label: "紧急" }, { value: "high", label: "高" }, { value: "normal", label: "普通" }, { value: "low", label: "低" },
];

export function Handoff() {
  const { user } = useAuth();
  const editable = canEdit(user?.role);
  const nav = useNavigate();
  const { message } = AntApp.useApp();
  const [data, setData] = useState<any>({ items: [], total: 0, open_count: 0 });
  const [loading, setLoading] = useState(false);
  const [params, setParams] = useState<any>({ status: "", priority: "", mine: false, page: 1, page_size: 10 });

  const load = () => {
    setLoading(true);
    handoffApi.tickets(params).then(setData).finally(() => setLoading(false));
  };
  useEffect(load, [JSON.stringify(params)]);

  const act = async (fn: () => Promise<any>, ok: string) => {
    try { await fn(); message.success(ok); load(); }
    catch (e) { message.error(apiError(e)); }
  };

  const columns = [
    {
      title: "优先级", dataIndex: "priority", width: 96,
      render: (p: string, r: any) => editable ? (
        <Select size="small" variant="borderless" value={p || "normal"} options={PRIO_OPTS} style={{ width: 84 }}
          onChange={(v) => act(() => handoffApi.update(r.id, { priority: v }), "已更新优先级")} />
      ) : <Tag color={PRIO[p]?.c}>{PRIO[p]?.t || p}</Tag>,
    },
    { title: "用户", dataIndex: "end_user_id", width: 140, ellipsis: true },
    { title: "渠道", dataIndex: "channel_type", width: 100, render: (c: string) => <Tag>{c}</Tag> },
    { title: "原因", dataIndex: "reason", width: 110, render: (r: string) => REASON_LABELS[r] || r },
    { title: "摘要", dataIndex: "conversation_summary", ellipsis: true },
    { title: "受理人", dataIndex: "assignee_email", width: 150, ellipsis: true, render: (e: string) => e || <span style={{ color: "#bbb" }}>未分配</span> },
    { title: "通知", dataIndex: "notified", width: 80, render: (n: boolean, r: any) => n ? <Tag color="green">已通知</Tag> : <Tag color="default" title={r.notify_error}>未送达</Tag> },
    { title: "状态", dataIndex: "status", width: 84, render: (s: string) => <Tag color={STATUS_COLORS[s]}>{s}</Tag> },
    {
      title: "操作", width: 240, render: (_: any, r: any) => (
        <Space size="small" wrap>
          <a onClick={() => nav(`/workbench?session=${r.session_id}`)}>去接管</a>
          <a onClick={() => nav(`/conversations?session=${r.session_id}`)}>查看对话</a>
          {editable && r.assignee_email !== user?.email && (
            <a onClick={() => act(() => handoffApi.update(r.id, { assignee: "me" }), "已认领")}>认领</a>
          )}
          {editable && !r.notified && (
            <a onClick={async () => {
              try {
                const res = await handoffApi.resend(r.id);
                res.notified ? message.success("已重发通知") : message.warning("仍未送达：" + (res.error || "未配置通知渠道"));
              } catch (e) { message.error(apiError(e)); }
              load();
            }}>重试通知</a>
          )}
          {editable && r.status !== "resolved" && (
            <Popconfirm title="标记该工单为已解决？" onConfirm={() => act(() => handoffApi.resolve(r.id, ""), "已解决")}>
              <a>标记解决</a>
            </Popconfirm>
          )}
        </Space>
      ),
    },
  ];

  return (
    <div>
      <div className="acs-page-title">
        转人工工单 <Badge count={data.open_count} style={{ marginLeft: 8 }} />
      </div>
      <div className="acs-page-sub">AI 转人工产生的工单：可认领、设优先级，「去接管」直达坐席工作台与客户实时对话。</div>
      <Card>
        <Space style={{ marginBottom: 12 }} wrap>
          <Select allowClear placeholder="状态" style={{ width: 130 }}
            options={[{ value: "open", label: "待处理" }, { value: "resolved", label: "已解决" }]}
            onChange={(v) => setParams((p: any) => ({ ...p, status: v || "", page: 1 }))} />
          <Select allowClear placeholder="优先级" style={{ width: 120 }} options={PRIO_OPTS}
            onChange={(v) => setParams((p: any) => ({ ...p, priority: v || "", page: 1 }))} />
          <Select value={params.mine ? "mine" : "all"} style={{ width: 130 }}
            options={[{ value: "all", label: "全部工单" }, { value: "mine", label: "我的工单" }]}
            onChange={(v) => setParams((p: any) => ({ ...p, mine: v === "mine", page: 1 }))} />
        </Space>
        <Table rowKey="id" loading={loading} columns={columns as any} dataSource={data.items} size="small"
          pagination={{
            current: params.page, pageSize: params.page_size, total: data.total, showTotal: (t) => `共 ${t} 条`,
            onChange: (page, page_size) => setParams((p: any) => ({ ...p, page, page_size })),
          }}
        />
      </Card>
    </div>
  );
}
