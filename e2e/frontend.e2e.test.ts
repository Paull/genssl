import { afterAll, beforeAll, describe, expect, setDefaultTimeout, test } from 'bun:test';
import { chromium, type Browser, type Page } from 'playwright';
import { cp, mkdtemp, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';

setDefaultTimeout(240_000);

const repoRoot = resolve(import.meta.dir, '..');
const fixtureServer = join(repoRoot, 'e2e', 'fixture-server.py');
const rootPassword = 'e2e-root-password';

let browser: Browser;

type Fixture = { baseURL: string; stop: () => Promise<void> };

async function waitForReady(process: ReturnType<typeof Bun.spawn>): Promise<string> {
  if (!process.stdout || typeof process.stdout === 'number') throw new Error('fixture server has no stdout');
  const reader = process.stdout.getReader();
  const decoder = new TextDecoder();
  let output = '';
  const timeout = setTimeout(() => process.kill(), 20_000);
  try {
    while (true) {
      const chunk = await reader.read();
      if (chunk.done) break;
      output += decoder.decode(chunk.value, { stream: true });
      const line = output.split('\n').find((value) => value.startsWith('READY '));
      if (line) return line.slice('READY '.length).trim();
    }
  } finally {
    clearTimeout(timeout);
    reader.releaseLock();
  }
  throw new Error(`fixture server exited before becoming ready: ${output}`);
}

async function startFixture(): Promise<Fixture> {
  const fixtureRoot = await mkdtemp(join(tmpdir(), 'corntech-e2e-'));
  let serverProcess: ReturnType<typeof Bun.spawn> | undefined;
  let fixtureStderr: Promise<string> | undefined;
  try {
    await Promise.all([
      cp(join(repoRoot, 'ca.cnf'), join(fixtureRoot, 'ca.cnf')),
      cp(join(repoRoot, 'flush.sh'), join(fixtureRoot, 'flush.sh')),
      cp(join(repoRoot, 'gen_root_cert.sh'), join(fixtureRoot, 'gen_root_cert.sh')),
      cp(join(repoRoot, 'gen_server_cert.sh'), join(fixtureRoot, 'gen_server_cert.sh')),
      cp(join(repoRoot, 'gen_client_cert.sh'), join(fixtureRoot, 'gen_client_cert.sh')),
    ]);
    await writeFile(join(fixtureRoot, '.fixture'), 'isolated\n');
    serverProcess = Bun.spawn(['python3', fixtureServer, '--root', fixtureRoot, '--web-dir', join(repoRoot, 'web')], {
      cwd: repoRoot,
      env: { ...process.env, ROOTPASS: rootPassword },
      stdout: 'pipe',
      stderr: 'pipe',
    });
    if (serverProcess.stderr && typeof serverProcess.stderr !== 'number') fixtureStderr = new Response(serverProcess.stderr).text();
    let baseURL: string;
    try {
      baseURL = await waitForReady(serverProcess);
    } catch (error) {
      const stderr = await fixtureStderr?.catch(() => '') || '';
      throw new Error(`${error instanceof Error ? error.message : error}\nfixture stderr: ${stderr}`);
    }
    const health = await fetch(`${baseURL}/api/health`);
    if (!health.ok) throw new Error(`fixture health check failed: ${health.status} ${await health.text()}`);
    return {
      baseURL,
      stop: async () => {
        serverProcess?.kill();
        if (serverProcess) await serverProcess.exited;
        await fixtureStderr?.catch(() => '');
        await rm(fixtureRoot, { recursive: true, force: true });
      },
    };
  } catch (error) {
    serverProcess?.kill();
    if (serverProcess) await serverProcess.exited.catch(() => -1);
    await fixtureStderr?.catch(() => '');
    await rm(fixtureRoot, { recursive: true, force: true });
    throw error;
  }
}

async function apiPost(baseURL: string, path: string, body: Record<string, unknown>): Promise<Response> {
  return fetch(`${baseURL}/api${path}`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  });
}

async function dashboard(page: Page, baseURL: string): Promise<void> {
  await page.goto(baseURL, { waitUntil: 'domcontentloaded' });
  await page.locator('#connection-text').waitFor({ state: 'attached' });
  await page.waitForFunction(() => document.querySelector('#connection-text')?.textContent === '服务在线');
  await page.getByRole('heading', { name: '证书总览' }).waitFor({ state: 'visible' });
}

async function textOf(page: Page, selector: string): Promise<string> {
  return (await page.locator(selector).textContent())?.trim() || '';
}

async function expectText(page: Page, selector: string, expected: string | RegExp): Promise<void> {
  const actual = await textOf(page, selector);
  if (expected instanceof RegExp) expect(actual).toMatch(expected);
  else expect(actual).toContain(expected);
}

