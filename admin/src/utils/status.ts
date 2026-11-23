/** 状态枚举的统一文案与语义色 —— 所有页面从这里取，避免同一状态跨页不一致。
 *  枚举值与 backend/app/models/enums.py 对应。
 *
 *  颜色只用 antd 的五个语义值（Badge 的 status、Tag 的 color 都认）：
 *  success 正常完成 / processing 进行中 / warning 需留意 / error 待处理或失败 / default 中性。
 *  渠道、来源、原因这类「分类」不是状态，一律 default，页面上按纯文字展示。 */

export type Tone = "success" | "processing" | "warning" | "error" | "default";
export type StatusMeta = { label: string; color: Tone };

export const SESSION_STATUS: Record<string, StatusMeta> = {
  active: { label: "进行中", color: "processing" },
  idle: { label: "空闲", color: "default" },
  closed: { label: "已结束", color: "default" },
  escalated: { label: "待人工", color: "error" },
  human_takeover: { label: "接管中", color: "warning" },
  human_handled: { label: "人工已处理", color: "success" },
};

export const HANDOFF_STATUS: Record<string, StatusMeta> = {
  open: { label: "待处理", color: "error" },
  in_progress: { label: "跟进中", color: "processing" },
  resolved: { label: "已解决", color: "success" },
};

export const HANDOFF_REASON: Record<string, StatusMeta> = {
  user_request: { label: "用户要求", color: "default" },
  model_decision: { label: "AI 判断", color: "default" },
  negative_feedback: { label: "差评触发", color: "default" },
  error_fallback: { label: "故障兜底", color: "default" },
};

export const KNOWLEDGE_STATUS: Record<string, StatusMeta> = {
  draft: { label: "草稿", color: "default" },
  published: { label: "已发布", color: "success" },
  archived: { label: "已归档", color: "default" },
};

export const KNOWLEDGE_SOURCE: Record<string, StatusMeta> = {
  manual: { label: "手动录入", color: "default" },
  import: { label: "批量导入", color: "default" },
  document: { label: "文档导入", color: "default" },
  url: { label: "网址导入", color: "default" },
  auto_distilled: { label: "对话沉淀", color: "default" },
};

export const CHANNEL_TYPE: Record<string, StatusMeta> = {
  web: { label: "网页", color: "default" },
  wechat_work: { label: "企业微信", color: "default" },
  feishu: { label: "飞书", color: "default" },
  dingtalk: { label: "钉钉", color: "default" },
};

export const CHUNK_STATUS: Record<string, StatusMeta> = {
  pending: { label: "待嵌入", color: "warning" },
  ready: { label: "已就绪", color: "success" },
  stale: { label: "待重建", color: "warning" },
  failed: { label: "失败", color: "error" },
};

/** 取状态元信息；未知值兜底展示原文，不再直出 undefined。 */
export function statusMeta(map: Record<string, StatusMeta>, value?: string | null): StatusMeta {
  return (value && map[value]) || { label: value || "-", color: "default" };
}
