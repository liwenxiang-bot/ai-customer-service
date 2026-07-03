import { defineConfig } from "vite";
import preact from "@preact/preset-vite";

// Build a single self-mounting IIFE bundle (widget.js) so a host page can embed it
// with one <script> tag. Output goes to widget/dist, which the backend serves at /embed.
export default defineConfig({
  plugins: [preact()],
  define: {
    "process.env.NODE_ENV": JSON.stringify("production"),
  },
  // 体积优化：去掉产物里的 license 注释（依赖仅 preact，MIT）
  esbuild: { legalComments: "none" },
  build: {
    outDir: "dist",
    emptyOutDir: true,
    // widget 本身已依赖 color-mix()/:host 等现代特性，压到 es2020 以下没有意义；
    // 提升目标让 esbuild 保留可选链/空值合并等原生语法，产物更小
    target: "es2020",
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
        assetFileNames: "widget.[ext]",
      },
    },
  },
  server: { port: 5174 },
});
