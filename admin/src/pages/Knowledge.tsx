import { useEffect, useRef, useState } from "react";
import {
  Button, Card, Drawer, Form, Input, Select, Space, Table, Tag, Tabs, Modal,
  Upload, Tooltip, List, Popconfirm, App as AntApp, Badge, Alert, Progress, theme,
  Checkbox, AutoComplete, DatePicker, Segmented,
} from "antd";
import {
  PlusOutlined, SearchOutlined, UploadOutlined, ExperimentOutlined,
  HistoryOutlined, DownloadOutlined, PartitionOutlined, ThunderboltOutlined,
  ReloadOutlined, ExportOutlined, EditOutlined,
} from "@ant-design/icons";
import ReactMarkdown from "react-markdown";
import dayjs from "dayjs";
import { Link, useLocation } from "react-router-dom";
import { knowledgeApi } from "../api";
import { apiError } from "../api/client";
import { useAuth, canEdit } from "../auth";
import { useDebounce } from "../hooks/useDebounce";
import { usePolling } from "../hooks/usePolling";
import { fmtTime, fmtShort } from "../utils/time";
import { KNOWLEDGE_STATUS, KNOWLEDGE_SOURCE, CHUNK_STATUS, statusMeta } from "../utils/status";

export function Knowledge() {
  const { user } = useAuth();
  const editable = canEdit(user?.role);
  const { message } = AntApp.useApp();
  const [tab, setTab] = useState("items");
  // 从「未命中问题」带过来的新建标题；nonce 保证同一问题可以反复触发
  const [prefill, setPrefill] = useState<{ title: string; nonce: number } | null>(null);
  const [reviewCount, setReviewCount] = useState(0);
  // 待打开的条目 id：来自 URL 深链 ?item=<id>，或站内跳转（审核撞重、通过后查看条目）
  const [openItemId, setOpenItemId] = useState<string | null>(null);
  const location = useLocation();

  // 挂载时用轻量 count 端点拉徽标数（不必为一个数字取回整包候选大文本）；
  // 之后由 ReviewTab 加载/审核时通过 onCount 上报，避免切 Tab 重复请求
  useEffect(() => {
    knowledgeApi.reviewCount().then((d) => setReviewCount(d.pending ?? 0)).catch(() => {});
  }, []);

  // 深链 ?item=<id>（引用统计、审核跳转等场景按此构造链接）→ 直接打开对应条目编辑
  useEffect(() => {
    const id = new URLSearchParams(location.search).get("item");
    if (id) { setOpenItemId(id); setTab("items"); }
  }, [location.search]);
  useEffect(() => {
    const t = new URLSearchParams(location.search).get("tab");
    if (t === "items" || t === "review" || t === "gaps") setTab(t);
  }, [location.search]);

  const jumpToItem = (id: string) => { setOpenItemId(id); setTab("items"); };

  return (
    <div>
      <div className="acs-page-title">知识库管理</div>
      <div className="acs-page-sub">维护 AI 回答所依据的资料；保存后自动向量化，支持版本回滚、批量导入与对话自动沉淀审核。</div>
      <Tabs
        activeKey={tab}
        onChange={setTab}
        items={[
          {
            key: "items", label: "知识条目",
            children: <ItemsTab editable={editable} openItemId={openItemId} onOpenedItem={() => setOpenItemId(null)} prefill={prefill} />,
          },
          {
            key: "review",
            label: <Badge count={reviewCount} size="small" offset={[10, 0]}>待审核</Badge>,
            children: <ReviewTab editable={editable} onCount={setReviewCount} onOpenItem={jumpToItem} />,
          },
          {
            key: "gaps", label: "未命中问题",
            children: <GapsTab editable={editable} onCreate={(title: string) => { setPrefill({ title, nonce: Date.now() }); setTab("items"); }} />,
          },
        ]}
      />
    </div>
  );
}

const GAP_KIND: Record<string, { label: string; color: string; tip: string }> = {
  miss: { label: "检索未命中", color: "red", tip: "知识库检索没有返回任何资料" },
  no_info: { label: "回答说没资料", color: "orange", tip: "检索有返回，但 AI 判断资料不能回答这个问题" },
  unsearched: { label: "未检索", color: "default", tip: "AI 没有查知识库就直接回答了（新版本已改为每轮必查）" },
};

