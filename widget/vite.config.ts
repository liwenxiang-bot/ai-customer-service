import { defineConfig } from "vite";
import preact from "@preact/preset-vite";

// Build a single self-mounting IIFE bundle (widget.js) so a host page can embed it
// with one <script> tag. Output goes to widget/dist, which the backend serves at /embed.
export default defineConfig({
  plugins: [preact()],
  define: {
    "process.env.NODE_ENV": JSON.stringify("production"),
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
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
