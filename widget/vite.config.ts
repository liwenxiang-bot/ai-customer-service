import { defineConfig } from "vite";
import preact from "@preact/preset-vite";

// Build a single self-mounting IIFE bundle (widget.js) so a host page can embed it
// with one <script> tag. Output goes to widget/dist, which the backend serves at /embed.
export default defineConfig({
  plugins: [preact()],
  define: {
    "process.env.NODE_ENV": JSON.stringify("production"),
  },
  // 体积优化：去掉产物里的 license 注释（依赖仅 preact/marked，均为 MIT）
  esbuild: { legalComments: "none" },
  build: {
    outDir: "dist",
    emptyOutDir: true,
    // widget 本身已依赖 color-mix()/:host 等现代特性，压到 es2020 以下没有意义；
    // 提升目标让 esbuild 保留可选链/空值合并等原生语法，产物更小
    // es2022: native class fields — at es2020 esbuild hoists helpers (`var Is=…,Ms=…,A=…`)
    // ABOVE the IIFE, i.e. into the host page's global scope, where a host-side `let A` makes
    // the whole widget throw. (Browsers that run this widget all support class fields.)
    target: "es2022",
    lib: {
      entry: "src/main.tsx",
      name: "ACSWidget",
      formats: ["iife"],
      fileName: () => "widget.js",
    },
    rollupOptions: {
      output: {
        // Inline everything into widget.js; no code splitting for an embeddable.
        inlineDynamicImports: true,
        // Belt and braces: wrap the whole file so no future helper can leak a global either.
        banner: "(function(){",
        footer: "})();",
        assetFileNames: "widget.[ext]",
      },
    },
  },
  server: { port: 5174 },
});
