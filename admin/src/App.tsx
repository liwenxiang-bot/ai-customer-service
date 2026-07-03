import { Suspense, lazy, useState } from "react";
import { Navigate, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { Layout, Menu, Spin, Dropdown, Avatar, Tag, Button, Tooltip, Alert, theme } from "antd";
import {
  DashboardOutlined,
  BookOutlined,
  RobotOutlined,
  ApiOutlined,
  MessageOutlined,
  CustomerServiceOutlined,
  TeamOutlined,
  LogoutOutlined,
  UserOutlined,
  BellOutlined,
  BulbOutlined,
  ThunderboltOutlined,
  ApartmentOutlined,
} from "@ant-design/icons";
import { useAuth, isAdmin, isSuperAdmin } from "./auth";
import { tenantStore } from "./api/client";
import { useThemeMode } from "./theme";

// Route-level code splitting: each page loads on demand (pages use named exports).
const Login = lazy(() => import("./pages/Login").then((m) => ({ default: m.Login })));
const Dashboard = lazy(() => import("./pages/Dashboard").then((m) => ({ default: m.Dashboard })));
const Workbench = lazy(() => import("./pages/Workbench").then((m) => ({ default: m.Workbench })));
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

const MENU = [
  { key: "/dashboard", icon: <DashboardOutlined />, label: "概览" },
  { key: "/workbench", icon: <CustomerServiceOutlined />, label: "坐席工作台" },
  { key: "/conversations", icon: <MessageOutlined />, label: "对话记录" },
  { key: "/knowledge", icon: <BookOutlined />, label: "知识库" },
  { key: "/handoff", icon: <BellOutlined />, label: "转人工工单" },
  { key: "/canned", icon: <ThunderboltOutlined />, label: "快捷回复" },
  { key: "/ai-config", icon: <RobotOutlined />, label: "AI 配置" },
  { key: "/channels", icon: <ApiOutlined />, label: "渠道配置" },
  { key: "/accounts", icon: <TeamOutlined />, label: "账号权限", adminOnly: true },
  { key: "/tenants", icon: <ApartmentOutlined />, label: "租户管理", superOnly: true },
];

function AppLayout() {
  const { user, logout } = useAuth();
  const nav = useNavigate();
  const loc = useLocation();
  const { token } = theme.useToken();
  const { dark, toggle } = useThemeMode();
  const [collapsed, setCollapsed] = useState(false);
  const items = MENU.filter(
    (m) => (!m.adminOnly || isAdmin(user?.role)) && (!m.superOnly || isSuperAdmin(user))
  );
  // Super-admin "act as tenant": a non-empty slug means we're scoped into another tenant.
  const actingAs = isSuperAdmin(user) ? tenantStore.slug : "";

  return (
    <Layout style={{ height: "100vh" }}>
      <Sider theme={dark ? "dark" : "light"} width={200} breakpoint="lg" collapsible collapsed={collapsed} onCollapse={setCollapsed} style={{ borderRight: `1px solid ${token.colorBorderSecondary}` }}>
        <div style={{ height: 56, display: "flex", alignItems: "center", justifyContent: collapsed ? "center" : "flex-start", gap: 8, padding: collapsed ? 0 : "0 20px", fontWeight: 700, fontSize: 16, color: dark ? "#2DD4BF" : "#0F766E", whiteSpace: "nowrap", overflow: "hidden" }}>
          <RobotOutlined style={{ fontSize: 18 }} />{!collapsed && "AI 客服后台"}
        </div>
        <Menu
          mode="inline"
          style={{ borderInlineEnd: "none" }}
          selectedKeys={[loc.pathname]}
          items={items.map((m) => ({ key: m.key, icon: m.icon, label: m.label }))}
          onClick={({ key }) => nav(key)}
        />
      </Sider>
      <Layout>
        <Header style={{ background: token.colorBgContainer, display: "flex", justifyContent: "flex-end", alignItems: "center", gap: 4, padding: "0 16px", borderBottom: `1px solid ${token.colorBorderSecondary}` }}>
          <Tooltip title={dark ? "切换到浅色" : "切换到深色"}>
            <Button type="text" icon={<BulbOutlined />} onClick={toggle} aria-label="切换主题" />
          </Tooltip>
          <Dropdown
            menu={{ items: [{ key: "logout", icon: <LogoutOutlined />, label: "退出登录", onClick: async () => { await logout(); nav("/login"); } }] }}
          >
            <span style={{ cursor: "pointer", display: "flex", alignItems: "center", gap: 8, marginLeft: 8, minWidth: 0 }}>
              <Avatar size="small" icon={<UserOutlined />} style={{ background: "#0F766E" }} />
              {/* Truncate long names on narrow screens instead of wrapping the header. */}
              <span style={{ maxWidth: 160, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{user?.name || user?.email}</span>
              <Tag color={isAdmin(user?.role) ? "purple" : user?.role === "operator" ? "blue" : "default"}>
                {user?.role === "admin" ? "管理员" : user?.role === "operator" ? "运营" : "只读"}
              </Tag>
            </span>
          </Dropdown>
        </Header>
        <Content style={{ overflow: "auto", background: token.colorBgLayout }}>
          {actingAs && (
            <Alert
              banner
              type="warning"
              showIcon
              message={<span>正在以租户 <b>{tenantStore.name || actingAs}</b> 身份查看 —— 所有数据已切换到该租户</span>}
              action={<Button size="small" onClick={() => { tenantStore.set(""); window.location.assign("/tenants"); }}>退出</Button>}
            />
          )}
          <div className="acs-content">
            <Suspense fallback={<PageLoading />}>
              <Routes>
                <Route path="/dashboard" element={<Dashboard />} />
                <Route path="/workbench" element={<Workbench />} />
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
          <Route path="*" element={<Navigate to="/login" state={{ from: loc.pathname }} />} />
        </Routes>
      </Suspense>
    );
  }
  return <AppLayout />;
}
