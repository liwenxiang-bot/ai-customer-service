/** Built-in emoji set (no external dependency — keeps the embeddable widget small). */
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

const RECENT_KEY = "acs_recent_emoji";
const RECENT_MAX = 16;

export function loadRecentEmoji(): string[] {
  try {
    const v = JSON.parse(localStorage.getItem(RECENT_KEY) || "[]");
    return Array.isArray(v) ? v.filter((x) => typeof x === "string").slice(0, RECENT_MAX) : [];
  } catch {
    return [];
  }
}

export function rememberEmoji(e: string): string[] {
  const next = [e, ...loadRecentEmoji().filter((x) => x !== e)].slice(0, RECENT_MAX);
  try {
    localStorage.setItem(RECENT_KEY, JSON.stringify(next));
  } catch {
    /* storage blocked by the host page — recents just won't persist */
  }
  return next;
}

/** Insert text at the textarea caret (replacing any selection); returns the new value + caret. */
export function insertAtCaret(el: HTMLTextAreaElement | null, value: string, text: string) {
  const start = el?.selectionStart ?? value.length;
  const end = el?.selectionEnd ?? value.length;
  return { value: value.slice(0, start) + text + value.slice(end), caret: start + text.length };
}
