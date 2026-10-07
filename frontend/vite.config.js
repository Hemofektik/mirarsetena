import { defineConfig } from "vite";

export default defineConfig({
  build: { outDir: "dist" },
  test: {
    environment: "node",
    include: ["tests/**/*.test.js"],
  },
  server: {
    proxy: {
      "/p": "http://localhost:8000",
      "/healthz": "http://localhost:8000",
    },
  },
});
