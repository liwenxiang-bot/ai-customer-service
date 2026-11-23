/** 登录页品牌区的图形：一段对话的线稿（客户在左，AI 与人工在右，实底的那条是人工回复）。
 *  纯装饰，静态，不带动效；颜色只用白色的不同透明度，浅色 / 深色品牌底上都成立。 */

/** 聊天气泡轮廓：三个大圆角 + 靠发言方一侧的小圆角。 */
function bubble(x: number, y: number, w: number, h: number, side: "l" | "r") {
  const r = 12;
  const bl = side === "l" ? 3 : r;
  const br = side === "r" ? 3 : r;
  return [
    `M${x + r},${y}`, `H${x + w - r}`, `A${r},${r} 0 0 1 ${x + w},${y + r}`,
    `V${y + h - br}`, `A${br},${br} 0 0 1 ${x + w - br},${y + h}`,
    `H${x + bl}`, `A${bl},${bl} 0 0 1 ${x},${y + h - bl}`,
    `V${y + r}`, `A${r},${r} 0 0 1 ${x + r},${y}`, "Z",
  ].join(" ");
}

type Turn = { x: number; y: number; w: number; h: number; side: "l" | "r"; lines: number[]; solid?: boolean };

const TURNS: Turn[] = [
  { x: 0, y: 0, w: 188, h: 46, side: "l", lines: [132] },
  { x: 132, y: 66, w: 228, h: 66, side: "r", lines: [176, 120] },
  { x: 0, y: 152, w: 136, h: 46, side: "l", lines: [80] },
  { x: 150, y: 218, w: 210, h: 66, side: "r", lines: [158, 96], solid: true },
];

export function LoginArt({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="-1 -1 362 318" fill="none" aria-hidden="true">
      {TURNS.map((t) => (
        <g key={t.y}>
          <path
            d={bubble(t.x, t.y, t.w, t.h, t.side)}
            fill={t.solid ? "rgba(255,255,255,0.10)" : "none"}
            stroke={t.solid ? "rgba(255,255,255,0.46)" : "rgba(255,255,255,0.20)"}
            strokeWidth="1.5"
          />
          {t.lines.map((len, i) => (
            <path
              key={i}
              d={`M${t.x + 18},${t.y + 23 + i * 20} h${len}`}
              stroke={t.solid ? "rgba(255,255,255,0.42)" : "rgba(255,255,255,0.16)"}
              strokeWidth="3"
              strokeLinecap="round"
            />
          ))}
        </g>
      ))}
      {/* 客户正在输入 */}
      <g fill="rgba(255,255,255,0.22)">
        <circle cx="6" cy="310" r="3" />
        <circle cx="20" cy="310" r="3" />
        <circle cx="34" cy="310" r="3" />
      </g>
    </svg>
  );
}