async function newPage(): Promise<Page> {
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  page.setDefaultTimeout(240_000);
  return page;
}

beforeAll(async () => {
  const build = Bun.spawn(['bun', 'run', 'build'], { cwd: repoRoot, stdout: 'pipe', stderr: 'pipe' });
  const buildExit = await build.exited;
  if (buildExit !== 0) throw new Error(`frontend build failed: ${await new Response(build.stderr).text()}`);
  browser = await chromium.launch({ headless: true });
});

afterAll(async () => {
  await browser?.close();
});

describe('user frontend', () => {
  test('loads the dashboard and initializes the CA from the UI', async () => {
    const fixture = await startFixture();
    const page = await newPage();
    try {
      await dashboard(page, fixture.baseURL);
      expect(await textOf(page, '#metric-total')).toBe('0');
      await expectText(page, '#ca-status-title', '等待初始化');
      await expectText(page, '#ca-roots', '缺失');

      await page.locator('[data-action="init-ca"]').click();
      await page.locator('#ca-rootpass').fill(rootPassword);
      const responsePromise = page.waitForResponse((response) => response.url() === `${fixture.baseURL}/api/ca/initialize` && response.request().method() === 'POST');
      await page.locator('#ca-form button[type="submit"]').click();
      expect((await responsePromise).status()).toBe(200);

      await page.waitForFunction(() => !document.querySelector('#overlay')?.classList.contains('open'));
      await expectText(page, '#ca-status-title', 'CA 状态正常');
      await expectText(page, '#ca-roots', '可用');
      await expectText(page, '#toast-region', '根 CA 初始化完成');
    } finally {
      await page.close();
      await fixture.stop();
    }
  });

  test('registers an application and supports drawer dismissal', async () => {
    const fixture = await startFixture();
    const page = await newPage();
    const appName = `e2e-orders-${Date.now()}`;
    try {
      await dashboard(page, fixture.baseURL);
      await page.locator('[data-action="register"]').click();
      await page.locator('#registration-name').fill(appName);
      await page.locator('#registration-owner').fill('platform-e2e');
      await page.locator('#registration-notes').fill('browser flow');
      const responsePromise = page.waitForResponse((response) => response.url() === `${fixture.baseURL}/api/registrations` && response.request().method() === 'POST');
      await page.locator('#registration-form button[type="submit"]').click();
      expect((await responsePromise).status()).toBe(201);
      await page.locator('.registration-card').filter({ hasText: appName }).waitFor({ state: 'visible' });
      await expectText(page, '#toast-region', '应用登记已保存');

      await page.locator('[data-action="register"]').click();
      await page.keyboard.press('Escape');
      await page.waitForFunction(() => !document.querySelector('#overlay')?.classList.contains('open'));
      await page.locator('[data-action="register"]').click();
      await page.locator('#overlay').click({ position: { x: 4, y: 4 } });
      await page.waitForFunction(() => !document.querySelector('#overlay')?.classList.contains('open'));
    } finally {
      await page.close();
      await fixture.stop();
    }
  });

  test('issues server and client certificates, filters them, and downloads artifacts', async () => {
    const fixture = await startFixture();
    const caResponse = await apiPost(fixture.baseURL, '/ca/initialize', { rootpass: rootPassword });
    expect([200, 409]).toContain(caResponse.status);
    const page = await newPage();
    const serverName = `e2e-server-${Date.now()}`;
    const clientName = `e2e-client-${Date.now()}`;
    const san = `api.${serverName}.example`;
    try {
      await dashboard(page, fixture.baseURL);

      await page.locator('[data-action="generate"]').click();
      await page.locator('#issue-name').fill(serverName);
      await page.locator('#issue-sans').fill(`${san}\n10.0.0.10`);
      await page.locator('#issue-days').fill('30');
      await page.locator('#issue-key').selectOption('rsa');
      const serverResponsePromise = page.waitForResponse((response) => response.url() === `${fixture.baseURL}/api/certificates` && response.request().method() === 'POST');
      await page.locator('#issue-submit').click();
      const serverResponse = await serverResponsePromise;
      if (serverResponse.status() !== 201) throw new Error(`server issue failed: ${serverResponse.status()} ${await serverResponse.text()}`);
      const serverRow = page.locator('#cert-list tr').filter({ hasText: serverName });
      await serverRow.waitFor({ state: 'visible', timeout: 180_000 });
      expect(await textOf(page, '#metric-server')).toBe('1');

      const serverId = await serverRow.locator('[data-action="detail"]').getAttribute('data-id');
      const detailResponsePromise = page.waitForResponse((response) => response.url() === `${fixture.baseURL}/api/certificates/${serverId}` && response.request().method() === 'GET');
      await serverRow.locator('[data-action="detail"]').click();
      expect((await detailResponsePromise).status()).toBe(200);
      await expectText(page, '.drawer h2', '证书详情');
      expect(await page.getByRole('dialog', { name: '证书详情' }).count()).toBe(1);
      await expectText(page, '.drawer', san);
      const downloadPromise = page.waitForEvent('download');
      await page.locator('.drawer-footer a[download]').click();
      const download = await downloadPromise;
      expect(download.suggestedFilename()).toMatch(new RegExp(serverName));
      await page.locator('.close-button').click();

      await page.locator('[data-action="generate"]').click();
      await page.locator('input[name="type"][value="client"]').check();
      await page.locator('#issue-name').fill(clientName);
      await page.locator('#issue-days').fill('365');
      await page.locator('#issue-key').selectOption('ec');
      const clientResponsePromise = page.waitForResponse((response) => response.url() === `${fixture.baseURL}/api/certificates` && response.request().method() === 'POST');
      await page.locator('#issue-submit').click();
      const clientResponse = await clientResponsePromise;
      if (clientResponse.status() !== 201) throw new Error(`client issue failed: ${clientResponse.status()} ${await clientResponse.text()}`);
      const clientRow = page.locator('#cert-list tr').filter({ hasText: clientName });
      await clientRow.waitFor({ state: 'visible', timeout: 180_000 });
      expect(await textOf(page, '#metric-client')).toBe('1');

      await page.locator('[data-filter="client"]').click();
      await clientRow.waitFor({ state: 'visible' });
      expect(await page.locator('[data-filter="client"]').getAttribute('aria-selected')).toBe('true');
      expect(await page.locator('#cert-list tr').filter({ hasText: serverName }).count()).toBe(0);
      await page.locator('[data-filter="all"]').click();
      await page.locator('#search-input').fill(san);
      await page.locator('#cert-list tr').filter({ hasText: serverName }).waitFor({ state: 'visible' });
      expect(await textOf(page, '#table-count')).toBe('显示 1 / 2 张证书');
    } finally {
      await page.close();
      await fixture.stop();
    }
  });

  test('shows API failures and preserves native form validation', async () => {
    const fixture = await startFixture();
    const page = await newPage();
    try {
      await page.route('**/api/registrations', (route) => route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ error: 'fixture unavailable' }) }));
      await page.goto(fixture.baseURL, { waitUntil: 'domcontentloaded' });
      await page.waitForFunction(() => document.querySelector('#connection-text')?.textContent === '服务未连接');
      await expectText(page, '#toast-region', '读取服务数据失败');
      expect(await textOf(page, '#metric-total')).toBe('0');
      await expectText(page, '#ca-status-title', '等待初始化');
      await expectText(page, '#registration-list', '登记信息暂不可用');
      await expectText(page, '#cert-list', '暂无证书');
      await page.unroute('**/api/registrations');

      await page.locator('[data-action="generate"]').click();
      await page.locator('#issue-name').fill('invalid name');
      await page.locator('#issue-submit').click();
      expect(await page.locator('#issue-name').evaluate((input) => (input as HTMLInputElement).validity.patternMismatch)).toBe(true);
      expect(await page.locator('#overlay').evaluate((overlay) => overlay.classList.contains('open'))).toBe(true);
    } finally {
      await page.close();
      await fixture.stop();
    }
  });

  test('keeps the dashboard usable at a mobile viewport', async () => {
    const fixture = await startFixture();
    const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
    page.setDefaultTimeout(240_000);
    try {
      await dashboard(page, fixture.baseURL);
      expect(await page.locator('.sidebar').isVisible()).toBe(true);
      expect(await page.locator('.page-heading .primary-button').isVisible()).toBe(true);
      await page.locator('[data-nav="certificates"]').click();
      expect(await page.locator('[data-nav="certificates"]').getAttribute('aria-current')).toBe('page');
      await page.locator('[data-nav="applications"]').click();
      expect(await page.locator('[data-nav="applications"]').getAttribute('aria-current')).toBe('page');
      expect(await page.getByRole('searchbox', { name: '搜索名称或 SAN' }).count()).toBe(1);
      await page.locator('[data-action="generate"]').click();
      await page.locator('input[name="type"][value="client"]').check();
      expect(await page.locator('#issue-sans').isDisabled()).toBe(true);
      expect(await page.locator('.drawer').isVisible()).toBe(true);
    } finally {
      await page.close();
      await fixture.stop();
    }
  });
});
