import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import console from 'node:console';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import process from 'node:process';
import { chromium } from '@playwright/test';

let input = '';
for await (const chunk of process.stdin) input += chunk;
const { origin, password, upload } = JSON.parse(input);
const directory = await mkdtemp(join(tmpdir(), 'spi-production-browser-'));
const browser = await chromium.launch({ executablePath: process.env.SPI_CHROME_EXECUTABLE });
try {
  const context = await browser.newContext({ baseURL: origin, acceptDownloads: true });
  const page = await context.newPage();
  const csp = [];
  page.on('console', (message) => {
    if (message.type() === 'error' && /Content Security Policy|violates the following/i.test(message.text())) {
      csp.push(message.text());
    }
  });
  await page.goto('/documents');
  await page.getByLabel('Email', { exact: true }).fill('alice@example.test');
  await page.getByLabel('Пароль', { exact: true }).fill(password);
  await page.getByRole('button', { name: 'Войти', exact: true }).click();
  const row = page.getByRole('listitem').filter({ hasText: 'Алиса Solar.pdf' });
  await row.getByRole('link', { name: 'Посмотреть результаты' }).click();
  const source = page.getByRole('list', { name: 'Научные записи' }).getByRole('listitem').filter({ hasText: '15.5 %' });
  await source.getByRole('link', { name: 'Открыть источник' }).click();
  await page.getByTestId('pdf-page-text').filter({ hasText: /Table 1/ }).waitFor({ timeout: 12_000 });
  await page.getByRole('region', { name: 'Ручная проверка' }).getByText('Подтверждено', { exact: true }).waitFor();
  const evidence = page.url();
  await page.goto(evidence);
  await page.getByTestId('pdf-page-text').waitFor();
  await page.getByRole('link', { name: 'К результатам загрузки' }).first().click();
  const download = page.waitForEvent('download');
  await page.getByRole('button', { name: 'Скачать ZIP' }).click();
  const archive = join(directory, 'alice.zip');
  await (await download).saveAs(archive);
  execFileSync('python', ['-m', 'spi.upload_export_verify', archive]);
  const cookies = await context.cookies();
  assert.ok(cookies.some((cookie) => cookie.name === '__Host-spi-session' && cookie.secure && cookie.httpOnly));
  assert.ok(evidence.includes(upload));
  assert.deepEqual(csp, []);
  console.log('PASS production Chromium with trusted local CA, PDF.js, Unicode, SPA reload and ZIP download');
} finally {
  await browser.close();
  await rm(directory, { recursive: true, force: true });
}
