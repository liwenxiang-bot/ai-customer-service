import { useEffect, useState } from "react";
import { Button, Card, Form, Input, Modal, Select, Space, Table, Tabs, Tag, Popconfirm, App as AntApp } from "antd";
import { PlusOutlined } from "@ant-design/icons";
import { accountApi } from "../api";
import { apiError } from "../api/client";
import { fmtTime } from "../utils/time";

const ROLE_LABELS: any = { admin: "管理员", operator: "运营", readonly: "只读" };
const ROLE_COLORS: any = { admin: "purple", operator: "blue", readonly: "default" };

export function Accounts() {
  return (
    <div>
      <div className="acs-page-title">账号与权限</div>
      <div className="acs-page-sub">管理后台账号与角色（管理员 / 运营 / 只读），查看敏感操作的审计日志。</div>
      <Tabs items={[
        { key: "users", label: "账号管理", children: <UsersTab /> },
        { key: "audit", label: "操作日志", children: <AuditTab /> },
      ]} />
    </div>
  );
}

function UsersTab() {
  const { message } = AntApp.useApp();
  const [users, setUsers] = useState<any[]>([]);
  const [modal, setModal] = useState<any>(null);
  const [form] = Form.useForm();
  const load = () => accountApi.users().then((d) => setUsers(d.users));
  useEffect(() => { load(); }, []);

  const openModal = (u: any) => {
    if (u) form.setFieldsValue({ ...u, password: "" }); else form.resetFields();
    setModal(u || {});
  };
  const save = async () => {
    const v = await form.validateFields();
    try {
      if (modal.id) await accountApi.update(modal.id, v);
      else await accountApi.create(v);
      message.success("已保存"); setModal(null); load();
    } catch (e) { message.error(apiError(e)); }
  };

  const columns = [
    { title: "邮箱", dataIndex: "email" },
    { title: "姓名", dataIndex: "name", render: (n: string) => n || "—" },
    { title: "角色", dataIndex: "role", render: (r: string) => <Tag color={ROLE_COLORS[r]}>{ROLE_LABELS[r]}</Tag> },
    { title: "状态", dataIndex: "is_active", render: (a: boolean) => a ? <Tag color="green">启用</Tag> : <Tag>停用</Tag> },
    { title: "最近登录", dataIndex: "last_login_at", render: (t: string) => fmtTime(t) },
    {
      title: "操作", render: (_: any, r: any) => (
        <Space>
          <a onClick={() => openModal(r)}>编辑</a>
          <Popconfirm title="删除该账号？" onConfirm={async () => { try { await accountApi.remove(r.id); message.success("已删除"); load(); } catch (e) { message.error(apiError(e)); } }}>
            <a style={{ color: "#ef4444" }}>删除</a>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  return (
    <Card>
      <Button type="primary" icon={<PlusOutlined />} style={{ marginBottom: 16 }} onClick={() => openModal(null)}>新增账号</Button>
      <Table rowKey="id" columns={columns as any} dataSource={users} pagination={false} size="small" />
      <Modal title={modal?.id ? "编辑账号" : "新增账号"} open={!!modal} onCancel={() => setModal(null)} onOk={save}>
        <Form form={form} layout="vertical">
          <Form.Item name="email" label="邮箱" rules={[{ required: true, type: "email" }]}><Input disabled={!!modal?.id} /></Form.Item>
          <Form.Item name="name" label="姓名"><Input /></Form.Item>
          <Form.Item name="role" label="角色" rules={[{ required: true }]}>
            <Select options={[{ value: "admin", label: "管理员" }, { value: "operator", label: "运营" }, { value: "readonly", label: "只读" }]} />
          </Form.Item>
          <Form.Item name="password" label={modal?.id ? "重置密码（留空不改）" : "密码"} rules={modal?.id ? [] : [{ required: true, min: 6 }]}>
            <Input.Password autoComplete="new-password" />
          </Form.Item>
          {modal?.id && <Form.Item name="is_active" label="状态" valuePropName="checked"><Select options={[{ value: true, label: "启用" }, { value: false, label: "停用" }]} /></Form.Item>}
        </Form>
      </Modal>
    </Card>
  );
}

function AuditTab() {
  const [data, setData] = useState<any>({ items: [], total: 0 });
  const [params, setParams] = useState<any>({ page: 1, page_size: 20 });
  useEffect(() => { accountApi.auditLogs(params).then(setData); }, [JSON.stringify(params)]);
  const columns = [
    { title: "时间", dataIndex: "created_at", width: 170, render: (t: string) => fmtTime(t) },
    { title: "操作人", dataIndex: "actor_email", width: 200 },
    { title: "动作", dataIndex: "action", width: 160, render: (a: string) => <Tag>{a}</Tag> },
    { title: "对象", render: (_: any, r: any) => `${r.target_type} ${r.target_id}` },
    { title: "IP", dataIndex: "ip", width: 130 },
  ];
  return (
    <Card>
      <Table rowKey="id" columns={columns as any} dataSource={data.items} size="small"
        pagination={{ current: params.page, pageSize: params.page_size, total: data.total,
          onChange: (page, page_size) => setParams({ page, page_size }) }}
      />
    </Card>
  );
}
