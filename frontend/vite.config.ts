import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// Dev: Vite proxies /api and /healthz to FastAPI so the browser stays same-origin (cookie auth).
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    proxy: { "/api": "http://localhost:8000", "/healthz": "http://localhost:8000" },
  },
  test: { environment: "node", include: ["src/**/*.test.ts"] },
});
