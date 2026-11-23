import { BRAND } from "../theme";

/** 产品标：主色方块里的一枚对话框。 */
export function Logo({ size = 24 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" aria-hidden style={{ flex: "none", display: "block" }}>
      <rect width="24" height="24" rx="5" fill={BRAND} />
      <path d="M6.4 7.2h11.2v7.4h-6.1l-3 2.6v-2.6H6.4z" fill="none" stroke="#fff" strokeWidth="1.6" strokeLinejoin="round" />
      <path d="M9.4 9.9h5.2M9.4 12h3" stroke="#fff" strokeWidth="1.4" strokeLinecap="round" />
    </svg>
  );
}
