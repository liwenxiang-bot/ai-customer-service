import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      // Dev: proxy API + WS to the backend so there's no CORS friction.
      "/api": { target: "http://127.0.0.1:8000", changeOrigin: true },
    },
  },
  build: {
    outDir: "dist",
    rollupOptions: {
      output: {
        // Split long-cache vendor chunks; page-only deps (recharts, react-markdown)
        // stay with their lazy route chunks.
        manualChunks(id) {
          if (!id.includes("node_modules")) return undefined;
          const parts = id.split("node_modules/").pop()!.split("/");
          const pkg = parts[0].startsWith("@") ? `${parts[0]}/${parts[1]}` : parts[0];
          if (["react", "react-dom", "scheduler", "react-router", "react-router-dom", "@remix-run/router"].includes(pkg)) {
            return "react-vendor";
          }
          if (pkg === "antd" || pkg.startsWith("@ant-design/") || pkg.startsWith("rc-") || pkg.startsWith("@rc-component/")) {
            return "antd-vendor";
          }
          return undefined;
        },
      },
    },
  },
});
