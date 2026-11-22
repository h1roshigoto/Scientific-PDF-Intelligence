import { execFileSync } from 'node:child_process';
import { createHash, randomUUID } from 'node:crypto';
import { expect, test } from '@playwright/test';
import type { APIRequestContext, Page } from '@playwright/test';
import { realApiUrl } from '../../playwright.config';

test.use({ baseURL: realApiUrl });

const endpoint = '/api/uploads';
const token = 'synthetic-playwright-token-0123456789';
const headers = { Authorization: `Bearer ${token}` };
const savedMessage = 'Файл сохранён.';
const panel = (page: Page) => page.getByRole('region', { name: 'Мои загрузки' });
const exportPanel = (page: Page) => page.getByRole('region', { name: 'Экспорт научного проекта' });

async function downloadProject(page: Page, format: 'CSV' | 'JSON' | 'PDF' | 'ZIP', filename: string) {
  const download = page.waitForEvent('download');
  await exportPanel(page).getByRole('button', { name: 'Скачать ' + format }).click();
  const file = await download;
  expect(file.suggestedFilename()).toBe(filename);
  return file.path();
}

async function syntheticPdf(request: APIRequestContext, domain: 'solar' | 'rag') {
  const response = await request.get(`/api/documents/${domain}%3AD001/file`);
  expect(response.ok()).toBe(true);
  return response.body();
}

async function signIn(page: Page, key = token) {
  const scope = panel(page);
  await scope.getByLabel('Токен доступа').fill(key);
  await scope.getByRole('button', { name: 'Войти' }).click();
}

test('posts a synthetic RAG PDF directly through Vite and downloads the original', async ({ request }) => {
  const pdf = await syntheticPdf(request, 'rag');
  const name = `Пример-${randomUUID()}.pdf`;
  const official = await (await request.get('/api/documents')).json();
  const response = await request.post(endpoint, {
    headers, multipart: { domain: 'rag', file: { name, mimeType: 'application/pdf', buffer: pdf } },
  });
  expect(response.status()).toBe(201);
  const entry = await response.json();
  expect(entry).toEqual({
    upload_id: expect.stringMatching(/^[a-f0-9]{32}$/),
    domain: 'rag', original_filename: name, page_count: 3, size_bytes: pdf.length,
    sha256: createHash('sha256').update(pdf).digest('hex'),
    status: 'stored', created_at: expect.any(String),
  });
  expect(await (await request.get(`${endpoint}/${entry.upload_id}`, { headers })).json()).toEqual(entry);
  const url = `${endpoint}/${entry.upload_id}/file`;
  expect((await request.get(url)).status()).toBe(401);
  const original = await request.get(url, { headers });
  expect(original.headers()['content-type']).toBe('application/pdf');
  expect(await original.body()).toEqual(pdf);
  expect(await (await request.get(endpoint, { headers })).json()).toContainEqual(entry);
  expect(await (await request.get('/api/documents')).json()).toEqual(official);
});

