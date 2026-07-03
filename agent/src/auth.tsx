import { createContext, useContext, useEffect, useState, ReactNode } from "react";
import { authApi, tokenStore } from "./api";

export interface User {
  id: string;
  email: string;
  name: string;
  role: "admin" | "operator" | "readonly";
}

interface AuthCtx {
  user: User | null;
  loading: boolean;
  login: (email: string, password: string) => Promise<void>;
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

  const login = async (email: string, password: string) => {
    setUser(await authApi.login(email, password));
  };
  const logout = async () => {
    try {
      await authApi.logout(); // 内部 finally 已清 token
    } catch {
      // 登出接口失败（断网/5xx）不阻断本地登出
    } finally {
      setUser(null);
    }
  };

  return <Ctx.Provider value={{ user, loading, login, logout }}>{children}</Ctx.Provider>;
}

export const useAuth = () => useContext(Ctx);
