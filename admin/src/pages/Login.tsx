import { useState } from "react";
import { Button, Card, Form, Input, Typography, App as AntApp } from "antd";
import { LockOutlined, UserOutlined } from "@ant-design/icons";
import { useNavigate } from "react-router-dom";
import { useAuth } from "../auth";
import { apiError } from "../api/client";

export function Login() {
  const { login } = useAuth();
  const nav = useNavigate();
  const { message } = AntApp.useApp();
  const [loading, setLoading] = useState(false);

  const onFinish = async (v: { email: string; password: string }) => {
    setLoading(true);
    try {
      await login(v.email, v.password);
      nav("/dashboard");
    } catch (e) {
      message.error(apiError(e));
    } finally {
      setLoading(false);
    }
  };

  return (
    <div style={{ height: "100vh", display: "grid", placeItems: "center", background: "linear-gradient(135deg,#4f46e5 0%,#7c73f0 100%)" }}>
      <Card style={{ width: 380, boxShadow: "0 12px 48px rgba(0,0,0,.2)" }}>
        <div style={{ textAlign: "center", marginBottom: 24 }}>
          <Typography.Title level={3} style={{ marginBottom: 4 }}>🤖 AI 客服后台</Typography.Title>
          <Typography.Text type="secondary">运营管理后台 · 登录</Typography.Text>
        </div>
        <Form layout="vertical" onFinish={onFinish} initialValues={{ email: "admin@example.com" }}>
          <Form.Item name="email" rules={[{ required: true, message: "请输入邮箱" }]}>
            <Input size="large" prefix={<UserOutlined />} placeholder="邮箱" />
          </Form.Item>
          <Form.Item name="password" rules={[{ required: true, message: "请输入密码" }]}>
            <Input.Password size="large" prefix={<LockOutlined />} placeholder="密码" />
          </Form.Item>
          <Button type="primary" size="large" block htmlType="submit" loading={loading}>
            登录
          </Button>
        </Form>
        <Typography.Text type="secondary" style={{ fontSize: 12, display: "block", marginTop: 16, textAlign: "center" }}>
          默认账号 admin@example.com / admin12345（首次登录后请修改）
        </Typography.Text>
      </Card>
    </div>
  );
}