test('signs in, validates, retries, persists and opens an uploaded Solar PDF', async ({ page, request }) => {
  const pdf = await syntheticPdf(request, 'solar');
  const filename = `Solar-${randomUUID()}.pdf`;
  const uploadRequests: string[] = [];
  page.on('request', (event) => {
    if (new URL(event.url()).pathname.startsWith(endpoint)) uploadRequests.push(event.url());
  });
  await page.setViewportSize({ width: 320, height: 700 });
  await page.goto('/');
  await page.getByRole('link', { name: 'Загрузить PDF' }).click();
  await expect(page).toHaveURL(/\/documents$/);
  const scope = panel(page);
  await signIn(page, 'неверный токен');
  await expect(scope.getByRole('alert')).toHaveText('Неверный токен доступа.');
  await signIn(page);
  const upload = scope.getByRole('button', { name: 'Загрузить PDF', exact: true });
  const picker = scope.getByLabel('Файл PDF');
  await expect(upload).toBeDisabled();
  await picker.setInputFiles({ name: 'oversize.pdf', mimeType: 'application/pdf', buffer: Buffer.alloc(16385) });
  await scope.getByLabel('Направление').selectOption('solar');
  await expect(scope.getByRole('alert')).toHaveText('Файл превышает допустимый размер.');
  await expect(upload).toBeDisabled();
  await picker.setInputFiles({ name: filename, mimeType: 'application/pdf', buffer: pdf });
  await expect(scope.getByText(new RegExp(filename))).toBeVisible();
  await expect(upload).toBeEnabled();

  await page.route(`**${endpoint}`, (route) => route.fulfill({
    status: 500, json: { detail: 'upload_storage_error' },
  }), { times: 1 });
  await upload.click();
  await expect(scope.getByRole('alert')).toContainText('Сервер не смог выполнить операцию');
  await expect(upload).toBeEnabled();

  await page.route(`**${endpoint}`, (route) => route.fulfill({ status: 200, json: {} }), { times: 1 });
  await upload.click();
  await expect(scope.getByRole('alert')).toHaveText('Сервер не подтвердил сохранение PDF.');
  await expect(scope.getByRole('status').getByText(savedMessage)).toHaveCount(0);

  let resume!: () => void;
  const pending = new Promise<void>((resolve) => { resume = resolve; });
  await page.route(`**${endpoint}`, async (route) => {
    await pending;
    await route.continue();
  }, { times: 1 });
  const serverResponse = page.waitForResponse((response) =>
    new URL(response.url()).pathname === endpoint && response.request().method() === 'POST');
  try {
    await upload.dblclick();
    await expect(scope.getByRole('button', { name: 'Отправляем PDF…' })).toBeDisabled();
    await expect(scope.getByRole('status').getByText(savedMessage)).toHaveCount(0);
  } finally {
    resume();
  }
  const response = await serverResponse;
  expect(response.status()).toBe(201);
  const entry = await response.json();
  expect(entry.sha256).toBe(createHash('sha256').update(pdf).digest('hex'));
  await expect(scope.getByRole('status')).toHaveText(savedMessage);

  const refresh = page.waitForResponse((reply) => new URL(reply.url()).pathname === endpoint
    && reply.request().method() === 'GET');
  await scope.getByRole('button', { name: 'Обновить список' }).click();
  expect((await refresh).status()).toBe(200);
  const row = scope.getByRole('listitem').filter({ hasText: filename });
  await expect(row).toHaveCount(1);
  await expect(row).toContainText('Solar · 3 стр.');
  await expect(row).toContainText('PDF сохранён');
  await expect(row.locator('time')).toHaveAttribute('datetime', entry.created_at);
  const original = page.waitForRequest((event) => new URL(event.url()).pathname === `${endpoint}/${entry.upload_id}/file`);
  await row.getByRole('button', { name: `Открыть оригинал ${filename}` }).click();
  expect((await (await original).allHeaders()).authorization).toBe(headers.Authorization);
  await expect(scope.getByTestId('pdf-page-text')).toHaveText('Test only SOLAR D001 page 1');
  await expect(scope.getByRole('link', { name: 'Открыть PDF в новой вкладке' })).toHaveAttribute('href', /^blob:.*#page=1$/);
  await expect(page.getByRole('region', { name: 'Реестр PDF' })).not.toContainText(filename);
  expect(await page.locator('html').evaluate((element) => element.scrollWidth - element.clientWidth)).toBe(0);

  const storage = await page.evaluate(() => JSON.stringify({
    local: { ...localStorage }, session: { ...sessionStorage }, cookies: document.cookie,
  }));
  expect(storage).not.toContain(token);
  expect([page.url(), ...uploadRequests].every((url) => !url.includes(token))).toBe(true);
  await page.reload();
  await expect(scope.getByLabel('Токен доступа')).toHaveValue('');
  await expect(row).toHaveCount(0);
  await signIn(page);
  await expect(row).toBeVisible();
});

test('analyzes a private RAG upload, recovers polling and access, then opens evidence and exports', async ({ page, request }, info) => {
  const fixture = await request.get('/api/__test/rag-fixture.pdf');
  expect(fixture.ok()).toBe(true);
  const filename = `RAG-${randomUUID()}.pdf`;
  await page.goto('/');
  await page.getByRole('link', { name: 'Загрузить PDF' }).click();
  await signIn(page);
  const scope = panel(page);
  await scope.getByLabel('Файл PDF').setInputFiles({ name: filename, mimeType: 'application/pdf', buffer: await fixture.body() });
  await scope.getByLabel('Направление').selectOption('rag');
  const uploaded = page.waitForResponse((reply) => new URL(reply.url()).pathname === endpoint && reply.request().method() === 'POST');
  await scope.getByRole('button', { name: 'Загрузить PDF', exact: true }).click();
  const upload = await uploaded;
  expect(upload.status()).toBe(201);
  const { upload_id: id } = await upload.json();
  const base = `${endpoint}/${id}`;
  const row = scope.getByRole('listitem').filter({ hasText: filename });
  const start = row.getByRole('button', { name: 'Начать анализ' });
  await expect(start).toBeEnabled();

  let starts = 0;
  page.on('request', (event) => {
    if (new URL(event.url()).pathname === `${base}/process`) starts += 1;
  });
  let resume!: () => void;
  const pending = new Promise<void>((resolve) => { resume = resolve; });
  await page.route(`**${base}/process`, async (route) => {
    await pending;
    await route.continue();
  }, { times: 1 });
  await page.route(`**${base}/processing`, (route) => route.fulfill({ status: 503 }), { times: 1 });
  const enqueued = page.waitForResponse((reply) => new URL(reply.url()).pathname === `${base}/process`);
  try {
    await start.dblclick();
    await expect(row.getByRole('button', { name: 'Запускаем анализ…' })).toBeDisabled();
  } finally {
    resume();
  }
  expect((await enqueued).status()).toBe(202);
  await expect(row).toContainText('В очереди');
  await expect(row.getByRole('alert')).toContainText('Не удалось обновить состояние');
  await expect(row).toContainText('Результатов — 16');
  await expect(row.getByRole('alert')).toHaveCount(0);
  expect(starts).toBe(1);

  await row.getByRole('link', { name: 'Посмотреть результаты' }).click();
  const results = page.getByRole('list', { name: 'Научные записи' });
  await expect(results.getByRole('listitem')).toHaveCount(16);
  const originalUrl = `**${base}/file`;
  await page.route(originalUrl, (route) => route.fulfill({ status: 401 }));
  await results.getByRole('link', { name: 'Открыть источник' }).first().click();
  await expect(page.getByRole('alert')).toContainText('Доступ к загрузкам истёк');
  await page.unroute(originalUrl);
  await page.getByLabel('Токен доступа').fill(token);
  await page.getByRole('button', { name: 'Войти' }).click();
  await expect(page.locator('canvas')).toHaveAttribute('data-rendered-page', '1');
  await expect(page.getByTestId('pdf-page-text')).toContainText('Qwen-Embed');

  await page.getByRole('link', { name: 'К результатам загрузки' }).first().click();
  await expect(exportPanel(page)).toContainText('Подтверждённых записей пока нет');
  const download = page.waitForEvent('download');
  await exportPanel(page).getByRole('button', { name: 'Скачать ZIP' }).click();
  const file = await download;
  expect(file.suggestedFilename()).toBe(`spi-project-rag-${id}.zip`);
  const archive = info.outputPath('private-rag.zip');
  await file.saveAs(archive);
  execFileSync('python', ['-m', 'spi.upload_export_verify', archive]);
});

test('reviews and exports real local extraction, keeps history after sign-in, and recovers from a revision conflict', async ({ page, request }) => {
  test.setTimeout(60_000);
  const fixture = await request.get('/api/__test/processing-fixture.pdf');
  expect(fixture.ok()).toBe(true);
  const pdf = await fixture.body();
  const filename = `Measurements-${randomUUID()}.pdf`;
  const officialEndpoints = ['/api/results', '/api/issues', '/api/documents'];
  const official = await Promise.all(officialEndpoints.map(async (url) => (await request.get(url)).json()));
  await page.goto('/documents');
  await signIn(page);
  const scope = panel(page);
  await scope.getByLabel('Файл PDF').setInputFiles({ name: filename, mimeType: 'application/pdf', buffer: pdf });
  await scope.getByLabel('Направление').selectOption('solar');
  const uploaded = page.waitForResponse((reply) => new URL(reply.url()).pathname === endpoint
    && reply.request().method() === 'POST');
  await scope.getByRole('button', { name: 'Загрузить PDF', exact: true }).click();
  const upload = await uploaded;
  expect(upload.status()).toBe(201);
  const entry = await upload.json();
  expect(entry.status).toBe('stored');
  const base = `${endpoint}/${entry.upload_id}`;
  const row = scope.getByRole('listitem').filter({ hasText: filename });
  await expect(row).toContainText('Анализ не запущен');
  expect((await (await request.get(`${base}/processing`, { headers })).json()).status).toBe('not_started');

  const enqueued = page.waitForResponse((reply) => new URL(reply.url()).pathname === `${base}/process`);
  await row.getByRole('button', { name: 'Начать анализ' }).click();
  const job = await enqueued;
  expect(job.status()).toBe(202);
  expect(await job.json()).toMatchObject({ upload_id: entry.upload_id, status: 'queued' });
  expect((await job.request().allHeaders()).authorization).toBe(headers.Authorization);
  await expect(row).toContainText('В очереди');
  await expect(row).toContainText('Анализ завершён');
  await expect(row).toContainText('Результатов — 4');
  const auto = await (await request.get(`${base}/results`, { headers })).json();
  await row.getByRole('link', { name: 'Посмотреть результаты' }).click();
  const results = page.getByRole('list', { name: 'Научные записи' });
  await expect(results.getByRole('listitem')).toHaveCount(4);
  const pce = results.getByRole('listitem').filter({ hasText: '15.5 %' });
  await expect(pce).toContainText('PCE');
  await expect(pce).toContainText('Извлечено');
  const original = page.waitForRequest((event) => new URL(event.url()).pathname === `${base}/file`);
  await pce.getByRole('link', { name: 'Открыть источник' }).click();
  expect((await (await original).allHeaders()).authorization).toBe(headers.Authorization);
  await expect(page.getByTestId('raw-value')).toHaveText('15.5 %');
  await expect(page.locator('blockquote')).toHaveText('15.5');
  await expect(page.locator('canvas')).toHaveAttribute('data-rendered-page', '2');
  await expect(page.getByTestId('pdf-page-text')).toContainText('Table 1');
  expect((await request.get(`${base}/results`)).status()).toBe(401);
  const review = page.getByRole('region', { name: 'Ручная проверка' });
  const history = page.getByRole('region', { name: 'История решений' });
  const check = review.getByLabel('Я сверил значение, единицу и контекст на странице PDF');
  const approve = review.getByRole('button', { name: 'Подтвердить исходное' });
  const back = () => page.getByRole('link', { name: 'К результатам загрузки' }).first().click();
  const open = (value: string) => results.getByRole('listitem').filter({ hasText: value }).first()
    .getByRole('link', { name: 'Открыть источник' }).click();

  await expect(approve).toBeDisabled();
  await review.getByLabel('Метка оператора (необязательно)').fill('Оператор теста');
  await check.check();
  await approve.click();
  await expect(review.getByText('Подтверждено', { exact: true })).toBeVisible();
  await history.getByText('Подтверждено', { exact: true }).click();
  await expect(history.getByText('Оператор теста', { exact: true })).toBeVisible();
  await back();

  await open('15.6 %');
  await review.getByRole('button', { name: 'Предложить исправление' }).click();
  await review.getByLabel('Исходное значение', { exact: true }).fill('15.7');
  await review.getByLabel('Нормализованное значение', { exact: true }).fill('15.7');
  await review.getByLabel('Основание нормализации').fill('В исправлении на странице 1 значение PCE дано в процентах.');
  await review.getByLabel('Страница PDF', { exact: true }).fill('1');
  await review.getByLabel('Фрагмент', { exact: true }).fill('B1 PCE was corrected from 15.6% to 15.7%.');
  await review.getByLabel('Таблица', { exact: true }).clear();
  await review.getByText('Научный контекст', { exact: true }).click();
  await expect(review.getByLabel('Прибор', { exact: true })).toHaveValue('B1');
  await review.getByLabel('Обоснование исправления').fill('На странице 1 опубликовано исправление значения PCE образца B1.');
  await expect(review.getByText('Было → Стало', { exact: true })).toBeVisible();
  await review.getByRole('button', { name: 'Сохранить предложение' }).click();
  await expect(review.getByText('Предложено исправление', { exact: true })).toBeVisible();
  expect(await (await request.get(`${base}/reviewed-results`, { headers })).json()).toHaveLength(1);
  const approveCorrection = review.getByRole('button', { name: 'Подтвердить исправление' });
  await expect(approveCorrection).toBeDisabled();
  await review.getByRole('button', { name: 'Открыть страницу исправления · 1' }).click();
  await expect(page.locator('canvas')).toHaveAttribute('data-rendered-page', '1');
  await expect(page.getByTestId('pdf-page-text')).toContainText('Erratum');
  await check.check();
  await approveCorrection.click();
  await expect(review.getByText('Исправление подтверждено', { exact: true })).toBeVisible();
  await expect(page.getByTestId('raw-value')).toHaveText('15.6 %');
  await expect(history.getByText('Предложено исправление', { exact: true })).toBeVisible();
  await expect(history.getByText('Исправление подтверждено', { exact: true })).toBeVisible();
  await back();

  await open('1200 mV');
  await review.getByLabel('Примечание или причина').fill('Сопоставление с измерительным протоколом требует повторной сверки.');
  await review.getByRole('button', { name: 'Отклонить', exact: true }).click();
  await expect(review.getByText('Отклонено', { exact: true })).toBeVisible();
  await history.getByText('Отклонено', { exact: true }).click();
  await expect(history.getByText('Сопоставление с измерительным протоколом требует повторной сверки.')).toBeVisible();
  const rejectedId = new URL(page.url()).searchParams.get('evidence');
  await back();
  const exports = exportPanel(page);
  await expect(exports).toContainText('Подтверждено исходных — 1');
  await expect(exports).toContainText('Подтверждено исправленных — 1');
  await expect(exports).toContainText('Отклонено — 1');
  await expect(exports).toContainText('Ожидают решения — 1');
  await expect(exports).toContainText('Устарело — 0');
  await expect(exports).toContainText('Научная полнота не подтверждена');

  const zipName = 'spi-project-solar-' + entry.upload_id + '.zip';
  const zipRoute = '**/export/project.zip';
  await page.route(zipRoute, (route) => route.fulfill({ status: 500, json: { detail: 'upload_export_storage_error' } }),
    { times: 1 });
  await exports.getByRole('button', { name: 'Скачать ZIP' }).click();
  await expect(exports.getByRole('alert')).toContainText('Сервер не смог подготовить экспорт');
  const csv = await downloadProject(page, 'CSV', 'reviewed_results.csv');
  const json = await downloadProject(page, 'JSON', 'reviewed_results.json');
  const pdfPassport = await downloadProject(page, 'PDF', 'scientific-passport.pdf');
  const zipRequests: string[] = [];
  page.on('request', (req) => {
    if (new URL(req.url()).pathname.endsWith('/export/project.zip')) zipRequests.push(req.url());
  });
  let releaseZip!: () => void;
  const delayed = new Promise<void>((resolve) => { releaseZip = resolve; });
  await page.route(zipRoute, async (route) => {
    await delayed;
    await route.continue();
  }, { times: 1 });
  const firstZip = page.waitForEvent('download');
  const zipRequest = page.waitForRequest((req) => new URL(req.url()).pathname.endsWith('/export/project.zip'));
  try {
    await exports.getByRole('button', { name: 'Скачать ZIP' }).dblclick();
    await expect(exports.getByRole('status')).toContainText('Подготавливается ZIP');
    await expect(exports.getByRole('button', { name: 'Скачать ZIP' })).toBeDisabled();
  } finally {
    releaseZip();
  }
  const zipHeaders = await (await zipRequest).allHeaders();
  expect(zipHeaders.authorization).toBe(headers.Authorization);
  expect(zipHeaders['x-spi-snapshot-sha256']).toMatch(/^[a-f0-9]{64}$/);
  const zipDownload = await firstZip;
  expect(zipDownload.suggestedFilename()).toBe(zipName);
  const zip = await zipDownload.path();
  expect(zipRequests).toHaveLength(1);

  await open('15.5 %');

  await page.setViewportSize({ width: 390, height: 844 });
  await page.reload();
  await expect(page.getByLabel('Токен доступа')).toHaveValue('');
  await expect(page.getByTestId('raw-value')).toHaveCount(0);
  await page.getByLabel('Токен доступа').fill(token);
  await page.getByRole('button', { name: 'Войти' }).click();
  await expect(page.getByTestId('raw-value')).toHaveText('15.5 %');
  await expect(page.locator('canvas')).toHaveAttribute('data-rendered-page', '2');
  await expect(review.getByText('Подтверждено', { exact: true })).toBeVisible();
  expect(await page.locator('html').evaluate((element) => element.scrollWidth - element.clientWidth)).toBe(0);
  await back();
  await expect(page.getByText('Анализ завершён', { exact: true })).toBeVisible();
  await expect(results.getByRole('listitem')).toHaveCount(4);
  const reopened = await downloadProject(page, 'ZIP', zipName);
  expect(zipRequests.every((url) => !url.includes(token))).toBe(true);
  expect(await page.evaluate(() => JSON.stringify({
    local: { ...localStorage }, session: { ...sessionStorage }, cookies: document.cookie,
  }))).not.toContain(token);
  expect(execFileSync('python', ['tests/e2e/check_private_export.py', zip, csv, json, pdfPassport, reopened, rejectedId!],
    { encoding: 'utf8' })).toContain('PASS: 6 files verified by SHA-256');
  const filter = page.getByLabel('Статус проверки');
  await filter.selectOption('approved');
  await expect(results.getByRole('listitem')).toHaveCount(2);
  await filter.selectOption('rejected');
  await expect(results.getByRole('listitem')).toHaveCount(1);
  await filter.selectOption('pending');
  await expect(results.getByRole('listitem')).toHaveCount(1);
  await open('1200 mV');

  const fourthId = new URL(page.url()).searchParams.get('evidence');
  expect(fourthId).toBeTruthy();
  expect(fourthId).not.toBe(rejectedId);
  const reviewPath = `${base}/results/${encodeURIComponent(fourthId!)}/review`;
  const pending = await (await request.get(reviewPath, { headers })).json();
  expect(pending.revision).toBe(0);
  expect((await request.post(reviewPath, { headers, data: {
    basis: pending.basis, expected_revision: 0, action: 'reject', reason: 'Параллельное решение оператора',
  } })).status()).toBe(201);
  const conflict = page.waitForResponse((reply) => reply.request().method() === 'POST' && reply.status() === 409);
  await check.check();
  await approve.click();
  expect((await (await conflict).json()).detail).toBe('review_revision_conflict');
  await expect(review.getByRole('alert')).toContainText('Рецензию уже изменили');
  await review.getByRole('button', { name: 'Обновить данные' }).click();
  await expect(review.getByText('Отклонено', { exact: true })).toBeVisible();
  await expect(review.getByText('Ревизия 1')).toBeVisible();
  await expect(approve).toBeDisabled();
  await check.check();
  await approve.click();
  await expect(review.getByText('Ревизия 2')).toBeVisible();
  await expect(history.getByText('Отклонено', { exact: true })).toBeVisible();
  await expect(history.getByText('Подтверждено', { exact: true })).toBeVisible();

  const reviewed = await (await request.get(`${base}/reviewed-results`, { headers })).json();
  expect(reviewed).toHaveLength(3);
  expect(reviewed.some((entry: { original_result: { result_id: string } }) => entry.original_result.result_id === rejectedId)).toBe(false);
  expect(reviewed).toContainEqual(expect.objectContaining({
    original_result: expect.objectContaining({ raw: { value: '15.6', unit: '%' } }),
    reviewed_result: expect.objectContaining({ raw: { value: '15.7', unit: '%' }, source: expect.objectContaining({ page: 1 }) }),
    decision: expect.objectContaining({ status: 'approved_corrected' }),
  }));
  expect(await (await request.get(`${base}/results`, { headers })).json()).toEqual(auto);
  expect(await (await request.get(`${base}/file`, { headers })).body()).toEqual(pdf);
  expect(await Promise.all(officialEndpoints.map(async (url) => (await request.get(url)).json()))).toEqual(official);
});
