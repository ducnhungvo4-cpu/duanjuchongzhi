import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  publicDir: false,
  build: {
    outDir: "../cloudflare/assets",
    emptyOutDir: true
  },
  server: {
    port: 5174,
    host: "127.0.0.1",
    proxy: {
      "/api": "http://127.0.0.1:8003"
    }
  }
});
