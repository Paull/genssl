import './styles.css';

type CertType = 'server' | 'client';
type Filter = 'all' | CertType;
interface Certificate {
  id: number;
  name: string;
  type?: CertType;
  cert_type?: CertType;
  key_type?: string;
  sans?: string[];
  days?: number;
  created_at?: string;
  expires_at?: string;
  status?: string;
  files?: string[];
  download_url?: string;
}
interface Registration { id: number; name: string; owner?: string; notes?: string; created_at?: string; }
interface CAStatus { available?: boolean; roots?: { name: string; available: boolean }[]; }
type DataErrors = { certificates: string | null; registrations: string | null; ca: string | null; }

const API = '/api';
const state: { certificates: Certificate[]; registrations: Registration[]; ca: CAStatus | null; filter: Filter; query: string; service: boolean; loadError: string | null; errors: DataErrors } = {
  certificates: [], registrations: [], ca: null, filter: 'all', query: '', service: false, loadError: null, errors: { certificates: null, registrations: null, ca: null },
};
let toastTimer: ReturnType<typeof setTimeout> | undefined;
let lastFocused: HTMLElement | null = null;

const svg = (name: string, cls = '') => {
  const paths: Record<string, string> = {
    grid: '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>',
    certificate: '<path d="M7 3h8l4 4v14H7a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2Z"/><path d="M15 3v5h5M9 13h6M9 17h4"/>',
    users: '<path d="M16 20v-1.5a3.5 3.5 0 0 0-3.5-3.5h-5A3.5 3.5 0 0 0 4 18.5V20M10 11a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7ZM17 4.5a3 3 0 0 1 0 5.8M20 20v-1.4a3.5 3.5 0 0 0-2.5-3.35"/>',
    shield: '<path d="M12 3 20 6v5c0 5.1-3.4 8.9-8 10-4.6-1.1-8-4.9-8-10V6l8-3Z"/><path d="m8.5 12 2.2 2.2 4.8-5"/>',
    settings: '<path d="M12 15.5a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7Z"/><path d="m19.4 15 .1.1a2 2 0 1 1-2.8 2.8l-.1-.1M4.6 9l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1M15 4.6l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1M9 19.4l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1M19 12h2M3 12h2M12 3v2M12 19v2"/>',
    plus: '<path d="M12 5v14M5 12h14"/>',
    search: '<circle cx="10.8" cy="10.8" r="6.8"/><path d="m16 16 5 5"/>',
    refresh: '<path d="M20 11a8 8 0 1 0 1 4"/><path d="M20 5v6h-6"/>',
    download: '<path d="M12 3v12M7 11l5 5 5-5M4 20h16"/>',
    eye: '<path d="M2.5 12s3.5-6 9.5-6 9.5 6 9.5 6-3.5 6-9.5 6-9.5-6-9.5-6Z"/><circle cx="12" cy="12" r="2.5"/>',
    close: '<path d="m6 6 12 12M18 6 6 18"/>',
    check: '<path d="m5 12 4.5 4.5L19 7"/>',
    clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    link: '<path d="M10 13.5a4 4 0 0 0 5.7.2l2-2a4 4 0 0 0-5.7-5.7l-1.2 1.2M14 10.5a4 4 0 0 0-5.7-.2l-2 2A4 4 0 0 0 8 18l1.2-1.2"/>',
  };
  return `<svg class="${cls}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths[name] || paths.certificate}</svg>`;
};

