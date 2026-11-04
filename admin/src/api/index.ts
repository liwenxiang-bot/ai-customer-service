import { api, tokenStore } from "./client";

// ---- Auth ----
export const authApi = {
  async login(email: string, password: string) {
    const { data } = await api.post("/auth/login", { email, password });
    tokenStore.set(data.access_token, data.refresh_token);
    return data.user;
  },
  async me() {
    return (await api.get("/auth/me")).data;
  },
  async logout() {
    try {
      await api.post("/auth/logout");
    } finally {
      tokenStore.clear();
    }
  },
};

// ---- Dashboard ----
export const dashboardApi = {
  overview: () => api.get("/dashboard/overview").then((r) => r.data),
  trend: (days = 14) => api.get("/dashboard/trend", { params: { days } }).then((r) => r.data),
  analytics: (days = 14) => api.get("/dashboard/analytics", { params: { days } }).then((r) => r.data),
};

// ---- Knowledge ----
export const knowledgeApi = {
  list: (params: any) => api.get("/knowledge", { params }).then((r) => r.data),
  categories: () => api.get("/knowledge/categories").then((r) => r.data),
  tags: () => api.get("/knowledge/tags").then((r) => r.data),
  get: (id: string) => api.get(`/knowledge/${id}`).then((r) => r.data),
  create: (body: any) => api.post("/knowledge", body).then((r) => r.data),
  update: (id: string, body: any) => api.put(`/knowledge/${id}`, body).then((r) => r.data),
  remove: (id: string) => api.delete(`/knowledge/${id}`).then((r) => r.data),
  versions: (id: string, full = false) =>
    api.get(`/knowledge/${id}/versions`, { params: full ? { full: true } : {} }).then((r) => r.data),
  rollback: (id: string, vid: string) => api.post(`/knowledge/${id}/rollback/${vid}`).then((r) => r.data),
  testRetrieval: (query: string) => api.post("/knowledge/test-retrieval", { query }).then((r) => r.data),
  embeddingStatus: () => api.get("/knowledge/embedding/status").then((r) => r.data),
  itemChunks: (id: string) => api.get(`/knowledge/${id}/chunks`).then((r) => r.data),
  reembedItem: (id: string) => api.post(`/knowledge/${id}/reembed`).then((r) => r.data),
  reembedPending: () => api.post("/knowledge/reembed-pending").then((r) => r.data),
  reviewList: (status = "pending") => api.get("/knowledge/review/list", { params: { status } }).then((r) => r.data),
  reviewApprove: (id: string, body: any) => api.post(`/knowledge/review/${id}/approve`, body).then((r) => r.data),
  reviewReject: (id: string) => api.post(`/knowledge/review/${id}/reject`).then((r) => r.data),
  // CSV/JSON → one item per row; md/txt/html/docx/pdf → one item per heading section.
  // opts (category/status/tags) only apply to document files.
  importFile: (file: File, dryRun = false, opts: { category?: string; status?: string; tags?: string } = {}) => {
    const fd = new FormData();
    fd.append("file", file);
    const params: Record<string, string | boolean> = {};
    if (dryRun) params.dry_run = true;
    if (opts.category) params.category = opts.category;
    if (opts.status) params.status = opts.status;
    if (opts.tags) params.tags = opts.tags;
    return api.post("/knowledge/import", fd, { params }).then((r) => r.data);
  },
  // One page, or a same-site crawl under it (≤50 pages); re-importing a URL updates changed sections.
  importUrl: (body: { url: string; crawl?: boolean; max_pages?: number; category?: string; status?: string; tags?: string[]; dry_run?: boolean }) =>
    api.post("/knowledge/import-url", body).then((r) => r.data),
  // Authenticated blob download — a bare <a href> can't carry the Bearer header.
  importTemplate: () => api.get("/knowledge/import/template", { responseType: "blob" }).then((r) => r.data),
  exportItems: (params: any) => api.get("/knowledge/export", { params, responseType: "blob" }),
  usageStats: () => api.get("/knowledge/usage-stats").then((r) => r.data),
  // 未命中问题：知识库答不上的客户问题，按语义归并（补知识的工作台）
  gaps: (days = 30) => api.get("/knowledge/gaps", { params: { days } }).then((r) => r.data),
  gapIgnore: (question: string) => api.post("/knowledge/gaps/ignore", { question }).then((r) => r.data),
  renameCategory: (fromName: string, toName: string) =>
    api.post("/knowledge/category/rename", { from_name: fromName, to_name: toName }).then((r) => r.data),
  rebuildAll: () => api.post("/knowledge/rebuild").then((r) => r.data),
  reviewCount: () => api.get("/knowledge/review/count").then((r) => r.data),
};

