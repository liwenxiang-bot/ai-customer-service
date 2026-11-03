// Regression: the embeddable bundle must not declare anything in the host page's global scope.
// A top-level `var X` collides with a host `let X` and makes the whole widget fail to load.
// Run after `pnpm build` (CI builds first).
import { readFileSync } from "node:fs";
import vm from "node:vm";

const src = readFileSync(new URL("../dist/widget.js", import.meta.url), "utf8");
const ctx = vm.createContext({});
vm.runInContext("let A = 1, Is = 2, Ms = 3, z = 4, ys = 5, vs = 6;", ctx); // previously leaked names
const before = new Set(Object.getOwnPropertyNames(ctx));
try {
  vm.runInContext(src, ctx);
} catch (e) {
  // No DOM in node: a ReferenceError for document/window is expected; a SyntaxError is not.
  if (e instanceof SyntaxError || e?.name === "SyntaxError") {
    console.error("✗ widget.js collides with host globals:", e.message);
    process.exit(1);
  }
}
const leaked = Object.getOwnPropertyNames(ctx).filter((k) => !before.has(k));
if (leaked.length) {
  console.error("✗ widget.js leaks globals:", leaked.join(", "));
  process.exit(1);
}
console.log("✓ widget.js 不向宿主页面泄漏全局变量");
