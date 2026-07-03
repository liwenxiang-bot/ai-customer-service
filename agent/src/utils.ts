/** 小工具集合：时间格式化 / 输入法守卫 / 状态文案。不引第三方库，移动端保持轻量。 */

// 后端时间是带时区的 UTC ISO 串，Date 解析后按本地时区展示。
const pad = (n: number) => String(n).padStart(2, "0");

/** 紧凑列表时间，例 06-24 14:02；空值 → ""。 */
export function fmtShort(s?: string | null): string {
  if (!s) return "";
  const d = new Date(s);
  if (Number.isNaN(d.getTime())) return "";
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** 判断回车 keydown 是否是输入法选词上屏（中文拼音等场景），是则不应触发“发送”。 */
export function isImeComposing(e: unknown): boolean {
  const ev = e as { nativeEvent?: KeyboardEvent; isComposing?: boolean; keyCode?: number };
  const native = ev.nativeEvent ?? (ev as KeyboardEvent);
  return Boolean(native?.isComposing) || native?.keyCode === 229 || ev.keyCode === 229;
}

// 枚举值与 backend/app/models/enums.py 对应；cls 对应 styles.css 的 badge-* 配色。
// 文案/颜色语义对齐 admin 的 src/utils/status.ts（本地小映射，不跨包 import）。
export const SESSION_STATUS: Record<string, { label: string; cls: string }> = {
  active: { label: "进行中", cls: "green" },
  idle: { label: "空闲", cls: "gray" },
  closed: { label: "已结束", cls: "gray" },
  escalated: { label: "待人工", cls: "red" },
  human_takeover: { label: "接管中", cls: "blue" },
  human_handled: { label: "人工已处理", cls: "purple" },
};

// 工单状态：open 红 / in_progress 橙 / resolved 绿（同 admin HANDOFF_STATUS 语义）。
export const HANDOFF_STATUS: Record<string, { label: string; cls: string }> = {
  open: { label: "待处理", cls: "red" },
  in_progress: { label: "跟进中", cls: "orange" },
  resolved: { label: "已解决", cls: "green" },
};

export const CHANNEL_TYPE: Record<string, string> = {
  web: "网页",
  wechat_work: "企业微信",
  feishu: "飞书",
  dingtalk: "钉钉",
};

export const channelLabel = (v?: string | null) => (v && CHANNEL_TYPE[v]) || v || "-";

export function sessionStatusMeta(v?: string | null): { label: string; cls: string } {
  return (v && SESSION_STATUS[v]) || { label: v || "-", cls: "gray" };
}
