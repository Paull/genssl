import { chromium } from 'playwright';
import { mkdir } from 'node:fs/promises';
import { resolve } from 'node:path';

const root = resolve(import.meta.dir, '..');
const outputDir = resolve(root, 'docs/screenshots');
const port = 18765;

const selectionPage = `<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>选择客户端证书</title>
<style>
:root{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:#172b4d;background:#f4f7fb}
*{box-sizing:border-box}body{margin:0;min-height:100vh;background:linear-gradient(135deg,#f6f9fd,#e7eef7);display:grid;place-items:center}
.browser{width:980px;min-height:590px;border:1px solid #cbd7e5;border-radius:14px;background:white;box-shadow:0 28px 80px #142b4528;overflow:hidden}
.chrome{height:54px;display:flex;align-items:center;gap:10px;padding:0 20px;background:#f7f9fc;border-bottom:1px solid #dce4ed;color:#64758a;font-size:13px}
.dot{width:10px;height:10px;border-radius:50%;background:#c6d0dc}.address{flex:1;padding:9px 14px;border:1px solid #d9e2ec;border-radius:8px;background:#fff;color:#50647a}.lock{color:#22936f}
.content{position:relative;min-height:536px;padding:72px 100px}.app h1{margin:0 0 10px;font-size:28px}.app p{margin:0;color:#71839a}.status{display:inline-flex;gap:8px;margin-top:26px;padding:9px 12px;border-radius:7px;background:#eaf8f1;color:#1d805d;font-size:13px}
.veil{position:absolute;inset:0;background:#0f274660}.dialog{position:absolute;top:70px;left:50%;width:590px;transform:translateX(-50%);padding:28px 30px 24px;border-radius:13px;background:#fff;box-shadow:0 20px 70px #0d213e52}.dialog h2{margin:0;font-size:21px}.dialog .hint{margin:8px 0 22px;color:#71839a;font-size:13px}.cert{display:flex;align-items:center;gap:14px;padding:14px;border:1px solid #d8e2ec;border-radius:9px;margin:10px 0;background:#fbfdff}.cert.active{border-color:#2d8df0;background:#f1f7ff;box-shadow:0 0 0 2px #2d8df022}.shield{display:grid;place-items:center;width:38px;height:38px;border-radius:10px;background:#e4f1ff;color:#1876d2;font-size:19px}.cert strong{display:block;font-size:14px}.cert small{display:block;margin-top:4px;color:#8090a1;font-size:12px}.check{margin-left:auto;color:#1c83e4;font-weight:700}.actions{display:flex;justify-content:flex-end;gap:10px;margin-top:22px}.button{padding:10px 18px;border-radius:7px;border:1px solid #d5e0ea;background:#fff;color:#5a6d81;font-size:13px}.primary{border-color:#237fe0;background:#237fe0;color:#fff}
</style></head><body><main class="browser"><header class="chrome"><i class="dot"></i><i class="dot"></i><i class="dot"></i><span class="address"><b class="lock">⌕</b> https://mtls.example.dev/secure</span><span>⋮</span></header><section class="content"><div class="app"><h1>订单服务</h1><p>此页面需要客户端证书完成双向 TLS 身份认证。</p><span class="status">● 已连接到安全网关</span></div><div class="veil"></div><section class="dialog" role="dialog" aria-label="选择客户端证书"><h2>选择客户端证书</h2><p class="hint">服务器请求使用客户端证书。请选择要用于此站点的证书。</p><div class="cert active"><span class="shield">✓</span><div><strong>CornTech · screenshots-operator</strong><small>颁发者：Corn Root CA　有效期至：2028-09-21</small></div><span class="check">✓</span></div><div class="cert"><span class="shield">✓</span><div><strong>CornTech · local-developer</strong><small>颁发者：Corn Root CA　有效期至：2028-09-21</small></div></div><div class="actions"><button class="button">取消</button><button class="button primary">继续</button></div></section></section></main></body></html>`;

const server = Bun.serve({ port, fetch(request) {
  const path = new URL(request.url).pathname;
  if (path === '/protected') {
    return new Response('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>400 Bad Request</title><style>body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#f4f7fb;color:#172b4d;margin:0;display:grid;place-items:center;min-height:100vh}.card{width:650px;padding:42px;border:1px solid #ead4d4;border-radius:14px;background:#fff;box-shadow:0 22px 70px #42171718}.code{display:inline-block;padding:6px 10px;border-radius:6px;background:#fff0f0;color:#c63b46;font-weight:800}.muted{color:#71839a}</style><main class="card"><span class="code">400 Bad Request</span><h1>需要客户端证书</h1><p class="muted">此 HTTPS 端点要求客户端提供证书，但当前请求没有完成客户端身份认证。</p><p>请在浏览器中选择由 Corn Root CA 签发的客户端证书后重试。</p></main></html>', { status: 400, headers: { 'Content-Type': 'text/html; charset=utf-8' } });
  }
  if (path === '/select') return new Response(selectionPage, { headers: { 'Content-Type': 'text/html; charset=utf-8' } });
  return new Response('not found', { status: 404 });
} });

await mkdir(outputDir, { recursive: true });
const browser = await chromium.launch({ headless: true });
try {
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 });
  const badResponse = await page.goto(`http://127.0.0.1:${port}/protected`, { waitUntil: 'domcontentloaded' });
  if (badResponse?.status() !== 400) throw new Error(`expected 400, got ${badResponse?.status()}`);
  await page.screenshot({ path: resolve(outputDir, 'mtls-no-client-certificate-400.png'), fullPage: true });
  await page.goto(`http://127.0.0.1:${port}/select`, { waitUntil: 'domcontentloaded' });
  await page.getByRole('dialog', { name: '选择客户端证书' }).waitFor({ state: 'visible' });
  await page.screenshot({ path: resolve(outputDir, 'mtls-client-certificate-selection.png'), fullPage: true });
  console.log('mTLS screenshots written');
} finally {
  await browser.close();
  server.stop();
}
