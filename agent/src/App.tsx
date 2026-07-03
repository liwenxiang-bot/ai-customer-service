import { Component, ReactElement, ReactNode } from "react";
import { HashRouter, Navigate, Route, Routes, useLocation, useParams } from "react-router-dom";
import { AuthProvider, useAuth } from "./auth";
import { Login } from "./pages/Login";
import { Inbox } from "./pages/Inbox";
import { SessionPage } from "./pages/Session";

/** 会话面板错误边界：把单会话渲染故障限制在右栏，避免整站白屏。
 *  按会话 id 作 key 重建，切换会话时自动清除上一个会话的错误态。 */
class SessionBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() {
    return { failed: true };
  }
  componentDidCatch(err: unknown) {
    console.error("会话面板渲染出错", err);
  }
  render() {
    if (this.state.failed) {
      return (
        <div className="desk-empty">
          <div className="desk-empty-title">出错了</div>
          <div className="desk-empty-sub">该会话面板加载异常，可点此重载或返回收件箱</div>
          <div style={{ display: "flex", gap: 8, marginTop: 14 }}>
            <button className="btn btn-primary btn-sm" onClick={() => window.location.reload()}>
              点此重载
            </button>
            <button className="btn btn-ghost btn-sm" onClick={() => { window.location.hash = "#/"; }}>
              返回收件箱
            </button>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}

/** 未登录访问任意页 → 跳 /login，登录后回跳原地址（含 /s/:id 通知深链）。 */
function RequireAuth({ children }: { children: ReactElement }) {
  const { user, loading } = useAuth();
  const loc = useLocation();
  if (loading) return <div className="page-center muted">加载中…</div>;
  if (!user) return <Navigate to="/login" replace state={{ from: loc.pathname }} />;
  return children;
}

/** 桌面端（≥1024px）右栏未选会话时的占位。 */
function DeskPlaceholder() {
  return (
    <div className="desk-empty">
      <svg className="desk-empty-icon" viewBox="0 0 64 64" fill="none" aria-hidden="true">
        <path d="M20 38v-5c0-7 5.4-12 12-12s12 5 12 12v5" stroke="currentColor" strokeWidth="3.4" strokeLinecap="round" />
        <rect x="16.6" y="36.4" width="7" height="11" rx="3.2" fill="currentColor" />
        <rect x="40.4" y="36.4" width="7" height="11" rx="3.2" fill="currentColor" />
        <path d="M43.6 47.4c-.6 3.2-3.8 5-7.6 5h-2.4" stroke="currentColor" strokeWidth="2.6" strokeLinecap="round" />
        <circle cx="32.4" cy="52.4" r="2.2" fill="currentColor" />
      </svg>
      <div className="desk-empty-title">选择一个会话开始接待</div>
      <div className="desk-empty-sub">左侧收件箱里点开任意会话，即可查看消息并回复客户</div>
    </div>
  );
}

/** 工作台布局：桌面双栏（左收件箱 340px 常驻 + 右会话面板），移动端单列全屏切换。
 *  两条路由（/ 与 /s/:id）渲染同一组件，收件箱在会话切换间保持挂载（未读追踪不中断）。 */
function Workspace() {
  const { id = "" } = useParams();
  return (
    <div className="workspace">
      <aside className={`pane pane-inbox${id ? " pane-off" : ""}`}>
        <Inbox activeId={id} />
      </aside>
      <section className={`pane pane-session${id ? "" : " pane-off"}`}>
        {id ? (
          <SessionBoundary key={id}>
            <SessionPage />
          </SessionBoundary>
        ) : (
          <DeskPlaceholder />
        )}
      </section>
    </div>
  );
}

export default function App() {
  return (
    <AuthProvider>
      <HashRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
        <Routes>
          <Route path="/login" element={<Login />} />
          <Route path="/" element={<RequireAuth><Workspace /></RequireAuth>} />
          <Route path="/s/:id" element={<RequireAuth><Workspace /></RequireAuth>} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </HashRouter>
    </AuthProvider>
  );
}
