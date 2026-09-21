import { chromium, type Page } from 'playwright';
import { cp, mkdir, mkdtemp, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';

const repoRoot = resolve(import.meta.dir, '..');
const fixtureServer = join(repoRoot, 'e2e', 'fixture-server.py');
const screenshotDir = join(repoRoot, 'docs', 'screenshots');
const rootPassword = 'screenshot-root-password';
const adminUsername = 'screenshot-admin';
const adminPassword = 'screenshot-admin-password';
const agentName = 'screenshots-partner';
const agentUsername = 'screenshots-operator';
const agentPassword = 'screenshots-operator-password';
const serverName = 'screenshots-service';
const clientName = 'screenshots-client';
const agentCertName = 'screenshots-agent-service';
const appName = 'screenshots-orders';
const agentAppName = 'screenshots-agent-orders';

async function waitForReady(process: ReturnType<typeof Bun.spawn>): Promise<string> {
  if (!process.stdout || typeof process.stdout === 'number') throw new Error('fixture server has no stdout');
  const reader = process.stdout.getReader();
  const decoder = new TextDecoder();
  let output = '';
  try {
    while (true) {
      const chunk = await reader.read();
      if (chunk.done) break;
      output += decoder.decode(chunk.value, { stream: true });
      const line = output.split('\n').find((value) => value.startsWith('READY '));
      if (line) return line.slice('READY '.length).trim();
    }
  } finally {
    reader.releaseLock();
  }
  throw new Error(`fixture server exited before becoming ready: ${output}`);
}

async function screenshot(page: Page, filename: string): Promise<void> {
  await page.waitForTimeout(450);
  await page.evaluate(() => { const region = document.querySelector('#toast-region'); if (region) region.innerHTML = ''; });
  await page.screenshot({ path: join(screenshotDir, filename), fullPage: true });
}

async function waitForLogin(page: Page, baseURL: string): Promise<void> {
  await page.goto(baseURL, { waitUntil: 'domcontentloaded' });
  await page.locator('#login-form').waitFor({ state: 'visible' });
}

async function login(page: Page, baseURL: string, username: string, password: string): Promise<void> {
  await waitForLogin(page, baseURL);
  await page.locator('#login-username').fill(username);
  await page.locator('#login-password').fill(password);
  await page.locator('#login-form button[type="submit"]').click();
  await page.getByRole('heading', { name: '证书总览' }).waitFor({ state: 'visible' });
  await page.waitForFunction(() => document.querySelector('#connection-text')?.textContent === '服务在线');
}

async function waitForResponse(page: Page, url: string, method: string, action: () => Promise<void>): Promise<void> {
  const response = page.waitForResponse((value) => value.url() === url && value.request().method() === method);
  await action();
  const result = await response;
  if (!result.ok()) throw new Error(`request failed: ${result.status()} ${await result.text()}`);
}

async function apiJson<T>(baseURL: string, path: string, token: string): Promise<T> {
  const response = await fetch(`${baseURL}${path}`, { headers: { Accept: 'application/json', Authorization: `Bearer ${token}` } });
  if (!response.ok) throw new Error(`GET ${path}: ${response.status} ${await response.text()}`);
  return await response.json() as T;
}

async function apiLogin(baseURL: string): Promise<string> {
  const response = await fetch(`${baseURL}/api/auth/login`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ username: adminUsername, password: adminPassword }) });
  if (!response.ok) throw new Error(`admin login failed: ${response.status} ${await response.text()}`);
  return String((await response.json() as { token: string }).token);
}

