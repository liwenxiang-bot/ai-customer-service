import { useEffect, useState } from "react";
import { App as AntApp, Button, Card, Drawer, Form, Input, InputNumber, Popconfirm, Space, Table, Tag } from "antd";
import { PlusOutlined, SearchOutlined } from "@ant-design/icons";
import { cannedApi } from "../api";
import { apiError } from "../api/client";
import { useAuth, canEdit } from "../auth";
import { useDebounce } from "../hooks/useDebounce";

export function CannedResponses() {
  const { user } = useAuth();
  const editable = canEdit(user?.role);
  const { message } = AntApp.useApp();
  const [items, setItems] = useState<any[]>([]);
  const [loading, setLoading] = useState(false);
  const [search, setSearch] = useState("");
  const dq = useDebounce(search, 400);
  const [drawer, setDrawer] = useState<any>(null);
  const [form] = Form.useForm();

  const load = () => {
    setLoading(true);
    cannedApi.list(dq).then((d) => setItems(d.items)).finally(() => setLoading(false));
  };
  useEffect(load, [dq]);

  const openEdit = (it: any) => {
    if (it) { form.setFieldsValue(it); setDrawer(it); }
    else { form.resetFields(); form.setFieldsValue({ sort_order: 0 }); setDrawer({}); }
  };
  const save = async () => {
    const v = await form.validateFields();
    try {
      if (drawer.id) await cannedApi.update(drawer.id, v);
      else await cannedApi.create(v);
      message.success("已保存");
      setDrawer(null);
      load();
    } catch (e) { message.error(apiError(e)); }
  };

  const columns = [
    { title: "标题", dataIndex: "title", width: 200, render: (t: string) => t || <span style={{ color: "#aaa" }}>（无标题）</span> },
    { title: "分类", dataIndex: "category", width: 120, render: (c: string) => c ? <Tag>{c}</Tag> : "—" },
    { title: "内容", dataIndex: "content", ellipsis: true },
    { title: "排序", dataIndex: "sort_order", width: 70 },
    ...(editable ? [{
      title: "操作", width: 120, render: (_: any, r: any) => (
        <Space size="small">
          <a onClick={() => openEdit(r)}>编辑</a>
          <Popconfirm title="删除该模板？" onConfirm={async () => { await cannedApi.remove(r.id); message.success("已删除"); load(); }}>
            <a style={{ color: "#ef4444" }}>删除</a>
          </Popconfirm>
        </Space>
      ),
    }] : []),
  ];

  return (
    <div>
      <div className="acs-page-title">快捷回复</div>
      <div className="acs-page-sub">维护常用回复模板，坐席在工作台接管对话时可一键插入。</div>
      <Card>
        <Space style={{ marginBottom: 12 }} wrap>
          <Input allowClear placeholder="搜索标题/内容" prefix={<SearchOutlined />} style={{ width: 220 }}
            value={search} onChange={(e) => setSearch(e.target.value)} />
          {editable && <Button type="primary" icon={<PlusOutlined />} onClick={() => openEdit(null)}>新增模板</Button>}
        </Space>
        <Table rowKey="id" loading={loading} columns={columns as any} dataSource={items} size="small" pagination={false} />
      </Card>

      <Drawer title={drawer?.id ? "编辑模板" : "新增模板"} width={520} open={!!drawer} onClose={() => setDrawer(null)}
        extra={editable && <Button type="primary" onClick={save}>保存</Button>}>
        <Form form={form} layout="vertical" disabled={!editable}>
          <Form.Item name="title" label="标题"><Input placeholder="简短标题，便于检索" /></Form.Item>
          <Form.Item name="content" label="内容" rules={[{ required: true, message: "请输入内容" }]}>
            <Input.TextArea rows={8} placeholder="回复正文" />
          </Form.Item>
          <Space size="large">
            <Form.Item name="category" label="分类"><Input placeholder="如 售后" /></Form.Item>
            <Form.Item name="sort_order" label="排序"><InputNumber min={0} /></Form.Item>
          </Space>
        </Form>
      </Drawer>
    </div>
  );
}
