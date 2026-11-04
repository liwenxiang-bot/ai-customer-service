/** 状态枚举的统一文案与 Tag 颜色 —— 所有页面从这里取，避免同一状态跨页不一致。
 *  枚举值与 backend/app/models/enums.py 对应。 */

export type StatusMeta = { label: string; color: string };

export const SESSION_STATUS: Record<string, StatusMeta> = {
  active: { label: "进行中", color: "green" },
  idle: { label: "空闲", color: "default" },
  closed: { label: "已结束", color: "default" },
  escalated: { label: "待人工", color: "red" },
  human_takeover: { label: "接管中", color: "geekblue" },
  human_handled: { label: "人工已处理", color: "purple" },
};

export const HANDOFF_STATUS: Record<string, StatusMeta> = {
  open: { label: "待处理", color: "red" },
  in_progress: { label: "跟进中", color: "orange" },
  resolved: { label: "已解决", color: "green" },
};

export const HANDOFF_REASON: Record<string, StatusMeta> = {
  user_request: { label: "用户要求", color: "blue" },
  model_decision: { label: "AI 判断", color: "cyan" },
  negative_feedback: { label: "差评触发", color: "orange" },
  error_fallback: { label: "故障兜底", color: "red" },
};

export const KNOWLEDGE_STATUS: Record<string, StatusMeta> = {
  draft: { label: "草稿", color: "default" },
  published: { label: "已发布", color: "green" },
  archived: { label: "已归档", color: "orange" },
};

export const KNOWLEDGE_SOURCE: Record<string, StatusMeta> = {
  manual: { label: "手动录入", color: "blue" },
  import: { label: "批量导入", color: "cyan" },
  document: { label: "文档导入", color: "geekblue" },
  url: { label: "网址导入", color: "volcano" },
  auto_distilled: { label: "对话沉淀", color: "purple" },
};

export const CHANNEL_TYPE: Record<string, StatusMeta> = {
  web: { label: "网页", color: "blue" },
  wechat_work: { label: "企业微信", color: "green" },
  feishu: { label: "飞书", color: "cyan" },
  dingtalk: { label: "钉钉", color: "geekblue" },
};

export const CHUNK_STATUS: Record<string, StatusMeta> = {
  pending: { label: "待嵌入", color: "orange" },
  ready: { label: "已就绪", color: "green" },
  stale: { label: "待重建", color: "gold" },
  failed: { label: "失败", color: "red" },
};

/** 取状态元信息；未知值兜底展示原文，不再直出 undefined。 */
export function statusMeta(map: Record<string, StatusMeta>, value?: string | null): StatusMeta {
  return (value && map[value]) || { label: value || "-", color: "default" };
}
