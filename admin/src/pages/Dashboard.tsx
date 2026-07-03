import { useEffect, useRef, useState } from "react";
import { Card, Col, Row, Statistic, Spin, Empty, Button, Progress, List, Tag, Space, theme, Segmented, Result, Alert, Tooltip as AntdTooltip } from "antd";
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
import { LineChart, Line, BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid } from "recharts";
import { useNavigate } from "react-router-dom";
import { dashboardApi, conversationApi } from "../api";
import { apiError } from "../api/client";
import { fmtShort } from "../utils/time";
import { CHANNEL_TYPE, SESSION_STATUS, statusMeta } from "../utils/status";

const DAY_OPTIONS = [
  { label: "近 7 天", value: 7 },
  { label: "近 14 天", value: 14 },
  { label: "近 30 天", value: 30 },
  { label: "近 90 天", value: 90 },
];

export function Dashboard() {
  const nav = useNavigate();
  const { token } = theme.useToken();
  const [days, setDays] = useState(14);
  const [data, setData] = useState<any>(null);
  const [overviewError, setOverviewError] = useState<string | null>(null);
  const [trend, setTrend] = useState<any[]>([]);
  const [trendError, setTrendError] = useState<string | null>(null);
  const [recent, setRecent] = useState<any[]>([]);
  const [recentError, setRecentError] = useState<string | null>(null);
  const [analytics, setAnalytics] = useState<any>(null);
  const [analyticsError, setAnalyticsError] = useState<string | null>(null);

  const loadOverview = () => {
    setOverviewError(null);
    dashboardApi.overview().then(setData).catch((e) => setOverviewError(apiError(e)));
  };
  const loadRecent = () => {
    setRecentError(null);
    conversationApi.list({ page_size: 6 }).then((d) => setRecent(d.items)).catch((e) => setRecentError(apiError(e)));
  };
  // 判序：快速切换时间窗时，旧窗口的慢响应不得覆盖新窗口的数据/错误态（同 Conversations 的 loadSeq 模式）
  const trendSeq = useRef(0);
  const loadTrend = (d: number) => {
    const seq = ++trendSeq.current;
    setTrendError(null);
    dashboardApi.trend(d)
      .then((r) => { if (seq === trendSeq.current) setTrend(r.trend); })
      .catch((e) => { if (seq === trendSeq.current) setTrendError(apiError(e)); });
  };
  const analyticsSeq = useRef(0);
  const loadAnalytics = (d: number) => {
    const seq = ++analyticsSeq.current;
    setAnalyticsError(null);
    dashboardApi.analytics(d)
      .then((r) => { if (seq === analyticsSeq.current) setAnalytics(r); })
      .catch((e) => { if (seq === analyticsSeq.current) setAnalyticsError(apiError(e)); });
  };

  useEffect(() => {
    loadOverview();
    loadRecent();
  }, []);
  useEffect(() => {
    loadTrend(days);
    loadAnalytics(days);
  }, [days]);

  if (overviewError) {
    return (
      <div className="acs-dash">
        <Result
          status="error"
          title="概览数据加载失败"
          subTitle={overviewError}
          extra={<Button type="primary" onClick={loadOverview}>重试</Button>}
        />
      </div>
    );
  }
  if (!data) {
    return (
      <div style={{ display: "flex", justifyContent: "center", alignItems: "center", minHeight: 320 }}>
        <Spin />
      </div>
    );
  }
  const t = data.today;
  const backlog = data.backlog || {};
  const kb = data.knowledge || {};
  const vecPct = kb.chunks_total ? Math.round((kb.chunks_ready / kb.chunks_total) * 100) : 100;

  const cards = [
    { title: "今日对话", value: t.conversations, icon: <MessageOutlined />, color: "#0F766E", bg: "rgba(15,118,110,.10)" },
    { title: "今日用户", value: t.users, icon: <UserOutlined />, color: "#0ea5e9", bg: "rgba(14,165,233,.10)" },
    { title: "平均响应(ms)", value: t.avg_latency_ms, icon: <ThunderboltOutlined />, color: "#f59e0b", bg: "rgba(245,158,11,.13)" },
    { title: "转人工率", value: (t.escalation_rate * 100).toFixed(1) + "%", icon: <CustomerServiceOutlined />, color: "#ef4444", bg: "rgba(239,68,68,.10)" },
    { title: "有帮助率", tip: "访客对 AI 回答点「有帮助」的占比（有帮助 / 有帮助+没帮助，累计）", value: data.satisfaction == null ? "—" : (data.satisfaction * 100).toFixed(0) + "%", icon: <LikeOutlined />, color: "#10b981", bg: "rgba(16,185,129,.10)" },
    { title: "今日成本($)", value: t.cost_usd, icon: <DollarOutlined />, color: "#8b5cf6", bg: "rgba(139,92,246,.10)" },
  ];

  return (
    <div className="acs-dash">
      <div className="acs-page-head">
        <div>
          <div className="acs-page-title">概览</div>
          <div className="acs-page-sub">今日核心指标、待处理事项与近 {days} 天趋势一览。</div>
        </div>
        <Segmented options={DAY_OPTIONS} value={days} onChange={(v) => setDays(v as number)} />
      </div>

      <Row gutter={[16, 16]}>
        {cards.map((c) => (
          <Col xs={12} sm={8} lg={4} key={c.title}>
            <Card size="small" styles={{ body: { padding: 16 } }}>
              <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
                <div className="acs-kpi-ic" style={{ background: c.bg, color: c.color }}>{c.icon}</div>
                <div style={{ minWidth: 0 }}>
                  <div className="acs-kpi-label" style={{ color: token.colorTextSecondary }}>
                    {c.tip ? <AntdTooltip title={c.tip}><span style={{ cursor: "help" }}>{c.title}</span></AntdTooltip> : c.title}
                  </div>
                  <div className="acs-kpi-value" style={{ color: token.colorText }}>{c.value}</div>
                </div>
              </div>
            </Card>
          </Col>
        ))}
      </Row>

      <Row gutter={[16, 16]} style={{ marginTop: 16 }}>
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
                <div style={{ fontSize: 13, color: token.colorTextSecondary, marginBottom: 6 }}>已向量化 {kb.chunks_ready ?? 0}/{kb.chunks_total ?? 0} 分块</div>
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

      <Row gutter={[16, 16]} style={{ marginTop: 16 }}>
        <Col xs={24} lg={15}>
          <Card size="small" title={`近 ${days} 天趋势`}>
            {trendError ? (
              <Alert type="error" showIcon message={trendError} action={<Button size="small" onClick={() => loadTrend(days)}>重试</Button>} />
            ) : trend.length === 0 ? (
              <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无数据" />
            ) : (
              <ResponsiveContainer width="100%" height={280}>
                <LineChart data={trend}>
                  <CartesianGrid strokeDasharray="3 3" stroke={token.colorBorderSecondary} />
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
            {recentError ? (
              <Alert type="error" showIcon message={recentError} action={<Button size="small" onClick={loadRecent}>重试</Button>} />
            ) : (
              <List
                size="small"
                className="acs-clickable-list"
                dataSource={recent}
                locale={{ emptyText: "暂无会话" }}
                renderItem={(it: any) => (
                  <List.Item
                    onClick={() => nav(`/conversations?session=${it.id}`)}
                  >
                    <List.Item.Meta
                      title={<span style={{ fontSize: 13 }}>{it.title || it.end_user_display || it.end_user_id || "匿名"}</span>}
                      description={<span style={{ fontSize: 12 }}>{fmtShort(it.last_activity_at)} · {statusMeta(CHANNEL_TYPE, it.channel_type).label}</span>}
                    />
                    {it.escalated
                      ? <Tag color={SESSION_STATUS.escalated.color}>{SESSION_STATUS.escalated.label}</Tag>
                      : it.status === "human_takeover"
                        ? <Tag color={SESSION_STATUS.human_takeover.color}>{SESSION_STATUS.human_takeover.label}</Tag>
                        : null}
                  </List.Item>
                )}
              />
            )}
          </Card>
        </Col>
      </Row>

      {analyticsError && (
        <Card size="small" title="运营分析" style={{ marginTop: 16 }}>
          <Alert type="error" showIcon message={analyticsError} action={<Button size="small" onClick={() => loadAnalytics(days)}>重试</Button>} />
        </Card>
      )}
      {!analyticsError && analytics && (
        <Row gutter={[16, 16]} style={{ marginTop: 16 }}>
          {/* CSAT 满意度评分 */}
          <Col xs={24} lg={8}>
            <Card
              size="small"
              title={
                <AntdTooltip title="会话结束后访客提交的 1–5 星服务评价">
                  <span style={{ cursor: "help" }}>{`满意度评分（近 ${analytics.days} 天）`}</span>
                </AntdTooltip>
              }
            >
              {analytics.csat.rated ? (
                <>
                  <Space size="large" style={{ marginBottom: 8 }}>
                    <Statistic title="平均分" value={analytics.csat.average ?? "—"} suffix="/ 5" valueStyle={{ color: "#10b981", fontSize: 24 }} />
                    <Statistic title="评分数" value={analytics.csat.rated} valueStyle={{ fontSize: 24 }} />
                  </Space>
                  <ResponsiveContainer width="100%" height={140}>
                    <BarChart data={analytics.csat.distribution}>
                      <CartesianGrid strokeDasharray="3 3" stroke={token.colorBorderSecondary} />
                      <XAxis dataKey="rating" tickFormatter={(v) => `${v}★`} fontSize={12} />
                      <YAxis allowDecimals={false} fontSize={12} />
                      <Tooltip />
                      <Bar dataKey="count" name="次数" fill="#10b981" radius={[4, 4, 0, 0]} />
                    </BarChart>
                  </ResponsiveContainer>
                </>
              ) : (
                <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无评分" />
              )}
            </Card>
          </Col>

          {/* 知识库命中 */}
          <Col xs={24} lg={8}>
            <Card size="small" title="知识库命中" extra={<Button type="link" size="small" onClick={() => nav("/knowledge")}>管理 <RightOutlined /></Button>}>
              <Statistic
                title="回答引用知识库比例"
                value={analytics.knowledge.grounding_rate == null ? "—" : (analytics.knowledge.grounding_rate * 100).toFixed(0) + "%"}
                valueStyle={{ color: "#0F766E", fontSize: 24 }}
              />
              <div style={{ fontSize: 12, color: token.colorTextSecondary, margin: "4px 0 8px" }}>Top 引用知识</div>
              <List
                size="small"
                dataSource={(analytics.knowledge.top_items || []).slice(0, 5)}
                locale={{ emptyText: "暂无引用" }}
                renderItem={(it: any) => (
                  <List.Item>
                    <List.Item.Meta
                      title={<span style={{ fontSize: 13 }}>{it.title}</span>}
                      description={<span style={{ fontSize: 12 }}>{it.hits} 次 · 均分 {it.avg_score}</span>}
                    />
                  </List.Item>
                )}
              />
            </Card>
          </Col>

          {/* 成本分解 */}
          <Col xs={24} lg={8}>
            <Card size="small" title={`成本分解（近 ${analytics.days} 天）`}>
              <div style={{ fontSize: 12, color: token.colorTextSecondary, marginBottom: 4 }}>按模型</div>
              <List
                size="small"
                dataSource={analytics.cost.by_model || []}
                locale={{ emptyText: "暂无成本" }}
                renderItem={(it: any) => (
                  <List.Item>
                    <span style={{ fontSize: 13 }}>{it.model}</span>
                    <span style={{ fontSize: 13 }}>${it.cost_usd} · {it.messages} 条</span>
                  </List.Item>
                )}
              />
              <div style={{ fontSize: 12, color: token.colorTextSecondary, margin: "8px 0 4px" }}>按渠道</div>
              <Space wrap>
                {(analytics.cost.by_channel || []).length === 0
                  ? <span style={{ fontSize: 12, color: token.colorTextTertiary }}>暂无</span>
                  : analytics.cost.by_channel.map((c: any) => <Tag key={c.channel}>{c.channel}: ${c.cost_usd}</Tag>)}
              </Space>
            </Card>
          </Col>
        </Row>
      )}
    </div>
  );
}
