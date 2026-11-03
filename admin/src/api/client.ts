import axios, { AxiosError, AxiosRequestConfig } from "axios";

const ACCESS_KEY = "acs_admin_access";
const REFRESH_KEY = "acs_admin_refresh";
const TENANT_KEY = "acs_admin_tenant";
const TENANT_NAME_KEY = "acs_admin_tenant_name";

// Tenant slug for multi-tenant deploys; blank → the default tenant (single-tenant).
// A super-admin sets this to "act as" a tenant (the backend honours X-Tenant-Slug only for
// the signed `super` claim); `name` is kept purely to label the acting-as banner.
export const tenantStore = {
  get slug() {
    return localStorage.getItem(TENANT_KEY) || "";
  },
  get name() {
    return localStorage.getItem(TENANT_NAME_KEY) || "";
  },
  set(slug: string, name?: string) {
    if (slug) {
      localStorage.setItem(TENANT_KEY, slug);
      if (name) localStorage.setItem(TENANT_NAME_KEY, name);
      else localStorage.removeItem(TENANT_NAME_KEY);
    } else {
      localStorage.removeItem(TENANT_KEY);
      localStorage.removeItem(TENANT_NAME_KEY);
    }
  },
};

export const tokenStore = {
  get access() {
    return localStorage.getItem(ACCESS_KEY) || "";
  },
  get refresh() {
    return localStorage.getItem(REFRESH_KEY) || "";
  },
  set(access: string, refresh: string) {
    localStorage.setItem(ACCESS_KEY, access);
    localStorage.setItem(REFRESH_KEY, refresh);
  },
  clear() {
    localStorage.removeItem(ACCESS_KEY);
    localStorage.removeItem(REFRESH_KEY);
  },
};

export const api = axios.create({ baseURL: "/api/admin", timeout: 60000 });

api.interceptors.request.use((cfg) => {
  const t = tokenStore.access;
  if (t) cfg.headers.Authorization = `Bearer ${t}`;
  if (tenantStore.slug) cfg.headers["X-Tenant-Slug"] = tenantStore.slug;
  return cfg;
});

let refreshing: Promise<string> | null = null;

/** Refresh tokens are single-use and rotate. Several tabs share them via localStorage, so
 *  another tab may rotate the pair while this one's request is in flight: if our refresh is
 *  rejected but storage already holds a NEWER pair, adopt it instead of logging out. */
async function doRefresh(): Promise<string> {
  const refresh = tokenStore.refresh;
  if (!refresh) throw new Error("no refresh token");
  try {
    const resp = await axios.post(
      "/api/admin/auth/refresh",
      { refresh_token: refresh },
      tenantStore.slug ? { headers: { "X-Tenant-Slug": tenantStore.slug } } : undefined,
    );
    tokenStore.set(resp.data.access_token, resp.data.refresh_token);
    return resp.data.access_token;
  } catch (e) {
    const rotatedElsewhere = tokenStore.refresh && tokenStore.refresh !== refresh && tokenStore.access;
    if (isAuthRejection(e) && rotatedElsewhere) return tokenStore.access;
    throw e;
  }
}

const isAuthRejection = (e: unknown) => {
  const status = (e as AxiosError)?.response?.status;
  return status === 401 || status === 403;
};

api.interceptors.response.use(
  (r) => r,
  async (error: AxiosError) => {
    const original = error.config as AxiosRequestConfig & { _retry?: boolean };
    if (error.response?.status === 401 && original && !original._retry && tokenStore.refresh) {
      original._retry = true;
      // Another tab already refreshed since this request was sent: just retry with that token.
      const sent = String(original.headers?.Authorization || "");
      const current = tokenStore.access;
      if (current && sent && sent !== `Bearer ${current}`) {
        original.headers = { ...original.headers, Authorization: `Bearer ${current}` };
        return api(original);
      }
      try {
        if (!refreshing) refreshing = doRefresh().finally(() => (refreshing = null));
        const newToken = await refreshing;
        original.headers = { ...original.headers, Authorization: `Bearer ${newToken}` };
        return api(original);
      } catch (refreshErr) {
        // Only a definitive rejection ends the session. A network blip / 5xx / timeout while
        // refreshing keeps the tokens so the next action can simply retry.
        if (isAuthRejection(refreshErr)) {
          tokenStore.clear();
          if (location.pathname !== "/login") {
            const back = location.pathname + location.search;
            location.href = `/login?from=${encodeURIComponent(back)}`;
          }
        }
      }
    }
    return Promise.reject(error);
  }
);

export function apiError(e: unknown): string {
  const err = e as AxiosError<{ detail?: unknown }>;
  const detail = err.response?.data?.detail;
  // FastAPI 422 校验错误的 detail 是对象数组：直接塞给 message.error 会因
  // 「Objects are not valid as a React child」静默不显示，这里拼成中文可读句
  if (Array.isArray(detail)) {
    const first: any = detail[0] ?? {};
    const loc = Array.isArray(first.loc) ? first.loc.filter((p: any) => p !== "body").join(".") : "";
    const msg = first.msg || "参数不合法";
    return `参数校验失败${loc ? `（${loc}）` : ""}：${msg}，请修正后重试`;
  }
  if (typeof detail === "string" && detail) return detail;
  // 结构化 detail（如查重 409 的 {message, item_id}）：通用调用方只展示 message
  if (detail && typeof detail === "object" && typeof (detail as any).message === "string") {
    return (detail as any).message;
  }
  if (detail != null) return JSON.stringify(detail);
  if (err.code === "ECONNABORTED" || /timeout/i.test(err.message || "")) return "请求超时，请稍后重试";
  if (err.message === "Network Error") return "网络异常，请检查网络连接后重试";
  return err.message || "请求失败";
}
