import { useEffect, useRef, useState } from "react";
import { Button, Card, Table, Tag, Select, Space, Popconfirm, Badge, Typography, App as AntApp } from "antd";
import { useNavigate } from "react-router-dom";
import { handoffApi } from "../api";
import { apiError } from "../api/client";
import { useAuth, canEdit } from "../auth";
import { CHANNEL_TYPE, HANDOFF_REASON, HANDOFF_STATUS, statusMeta } from "../utils/status";

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

  // 递增序号防过期响应：快速切换筛选/翻页时，只有最新一次请求的结果才落地。
  const loadSeq = useRef(0);
  const load = () => {
    const seq = ++loadSeq.current;
    setLoading(true);
    handoffApi.tickets(params)
      .then((d) => { if (seq === loadSeq.current) setData(d); })
      .catch((e) => { if (seq === loadSeq.current) message.error(apiError(e)); })
      .finally(() => { if (seq === loadSeq.current) setLoading(false); });
  };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { load(); }, [params.status, params.priority, params.mine, params.page, params.page_size]);

  const act = async (fn: () => Promise<any>, ok: string) => {
    try { await fn(); message.success(ok); load(); }
    catch (e) { message.error(apiError(e)); }
  };

  // 认领冲突（409 = 已被他人认领）用 warning 提示，成功/失败都刷新拿最新受理人
  const claim = async (id: string) => {
    try { await handoffApi.claim(id); message.success("已认领"); }
    catch (e: any) {
      if (e?.response?.status === 409) message.warning(apiError(e));
      else message.error(apiError(e));
    }
    load();
  };
  const isAssigned = (r: any) => !!(r.assignee_id || r.assignee_name || r.assignee_email);
  const isMine = (r: any) =>
    !!user && (r.assignee_id === user.id || (!r.assignee_id && !!r.assignee_email && r.assignee_email === user.email));

  const columns = [
    {
      title: "优先级", dataIndex: "priority", width: 96,
      render: (p: string, r: any) => editable ? (
        <Select size="small" variant="borderless" value={p || "normal"} options={PRIO_OPTS} style={{ width: 84 }}
          onChange={(v) => act(() => handoffApi.update(r.id, { priority: v }), "已更新优先级")} />
      ) : <Tag color={PRIO[p]?.c}>{PRIO[p]?.t || p}</Tag>,
    },
    { title: "用户", dataIndex: "end_user_id", width: 140, ellipsis: true },
    { title: "渠道", dataIndex: "channel_type", width: 100, render: (c: string) => { const m = statusMeta(CHANNEL_TYPE, c); return <Tag color={m.color}>{m.label}</Tag>; } },
    { title: "原因", dataIndex: "reason", width: 110, render: (r: string) => statusMeta(HANDOFF_REASON, r).label },
    { title: "摘要", dataIndex: "conversation_summary", ellipsis: true },
    { title: "受理人", dataIndex: "assignee_name", width: 150, ellipsis: true, render: (n: string, r: any) => n || r.assignee_email || <Typography.Text type="secondary">未分配</Typography.Text> },
    { title: "通知", dataIndex: "notified", width: 80, render: (n: boolean, r: any) => n ? <Tag color="green">已通知</Tag> : <Tag color="default" title={r.notify_error}>未送达</Tag> },
    { title: "状态", dataIndex: "status", width: 84, render: (s: string) => { const m = statusMeta(HANDOFF_STATUS, s); return <Tag color={m.color}>{m.label}</Tag>; } },
    {
      title: "操作", width: 240, render: (_: any, r: any) => (
        <Space size="small" wrap>
          <Button type="link" size="small" style={{ padding: 0 }} onClick={() => nav(`/workbench?session=${r.session_id}`)}>去接管</Button>
          <Button type="link" size="small" style={{ padding: 0 }} onClick={() => nav(`/conversations?session=${r.session_id}`)}>查看对话</Button>
          {editable && r.status === "open" && !isAssigned(r) && (
            <Button type="link" size="small" style={{ padding: 0 }} onClick={() => claim(r.id)}>认领</Button>
          )}
          {editable && r.status !== "resolved" && isMine(r) && (
            <Button type="link" size="small" style={{ padding: 0 }} onClick={() => act(() => handoffApi.unclaim(r.id), "已退回")}>退回</Button>
          )}
          {editable && !r.notified && (
            <Button type="link" size="small" style={{ padding: 0 }} onClick={async () => {
              try {
                const res = await handoffApi.resend(r.id);
                res.notified ? message.success("已重发通知") : message.warning("仍未送达：" + (res.error || "未配置通知渠道"));
              } catch (e) { message.error(apiError(e)); }
              load();
            }}>重试通知</Button>
          )}
          {editable && r.status !== "resolved" && (
            <Popconfirm title="标记该工单为已解决？" onConfirm={() => act(() => handoffApi.resolve(r.id, ""), "已解决")}>
              <Button type="link" size="small" style={{ padding: 0 }}>标记解决</Button>
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
            options={Object.entries(HANDOFF_STATUS).map(([value, m]) => ({ value, label: m.label }))}
            onChange={(v) => setParams((p: any) => ({ ...p, status: v || "", page: 1 }))} />
          <Select allowClear placeholder="优先级" style={{ width: 120 }} options={PRIO_OPTS}
            onChange={(v) => setParams((p: any) => ({ ...p, priority: v || "", page: 1 }))} />
          <Select value={params.mine ? "mine" : "all"} style={{ width: 130 }}
            options={[{ value: "all", label: "全部工单" }, { value: "mine", label: "我的工单" }]}
            onChange={(v) => setParams((p: any) => ({ ...p, mine: v === "mine", page: 1 }))} />
        </Space>
        <Table rowKey="id" loading={loading} columns={columns as any} dataSource={data.items} size="small"
          scroll={{ x: "max-content" }}
          pagination={{
            current: params.page, pageSize: params.page_size, total: data.total, showTotal: (t) => `共 ${t} 条`,
            onChange: (page, page_size) => setParams((p: any) => ({ ...p, page, page_size })),
          }}
        />
      </Card>
    </div>
  );
}