/** 未命中问题：知识库答不上的客户问题，按语义归并，作为补知识的工作台。 */
function GapsTab({ editable, onCreate }: { editable: boolean; onCreate: (title: string) => void }) {
  const { message } = AntApp.useApp();
  const { token } = theme.useToken();
  const [days, setDays] = useState(30);
  const [data, setData] = useState<any>(null);
  const [loading, setLoading] = useState(false);

  const load = async () => {
    setLoading(true);
    try {
      setData(await knowledgeApi.gaps(days));
    } catch (e) {
      message.error(apiError(e));
    } finally {
      setLoading(false);
    }
  };
  useEffect(() => { load(); /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, [days]);

  const ignore = async (q: string) => {
    try {
      await knowledgeApi.gapIgnore(q);
      message.success("已忽略，相近问法也不再显示");
      load();
    } catch (e) {
      message.error(apiError(e));
    }
  };

  const groups: any[] = data?.groups || [];
  return (
    <Card>
      <Space wrap style={{ marginBottom: 12, width: "100%", justifyContent: "space-between" }}>
        <Space wrap>
          <Segmented value={days} onChange={(v) => setDays(Number(v))} options={[{ label: "近 7 天", value: 7 }, { label: "近 30 天", value: 30 }, { label: "近 90 天", value: 90 }]} />
          <Button icon={<ReloadOutlined />} onClick={load} loading={loading}>刷新</Button>
        </Space>
        {data && (
          <span style={{ fontSize: 12, color: token.colorTextSecondary }}>
            扫描 {data.scanned_answers} 条回答，其中 {data.gap_answers} 条没答上，归并为 {groups.length} 个问题{data.ignored ? ` · 已忽略 ${data.ignored} 个` : ""}
          </span>
        )}
      </Space>
      <Alert
        style={{ marginBottom: 12 }} type="info" showIcon
        message="这里是客户问过、但知识库答不上的问题，相近问法已合并。点「新建知识」会把问题带入标题，写上答案并发布即可；一次性或无关的问题点「忽略」。"
      />
      <Table
        rowKey="question" loading={loading} dataSource={groups} size="middle"
        pagination={{ pageSize: 20, showSizeChanger: false }}
        locale={{ emptyText: "这个时间段内没有答不上的问题" }}
        columns={[
          {
            title: "客户问题", dataIndex: "question",
            render: (q: string, r: any) => (
              <div>
                <div style={{ fontWeight: 500 }}>{q}</div>
                {r.examples?.length > 1 && (
                  <div style={{ fontSize: 12, color: token.colorTextTertiary, marginTop: 2 }}>
                    其他问法：{r.examples.slice(1).join("　/　")}
                  </div>
                )}
              </div>
            ),
          },
          { title: "次数", dataIndex: "count", width: 80, align: "right" as const, sorter: (a: any, b: any) => a.count - b.count, defaultSortOrder: "descend" as const },
          {
            title: "原因", dataIndex: "kinds", width: 220,
            render: (kinds: Record<string, number>) => (
              <Space size={4} wrap>
                {Object.entries(kinds || {}).map(([k, n]) => {
                  const m = GAP_KIND[k] || { label: k, color: "default", tip: "" };
                  return <Tooltip key={k} title={m.tip}><Tag color={m.color}>{m.label}{n > 1 ? ` ×${n}` : ""}</Tag></Tooltip>;
                })}
              </Space>
            ),
          },
          { title: "最近", dataIndex: "last_at", width: 120, render: (t: string) => fmtShort(t) },
          {
            title: "操作", width: 240,
            render: (_: any, r: any) => (
              <Space size={4} wrap>
                {editable && <Button type="link" size="small" icon={<PlusOutlined />} onClick={() => onCreate(r.question)}>新建知识</Button>}
                {r.session_id && <Link to={`/conversations?session=${r.session_id}`}>查看对话</Link>}
                {editable && (
                  <Popconfirm title="忽略这个问题？" description="相近的问法也不再显示" onConfirm={() => ignore(r.question)}>
                    <Button type="link" size="small" danger>忽略</Button>
                  </Popconfirm>
                )}
              </Space>
            ),
          },
        ]}
      />
    </Card>
  );
}

function ItemsTab({ editable, openItemId, onOpenedItem, prefill }: { editable: boolean; openItemId?: string | null; onOpenedItem?: () => void; prefill?: { title: string; nonce: number } | null }) {
  const { message, modal } = AntApp.useApp();
  const { token } = theme.useToken();
  const [data, setData] = useState<any>({ items: [], total: 0 });
  const [loading, setLoading] = useState(false);
  const [params, setParams] = useState<any>({ q: "", status: "", category: "", tag: "", source: "", validity: "", page: 1, page_size: 10 });
  const [drawer, setDrawer] = useState<any>(null); // editing item or {} for new
  const [form] = Form.useForm();
  const [testOpen, setTestOpen] = useState(false);
  const [importOpen, setImportOpen] = useState(false);
  const [versionsItem, setVersionsItem] = useState<any>(null);
  const [chunksItem, setChunksItem] = useState<any>(null);
  const [cats, setCats] = useState<any[]>([]);
  const [allTags, setAllTags] = useState<string[]>([]);
  const [selected, setSelected] = useState<any[]>([]);
  const [vec, setVec] = useState<any>(null);
  const [usage, setUsage] = useState<Record<string, { citation_count: number; last_cited_at?: string }>>({});
  const [unusedOnly, setUnusedOnly] = useState(false);
  // 编辑保存后向量重嵌是异步的，列表里的 ready/total 短暂是旧内容的统计——
  // 本地记下刚保存的条目 id，「向量」列先显示「重建中」占位，轮询收敛后自动清除。
  const [reembedMarks, setReembedMarks] = useState<{ id: string; at: number }[]>([]);
  const [catModalOpen, setCatModalOpen] = useState(false);
  const [renameOpen, setRenameOpen] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [search, setSearch] = useState("");
  const dq = useDebounce(search, 400);
  useEffect(() => { setParams((p: any) => ({ ...p, q: dq, page: 1 })); }, [dq]);

  const loadMeta = () => {
    knowledgeApi.categories().then((d) => setCats(d.categories)).catch(() => {});
    knowledgeApi.tags().then((d) => setAllTags(d.tags)).catch(() => {});
    knowledgeApi.embeddingStatus().then(setVec).catch(() => {});
    knowledgeApi.usageStats().then((d) => {
      const m: Record<string, any> = {};
      (d.items || []).forEach((it: any) => { m[it.item_id] = it; });
      setUsage(m);
    }).catch(() => {});
  };
  // 递增序号防过期响应：快速切换筛选/翻页时，只有最新一次请求的结果才落地。
  const loadSeq = useRef(0);
  const load = () => {
    const seq = ++loadSeq.current;
    setLoading(true);
    const query: any = { ...params };
    if (!query.validity) delete query.validity; // validity 是枚举参数，空串会被后端拒绝
    knowledgeApi.list(query)
      .then((d) => { if (seq === loadSeq.current) setData(d); })
      .catch((e) => { if (seq === loadSeq.current) message.error(apiError(e)); })
      .finally(() => { if (seq === loadSeq.current) setLoading(false); });
  };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { load(); }, [params.q, params.status, params.category, params.tag, params.source, params.validity, params.page, params.page_size]);
  // 分类/标签/向量化进度变化不频繁：只在挂载和写操作后刷新，翻页/搜索不重复拉
  useEffect(loadMeta, []);
  // 后台嵌入/排队/刚保存的条目在途时轮询进度条，收敛即自动停止；空闲零请求
  const queuedItems = vec?.queued_items ?? 0;
  const embedding = !!vec && (vec.ready_chunks < vec.total_chunks || vec.rebuild?.status === "running" || queuedItems > 0 || reembedMarks.length > 0);
  usePolling(() => { knowledgeApi.embeddingStatus().then(setVec).catch(() => {}); }, 5000, { enabled: embedding });
  // 向量任务收敛（全就绪、无排队、无重建）后清掉「重建中」占位并刷新列表拿新计数；
  // 4 秒缓冲避开「保存后 worker 尚未领取任务、状态短暂显示空闲」的误判窗口。
  useEffect(() => {
    if (!vec || !reembedMarks.length) return;
    const idle = vec.ready_chunks >= vec.total_chunks && vec.rebuild?.status !== "running" && !((vec.queued_items ?? 0) > 0);
    if (!idle) return;
    const remain = reembedMarks.filter((m) => Date.now() - m.at < 4000);
    if (remain.length !== reembedMarks.length) { setReembedMarks(remain); load(); }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [vec]);
  const reloadAll = () => { load(); loadMeta(); };

  const bulkDelete = async () => {
    try {
      const results = await Promise.allSettled(selected.map((id) => knowledgeApi.remove(String(id))));
      const failedIds = selected.filter((_, i) => results[i].status === "rejected");
      // 后端对已发布条目返回 409「请先归档」——失败条目保留选中，引导直接点「归档」再删
      const blocked = results.filter((r) => r.status === "rejected" && (r as any).reason?.response?.status === 409).length;
      if (failedIds.length) {
        message.warning(
          blocked
            ? `删除成功 ${selected.length - failedIds.length} 条；${blocked} 条为已发布条目须先归档。失败条目已保留选中，可点上方「归档」后再删除`
            : `删除成功 ${selected.length - failedIds.length} 条，失败 ${failedIds.length} 条，失败条目已保留选中`
        );
      } else message.success(`已删除 ${selected.length} 条`);
      setSelected(failedIds);
    } finally {
      reloadAll();
    }
  };
  const bulkUpdate = async (body: any, verb: string) => {
    try {
      const results = await Promise.allSettled(selected.map((id) => knowledgeApi.update(String(id), body)));
      const failedIds = selected.filter((_, i) => results[i].status === "rejected");
      if (failedIds.length) message.warning(`${verb}成功 ${selected.length - failedIds.length} 条，失败 ${failedIds.length} 条，失败条目已保留选中`);
      else message.success(`已${verb} ${selected.length} 条`);
      setSelected(failedIds);
    } finally {
      reloadAll();
    }
  };
  const bulkStatus = (status: string) => bulkUpdate({ status }, "更新");

  // 导出走认证的 blob 下载（裸 <a href> 带不上 Bearer 头）；后端导出仅支持状态/分类筛选
  const exportNow = async () => {
    setExporting(true);
    try {
      const p: any = { format: "csv" };
      if (params.status) p.status = params.status;
      if (params.category) p.category = params.category;
      const r = await knowledgeApi.exportItems(p);
      const url = URL.createObjectURL(r.data as Blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `knowledge_${new Date().toISOString().slice(0, 10)}.csv`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (e) {
      message.error(apiError(e));
    } finally {
      setExporting(false);
    }
  };

  const reembedPending = async () => {
    try {
      const r = await knowledgeApi.reembedPending();
      message.success(r.queued ? `已排队重嵌 ${r.queued} 条，向量将后台生成` : "没有待处理的条目");
      loadMeta();
    } catch (e) {
      message.error(apiError(e));
    }
  };

  const openEdit = async (item: any) => {
    if (item?.id) {
      try {
        const full = await knowledgeApi.get(item.id);
        form.resetFields(); // 清掉上一次编辑残留的 touched 状态，脏检查才准确
        form.setFieldsValue({
          ...full,
          effective_from: full.effective_from ? dayjs(full.effective_from) : null,
          expires_at: full.expires_at ? dayjs(full.expires_at) : null,
        });
        setDrawer(full);
      } catch (e) {
        message.error(apiError(e));
      }
    } else {
      form.resetFields();
      form.setFieldsValue({ status: "published", tags: [] });
      setDrawer({});
    }
  };

  // 深链/站内跳转（?item=<id>、审核 Tab 的「查看条目」等）→ 打开对应条目的编辑抽屉
  useEffect(() => {
    if (!openItemId) return;
    openEdit({ id: openItemId }); // 条目不存在时 openEdit 内部会 message 提示
    onOpenedItem?.();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [openItemId]);

  // 「未命中问题」→ 新建知识：打开新建抽屉并把客户问题带入标题
  useEffect(() => {
    if (!prefill) return;
    form.resetFields();
    form.setFieldsValue({ status: "published", tags: [], title: prefill.title });
    setDrawer({});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [prefill?.nonce]);

  const [saving, setSaving] = useState(false);
  const save = async () => {
    let v: any;
    try {
      v = await form.validateFields();
    } catch {
      return; // 校验失败：字段旁已有内联错误提示
    }
    setSaving(true);
    try {
      const body = {
        ...v,
        // 时效字段 null 是有意义的「清空」，必须显式发送（undefined 会被 JSON 序列化丢弃）
        effective_from: v.effective_from ? v.effective_from.toISOString() : null,
        expires_at: v.expires_at ? v.expires_at.toISOString() : null,
      };
      if (drawer.id) {
        // base_version = 打开抽屉时的版本：他人先保存时后端返回 409，不静默覆盖
        await knowledgeApi.update(drawer.id, { ...body, base_version: drawer.version });
        setReembedMarks((ms) => [...ms.filter((m) => m.id !== drawer.id), { id: drawer.id, at: Date.now() }]);
      } else await knowledgeApi.create(body);
      message.success("已保存，向量将后台异步生成");
      setDrawer(null);
      reloadAll();
    } catch (e: any) {
      if (drawer.id && e?.response?.status === 409) {
        modal.confirm({
          title: "保存冲突",
          content: `${apiError(e)}。加载最新版本会丢弃你当前的修改，请先复制要保留的内容。`,
          okText: "加载最新版本",
          cancelText: "留在当前编辑",
          onOk: () => openEdit({ id: drawer.id }),
        });
      } else message.error(apiError(e));
    } finally {
      setSaving(false);
    }
  };

  // 已发布条目后端拒绝直接删除（409）：就地给「归档」入口，不让用户跑回列表找按钮
  const removeOne = async (r: any) => {
    try {
      await knowledgeApi.remove(r.id);
      message.success("已删除");
      reloadAll();
    } catch (e: any) {
      if (e?.response?.status === 409) {
        modal.confirm({
          title: "已发布条目须先归档",
          content: apiError(e),
          okText: "归档此条",
          cancelText: "取消",
          onOk: async () => {
            try {
              await knowledgeApi.update(r.id, { status: "archived" });
              message.success("已归档（已停止参与检索）；确认无影响后可再次删除");
            } catch (e2) {
              message.error(apiError(e2));
            }
            reloadAll();
          },
        });
      } else {
        message.error(apiError(e));
        reloadAll();
      }
    }
  };

  // 抽屉带未保存修改时（点关闭/按 ESC）先确认再丢弃；保存成功路径直接 setDrawer(null) 不经过这里
  const closeDrawer = () => {
    if (editable && form.isFieldsTouched()) {
      modal.confirm({
        title: "有未保存的修改",
        content: "关闭后当前修改将丢失，确认放弃？",
        okText: "放弃修改",
        okButtonProps: { danger: true },
        cancelText: "继续编辑",
        onOk: () => setDrawer(null),
      });
    } else setDrawer(null);
  };

  const columns = [
    { title: "标题", dataIndex: "title", render: (t: string, r: any) => t || <span style={{ color: token.colorTextTertiary }}>{r.content.slice(0, 30)}…</span> },
    { title: "分类", dataIndex: "category", width: 120, render: (c: string) => c ? <Tag>{c}</Tag> : "—" },
    { title: "状态", dataIndex: "status", width: 90, render: (s: string) => { const m = statusMeta(KNOWLEDGE_STATUS, s); return <Tag color={m.color}>{m.label}</Tag>; } },
    {
      title: "有效期", width: 100, render: (_: any, r: any) => {
        const now = Date.now();
        if (r.expires_at && new Date(r.expires_at).getTime() <= now)
          return <Tooltip title={`已于 ${fmtTime(r.expires_at)} 过期，检索不再命中——请更新时效或归档`}><Tag color="red">已过期</Tag></Tooltip>;
        if (r.effective_from && new Date(r.effective_from).getTime() > now)
          return <Tooltip title={`${fmtTime(r.effective_from)} 起才生效，此前检索不命中`}><Tag color="blue">未生效</Tag></Tooltip>;
        if (r.expires_at)
          return <Tooltip title={`有效至 ${fmtTime(r.expires_at)}`}><span style={{ fontSize: 12, color: token.colorTextSecondary }}>至 {fmtShort(r.expires_at)}</span></Tooltip>;
        return "—";
      },
    },
    { title: "来源", dataIndex: "source", width: 100, render: (s: string) => { const m = statusMeta(KNOWLEDGE_SOURCE, s); return <Tag color={m.color}>{m.label}</Tag>; } },
    { title: "版本", dataIndex: "version", width: 70 },
    {
      title: "向量", width: 90, render: (_: any, r: any) => {
        if (reembedMarks.some((m) => m.id === r.id))
          return <Tooltip title="内容已更新，向量重建中——完成前检索命中的仍是旧内容"><Tag color="processing">重建中</Tag></Tooltip>;
        const total = r.chunk_count ?? 0, ready = r.ready_count ?? 0;
        if (!total) return <Tooltip title="保存后系统自动分块并生成向量，稍候刷新即可"><Tag>未分块</Tag></Tooltip>;
        return (
          <Tooltip title={ready >= total ? "向量已就绪" : "部分分片缺向量，检索可能漏命中——点「分片」查看或重新嵌入"}>
            <Tag color={ready >= total ? "green" : "error"}>{ready}/{total}</Tag>
          </Tooltip>
        );
      },
    },
    {
      title: "引用", width: 90,
      sorter: (a: any, b: any) => (usage[a.id]?.citation_count || 0) - (usage[b.id]?.citation_count || 0),
      render: (_: any, r: any) => {
        const u = usage[r.id];
        const n = u?.citation_count || 0;
        if (!n) return <Tooltip title="从未被 AI 回答引用——可能是死知识，建议核查内容或归档"><Tag>未引用</Tag></Tooltip>;
        return <Tooltip title={u?.last_cited_at ? `最近引用：${fmtTime(u.last_cited_at)}` : undefined}><Tag color="blue">{n} 次</Tag></Tooltip>;
      },
    },
    { title: "更新时间", dataIndex: "updated_at", width: 160, render: (t: string) => fmtTime(t) },
    {
      title: "操作", width: 210, render: (_: any, r: any) => (
        <Space size="small">
          <a onClick={() => openEdit(r)}>{editable ? "编辑" : "查看"}</a>
          <a onClick={() => setChunksItem(r)}><PartitionOutlined /> 分片</a>
          <a onClick={() => setVersionsItem(r)}><HistoryOutlined /> 版本</a>
          {editable && (
            <Popconfirm title="确认删除？删除后不可恢复" onConfirm={() => removeOne(r)}>
              <a style={{ color: "#ef4444" }}>删除</a>
            </Popconfirm>
          )}
        </Space>
      ),
    },
  ];

  return (
    <Card>
      {vec && (
        <div style={{ marginBottom: 12, display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
          <span style={{ fontSize: 13, color: token.colorTextSecondary }}>向量化 {vec.ready_chunks}/{vec.total_chunks} 分块</span>
          <Progress
            percent={vec.total_chunks ? Math.round((vec.ready_chunks / vec.total_chunks) * 100) : 100}
            size="small" style={{ width: 180 }}
            status={
              vec.rebuild?.status === "failed" ? "exception"
                : vec.rebuild?.status === "running" ? "active"
                : vec.ready_chunks < vec.total_chunks || queuedItems > 0 || (vec.rebuild?.status === "completed" && vec.rebuild?.error) ? "normal"
                : "success"
            }
          />
          {vec.rebuild?.status === "running" && <Tag color="processing">重建中 {Math.round((vec.rebuild.progress || 0) * 100)}%</Tag>}
          {queuedItems > 0 && <Tag color="processing">排队中 {queuedItems} 条</Tag>}
          {vec.rebuild?.status === "failed" && (
            <span style={{ fontSize: 12, color: token.colorError }}>
              重建失败：{vec.rebuild.error || "未知原因"}——修复配置后到「AI 配置」页重试
            </span>
          )}
          {vec.rebuild?.status === "completed" && vec.rebuild?.error && (
            <span style={{ fontSize: 12, color: token.colorWarning }}>{vec.rebuild.error}</span>
          )}
        </div>
      )}

      <Space style={{ marginBottom: 12 }} wrap>
        <Input
          allowClear placeholder="搜索标题/内容" prefix={<SearchOutlined />} style={{ width: 220 }}
          value={search} onChange={(e) => setSearch(e.target.value)}
        />
        <Select
          allowClear placeholder="分类" style={{ width: 130 }}
          options={cats.map((c: any) => ({ value: c.name, label: `${c.name} (${c.count})` }))}
          onChange={(v) => setParams((p: any) => ({ ...p, category: v || "", page: 1 }))}
        />
        {editable && (
          <Tooltip title="重命名分类；改成已有分类名即合并两个分类">
            <Button icon={<EditOutlined />} onClick={() => setRenameOpen(true)}>重命名分类</Button>
          </Tooltip>
        )}
        <Select
          allowClear showSearch placeholder="标签" style={{ width: 130 }}
          options={allTags.map((t) => ({ value: t, label: t }))}
          onChange={(v) => setParams((p: any) => ({ ...p, tag: v || "", page: 1 }))}
        />
        <Select
          allowClear placeholder="状态" style={{ width: 110 }}
          options={Object.entries(KNOWLEDGE_STATUS).map(([value, m]) => ({ value, label: m.label }))}
          onChange={(v) => setParams((p: any) => ({ ...p, status: v || "", page: 1 }))}
        />
        <Select
          allowClear placeholder="来源" style={{ width: 120 }}
          options={Object.entries(KNOWLEDGE_SOURCE).map(([value, m]) => ({ value, label: m.label }))}
          onChange={(v) => setParams((p: any) => ({ ...p, source: v || "", page: 1 }))}
        />
        <Select
          allowClear placeholder="有效期" style={{ width: 110 }}
          options={[
            { value: "active", label: "生效中" },
            { value: "expired", label: "已过期" },
            { value: "scheduled", label: "未生效" },
          ]}
          onChange={(v) => setParams((p: any) => ({ ...p, validity: v || "", page: 1 }))}
        />
        <Checkbox checked={unusedOnly} onChange={(e) => setUnusedOnly(e.target.checked)}>只看未引用</Checkbox>
        <Button icon={<ExperimentOutlined />} onClick={() => setTestOpen(true)}>测试检索</Button>
        {editable && <Button icon={<UploadOutlined />} onClick={() => setImportOpen(true)}>批量导入</Button>}
        <Tooltip title="按当前状态/分类筛选导出 CSV（搜索词、标签等其他筛选不参与）">
          <Button icon={<ExportOutlined />} loading={exporting} onClick={exportNow}>导出</Button>
        </Tooltip>
        {editable && (
          <Tooltip title="为所有缺向量/未分块的条目重新生成向量；系统每 10 分钟也会自动补齐，此按钮用于立即触发">
            <Button icon={<ReloadOutlined />} onClick={reembedPending}>重嵌待处理</Button>
          </Tooltip>
        )}
        {editable && <Button type="primary" icon={<PlusOutlined />} onClick={() => openEdit(null)}>新增条目</Button>}
      </Space>

      {editable && selected.length > 0 && (
        <div style={{ marginBottom: 12, padding: "7px 12px", background: token.colorPrimaryBg, borderRadius: 6, display: "flex", alignItems: "center", gap: 10 }}>
          <span style={{ fontSize: 13 }}>已选 {selected.length} 项</span>
          <Button size="small" onClick={() => bulkStatus("published")}>发布</Button>
          <Button size="small" onClick={() => bulkStatus("archived")}>归档</Button>
          <Button size="small" onClick={() => setCatModalOpen(true)}>设置分类</Button>
          <Popconfirm
            title={`确认删除选中的 ${selected.length} 条？`}
            description="删除不可恢复（版本历史一并清除）；已发布条目请先归档再删除"
            okText="删除" okButtonProps={{ danger: true }}
            onConfirm={bulkDelete}
          >
            <Button size="small" danger>删除</Button>
          </Popconfirm>
          <a onClick={() => setSelected([])} style={{ fontSize: 12 }}>取消选择</a>
        </div>
      )}

      <Table
        rowKey="id" loading={loading} columns={columns as any} size="small"
        dataSource={unusedOnly ? (data.items || []).filter((r: any) => !((usage[r.id]?.citation_count ?? 0) > 0)) : data.items}
        scroll={{ x: "max-content" }}
        rowSelection={editable ? { selectedRowKeys: selected, onChange: setSelected } : undefined}
        pagination={{
          current: params.page, pageSize: params.page_size, total: data.total,
          onChange: (page, page_size) => setParams((p: any) => ({ ...p, page, page_size })),
          showTotal: (t) => `共 ${t} 条`,
        }}
      />

      <Drawer
        title={drawer?.id ? "编辑知识条目" : "新增知识条目"} width={640} open={!!drawer}
        maskClosable={false}
        onClose={closeDrawer}
        extra={editable && <Button type="primary" loading={saving} disabled={saving} onClick={save}>保存</Button>}
      >
        {drawer?.extra?.session_id && (
          <Alert
            style={{ marginBottom: 12 }} type="info" showIcon
            message={<span>本条目由会话一键沉淀，<Link to={`/conversations?session=${drawer.extra.session_id}`}>查看来源对话</Link></span>}
          />
        )}
        <Form form={form} layout="vertical" disabled={!editable}>
          <Form.Item name="title" label="标题"><Input placeholder="简短标题" /></Form.Item>
          <Form.Item name="content" label="内容（支持 Markdown）" rules={[{ required: true, message: "请输入内容" }]}>
            <Input.TextArea rows={12} placeholder="知识正文。保存后系统会自动分块并生成向量。" />
          </Form.Item>
          <Form.Item noStyle shouldUpdate={(p, c) => p.content !== c.content}>
            {() => {
              const md = form.getFieldValue("content") || "";
              return md ? (
                <div style={{ marginTop: -8, marginBottom: 12 }}>
                  <div style={{ fontSize: 12, color: token.colorTextTertiary, marginBottom: 4 }}>Markdown 预览</div>
                  <div className="acs-md" style={{ border: `1px solid ${token.colorBorderSecondary}`, borderRadius: 6, padding: "8px 12px", background: token.colorFillQuaternary, maxHeight: 260, overflow: "auto" }}>
                    <ReactMarkdown>{md}</ReactMarkdown>
                  </div>
                </div>
              ) : null;
            }}
          </Form.Item>
          <Space style={{ width: "100%" }} size="large">
            <Form.Item name="category" label="分类" style={{ flex: 1 }}>
              <AutoComplete
                style={{ minWidth: 200 }}
                options={cats.map((c: any) => ({ value: c.name, label: `${c.name} (${c.count})` }))}
                placeholder="选择已有分类，或输入新分类名"
                filterOption={(input, option) => String(option?.value ?? "").toLowerCase().includes(input.toLowerCase())}
              />
            </Form.Item>
            <Form.Item name="status" label="状态">
              <Select style={{ width: 140 }} options={Object.entries(KNOWLEDGE_STATUS).map(([value, m]) => ({ value, label: m.label }))} />
            </Form.Item>
          </Space>
          <Form.Item name="tags" label="标签">
            <Select mode="tags" placeholder="回车添加标签" options={allTags.map((t) => ({ value: t }))} />
          </Form.Item>
          <Space size="large">
            <Form.Item name="effective_from" label="生效时间" tooltip="留空立即生效；未到生效时间的条目不参与检索">
              <DatePicker showTime allowClear style={{ width: 200 }} placeholder="留空立即生效" />
            </Form.Item>
            <Form.Item
              name="expires_at" label="过期时间" dependencies={["effective_from"]}
              tooltip="留空长期有效；过期后条目自动停止参与检索"
              rules={[expiresAfterEffective]}
            >
              <DatePicker showTime allowClear style={{ width: 200 }} placeholder="留空长期有效" />
            </Form.Item>
          </Space>
          {drawer?.id && <Alert type="info" showIcon message={`当前版本 v${drawer.version} · 已分块 ${drawer.chunk_count ?? "?"} 段`} />}
        </Form>
      </Drawer>

      <TestRetrievalModal
        open={testOpen} onClose={() => setTestOpen(false)}
        onOpenItem={(id: string) => { setTestOpen(false); openEdit({ id }); }}
      />
      <ImportModal open={importOpen} onClose={() => setImportOpen(false)} onDone={reloadAll} />
      <SetCategoryModal
        open={catModalOpen} count={selected.length} cats={cats}
        onClose={() => setCatModalOpen(false)}
        onOk={async (cat: string) => { setCatModalOpen(false); await bulkUpdate({ category: cat }, "设置分类"); }}
      />
      <RenameCategoryModal
        open={renameOpen} cats={cats}
        onClose={() => setRenameOpen(false)}
        onDone={(from: string, to: string) => {
          // 筛选正停在被改名的分类上时跟随新名，避免列表瞬间空掉
          setParams((p: any) => (p.category === from ? { ...p, category: to, page: 1 } : p));
          reloadAll();
        }}
      />
      <VersionsModal item={versionsItem} onClose={() => setVersionsItem(null)} onRollback={reloadAll} editable={editable} />
      <ChunksModal item={chunksItem} editable={editable} onClose={() => setChunksItem(null)} onReembedded={reloadAll} />
    </Card>
  );
}

function ChunksModal({ item, editable, onClose, onReembedded }: any) {
  const { message } = AntApp.useApp();
  const { token } = theme.useToken();
  const [data, setData] = useState<any>(null);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const loadChunks = () => {
    if (!item?.id) return;
    setLoading(true);
    knowledgeApi.itemChunks(item.id).then(setData).catch((e) => message.error(apiError(e))).finally(() => setLoading(false));
  };
  useEffect(() => {
    if (!item?.id) { setData(null); return; }
    loadChunks();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [item?.id]);
  const reembed = async () => {
    setBusy(true);
    try {
      await knowledgeApi.reembedItem(item.id);
      message.success("已排队重新嵌入，稍后刷新查看");
      onReembedded?.();
    } catch (e) {
      message.error(apiError(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal
      title={`分片详情 — ${item?.title || "（无标题）"}`} open={!!item} onCancel={onClose} width={760}
      footer={[
        // 重嵌是异步任务，弹窗数据只在打开时拉一次——刷新按钮让「重嵌→确认结果」不必关弹窗重开
        <Button key="f" icon={<ReloadOutlined />} loading={loading} onClick={() => { loadChunks(); onReembedded?.(); }}>刷新</Button>,
        ...(editable ? [<Button key="r" icon={<ThunderboltOutlined />} loading={busy} onClick={reembed}>重新嵌入此条</Button>] : []),
        <Button key="c" type="primary" onClick={onClose}>关闭</Button>,
      ]}
    >
      {data && (
        <div style={{ marginBottom: 10, fontSize: 13, color: token.colorTextSecondary }}>
          共 {data.chunk_count} 段，已向量化 {data.ready_count}/{data.chunk_count}
          {data.ready_count < data.chunk_count && <Tag color="error" style={{ marginLeft: 8 }}>有分片缺向量</Tag>}
        </div>
      )}
      <List
        loading={loading} size="small" dataSource={data?.chunks || []}
        style={{ maxHeight: 460, overflow: "auto" }}
        pagination={{ pageSize: 20, size: "small", showTotal: (t) => `共 ${t} 段` }}
        locale={{ emptyText: "暂无分片（保存内容后会自动分块）" }}
        renderItem={(c: any) => (
          <List.Item>
            <div style={{ width: "100%" }}>
              <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 2 }}>
                <span style={{ fontSize: 12, color: token.colorTextTertiary }}>#{c.chunk_index} · {c.token_count} tokens</span>
                <Tag color={c.embedded ? statusMeta(CHUNK_STATUS, c.status).color : "error"}>
                  {c.embedded ? statusMeta(CHUNK_STATUS, c.status).label : "无向量"}
                </Tag>
              </div>
              <div style={{ fontSize: 13, whiteSpace: "pre-wrap", wordBreak: "break-word" }}>{c.content}</div>
            </div>
          </List.Item>
        )}
      />
    </Modal>
  );
}

// 表单里的时效窗口校验：与后端「expires_at 必须晚于 effective_from」保持一致，前端先拦一道
const expiresAfterEffective = ({ getFieldValue }: any) => ({
  validator(_: any, value: any) {
    const eff = getFieldValue("effective_from");
    if (value && eff && !dayjs(value).isAfter(dayjs(eff)))
      return Promise.reject(new Error("过期时间必须晚于生效时间"));
    return Promise.resolve();
  },
});

function TestRetrievalModal({ open, onClose, onOpenItem }: any) {
  const { message } = AntApp.useApp();
  const [q, setQ] = useState("");
  const [results, setResults] = useState<any[]>([]);
  // fusion(RRF, ≈0.01-0.05) 与 rerank(≈0-1) 分数量纲不可比，按响应顶层 kind 标注
  const [kind, setKind] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const run = async () => {
    setLoading(true);
    try {
      const d = await knowledgeApi.testRetrieval(q);
      setResults(d.results);
      setKind(d.score_kind ?? null);
    } catch (e) {
      message.error(apiError(e));
    } finally {
      setLoading(false);
    }
  };
  const kindTip = kind === "rerank"
    ? "重排分（rerank）：范围约 0-1，越接近 1 越相关。与融合分量纲不同，不能互相比较。"
    : "RRF 融合分（fusion）：正常范围约 0.01-0.05，数值小是正常现象，不代表命中差。与重排分量纲不同，不能互相比较。";
  return (
    <Modal title="测试检索命中" open={open} onCancel={onClose} footer={null} width={680}>
      <Space.Compact style={{ width: "100%", marginBottom: 16 }}>
        <Input value={q} onChange={(e) => setQ(e.target.value)} placeholder="输入用户可能的问题" onPressEnter={run} />
        <Button type="primary" onClick={run} loading={loading}>检索</Button>
      </Space.Compact>
      <List
        dataSource={results} locale={{ emptyText: "暂无结果" }}
        renderItem={(r: any) => (
          <List.Item>
            <List.Item.Meta
              title={
                <span>
                  {r.item_id && onOpenItem
                    ? <a onClick={() => onOpenItem(r.item_id)}>{r.title || "（无标题）"}</a>
                    : (r.title || "（无标题）")}{" "}
                  <Tooltip title={kindTip}>
                    <Tag color={kind === "rerank" ? "blue" : "default"}>
                      {kind ? `${kind} ${r.score}` : `score ${r.score}`}
                    </Tag>
                  </Tooltip>
                </span>
              }
              description={r.snippet}
            />
          </List.Item>
        )}
      />
    </Modal>
  );
}

/** 触发浏览器保存一个 Blob；带 Bearer 头的下载都走这条路（裸 <a href> 带不上认证）。 */
function saveBlob(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

/** 逐行明细的兜底格式化：后端可能给字符串或 {row,title,reason} 结构。 */
function fmtRowNote(s: any): string {
  if (typeof s === "string") return s;
  const parts = [s?.row != null ? `第${s.row}行` : "", s?.title, s?.reason || s?.error].filter(Boolean);
  return parts.length ? parts.join(" · ") : JSON.stringify(s);
}

function ImportModal({ open, onClose, onDone }: any) {
  const { message } = AntApp.useApp();
  const { token } = theme.useToken();
  const [file, setFile] = useState<File | null>(null);
  const [preview, setPreview] = useState<any>(null); // dry-run 预检报告（未入库）
  const [result, setResult] = useState<any>(null); // 正式导入报告
  const [checking, setChecking] = useState(false);
  const [importing, setImporting] = useState(false);

  const reset = () => { setFile(null); setPreview(null); setResult(null); };

  const downloadTemplate = async () => {
    try {
      saveBlob((await knowledgeApi.importTemplate()) as Blob, "knowledge_template.csv");
    } catch (e) {
      message.error(apiError(e));
    }
  };

  // 第一步：选文件即 dry-run 预检，展示将创建/跳过/错误后再由用户确认
  const pick = async (f: File) => {
    setChecking(true);
    setResult(null);
    try {
      const r = await knowledgeApi.importFile(f, true);
      setFile(f);
      setPreview(r);
    } catch (e) {
      message.error(apiError(e));
    } finally {
      setChecking(false);
    }
  };

  // 第二步：确认后正式导入
  const doImport = async () => {
    if (!file) return;
    setImporting(true);
    try {
      const r = await knowledgeApi.importFile(file, false);
      setResult(r);
      setPreview(null);
      const failed = r.failed || 0;
      const warns = r.warnings?.length || 0;
      if (!r.created && (failed > 0 || r.errors?.length > 0)) message.error("导入失败，未新增条目——请查看下方明细");
      else if (failed > 0 || warns > 0) message.warning(`导入完成：新增 ${r.created} 条，但存在${failed ? `失败 ${failed} 行` : ""}${failed && warns ? "与" : ""}${warns ? "警告" : ""}，请查看明细`);
      else message.success(`导入完成：新增 ${r.created} 条${r.skipped_duplicates ? `，重复跳过 ${r.skipped_duplicates} 条` : ""}`);
      onDone();
    } catch (e) {
      message.error(apiError(e));
    } finally {
      setImporting(false);
    }
  };

  // 把后端返回的失败行拼成 CSV（带 BOM 方便 Excel），修正后可直接重传
  const downloadFailedRows = () => {
    const rows: any[] = result?.failed_rows || [];
    if (!rows.length) return;
    const esc = (v: any) => `"${String(v ?? "").replace(/"/g, '""')}"`;
    let csv: string;
    if (typeof rows[0] === "string") {
      csv = ["error", ...rows.map(esc)].join("\r\n");
    } else {
      const cols = Array.from(rows.reduce((s: Set<string>, r: any) => { Object.keys(r || {}).forEach((k) => s.add(k)); return s; }, new Set<string>()));
      csv = [cols.map(esc).join(","), ...rows.map((r: any) => cols.map((c) => esc(Array.isArray(r?.[c]) ? r[c].join(";") : r?.[c])).join(","))].join("\r\n");
    }
    saveBlob(new Blob(["\ufeff" + csv], { type: "text/csv;charset=utf-8" }), "import_failed_rows.csv");
  };

  // 预检与结果同构，共用一个报告渲染；完整消费 created/skipped/warnings/errors/encoding
  const report = (r: any, isPreview: boolean) => {
    const skipped = r.skipped_duplicates ?? r.skipped?.length ?? 0;
    const hasError = (r.errors?.length || 0) > 0 || (r.failed || 0) > 0;
    return (
      <>
        {r.warnings?.length > 0 && (
          <Alert
            style={{ marginBottom: 12 }} type="warning" showIcon message="导入警告"
            description={<ul style={{ margin: 0, paddingLeft: 18 }}>{r.warnings.map((w: string, i: number) => <li key={i}>{w}</li>)}</ul>}
          />
        )}
        <Alert
          type={!r.created && hasError ? "error" : hasError || r.warnings?.length ? "warning" : "success"}
          showIcon
          message={
            `共 ${r.total ?? 0} 行：${isPreview ? "将创建" : "新增"} ${r.created ?? 0} · 重复跳过 ${skipped} · 失败 ${r.failed ?? 0}` +
            (r.encoding ? `（编码 ${r.encoding}）` : "")
          }
          description={
            (r.errors?.length > 0 || r.skipped?.length > 0) ? (
              <div style={{ maxHeight: 220, overflow: "auto" }}>
                {r.errors?.length > 0 && (
                  <ul style={{ margin: "4px 0", paddingLeft: 18 }}>{r.errors.map((e: any, i: number) => <li key={`e${i}`}>{fmtRowNote(e)}</li>)}</ul>
                )}
                {r.skipped?.length > 0 && (
                  <div style={{ color: token.colorTextSecondary, fontSize: 12 }}>
                    跳过明细：
                    <ul style={{ margin: "4px 0", paddingLeft: 18 }}>{r.skipped.map((s: any, i: number) => <li key={`s${i}`}>{fmtRowNote(s)}</li>)}</ul>
                  </div>
                )}
              </div>
            ) : null
          }
        />
      </>
    );
  };

  return (
    <Modal title="批量导入知识" open={open} onCancel={() => { reset(); onClose(); }} footer={null} width={560}>
      <Alert
        style={{ marginBottom: 16 }} type="info" showIcon
        message="支持 CSV / JSON。CSV 列：title,content,category,tags（标签用分号或逗号分隔）。选择文件后先预检、确认后才写入，向量在导入后异步生成。"
      />
      <Space style={{ marginBottom: 16 }} wrap>
        <Button icon={<DownloadOutlined />} onClick={downloadTemplate}>下载模板</Button>
        <Upload accept=".csv,.json" showUploadList={false} customRequest={({ file: f }: any) => pick(f)}>
          <Button icon={<UploadOutlined />} loading={checking}>{file ? "重新选择文件" : "选择文件（先预检，不入库）"}</Button>
        </Upload>
        {preview && (
          <Button type="primary" icon={<UploadOutlined />} loading={importing} disabled={!(preview.created > 0)} onClick={doImport}>
            确认导入 {preview.created ?? 0} 条
          </Button>
        )}
      </Space>
      {file && <div style={{ marginBottom: 12, fontSize: 12, color: token.colorTextSecondary }}>文件：{file.name}</div>}
      {preview && (
        <>
          <div style={{ marginBottom: 8, fontSize: 13 }}>预检结果（尚未写入知识库）：</div>
          {report(preview, true)}
          {!(preview.created > 0) && (
            <div style={{ marginTop: 8, fontSize: 12, color: token.colorTextSecondary }}>没有可新增的行：请按上方明细修正文件后重新选择。</div>
          )}
        </>
      )}
      {result && (
        <>
          {report(result, false)}
          {result.failed_rows?.length > 0 && (
            <Button style={{ marginTop: 12 }} icon={<DownloadOutlined />} onClick={downloadFailedRows}>
              下载失败行 CSV（{result.failed_rows.length} 行，修正后可直接重新导入）
            </Button>
          )}
        </>
      )}
    </Modal>
  );
}

function SetCategoryModal({ open, count, cats, onClose, onOk }: any) {
  const { token } = theme.useToken();
  const [value, setValue] = useState("");
  useEffect(() => { if (open) setValue(""); }, [open]);
  return (
    <Modal
      title={`为选中的 ${count} 条设置分类`} open={open} onCancel={onClose}
      okText="应用" okButtonProps={{ disabled: !value.trim() }} onOk={() => onOk(value.trim())}
    >
      <AutoComplete
        style={{ width: "100%" }} value={value} onChange={setValue}
        options={cats.map((c: any) => ({ value: c.name, label: `${c.name} (${c.count})` }))}
        placeholder="选择已有分类，或输入新分类名"
        filterOption={(input, option) => String(option?.value ?? "").toLowerCase().includes(input.toLowerCase())}
      />
      <div style={{ marginTop: 8, fontSize: 12, color: token.colorTextTertiary }}>输入已有分类名即归入该分类；输入新名称将创建新分类。</div>
    </Modal>
  );
}

function RenameCategoryModal({ open, cats, onClose, onDone }: any) {
  const { message } = AntApp.useApp();
  const [from, setFrom] = useState<string | undefined>();
  const [to, setTo] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => { if (open) { setFrom(undefined); setTo(""); } }, [open]);
  const target = to.trim();
  const merging = !!target && target !== from && cats.some((c: any) => c.name === target);
  const submit = async () => {
    if (!from || !target || target === from) return;
    setBusy(true);
    try {
      const r = await knowledgeApi.renameCategory(from, target);
      message.success(`已重命名：${r.updated ?? 0} 条条目从「${from}」移入「${target}」`);
      onClose();
      onDone(from, target);
    } catch (e) {
      message.error(apiError(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal
      title="重命名分类" open={open} onCancel={onClose} okText="重命名" confirmLoading={busy}
      okButtonProps={{ disabled: !from || !target || target === from }} onOk={submit}
    >
      <Space direction="vertical" style={{ width: "100%" }} size="middle">
        <Select
          style={{ width: "100%" }} placeholder="选择要重命名的分类" value={from} onChange={setFrom}
          options={cats.map((c: any) => ({ value: c.name, label: `${c.name} (${c.count})` }))}
        />
        <AutoComplete
          style={{ width: "100%" }} value={to} onChange={setTo}
          options={cats.filter((c: any) => c.name !== from).map((c: any) => ({ value: c.name }))}
          placeholder="新分类名（填已有分类名即合并进去）"
        />
        {merging && <Alert type="warning" showIcon message={`「${target}」已存在：两个分类将合并，合并后无法自动拆分`} />}
      </Space>
    </Modal>
  );
}

type DiffLine = { type: "same" | "del" | "add"; text: string };

/** 轻量行级 diff（LCS）：版本回滚对比够用，不为此引第三方库。超长文本返回 null 跳过。 */
function diffLines(oldText: string, newText: string): DiffLine[] | null {
  const a = oldText.split("\n");
  const b = newText.split("\n");
  const n = a.length, m = b.length;
  if (n * m > 250_000) return null; // O(n·m) 的 DP，超长文档跳过对比避免卡顿
  const dp: number[][] = Array.from({ length: n + 1 }, () => new Array(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i--)
    for (let j = m - 1; j >= 0; j--)
      dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
  const out: DiffLine[] = [];
  let i = 0, j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) { out.push({ type: "same", text: a[i] }); i++; j++; }
    else if (dp[i + 1][j] >= dp[i][j + 1]) { out.push({ type: "del", text: a[i] }); i++; }
    else { out.push({ type: "add", text: b[j] }); j++; }
  }
  while (i < n) out.push({ type: "del", text: a[i++] });
  while (j < m) out.push({ type: "add", text: b[j++] });
  return out;
}

function DiffView({ oldText, newText }: { oldText: string; newText: string }) {
  const { token } = theme.useToken();
  if (oldText === newText) return <Alert type="success" showIcon message="该版本正文与当前内容一致（差异可能只在标题/分类/标签）" />;
  const lines = diffLines(oldText, newText);
  if (!lines) return <Alert type="warning" showIcon message="内容过长，已跳过逐行对比；确认回滚仍会完整恢复该版本" />;
  return (
    <>
      <div style={{ fontSize: 12, color: token.colorTextSecondary, marginBottom: 6 }}>
        <span style={{ background: token.colorErrorBg, padding: "0 4px" }}>红色</span> 为当前内容中将被移除的行；
        <span style={{ background: token.colorSuccessBg, padding: "0 4px" }}>绿色</span> 为回滚后恢复的行
      </div>
      <div style={{ maxHeight: 360, overflow: "auto", border: `1px solid ${token.colorBorderSecondary}`, borderRadius: 6, fontSize: 13, fontFamily: "monospace", padding: "4px 0" }}>
        {lines.map((l, i) => (
          <div
            key={i}
            style={{
              padding: "0 8px", whiteSpace: "pre-wrap", wordBreak: "break-word",
              background: l.type === "del" ? token.colorErrorBg : l.type === "add" ? token.colorSuccessBg : undefined,
              color: l.type === "same" ? token.colorTextSecondary : undefined,
            }}
          >
            {(l.type === "del" ? "- " : l.type === "add" ? "+ " : "  ") + l.text}
          </div>
        ))}
      </div>
    </>
  );
}

function VersionsModal({ item, onClose, onRollback, editable }: any) {
  const { message } = AntApp.useApp();
  const { token } = theme.useToken();
  const [versions, setVersions] = useState<any[]>([]);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [rollbackTarget, setRollbackTarget] = useState<any>(null); // 待确认回滚的版本
  const [current, setCurrent] = useState<any>(null); // 对比用的当前条目全文
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    setExpandedId(null);
    // full=true：全文预览与逐行对比需要完整正文（列表接口默认截断 200 字）
    if (item?.id) knowledgeApi.versions(item.id, true).then((d) => setVersions(d.versions)).catch((e) => message.error(apiError(e)));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [item]);

  // 回滚前重新拉当前内容做对比：列表行数据可能已过期
  const prepareRollback = async (v: any) => {
    try {
      const full = await knowledgeApi.get(item.id);
      setCurrent(full);
      setRollbackTarget(v);
    } catch (e) {
      message.error(apiError(e));
    }
  };
  const doRollback = async () => {
    setBusy(true);
    try {
      await knowledgeApi.rollback(item.id, rollbackTarget.id);
      message.success("已回滚；回滚前的内容已自动存为新版本，误操作可再滚回");
      setRollbackTarget(null);
      onRollback();
      onClose();
    } catch (e) {
      message.error(apiError(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal title="版本历史" open={!!item} onCancel={onClose} footer={null} width={640}>
      <List
        dataSource={versions} locale={{ emptyText: "暂无历史版本" }}
        renderItem={(v: any) => (
          <List.Item
            actions={[
              <a key="expand" onClick={() => setExpandedId(expandedId === v.id ? null : v.id)}>
                {expandedId === v.id ? "收起" : "查看全文"}
              </a>,
              ...(editable ? [<a key="rollback" onClick={() => prepareRollback(v)}>回滚</a>] : []),
            ]}
          >
            <List.Item.Meta
              title={
                <span>
                  v{v.version} <Tag>{v.editor_email || "系统"}</Tag>
                  <span style={{ color: token.colorTextSecondary, fontSize: 12, marginRight: 8 }}>{fmtTime(v.created_at)}</span>
                  <span style={{ color: token.colorTextTertiary, fontSize: 12 }}>{v.change_note}</span>
                </span>
              }
              description={
                <div>
                  <b>{v.title}</b>
                  {expandedId === v.id ? (
                    <>
                      <div style={{ whiteSpace: "pre-wrap", wordBreak: "break-word", fontSize: 13, marginTop: 4, maxHeight: 300, overflow: "auto", border: `1px solid ${token.colorBorderSecondary}`, borderRadius: 6, padding: "6px 10px" }}>
                        {v.content}
                      </div>
                      {v.content_truncated && (
                        <div style={{ fontSize: 12, color: token.colorWarning, marginTop: 4 }}>
                          该版本正文较长，接口仅返回前 200 字预览；回滚会完整恢复全文
                        </div>
                      )}
                    </>
                  ) : (
                    <div style={{ color: token.colorTextSecondary, fontSize: 12 }}>
                      {v.content?.slice(0, 120)}{(v.content_truncated || (v.content?.length ?? 0) > 120) ? "…" : ""}
                    </div>
                  )}
                </div>
              }
            />
          </List.Item>
        )}
      />
      <Modal
        title={rollbackTarget ? `回滚确认：v${current?.version ?? "?"}（当前）→ v${rollbackTarget.version}` : "回滚确认"}
        open={!!rollbackTarget} onCancel={() => setRollbackTarget(null)}
        okText="确认回滚" onOk={doRollback} confirmLoading={busy} width={680}
      >
        <Alert
          style={{ marginBottom: 12 }} type="info" showIcon
          message="回滚会把该版本内容恢复为当前内容；回滚前的当前内容会自动保存为新版本，误操作可再滚回。"
        />
        {rollbackTarget?.content_truncated ? (
          <Alert
            type="warning" showIcon
            message="该版本正文较长，接口仅返回前 200 字，无法逐行对比；确认回滚仍会完整恢复该版本全文。"
            description={<div style={{ whiteSpace: "pre-wrap", fontSize: 12, marginTop: 4 }}>{rollbackTarget.content}…</div>}
          />
        ) : (
          <DiffView oldText={current?.content || ""} newText={rollbackTarget?.content || ""} />
        )}
      </Modal>
    </Modal>
  );
}

function ReviewTab({ editable, onCount, onOpenItem }: any) {
  const { message } = AntApp.useApp();
  const { token } = theme.useToken();
  const [items, setItems] = useState<any[]>([]);
  const [cats, setCats] = useState<any[]>([]);
  const [editCand, setEditCand] = useState<any>(null); // 「编辑后通过」表单里的候选
  // 原样通过撞查重时带给表单的 409 提示：{ text, itemId }（后端 detail 为结构化对象）
  const [dupNotice, setDupNotice] = useState<any>(null);
  // 加载同时上报徽标数，父组件不再按 tab 重复请求
  const load = () =>
    knowledgeApi.reviewList("pending")
      .then((d) => { setItems(d.candidates); onCount?.(d.candidates.length); })
      .catch((e) => message.error(apiError(e)));
  useEffect(() => {
    load();
    knowledgeApi.categories().then((d) => setCats(d.categories)).catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const approvePlain = async (c: any) => {
    try {
      const r = await knowledgeApi.reviewApprove(c.id, {});
      message.success(
        <span>
          已通过并加入知识库
          {r.auto_rejected_duplicates ? `（自动拒绝了 ${r.auto_rejected_duplicates} 条重复候选）` : ""}{" "}
          {r.item_id && <a onClick={() => onOpenItem?.(r.item_id)}>查看条目</a>}
        </span>
      );
    } catch (e: any) {
      const detail = apiError(e);
      const raw = e?.response?.data?.detail;
      const dupD = raw && typeof raw === "object" && !Array.isArray(raw) ? raw : null;
      if (e?.response?.status === 409 && (dupD?.item_id || detail.includes("重复"))) {
        // 撞查重：直接打开「编辑后通过」表单让审核员就地改标题/内容再提交
        setDupNotice({ text: dupD?.message || detail, itemId: dupD?.item_id });
        setEditCand(c);
      } else message.error(detail);
    }
    load();
  };

  return (
    <Card>
      <Alert style={{ marginBottom: 16 }} type="info" showIcon message="以下是从对话中自动沉淀的候选知识，审核通过后将进入知识库；可在通过前修正标题/内容或补充分类、标签与时效。" />
      <List
        dataSource={items} locale={{ emptyText: "暂无待审核内容" }}
        pagination={{ pageSize: 10, size: "small", showTotal: (t) => `共 ${t} 条` }}
        renderItem={(c: any) => (
          <List.Item
            actions={editable ? [
              <Popconfirm key="ok" title="通过并加入知识库？" onConfirm={() => approvePlain(c)}>
                <a style={{ color: "#10b981" }}>通过</a>
              </Popconfirm>,
              <a key="edit" onClick={() => { setDupNotice(null); setEditCand(c); }}>编辑后通过</a>,
              <Popconfirm key="no" title="拒绝该候选？" onConfirm={async () => { try { await knowledgeApi.reviewReject(c.id); message.success("已拒绝"); } catch (e) { message.error(apiError(e)); } load(); }}>
                <a style={{ color: "#ef4444" }}>拒绝</a>
              </Popconfirm>,
            ] : []}
          >
            <List.Item.Meta
              title={
                <span>
                  {c.suggested_title || "（无标题）"}
                  {c.suggested_category && <Tag style={{ marginLeft: 8 }}>{c.suggested_category}</Tag>}
                </span>
              }
              description={
                <div>
                  <div style={{ marginBottom: 4 }}>{c.suggested_content}</div>
                  <div style={{ color: token.colorTextTertiary, fontSize: 12, whiteSpace: "pre-wrap" }}>原始片段：{c.raw_excerpt?.slice(0, 160)}…</div>
                  {c.source_session_id && (
                    <div style={{ fontSize: 12, marginTop: 2 }}>
                      <Link to={`/conversations?session=${c.source_session_id}`}>查看来源对话</Link>
                    </div>
                  )}
                </div>
              }
            />
          </List.Item>
        )}
      />
      <EditApproveModal
        cand={editCand} cats={cats} dupNotice={dupNotice}
        onClose={() => { setEditCand(null); setDupNotice(null); }}
        onDone={load}
        onOpenItem={onOpenItem}
      />
    </Card>
  );
}

function EditApproveModal({ cand, cats, dupNotice, onClose, onDone, onOpenItem }: any) {
  const { message } = AntApp.useApp();
  const [form] = Form.useForm();
  const [saving, setSaving] = useState(false);
  const [dup, setDup] = useState<any>(null);
  useEffect(() => {
    if (!cand) return;
    setDup(dupNotice || null);
    form.resetFields();
    form.setFieldsValue({
      title: cand.suggested_title,
      content: cand.suggested_content,
      category: cand.suggested_category,
      tags: [],
      status: "published",
      effective_from: null,
      expires_at: null,
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cand]);

  // 后端 409 detail 为结构化 {message, item_id, title}；老格式纯文本时用 UUID 正则兜底
  const dupItemId =
    dup?.itemId ||
    (dup?.text || "").match(/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i)?.[0];

  const submit = async () => {
    let v: any;
    try {
      v = await form.validateFields();
    } catch {
      return;
    }
    setSaving(true);
    try {
      const body = {
        ...v,
        effective_from: v.effective_from ? v.effective_from.toISOString() : null,
        expires_at: v.expires_at ? v.expires_at.toISOString() : null,
      };
      const r = await knowledgeApi.reviewApprove(cand.id, body);
      message.success(
        <span>
          已通过并加入知识库{" "}
          {r.item_id && <a onClick={() => onOpenItem?.(r.item_id)}>查看条目</a>}
        </span>
      );
      onDone();
      onClose();
    } catch (e: any) {
      const detail = apiError(e);
      const raw = e?.response?.data?.detail;
      const dupD = raw && typeof raw === "object" && !Array.isArray(raw) ? raw : null;
      if (e?.response?.status === 409 && (dupD?.item_id || detail.includes("重复"))) {
        setDup({ text: dupD?.message || detail, itemId: dupD?.item_id }); // 留在表单里改后重试
      } else if (e?.response?.status === 409) {
        // 已被其他管理员处理：刷新列表即可
        message.warning(detail);
        onDone();
        onClose();
      } else message.error(detail);
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal
      title="编辑后通过" open={!!cand} onCancel={onClose} width={640}
      okText="通过并入库" onOk={submit} confirmLoading={saving} maskClosable={false}
    >
      {dup && (
        <Alert
          style={{ marginBottom: 12 }} type="warning" showIcon message="知识库已有相似条目"
          description={
            <span>
              {dup?.text}
              {dupItemId && <>{" "}<a onClick={() => { onClose(); onOpenItem?.(dupItemId); }}>打开该条目</a></>}
            </span>
          }
        />
      )}
      <Form form={form} layout="vertical">
        <Form.Item name="title" label="标题"><Input placeholder="简短标题" maxLength={500} /></Form.Item>
        <Form.Item name="content" label="内容" rules={[{ required: true, message: "请输入内容" }]}>
          <Input.TextArea rows={8} />
        </Form.Item>
        <Space style={{ width: "100%" }} size="large">
          <Form.Item name="category" label="分类">
            <AutoComplete
              style={{ width: 220 }}
              options={cats.map((c: any) => ({ value: c.name, label: `${c.name} (${c.count})` }))}
              placeholder="选择已有分类，或输入新分类名"
              filterOption={(input, option) => String(option?.value ?? "").toLowerCase().includes(input.toLowerCase())}
            />
          </Form.Item>
          <Form.Item name="status" label="状态" tooltip="选「草稿」则先不参与检索，之后可在条目列表发布">
            <Select style={{ width: 140 }} options={[{ value: "published", label: "已发布" }, { value: "draft", label: "草稿" }]} />
          </Form.Item>
        </Space>
        <Form.Item name="tags" label="标签"><Select mode="tags" placeholder="回车添加标签" /></Form.Item>
        <Space size="large">
          <Form.Item name="effective_from" label="生效时间" tooltip="留空立即生效；未到生效时间的条目不参与检索">
            <DatePicker showTime allowClear style={{ width: 200 }} placeholder="留空立即生效" />
          </Form.Item>
          <Form.Item
            name="expires_at" label="过期时间" dependencies={["effective_from"]}
            tooltip="留空长期有效；过期后条目自动停止参与检索"
            rules={[expiresAfterEffective]}
          >
            <DatePicker showTime allowClear style={{ width: 200 }} placeholder="留空长期有效" />
          </Form.Item>
        </Space>
      </Form>
    </Modal>
  );
}
