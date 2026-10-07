import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "tests/e2e",
  timeout: 90_000,
  use: { baseURL: "http://127.0.0.1:4173" },
  webServer: {
    command:
      "python3 -m uvicorn mirarsetena.app:app --host 127.0.0.1 --port 4173",
    cwd: "..",
    env: { MIRAR_STORAGE_ROOT: "data/cache-e2e" },
    url: "http://127.0.0.1:4173/healthz",
    reuseExistingServer: true,
    timeout: 60_000,
  },
});
