import { useEffect, useState } from "react";
import {
  Button, Card, Drawer, Form, Input, Select, Space, Table, Tag, Tabs, Modal,
  Upload, Tooltip, List, Popconfirm, App as AntApp, Badge, Alert,
} from "antd";
import {
  PlusOutlined, SearchOutlined, UploadOutlined, ExperimentOutlined,
  HistoryOutlined, DownloadOutlined,
} from "@ant-design/icons";
import { knowledgeApi } from "../api";
import { apiError } from "../api/client";
import { useAuth, canEdit } from "../auth";

const STATUS_COLORS: any = { published: "green", draft: "orange", archived: "default" };
const SOURCE_LABELS: any = { manual: "手动", import: "导入", auto_distilled: "自动沉淀" };

export function Knowledge() {
  const { user } = useAuth();
  const editable = canEdit(user?.role);
  const { message } = AntApp.useApp();
  const [tab, setTab] = useState("items");
  const [reviewCount, setReviewCount] = useState(0);

  useEffect(() => {
    knowledgeApi.reviewList("pending").then((d) => setReviewCount(d.candidates.length)).catch(() => {});
  }, [tab]);

  return (
    <div>
      <div className="acs-page-title">知识库管理</div>
      <Tabs
        activeKey={tab}
        onChange={setTab}
        items={[
          { key: "items", label: "知识条目", children: <ItemsTab editable={editable} /> },
          {
            key: "review",
            label: <Badge count={reviewCount} size="small" offset={[10, 0]}>待审核</Badge>,
            children: <ReviewTab editable={editable} onChange={() => knowledgeApi.reviewList("pending").then((d) => setReviewCount(d.candidates.length))} />,
          },
        ]}
      />
    </div>
  );
}

function ItemsTab({ editable }: { editable: boolean }) {
  const { message } = AntApp.useApp();
  const [data, setData] = useState<any>({ items: [], total: 0 });
  const [loading, setLoading] = useState(false);
  const [params, setParams] = useState<any>({ q: "", status: "", page: 1, page_size: 10 });
  const [drawer, setDrawer] = useState<any>(null); // editing item or {} for new
  const [form] = Form.useForm();
  const [testOpen, setTestOpen] = useState(false);
  const [importOpen, setImportOpen] = useState(false);
  const [versionsItem, setVersionsItem] = useState<any>(null);

  const load = () => {
    setLoading(true);
    knowledgeApi.list(params).then(setData).finally(() => setLoading(false));
  };
  useEffect(load, [JSON.stringify(params)]);

  const openEdit = async (item: any) => {
    if (item?.id) {
      const full = await knowledgeApi.get(item.id);
      form.setFieldsValue(full);
      setDrawer(full);
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
      load();
    } catch (e) {
      message.error(apiError(e));
    }
  };

  const columns = [
    { title: "标题", dataIndex: "title", render: (t: string, r: any) => t || <span style={{ color: "#aaa" }}>{r.content.slice(0, 30)}…</span> },
    { title: "分类", dataIndex: "category", width: 120, render: (c: string) => c ? <Tag>{c}</Tag> : "—" },
    { title: "状态", dataIndex: "status", width: 90, render: (s: string) => <Tag color={STATUS_COLORS[s]}>{s}</Tag> },
    { title: "来源", dataIndex: "source", width: 100, render: (s: string) => SOURCE_LABELS[s] || s },
    { title: "版本", dataIndex: "version", width: 70 },
    { title: "更新时间", dataIndex: "updated_at", width: 170, render: (t: string) => t?.replace("T", " ").slice(0, 19) },
    {
      title: "操作", width: 170, render: (_: any, r: any) => (
        <Space size="small">
          <a onClick={() => openEdit(r)}>{editable ? "编辑" : "查看"}</a>
          <a onClick={() => setVersionsItem(r)}><HistoryOutlined /> 版本</a>
          {editable && (
            <Popconfirm title="确认删除？" onConfirm={async () => { await knowledgeApi.remove(r.id); message.success("已删除"); load(); }}>
              <a style={{ color: "#ef4444" }}>删除</a>
            </Popconfirm>
          )}
        </Space>
      ),
    },
  ];

  return (
    <Card>
      <Space style={{ marginBottom: 16 }} wrap>
        <Input
          allowClear placeholder="搜索标题/内容" prefix={<SearchOutlined />} style={{ width: 240 }}
          onChange={(e) => setParams((p: any) => ({ ...p, q: e.target.value, page: 1 }))}
        />
        <Select
          allowClear placeholder="状态" style={{ width: 120 }}
          options={[{ value: "published", label: "已发布" }, { value: "draft", label: "草稿" }, { value: "archived", label: "已归档" }]}
          onChange={(v) => setParams((p: any) => ({ ...p, status: v || "", page: 1 }))}
        />
        <Button icon={<ExperimentOutlined />} onClick={() => setTestOpen(true)}>测试检索</Button>
        {editable && <Button icon={<UploadOutlined />} onClick={() => setImportOpen(true)}>批量导入</Button>}
        {editable && <Button type="primary" icon={<PlusOutlined />} onClick={() => openEdit(null)}>新增条目</Button>}
      </Space>

      <Table
        rowKey="id" loading={loading} columns={columns as any} dataSource={data.items}
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
          <Space style={{ width: "100%" }} size="large">
            <Form.Item name="category" label="分类" style={{ flex: 1 }}><Input placeholder="如 售后" /></Form.Item>
            <Form.Item name="status" label="状态">
              <Select style={{ width: 140 }} options={[{ value: "published", label: "已发布" }, { value: "draft", label: "草稿" }, { value: "archived", label: "已归档" }]} />
            </Form.Item>
          </Space>
          <Form.Item name="tags" label="标签"><Select mode="tags" placeholder="回车添加标签" /></Form.Item>
          {drawer?.id && <Alert type="info" showIcon message={`当前版本 v${drawer.version} · 已分块 ${drawer.chunk_count ?? "?"} 段`} />}
        </Form>
      </Drawer>

      <TestRetrievalModal open={testOpen} onClose={() => setTestOpen(false)} />
      <ImportModal open={importOpen} onClose={() => setImportOpen(false)} onDone={load} />
      <VersionsModal item={versionsItem} onClose={() => setVersionsItem(null)} onRollback={load} editable={editable} />
    </Card>
  );
}

