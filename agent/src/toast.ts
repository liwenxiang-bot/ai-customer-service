/** 极简 toast（不引组件库）：toast("已被 XX 认领", "error")。样式见 styles.css。 */

let container: HTMLDivElement | null = null;

export function toast(msg: string, type: "info" | "success" | "error" = "info") {
  if (!container) {
    container = document.createElement("div");
    container.className = "toast-container";
    document.body.appendChild(container);
  }
  const el = document.createElement("div");
  el.className = `toast toast-${type}`;
  el.textContent = msg;
  container.appendChild(el);
  setTimeout(() => {
    el.classList.add("toast-out");
    setTimeout(() => el.remove(), 300);
  }, 2600);
}
