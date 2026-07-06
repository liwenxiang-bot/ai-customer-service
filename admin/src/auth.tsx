import { createContext, useContext, useEffect, useState, ReactNode } from "react";
import { authApi } from "./api";
import { tokenStore, tenantStore } from "./api/client";

interface User {
  id: string;
  email: string;
  name: string;
  role: "admin" | "operator" | "readonly";
  is_super_admin?: boolean;
  agent_console_url?: string; // 独立坐席工作台地址（后端按 AGENT_CONSOLE_URL 解析）
}

interface AuthCtx {
  user: User | null;
  loading: boolean;
  login: (email: string, password: string, tenant?: string) => Promise<void>;
  logout: () => Promise<void>;
}

const Ctx = createContext<AuthCtx>(null as any);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    // 向后兼容：旧版「记住登录」曾把 base64 密码存进 localStorage，启动时清掉密码字段（只保留邮箱/租户）。
    try {
      const saved = localStorage.getItem("acs_login_remember");
      if (saved) {
        const { email, password, tenant } = JSON.parse(saved);
        if (password) localStorage.setItem("acs_login_remember", JSON.stringify({ email, tenant: tenant || "" }));
      }
    } catch { /* ignore corrupt value */ }
    (async () => {
      if (tokenStore.access) {
        try {
          setUser(await authApi.me());
        } catch {
          tokenStore.clear();
        }
      }
      setLoading(false);
    })();
  }, []);

  const login = async (email: string, password: string, tenant?: string) => {
    // 先写 store 是必须的：登录请求本身靠 X-Tenant-Slug 头路由到目标租户。
    // 但失败要回滚，否则一次错误尝试就清掉代管遗留的租户上下文，
    // 租户字段随之从登录页永久消失（无 UI 入口可恢复）。
    const prevSlug = tenantStore.slug;
    const prevName = tenantStore.name;
    tenantStore.set((tenant || "").trim());
    try {
      const u = await authApi.login(email, password);
      setUser(u);
    } catch (e) {
      tenantStore.set(prevSlug, prevName || undefined);
      throw e;
    }
  };
  const logout = async () => {
    try {
      await authApi.logout(); // 内部 finally 已清 token
    } catch {
      // 登出接口失败（断网/5xx）不应阻断本地登出，吞掉异常让调用方继续跳转登录页
    } finally {
      tenantStore.set(""); // drop any super-admin "act as tenant" scope
      setUser(null);
    }
  };

  return <Ctx.Provider value={{ user, loading, login, logout }}>{children}</Ctx.Provider>;
}

export const useAuth = () => useContext(Ctx);

// Role helper for conditional UI.
const RANK = { readonly: 0, operator: 1, admin: 2 };
export function canEdit(role?: string) {
  return RANK[(role as keyof typeof RANK) ?? "readonly"] >= RANK.operator;
}
export function isAdmin(role?: string) {
  return role === "admin";
}
export function isSuperAdmin(user?: { is_super_admin?: boolean } | null) {
  return !!user?.is_super_admin;
}

// ---- 坐席工作台（独立站）入口 ----
// 后端 /me 返回 agent_console_url（AGENT_CONSOLE_URL 或 {APP_BASE_URL}/agent）。admin 与
// 坐席站可能不同域，必须用后端给的绝对地址；未取到时回退相对 /agent 兜底。
type WithAgentUrl = { agent_console_url?: string } | null | undefined;
export function agentConsoleUrl(user: WithAgentUrl): string {
  return user?.agent_console_url || "/agent";
}
/** 坐席站会话深链（HashRouter /s/:id）：从工单/概览一键跳进某会话接管。 */
export function agentSessionLink(user: WithAgentUrl, sessionId: string): string {
  return `${agentConsoleUrl(user)}/#/s/${sessionId}`;
}
