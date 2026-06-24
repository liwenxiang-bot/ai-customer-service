import { useState } from "react";
import { Navigate, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { Layout, Menu, Spin, Dropdown, Avatar, Tag, Button, Tooltip, theme } from "antd";
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
} from "@ant-design/icons";
import { useAuth, isAdmin } from "./auth";
import { useThemeMode } from "./theme";
import { Login } from "./pages/Login";
import { Dashboard } from "./pages/Dashboard";
import { Workbench } from "./pages/Workbench";
import { Knowledge } from "./pages/Knowledge";
import { AIConfig } from "./pages/AIConfig";
import { Channels } from "./pages/Channels";
import { Conversations } from "./pages/Conversations";
import { Handoff } from "./pages/Handoff";
import { Accounts } from "./pages/Accounts";

const { Header, Sider, Content } = Layout;

const MENU = [
  { key: "/dashboard", icon: <DashboardOutlined />, label: "概览" },
  { key: "/workbench", icon: <CustomerServiceOutlined />, label: "坐席工作台" },
  { key: "/conversations", icon: <MessageOutlined />, label: "对话记录" },
  { key: "/knowledge", icon: <BookOutlined />, label: "知识库" },
  { key: "/handoff", icon: <BellOutlined />, label: "转人工工单" },
  { key: "/ai-config", icon: <RobotOutlined />, label: "AI 配置" },
  { key: "/channels", icon: <ApiOutlined />, label: "渠道配置" },
  { key: "/accounts", icon: <TeamOutlined />, label: "账号权限", adminOnly: true },
];

function AppLayout() {
  const { user, logout } = useAuth();
  const nav = useNavigate();
  const loc = useLocation();
  const { token } = theme.useToken();
  const { dark, toggle } = useThemeMode();
  const [collapsed, setCollapsed] = useState(false);
  const items = MENU.filter((m) => !m.adminOnly || isAdmin(user?.role));

  return (
    <Layout style={{ height: "100vh" }}>
      <Sider theme={dark ? "dark" : "light"} width={200} collapsible collapsed={collapsed} onCollapse={setCollapsed} style={{ borderRight: `1px solid ${token.colorBorderSecondary}` }}>
        <div style={{ height: 56, display: "flex", alignItems: "center", justifyContent: collapsed ? "center" : "flex-start", gap: 8, padding: collapsed ? 0 : "0 20px", fontWeight: 700, fontSize: 16, color: dark ? "#2DD4BF" : "#0F766E", whiteSpace: "nowrap", overflow: "hidden" }}>
          <RobotOutlined style={{ fontSize: 18 }} />{!collapsed && "AI 客服后台"}
        </div>
        <Menu
          mode="inline"
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
            <span style={{ cursor: "pointer", display: "flex", alignItems: "center", gap: 8, marginLeft: 8 }}>
              <Avatar size="small" icon={<UserOutlined />} style={{ background: "#0F766E" }} />
              {user?.name || user?.email}
              <Tag color={isAdmin(user?.role) ? "purple" : user?.role === "operator" ? "blue" : "default"}>
                {user?.role === "admin" ? "管理员" : user?.role === "operator" ? "运营" : "只读"}
              </Tag>
            </span>
          </Dropdown>
        </Header>
        <Content style={{ overflow: "auto", background: token.colorBgLayout }}>
          <div className="acs-content">
            <Routes>
              <Route path="/dashboard" element={<Dashboard />} />
              <Route path="/workbench" element={<Workbench />} />
              <Route path="/knowledge" element={<Knowledge />} />
              <Route path="/conversations" element={<Conversations />} />
              <Route path="/handoff" element={<Handoff />} />
              <Route path="/ai-config" element={<AIConfig />} />
              <Route path="/channels" element={<Channels />} />
              <Route path="/accounts" element={isAdmin(user?.role) ? <Accounts /> : <Navigate to="/dashboard" />} />
              <Route path="*" element={<Navigate to="/dashboard" />} />
            </Routes>
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
      <Routes>
        <Route path="/login" element={<Login />} />
        <Route path="*" element={<Navigate to="/login" state={{ from: loc.pathname }} />} />
      </Routes>
    );
  }
  return <AppLayout />;
}
