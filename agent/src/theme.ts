/** 亮/暗主题：默认跟随系统（prefers-color-scheme 自动），手动切换后写 localStorage 覆盖。
 *  首屏防闪白由 index.html 里的内联脚本完成（在 CSS 之前把 data-theme 打到 <html> 上），
 *  这里只负责运行时读取与切换。CSS 侧用 light-dark() 双值变量，见 styles.css。 */

const KEY = "acs_agent_theme";

export type ThemeMode = "light" | "dark";

/** 当前生效的主题：显式选择优先，否则跟随系统。 */
export function currentTheme(): ThemeMode {
  const t = document.documentElement.dataset.theme;
  if (t === "light" || t === "dark") return t;
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

/** 切换主题并持久化，返回切换后的值。 */
export function toggleTheme(): ThemeMode {
  const next: ThemeMode = currentTheme() === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  try {
    localStorage.setItem(KEY, next);
  } catch {
    /* 隐私模式等场景写入失败，不影响本次会话 */
  }
  return next;
}
