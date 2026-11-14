import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In development the React app runs on :5173 and forwards /api to the FastAPI server on :8000.
// In production (Vercel) both are served from the same domain, so no proxy and no CORS are needed.
export default defineConfig({
  plugins: [react()],
  server: { proxy: { "/api": "http://127.0.0.1:8000" } },
  build: { outDir: "dist" },
});
