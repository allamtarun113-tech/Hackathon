import { defineConfig } from "@playwright/test";

/**
 * Browser tests run against a deployed (or locally running) stack with real services:
 *   E2E_BASE_URL=https://pagermind.vercel.app E2E_OPERATOR_TOKEN=... npm run test:e2e
 */
export default defineConfig({
  testDir: "./e2e",
  timeout: 120_000,
  retries: process.env.CI ? 1 : 0,
  reporter: [["list"], ["html", { open: "never" }]],
  use: {
    baseURL: process.env.E2E_BASE_URL ?? "http://localhost:3000",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
});
