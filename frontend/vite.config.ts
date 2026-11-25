import path from "node:path";
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: { alias: { "@": path.resolve(__dirname, "src") } },
  server: {
    // `pnpm dev` talks to a locally running backend (`uv run python -m locomotive`).
    proxy: { "/api": process.env.LOCOMOTIVE_DEV_BACKEND ?? "http://127.0.0.1:8000" },
  },
});
