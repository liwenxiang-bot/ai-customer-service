import { useEffect, useState } from "react";
import { Button, Checkbox, Form, Input, App as AntApp } from "antd";
import { Logo } from "../components/Logo";
import { LoginArt } from "../components/LoginArt";
import { useLocation, useNavigate, useSearchParams } from "react-router-dom";
import { useAuth } from "../auth";
import { apiError, tenantStore } from "../api/client";

const REMEMBER_KEY = "acs_login_remember";

// "记住邮箱" only persists email + tenant slug locally; the password is never stored —
// saving credentials is the browser password manager's job (autoComplete hints below).

export function Login() {
  const { login } = useAuth();
  const nav = useNavigate();
  const loc = useLocation();
  const { message } = AntApp.useApp();
  const [loading, setLoading] = useState(false);
  const [showTenant, setShowTenant] = useState(false);
  const [form] = Form.useForm();
  const [params] = useSearchParams();

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
    // 多租户深链兜底：/login?tenant=acme 直接展开并回填租户字段。这是重复邮箱用户
    // 在全新浏览器上唯一的自助入口（页面已不再提供「指定租户登录」链接），
    // 超管把带 slug 的登录链接发给对方即可。URL 优先于本地记忆。
    const urlTenant = (params.get("tenant") || "").trim();
    if (urlTenant) {
      form.setFieldsValue({ tenant: urlTenant });
      setShowTenant(true);
    }
  }, [form, params]);

  const onFinish = async (v: { email: string; password: string; tenant?: string; remember?: boolean }) => {
    setLoading(true);
    try {
      await login(v.email, v.password, v.tenant);
      if (v.remember) {
        localStorage.setItem(REMEMBER_KEY, JSON.stringify({ email: v.email, tenant: v.tenant || "" }));
      } else {
        localStorage.removeItem(REMEMBER_KEY);
      }
      nav(safeReturnPath((loc.state as any)?.from || params.get("from")), { replace: true });
    } catch (e) {
      message.error(apiError(e));
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="acs-login">
      <aside className="acs-login-aside">
        <div className="acs-login-brand"><Logo size={26} />AI 客服</div>
        <LoginArt className="acs-login-art" />
        <div>
          <div className="acs-login-statement">运营后台</div>
          <div className="acs-login-note">管理知识库、对话记录、转人工工单和模型配置。</div>
        </div>
      </aside>

      <main className="acs-login-main">
        <div className="acs-login-form">
          <h1 className="acs-login-title">登录</h1>
          <div className="acs-login-sub">使用管理员为你开通的账号</div>

          <Form form={form} layout="vertical" onFinish={onFinish} requiredMark={false} size="large">
            <Form.Item label="邮箱" name="email" rules={[{ required: true, message: "请输入邮箱" }]}>
              <Input placeholder="name@company.com" autoComplete="username" />
            </Form.Item>
            <Form.Item label="密码" name="password" rules={[{ required: true, message: "请输入密码" }]}>
              <Input.Password autoComplete="current-password" />
            </Form.Item>
            {/* 仅 /login?tenant= 深链或代管过的浏览器自动展开；常规部署按邮箱自动识别租户，无需入口 */}
            <Form.Item label="租户标识" name="tenant" hidden={!showTenant}
              tooltip="仅当同一邮箱存在于多个租户时才需要填写；通常留空即可（系统按邮箱自动识别租户）">
              <Input placeholder="租户 slug（一般无需填写）" autoComplete="organization" />
            </Form.Item>

            <Form.Item name="remember" valuePropName="checked" style={{ marginBottom: 12 }}>
              <Checkbox>记住邮箱</Checkbox>
            </Form.Item>

            <Button type="primary" block htmlType="submit" loading={loading} autoInsertSpace={false} className="acs-login-btn">登录</Button>
          </Form>
          <div className="acs-login-hint">登录遇到问题，请联系超级管理员。</div>
        </div>
      </main>
    </div>
  );
}

/** Only same-app relative paths (never "//evil" or absolute URLs) — avoids an open redirect. */
function safeReturnPath(p: unknown): string {
  return typeof p === "string" && p.startsWith("/") && !p.startsWith("//") && !p.startsWith("/login")
    ? p
    : "/dashboard";
}
