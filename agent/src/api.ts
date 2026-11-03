import axios, { AxiosError, AxiosRequestConfig } from "axios";

// 与 admin 站隔离的 localStorage 键（同域部署时互不干扰）。
const ACCESS_KEY = "acs_agent_access";
const REFRESH_KEY = "acs_agent_refresh";

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
  return cfg;
});

let refreshing: Promise<string> | null = null;

/** Shared with other tabs via localStorage: if our (single-use) refresh is rejected but
 *  storage already holds a newer pair, another tab rotated it — adopt that instead. */
async function doRefresh(): Promise<string> {
  const refresh = tokenStore.refresh;
  if (!refresh) throw new Error("no refresh token");
  try {
    const resp = await axios.post("/api/admin/auth/refresh", { refresh_token: refresh });
    tokenStore.set(resp.data.access_token, resp.data.refresh_token);
    return resp.data.access_token;
  } catch (e) {
    if (isAuthRejection(e) && tokenStore.refresh && tokenStore.refresh !== refresh && tokenStore.access) {
      return tokenStore.access;
    }
    throw e;
  }
}

const isAuthRejection = (e: unknown) => {
  const status = (e as AxiosError)?.response?.status;
  return status === 401 || status === 403;
};

/** The session is over: clear tokens AND tell the auth context (via an event) so `user`
 *  resets — otherwise the login page sees a stale user, bounces straight back into the app,
 *  and every request 401s in a loop. */
export const AUTH_EXPIRED_EVENT = "acs:auth-expired";
function endSession() {
  tokenStore.clear();
  window.dispatchEvent(new Event(AUTH_EXPIRED_EVENT));
  if (!location.hash.startsWith("#/login")) location.hash = "#/login"; // HashRouter
}

api.interceptors.response.use(
  (r) => r,
  async (error: AxiosError) => {
    const original = error.config as AxiosRequestConfig & { _retry?: boolean };
    if (error.response?.status !== 401 || !original) return Promise.reject(error);
    if (!tokenStore.refresh) {
      endSession(); // nothing to refresh with
      return Promise.reject(error);
    }
    if (original._retry) return Promise.reject(error);
    original._retry = true;
    const sent = String(original.headers?.Authorization || "");
    const current = tokenStore.access;
    if (current && sent && sent !== `Bearer ${current}`) {
      // another tab refreshed while this request was in flight
      original.headers = { ...original.headers, Authorization: `Bearer ${current}` };
      return api(original);
    }
    try {
      if (!refreshing) refreshing = doRefresh().finally(() => (refreshing = null));
      const newToken = await refreshing;
      original.headers = { ...original.headers, Authorization: `Bearer ${newToken}` };
      return api(original);
    } catch (refreshErr) {
      // Only a definitive rejection ends the session; a network blip / 5xx keeps the tokens.
      if (isAuthRejection(refreshErr)) endSession();
      return Promise.reject(error);
    }
  }
);

export function apiError(e: unknown): string {
  const err = e as AxiosError<{ detail?: string }>;
  return err.response?.data?.detail || err.message || "请求失败";
}

// ---------------- 本站用到的接口封装 ----------------

export const authApi = {
  async login(email: string, password: string) {
    const { data } = await api.post("/auth/login", { email, password });
    tokenStore.set(data.access_token, data.refresh_token);
    return data.user;
  },
  me: () => api.get("/auth/me").then((r) => r.data),
  async logout() {
    try {
      await api.post("/auth/logout");
    } finally {
      tokenStore.clear();
    }
  },
};

/** 后端 attachment_descriptor 的形状（坐席上传与客户上传一致）。 */
export type Attachment = {
  url: string;
  key: string;
  name: string;
  content_type: string;
  size: number;
  kind: "image" | "file";
};

/** 与后端 storage.ALLOWED_UPLOAD_TYPES / MAX_UPLOAD_BYTES 保持一致。 */
export const UPLOAD_ACCEPT =
  "image/png,image/jpeg,image/webp,image/gif,application/pdf,text/plain,text/csv," +
  "application/msword,application/vnd.openxmlformats-officedocument.wordprocessingml.document," +
  "application/vnd.ms-excel,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet";
export const MAX_UPLOAD_BYTES = 10 * 1024 * 1024;
export const MAX_ATTACHMENTS = 5;

export const conversationApi = {
  list: (params: any) => api.get("/conversations", { params }).then((r) => r.data),
  // 契约 A：{waiting, mine, takeover, all}
  queueCounts: () => api.get("/conversations/queue-counts").then((r) => r.data),
  detail: (id: string) => api.get(`/conversations/${id}`).then((r) => r.data),
  takeover: (id: string) => api.post(`/conversations/${id}/takeover`).then((r) => r.data),
  reply: (id: string, content: string, attachments: Attachment[] = []) =>
    api.post(`/conversations/${id}/reply`, { content, attachments }).then((r) => r.data),
  upload: (id: string, file: File): Promise<Attachment> => {
    const fd = new FormData();
    fd.append("file", file);
    return api.post(`/conversations/${id}/upload`, fd).then((r) => r.data);
  },
  release: (id: string, resumeAi: boolean) =>
    api.post(`/conversations/${id}/release`, { resume_ai: resumeAi }).then((r) => r.data),
  markHandled: (id: string) => api.post(`/conversations/${id}/mark-handled`).then((r) => r.data),
};

export const handoffApi = {
  tickets: (params: any) => api.get("/handoff/tickets", { params }).then((r) => r.data),
  // 契约 A：仅 open 且未分配可认领（同人幂等 200）；他人已认领 → 409 detail="已被 {姓名} 认领"
  claim: (ticketId: string) => api.post(`/handoff/tickets/${ticketId}/claim`).then((r) => r.data),
  // 契约 A：仅本人或 admin 可退回，status→open、assignee 清空
  unclaim: (ticketId: string) => api.post(`/handoff/tickets/${ticketId}/unclaim`).then((r) => r.data),
};

export const cannedApi = {
  list: (q = "") => api.get("/canned", { params: { q } }).then((r) => r.data),
};
