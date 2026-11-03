import { useEffect, useRef, useState } from "react";

/** Built-in emoji set, shared shape with the customer widget (widget/src/emoji.ts). */
export const EMOJI_GROUPS: { name: string; items: string[] }[] = [
  {
    name: "笑脸",
    items: [
      "😀", "😄", "😁", "😆", "😅", "😂", "🤣", "😊", "🙂", "😉", "😍", "🥰", "😘", "😋", "😎", "🤩",
      "🤔", "🤗", "🤭", "😐", "😶", "🙄", "😏", "😮", "😯", "😲", "😳", "🥺", "😢", "😭", "😤", "😠",
      "😡", "🤯", "😱", "😨", "😰", "😴", "🤤", "😷", "🤒", "🤧", "🥳", "😇", "🤓", "🧐", "😬", "🙃",
    ],
  },
  {
    name: "手势",
    items: [
      "👍", "👎", "👌", "✌️", "🤞", "🤝", "👏", "🙌", "👐", "🙏", "💪", "👋", "🤙", "👉", "👈", "👆",
      "👇", "✋", "🤚", "🖐️", "✊", "👊", "🤘", "🫶",
    ],
  },
  {
    name: "爱心",
    items: [
      "❤️", "🧡", "💛", "💚", "💙", "💜", "🖤", "💔", "💕", "💖", "💯", "✅", "❌", "❓", "❗", "⭐",
      "🌟", "✨", "🔥", "🎉", "🎊", "💡", "⚠️", "🆗",
    ],
  },
  {
    name: "物品",
    items: [
      "📦", "🚚", "🛒", "💰", "💳", "🧾", "📱", "💻", "⌚", "🎁", "📷", "🔧", "🔑", "📄", "📞", "📧",
      "⏰", "📅", "📍", "🏠", "☕", "🍵", "🌹", "🌈",
    ],
  },
];

const RECENT_KEY = "acs_agent_recent_emoji";

function loadRecent(): string[] {
  try {
    const v = JSON.parse(localStorage.getItem(RECENT_KEY) || "[]");
    return Array.isArray(v) ? v.filter((x) => typeof x === "string").slice(0, 16) : [];
  } catch {
    return [];
  }
}

function remember(e: string) {
  try {
    localStorage.setItem(RECENT_KEY, JSON.stringify([e, ...loadRecent().filter((x) => x !== e)].slice(0, 16)));
  } catch {
    /* ignore */
  }
}

/** Popover emoji picker anchored above the composer. Closes on outside click / Esc. */
export function EmojiPicker({ onPick, onClose }: { onPick: (e: string) => void; onClose: () => void }) {
  const [recent, setRecent] = useState<string[]>(loadRecent);
  const tabs = [...(recent.length ? [{ name: "最近", items: recent }] : []), ...EMOJI_GROUPS];
  const [tabName, setTabName] = useState(tabs[0].name);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const onDown = (ev: PointerEvent) => {
      const t = ev.target as Element | null;
      if (ref.current?.contains(t) || t?.closest?.(".composer-emoji")) return;
      onClose();
    };
    const onKey = (ev: KeyboardEvent) => ev.key === "Escape" && onClose();
    document.addEventListener("pointerdown", onDown, true);
    document.addEventListener("keydown", onKey, true);
    return () => {
      document.removeEventListener("pointerdown", onDown, true);
      document.removeEventListener("keydown", onKey, true);
    };
  }, [onClose]);

  const current = tabs.find((t) => t.name === tabName) || tabs[0];
  return (
    <div className="emoji-panel" ref={ref} role="dialog" aria-label="选择表情">
      <div className="emoji-tabs" role="tablist">
        {tabs.map((t) => (
          <button
            key={t.name}
            type="button"
            role="tab"
            aria-selected={t.name === current.name}
            className={`emoji-tab${t.name === current.name ? " emoji-tab-on" : ""}`}
            onClick={() => setTabName(t.name)}
          >
            {t.name}
          </button>
        ))}
      </div>
      <div className="emoji-grid">
        {current.items.map((e) => (
          <button
            key={e}
            type="button"
            className="emoji-item"
            aria-label={e}
            onClick={() => {
              onPick(e);
              remember(e);
              setRecent(loadRecent());
            }}
          >
            {e}
          </button>
        ))}
      </div>
    </div>
  );
}
