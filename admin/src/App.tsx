import { Navigate, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { Layout, Menu, Spin, Dropdown, Avatar, Tag } from "antd";
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
} from "@ant-design/icons";
import { useAuth, isAdmin } from "./auth";
import { Login } from "./pages/Login";
import { Dashboard } from "./pages/Dashboard";
import { Knowledge } from "./pages/Knowledge";
import { AIConfig } from "./pages/AIConfig";
import { Channels } from "./pages/Channels";
import { Conversations } from "./pages/Conversations";
import { Handoff } from "./pages/Handoff";
import { Accounts } from "./pages/Accounts";

const { Header, Sider, Content } = Layout;

const MENU = [
  { key: "/dashboard", icon: <DashboardOutlined />, label: "概览" },
  { key: "/knowledge", icon: <BookOutlined />, label: "知识库" },
  { key: "/conversations", icon: <MessageOutlined />, label: "对话记录" },
  { key: "/handoff", icon: <CustomerServiceOutlined />, label: "转人工" },
  { key: "/ai-config", icon: <RobotOutlined />, label: "AI 配置" },
  { key: "/channels", icon: <ApiOutlined />, label: "渠道配置" },
  { key: "/accounts", icon: <TeamOutlined />, label: "账号权限", adminOnly: true },
];

function AppLayout() {
  const { user, logout } = useAuth();
  const nav = useNavigate();
  const loc = useLocation();
  const items = MENU.filter((m) => !m.adminOnly || isAdmin(user?.role));

  return (
    <Layout style={{ height: "100vh" }}>
      <Sider theme="light" width={208} style={{ borderRight: "1px solid #f0f0f0" }}>
        <div style={{ height: 56, display: "flex", alignItems: "center", padding: "0 20px", fontWeight: 700, fontSize: 16, color: "#4f46e5" }}>
          🤖 AI 客服后台
        </div>
        <Menu
          mode="inline"
          selectedKeys={[loc.pathname]}
          items={items.map((m) => ({ key: m.key, icon: m.icon, label: m.label }))}
          onClick={({ key }) => nav(key)}
        />
      </Sider>
      <Layout>
        <Header style={{ background: "#fff", display: "flex", justifyContent: "flex-end", alignItems: "center", padding: "0 24px", borderBottom: "1px solid #f0f0f0" }}>
          <Dropdown
            menu={{ items: [{ key: "logout", icon: <LogoutOutlined />, label: "退出登录", onClick: async () => { await logout(); nav("/login"); } }] }}
          >
            <span style={{ cursor: "pointer", display: "flex", alignItems: "center", gap: 8 }}>
              <Avatar size="small" icon={<UserOutlined />} style={{ background: "#4f46e5" }} />
              {user?.name || user?.email}
              <Tag color={isAdmin(user?.role) ? "purple" : user?.role === "operator" ? "blue" : "default"}>
                {user?.role === "admin" ? "管理员" : user?.role === "operator" ? "运营" : "只读"}
              </Tag>
            </span>
          </Dropdown>
        </Header>
        <Content style={{ overflow: "auto", background: "#f5f6fa" }}>
          <div className="acs-content">
            <Routes>
              <Route path="/dashboard" element={<Dashboard />} />
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
