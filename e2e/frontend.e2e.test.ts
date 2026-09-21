import { afterAll, beforeAll, describe, expect, setDefaultTimeout, test } from 'bun:test';
import { chromium, type Browser, type BrowserContext, type Page } from 'playwright';
import { cp, mkdtemp, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';

setDefaultTimeout(240_000);

const repoRoot = resolve(import.meta.dir, '..');
const fixtureServer = join(repoRoot, 'e2e', 'fixture-server.py');
const rootPassword = 'e2e-root-password';

let browser: Browser;

type Fixture = { baseURL: string; stop: () => Promise<void> };
type FixtureOptions = { webUsername?: string; webPassword?: string; allowAnonymous?: boolean };

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

async function startFixture(options: FixtureOptions = {}): Promise<Fixture> {
  const fixtureRoot = await mkdtemp(join(tmpdir(), 'corntech-e2e-'));
  let serverProcess: ReturnType<typeof Bun.spawn> | undefined;
  let fixtureStderr: Promise<string> | undefined;
  try {
    await Promise.all([
      cp(join(repoRoot, 'ca.cnf'), join(fixtureRoot, 'ca.cnf')),
      cp(join(repoRoot, 'server.py'), join(fixtureRoot, 'server.py')),
      cp(join(repoRoot, 'flush.sh'), join(fixtureRoot, 'flush.sh')),
      cp(join(repoRoot, 'gen_root_cert.sh'), join(fixtureRoot, 'gen_root_cert.sh')),
      cp(join(repoRoot, 'gen_server_cert.sh'), join(fixtureRoot, 'gen_server_cert.sh')),
      cp(join(repoRoot, 'gen_client_cert.sh'), join(fixtureRoot, 'gen_client_cert.sh')),
      cp(join(repoRoot, 'scripts'), join(fixtureRoot, 'scripts'), { recursive: true }),
    ]);
    await writeFile(join(fixtureRoot, '.fixture'), 'isolated\n');
    const fixtureArgs = ['python3', fixtureServer, '--root', fixtureRoot, '--web-dir', join(repoRoot, 'web')];
    if (options.allowAnonymous !== false) fixtureArgs.push('--allow-anonymous');
    serverProcess = Bun.spawn(fixtureArgs, {
      cwd: repoRoot,
      env: {
        ...process.env,
        ROOTPASS: rootPassword,
        WEB_USERNAME: options.webUsername || '',
        WEB_PASSWORD: options.webPassword || '',
        CERT_ALLOW_ANONYMOUS: options.allowAnonymous === false ? '' : '1',
      },
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

async function apiRequest(baseURL: string, path: string, options: RequestInit = {}): Promise<Response> {
  return fetch(`${baseURL}${path}`, {
    ...options,
    headers: { ...(options.body ? { 'content-type': 'application/json' } : {}), ...(options.headers || {}) },
  });
}

async function apiPost(baseURL: string, path: string, body: Record<string, unknown>): Promise<Response> {
  return apiRequest(baseURL, `/api${path}`, { method: 'POST', body: JSON.stringify(body) });
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

async function refreshFromUI(page: Page, baseURL: string, selector: string): Promise<void> {
  const responses = Promise.all(['/api/certificates', '/api/registrations', '/api/ca'].map((path) => page.waitForResponse((response) => response.url() === `${baseURL}${path}` && response.request().method() === 'GET')));
  await page.locator(selector).click();
  expect((await responses).every((response) => response.status() === 200)).toBe(true);
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
      await page.waitForFunction(() => document.querySelector('#ca-status-title')?.textContent === 'CA 状态正常');
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

  test('completes the first-run golden path from CA setup to artifact downloads', async () => {
    const fixture = await startFixture();
    const page = await newPage();
    const suffix = Date.now();
    const appName = `golden-orders-${suffix}`;
    const serverName = `golden-server-${suffix}`;
    const clientName = `golden-client-${suffix}`;
    const san = `api.${serverName}.example`;
    try {
      await dashboard(page, fixture.baseURL);

      await page.locator('[data-action="init-ca"]').click();
      await page.locator('#ca-rootpass').fill(rootPassword);
      const caResponse = page.waitForResponse((response) => response.url() === `${fixture.baseURL}/api/ca/initialize` && response.request().method() === 'POST');
      await page.locator('#ca-form button[type="submit"]').click();
      expect((await caResponse).status()).toBe(200);
      await page.waitForFunction(() => !document.querySelector('#overlay')?.classList.contains('open'));
      await page.waitForFunction(() => document.querySelector('#ca-status-title')?.textContent === 'CA 状态正常');
      await expectText(page, '#ca-status-title', 'CA 状态正常');

      await page.locator('[data-action="register"]').click();
      await page.locator('#registration-name').fill(appName);
      await page.locator('#registration-owner').fill('golden-platform');
      await page.locator('#registration-notes').fill('production-like browser journey');
      const registrationResponse = page.waitForResponse((response) => response.url() === `${fixture.baseURL}/api/registrations` && response.request().method() === 'POST');
      await page.locator('#registration-form button[type="submit"]').click();
      expect((await registrationResponse).status()).toBe(201);
      await page.locator('.registration-card').filter({ hasText: appName }).waitFor({ state: 'visible' });

      await page.locator('[data-nav="certificates"]').click();
      expect(await page.locator('[data-nav="certificates"]').getAttribute('aria-current')).toBe('page');
      await page.locator('[data-nav="dashboard"]').click();
      expect(await page.locator('[data-nav="dashboard"]').getAttribute('aria-current')).toBe('page');

      await page.locator('[data-action="generate"]').click();
      await page.locator('#issue-name').fill(serverName);
      await page.locator('#issue-sans').fill(`${san}\n10.0.0.20`);
      await page.locator('#issue-days').fill('398');
      await page.locator('#issue-key').selectOption('both');
      const serverResponse = page.waitForResponse((response) => response.url() === `${fixture.baseURL}/api/certificates` && response.request().method() === 'POST');
      await page.locator('#issue-submit').click();
      expect((await serverResponse).status()).toBe(201);
      const serverRow = page.locator('#cert-list tr').filter({ hasText: serverName });
      await serverRow.waitFor({ state: 'visible', timeout: 180_000 });
      expect((await serverRow.locator('.key-pill').textContent())?.toLowerCase()).toBe('both');

      const serverId = await serverRow.locator('[data-action="detail"]').getAttribute('data-id');
      const serverDetail = page.waitForResponse((response) => response.url() === `${fixture.baseURL}/api/certificates/${serverId}` && response.request().method() === 'GET');
      await serverRow.locator('[data-action="detail"]').click();
      expect((await serverDetail).status()).toBe(200);
      await expectText(page, '.drawer', san);
      expect(await page.locator('.drawer .file-item a[download]').count()).toBeGreaterThan(0);
      const fileDownload = page.waitForEvent('download');
      await page.locator('.drawer .file-item a[download]').first().click();
      expect((await fileDownload).suggestedFilename()).toMatch(new RegExp(serverName));
      const zipDownload = page.waitForEvent('download');
      await page.locator('.drawer-footer a[download]').click();
      expect((await zipDownload).suggestedFilename()).toMatch(new RegExp(serverName));
      await page.locator('.close-button').click();

      await page.locator('[data-action="generate"]').click();
      await page.locator('input[name="type"][value="client"]').check();
      await page.locator('#issue-name').fill(clientName);
      await page.locator('#issue-days').fill('365');
      await page.locator('#issue-key').selectOption('both');
      await page.locator('#issue-rootpass').fill(rootPassword);
      expect(await page.locator('#issue-sans').isDisabled()).toBe(true);
      const clientResponse = page.waitForResponse((response) => response.url() === `${fixture.baseURL}/api/certificates` && response.request().method() === 'POST');
      await page.locator('#issue-submit').click();
      expect((await clientResponse).status()).toBe(201);
      const clientRow = page.locator('#cert-list tr').filter({ hasText: clientName });
      await clientRow.waitFor({ state: 'visible', timeout: 180_000 });
      expect((await clientRow.locator('.key-pill').textContent())?.toLowerCase()).toBe('both');
      const clientId = await clientRow.locator('[data-action="detail"]').getAttribute('data-id');
      const clientDetail = page.waitForResponse((response) => response.url() === `${fixture.baseURL}/api/certificates/${clientId}` && response.request().method() === 'GET');
      await clientRow.locator('[data-action="detail"]').click();
      expect((await clientDetail).status()).toBe(200);
      await expectText(page, '.drawer', '客户端证书');
      await expectText(page, '.drawer', '文件');
      await page.locator('.close-button').click();

      await page.locator('[data-filter="server"]').click();
      expect(await page.locator('#cert-list tr').filter({ hasText: serverName }).count()).toBe(1);
      expect(await page.locator('#cert-list tr').filter({ hasText: clientName }).count()).toBe(0);
      await page.locator('[data-filter="client"]').click();
      expect(await page.locator('#cert-list tr').filter({ hasText: clientName }).count()).toBe(1);
      await page.locator('[data-filter="all"]').click();
      await page.locator('#search-input').fill('does-not-exist');
      await expectText(page, '#cert-list', '没有匹配的证书');
      await page.locator('#search-input').fill(san);
      expect(await textOf(page, '#table-count')).toBe('显示 1 / 2 张证书');
      await page.locator('#search-input').fill('');

      await refreshFromUI(page, fixture.baseURL, '#refresh-button');
      await refreshFromUI(page, fixture.baseURL, '#table-refresh');
      await page.locator('[data-nav="applications"]').click();
      await page.locator('.registration-card').filter({ hasText: appName }).waitFor({ state: 'visible' });

      await page.reload({ waitUntil: 'domcontentloaded' });
      await dashboard(page, fixture.baseURL);
      expect(await textOf(page, '#metric-total')).toBe('2');
      await page.locator('#cert-list tr').filter({ hasText: serverName }).waitFor({ state: 'visible' });
      await page.locator('#cert-list tr').filter({ hasText: clientName }).waitFor({ state: 'visible' });
      await page.locator('.registration-card').filter({ hasText: appName }).waitFor({ state: 'visible' });
    } finally {
      await page.close();
      await fixture.stop();
    }
  });

  test('keeps documented API, download, and static compatibility paths working', async () => {
    const fixture = await startFixture();
    try {
      const health = await apiRequest(fixture.baseURL, '/api/health');
      expect(health.status).toBe(200);
      const healthBody = await health.json() as { status: string; certificates: number };
      expect(healthBody.status).toBe('ok');

      const ca = await apiRequest(fixture.baseURL, '/api/ca');
      const roots = await apiRequest(fixture.baseURL, '/api/roots');
      const v1Certificates = await apiRequest(fixture.baseURL, '/api/v1/certificates');
      expect(ca.status).toBe(200);
      expect(roots.status).toBe(200);
      const caBody = await ca.clone().text();
      expect(await roots.text()).toBe(caBody);
      expect(v1Certificates.status).toBe(200);
      expect((await v1Certificates.json()).total).toBe(0);

      const options = await apiRequest(fixture.baseURL, '/api/certificates', { method: 'OPTIONS' });
      expect(options.status).toBe(204);
      expect(options.headers.get('access-control-allow-methods')).toContain('POST');
      const assetHead = await apiRequest(fixture.baseURL, '/app.js', { method: 'HEAD' });
      const spaHead = await apiRequest(fixture.baseURL, '/dashboard', { method: 'HEAD' });
      expect(assetHead.status).toBe(200);
      expect(assetHead.headers.get('content-type')).toContain('javascript');
      expect(spaHead.status).toBe(200);
      expect(spaHead.headers.get('content-type')).toContain('text/html');

      const registration = await apiPost(fixture.baseURL, '/registrations', { name: 'compat-app', owner: 'e2e', notes: 'compatibility' });
      expect(registration.status).toBe(201);
      expect((await apiRequest(fixture.baseURL, '/api/registrations')).status).toBe(200);
      expect((await apiPost(fixture.baseURL, '/ca/initialize', { rootpass: rootPassword })).status).toBe(200);
      const badIssue = await apiPost(fixture.baseURL, '/certificates', { type: 'server', name: 'bad-input', sans: ['bad value'] });
      expect(badIssue.status).toBe(400);

      const issue = await apiPost(fixture.baseURL, '/certificates', { type: 'server', name: 'compat-service', sans: ['compat.example', '10.0.0.30'], days: 30, key_type: 'both', rootpass: rootPassword });
      expect(issue.status).toBe(201);
      const record = await issue.json() as { id: number; files: string[] };
      const serverList = await apiRequest(fixture.baseURL, '/api/certificates?type=server');
      const clientList = await apiRequest(fixture.baseURL, '/api/certificates?type=client');
      const v1ClientList = await apiRequest(fixture.baseURL, '/api/v1/certificates?type=client');
      expect((await serverList.json()).total).toBe(1);
      expect((await clientList.json()).total).toBe(0);
      expect((await v1ClientList.json()).total).toBe(0);
      const detail = await apiRequest(fixture.baseURL, `/api/certificates/${record.id}`);
      expect(detail.status).toBe(200);
      const detailBody = await detail.json() as { files: string[] };
      expect(detailBody.files.length).toBeGreaterThan(0);
      const firstFile = detailBody.files[0];
      const fileResponse = await apiRequest(fixture.baseURL, `/api/certificates/${record.id}/files/${encodeURIComponent(firstFile)}`);
      expect(fileResponse.status).toBe(200);
      expect((await fileResponse.arrayBuffer()).byteLength).toBeGreaterThan(0);
      const archive = await apiRequest(fixture.baseURL, `/api/certificates/${record.id}/download`);
      expect(archive.status).toBe(200);
      expect(archive.headers.get('content-type')).toContain('application/zip');
      expect(new Uint8Array(await archive.arrayBuffer()).slice(0, 2)).toEqual(new Uint8Array([80, 75]));
      for (const format of ['crt', 'key', 'p12', 'pem', 'bundle']) {
        const formatted = await apiRequest(fixture.baseURL, `/api/certificates/${record.id}/download?format=${format}`);
        expect(formatted.status).toBe(200);
        expect((await formatted.arrayBuffer()).byteLength).toBeGreaterThan(0);
      }
      expect((await apiRequest(fixture.baseURL, `/api/certificates/${record.id}/download?format=unknown`)).status).toBe(400);
      expect((await apiRequest(fixture.baseURL, `/api/certificates/${record.id}/files/%2e%2e%2fserver.py`)).status).toBe(404);
      expect((await apiRequest(fixture.baseURL, `/api/certificates/${record.id + 999}/download`)).status).toBe(404);
      expect((await apiRequest(fixture.baseURL, '/api/certificates/not-an-id')).status).toBe(400);
    } finally {
      await fixture.stop();
    }
  });

  test('protects the website entry and API with configured Basic Auth', async () => {
    const username = 'e2e-admin';
    const password = 'e2e-web-password';
    const fixture = await startFixture({ webUsername: username, webPassword: password, allowAnonymous: false });
    let context: BrowserContext | undefined;
    try {
      expect((await apiRequest(fixture.baseURL, '/api/health')).status).toBe(200);
      const publicEntry = await apiRequest(fixture.baseURL, '/');
      expect(publicEntry.status).toBe(200);
      expect(publicEntry.headers.get('content-type')).toContain('text/html');
      expect((await apiRequest(fixture.baseURL, '/api/certificates')).status).toBe(401);

      const authorization = `Basic ${btoa(`${username}:${password}`)}`;
      const authorized = await apiRequest(fixture.baseURL, '/', { headers: { Authorization: authorization } });
      expect(authorized.status).toBe(200);
      expect(authorized.headers.get('content-type')).toContain('text/html');

      context = await browser.newContext();
      const page = await context.newPage();
      page.setDefaultTimeout(30_000);
      await page.goto(fixture.baseURL, { waitUntil: 'domcontentloaded' });
      await page.getByRole('heading', { name: '登录证书中心' }).waitFor({ state: 'visible' });
      await page.locator('#login-username').fill(username);
      await page.locator('#login-password').fill(password);
      await page.locator('#login-form button[type="submit"]').click();
      await page.getByRole('heading', { name: '证书总览' }).waitFor({ state: 'visible' });
      await page.waitForFunction(() => document.querySelector('#connection-text')?.textContent === '服务在线');
    } finally {
      await context?.close();
      await fixture.stop();
    }
  });

  test('keeps the agent workspace scoped and hides administrator CA controls', async () => {
    const username = 'v3-admin';
    const password = 'v3-admin-password';
    const fixture = await startFixture({ webUsername: username, webPassword: password, allowAnonymous: false });
    const page = await newPage();
    const agentName = `agent-${Date.now()}`;
    const agentUser = `operator-${Date.now()}`;
    const agentPassword = 'operator-password';
    try {
      const adminLogin = await apiRequest(fixture.baseURL, '/api/auth/login', { method: 'POST', body: JSON.stringify({ username, password }) });
      expect(adminLogin.status).toBe(200);
      const adminToken = (await adminLogin.json() as { token: string }).token;
      const auth = { authorization: `Bearer ${adminToken}` };
      const agentResponse = await apiRequest(fixture.baseURL, '/api/agents', { method: 'POST', headers: auth, body: JSON.stringify({ name: agentName }) });
      expect(agentResponse.status).toBe(201);
      const agent = await agentResponse.json() as { id: number };
      const userResponse = await apiRequest(fixture.baseURL, '/api/users', { method: 'POST', headers: auth, body: JSON.stringify({ username: agentUser, password: agentPassword, role: 'agent', agent_id: agent.id }) });
      expect(userResponse.status).toBe(201);
      const caResponse = await apiRequest(fixture.baseURL, '/api/ca/initialize', { method: 'POST', headers: auth, body: JSON.stringify({ rootpass: rootPassword }) });
      expect(caResponse.status).toBe(200);

      await page.goto(fixture.baseURL, { waitUntil: 'domcontentloaded' });
      await page.locator('#login-form').waitFor({ state: 'visible' });
      await page.locator('#login-username').fill(agentUser);
      await page.locator('#login-password').fill(agentPassword);
      await page.locator('#login-form button[type="submit"]').click();
      await page.getByRole('heading', { name: '证书总览' }).waitFor({ state: 'visible' });
      await page.waitForFunction(() => document.querySelector('#connection-text')?.textContent === '服务在线');
      await expectText(page, '#profile-role', `代理商 · ${agentName}`);
      expect(await page.locator('[data-nav="agents"]').isHidden()).toBe(true);

      await page.locator('[data-action="generate"]').click();
      expect(await page.locator('#issue-rootpass').count()).toBe(0);
      const serverName = `agent-server-${Date.now()}`;
      await page.locator('#issue-name').fill(serverName);
      const issueResponse = page.waitForResponse((response) => response.url() === `${fixture.baseURL}/api/certificates` && response.request().method() === 'POST');
      await page.locator('#issue-submit').click();
      expect((await issueResponse).status()).toBe(201);
      await page.locator('#cert-list tr').filter({ hasText: serverName }).waitFor({ state: 'visible', timeout: 180_000 });
      await page.locator('[data-action="register"]').click();
      await page.locator('#registration-name').fill(`agent-app-${Date.now()}`);
      await page.locator('#registration-form button[type="submit"]').click();
      await expectText(page, '#applications-title', '我的应用');
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
