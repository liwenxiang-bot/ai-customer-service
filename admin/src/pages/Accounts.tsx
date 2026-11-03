import { useEffect, useRef, useState } from "react";
import { Button, Card, Form, Input, Modal, Select, Space, Table, Tabs, Tag, Popconfirm, App as AntApp } from "antd";
import { PlusOutlined } from "@ant-design/icons";
import { accountApi } from "../api";
import { apiError } from "../api/client";
import { useAuth } from "../auth";
import { fmtTime } from "../utils/time";

const ROLE_LABELS: any = { admin: "管理员", operator: "运营", readonly: "只读" };
const ROLE_COLORS: any = { admin: "purple", operator: "blue", readonly: "default" };

// 与 backend 各处 write_audit 的 action 值对应，用于日志筛选与展示。
const ACTION_LABELS: Record<string, string> = {
  "auth.login": "登录",
  "account.create": "创建账号",
  "account.update": "更新账号",
  "account.delete": "删除账号",
  "ai_config.update": "更新 AI 配置",
  "ai_config.rebuild": "触发向量重建",
  "channel.update": "更新渠道配置",
  "knowledge.create": "新增知识",
  "knowledge.update": "更新知识",
  "knowledge.delete": "删除知识",
};

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
  const { user: me } = useAuth(); // 当前登录账号禁止删除/停用自己，避免自锁
  const [users, setUsers] = useState<any[]>([]);
  const [modal, setModal] = useState<any>(null);
  const [form] = Form.useForm();
  const load = () => accountApi.users().then((d) => setUsers(d.users)).catch((e) => message.error(apiError(e)));
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
          <Popconfirm title="删除该账号？" disabled={r.id === me?.id} onConfirm={async () => { try { await accountApi.remove(r.id); message.success("已删除"); load(); } catch (e) { message.error(apiError(e)); } }}>
            <Button type="link" size="small" danger style={{ padding: 0 }} disabled={r.id === me?.id}
              title={r.id === me?.id ? "不能删除当前登录账号" : undefined}>删除</Button>
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
          <Form.Item name="password" label={modal?.id ? "重置密码（留空不改）" : "密码"} rules={modal?.id ? [{ min: 8, message: "密码至少 8 位" }] : [{ required: true, min: 8, message: "密码至少 8 位" }]}>
            <Input.Password autoComplete="new-password" />
          </Form.Item>
          {modal?.id && (
            <Form.Item name="is_active" label="状态" extra={modal.id === me?.id ? "不能停用当前登录账号" : undefined}>
              <Select disabled={modal.id === me?.id} options={[{ value: true, label: "启用" }, { value: false, label: "停用" }]} />
            </Form.Item>
          )}
        </Form>
      </Modal>
    </Card>
  );
}

function AuditTab() {
  const { message } = AntApp.useApp();
  const [data, setData] = useState<any>({ items: [], total: 0 });
  const [params, setParams] = useState<any>({ page: 1, page_size: 20, action: "" });
  // 递增序号防过期响应：快速切换筛选/翻页时，只有最新一次请求的结果才落地。
  const loadSeq = useRef(0);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => {
    const seq = ++loadSeq.current;
    accountApi.auditLogs(params)
      .then((d) => { if (seq === loadSeq.current) setData(d); })
      .catch((e) => { if (seq === loadSeq.current) message.error(apiError(e)); });
  }, [params.page, params.page_size, params.action]);
  const columns = [
    { title: "时间", dataIndex: "created_at", width: 170, render: (t: string) => fmtTime(t) },
    { title: "操作人", dataIndex: "actor_email", width: 200 },
    { title: "动作", dataIndex: "action", width: 160, render: (a: string) => <Tag>{ACTION_LABELS[a] || a}</Tag> },
    { title: "对象", render: (_: any, r: any) => `${r.target_type} ${r.target_id}` },
    { title: "IP", dataIndex: "ip", width: 130 },
  ];
  return (
    <Card>
      <Select allowClear placeholder="按动作筛选" style={{ width: 180, marginBottom: 12 }}
        options={Object.entries(ACTION_LABELS).map(([value, label]) => ({ value, label }))}
        onChange={(v) => setParams((p: any) => ({ ...p, action: v || "", page: 1 }))} />
      <Table rowKey="id" columns={columns as any} dataSource={data.items} size="small"
        scroll={{ x: "max-content" }}
        pagination={{ current: params.page, pageSize: params.page_size, total: data.total,
          onChange: (page, page_size) => setParams((p: any) => ({ ...p, page, page_size })) }}
      />
    </Card>
  );
}
