import { chromium } from 'playwright';
import { cp, mkdir, mkdtemp, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';

const repoRoot = resolve(import.meta.dir, '..');
const fixtureServer = join(repoRoot, 'e2e', 'fixture-server.py');
const screenshotDir = join(repoRoot, 'docs', 'screenshots');
const rootPassword = 'screenshot-root-password';
const serverName = 'screenshots-service';
const clientName = 'screenshots-client';
const appName = 'screenshots-orders';

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

async function waitForDashboard(page: import('playwright').Page, baseURL: string): Promise<void> {
  await page.goto(baseURL, { waitUntil: 'domcontentloaded' });
  await page.waitForFunction(() => document.querySelector('#connection-text')?.textContent === '服务在线');
  await page.getByRole('heading', { name: '证书总览' }).waitFor({ state: 'visible' });
}

async function waitForResponse(page: import('playwright').Page, url: string, action: () => Promise<void>): Promise<void> {
  const response = page.waitForResponse((value) => value.url() === url && value.request().method() === 'POST');
  await action();
  const result = await response;
  if (!result.ok()) throw new Error(`request failed: ${result.status()} ${await result.text()}`);
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
  await writeFile(join(fixtureRoot, '.fixture'), 'screenshots\n');
  serverProcess = Bun.spawn(['python3', fixtureServer, '--root', fixtureRoot, '--web-dir', join(repoRoot, 'web'), '--allow-anonymous'], {
    cwd: repoRoot,
    env: { ...process.env, ROOTPASS: rootPassword, WEB_USERNAME: '', WEB_PASSWORD: '' },
    stdout: 'pipe',
    stderr: 'inherit',
  });
  const baseURL = await waitForReady(serverProcess);
  browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 }, deviceScaleFactor: 1 });
  page.setDefaultTimeout(240_000);

  await waitForDashboard(page, baseURL);
  await page.screenshot({ path: join(screenshotDir, 'dashboard-empty.png'), fullPage: true });

  await page.locator('[data-action="init-ca"]').click();
  await page.screenshot({ path: join(screenshotDir, 'ca-initialization.png'), fullPage: true });
  await page.locator('#ca-rootpass').fill(rootPassword);
  await waitForResponse(page, `${baseURL}/api/ca/initialize`, async () => page.locator('#ca-form button[type="submit"]').click());
  await page.waitForFunction(() => !document.querySelector('#overlay')?.classList.contains('open'));
  await page.waitForFunction(() => document.querySelector('#ca-status-title')?.textContent === 'CA 状态正常');

  await page.locator('[data-action="generate"]').click();
  await page.locator('#issue-name').fill(serverName);
  await page.locator('#issue-sans').fill('api.screenshots.example\n10.0.0.40');
  await page.locator('#issue-key').selectOption('both');
  await page.screenshot({ path: join(screenshotDir, 'certificate-generation.png'), fullPage: true });
  await waitForResponse(page, `${baseURL}/api/certificates`, async () => page.locator('#issue-submit').click());
  const serverRow = page.locator('#cert-list tr').filter({ hasText: serverName });
  await serverRow.waitFor({ state: 'visible' });

  await page.locator('[data-action="register"]').click();
  await page.locator('#registration-name').fill(appName);
  await page.locator('#registration-owner').fill('platform-team');
  await page.locator('#registration-notes').fill('Screenshot example application');
  await page.screenshot({ path: join(screenshotDir, 'application-registration.png'), fullPage: true });
  await waitForResponse(page, `${baseURL}/api/registrations`, async () => page.locator('#registration-form button[type="submit"]').click());
  await page.locator('.registration-card').filter({ hasText: appName }).waitFor({ state: 'visible' });

  await page.locator('[data-action="generate"]').click();
  await page.locator('input[name="type"][value="client"]').check();
  await page.locator('#issue-name').fill(clientName);
  await page.locator('#issue-key').selectOption('ec');
  await waitForResponse(page, `${baseURL}/api/certificates`, async () => page.locator('#issue-submit').click());
  await page.locator('#cert-list tr').filter({ hasText: clientName }).waitFor({ state: 'visible' });
  await page.waitForFunction(() => !document.querySelector('#overlay')?.classList.contains('open'));
  await Bun.sleep(350);

  await page.screenshot({ path: join(screenshotDir, 'dashboard-managed.png'), fullPage: true });
  await serverRow.locator('[data-action="detail"]').click();
  await page.locator('.drawer h2').filter({ hasText: '证书详情' }).waitFor({ state: 'visible' });
  await page.screenshot({ path: join(screenshotDir, 'certificate-detail.png'), fullPage: true });

  console.log(`Screenshots written to ${screenshotDir}`);
} finally {
  await browser?.close();
  serverProcess?.kill();
  if (serverProcess) await serverProcess.exited;
  await rm(fixtureRoot, { recursive: true, force: true });
}