const esc = (value: unknown): string => String(value ?? '').replace(/[&<>"']/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char] || char));
const el = <T extends Element>(selector: string): T => document.querySelector(selector) as T;
const all = <T extends Element>(selector: string): T[] => [...document.querySelectorAll(selector)] as T[];
const certType = (cert: Certificate): CertType => cert.type || cert.cert_type || 'server';
const certId = (cert: Certificate): string => String(cert.id);
const date = (value?: string): string => { if (!value) return '—'; const d = new Date(value); return Number.isNaN(d.valueOf()) ? '日期无效' : d.toLocaleDateString('zh-CN', { year: 'numeric', month: 'short', day: 'numeric' }); };
const dateTime = (value?: string): string => { if (!value) return '—'; const d = new Date(value); return Number.isNaN(d.valueOf()) ? '日期无效' : d.toLocaleString('zh-CN', { dateStyle: 'medium', timeStyle: 'short' }); };
const daysLeft = (cert: Certificate): number | null => {
  if (!cert.expires_at) return null;
  const difference = new Date(cert.expires_at).valueOf() - Date.now();
  if (Number.isNaN(difference)) return null;
  return difference < 0 ? Math.floor(difference / 86400000) : Math.ceil(difference / 86400000);
};
const visibleCerts = (): Certificate[] => state.certificates.filter((cert) => (state.filter === 'all' || certType(cert) === state.filter) && (!state.query || `${cert.name} ${(cert.sans || []).join(' ')}`.toLowerCase().includes(state.query.toLowerCase())));
const initials = (name: string): string => name.trim().split(/\s+/).map((part) => part[0]).join('').slice(0, 2).toUpperCase() || '—';

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(API + path, { ...options, headers: { Accept: 'application/json', ...(options.body ? { 'Content-Type': 'application/json' } : {}), ...(options.headers || {}) } });
  const contentType = response.headers.get('content-type') || '';
  const payload = contentType.includes('json') ? await response.json() : await response.text();
  if (!response.ok) throw new Error(typeof payload === 'object' && payload ? (payload.error || payload.detail || '请求失败') : String(payload || response.statusText));
  return payload as T;
}

function shell(): string {
  return `<aside class="sidebar">
    <div class="brand"><div class="brand-mark">${svg('shield')}</div><div class="brand-text"><strong>CornTech</strong><span>证书中心 · 单机版</span></div></div>
    <div class="sidebar-divider"></div><div class="nav-caption">工作台</div>
    <nav class="nav-list" aria-label="工作区"><button class="nav-item active" data-nav="dashboard" aria-current="page">${svg('grid')}<span>总览</span><b class="nav-count" id="nav-total">—</b></button><button class="nav-item" data-nav="certificates" aria-current="false">${svg('certificate')}<span>证书</span></button><button class="nav-item" data-nav="applications" aria-current="false">${svg('users')}<span>应用登记</span></button></nav>
    <div class="sidebar-bottom"><div class="ca-mini"><div class="ca-mini-head"><span>${svg('shield')} CA 根状态</span><i class="ca-pulse warn" id="ca-pulse"></i></div><div class="ca-mini-value" id="ca-mini-value">正在检查…</div><small id="ca-mini-meta">RSA · EC</small></div><div class="profile"><div class="avatar">CT</div><div><strong>本地工作区</strong><span>无远程连接</span></div></div></div>
  </aside>
  <main class="main"><header class="topbar"><div class="breadcrumb"><strong>工作台</strong><span>　/　证书总览</span></div><div class="top-actions"><span class="connection"><i class="connection-dot offline" id="connection-dot"></i><span id="connection-text">连接中…</span></span><button class="icon-button" id="refresh-button" title="刷新数据" aria-label="刷新数据">${svg('refresh')}</button></div></header>
    <section class="page-heading" id="dashboard-section"><div><h1>证书总览</h1><p>统一签发、登记和管理应用证书</p></div><button class="primary-button" data-action="generate">${svg('plus')}生成证书</button></section>
    <section class="metrics"><article class="metric-card"><div class="metric-top"><span>证书总数</span><i class="metric-icon blue">${svg('certificate')}</i></div><strong class="metric-value" id="metric-total">—</strong><span class="metric-detail" id="metric-total-detail">等待数据</span></article><article class="metric-card"><div class="metric-top"><span>服务器证书</span><i class="metric-icon teal">${svg('shield')}</i></div><strong class="metric-value" id="metric-server">—</strong><span class="metric-detail">含 SAN 域名</span></article><article class="metric-card"><div class="metric-top"><span>客户端证书</span><i class="metric-icon orange">${svg('users')}</i></div><strong class="metric-value" id="metric-client">—</strong><span class="metric-detail">应用身份认证</span></article><article class="metric-card"><div class="metric-top"><span>即将到期</span><i class="metric-icon red">${svg('clock')}</i></div><strong class="metric-value" id="metric-expiring">—</strong><span class="metric-detail">未来 30 天内</span></article></section>
    <div class="content-grid"><section class="panel" id="certificates-section"><div class="panel-header"><div><h2>证书清单</h2><p id="list-caption">所有已签发证书</p></div><div class="panel-tools"><div class="search-box"><label class="sr-only" for="search-input">搜索名称或 SAN</label>${svg('search')}<input id="search-input" type="search" placeholder="搜索名称或 SAN" autocomplete="off" /></div><div class="segmented" role="tablist" aria-label="证书类型"><button class="active" role="tab" aria-selected="true" data-filter="all">全部</button><button role="tab" aria-selected="false" data-filter="server">服务器</button><button role="tab" aria-selected="false" data-filter="client">客户端</button></div></div></div><div class="cert-table-wrap"><table><thead><tr><th>证书名称</th><th>类型</th><th>密钥</th><th>有效期</th><th>状态</th><th></th></tr></thead><tbody id="cert-list"><tr class="empty-row"><td colspan="6">正在读取证书…</td></tr></tbody></table></div><div class="table-footer"><span id="table-count">—</span><button class="ghost-button compact" id="table-refresh" aria-label="刷新证书清单">${svg('refresh')}刷新</button></div></section><aside class="side-stack"><section class="panel activity-panel"><div class="panel-header"><div><h2>最近动态</h2><p>本地操作记录</p></div></div><div class="activity-list" id="activity-list"><div class="no-data">暂无动态</div></div></section><section class="panel ca-panel"><div class="panel-header"><div><h2>CA 根证书</h2><p>签发链状态</p></div></div><div class="ca-body"><div class="ca-status-line"><i class="status-mark" id="ca-status-icon">${svg('clock')}</i><div><strong id="ca-status-title">正在检查</strong><span id="ca-status-subtitle">读取本地根证书</span></div></div><div class="ca-roots" id="ca-roots"></div><button class="ghost-button compact ca-action" data-action="init-ca">初始化 / 更新 CA</button></div></section></aside></div>
    <section class="panel registrations-panel" id="applications-section"><div class="panel-header"><div><h2>应用登记</h2><p>追踪证书归属的应用与容器</p></div><button class="ghost-button compact" data-action="register">${svg('plus')}登记应用</button></div><div class="registration-list" id="registration-list"><div class="no-data">正在读取登记信息…</div></div></section>
  </main><div id="overlay" class="overlay" aria-hidden="true"></div><div id="toast-region" aria-live="polite"></div>`;
}

function renderMetrics(): void {
  if (state.errors.certificates) {
    el<HTMLElement>('#metric-total').textContent = '—';
    el<HTMLElement>('#metric-server').textContent = '—';
    el<HTMLElement>('#metric-client').textContent = '—';
    el<HTMLElement>('#metric-expiring').textContent = '—';
    el<HTMLElement>('#metric-total-detail').textContent = '数据暂不可用';
    el<HTMLElement>('#nav-total').textContent = '—';
    return;
  }
  const total = state.certificates.length;
  const server = state.certificates.filter((c) => certType(c) === 'server').length;
  const client = total - server;
  const expiring = state.certificates.filter((c) => { const left = daysLeft(c); return left !== null && left >= 0 && left <= 30; }).length;
  el<HTMLElement>('#metric-total').textContent = String(total);
  el<HTMLElement>('#metric-server').textContent = String(server);
  el<HTMLElement>('#metric-client').textContent = String(client);
  el<HTMLElement>('#metric-expiring').textContent = String(expiring);
  el<HTMLElement>('#metric-total-detail').innerHTML = total ? '<em>●</em>　签发记录已同步' : '尚未签发证书';
  el<HTMLElement>('#nav-total').textContent = String(total);
}
function renderCerts(): void {
  const list = visibleCerts();
  const body = el<HTMLElement>('#cert-list');
  el<HTMLElement>('#table-count').textContent = state.errors.certificates ? '证书数据不可用' : `显示 ${list.length} / ${state.certificates.length} 张证书`;
  el<HTMLElement>('#list-caption').textContent = state.errors.certificates ? '无法读取证书服务' : state.query ? `“${state.query}”的搜索结果` : state.filter === 'all' ? '所有已签发证书' : `${state.filter === 'server' ? '服务器' : '客户端'}证书`;
  if (!list.length) {
    const empty = state.errors.certificates ? '无法读取证书，请检查服务连接' : state.certificates.length ? '没有匹配的证书' : '暂无证书，请点击右上角生成';
    body.innerHTML = `<tr class="empty-row"><td colspan="6">${empty}</td></tr>`;
    return;
  }
  body.innerHTML = list.map((cert) => {
    const type = certType(cert);
    const left = daysLeft(cert);
    const statusClass = left === null ? 'unknown' : left < 0 ? 'expired' : left <= 30 ? 'warn' : '';
    const statusText = left === null ? '未知' : left < 0 ? '已过期' : left <= 30 ? `${left} 天后到期` : '有效';
    return `<tr><td><div class="cert-name"><i class="cert-mark ${type === 'client' ? 'client' : ''}">${svg(type === 'client' ? 'users' : 'shield')}</i><div><strong title="${esc(cert.name)}">${esc(cert.name)}</strong><small>ID #${esc(cert.id)} · ${esc((cert.sans || []).length ? `${cert.sans?.length} 个 SAN` : '无 SAN')}</small></div></div></td><td><span class="type-pill ${type}">${type === 'server' ? '服务器' : '客户端'}</span></td><td><span class="key-pill">${esc(cert.key_type || 'RSA')}</span></td><td><div class="date-cell"><strong>${esc(date(cert.expires_at))}</strong><small>签发于 ${esc(date(cert.created_at))}</small></div></td><td><span class="status-pill ${statusClass}"><i>●</i>${statusText}</span></td><td><div class="row-actions"><button class="row-action" data-action="detail" data-id="${esc(cert.id)}" title="查看详情" aria-label="查看 ${esc(cert.name)} 详情">${svg('eye')}</button><a class="row-action" href="${API}/certificates/${encodeURIComponent(certId(cert))}/download" title="下载 ZIP" aria-label="下载 ${esc(cert.name)} ZIP">${svg('download')}</a></div></td></tr>`;
  }).join('');
}
function renderCA(): void {
  const ca = state.ca;
  const unavailable = Boolean(state.errors.ca);
  const available = Boolean(ca?.available);
  const pulse = el<HTMLElement>('#ca-pulse');
  pulse.className = `ca-pulse${available ? '' : ' warn'}`;
  el<HTMLElement>('#ca-mini-value').textContent = unavailable ? '服务未连接' : available ? '运行正常 · 双根 CA' : '尚未初始化';
  el<HTMLElement>('#ca-mini-meta').textContent = unavailable ? '无法读取根证书状态' : available ? 'RSA · EC 根证书可用' : '请先初始化根证书';
  el<HTMLElement>('#ca-status-title').textContent = unavailable ? '服务不可用' : available ? 'CA 状态正常' : '等待初始化';
  el<HTMLElement>('#ca-status-subtitle').textContent = unavailable ? '请检查 API 服务连接' : available ? 'RSA / EC 根证书均已就绪' : '需要根密码以生成证书';
  el<HTMLElement>('#ca-status-icon').innerHTML = svg(available ? 'check' : 'clock');
  const roots = ca?.roots || [];
  el<HTMLElement>('#ca-roots').innerHTML = unavailable ? '<div class="ca-root"><span>根证书</span><span class="off">服务不可用</span></div>' : roots.length ? roots.map((root) => `<div class="ca-root"><span>${esc(root.name.replace('_', ' ').replace('.crt', '').toUpperCase())}</span><span class="${root.available ? '' : 'off'}">${root.available ? '可用' : '缺失'}</span></div>`).join('') : '<div class="ca-root"><span>根证书</span><span class="off">未初始化</span></div>';
}
function renderRegistrations(): void {
  const target = el<HTMLElement>('#registration-list');
  if (state.errors.registrations && !state.registrations.length) { target.innerHTML = '<div class="no-data">登记信息暂不可用，请检查服务连接</div>'; return; }
  if (!state.registrations.length) { target.innerHTML = '<div class="no-data">暂无应用登记，点击右侧按钮添加</div>'; return; }
  target.innerHTML = state.registrations.slice(0, 6).map((item) => `<div class="registration-card"><i class="registration-avatar">${esc(initials(item.name))}</i><div><strong title="${esc(item.name)}">${esc(item.name)}</strong><span>${esc(item.owner || '未填写负责人')}</span></div></div>`).join('');
}
function renderActivity(): void {
  const target = el<HTMLElement>('#activity-list');
  if (state.errors.certificates && !state.certificates.length) { target.innerHTML = '<div class="no-data">动态暂不可用，请检查服务连接</div>'; return; }
  const items = [...state.certificates].sort((a, b) => new Date(b.created_at || 0).valueOf() - new Date(a.created_at || 0).valueOf()).slice(0, 4);
  if (!items.length) { target.innerHTML = '<div class="no-data">暂无签发记录</div>'; return; }
  target.innerHTML = items.map((cert, index) => `<div class="activity-item"><i class="activity-dot ${index % 3 === 1 ? 'teal' : index % 3 === 2 ? 'orange' : ''}"></i><div><strong>${certType(cert) === 'server' ? '签发服务器证书' : '签发客户端证书'} · ${esc(cert.name)}</strong><span>${esc(dateTime(cert.created_at))}</span></div></div>`).join('');
}
function setConnected(connected: boolean): void { state.service = connected; el<HTMLElement>('#connection-dot').className = `connection-dot${connected ? '' : ' offline'}`; el<HTMLElement>('#connection-text').textContent = connected ? '服务在线' : '服务未连接'; }
function toast(message: string, kind = ''): void { const region = el<HTMLElement>('#toast-region'); region.innerHTML = `<div class="toast ${kind}">${esc(message)}</div>`; if (toastTimer) clearTimeout(toastTimer); toastTimer = setTimeout(() => { region.innerHTML = ''; }, 4200); }

async function loadData(): Promise<void> {
  const results = await Promise.allSettled([
    request<{ items?: Certificate[] }>('/certificates'),
    request<{ items?: Registration[] }>('/registrations'),
    request<CAStatus>('/ca'),
  ]);
  const [certResult, registrationResult, caResult] = results;
  const message = (reason: unknown): string => reason instanceof Error ? reason.message : String(reason);
  state.errors = {
    certificates: certResult.status === 'rejected' ? message(certResult.reason) : null,
    registrations: registrationResult.status === 'rejected' ? message(registrationResult.reason) : null,
    ca: caResult.status === 'rejected' ? message(caResult.reason) : null,
  };
  if (certResult.status === 'fulfilled') {
    const payload = certResult.value;
    state.certificates = Array.isArray(payload) ? payload : payload.items || [];
  }
  if (registrationResult.status === 'fulfilled') {
    const payload = registrationResult.value;
    state.registrations = Array.isArray(payload) ? payload : payload.items || [];
  }
  if (caResult.status === 'fulfilled') state.ca = caResult.value;
  const errors = Object.entries(state.errors).filter(([, value]) => value).map(([name, value]) => `${name}: ${value}`);
  state.loadError = errors.length ? errors.join('；') : null;
  setConnected(!state.loadError);
  if (state.loadError) toast(`读取服务数据失败：${state.loadError}`, 'error');
  renderMetrics(); renderCerts(); renderCA(); renderRegistrations(); renderActivity();
}

function closeDrawer(): void { const overlay = el<HTMLElement>('#overlay'); overlay.classList.remove('open'); overlay.setAttribute('aria-hidden', 'true'); setTimeout(() => { if (!overlay.classList.contains('open')) { overlay.innerHTML = ''; lastFocused?.focus(); lastFocused = null; } }, 240); }
function openDrawer(content: string): void { const overlay = el<HTMLElement>('#overlay'); lastFocused = document.activeElement instanceof HTMLElement ? document.activeElement : null; overlay.innerHTML = `<section class="drawer" role="dialog" aria-modal="true" aria-labelledby="drawer-title">${content}</section>`; overlay.classList.add('open'); overlay.setAttribute('aria-hidden', 'false'); overlay.querySelector<HTMLElement>('.close-button, input, button, a')?.focus(); }
function openGenerate(): void {
  openDrawer(`<div class="drawer-header"><div><h2 id="drawer-title">生成证书</h2><p>使用本地 CA 为应用签发新证书</p></div><button class="close-button" data-action="close" aria-label="关闭">${svg('close')}</button></div><form id="issue-form" class="drawer-body"><div class="form-section"><h3 class="form-section-title">证书用途</h3><div class="radio-grid"><label class="radio-card"><input type="radio" name="type" value="server" checked /><strong>服务器证书</strong><span>域名 / IP 服务</span></label><label class="radio-card"><input type="radio" name="type" value="client" /><strong>客户端证书</strong><span>应用身份认证</span></label></div><label class="form-label" for="issue-name">名称 / Common Name</label><input class="form-control" id="issue-name" name="name" required pattern="[A-Za-z0-9._\\-]{1,128}" placeholder="例如 api.example.dev" /><p class="form-hint">名称会作为输出目录和证书 CN，仅支持字母、数字、点、下划线和短横线。</p><label class="form-label" for="issue-sans">SAN（可选）</label><textarea class="form-control" id="issue-sans" name="sans" placeholder="每行或逗号分隔，例如：\napi.example.dev\n10.0.0.10"></textarea><p class="form-hint">服务器证书可添加多个域名或 IP；客户端证书无需填写。</p></div><div class="form-section"><h3 class="form-section-title">有效期与密钥</h3><div class="form-row"><div><label class="form-label" for="issue-days">有效期（天）</label><input class="form-control" id="issue-days" name="days" type="number" min="1" max="7300" value="398" /></div><div><label class="form-label" for="issue-key">密钥类型</label><select class="form-control" id="issue-key" name="key_type"><option value="both">RSA + EC</option><option value="rsa">RSA 2048</option><option value="ec">EC P-256</option></select></div></div><label class="form-label" for="issue-rootpass">根 CA 密码（可选）</label><input class="form-control" id="issue-rootpass" name="rootpass" type="password" autocomplete="new-password" placeholder="未设置 ROOTPASS 时填写" /></div><div class="drawer-footer"><button type="button" class="ghost-button" data-action="close">取消</button><button type="submit" class="primary-button" id="issue-submit">${svg('certificate')}生成并登记</button></div></form>`);
  el<HTMLInputElement>('#issue-name').focus();
}
function openRegistration(): void {
  openDrawer(`<div class="drawer-header"><div><h2 id="drawer-title">登记应用</h2><p>记录证书归属的应用或容器</p></div><button class="close-button" data-action="close" aria-label="关闭">${svg('close')}</button></div><form id="registration-form" class="drawer-body"><div class="form-section"><h3 class="form-section-title">应用信息</h3><label class="form-label" for="registration-name">应用 / 容器名称</label><input class="form-control" id="registration-name" name="name" required maxlength="128" placeholder="例如 orders-api" /><label class="form-label" for="registration-owner">负责人</label><input class="form-control" id="registration-owner" name="owner" maxlength="256" placeholder="例如 platform-team" /><label class="form-label" for="registration-notes">备注</label><textarea class="form-control" id="registration-notes" name="notes" maxlength="2000" placeholder="环境、用途或部署说明（可选）"></textarea></div><div class="drawer-footer"><button type="button" class="ghost-button" data-action="close">取消</button><button type="submit" class="primary-button">${svg('check')}保存登记</button></div></form>`);
  el<HTMLInputElement>('#registration-name').focus();
}
function detailMarkup(cert: Certificate): string {
  const type = certType(cert);
  const files = cert.files || [];
  const left = daysLeft(cert);
  const status = left === null ? '未知' : left < 0 ? '已过期' : '有效';
  return `<div class="drawer-header"><div><h2 id="drawer-title">证书详情</h2><p>签发记录与文件清单</p></div><button class="close-button" data-action="close" aria-label="关闭">${svg('close')}</button></div><div class="drawer-body drawer-detail"><div class="detail-hero"><i class="cert-mark ${type === 'client' ? 'client' : ''}">${svg(type === 'client' ? 'users' : 'shield')}</i><div><h3>${esc(cert.name)}</h3><p>${type === 'server' ? '服务器证书' : '客户端证书'} · ID #${esc(cert.id)}</p></div></div><div class="detail-grid"><div class="detail-item"><label>密钥类型</label><strong>${esc((cert.key_type || 'RSA').toUpperCase())}</strong></div><div class="detail-item"><label>状态</label><strong>${status}</strong></div><div class="detail-item"><label>签发时间</label><strong>${esc(dateTime(cert.created_at))}</strong></div><div class="detail-item"><label>到期时间</label><strong>${esc(dateTime(cert.expires_at))}</strong></div></div><div class="detail-block"><h4>SAN / 域名</h4><div class="san-list">${(cert.sans || []).length ? (cert.sans || []).map((san) => `<span class="san">${esc(san)}</span>`).join('') : '<span class="form-hint">无 SAN</span>'}</div></div><div class="detail-block"><h4>文件</h4><div class="file-list">${files.length ? files.map((file) => `<div class="file-item"><span>${esc(file)}</span><a href="${API}/certificates/${encodeURIComponent(certId(cert))}/files/${encodeURIComponent(file)}" download>下载</a></div>`).join('') : '<span class="form-hint">文件列表不可用</span>'}</div></div></div><div class="drawer-footer"><a class="primary-button" href="${API}/certificates/${encodeURIComponent(certId(cert))}/download" download>${svg('download')}下载 ZIP</a></div>`;
}

async function openDetail(cert: Certificate): Promise<void> {
  openDrawer(`<div class="drawer-header"><div><h2 id="drawer-title">证书详情</h2><p>正在读取签发记录</p></div><button class="close-button" data-action="close" aria-label="关闭">${svg('close')}</button></div><div class="drawer-body"><p class="form-hint">正在读取详情…</p></div>`);
  try {
    const detail = await request<Certificate>(`/certificates/${encodeURIComponent(certId(cert))}`);
    const drawer = document.querySelector<HTMLElement>('.overlay.open .drawer');
    if (!drawer) return;
    drawer.innerHTML = detailMarkup(detail);
    drawer.querySelector<HTMLElement>('.close-button')?.focus();
  } catch (error) {
    toast(`读取证书详情失败：${(error as Error).message}`, 'error');
  }
}
function openCAInit(): void {
  openDrawer(`<div class="drawer-header"><div><h2 id="drawer-title">初始化根 CA</h2><p>生成或更新 RSA / EC 根证书</p></div><button class="close-button" data-action="close" aria-label="关闭">${svg('close')}</button></div><form id="ca-form" class="drawer-body"><div class="form-section"><h3 class="form-section-title">根 CA 密码</h3><label class="form-label" for="ca-rootpass">密码</label><input class="form-control" id="ca-rootpass" name="rootpass" type="password" autocomplete="new-password" placeholder="请输入根 CA 密码" /><p class="form-hint">服务端未配置 ROOTPASS 时必须填写。重复初始化不会覆盖现有根证书。</p></div><div class="drawer-footer"><button type="button" class="ghost-button" data-action="close">取消</button><button type="submit" class="primary-button">${svg('shield')}初始化 CA</button></div></form>`);
  el<HTMLInputElement>('#ca-rootpass').focus();
}

async function handleSubmit(form: HTMLFormElement): Promise<void> {
  const data = new FormData(form); const submit = form.querySelector<HTMLButtonElement>('button[type="submit"]'); if (submit) submit.disabled = true;
  try {
    if (form.id === 'issue-form') {
      const type = String(data.get('type') || 'server') as CertType; const sans = String(data.get('sans') || '').split(/[\n,\s]+/).map((value) => value.trim()).filter(Boolean); const payload = { type, name: String(data.get('name') || '').trim(), sans, days: Number(data.get('days') || 398), key_type: String(data.get('key_type') || 'both'), rootpass: String(data.get('rootpass') || '') || undefined }; await request<Certificate>('/certificates', { method: 'POST', body: JSON.stringify(payload) }); closeDrawer(); toast('证书已生成并登记', 'ok'); await loadData();
    } else if (form.id === 'registration-form') { await request<Registration>('/registrations', { method: 'POST', body: JSON.stringify({ name: String(data.get('name') || '').trim(), owner: String(data.get('owner') || '').trim(), notes: String(data.get('notes') || '').trim() }) }); closeDrawer(); toast('应用登记已保存', 'ok'); await loadData();
    } else { await request<CAStatus>('/ca/initialize', { method: 'POST', body: JSON.stringify({ rootpass: String(data.get('rootpass') || '') }) }); closeDrawer(); toast('根 CA 初始化完成', 'ok'); await loadData(); }
  } catch (error) { toast(`操作失败：${(error as Error).message}`, 'error'); if (submit) submit.disabled = false; }
}

function navigate(button: HTMLButtonElement): void {
  const key = button.dataset.nav;
  all<HTMLButtonElement>('[data-nav]').forEach((item) => {
    const active = item === button;
    item.classList.toggle('active', active);
    item.setAttribute('aria-current', active ? 'page' : 'false');
  });
  const targetId = key === 'certificates' ? 'certificates-section' : key === 'applications' ? 'applications-section' : 'dashboard-section';
  document.getElementById(targetId)?.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function bindEvents(): void {
  document.addEventListener('click', (event) => {
    const target = event.target as HTMLElement;
    const action = target.closest<HTMLElement>('[data-action]')?.dataset.action;
    if (action === 'generate') openGenerate();
    else if (action === 'register') openRegistration();
    else if (action === 'init-ca') openCAInit();
    else if (action === 'close') closeDrawer();
    else if (action === 'detail') {
      const row = target.closest<HTMLElement>('[data-id]');
      const cert = state.certificates.find((item) => String(item.id) === row?.dataset.id);
      if (cert) openDetail(cert);
    } else if (target.closest('#refresh-button, #table-refresh')) {
      void loadData();
      toast('正在刷新数据…');
    } else {
      const filter = target.closest<HTMLButtonElement>('[data-filter]');
      const nav = target.closest<HTMLButtonElement>('[data-nav]');
      if (filter) {
        state.filter = (filter.dataset.filter || 'all') as Filter;
        all<HTMLButtonElement>('[data-filter]').forEach((item) => {
          const active = item === filter;
          item.classList.toggle('active', active);
          item.setAttribute('aria-selected', String(active));
        });
        renderCerts();
      } else if (nav) navigate(nav);
    }
  });
  document.addEventListener('input', (event) => { if ((event.target as HTMLElement).id === 'search-input') { state.query = (event.target as HTMLInputElement).value.trim(); renderCerts(); } });
  document.addEventListener('change', (event) => {
    const target = event.target as HTMLInputElement;
    if (target.name === 'type' && target.form?.id === 'issue-form') {
      const sans = el<HTMLTextAreaElement>('#issue-sans');
      const client = target.value === 'client';
      sans.disabled = client;
      if (client) sans.value = '';
    }
  });
  document.addEventListener('submit', (event) => { const form = event.target as HTMLFormElement; if (['issue-form', 'registration-form', 'ca-form'].includes(form.id)) { event.preventDefault(); void handleSubmit(form); } });
  el<HTMLElement>('#overlay').addEventListener('click', (event) => { if (event.target === el<HTMLElement>('#overlay')) closeDrawer(); });
  document.addEventListener('keydown', (event) => { if (event.key === 'Escape') closeDrawer(); });
}

function mount(): void { el<HTMLElement>('#app').innerHTML = shell(); bindEvents(); void loadData(); }
mount();
