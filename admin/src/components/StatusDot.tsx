import { Badge } from "antd";
import type { StatusMeta } from "../utils/status";

/** 状态统一用「圆点 + 文字」呈现；彩色 Tag 只留给需要强提醒的少数场景。 */
export function StatusDot({ meta }: { meta: StatusMeta }) {
  return <Badge status={meta.color} text={meta.label} style={{ whiteSpace: "nowrap" }} />;
}
