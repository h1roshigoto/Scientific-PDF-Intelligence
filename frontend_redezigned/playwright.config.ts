import { defineConfig } from '@playwright/test';

const host = 'http://127.0.0.1';
export const mockUrl = `${host}:4175`;
export const apiUrl = `${host}:4176`;
export const realApiUrl = `${host}:4177`;
export const multiuserUrl = `${host}:4178`;
const backendUrl = `${host}:8182`;
const multiuserBackend = `${host}:8183`;

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
    { command: 'node node_modules/vite/bin/vite.js --host 127.0.0.1 --port 4176 --strictPort', url: apiUrl, env: { VITE_DATA_SOURCE: 'api', VITE_CSV_ARTIFACT_BASE: '/fixture-csv', VITE_SPI_AUTH_MODE: 'private' } },
    { command: 'python tests/e2e/serve_test_store.py', url: `${backendUrl}/health`, gracefulShutdown: { signal: 'SIGTERM', timeout: 5_000 } },
    {
      command: 'node node_modules/vite/bin/vite.js --host 127.0.0.1 --port 4177 --strictPort',
      url: realApiUrl,
      env: {
        SPI_BACKEND_URL: backendUrl,
        VITE_DATA_SOURCE: 'api',
        VITE_API_BASE: '/api',
        VITE_CSV_ARTIFACT_BASE: ' ',
        VITE_PROJECT_PASSPORT_PATH: 'export/project-passport.pdf',
        VITE_PROJECT_RUN_ID: 'playwright-e2e',
        VITE_PROJECT_PASSPORT_GENERATED_AT_HEADER: 'X-SPI-Generated-At',
      },
    },
    { command: 'python tests/e2e/serve_multiuser_store.py', url: `${multiuserBackend}/health`,
      gracefulShutdown: { signal: 'SIGTERM', timeout: 5_000 } },
    {
      command: 'node node_modules/vite/bin/vite.js --host 127.0.0.1 --port 4178 --strictPort',
      url: multiuserUrl,
      env: { SPI_BACKEND_URL: multiuserBackend, VITE_DATA_SOURCE: 'api', VITE_SPI_AUTH_MODE: '' },
    },
  ],
});
