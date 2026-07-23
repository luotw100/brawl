import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// 开发时后端走 uvicorn :8000，Vite 只代理；生产由 FastAPI 托管构建产物，无跨域。
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/replay": "http://localhost:8000",
      "/ws": { target: "ws://localhost:8000", ws: true },
    },
  },
});
