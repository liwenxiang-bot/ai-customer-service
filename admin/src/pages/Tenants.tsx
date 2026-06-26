import { useEffect, useState } from "react";
import { App as AntApp, Button, Card, Form, Input, Modal, Space, Switch, Table, Tag } from "antd";
import { LoginOutlined, PlusOutlined } from "@ant-design/icons";
import { tenantApi } from "../api";
import { apiError, tenantStore } from "../api/client";

/** 租户管理 (super-admin only): create/list/suspend tenants. Isolation is DB-enforced (RLS);
 *  this page just drives the registry + onboarding. */
export function Tenants() {
  const { message } = AntApp.useApp();
  const [rows, setRows] = useState<any[]>([]);
  const [loading, setLoading] = useState(false);
  const [open, setOpen] = useState(false);
  const [created, setCreated] = useState<any>(null);
  const [form] = Form.useForm();

  const load = () => {
    setLoading(true);
    tenantApi.list().then((d) => setRows(d.tenants)).catch((e) => message.error(apiError(e))).finally(() => setLoading(false));
  };
  useEffect(load, []);

  const create = async () => {
    const v = await form.validateFields();
    try {
      const r = await tenantApi.create(v);
      setCreated(r);
      setOpen(false);
      form.resetFields();
      load();
    } catch (e) {
      message.error(apiError(e));
    }
  };
  const toggle = async (r: any) => {
    try {
      await tenantApi.setActive(r.id, !r.is_active);
      load();
    } catch (e) {
      message.error(apiError(e));
    }
  };
  // "Act as" this tenant: scope every request to its slug, then hard-reload so all pages
  // refetch under the new context and the acting-as banner shows.
  const enter = (r: any) => {
    tenantStore.set(r.slug, r.name);
    window.location.assign("/knowledge");
  };
  const active = tenantStore.slug;

  const columns = [
    { title: "名称", dataIndex: "name" },
    { title: "标识 (slug)", dataIndex: "slug", render: (s: string) => <code>{s}</code> },
    { title: "Widget channel_key", dataIndex: "web_channel_key", render: (s: string) => <code>{s || "—"}</code> },
    { title: "管理员", dataIndex: "admins", width: 80 },
    { title: "知识条目", dataIndex: "knowledge_items", width: 90 },
    { title: "状态", dataIndex: "is_active", width: 80, render: (a: boolean) => <Tag color={a ? "green" : "red"}>{a ? "启用" : "停用"}</Tag> },
    {
      title: "操作", width: 200, render: (_: any, r: any) => (
        <Space>
          {active === r.slug ? (
            <Tag color="gold">查看中</Tag>
          ) : (
            <Button size="small" icon={<LoginOutlined />} disabled={!r.is_active} onClick={() => enter(r)}>进入</Button>
          )}
          <Switch size="small" checked={r.is_active} onChange={() => toggle(r)} checkedChildren="启用" unCheckedChildren="停用" />
        </Space>
      ),
    },
  ];

  return (
    <Card>
      <div className="acs-page-title">租户管理</div>
      <div className="acs-page-sub">超级管理员可创建租户；各租户的知识库、会话、坐席、账号由数据库 RLS 强制隔离。</div>
      <div style={{ marginBottom: 12 }}>
        <Button type="primary" icon={<PlusOutlined />} onClick={() => setOpen(true)}>新建租户</Button>
      </div>
      <Table rowKey="id" loading={loading} columns={columns as any} dataSource={rows} size="small" pagination={false} />

      <Modal title="新建租户" open={open} onCancel={() => setOpen(false)} onOk={create} okText="创建并开通" destroyOnHidden>
        <Form form={form} layout="vertical">
          <Form.Item name="name" label="租户名称" rules={[{ required: true, message: "请输入名称" }]}>
            <Input placeholder="如 Acme 公司" />
          </Form.Item>
          <Form.Item name="slug" label="标识 slug（留空自动生成；也是该租户 widget 的 channel_key）">
            <Input placeholder="acme" />
          </Form.Item>
          <Form.Item name="admin_email" label="管理员邮箱" rules={[{ required: true, type: "email", message: "请输入有效邮箱" }]}>
            <Input placeholder="admin@acme.com" />
          </Form.Item>
          <Form.Item name="admin_password" label="管理员初始密码" rules={[{ required: true, min: 8, message: "至少 8 位" }]}>
            <Input.Password placeholder="至少 8 位" />
          </Form.Item>
        </Form>
      </Modal>

      <Modal title="租户已创建 ✅" open={!!created} onCancel={() => setCreated(null)}
        footer={[<Button key="ok" type="primary" onClick={() => setCreated(null)}>知道了</Button>]}>
        {created && (
          <div style={{ lineHeight: 2 }}>
            <div>租户：<b>{created.name}</b></div>
            <div>登录标识 slug：<code>{created.slug}</code>（该租户管理员登录时填这个）</div>
            <div>Widget channel_key：<code>{created.web_channel_key}</code>（嵌入该租户网站时用）</div>
            <div style={{ color: "#888", fontSize: 12, marginTop: 8 }}>
              管理员用上面的邮箱+初始密码、并在登录页填入 slug 即可登录；数据与其他租户完全隔离。
            </div>
          </div>
        )}
      </Modal>
    </Card>
  );
}
