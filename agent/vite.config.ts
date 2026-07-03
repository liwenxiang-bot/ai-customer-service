import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  // 生产环境挂在后端 /agent 路径下（backend main.py 静态挂载），
  // 相对 base 让 index.html 里的资源引用在任意挂载前缀下都能解析。
  base: "./",
  server: {
    port: 5176,
    proxy: {
      // Dev: proxy API + WS to the backend so there's no CORS friction.
      "/api": { target: "http://127.0.0.1:8000", changeOrigin: true },
      "/ws": { target: "http://127.0.0.1:8000", changeOrigin: true, ws: true },
    },
  },
  build: { outDir: "dist" },
});
