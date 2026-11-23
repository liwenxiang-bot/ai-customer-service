import { Suspense, lazy, useState, type ReactNode } from "react";
import { Navigate, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { Layout, Menu, Spin, Dropdown, Button, Tooltip, Alert, theme, type MenuProps } from "antd";
import {
  IconOverview, IconConversations, IconTicket, IconAgentDesk, IconKnowledge, IconCanned,
  IconModel, IconChannel, IconAccounts, IconTenant,
  IconMenuFold, IconMenuUnfold, IconSun, IconMoon, IconLogout, IconCaretDown, IconExternal,
} from "./icons";
import { useAuth, isAdmin, isSuperAdmin, agentConsoleUrl } from "./auth";
import { tenantStore } from "./api/client";
import { useThemeMode } from "./theme";
import { Logo } from "./components/Logo";

// Route-level code splitting: each page loads on demand (pages use named exports).
const Login = lazy(() => import("./pages/Login").then((m) => ({ default: m.Login })));
const Dashboard = lazy(() => import("./pages/Dashboard").then((m) => ({ default: m.Dashboard })));
const Knowledge = lazy(() => import("./pages/Knowledge").then((m) => ({ default: m.Knowledge })));
const AIConfig = lazy(() => import("./pages/AIConfig").then((m) => ({ default: m.AIConfig })));
const Channels = lazy(() => import("./pages/Channels").then((m) => ({ default: m.Channels })));
const Conversations = lazy(() => import("./pages/Conversations").then((m) => ({ default: m.Conversations })));
const Handoff = lazy(() => import("./pages/Handoff").then((m) => ({ default: m.Handoff })));
const CannedResponses = lazy(() => import("./pages/CannedResponses").then((m) => ({ default: m.CannedResponses })));
const Accounts = lazy(() => import("./pages/Accounts").then((m) => ({ default: m.Accounts })));
const Tenants = lazy(() => import("./pages/Tenants").then((m) => ({ default: m.Tenants })));

const { Header, Sider, Content } = Layout;

// Centered fallback while a lazy page chunk downloads.
const PageLoading = ({ full }: { full?: boolean }) => (
  <div style={{ height: full ? "100vh" : "60vh", display: "grid", placeItems: "center" }}>
    <Spin size="large" />
  </div>
);

// 坐席工作台已拆成独立站（agent/），后台不再内嵌该页；菜单里保留一个入口，点击在新
// 标签打开独立站。地址由后端 /me 的 agent_console_url 提供（AGENT_CONSOLE_URL 或
// {APP_BASE_URL}/agent），admin 与坐席站可能不同域，不能写相对路径。
const AGENT_MENU_KEY = "__agent_console__";

type NavItem = { key: string; icon: ReactNode; label: string; external?: boolean; adminOnly?: boolean; superOnly?: boolean };

// 导航按「日常运营 → 内容与模型 → 系统」分三组，组内按使用频率排序。
const NAV: { title: string; items: NavItem[] }[] = [
  {
    title: "运营",
    items: [
      { key: "/dashboard", icon: <IconOverview />, label: "概览" },
      { key: "/conversations", icon: <IconConversations />, label: "对话记录" },
      { key: "/handoff", icon: <IconTicket />, label: "转人工工单" },
      { key: AGENT_MENU_KEY, icon: <IconAgentDesk />, label: "坐席工作台", external: true },
    ],
  },
  {
    title: "知识与模型",
    items: [
      { key: "/knowledge", icon: <IconKnowledge />, label: "知识库" },
      { key: "/canned", icon: <IconCanned />, label: "快捷回复" },
      { key: "/ai-config", icon: <IconModel />, label: "AI 配置" },
    ],
  },
  {
    title: "系统",
    items: [
      { key: "/channels", icon: <IconChannel />, label: "渠道配置" },
      { key: "/accounts", icon: <IconAccounts />, label: "账号权限", adminOnly: true },
      { key: "/tenants", icon: <IconTenant />, label: "租户管理", superOnly: true },
    ],
  },
];

const ROLE_LABEL: Record<string, string> = { admin: "管理员", operator: "运营", readonly: "只读" };

function AppLayout() {
  const { user, logout } = useAuth();
  const nav = useNavigate();
  const loc = useLocation();
  const { token } = theme.useToken();
  const { dark, toggle } = useThemeMode();
  const [collapsed, setCollapsed] = useState(false);

  const groups = NAV.map((g) => ({
    ...g,
    items: g.items.filter((m) => (!m.adminOnly || isAdmin(user?.role)) && (!m.superOnly || isSuperAdmin(user))),
  })).filter((g) => g.items.length > 0);
  const toMenuItem = (m: NavItem) => ({
    key: m.key,
    icon: m.icon,
    title: m.label,
    label: m.external ? <>{m.label}<IconExternal className="acs-nav-ext" /></> : m.label,
  });
  // 展开时带分组标题；折叠成图标栏后标题放不下，改用分隔线保留分组。
  const menuItems: MenuProps["items"] = collapsed
    ? groups.flatMap((g, i) => [...(i ? [{ type: "divider" as const, key: `d${i}` }] : []), ...g.items.map(toMenuItem)])
    : groups.map((g) => ({ type: "group" as const, key: g.title, label: g.title, children: g.items.map(toMenuItem) }));

  // 坐席工作台是独立站：点击在新标签打开，不走站内路由。
  const agentUrl = agentConsoleUrl(user);
  const onMenuClick = (key: string) => {
    if (key === AGENT_MENU_KEY) { window.open(agentUrl, "_blank", "noopener,noreferrer"); return; }
    nav(key);
  };
  // Super-admin "act as tenant": a non-empty slug means we're scoped into another tenant.
  const actingAs = isSuperAdmin(user) ? tenantStore.slug : "";

  return (
    <Layout style={{ height: "100vh" }}>
      <Header className="acs-bar">
        <button type="button" className="acs-bar-btn" onClick={() => setCollapsed((c) => !c)} aria-label={collapsed ? "展开导航" : "收起导航"}>
          {collapsed ? <IconMenuUnfold /> : <IconMenuFold />}
        </button>
        <div className="acs-brand">
          <Logo size={24} />
          AI 客服
          <span className="acs-brand-scope">运营后台</span>
        </div>
        <div className="acs-bar-space" />
        <Tooltip title={dark ? "切换到浅色" : "切换到深色"}>
          <button type="button" className="acs-bar-btn" onClick={toggle} aria-label="切换主题">
            {dark ? <IconSun /> : <IconMoon />}
          </button>
        </Tooltip>
        <Dropdown
          trigger={["click"]}
          menu={{ items: [{ key: "logout", icon: <IconLogout />, label: "退出登录", onClick: async () => { await logout(); nav("/login"); } }] }}
        >
          <button type="button" className="acs-user">
            <span className="acs-user-name">{user?.name || user?.email}</span>
            <span className="acs-user-role">{ROLE_LABEL[user?.role || ""] || "只读"}</span>
            <IconCaretDown />
          </button>
        </Dropdown>
      </Header>
      <Layout>
        <Sider
          className="acs-sider"
          theme="light"
          width={208}
          collapsedWidth={56}
          breakpoint="lg"
          trigger={null}
          collapsed={collapsed}
          onBreakpoint={setCollapsed}
          style={{ background: token.colorBgContainer }}
        >
          <Menu
            mode="inline"
            inlineIndent={16}
            selectedKeys={[loc.pathname]}
            items={menuItems}
            onClick={({ key }) => onMenuClick(key)}
          />
        </Sider>
        <Content style={{ overflow: "auto", background: token.colorBgLayout }}>
          {actingAs && (
            <Alert
              banner
              type="warning"
              showIcon
              message={<span>正在以租户 <b>{tenantStore.name || actingAs}</b> 身份查看，所有数据已切换到该租户</span>}
              action={<Button size="small" onClick={() => { tenantStore.set(""); window.location.assign("/tenants"); }}>退出</Button>}
            />
          )}
          <div className="acs-content">
            <Suspense fallback={<PageLoading />}>
              <Routes>
                <Route path="/dashboard" element={<Dashboard />} />
                <Route path="/knowledge" element={<Knowledge />} />
                <Route path="/conversations" element={<Conversations />} />
                <Route path="/handoff" element={<Handoff />} />
                <Route path="/canned" element={<CannedResponses />} />
                <Route path="/ai-config" element={<AIConfig />} />
                <Route path="/channels" element={<Channels />} />
                <Route path="/accounts" element={isAdmin(user?.role) ? <Accounts /> : <Navigate to="/dashboard" />} />
                <Route path="/tenants" element={isSuperAdmin(user) ? <Tenants /> : <Navigate to="/dashboard" />} />
                <Route path="*" element={<Navigate to="/dashboard" />} />
              </Routes>
            </Suspense>
          </div>
        </Content>
      </Layout>
    </Layout>
  );
}

export function App() {
  const { user, loading } = useAuth();
  const loc = useLocation();

  if (loading) {
    return <div style={{ height: "100vh", display: "grid", placeItems: "center" }}><Spin size="large" /></div>;
  }
  if (!user) {
    return (
      <Suspense fallback={<PageLoading full />}>
        <Routes>
          <Route path="/login" element={<Login />} />
          <Route path="*" element={<Navigate to="/login" state={{ from: loc.pathname + loc.search }} />} />
        </Routes>
      </Suspense>
    );
  }
  return <AppLayout />;
}
