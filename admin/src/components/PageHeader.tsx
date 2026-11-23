import type { ReactNode } from "react";

/** 页头：通栏白底条，左侧标题（可带一行说明），右侧放页面级操作。 */
export function PageHeader({ title, sub, extra }: { title: ReactNode; sub?: ReactNode; extra?: ReactNode }) {
  return (
    <div className="acs-page-header">
      <div style={{ minWidth: 0 }}>
        <h1 className="acs-page-title">{title}</h1>
        {sub && <div className="acs-page-sub">{sub}</div>}
      </div>
      {extra && <div className="acs-page-extra">{extra}</div>}
    </div>
  );
}
