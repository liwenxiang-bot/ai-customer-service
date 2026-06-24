import { useEffect, useState } from "react";
import { Card, Col, Row, Statistic, Spin, Empty, Button, Progress, List, Tag, Space } from "antd";
import {
  MessageOutlined,
  UserOutlined,
  ThunderboltOutlined,
  CustomerServiceOutlined,
  LikeOutlined,
  DollarOutlined,
  BookOutlined,
  ImportOutlined,
  RobotOutlined,
  RightOutlined,
} from "@ant-design/icons";
import { LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid } from "recharts";
import { useNavigate } from "react-router-dom";
import { dashboardApi, conversationApi } from "../api";

export function Dashboard() {
  const nav = useNavigate();
  const [data, setData] = useState<any>(null);
  const [trend, setTrend] = useState<any[]>([]);
  const [recent, setRecent] = useState<any[]>([]);

  useEffect(() => {
    dashboardApi.overview().then(setData);
    dashboardApi.trend(14).then((d) => setTrend(d.trend));
    conversationApi.list({ page_size: 6 }).then((d) => setRecent(d.items));
  }, []);

  if (!data) return <Spin />;
  const t = data.today;
  const backlog = data.backlog || {};
  const kb = data.knowledge || {};
  const vecPct = kb.chunks_total ? Math.round((kb.chunks_ready / kb.chunks_total) * 100) : 100;

  const cards = [
    { title: "今日对话", value: t.conversations, icon: <MessageOutlined />, color: "#0F766E" },
    { title: "今日用户", value: t.users, icon: <UserOutlined />, color: "#0ea5e9" },
    { title: "平均响应(ms)", value: t.avg_latency_ms, icon: <ThunderboltOutlined />, color: "#f59e0b" },
    { title: "转人工率", value: (t.escalation_rate * 100).toFixed(1) + "%", icon: <CustomerServiceOutlined />, color: "#ef4444" },
    { title: "满意度", value: data.satisfaction == null ? "—" : (data.satisfaction * 100).toFixed(0) + "%", icon: <LikeOutlined />, color: "#10b981" },
    { title: "今日成本($)", value: t.cost_usd, icon: <DollarOutlined />, color: "#8b5cf6" },
  ];

  return (
    <div>
      <div className="acs-page-title">概览</div>
      <div className="acs-page-sub">今日核心指标、待处理事项与近 14 天趋势一览。</div>

      <Row gutter={[12, 12]}>
        {cards.map((c) => (
          <Col xs={12} sm={8} lg={4} key={c.title}>
            <Card size="small">
              <Statistic title={<span>{c.icon} {c.title}</span>} value={c.value} valueStyle={{ color: c.color, fontSize: 22 }} />
            </Card>
          </Col>
        ))}
      </Row>

      <Row gutter={[12, 12]} style={{ marginTop: 12 }}>
        {/* backlog → workbench */}
        <Col xs={24} lg={8}>
          <Card size="small" title="待处理" extra={<Button type="link" size="small" onClick={() => nav("/workbench")}>去工作台 <RightOutlined /></Button>}>
            <Space size="large">
              <Statistic title="待人工" value={backlog.pending_human ?? 0} valueStyle={{ color: backlog.pending_human ? "#DC2626" : undefined, fontSize: 26 }} />
              <Statistic title="接管中" value={backlog.in_takeover ?? 0} valueStyle={{ color: backlog.in_takeover ? "#16A34A" : undefined, fontSize: 26 }} />
            </Space>
          </Card>
        </Col>

        {/* knowledge health */}
        <Col xs={24} lg={8}>
          <Card size="small" title="知识库" extra={<Button type="link" size="small" onClick={() => nav("/knowledge")}>管理 <RightOutlined /></Button>}>
            <Space size="large" align="start" style={{ width: "100%", justifyContent: "space-between" }}>
              <Statistic title="知识条目" value={kb.items ?? 0} valueStyle={{ fontSize: 26 }} />
              <div style={{ flex: 1 }}>
                <div style={{ fontSize: 13, color: "#5b6573", marginBottom: 6 }}>已向量化 {kb.chunks_ready ?? 0}/{kb.chunks_total ?? 0} 分块</div>
                <Progress percent={vecPct} size="small" status={vecPct < 100 ? "active" : "success"} />
              </div>
            </Space>
          </Card>
        </Col>

        {/* quick actions */}
        <Col xs={24} lg={8}>
          <Card size="small" title="快捷入口">
            <Space wrap>
              <Button icon={<CustomerServiceOutlined />} onClick={() => nav("/workbench")}>坐席工作台</Button>
              <Button icon={<BookOutlined />} onClick={() => nav("/knowledge")}>新增知识</Button>
              <Button icon={<ImportOutlined />} onClick={() => nav("/knowledge")}>批量导入</Button>
              <Button icon={<RobotOutlined />} onClick={() => nav("/ai-config")}>AI 配置</Button>
            </Space>
          </Card>
        </Col>
      </Row>

      <Row gutter={[12, 12]} style={{ marginTop: 12 }}>
        <Col xs={24} lg={15}>
          <Card size="small" title="近 14 天趋势">
            {trend.length === 0 ? (
              <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无数据" />
            ) : (
              <ResponsiveContainer width="100%" height={280}>
                <LineChart data={trend}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#eee" />
                  <XAxis dataKey="day" fontSize={12} />
                  <YAxis fontSize={12} />
                  <Tooltip />
                  <Line type="monotone" dataKey="conversations" name="对话数" stroke="#0F766E" strokeWidth={2} />
                  <Line type="monotone" dataKey="messages" name="消息数" stroke="#0ea5e9" strokeWidth={2} />
                  <Line type="monotone" dataKey="escalations" name="转人工" stroke="#ef4444" strokeWidth={2} />
                </LineChart>
              </ResponsiveContainer>
            )}
          </Card>
        </Col>

        {/* recent conversations */}
        <Col xs={24} lg={9}>
          <Card size="small" title="最近会话" extra={<Button type="link" size="small" onClick={() => nav("/conversations")}>全部 <RightOutlined /></Button>}>
            <List
              size="small"
              dataSource={recent}
              locale={{ emptyText: "暂无会话" }}
              renderItem={(it: any) => (
                <List.Item
                  style={{ cursor: "pointer" }}
                  onClick={() => nav(`/conversations?session=${it.id}`)}
                >
                  <List.Item.Meta
                    title={<span style={{ fontSize: 13 }}>{it.title || it.end_user_display || it.end_user_id || "匿名"}</span>}
                    description={<span style={{ fontSize: 12 }}>{(it.last_activity_at || "").replace("T", " ").slice(5, 16)} · {it.channel_type}</span>}
                  />
                  {it.escalated ? <Tag color="red">待人工</Tag> : it.status === "human_takeover" ? <Tag color="green">接管中</Tag> : null}
                </List.Item>
              )}
            />
          </Card>
        </Col>
      </Row>
    </div>
  );
}
