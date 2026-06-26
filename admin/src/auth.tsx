import { createContext, useContext, useEffect, useState, ReactNode } from "react";
import { authApi } from "./api";
import { tokenStore, tenantStore } from "./api/client";

interface User {
  id: string;
  email: string;
  name: string;
  role: "admin" | "operator" | "readonly";
  is_super_admin?: boolean;
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
    tenantStore.set((tenant || "").trim()); // route this login (and later requests) to the tenant
    const u = await authApi.login(email, password);
    setUser(u);
  };
  const logout = async () => {
    await authApi.logout();
    tenantStore.set(""); // drop any super-admin "act as tenant" scope
    setUser(null);
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