await mkdir(screenshotDir, { recursive: true });
const fixtureRoot = await mkdtemp(join(tmpdir(), 'corntech-screenshots-'));
let serverProcess: ReturnType<typeof Bun.spawn> | undefined;
let browser: Awaited<ReturnType<typeof chromium.launch>> | undefined;
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
  await writeFile(join(fixtureRoot, '.fixture'), 'screenshots-v3\n');
  serverProcess = Bun.spawn(['python3', fixtureServer, '--root', fixtureRoot, '--web-dir', join(repoRoot, 'web')], {
    cwd: repoRoot,
    env: { ...process.env, ROOTPASS: rootPassword, WEB_USERNAME: adminUsername, WEB_PASSWORD: adminPassword, CERT_ALLOW_ANONYMOUS: '' },
    stdout: 'pipe',
    stderr: 'inherit',
  });
  const baseURL = await waitForReady(serverProcess);
  const adminToken = await apiLogin(baseURL);
  browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 }, deviceScaleFactor: 1 });
  page.setDefaultTimeout(240_000);

  await waitForLogin(page, baseURL);
  await screenshot(page, 'login.png');
  await login(page, baseURL, adminUsername, adminPassword);
  await screenshot(page, 'dashboard-empty.png');

  await page.locator('[data-action="init-ca"]').click();
  await screenshot(page, 'ca-initialization.png');
  await page.locator('#ca-rootpass').fill(rootPassword);
  await waitForResponse(page, `${baseURL}/api/ca/initialize`, 'POST', async () => page.locator('#ca-form button[type="submit"]').click());
  await page.waitForFunction(() => !document.querySelector('#overlay')?.classList.contains('open'));
  await page.waitForFunction(() => document.querySelector('#ca-status-title')?.textContent === 'CA 状态正常');

  await page.locator('[data-action="create-agent"]').click();
  await page.locator('#admin-name').fill(agentName);
  await screenshot(page, 'agent-create.png');
  await waitForResponse(page, `${baseURL}/api/agents`, 'POST', async () => page.locator('#agent-form button[type="submit"]').click());
  const agents = await apiJson<{ items: { id: number; name: string }[] }>(baseURL, '/api/agents', adminToken);
  const agent = agents.items.find((value) => value.name === agentName);
  if (!agent) throw new Error('created screenshot agent was not returned by the API');

  await page.locator('[data-action="create-account"]').click();
  await page.locator('#admin-name').fill(agentUsername);
  await page.locator('#admin-password').fill(agentPassword);
  await page.locator('#admin-agent-id').fill(String(agent.id));
  await screenshot(page, 'account-create.png');
  await waitForResponse(page, `${baseURL}/api/users`, 'POST', async () => page.locator('#account-form button[type="submit"]').click());
  await page.locator('#account-list').filter({ hasText: agentUsername }).waitFor({ state: 'visible' });
  await screenshot(page, 'agent-management.png');

  await page.locator('[data-action="generate"]').click();
  await page.locator('#issue-name').fill(serverName);
  await page.locator('#issue-sans').fill('api.screenshots.example\n10.0.0.40');
  await page.locator('#issue-key').selectOption('both');
  await screenshot(page, 'certificate-generation.png');
  await waitForResponse(page, `${baseURL}/api/certificates`, 'POST', async () => page.locator('#issue-submit').click());
  const serverRow = page.locator('#cert-list tr').filter({ hasText: serverName });
  await serverRow.waitFor({ state: 'visible' });

  await page.locator('[data-action="register"]').click();
  await page.locator('#registration-name').fill(appName);
  await page.locator('#registration-owner').fill('platform-team');
  await page.locator('#registration-notes').fill('Screenshot example application');
  await screenshot(page, 'application-registration.png');
  await waitForResponse(page, `${baseURL}/api/registrations`, 'POST', async () => page.locator('#registration-form button[type="submit"]').click());
  await page.locator('.registration-card').filter({ hasText: appName }).waitFor({ state: 'visible' });

  await page.locator('[data-action="generate"]').click();
  await page.locator('input[name="type"][value="client"]').check();
  await page.locator('#issue-name').fill(clientName);
  await page.locator('#issue-key').selectOption('ec');
  await waitForResponse(page, `${baseURL}/api/certificates`, 'POST', async () => page.locator('#issue-submit').click());
  await page.locator('#cert-list tr').filter({ hasText: clientName }).waitFor({ state: 'visible' });
  await page.waitForFunction(() => !document.querySelector('#overlay')?.classList.contains('open'));
  await screenshot(page, 'dashboard-managed.png');

  await serverRow.locator('[data-action="detail"]').click();
  await page.locator('.drawer h2').filter({ hasText: '证书详情' }).waitFor({ state: 'visible' });
  await screenshot(page, 'certificate-detail.png');
  await page.locator('.close-button').click();

  const agentPage = await browser.newPage({ viewport: { width: 1440, height: 1000 }, deviceScaleFactor: 1 });
  agentPage.setDefaultTimeout(240_000);
  await login(agentPage, baseURL, agentUsername, agentPassword);
  await screenshot(agentPage, 'agent-workspace.png');
  await agentPage.locator('[data-action="generate"]').click();
  await agentPage.locator('#issue-name').fill(agentCertName);
  await agentPage.locator('#issue-sans').fill('agent.screenshots.example');
  await agentPage.locator('#issue-key').selectOption('rsa');
  await screenshot(agentPage, 'agent-certificate-generation.png');
  await waitForResponse(agentPage, `${baseURL}/api/certificates`, 'POST', async () => agentPage.locator('#issue-submit').click());
  await agentPage.locator('#cert-list tr').filter({ hasText: agentCertName }).waitFor({ state: 'visible' });
  await agentPage.locator('[data-action="register"]').click();
  await agentPage.locator('#registration-name').fill(agentAppName);
  await agentPage.locator('#registration-owner').fill(agentUsername);
  await waitForResponse(agentPage, `${baseURL}/api/registrations`, 'POST', async () => agentPage.locator('#registration-form button[type="submit"]').click());
  await agentPage.locator('.registration-card').filter({ hasText: agentAppName }).waitFor({ state: 'visible' });
  await screenshot(agentPage, 'agent-workspace-managed.png');
  await agentPage.close();

  console.log(`V3 screenshots written to ${screenshotDir}`);
} finally {
  await browser?.close();
  serverProcess?.kill();
  if (serverProcess) await serverProcess.exited;
  await rm(fixtureRoot, { recursive: true, force: true });
}
