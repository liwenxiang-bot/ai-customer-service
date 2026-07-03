import { useEffect, useState } from "react";
import { Button, Checkbox, Form, Input, App as AntApp } from "antd";
import { LockOutlined, UserOutlined, RobotOutlined, ApartmentOutlined } from "@ant-design/icons";
import { useNavigate } from "react-router-dom";
import { useAuth } from "../auth";
import { apiError, tenantStore } from "../api/client";

const REMEMBER_KEY = "acs_login_remember";

// "记住邮箱" only persists email + tenant slug locally; the password is never stored —
// saving credentials is the browser password manager's job (autoComplete hints below).

export function Login() {
  const { login } = useAuth();
  const nav = useNavigate();
  const { message } = AntApp.useApp();
  const [loading, setLoading] = useState(false);
  const [showTenant, setShowTenant] = useState(false);
  const [form] = Form.useForm();

  // Pre-fill from a previously remembered login (email/tenant only).
  useEffect(() => {
    const saved = localStorage.getItem(REMEMBER_KEY);
    if (saved) {
      try {
        const { email, password, tenant } = JSON.parse(saved);
        // 向后兼容：旧版本曾把 base64 密码一并存入本地，这里只回填邮箱并顺手清掉密码字段。
        if (password) localStorage.setItem(REMEMBER_KEY, JSON.stringify({ email, tenant: tenant || "" }));
        form.setFieldsValue({ email, tenant: tenant || tenantStore.slug, remember: true });
        if (tenant || tenantStore.slug) setShowTenant(true);
      } catch { /* ignore corrupt value */ }
    } else if (tenantStore.slug) {
      form.setFieldsValue({ tenant: tenantStore.slug });
      setShowTenant(true);
    }
  }, [form]);

  const onFinish = async (v: { email: string; password: string; tenant?: string; remember?: boolean }) => {
    setLoading(true);
    try {
      await login(v.email, v.password, v.tenant);
      if (v.remember) {
        localStorage.setItem(REMEMBER_KEY, JSON.stringify({ email: v.email, tenant: v.tenant || "" }));
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
    <div className="acs-login-bg">
      <div className="acs-login-card">
        <div className="acs-login-brand">
          <div className="acs-login-logo"><RobotOutlined /></div>
          <div>
            <div className="acs-login-title">AI 客服后台</div>
            <div className="acs-login-sub">运营管理 · 知识库驱动的智能客服</div>
          </div>
        </div>

        <Form form={form} layout="vertical" onFinish={onFinish} requiredMark={false} size="large">
          <Form.Item name="email" rules={[{ required: true, message: "请输入邮箱" }]}>
            <Input prefix={<UserOutlined className="acs-login-icon" />} placeholder="邮箱" autoComplete="username" />
          </Form.Item>
          <Form.Item name="password" rules={[{ required: true, message: "请输入密码" }]} style={{ marginBottom: 12 }}>
            <Input.Password prefix={<LockOutlined className="acs-login-icon" />} placeholder="密码" autoComplete="current-password" />
          </Form.Item>
          <Form.Item name="tenant" hidden={!showTenant} style={{ marginBottom: 12 }}
            tooltip="仅当同一邮箱存在于多个租户时才需要填写；通常留空即可（系统按邮箱自动识别租户）">
            <Input prefix={<ApartmentOutlined className="acs-login-icon" />} placeholder="租户标识 slug（一般无需填写）" autoComplete="organization" />
          </Form.Item>

          <div className="acs-login-row">
            <Form.Item name="remember" valuePropName="checked" noStyle>
              <Checkbox>记住邮箱</Checkbox>
            </Form.Item>
            {!showTenant && (
              <span className="acs-login-link" onClick={() => setShowTenant(true)}>指定租户登录</span>
            )}
          </div>

          <Button type="primary" block htmlType="submit" loading={loading} className="acs-login-btn">登 录</Button>
        </Form>
      </div>
    </div>
  );
}
