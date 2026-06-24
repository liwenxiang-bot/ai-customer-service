import { useEffect, useState } from "react";
import { Card, Col, Row, Statistic, Spin, Empty } from "antd";
import {
  MessageOutlined,
  UserOutlined,
  ThunderboltOutlined,
  CustomerServiceOutlined,
  LikeOutlined,
  DollarOutlined,
} from "@ant-design/icons";
import { LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid } from "recharts";
import { dashboardApi } from "../api";

export function Dashboard() {
  const [data, setData] = useState<any>(null);
  const [trend, setTrend] = useState<any[]>([]);

  useEffect(() => {
    dashboardApi.overview().then(setData);
    dashboardApi.trend(14).then((d) => setTrend(d.trend));
  }, []);

  if (!data) return <Spin />;
  const t = data.today;

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
      <Row gutter={[16, 16]}>
        {cards.map((c) => (
          <Col xs={12} sm={8} lg={4} key={c.title}>
            <Card>
              <Statistic
                title={<span>{c.icon} {c.title}</span>}
                value={c.value}
                valueStyle={{ color: c.color, fontSize: 24 }}
              />
            </Card>
          </Col>
        ))}
      </Row>

      <Card title="近 14 天趋势" style={{ marginTop: 16 }}>
        {trend.length === 0 ? (
          <Empty description="暂无数据" />
        ) : (
          <ResponsiveContainer width="100%" height={300}>
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
    </div>
  );
}
