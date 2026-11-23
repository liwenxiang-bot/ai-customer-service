import { FormEvent, useEffect, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { apiError } from "../api";
import { useAuth } from "../auth";
import { Logo } from "../components/Logo";
import { LoginArt } from "../components/LoginArt";

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
    <div className="login">
      <aside className="login-aside">
        <div className="login-brand"><Logo size={26} />AI 客服</div>
        <LoginArt className="login-art" />
        <div className="login-aside-foot">
          <div className="login-statement">坐席工作台</div>
          <div className="login-note">认领转人工的工单，接管对话并回复客户。</div>
        </div>
      </aside>

      <main className="login-main">
        <form className="login-form" onSubmit={submit}>
          <h1 className="login-title">登录坐席工作台</h1>
          <div className="login-sub">使用管理员为你开通的账号</div>
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
          <div className="login-hint">登录遇到问题，请联系管理员重置密码。</div>
        </form>
      </main>
    </div>
  );
}
