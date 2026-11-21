import { defineConfig } from '@playwright/test';

const host = 'http://127.0.0.1';
export const mockUrl = `${host}:4175`;
export const apiUrl = `${host}:4176`;

export default defineConfig({
  testDir: 'tests/e2e',
  workers: 2,
  timeout: 30_000,
  expect: { timeout: 8_000 },
  outputDir: 'test-results/e2e',
  reporter: [['list'], ['html', { open: 'never' }], ['junit', { outputFile: 'test-results/playwright.xml' }]],
  use: {
    baseURL: mockUrl,
    browserName: 'chromium',
    channel: process.env.PLAYWRIGHT_CHANNEL || undefined,
    viewport: { width: 1440, height: 930 },
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  webServer: [
    { command: 'node node_modules/vite/bin/vite.js --host 127.0.0.1 --port 4175 --strictPort', url: mockUrl, env: { VITE_DATA_SOURCE: 'mock' } },
    { command: 'node node_modules/vite/bin/vite.js --host 127.0.0.1 --port 4176 --strictPort', url: apiUrl, env: { VITE_DATA_SOURCE: 'api', VITE_CSV_ARTIFACT_BASE: '/fixture-csv' } },
  ],
});
