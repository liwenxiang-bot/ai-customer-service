import { useEffect, useRef, useState } from "react";
import {
  Button, Card, Drawer, Form, Input, Select, Space, Table, Tag, Tabs, Modal,
  Upload, Tooltip, List, Popconfirm, App as AntApp, Badge, Alert, Progress, theme,
} from "antd";
import {
  PlusOutlined, SearchOutlined, UploadOutlined, ExperimentOutlined,
  HistoryOutlined, DownloadOutlined, PartitionOutlined, ThunderboltOutlined,
  ReloadOutlined,
} from "@ant-design/icons";
import ReactMarkdown from "react-markdown";
import { knowledgeApi } from "../api";
import { apiError } from "../api/client";
import { useAuth, canEdit } from "../auth";
import { useDebounce } from "../hooks/useDebounce";
import { usePolling } from "../hooks/usePolling";
import { fmtTime } from "../utils/time";
import { KNOWLEDGE_STATUS, KNOWLEDGE_SOURCE, CHUNK_STATUS, statusMeta } from "../utils/status";

export function Knowledge() {
  const { user } = useAuth();
  const editable = canEdit(user?.role);
  const { message } = AntApp.useApp();
  const [tab, setTab] = useState("items");
  const [reviewCount, setReviewCount] = useState(0);

  // 挂载时拉一次徽标数；之后由 ReviewTab 加载/审核时通过 onCount 上报，避免切 Tab 重复请求
  useEffect(() => {
    knowledgeApi.reviewList("pending").then((d) => setReviewCount(d.candidates.length)).catch(() => {});
  }, []);

  return (
    <div>
      <div className="acs-page-title">知识库管理</div>
      <div className="acs-page-sub">维护 AI 回答所依据的资料；保存后自动向量化，支持版本回滚、批量导入与对话自动沉淀审核。</div>
      <Tabs
        activeKey={tab}
        onChange={setTab}
        items={[
          { key: "items", label: "知识条目", children: <ItemsTab editable={editable} /> },
          {
            key: "review",
            label: <Badge count={reviewCount} size="small" offset={[10, 0]}>待审核</Badge>,
            children: <ReviewTab editable={editable} onCount={setReviewCount} />,
          },
        ]}
      />
    </div>
  );
}

