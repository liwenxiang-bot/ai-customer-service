import { useEffect, useState } from "react";
import { Card, Table, Tag, Select, Space, Button, Popconfirm, Badge, App as AntApp, Typography } from "antd";
import { handoffApi } from "../api";
import { apiError } from "../api/client";
import { useAuth, canEdit } from "../auth";

const REASON_LABELS: any = {
  user_request: "用户要求", model_decision: "AI 判断", negative_feedback: "连续负反馈", error_fallback: "异常兜底",
};
const STATUS_COLORS: any = { open: "red", in_progress: "orange", resolved: "green" };

export function Handoff() {
  const { user } = useAuth();
  const editable = canEdit(user?.role);
  const { message } = AntApp.useApp();
  const [data, setData] = useState<any>({ items: [], total: 0, open_count: 0 });
  const [loading, setLoading] = useState(false);
  const [params, setParams] = useState<any>({ status: "", page: 1, page_size: 10 });

  const load = () => {
    setLoading(true);
    handoffApi.tickets(params).then(setData).finally(() => setLoading(false));
  };
  useEffect(load, [JSON.stringify(params)]);

  const columns = [
    { title: "用户", dataIndex: "end_user_id", width: 160, ellipsis: true },
    { title: "渠道", dataIndex: "channel_type", width: 110, render: (c: string) => <Tag>{c}</Tag> },
    { title: "原因", dataIndex: "reason", width: 120, render: (r: string) => REASON_LABELS[r] || r },
    { title: "摘要", dataIndex: "conversation_summary", ellipsis: true },
    { title: "通知", dataIndex: "notified", width: 90, render: (n: boolean, r: any) => n ? <Tag color="green">已通知</Tag> : <Tag color="default" title={r.notify_error}>未送达</Tag> },
    { title: "状态", dataIndex: "status", width: 90, render: (s: string) => <Tag color={STATUS_COLORS[s]}>{s}</Tag> },
    { title: "时间", dataIndex: "created_at", width: 160, render: (t: string) => t?.replace("T", " ").slice(0, 19) },
    {
      title: "操作", width: 150, render: (_: any, r: any) => (
        <Space size="small">
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
            <Popconfirm title="标记该工单为已解决？" onConfirm={async () => { await handoffApi.resolve(r.id, ""); message.success("已解决"); load(); }}>
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
      <div className="acs-page-sub">AI 转人工产生的工单与通知送达记录；需要实时与客户对话请到「坐席工作台」。</div>
      <Card>
        <Space style={{ marginBottom: 12 }}>
          <Select allowClear placeholder="状态" style={{ width: 140 }}
            options={[{ value: "open", label: "待处理" }, { value: "resolved", label: "已解决" }]}
            onChange={(v) => setParams((p: any) => ({ ...p, status: v || "", page: 1 }))} />
        </Space>
        <Table rowKey="id" loading={loading} columns={columns as any} dataSource={data.items} size="small"
          pagination={{ current: params.page, pageSize: params.page_size, total: data.total, showTotal: (t) => `共 ${t} 条`,
            onChange: (page, page_size) => setParams((p: any) => ({ ...p, page, page_size })) }}
        />
      </Card>
    </div>
  );
}
