import { useEffect, useState } from "react";
import { Button, Card, Checkbox, Form, Input, Typography, App as AntApp } from "antd";
import { LockOutlined, UserOutlined } from "@ant-design/icons";
import { useNavigate } from "react-router-dom";
import { useAuth } from "../auth";
import { apiError } from "../api/client";

const REMEMBER_KEY = "acs_login_remember";

// Lightweight local persistence for the "remember me" convenience on trusted machines.
// (base64 is obfuscation, not encryption — leave unchecked on shared computers.)
const enc = (s: string) => btoa(unescape(encodeURIComponent(s)));
const dec = (s: string) => {
  try { return decodeURIComponent(escape(atob(s))); } catch { return ""; }
};

export function Login() {
  const { login } = useAuth();
  const nav = useNavigate();
  const { message } = AntApp.useApp();
  const [loading, setLoading] = useState(false);
  const [form] = Form.useForm();

  // Pre-fill from a previously remembered login.
  useEffect(() => {
    const saved = localStorage.getItem(REMEMBER_KEY);
    if (saved) {
      try {
        const { email, password } = JSON.parse(saved);
        form.setFieldsValue({ email, password: dec(password || ""), remember: true });
      } catch { /* ignore corrupt value */ }
    }
  }, [form]);

  const onFinish = async (v: { email: string; password: string; remember?: boolean }) => {
    setLoading(true);
    try {
      await login(v.email, v.password);
      if (v.remember) {
        localStorage.setItem(REMEMBER_KEY, JSON.stringify({ email: v.email, password: enc(v.password) }));
      } else {
        localStorage.removeItem(REMEMBER_KEY);
      }
      nav("/dashboard");
    } catch (e) {
      message.error(apiError(e));
    } finally {
      setLoading(false);
    }
  };

  return (
    <div style={{ height: "100vh", display: "grid", placeItems: "center", background: "linear-gradient(140deg,#0F766E 0%,#115E59 55%,#134E4A 100%)" }}>
      <Card style={{ width: 380, boxShadow: "0 12px 48px rgba(0,0,0,.2)" }}>
        <div style={{ textAlign: "center", marginBottom: 24 }}>
          <Typography.Title level={3} style={{ marginBottom: 4 }}>🤖 AI 客服后台</Typography.Title>
          <Typography.Text type="secondary">运营管理后台 · 登录</Typography.Text>
        </div>
        <Form form={form} layout="vertical" onFinish={onFinish}>
          <Form.Item name="email" rules={[{ required: true, message: "请输入邮箱" }]}>
            <Input size="large" prefix={<UserOutlined />} placeholder="邮箱" autoComplete="username" />
          </Form.Item>
          <Form.Item name="password" rules={[{ required: true, message: "请输入密码" }]}>
            <Input.Password size="large" prefix={<LockOutlined />} placeholder="密码" autoComplete="current-password" />
          </Form.Item>
          <Form.Item name="remember" valuePropName="checked" style={{ marginBottom: 16 }}>
            <Checkbox>记住账号密码</Checkbox>
          </Form.Item>
          <Button type="primary" size="large" block htmlType="submit" loading={loading}>
            登录
          </Button>
        </Form>
      </Card>
    </div>
  );
}