function ItemsTab({ editable }: { editable: boolean }) {
  const { message } = AntApp.useApp();
  const { token } = theme.useToken();
  const [data, setData] = useState<any>({ items: [], total: 0 });
  const [loading, setLoading] = useState(false);
  const [params, setParams] = useState<any>({ q: "", status: "", category: "", tag: "", source: "", page: 1, page_size: 10 });
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
  const [search, setSearch] = useState("");
  const dq = useDebounce(search, 400);
  useEffect(() => { setParams((p: any) => ({ ...p, q: dq, page: 1 })); }, [dq]);

  const loadMeta = () => {
    knowledgeApi.categories().then((d) => setCats(d.categories)).catch(() => {});
    knowledgeApi.tags().then((d) => setAllTags(d.tags)).catch(() => {});
    knowledgeApi.embeddingStatus().then(setVec).catch(() => {});
  };
  // 递增序号防过期响应：快速切换筛选/翻页时，只有最新一次请求的结果才落地。
  const loadSeq = useRef(0);
  const load = () => {
    const seq = ++loadSeq.current;
    setLoading(true);
    knowledgeApi.list(params)
      .then((d) => { if (seq === loadSeq.current) setData(d); })
      .catch((e) => { if (seq === loadSeq.current) message.error(apiError(e)); })
      .finally(() => { if (seq === loadSeq.current) setLoading(false); });
  };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { load(); }, [params.q, params.status, params.category, params.tag, params.source, params.page, params.page_size]);
  // 分类/标签/向量化进度变化不频繁：只在挂载和写操作后刷新，翻页/搜索不重复拉
  useEffect(loadMeta, []);
  // 后台嵌入进行中时轮询进度条，完成即自动停止；空闲零请求
  const embedding = !!vec && (vec.ready_chunks < vec.total_chunks || vec.rebuild?.status === "running");
  usePolling(() => { knowledgeApi.embeddingStatus().then(setVec).catch(() => {}); }, 5000, { enabled: embedding });
  const reloadAll = () => { load(); loadMeta(); };

  const bulkDelete = async () => {
    try {
      const results = await Promise.allSettled(selected.map((id) => knowledgeApi.remove(String(id))));
      const failedIds = selected.filter((_, i) => results[i].status === "rejected");
      if (failedIds.length) message.warning(`删除成功 ${selected.length - failedIds.length} 条，失败 ${failedIds.length} 条，失败条目已保留选中`);
      else message.success(`已删除 ${selected.length} 条`);
      setSelected(failedIds);
    } finally {
      reloadAll();
    }
  };
  const bulkStatus = async (status: string) => {
    try {
      const results = await Promise.allSettled(selected.map((id) => knowledgeApi.update(String(id), { status })));
      const failedIds = selected.filter((_, i) => results[i].status === "rejected");
      if (failedIds.length) message.warning(`更新成功 ${selected.length - failedIds.length} 条，失败 ${failedIds.length} 条，失败条目已保留选中`);
      else message.success(`已更新 ${selected.length} 条`);
      setSelected(failedIds);
    } finally {
      reloadAll();
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
        form.setFieldsValue(full);
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

  const save = async () => {
    const v = await form.validateFields();
    try {
      if (drawer.id) await knowledgeApi.update(drawer.id, v);
      else await knowledgeApi.create(v);
      message.success("已保存，向量将后台异步生成");
      setDrawer(null);
      reloadAll();
    } catch (e) {
      message.error(apiError(e));
    }
  };

  const columns = [
    { title: "标题", dataIndex: "title", render: (t: string, r: any) => t || <span style={{ color: token.colorTextTertiary }}>{r.content.slice(0, 30)}…</span> },
    { title: "分类", dataIndex: "category", width: 120, render: (c: string) => c ? <Tag>{c}</Tag> : "—" },
    { title: "状态", dataIndex: "status", width: 90, render: (s: string) => { const m = statusMeta(KNOWLEDGE_STATUS, s); return <Tag color={m.color}>{m.label}</Tag>; } },
    { title: "来源", dataIndex: "source", width: 100, render: (s: string) => { const m = statusMeta(KNOWLEDGE_SOURCE, s); return <Tag color={m.color}>{m.label}</Tag>; } },
    { title: "版本", dataIndex: "version", width: 70 },
    {
      title: "向量", width: 90, render: (_: any, r: any) => {
        const total = r.chunk_count ?? 0, ready = r.ready_count ?? 0;
        if (!total) return <Tag>未分块</Tag>;
        return (
          <Tooltip title={ready >= total ? "向量已就绪" : "部分分片缺向量，检索可能漏命中——点「分片」查看或重新嵌入"}>
            <Tag color={ready >= total ? "green" : "error"}>{ready}/{total}</Tag>
          </Tooltip>
        );
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
            <Popconfirm title="确认删除？删除后不可恢复" onConfirm={async () => { try { await knowledgeApi.remove(r.id); message.success("已删除"); } catch (e) { message.error(apiError(e)); } reloadAll(); }}>
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
            status={vec.rebuild?.status === "running" ? "active" : "success"}
          />
          {vec.rebuild?.status === "running" && <Tag color="processing">重建中 {Math.round((vec.rebuild.progress || 0) * 100)}%</Tag>}
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
        <Button icon={<ExperimentOutlined />} onClick={() => setTestOpen(true)}>测试检索</Button>
        {editable && <Button icon={<UploadOutlined />} onClick={() => setImportOpen(true)}>批量导入</Button>}
        {editable && (
          <Tooltip title="为所有缺向量/未分块的条目重新生成向量">
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
          <Popconfirm title={`删除选中的 ${selected.length} 条？删除后不可恢复`} onConfirm={bulkDelete}>
            <Button size="small" danger>删除</Button>
          </Popconfirm>
          <a onClick={() => setSelected([])} style={{ fontSize: 12 }}>取消选择</a>
        </div>
      )}

      <Table
        rowKey="id" loading={loading} columns={columns as any} dataSource={data.items} size="small"
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
        onClose={() => setDrawer(null)}
        extra={editable && <Button type="primary" onClick={save}>保存</Button>}
      >
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
            <Form.Item name="category" label="分类" style={{ flex: 1 }}><Input placeholder="如 售后" /></Form.Item>
            <Form.Item name="status" label="状态">
              <Select style={{ width: 140 }} options={Object.entries(KNOWLEDGE_STATUS).map(([value, m]) => ({ value, label: m.label }))} />
            </Form.Item>
          </Space>
          <Form.Item name="tags" label="标签"><Select mode="tags" placeholder="回车添加标签" /></Form.Item>
          {drawer?.id && <Alert type="info" showIcon message={`当前版本 v${drawer.version} · 已分块 ${drawer.chunk_count ?? "?"} 段`} />}
        </Form>
      </Drawer>

      <TestRetrievalModal open={testOpen} onClose={() => setTestOpen(false)} />
      <ImportModal open={importOpen} onClose={() => setImportOpen(false)} onDone={reloadAll} />
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
  useEffect(() => {
    if (!item?.id) { setData(null); return; }
    setLoading(true);
    knowledgeApi.itemChunks(item.id).then(setData).catch((e) => message.error(apiError(e))).finally(() => setLoading(false));
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
      footer={editable ? [
        <Button key="r" icon={<ThunderboltOutlined />} loading={busy} onClick={reembed}>重新嵌入此条</Button>,
        <Button key="c" type="primary" onClick={onClose}>关闭</Button>,
      ] : null}
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

function TestRetrievalModal({ open, onClose }: any) {
  const { message } = AntApp.useApp();
  const [q, setQ] = useState("");
  const [results, setResults] = useState<any[]>([]);
  const [loading, setLoading] = useState(false);
  const run = async () => {
    setLoading(true);
    try {
      const d = await knowledgeApi.testRetrieval(q);
      setResults(d.results);
    } catch (e) {
      message.error(apiError(e));
    } finally {
      setLoading(false);
    }
  };
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
              title={<span>{r.title || "（无标题）"} <Tag color="blue">score {r.score}</Tag></span>}
              description={r.snippet}
            />
          </List.Item>
        )}
      />
    </Modal>
  );
}

function ImportModal({ open, onClose, onDone }: any) {
  const { message } = AntApp.useApp();
  const [result, setResult] = useState<any>(null);
  const [loading, setLoading] = useState(false);
  return (
    <Modal title="批量导入知识" open={open} onCancel={() => { setResult(null); onClose(); }} footer={null}>
      <Alert style={{ marginBottom: 16 }} type="info" showIcon
        message="支持 CSV / JSON。CSV 列：title,content,category,tags（标签用分号或逗号分隔）。导入后向量异步生成。" />
      <Space style={{ marginBottom: 16 }}>
        <Button icon={<DownloadOutlined />} href="/api/admin/knowledge/import/template" target="_blank">下载模板</Button>
        <Upload
          accept=".csv,.json" showUploadList={false} customRequest={async ({ file }: any) => {
            setLoading(true);
            try {
              const r = await knowledgeApi.importFile(file);
              setResult(r);
              message.success(`导入完成：成功 ${r.created} 条`);
              onDone();
            } catch (e) { message.error(apiError(e)); }
            finally { setLoading(false); }
          }}
        >
          <Button type="primary" icon={<UploadOutlined />} loading={loading}>选择文件上传</Button>
        </Upload>
      </Space>
      {result && (
        <Alert
          type={result.failed ? "warning" : "success"}
          message={`共 ${result.total} 行，成功 ${result.created}，失败 ${result.failed}`}
          description={result.errors?.length ? <ul>{result.errors.map((e: string, i: number) => <li key={i}>{e}</li>)}</ul> : null}
        />
      )}
    </Modal>
  );
}

function VersionsModal({ item, onClose, onRollback, editable }: any) {
  const { message } = AntApp.useApp();
  const { token } = theme.useToken();
  const [versions, setVersions] = useState<any[]>([]);
  useEffect(() => {
    if (item?.id) knowledgeApi.versions(item.id).then((d) => setVersions(d.versions)).catch((e) => message.error(apiError(e)));
  }, [item]);
  return (
    <Modal title="版本历史" open={!!item} onCancel={onClose} footer={null} width={640}>
      <List
        dataSource={versions} locale={{ emptyText: "暂无历史版本" }}
        renderItem={(v: any) => (
          <List.Item
            actions={editable ? [
              <Popconfirm title={`回滚到 v${v.version}？`} onConfirm={async () => {
                try {
                  await knowledgeApi.rollback(item.id, v.id); message.success("已回滚"); onRollback(); onClose();
                } catch (e) { message.error(apiError(e)); }
              }}><a>回滚</a></Popconfirm>,
            ] : []}
          >
            <List.Item.Meta
              title={<span>v{v.version} <Tag>{v.editor_email || "系统"}</Tag> <span style={{ color: token.colorTextTertiary, fontSize: 12 }}>{v.change_note}</span></span>}
              description={<div><b>{v.title}</b><div style={{ color: token.colorTextSecondary, fontSize: 12 }}>{v.content?.slice(0, 120)}…</div></div>}
            />
          </List.Item>
        )}
      />
    </Modal>
  );
}

function ReviewTab({ editable, onCount }: any) {
  const { message } = AntApp.useApp();
  const { token } = theme.useToken();
  const [items, setItems] = useState<any[]>([]);
  // 加载同时上报徽标数，父组件不再按 tab 重复请求
  const load = () =>
    knowledgeApi.reviewList("pending")
      .then((d) => { setItems(d.candidates); onCount?.(d.candidates.length); })
      .catch((e) => message.error(apiError(e)));
  useEffect(() => { load(); }, []);

  return (
    <Card>
      <Alert style={{ marginBottom: 16 }} type="info" showIcon message="以下是从对话中自动沉淀的候选知识，审核通过后将进入知识库。" />
      <List
        dataSource={items} locale={{ emptyText: "暂无待审核内容" }}
        pagination={{ pageSize: 10, size: "small", showTotal: (t) => `共 ${t} 条` }}
        renderItem={(c: any) => (
          <List.Item
            actions={editable ? [
              <Popconfirm title="通过并加入知识库？" onConfirm={async () => { try { await knowledgeApi.reviewApprove(c.id, {}); message.success("已通过"); } catch (e) { message.error(apiError(e)); } load(); }}>
                <a style={{ color: "#10b981" }}>通过</a>
              </Popconfirm>,
              <Popconfirm title="拒绝该候选？" onConfirm={async () => { try { await knowledgeApi.reviewReject(c.id); message.success("已拒绝"); } catch (e) { message.error(apiError(e)); } load(); }}>
                <a style={{ color: "#ef4444" }}>拒绝</a>
              </Popconfirm>,
            ] : []}
          >
            <List.Item.Meta
              title={c.suggested_title || "（无标题）"}
              description={
                <div>
                  <div style={{ marginBottom: 4 }}>{c.suggested_content}</div>
                  <div style={{ color: token.colorTextTertiary, fontSize: 12, whiteSpace: "pre-wrap" }}>原始片段：{c.raw_excerpt?.slice(0, 160)}…</div>
                </div>
              }
            />
          </List.Item>
        )}
      />
    </Card>
  );
}
