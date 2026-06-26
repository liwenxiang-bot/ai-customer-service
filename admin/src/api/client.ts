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

async function doRefresh(): Promise<string> {
  const refresh = tokenStore.refresh;
  if (!refresh) throw new Error("no refresh token");
  const resp = await axios.post(
    "/api/admin/auth/refresh",
    { refresh_token: refresh },
    tenantStore.slug ? { headers: { "X-Tenant-Slug": tenantStore.slug } } : undefined,
  );
  tokenStore.set(resp.data.access_token, resp.data.refresh_token);
  return resp.data.access_token;
}

api.interceptors.response.use(
  (r) => r,
  async (error: AxiosError) => {
    const original = error.config as AxiosRequestConfig & { _retry?: boolean };
    if (error.response?.status === 401 && original && !original._retry && tokenStore.refresh) {
      original._retry = true;
      try {
        if (!refreshing) refreshing = doRefresh().finally(() => (refreshing = null));
        const newToken = await refreshing;
        original.headers = { ...original.headers, Authorization: `Bearer ${newToken}` };
        return api(original);
      } catch {
        tokenStore.clear();
        if (location.pathname !== "/login") location.href = "/login";
      }
    }
    return Promise.reject(error);
  }
);

export function apiError(e: unknown): string {
  const err = e as AxiosError<{ detail?: string }>;
  return err.response?.data?.detail || err.message || "请求失败";
}