// ---- AI config ----
export const aiConfigApi = {
  get: () => api.get("/ai-config").then((r) => r.data),
  update: (body: any) => api.put("/ai-config", body).then((r) => r.data),
  testLLM: (message: string) => api.post("/ai-config/test-llm", { message }).then((r) => r.data),
  rebuildStatus: () => api.get("/ai-config/rebuild-status").then((r) => r.data),
  rebuild: () => api.post("/ai-config/rebuild").then((r) => r.data),
};

// ---- Channels ----
export const channelApi = {
  getWeb: () => api.get("/channels/web").then((r) => r.data),
  updateWeb: (body: any) => api.put("/channels/web", body).then((r) => r.data),
  getNotify: () => api.get("/channels/notify").then((r) => r.data),
  updateNotify: (body: any) => api.put("/channels/notify", body).then((r) => r.data),
  getWeChat: () => api.get("/channels/wechat").then((r) => r.data),
  updateWeChat: (body: any) => api.put("/channels/wechat", body).then((r) => r.data),
};

// ---- Conversations ----
export const conversationApi = {
  list: (params: any) => api.get("/conversations", { params }).then((r) => r.data),
  queueCounts: () => api.get("/conversations/queue-counts").then((r) => r.data),
  exportCsv: async (params: any) => {
    const r = await api.get("/conversations/export", { params, responseType: "blob" });
    const url = URL.createObjectURL(r.data as Blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "conversations.csv";
    a.click();
    URL.revokeObjectURL(url);
  },
  detail: (id: string) => api.get(`/conversations/${id}`).then((r) => r.data),
  markHandled: (id: string) => api.post(`/conversations/${id}/mark-handled`).then((r) => r.data),
  toKnowledge: (id: string, body: any) => api.post(`/conversations/${id}/to-knowledge`, body).then((r) => r.data),
  takeover: (id: string) => api.post(`/conversations/${id}/takeover`).then((r) => r.data),
  reply: (id: string, content: string) => api.post(`/conversations/${id}/reply`, { content }).then((r) => r.data),
  release: (id: string, resumeAi: boolean) => api.post(`/conversations/${id}/release`, { resume_ai: resumeAi }).then((r) => r.data),
};

// ---- Handoff ----
export const handoffApi = {
  tickets: (params: any) => api.get("/handoff/tickets", { params }).then((r) => r.data),
  claim: (id: string) => api.post(`/handoff/tickets/${id}/claim`).then((r) => r.data),
  unclaim: (id: string) => api.post(`/handoff/tickets/${id}/unclaim`).then((r) => r.data),
  resolve: (id: string, note: string) => api.post(`/handoff/tickets/${id}/resolve`, { note }).then((r) => r.data),
  resend: (id: string) => api.post(`/handoff/tickets/${id}/resend`).then((r) => r.data),
  update: (id: string, body: any) => api.post(`/handoff/tickets/${id}/update`, body).then((r) => r.data),
};

// ---- Accounts ----
export const accountApi = {
  users: () => api.get("/accounts/users").then((r) => r.data),
  create: (body: any) => api.post("/accounts/users", body).then((r) => r.data),
  update: (id: string, body: any) => api.put(`/accounts/users/${id}`, body).then((r) => r.data),
  remove: (id: string) => api.delete(`/accounts/users/${id}`).then((r) => r.data),
  auditLogs: (params: any) => api.get("/accounts/audit-logs", { params }).then((r) => r.data),
};

// ---- Tenants (super-admin) ----
export const tenantApi = {
  list: () => api.get("/tenants").then((r) => r.data),
  create: (body: any) => api.post("/tenants", body).then((r) => r.data),
  setActive: (id: string, is_active: boolean) =>
    api.patch(`/tenants/${id}`, { is_active }).then((r) => r.data),
};

// ---- Canned responses ----
export const cannedApi = {
  list: (q = "") => api.get("/canned", { params: { q } }).then((r) => r.data),
  create: (body: any) => api.post("/canned", body).then((r) => r.data),
  update: (id: string, body: any) => api.put(`/canned/${id}`, body).then((r) => r.data),
  remove: (id: string) => api.delete(`/canned/${id}`).then((r) => r.data),
};
