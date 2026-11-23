/** 状态统一用「圆点 + 文字」：颜色只落在圆点上，文字保持中性（与管理后台一致）。 */
export function Status({ tone, label }: { tone: string; label: string }) {
  return <span className={`status status-${tone}`}>{label}</span>;
}