function TestRetrievalModal({ open, onClose }: any) {
  const [q, setQ] = useState("");
  const [results, setResults] = useState<any[]>([]);
  const [loading, setLoading] = useState(false);
  const run = async () => {
    setLoading(true);
    try {
      const d = await knowledgeApi.testRetrieval(q);
      setResults(d.results);
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
  const [versions, setVersions] = useState<any[]>([]);
  useEffect(() => {
    if (item?.id) knowledgeApi.versions(item.id).then((d) => setVersions(d.versions));
  }, [item]);
  return (
    <Modal title="版本历史" open={!!item} onCancel={onClose} footer={null} width={640}>
      <List
        dataSource={versions} locale={{ emptyText: "暂无历史版本" }}
        renderItem={(v: any) => (
          <List.Item
            actions={editable ? [
              <Popconfirm title={`回滚到 v${v.version}？`} onConfirm={async () => {
                await knowledgeApi.rollback(item.id, v.id); message.success("已回滚"); onRollback(); onClose();
              }}><a>回滚</a></Popconfirm>,
            ] : []}
          >
            <List.Item.Meta
              title={<span>v{v.version} <Tag>{v.editor_email || "系统"}</Tag> <span style={{ color: "#999", fontSize: 12 }}>{v.change_note}</span></span>}
              description={<div><b>{v.title}</b><div style={{ color: "#666", fontSize: 12 }}>{v.content?.slice(0, 120)}…</div></div>}
            />
          </List.Item>
        )}
      />
    </Modal>
  );
}

function ReviewTab({ editable, onChange }: any) {
  const { message } = AntApp.useApp();
  const [items, setItems] = useState<any[]>([]);
  const load = () => knowledgeApi.reviewList("pending").then((d) => setItems(d.candidates));
  useEffect(() => { load(); }, []);

  return (
    <Card>
      <Alert style={{ marginBottom: 16 }} type="info" showIcon message="以下是从对话中自动沉淀的候选知识，审核通过后将进入知识库。" />
      <List
        dataSource={items} locale={{ emptyText: "暂无待审核内容" }}
        renderItem={(c: any) => (
          <List.Item
            actions={editable ? [
              <Popconfirm title="通过并加入知识库？" onConfirm={async () => { await knowledgeApi.reviewApprove(c.id, {}); message.success("已通过"); load(); onChange(); }}>
                <a style={{ color: "#10b981" }}>通过</a>
              </Popconfirm>,
              <Popconfirm title="拒绝该候选？" onConfirm={async () => { await knowledgeApi.reviewReject(c.id); message.success("已拒绝"); load(); onChange(); }}>
                <a style={{ color: "#ef4444" }}>拒绝</a>
              </Popconfirm>,
            ] : []}
          >
            <List.Item.Meta
              title={c.suggested_title || "（无标题）"}
              description={
                <div>
                  <div style={{ marginBottom: 4 }}>{c.suggested_content}</div>
                  <div style={{ color: "#999", fontSize: 12, whiteSpace: "pre-wrap" }}>原始片段：{c.raw_excerpt?.slice(0, 160)}…</div>
                </div>
              }
            />
          </List.Item>
        )}
      />
    </Card>
  );
}
