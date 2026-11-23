import { useEffect, useRef, useState } from "react";
import { Card, Col, Row, Spin, Empty, Button, Progress, theme, Segmented, Result, Alert, Tooltip as AntdTooltip } from "antd";
import { LineChart, Line, BarChart, Bar, XAxis, YAxis, Tooltip, Legend, ResponsiveContainer, CartesianGrid } from "recharts";
import { useNavigate } from "react-router-dom";
import { dashboardApi, conversationApi } from "../api";
import { apiError } from "../api/client";
import { useAuth, agentConsoleUrl } from "../auth";
import { fmtShort } from "../utils/time";
import { CHANNEL_TYPE, SESSION_STATUS, statusMeta } from "../utils/status";
import { IconChevronRight, IconExternal } from "../icons";
import { PageHeader } from "../components/PageHeader";
import { StatusDot } from "../components/StatusDot";

const DAY_OPTIONS = [
  { label: "近 7 天", value: 7 },
  { label: "近 14 天", value: 14 },
  { label: "近 30 天", value: 30 },
  { label: "近 90 天", value: 90 },
];

export function Dashboard() {
  const nav = useNavigate();
  const { user } = useAuth();
  const openAgentConsole = () => window.open(agentConsoleUrl(user), "_blank", "noopener,noreferrer");
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
  const guard = data.guard || {};
  const kb = data.knowledge || {};
  const vecPct = kb.chunks_total ? Math.round((kb.chunks_ready / kb.chunks_total) * 100) : 100;
  const pct = (v: number | null | undefined, digits = 0) => (v == null ? "—" : (v * 100).toFixed(digits));

  const metrics: { label: string; value: string | number; unit?: string; tip?: string }[] = [
    { label: "今日对话", value: t.conversations },
    { label: "今日用户", value: t.users },
    { label: "平均响应", value: t.avg_latency_ms, unit: "ms" },
    { label: "转人工率", value: pct(t.escalation_rate, 1), unit: "%" },
    { label: "有帮助率", value: pct(data.satisfaction), unit: data.satisfaction == null ? undefined : "%", tip: "访客对 AI 回答点「有帮助」的占比（有帮助 / 有帮助+没帮助，累计）" },
    { label: "今日成本", value: t.cost_usd, unit: "USD" },
  ];

  // 图表配色：主线用品牌色，次要序列用中性灰，「转人工」用告警红 —— 颜色即语义。
  const series = { main: token.colorPrimary, muted: token.colorTextQuaternary, alert: token.colorError };
  const axis = { fontSize: 12, tickLine: false, axisLine: false, tick: { fill: token.colorTextTertiary } } as const;
  const tooltipStyle = {
    contentStyle: { background: token.colorBgElevated, border: `1px solid ${token.colorBorderSecondary}`, borderRadius: 4, fontSize: 12, boxShadow: "none" },
    labelStyle: { color: token.colorTextSecondary, marginBottom: 4 },
    itemStyle: { padding: 0 },
  };
  const more = (label: string, onClick: () => void, external?: boolean) => (
    <Button type="link" size="small" onClick={onClick} style={{ paddingInline: 0 }}>
      {label}{external ? <IconExternal /> : <IconChevronRight />}
    </Button>
  );
  const k = analytics?.knowledge?.by_kind || {};
  const search = analytics?.knowledge?.search || {};

  return (
    <div className="acs-dash">
      <PageHeader title="概览" extra={<Segmented options={DAY_OPTIONS} value={days} onChange={(v) => setDays(v as number)} />} />

      <div className="acs-metrics">
        {metrics.map((m) => (
          <div className="acs-metric" key={m.label}>
            {m.tip
              ? <AntdTooltip title={m.tip}><span className="acs-metric-label" data-tip>{m.label}</span></AntdTooltip>
              : <span className="acs-metric-label">{m.label}</span>}
            <div className="acs-metric-value">
              {m.value}
              {m.unit && <span className="acs-metric-unit">{m.unit}</span>}
            </div>
          </div>
        ))}
      </div>

      <Row gutter={[16, 16]} className="acs-dash-grid">
        <Col xs={24} lg={16}>
          <Card size="small" title={`近 ${days} 天趋势`}>
            {trendError ? (
              <Alert type="error" showIcon message={trendError} action={<Button size="small" onClick={() => loadTrend(days)}>重试</Button>} />
            ) : trend.length === 0 ? (
              <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="这段时间还没有对话" style={{ marginBlock: 96 }} />
            ) : (
              <ResponsiveContainer width="100%" height={292}>
                <LineChart data={trend} margin={{ top: 4, right: 8, bottom: 0, left: -16 }}>
                  <CartesianGrid vertical={false} stroke={token.colorBorderSecondary} />
                  <XAxis dataKey="day" {...axis} />
                  <YAxis allowDecimals={false} {...axis} />
                  <Tooltip {...tooltipStyle} cursor={{ stroke: token.colorBorder }} />
                  <Legend verticalAlign="top" align="right" iconType="plainline" height={28} wrapperStyle={{ fontSize: 12 }} />
                  <Line type="linear" dataKey="conversations" name="对话数" stroke={series.main} strokeWidth={2} dot={false} activeDot={{ r: 3 }} />
                  <Line type="linear" dataKey="messages" name="消息数" stroke={series.muted} strokeWidth={2} dot={false} activeDot={{ r: 3 }} />
                  <Line type="linear" dataKey="escalations" name="转人工" stroke={series.alert} strokeWidth={2} dot={false} activeDot={{ r: 3 }} />
                </LineChart>
              </ResponsiveContainer>
            )}
          </Card>
        </Col>

        <Col xs={24} lg={8}>
          <div style={{ display: "flex", flexDirection: "column", gap: 16, height: "100%" }}>
            {/* backlog → 坐席工作台（独立站，新标签打开） */}
            <Card size="small" title="待处理" extra={more("去工作台", openAgentConsole, true)} style={{ flex: 1 }}>
              <div className="acs-figures">
                <div>
                  <div className="acs-figure-label">待人工</div>
                  <div className="acs-figure-value" data-tone={backlog.pending_human ? "error" : undefined}>{backlog.pending_human ?? 0}</div>
                </div>
                <div>
                  <div className="acs-figure-label">接管中</div>
                  <div className="acs-figure-value" data-tone={backlog.in_takeover ? "warning" : undefined}>{backlog.in_takeover ?? 0}</div>
                </div>
                <AntdTooltip title={`离题 ${guard.by_kind?.off_topic ?? 0}，越狱话术 ${guard.by_kind?.jailbreak ?? 0}，超长 ${guard.by_kind?.too_long ?? 0}（可在「AI 配置 → 防滥用」调整）`}>
                  <div>
                    <div className="acs-figure-label">今日拦截</div>
                    <div className="acs-figure-value">{guard.total ?? 0}</div>
                  </div>
                </AntdTooltip>
              </div>
            </Card>

            <Card size="small" title="知识库" extra={more("管理", () => nav("/knowledge"))} style={{ flex: 1 }}>
              <div className="acs-figures" style={{ flexWrap: "nowrap", alignItems: "flex-start" }}>
                <div>
                  <div className="acs-figure-label">知识条目</div>
                  <div className="acs-figure-value">{kb.items ?? 0}</div>
                </div>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div className="acs-figure-label">已向量化分块 {kb.chunks_ready ?? 0} / {kb.chunks_total ?? 0}</div>
                  <Progress percent={vecPct} size="small" status={vecPct < 100 ? "active" : "normal"} style={{ marginTop: 8 }} />
                </div>
              </div>
            </Card>
          </div>
        </Col>
      </Row>

      <Row gutter={[16, 16]} className="acs-dash-grid">
        <Col xs={24} lg={12}>
          <Card size="small" title="最近会话" extra={more("全部", () => nav("/conversations"))}>
            {recentError ? (
              <Alert type="error" showIcon message={recentError} action={<Button size="small" onClick={loadRecent}>重试</Button>} />
            ) : recent.length === 0 ? (
              <div className="acs-rows-empty">还没有会话</div>
            ) : (
              <div className="acs-rows">
                {recent.map((it: any) => (
                  <button type="button" className="acs-row" key={it.id} onClick={() => nav(`/conversations?session=${it.id}`)}>
                    <span className="acs-row-main">{it.title || it.end_user_display || it.end_user_id || "匿名"}</span>
                    {it.status === "human_takeover"
                      ? <StatusDot meta={SESSION_STATUS.human_takeover} />
                      : it.escalated ? <StatusDot meta={SESSION_STATUS.escalated} /> : null}
                    <span className="acs-row-meta">{statusMeta(CHANNEL_TYPE, it.channel_type).label}</span>
                    <span className="acs-row-meta">{fmtShort(it.last_activity_at)}</span>
                  </button>
                ))}
              </div>
            )}
          </Card>
        </Col>

        {analyticsError && (
          <Col xs={24} lg={12}>
            <Card size="small" title="运营分析">
              <Alert type="error" showIcon message={analyticsError} action={<Button size="small" onClick={() => loadAnalytics(days)}>重试</Button>} />
            </Card>
          </Col>
        )}

        {!analyticsError && analytics && (
          <>
            {/* 知识库命中 */}
            <Col xs={24} lg={12}>
              <Card size="small" title="知识库命中" extra={more("未命中问题", () => nav("/knowledge?tab=gaps"))}>
                <div className="acs-figures" style={{ marginBottom: 12 }}>
                  <AntdTooltip title="业务问题中，回答真正用上了知识库资料的比例。分母不含寒暄、转人工、拒答和失败的回答；回答说「没查到」的不算命中。">
                    <div>
                      <div className="acs-figure-label">有效命中率</div>
                      <div className="acs-figure-value">
                        {pct(analytics.knowledge.effective_rate)}
                        {analytics.knowledge.effective_rate != null && <span className="acs-metric-unit">%</span>}
                      </div>
                    </div>
                  </AntdTooltip>
                  <div>
                    <div className="acs-figure-label">业务问题</div>
                    <div className="acs-figure-value">{analytics.knowledge.business_answers ?? 0}</div>
                  </div>
                  <div>
                    <div className="acs-figure-label">用上资料</div>
                    <div className="acs-figure-value">{k.grounded ?? 0}</div>
                  </div>
                  <div>
                    <div className="acs-figure-label">说没资料</div>
                    <div className="acs-figure-value">{k.no_info ?? 0}</div>
                  </div>
                  <div>
                    <div className="acs-figure-label">未检索</div>
                    <div className="acs-figure-value">{k.general ?? 0}</div>
                  </div>
                </div>
                <div className="acs-caption">
                  检索调用 {search.calls ?? 0} 次，命中 {search.hit_rate == null ? "—" : pct(search.hit_rate) + "%"}
                  {k.unclassified ? `；另有未分类旧数据 ${k.unclassified} 条` : ""}
                </div>
                <div className="acs-caption" style={{ margin: "14px 0 2px" }}>引用最多的知识</div>
                {(analytics.knowledge.top_items || []).length === 0 ? (
                  <div className="acs-rows-empty">这段时间还没有引用</div>
                ) : (
                  <div className="acs-rows">
                    {(analytics.knowledge.top_items || []).slice(0, 5).map((it: any, i: number) => (
                      // 点击跳知识库并深链到该条目（?item=）；旧引用可能缺 item_id，退回默认列表
                      <button type="button" className="acs-row" key={it.item_id || i} onClick={() => nav(it.item_id ? `/knowledge?item=${it.item_id}` : "/knowledge")}>
                        <span className="acs-row-main">{it.title}</span>
                        <span className="acs-row-meta">均分 {it.avg_score}</span>
                        <span className="acs-row-meta" style={{ minWidth: 48, textAlign: "right" }}>{it.hits} 次</span>
                      </button>
                    ))}
                  </div>
                )}
              </Card>
            </Col>

            {/* CSAT 满意度评分 */}
            <Col xs={24} lg={12}>
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
                    <div className="acs-figures" style={{ marginBottom: 8 }}>
                      <div>
                        <div className="acs-figure-label">平均分</div>
                        <div className="acs-figure-value">{analytics.csat.average ?? "—"}<span className="acs-metric-unit">/ 5</span></div>
                      </div>
                      <div>
                        <div className="acs-figure-label">评分数</div>
                        <div className="acs-figure-value">{analytics.csat.rated}</div>
                      </div>
                    </div>
                    <ResponsiveContainer width="100%" height={150}>
                      <BarChart data={analytics.csat.distribution} margin={{ top: 4, right: 8, bottom: 0, left: -16 }}>
                        <CartesianGrid vertical={false} stroke={token.colorBorderSecondary} />
                        <XAxis dataKey="rating" tickFormatter={(v) => `${v} 星`} {...axis} />
                        <YAxis allowDecimals={false} {...axis} />
                        <Tooltip {...tooltipStyle} cursor={{ fill: token.colorFillQuaternary }} labelFormatter={(v) => `${v} 星`} />
                        <Bar dataKey="count" name="次数" fill={series.main} maxBarSize={36} radius={[2, 2, 0, 0]} />
                      </BarChart>
                    </ResponsiveContainer>
                  </>
                ) : (
                  <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="这段时间还没有评分" />
                )}
              </Card>
            </Col>

            {/* 成本分解 */}
            <Col xs={24} lg={12}>
              <Card size="small" title={`成本分解（近 ${analytics.days} 天）`}>
                <div className="acs-caption">按模型</div>
                {(analytics.cost.by_model || []).length === 0 ? (
                  <div className="acs-rows-empty">这段时间还没有成本</div>
                ) : (
                  <div className="acs-rows">
                    {analytics.cost.by_model.map((it: any) => (
                      <div className="acs-row" key={it.model}>
                        <span className="acs-row-main">{it.model}</span>
                        <span className="acs-row-meta">{it.messages} 条</span>
                        <span style={{ minWidth: 72, textAlign: "right" }}>${it.cost_usd}</span>
                      </div>
                    ))}
                  </div>
                )}
                {(analytics.cost.by_channel || []).length > 0 && (
                  <>
                    <div className="acs-caption" style={{ marginTop: 12 }}>按渠道</div>
                    <div className="acs-rows">
                      {analytics.cost.by_channel.map((c: any) => (
                        <div className="acs-row" key={c.channel}>
                          <span className="acs-row-main">{statusMeta(CHANNEL_TYPE, c.channel).label}</span>
                          <span style={{ minWidth: 72, textAlign: "right" }}>${c.cost_usd}</span>
                        </div>
                      ))}
                    </div>
                  </>
                )}
              </Card>
            </Col>
          </>
        )}
      </Row>
    </div>
  );
}
