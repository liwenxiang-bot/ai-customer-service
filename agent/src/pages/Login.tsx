import { FormEvent, useEffect, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { apiError } from "../api";
import { useAuth } from "../auth";

const REMEMBER_KEY = "acs_agent_login_email";

export function Login() {
  const { user, login } = useAuth();
  const nav = useNavigate();
  const loc = useLocation();
  const from = (loc.state as { from?: string } | null)?.from || "/";

  const saved = localStorage.getItem(REMEMBER_KEY) || "";
  const [email, setEmail] = useState(saved);
  const [password, setPassword] = useState("");
  const [remember, setRemember] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  // 已登录（含刷新后恢复会话）直接回跳
  useEffect(() => {
    if (user) nav(from, { replace: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [user]);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!email.trim() || !password || busy) return;
    setBusy(true);
    setError("");
    try {
      await login(email.trim(), password);
      if (remember) localStorage.setItem(REMEMBER_KEY, email.trim());
      else localStorage.removeItem(REMEMBER_KEY);
      nav(from, { replace: true });
    } catch (err) {
      setError(apiError(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="login-page">
      <form className="login-card" onSubmit={submit}>
        <div className="login-brand" aria-hidden="true">
          <svg viewBox="0 0 64 64" width="28" height="28" fill="none">
            <path d="M20 38v-5c0-7 5.4-12 12-12s12 5 12 12v5" stroke="currentColor" strokeWidth="4" strokeLinecap="round" />
            <rect x="16.6" y="36.4" width="7" height="11" rx="3.2" fill="currentColor" />
            <rect x="40.4" y="36.4" width="7" height="11" rx="3.2" fill="currentColor" />
            <path d="M43.6 47.4c-.6 3.2-3.8 5-7.6 5h-2.4" stroke="currentColor" strokeWidth="3" strokeLinecap="round" />
            <circle cx="32.4" cy="52.4" r="2.4" fill="currentColor" />
          </svg>
        </div>
        <div className="login-title">客服坐席</div>
        <div className="login-sub">登录后开始接待客户会话</div>
        <label className="field">
          <span className="field-label">邮箱</span>
          <input
            className="input"
            type="email"
            autoComplete="username"
            placeholder="you@example.com"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
          />
        </label>
        <label className="field">
          <span className="field-label">密码</span>
          <input
            className="input"
            type="password"
            autoComplete="current-password"
            placeholder="请输入密码"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </label>
        <label className="remember-row">
          <input type="checkbox" checked={remember} onChange={(e) => setRemember(e.target.checked)} />
          <span>记住邮箱</span>
        </label>
        {error && <div className="form-error">{error}</div>}
        <button className="btn btn-primary btn-block" type="submit" disabled={busy}>
          {busy ? "登录中…" : "登录"}
        </button>
      </form>
    </div>
  );
}
