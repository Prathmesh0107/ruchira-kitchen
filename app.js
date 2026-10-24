const app = document.getElementById('app');

// Bluetooth printing lives in bluetooth.js. If that file is missing, the rest of the app keeps working.
if (!window.DKBT) {
  const missing = async () => { throw new Error('bluetooth.js is missing from the frontend folder.'); };
  window.DKBT = { supported: () => false, secure: () => true, status: () => 'unsupported', preferred: () => false, optedOut: () => true, savedName: () => '', name: () => '', connected: () => false, info: () => '', onChange() {}, explain: e => String((e && e.message) || e), connect: missing, ensure: missing, disconnect() {}, printOrder: missing, printOrders: missing, printSummary: missing, printTest: missing };
}
let state = {
  page: 'loading', user: null, loginError: '', loginUser: '', users: [], invItems: [], invView: 'month', invPurchases: null, orders: [], menu: [], categories: [], settings: null, reports: null, newOrder: {category: null, cart: {}},
  selectedMonth: new Date().toISOString().slice(0, 7),
  inventory: { month: new Date().toISOString().slice(0, 7), total_spend: 0, purchases: [], by_item: [] },
  printers: { printers: [], ports: [] }, printerSetup: { step: 1, test: null }, search: '', inventorySearch: '', orderStatus: '', paymentFilter: '', selectedOrder: null
};

const money = n => new Intl.NumberFormat('en-IN', { style: 'currency', currency: 'INR', maximumFractionDigits: 0 }).format(n || 0);
const num = n => new Intl.NumberFormat('en-IN', { maximumFractionDigits: 2 }).format(n || 0);
const esc = s => String(s ?? '').replace(/[&<>'"]/g, c => ({ '&':'&amp;', '<':'&lt;', '>':'&gt;', "'":'&#039;', '"':'&quot;' }[c]));
function storedBiz() { try { return localStorage.getItem('dk_biz') || ''; } catch (e) { return ''; } }
function bizName(s) {
  const n = String((s || state.settings || {}).business_name || storedBiz() || '').trim();
  return n || 'Demo Kitchen';
}
function bizInitials(name) {
  const parts = String(name || '').trim().split(/\s+/).filter(Boolean);
  if (!parts.length) return 'DK';
  const a = parts[0][0] || '';
  const b = (parts[1] ? parts[1][0] : parts[0][1]) || '';
  return (a + b).toUpperCase();
}
function rememberBiz(name) {
  const n = String(name || '').trim();
  if (n) { try { localStorage.setItem('dk_biz', n); } catch (e) { /* private mode */ } document.title = n; }
  else { const stored = storedBiz(); if (stored) document.title = stored; }
}
const fmtTime = iso => new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
const fmtDate = iso => new Date(iso).toLocaleDateString('en-IN', { day: '2-digit', month: 'short', year: 'numeric' });
// Name and phone are optional when billing. Show a friendly label when the name was left blank.
const custName = o => (o && String(o.customer_name || '').trim()) || 'Walk-in';
function customerBlockHtml(o) {
  const name = String(o.customer_name || '').trim(), phone = String(o.phone || '').trim(), addr = String(o.address || '').trim();
  if (!name && !phone && !addr) return '';
  return `${name ? `<div>Customer: ${esc(name)}</div>` : ''}${phone ? `<div>Phone: ${esc(phone)}</div>` : ''}${addr ? `<div>Address: ${esc(addr)}</div>` : ''}<div class="line"></div>`;
}
const licenseHtml = s => (s && s.food_license ? `<br>FSSAI Lic: ${esc(s.food_license)}` : '');
const api = async (url, opts = {}) => {
  const r = await fetch(url, { headers: { 'Content-Type': 'application/json', ...(opts.headers || {}) }, ...opts });
  if (!r.ok) {
    const text = await r.text();
    let msg = text; try { const d = JSON.parse(text).detail; if (typeof d === 'string') msg = d; } catch (e) { /* plain text error */ }
    if (r.status === 401 && !/^\/api\/(login|me)(\?|$)/.test(url)) sessionExpired(); // logged out elsewhere or session expired
    const err = new Error(msg || 'Request failed'); err.status = r.status; throw err;
  }
  return r.json();
};

async function loadPeriodData() {
  const month = state.selectedMonth || new Date().toISOString().slice(0, 7);
  state.inventory.month = month;
  [state.orders, state.reports, state.inventory] = await Promise.all([
    api('/api/orders?month=' + encodeURIComponent(month)),
    api('/api/reports?month=' + encodeURIComponent(month)),
    api('/api/inventory?month=' + encodeURIComponent(month) + (state.inventorySearch ? '&q=' + encodeURIComponent(state.inventorySearch) : ''))
  ]);
}
async function load() {
  const month = state.selectedMonth || new Date().toISOString().slice(0, 7);
  state.inventory.month = month;
  await Promise.all([
    loadPeriodData(),
    (async()=>{ [state.menu, state.categories, state.settings] = await Promise.all([api('/api/menu-items'), api('/api/categories'), api('/api/settings')]); rememberBiz(state.settings && state.settings.business_name); })()
  ]);
  render();
}
async function loadInventory() {
  state.selectedMonth = state.inventory.month || state.selectedMonth;
  await loadPeriodData();
  render();
}
async function changeMonth(month) {
  if (!/^\d{4}-\d{2}$/.test(month)) return;
  state.selectedMonth = month;
  state.inventory.month = month;
  state.inventorySearch = '';
  await loadPeriodData();
  render();
}
async function loadPrinters() {
  state.printers = await api('/api/printers');
  render();
}
function nav(page) {
  state.page = page; state.selectedOrder = null; render(); window.scrollTo(0, 0);
  if (page === 'printers') loadPrinters().catch(() => {});
  if (page === 'inventory') { state.inventorySearch = ''; state.invPurchases = null; Promise.all([loadPeriodData(), loadInvItems()]).then(() => { if (state.page === 'inventory') render(); }).catch(() => {}); }
  if (page === 'settings' && isOwner()) loadUsers().then(() => { if (state.page === 'settings') render(); }).catch(() => {});
  if (page === 'reports') loadPeriodData().then(() => { if (state.page === 'reports') render(); }).catch(() => {});
}
function statusClass(s) { return String(s).toLowerCase().replace(/\s/g, '-'); }
const PAY_METHODS = ['Cash', 'UPI'];
function paymentMethod(o) {
  const m = String((o && (o.payment_method || o.payment_status)) || '').trim().toUpperCase();
  return m === 'UPI' ? 'UPI' : 'Cash';
}
function paySelectHtml(selected) {
  const cur = paymentMethod({ payment_method: selected });
  return `<label>Payment<select name="payment_method">${PAY_METHODS.map(m => `<option ${cur === m ? 'selected' : ''}>${m}</option>`).join('')}</select></label>`;
}
function ticketPrintCss() {
  return `@page{size:58mm auto;margin:0}
html,body{width:58mm;margin:0;padding:8px;background:#fff!important;color:#000!important;font-family:"Courier New",Courier,monospace;font-size:14px;font-weight:800;line-height:1.4;-webkit-print-color-adjust:exact!important;print-color-adjust:exact!important;color-adjust:exact!important;forced-color-adjust:none}
*{color:#000!important;background:transparent!important;-webkit-print-color-adjust:exact!important;print-color-adjust:exact!important}
.center{text-align:center;font-weight:900}
.biz{font-size:16px;font-weight:900}
.row{display:flex;justify-content:space-between;gap:6px;font-weight:800}
.line{border-top:2px dashed #000;margin:8px 0}
.total{font-weight:900;font-size:17px}
.note{white-space:pre-wrap;font-weight:800}
.thanks{text-align:center;margin-top:10px;font-weight:900;font-size:15px}
.b{font-weight:900;margin-top:6px}
.items{padding-left:6px;font-weight:800}
@media print{html,body{color:#000!important;background:#fff!important;-webkit-print-color-adjust:exact!important;print-color-adjust:exact!important}*{color:#000!important}}`;
}

function orderCard(o) {
  return `<article class="order-card">
    <div class="order-top"><span class="order-num">#${o.order_number}</span><span class="pill ${statusClass(paymentMethod(o))}">${esc(paymentMethod(o))}</span></div>
    <div class="customer"><div class="avatar">${esc(custName(o)[0]).toUpperCase()}</div><div><b>${esc(custName(o))}</b><div class="muted">${esc(o.phone || '')}</div></div></div>
    <div class="order-meta"><span>${esc(o.order_type)}</span><span>${fmtTime(o.created_at)}</span></div>
    <div class="items">${o.items.slice(0, 3).map(i => `<div><span>${i.quantity}× ${esc(i.item_name)}</span><strong>${money(i.unit_price * i.quantity)}</strong></div>`).join('')}${o.items.length > 3 ? '<div class="muted">+ more items</div>' : ''}</div>
    ${o.special_instructions ? `<div class="note">${esc(o.special_instructions)}</div>` : ''}
    <div class="order-foot"><strong>${money(o.total)}</strong><div class="actions"><button class="btn btn-small" onclick="showOrder(${o.id})">View</button><button class="btn btn-small" onclick="editOrder(${o.id})">Edit</button><button class="btn btn-small btn-primary" onclick="printOrder(${o.id})">Print</button></div></div>
  </article>`;
}

function printerChipHtml() {
  return `<div class="printer ${printerChipClass()}" onclick="printerChipClick()" title="Printer connection"><span class="dot"></span>${printerDisplay()} </div>`;
}

function render() {
  rememberBiz((state.settings && state.settings.business_name) || storedBiz());
  if (state.page === 'loading') { app.innerHTML = splashHtml(); return; }
  if (!state.user) state.page = 'login';
  if (state.page === 'login') { app.innerHTML = loginPage(); focusLogin(); return; }
  const counts = { New:0, Accepted:0, Preparing:0, Ready:0, Completed:0, Cancelled:0 };
  state.orders.forEach(o => counts[o.status] = (counts[o.status] || 0) + 1);
  if (state.page === 'home') { app.innerHTML = homePage(); tick(); return; }
  const pending = counts.New + counts.Accepted + counts.Preparing + counts.Ready;
  app.innerHTML = `<div class="shell no-nav"><main class="main"><header class="topbar">
      <div class="top-left"><button class="menu-back" onclick="nav('home')">← Main Menu</button><h1>${pageTitle(state.page)}</h1></div>
      <div class="top-actions">${printerChipHtml()}<div class="clock" id="clock"></div></div>
    </header><section class="content">${pageView(pending, counts)}</section></main></div>`;
  tick();
}

// ---------- Main menu (first page) ----------
const isToday = iso => { const d = new Date(iso), n = new Date(); return d.getFullYear() === n.getFullYear() && d.getMonth() === n.getMonth() && d.getDate() === n.getDate(); };
function currentMonthKey() { const n = new Date(); return n.getFullYear() + '-' + String(n.getMonth() + 1).padStart(2, '0'); }

function homePage() {
  const s = state.settings || {};
  const name = bizName(s);
  const live = state.orders.filter(o => ['New', 'Accepted', 'Preparing', 'Ready'].includes(o.status)).length;
  const todayCount = state.orders.filter(o => o.status !== 'Cancelled' && isToday(o.created_at)).length;
  const printerSub = btMode() ? (DKBT.connected() ? 'Connected · ' + DKBT.name() : 'Not connected yet') : 'Connect your printer';
  const tiles = [
    ['dashboard', '🏠', 'Dashboard', 'Take orders and run the kitchen', live ? live + ' active' : ''],
    ['orders', '🧾', 'Orders', "All orders · print today's total", todayCount ? todayCount + ' today' : ''],
    ['menu', '📖', 'Menu', 'Items and prices', ''],
    ['inventory', '📦', 'Inventory', 'Raw material purchases', ''],
    ['reports', '📊', 'Reports', 'Sales and insights', ''],
    ['printers', '🖨️', 'Printer Setup', printerSub, ''],
    ['settings', '⚙️', 'Settings', 'Business details and logins', '']
  ].filter(t => t[0] !== 'settings' || isOwner());
  return `<div class="home"><header class="home-head"><div class="home-brand"><div class="mark">${esc(bizInitials(name))}</div><div><strong>${esc(name)}</strong><span>Main Menu</span></div></div><div class="top-actions">${printerChipHtml()}</div></header>
    ${defaultPasswordBanner()}<div class="home-list">${tiles.map(([p, ico, title, sub, badge], i) => `<button class="home-tile ${i === 0 ? 'primary' : ''}" onclick="nav('${p}')"><span class="home-ico">${ico}</span><span class="home-text"><b>${title}</b><small>${esc(sub)}</small></span>${badge ? `<span class="home-badge">${badge}</span>` : ''}<span class="home-go">›</span></button>`).join('')}</div>${accountBarHtml()}</div>`;
}

function pageLabel(p) { return p === 'dashboard' ? 'Overview' : 'Management'; }
function pageTitle(p) { return ({dashboard:'Kitchen Dashboard', orders:'Order History', menu:'Menu Management', inventory:'Inventory Management', reports:'Reports & Insights', printers:'Bluetooth Printer Setup', settings:'Settings', account:'My Account'}[p] || 'Demo Kitchen'); }
function icon(p) { return ({dashboard:'⌂', orders:'▤', menu:'▦', inventory:'◈', reports:'◔', printers:'▣', settings:'⚙'}[p] || '•'); }
function printerDisplay() {
  const s = state.settings || {};
  if (btMode()) return DKBT.connected()
    ? `<b>${esc(DKBT.name())}</b><span class="connected">Bluetooth</span>`
    : `<b>${esc(DKBT.savedName() || 'No printer')}</b><span class="connected off">Tap to connect</span>`;
  if ((s.printer_mode === 'windows' || s.printer_mode === 'serial') && s.printer_target) return `<b>${esc(s.printer_target)}</b><span class="connected">Ready</span>`;
  return `<b>Browser Print</b><span class="connected">Fallback</span>`;
}
function tick() { const el = document.getElementById('clock'); if (el) el.textContent = new Date().toLocaleString([], { day:'2-digit', month:'short', hour:'2-digit', minute:'2-digit' }); }
setInterval(tick, 30000);

function pageView(pending, counts) {
  if (state.page === 'dashboard') return dashboard(pending, counts);
  if (state.page === 'orders') return ordersPage();
  if (state.page === 'menu') return menuPage();
  if (state.page === 'inventory') return inventoryPage();
  if (state.page === 'reports') return reportsPage();
  if (state.page === 'printers') return printersPage();
  if (state.page === 'account') return accountPage();
  return isOwner() ? settingsPage() : ownerOnlyHtml();
}

function monthPicker(extraClass = '') {
  return `<label class="period-picker ${extraClass}"><span>Month</span><input class="month-input" type="month" value="${esc(state.selectedMonth)}" onchange="changeMonth(this.value)"></label>`;
}

const KITCHEN_COLS = [
  { key: 'new', title: 'New', hint: 'Waiting to start', statuses: ['New', 'Accepted'], empty: 'No new orders' },
  { key: 'cooking', title: 'Cooking', hint: 'In the kitchen', statuses: ['Preparing'], empty: 'Nothing cooking' },
  { key: 'ready', title: 'Ready', hint: 'Give to customer', statuses: ['Ready'], empty: 'Nothing ready' },
  { key: 'done', title: 'Done', hint: 'Latest finished orders', statuses: ['Completed'], empty: 'No finished orders yet' }
];
const NEXT_STEP = { New: ['Preparing', '▶ Start Cooking'], Accepted: ['Preparing', '▶ Start Cooking'], Preparing: ['Ready', '✔ Mark Ready'], Ready: ['Completed', '✔ Order Done'] };

function kitchenCard(o) {
  const next = NEXT_STEP[o.status];
  return `<article class="kcard ${statusClass(o.status)}">
    <div class="kcard-top"><span class="kcard-num">#${o.order_number}</span><span class="kcard-time">${fmtTime(o.created_at)}</span></div>
    <div class="kcard-who"><b>${esc(custName(o))}</b><span>${esc(o.order_type)}${o.phone ? ' · ' + esc(o.phone) : ''}</span></div>
    <ul class="kcard-items">${(o.items || []).map(i => `<li>${i.quantity} × ${esc(i.item_name)}</li>`).join('')}</ul>
    ${o.special_instructions ? `<div class="kcard-note">📝 ${esc(o.special_instructions)}</div>` : ''}
    <div class="kcard-total"><span class="pill ${statusClass(paymentMethod(o))}">${esc(paymentMethod(o))}</span><strong>${money(o.total)}</strong></div>
    ${next ? `<button class="kbtn-next" onclick="advanceOrder(${o.id},'${next[0]}',this)">${next[1]}</button>` : '<div class="kdone">✔ Completed</div>'}
    <div class="kcard-actions"><button class="btn" onclick="printOrder(${o.id})">🖨 Print</button><button class="btn" onclick="editOrder(${o.id})">✏️ Edit</button><button class="btn btn-danger" onclick="deleteOrder(${o.id})">🗑 Delete</button></div>
  </article>`;
}

function dashboard(pending, counts) {
  const month = labelMonth(state.selectedMonth);
  const today = state.orders.filter(o => o.status !== 'Cancelled' && isToday(o.created_at));
  const todaySales = today.reduce((a, o) => a + Number(o.total || 0), 0);
  const toCook = (counts.New || 0) + (counts.Accepted || 0) + (counts.Preparing || 0);
  const newestFirst = (x, y) => (new Date(y.created_at) - new Date(x.created_at)) || (y.id - x.id); // newest order on top
  const board = KITCHEN_COLS.map(c => {
    const all = state.orders.filter(o => c.statuses.includes(o.status));
    const sorted = all.slice().sort(newestFirst);
    const list = c.key === 'done' ? sorted.slice(0, 10) : sorted;
    return `<section class="kcol ${c.key}"><div class="kcol-head"><div><h3>${c.title}</h3><span>${c.hint}</span></div><span class="count">${all.length}</span></div><div class="kcol-body">${list.map(kitchenCard).join('') || `<div class="empty">${c.empty}</div>`}</div>${c.key === 'done' && all.length > 10 ? `<button class="btn kcol-more" onclick="nav('orders')">See all ${all.length} orders</button>` : ''}</section>`;
  }).join('');
  return `<div class="kitchen-top"><button class="big-new-order" onclick="openNewOrder()"><span>＋</span> New Order</button><div class="kitchen-month">${monthPicker()}</div></div>
    <div class="today-stats"><div class="tstat"><span>Today's Orders</span><b>${today.length}</b></div><div class="tstat"><span>Today's Sales</span><b>${money(todaySales)}</b></div><div class="tstat warn"><span>To Cook</span><b>${toCook}</b></div><div class="tstat good"><span>Ready</span><b>${counts.Ready || 0}</b></div></div>
    ${state.orders.length ? `<p class="kitchen-hint">Showing orders for ${month}. Newest orders are on top. Tap the big button on an order to move it to the next step.</p>` : `<div class="kitchen-empty"><b>No orders yet</b><span>Tap the green <b>＋ New Order</b> button to take the first one.</span></div>`}
    <div class="kboard">${board}</div>`;
}

async function advanceOrder(id, status, btn) {
  if (btn) { btn.disabled = true; btn.textContent = 'Please wait…'; }
  try {
    const updated = await api('/api/orders/' + id, { method: 'PUT', body: JSON.stringify({ status }) });
    const idx = state.orders.findIndex(x => x.id === id);
    if (idx >= 0) state.orders[idx] = updated;
  } catch (err) { alert(err.message || 'Could not update the order'); }
  render();
}

async function deleteOrder(id) {
  const o = (state.orders || []).find(x => x.id === id);
  const label = o ? '#' + o.order_number + ' (' + custName(o) + ', ' + money(o.total) + ')' : '#' + id;
  if (!confirm('Delete order ' + label + '?\n\nThis removes it completely and cannot be undone.')) return;
  try {
    await api('/api/orders/' + id, { method: 'DELETE' });
    state.orders = state.orders.filter(x => x.id !== id);
    state.selectedOrder = null;
    closeModal();
    render();
    toast('Order deleted', 'ok');
    api('/api/reports?month=' + encodeURIComponent(state.selectedMonth)).then(r => { state.reports = r; }).catch(() => {});
  } catch (err) { alert(err.message || 'Could not delete the order'); }
}

// ---------- Today's total (one slip with every order of today) ----------
function summarizeToday(orders) {
  const todays = orders.filter(o => isToday(o.created_at));
  const counted = todays.filter(o => o.status !== 'Cancelled').sort((a, b) => new Date(a.created_at) - new Date(b.created_at));
  const sum = list => list.reduce((a, o) => a + Number(o.total || 0), 0);
  const total = sum(counted);
  const paid = sum(counted.filter(o => o.payment_status === 'Paid'));
  const items = counted.reduce((a, o) => a + (o.items || []).reduce((x, i) => x + Number(i.quantity || 0), 0), 0);
  return { orders: counted, cancelled: todays.length - counted.length, total, paid, pending: total - paid, items };
}
function todayBannerHtml() {
  const cur = state.selectedMonth === currentMonthKey();
  const t = cur ? summarizeToday(state.orders) : null;
  const info = t ? t.orders.length + ' order' + (t.orders.length === 1 ? '' : 's') + (t.cancelled ? ' · ' + t.cancelled + ' cancelled (not counted)' : '') : "Switch to this month to see today's numbers";
  return `<div class="today-banner"><div><b>Today</b><span>${info}</span></div>${t ? `<strong>${money(t.total)}</strong>` : ''}<button class="btn btn-primary" onclick="printTodaySummary()">🖨 Print Today's Total</button></div>`;
}
function summaryHtml(t) {
  const s = state.settings || {};
  const rows = t.orders.map(o => `<div class="row b"><span>#${o.order_number} ${fmtTime(o.created_at)}</span><span>${money(o.total)}</span></div><div class="items">${esc((o.items || []).map(i => i.quantity + 'x ' + i.item_name).join(', '))}</div>`).join('');
  return `<html><head><title>Today's orders</title><style>${ticketPrintCss()}</style></head><body><div class="center"><b>${esc(bizName(s))}</b>${licenseHtml(s)}<br>TODAY'S ORDERS<br>${fmtDate(new Date())}</div><div class="line"></div>${rows}<div class="line"></div><div class="row"><span>Total orders</span><span>${t.orders.length}</span></div><div class="row"><span>Items sold</span><span>${t.items}</span></div>${t.pending > 0 ? `<div class="row"><span>Paid</span><span>${money(t.paid)}</span></div><div class="row"><span>Payment pending</span><span>${money(t.pending)}</span></div>` : ''}${t.cancelled ? `<div class="row"><span>Cancelled (not counted)</span><span>${t.cancelled}</span></div>` : ''}<div class="row total"><span>TOTAL</span><span>${money(t.total)}</span></div><div class="line"></div><div class="center">END OF REPORT</div><script>window.onload=()=>setTimeout(()=>window.print(),250)</script></body></html>`;
}
async function loadTodayOrders() {
  const rows = state.selectedMonth === currentMonthKey() ? state.orders : await api('/api/orders?month=' + currentMonthKey());
  return summarizeToday(rows);
}
async function printTodaySummary() {
  if (btMode()) {
    return printViaBluetooth(async () => {
      const t = await loadTodayOrders();
      if (!t.orders.length) { alert('There are no orders today to print.'); return false; }
      await DKBT.printSummary(t, state.settings);
    }, "Today's total sent to " + DKBT.name());
  }
  const win = openPrintShell();
  try {
    const t = await loadTodayOrders();
    if (!t.orders.length) { if (win) win.close(); return alert('There are no orders today to print.'); }
    if (!win) return alert("Allow popups to print today's total.");
    win.document.open(); win.document.write(summaryHtml(t)); win.document.close();
  } catch (err) { if (win) win.close(); alert(err.message || "Could not print today's total."); }
}

// ---------- Start again from zero orders ----------
async function clearAllOrders() {
  const typed = prompt('This deletes ALL orders and cannot be undone.\n(Menu, inventory and settings are kept.)\n\nType RESET to confirm:');
  if (typed === null) return;
  if (String(typed).trim().toUpperCase() !== 'RESET') return alert('Nothing was deleted. You need to type RESET.');
  try {
    await api('/api/orders/clear', { method: 'POST', body: JSON.stringify({ confirm: 'RESET' }) });
    state.orders = [];
    state.selectedOrder = null;
    await load();
    toast('All orders cleared. Starting from zero.', 'ok');
  } catch (err) { alert(err.message || 'Could not clear the orders'); }
}

function filteredOrders() {
  const q = String(state.search || '').toLowerCase();
  return state.orders.filter(o =>
    (!state.orderStatus || o.status === state.orderStatus) &&
    (!q || String(o.customer_name + ' ' + o.phone + ' ' + o.order_number).toLowerCase().includes(q)) &&
    (!state.paymentFilter || paymentMethod(o) === state.paymentFilter)
  );
}

function ordersPage() {
  const shownCount = filteredOrders().length;
  const month = labelMonth(state.selectedMonth);
  return `<div class="toolbar"><div><span class="muted">${state.orders.length} orders in ${month}</span><div class="subtitle">Past months remain saved. Search, inspect, edit and print tickets.</div></div><div class="mini-actions">${monthPicker()}<button class="btn" onclick="printAllOrders()">Print each ticket</button><button class="btn btn-primary" onclick="openNewOrder()">＋ New Order</button></div></div>
  <div class="filters"><input placeholder="Search order number, customer or phone" value="${esc(state.search)}" oninput="state.search=this.value;filterOrders()"><select onchange="state.orderStatus=this.value;filterOrders()"><option value="">All statuses</option>${['New','Accepted','Preparing','Ready','Completed','Cancelled'].map(s => `<option ${state.orderStatus===s?'selected':''}>${s}</option>`).join('')}</select><select id="payment-filter" onchange="state.paymentFilter=this.value;filterOrders()"><option value="">All payments</option>${PAY_METHODS.map(m => `<option ${state.paymentFilter===m?'selected':''}>${m}</option>`).join('')}</select></div>
  ${todayBannerHtml()}<div class="print-all-hint">${shownCount ? `"Print each ticket" prints one ticket per order matching the current filters (${shownCount} match).` : 'No orders match the current filters.'}</div>
  <div class="history-table"><div class="table-head"><span>Order</span><span>Customer</span><span>Status</span><span>Total</span><span>Actions</span></div>${state.orders.map(o => `<div class="table-row" data-id="${o.id}" data-status="${o.status}" data-payment="${paymentMethod(o)}" data-q="${esc((o.customer_name+' '+o.phone+' '+o.order_number).toLowerCase())}"><span><b>#${o.order_number}</b><small>${fmtTime(o.created_at)}</small></span><span><b>${esc(custName(o))}</b><small>${esc(o.order_type)}</small></span><span><span class="status-dot ${statusClass(o.status)}"></span>${o.status}</span><span><b>${money(o.total)}</b><small>${paymentMethod(o)}</small></span><span class="actions"><button class="btn btn-small" onclick="showOrder(${o.id})">View</button><button class="btn btn-small" onclick="editOrder(${o.id})">Edit</button><button class="btn btn-small btn-primary" onclick="printOrder(${o.id})">Print</button><button class="btn btn-small btn-danger" onclick="deleteOrder(${o.id})">Delete</button></span></div>`).join('') || '<div class="empty">No orders found.</div>'}</div>`;
}
function filterOrders() {
  const q = String(state.search || '').toLowerCase();
  const status = state.orderStatus || '';
  const pay = state.paymentFilter || '';
  document.querySelectorAll('.table-row[data-status]').forEach(r => {
    const match = (!status || r.dataset.status === status) && (!q || r.dataset.q.includes(q)) && (!pay || r.dataset.payment === pay);
    r.style.display = match ? 'grid' : 'none';
  });
}

function menuPage() {
  const cats = state.categories || [];
  const totalItems = state.menu.length;
  const categoryCards = cats.map((c, idx) => {
    const rows = state.menu.filter(i => i.category === c.name);
    const available = rows.filter(i => i.available).length;
    return `<div class="category-admin-card">
      <div class="category-admin-main">
        ${c.icon ? `<span class="category-admin-icon">${esc(c.icon)}</span>` : '<span class="category-admin-icon empty">•</span>'}
        <div><h3>${esc(c.name)}</h3><p>${rows.length} item${rows.length===1?'':'s'} · ${available} available</p></div>
      </div>
      <div class="category-order-tools">
        <button class="icon-btn" title="Move category up" onclick="reorderCategory(${c.id},-1)" ${idx===0?'disabled':''}>↑</button>
        <button class="icon-btn" title="Move category down" onclick="reorderCategory(${c.id},1)" ${idx===cats.length-1?'disabled':''}>↓</button>
      </div>
      <div class="menu-actions category-admin-actions">
        <button class="btn btn-small" onclick='openCategoryModal(${JSON.stringify(c).replace(/'/g,"&#39;")})'>Edit</button>
        <button class="btn btn-small btn-primary" onclick='openMenuModal(null, ${jsArg(c.name)})'>＋ Add Item</button>
        <button class="icon-btn danger" title="Delete category" onclick="deleteCategory(${c.id})">×</button>
      </div>
    </div>`;
  }).join('');

  const groups = cats.map(c => {
    const rows = state.menu.filter(i => i.category === c.name);
    return `<div class="menu-group">
      <div class="section-head tight menu-group-head">
        <div class="menu-group-title">
          ${c.icon ? `<span class="menu-group-icon">${esc(c.icon)}</span>` : ''}
          <div><h2>${esc(c.name)}</h2><p>${rows.length} item${rows.length===1?'':'s'} · ${rows.filter(i=>i.available).length} available · ${rows.filter(i=>!i.available).length} hidden</p></div>
        </div>
        <div class="mini-actions">
          <button class="btn btn-small" onclick='openCategoryModal(${JSON.stringify(c).replace(/'/g,"&#39;")})'>Edit category</button>
          <button class="btn btn-small btn-primary" onclick='openMenuModal(null, ${jsArg(c.name)})'>＋ Add dish</button>
        </div>
      </div>
      ${rows.length ? `<div class="menu-grid">${rows.map(i => `<div class="menu-card ${i.available?'':'disabled'}">
        <div><span class="category-chip">${i.available?'Available':'Hidden'}</span><h3>${esc(i.name)}</h3><b>${money(i.price)}</b></div>
        <div class="menu-actions">
          <button class="availability-toggle ${i.available?'on':'off'}" onclick="toggleMenu(${i.id},${i.available?'false':'true'})">${i.available?'✓ Available':'○ Hidden'}</button>
          <button class="btn btn-small" onclick='openMenuModal(${JSON.stringify(i).replace(/'/g,"&#39;")})'>Edit</button>
          <button class="icon-btn danger" title="Delete item" onclick="deleteMenu(${i.id})">×</button>
        </div>
      </div>`).join('')}</div>` : `<div class="menu-empty-card"><div><b>No dishes in ${esc(c.name)} yet.</b><span>Add the first dish to this category.</span></div><button class="btn btn-primary btn-small" onclick='openMenuModal(null, ${jsArg(c.name)})'>＋ Add dish</button></div>`}
    </div>`;
  }).join('');

  const uncategorized = state.menu.filter(i => !cats.some(c => c.name === i.category));
  const uncategorizedBlock = uncategorized.length ? `<div class="menu-group">
    <div class="section-head tight menu-group-head"><div><div class="menu-group-title"><div><h2>Other</h2><p>${uncategorized.length} item${uncategorized.length===1?'':'s'} not assigned to a category</p></div></div></div><button class="btn btn-small btn-primary" onclick="openCategoryModal()">＋ Create category</button></div>
    <div class="menu-grid">${uncategorized.map(i => `<div class="menu-card ${i.available?'':'disabled'}"><div><span class="category-chip">${i.available?'Available':'Hidden'}</span><h3>${esc(i.name)}</h3><b>${money(i.price)}</b></div><div class="menu-actions"><button class="availability-toggle ${i.available?'on':'off'}" onclick="toggleMenu(${i.id},${i.available?'false':'true'})">${i.available?'✓ Available':'○ Hidden'}</button><button class="btn btn-small" onclick='openMenuModal(${JSON.stringify(i).replace(/'/g,"&#39;")})'>Edit</button><button class="icon-btn danger" onclick="deleteMenu(${i.id})">×</button></div></div>`).join('')}</div>
  </div>` : '';

  return `<div class="toolbar"><div><span class="muted">${totalItems} menu items · ${cats.length} categories</span><div class="subtitle">Manage availability, category order, and dishes exactly how the restaurant wants to see them.</div></div><div class="mini-actions"><button class="btn" onclick="openCategoryModal()">＋ Category</button><button class="btn btn-primary" onclick="openMenuModal()">＋ Add Item</button></div></div>
    <div class="panel menu-structure-panel"><div class="section-head tight"><div><h2>Categories</h2><p>Use ↑ / ↓ to control the order shown in New Order. Icons are optional.</p></div><span class="muted">${cats.length} configured</span></div><div class="category-admin-grid">${categoryCards || '<div class="empty">No categories yet. Add one to start building the menu.</div>'}</div></div>
    ${groups}${uncategorizedBlock}`;
}

function openCategoryModal(category = null) {
  const itemCount = category ? state.menu.filter(i => i.category === category.name).length : 0;
  showModal(`<div class="modal-card"><div class="modal-head"><div><span class="muted">Menu structure</span><h2>${category ? 'Edit category' : 'Add category'}</h2></div><button class="icon-btn" onclick="closeModal()">×</button></div>
    <p class="muted">Use an icon only when it helps. Leave it empty for a simple text-only category.</p>
    <form id="category-form"><label>Category name<input name="name" required value="${esc(category?.name || '')}" placeholder="Paneer"></label><label>Icon / emoji (optional)<input name="icon" maxlength="4" value="${esc(category?.icon || '')}" placeholder="🍛"></label>${category ? `<div class="hint-box">${itemCount ? `${itemCount} menu item${itemCount===1?'':'s'} use this category. Renaming it will update those items automatically.` : 'This category has no menu items yet.'}</div>` : ''}<div class="modal-foot"><button class="btn" type="button" onclick="closeModal()">Cancel</button><button class="btn btn-primary">Save category</button></div></form></div>`);
  document.getElementById('category-form').onsubmit = async e => { e.preventDefault(); const f=new FormData(e.target); try { await api(category ? '/api/categories/'+category.id : '/api/categories',{method:category?'PUT':'POST',body:JSON.stringify({name:String(f.get('name')).trim(),icon:String(f.get('icon')||'').trim()})}); closeModal(); await load(); } catch(err){ alert(err.message || 'Could not save category'); } };
}
async function reorderCategory(id, direction) {
  const ids = (state.categories || []).map(c => c.id);
  const idx = ids.indexOf(id);
  const target = idx + direction;
  if (idx < 0 || target < 0 || target >= ids.length) return;
  [ids[idx], ids[target]] = [ids[target], ids[idx]];
  try { await api('/api/categories/reorder', {method:'POST', body:JSON.stringify({category_ids:ids})}); await load(); }
  catch (err) { alert(err.message || 'Could not reorder categories'); }
}
async function deleteCategory(id) { if (!confirm('Delete this category? Categories with menu items cannot be deleted until those items are moved or deleted.')) return; try { await api('/api/categories/'+id,{method:'DELETE'}); await load(); } catch(err){ alert(err.message || 'Could not delete category'); } }

function labelMonth(m) { if (!m) return 'This Month'; const [y, mo] = m.split('-'); return new Date(Number(y), Number(mo)-1, 1).toLocaleDateString('en-IN', { month:'short', year:'numeric' }); }

function reportsPage() {
  const r = state.reports || {}, inv = state.inventory || {}, top = r.most_ordered || [], month = labelMonth(state.selectedMonth);
  return `<div class="toolbar"><div><span class="muted">${month} reports</span><div class="subtitle">Historical months remain available; selecting a new month does not delete anything.</div></div><div class="mini-actions">${monthPicker()}</div></div>
  <div class="stats reports"><div class="stat"><span>${month} Revenue</span><b>${money(r.revenue ?? r.today_revenue)}</b><small>Completed orders</small></div><div class="stat"><span>${month} Orders</span><b>${r.orders || 0}</b><small>All saved orders</small></div><div class="stat"><span>Average Completed Order</span><b>${money(r.average_order)}</b><small>Selected month</small></div><div class="stat"><span>${month} Inventory</span><b>${money(inv.total_spend)}</b><small>Raw material spend</small></div></div>
  <div class="report-grid"><div class="panel"><div class="section-head tight"><div><h2>Most ordered items</h2><p>Based on orders in ${month}.</p></div></div>${top.map(([name,qty]) => `<div class="bar-row"><span>${esc(name)}</span><div class="bar"><i style="width:${Math.min(100, qty * 18)}%"></i></div><b>${qty}</b></div>`).join('') || '<div class="empty">No data</div>'}</div>
  <div class="panel"><div class="section-head tight"><div><h2>Inventory spend</h2><p>Purchases for ${month}.</p></div></div>${mergedSpend(inv).slice(0, 5).map(x => `<div class="metric-line"><span>${esc(x.item_name)}</span><b>${money(x.total)}</b></div>`).join('') || '<div class="empty">No purchases</div>'}<div class="metric-line"><span>Total for ${month}</span><b>${money(inv.total_spend)}</b></div></div></div>`;
}

// ---------- Bluetooth printing (Web Bluetooth) ----------
let btBusy = false;
let toastTimer = null;
function toast(msg, kind = '') {
  let t = document.getElementById('dk-toast');
  if (!t) { t = document.createElement('div'); t.id = 'dk-toast'; }
  document.body.appendChild(t); // re-append so it always sits above the modal layer
  t.textContent = msg;
  t.className = 'dk-toast show ' + kind;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove('show'), 2800);
}
function hideToast() { const t = document.getElementById('dk-toast'); if (t) t.classList.remove('show'); }
// Print over Bluetooth whenever this browser can, unless someone switched it off on this device
// or a Windows / COM printer is already configured (that setup keeps working as before).
function btMode() {
  if (!DKBT.supported() || DKBT.optedOut()) return false;
  if (DKBT.preferred()) return true;
  const s = state.settings || {};
  return !(['windows', 'serial'].includes(s.printer_mode) && s.printer_target);
}
function printerChipClass() { return btMode() && !DKBT.connected() ? 'bt-off' : ''; }
function refreshBtUi() {
  if (state.page === 'printers') return render();
  const chip = document.querySelector('.top-actions .printer');
  if (chip) { chip.className = 'printer ' + printerChipClass(); chip.innerHTML = '<span class="dot"></span>' + printerDisplay() + ' '; }
}
// Tapping the printer chip reconnects a Bluetooth printer, otherwise opens Printer Setup.
async function printerChipClick() {
  if (!btMode() || DKBT.connected()) return nav('printers');
  if (btBusy) return;
  btBusy = true;
  try { await DKBT.ensure(); toast('Connected to ' + DKBT.name(), 'ok'); }
  catch (err) { const m = DKBT.explain(err); if (m) alert(m); }
  finally { btBusy = false; refreshBtUi(); }
}
// Runs a print job over Bluetooth: connect if needed, send, show a small toast.
async function printViaBluetooth(task, doneMsg) {
  if (btBusy) return toast('Printer is busy, one moment…');
  btBusy = true;
  try {
    await DKBT.ensure();
    toast('Printing…');
    const r = await task();
    if (r === false) hideToast(); else toast(doneMsg || 'Sent to ' + DKBT.name(), 'ok');
  } catch (err) {
    hideToast();
    const m = DKBT.explain(err);
    if (m) alert(m);
  } finally { btBusy = false; refreshBtUi(); }
}
async function btConnectUi() {
  if (btBusy) return;
  btBusy = true;
  try {
    const name = await DKBT.connect();
    state.printerSetup.btTest = null;
    toast('Connected to ' + name, 'ok');
  } catch (err) {
    const m = DKBT.explain(err);
    if (m) alert(m);
  } finally { btBusy = false; refreshBtUi(); }
}
async function btTestUi() {
  if (btBusy) return toast('Printer is busy, one moment…');
  btBusy = true;
  try {
    await DKBT.ensure();
    await DKBT.printTest(state.settings);
    state.printerSetup.btTest = { ok: true, message: 'Test receipt sent to ' + DKBT.name() + '. If it printed a line of numbers 1234567890…, printing is ready.' };
  } catch (err) {
    const m = DKBT.explain(err);
    if (m) state.printerSetup.btTest = { ok: false, message: m };
  } finally { btBusy = false; refreshBtUi(); }
}
function btDisconnectUi() {
  DKBT.disconnect();
  state.printerSetup.btTest = null;
  toast('Bluetooth off, using normal browser printing');
}
function setPrinterTab(tab) {
  state.printerSetup.tab = tab;
  render();
  if (tab === 'windows') loadPrinters().catch(() => {});
}

function printersPage() {
  const ps = state.printerSetup || (state.printerSetup = { step: 1, test: null });
  const s = state.settings || {};
  const winConfigured = ['windows', 'serial'].includes(s.printer_mode) && s.printer_target;
  const tab = ps.tab || (!DKBT.preferred() && winConfigured ? 'windows' : 'bluetooth');
  const tabs = `<div class="setup-steps two"><button class="setup-step ${tab === 'bluetooth' ? 'active' : ''}" onclick="setPrinterTab('bluetooth')"><span>📱</span><b>Phone / Bluetooth</b></button><button class="setup-step ${tab === 'windows' ? 'active' : ''}" onclick="setPrinterTab('windows')"><span>🖥</span><b>Windows PC / COM port</b></button></div>`;
  return tabs + (tab === 'windows' ? windowsPrinterPage() : bluetoothPrinterPage());
}

function isIOS() {
  return /iPhone|iPad|iPod/i.test(navigator.userAgent || '') || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
}
async function copyAppLink() {
  const url = location.origin + '/';
  try { await navigator.clipboard.writeText(url); toast('Link copied. Open Bluefy and paste it.', 'ok'); }
  catch (e) { prompt('Copy this link, then open it in Bluefy:', url); }
}
function unsupportedGuide() {
  if (isIOS()) return `<div class="hint-box keep"><b>iPhone / iPad can't print over Bluetooth from Safari or Chrome.</b> Apple blocks it for every website, so the Connect button stays grey here and Print uses the normal iPhone print sheet. To print straight to the Bluetooth printer, open this app in the <b>Bluefy</b> browser instead:</div>
    <div class="instruction-list keep">
      <div class="instruction"><span>1</span><div><b>Install Bluefy</b><small>Search "Bluefy" (Web BLE Browser) in the App Store.</small></div></div>
      <div class="instruction"><span>2</span><div><b>Copy this app's link</b><small>Tap the button below, open Bluefy, and paste the link in its address bar.</small></div></div>
      <div class="instruction"><span>3</span><div><b>Connect the printer</b><small>In Bluefy go to Printer Setup, tap Connect printer and pick your printer. After that, Print goes straight to it.</small></div></div>
    </div>
    <div class="mini-actions bt-actions keep"><button class="btn btn-primary" onclick="copyAppLink()">Copy app link</button></div>
    <div class="hint-box keep">Easier option: an <b>Android phone</b> or a <b>laptop</b> with Chrome or Edge works with no extra app.</div>`;
  return `<div class="hint-box keep"><b>This browser can't use Bluetooth printing.</b> Open this app in <b>Chrome</b> or <b>Edge</b> on Android, Windows, Mac or Chromebook.</div>
    <div class="mini-actions bt-actions keep"><button class="btn btn-primary" onclick="copyAppLink()">Copy app link</button></div>`;
}

function bluetoothPrinterPage() {
  const ps = state.printerSetup || {};
  const status = DKBT.status();
  const ok = status === 'ok';
  const on = DKBT.connected();
  const saved = DKBT.savedName();
  const test = ps.btTest;
  const badge = !ok ? '<span class="connection-badge bad">Not available here</span>'
    : on ? `<span class="connection-badge">Connected: ${esc(DKBT.name())}</span>`
    : DKBT.optedOut() ? '<span class="connection-badge off">Turned off</span>'
    : '<span class="connection-badge off">Not connected</span>';
  const blocked = status === 'insecure'
    ? '<div class="hint-box"><b>Needs HTTPS.</b> Browsers only allow Bluetooth on secure pages. Open this app with its https:// address (your Render link), or use localhost on the same computer.</div>'
    : status === 'unsupported' ? unsupportedGuide()
    : '';
  return `<div class="setup-grid"><div class="panel setup-main ${ok ? '' : 'bt-na'}">
    <div class="section-head tight"><div><span class="eyebrow">PHONE · TABLET · PC</span><h2>Print over Bluetooth</h2><p>Connects this browser straight to a 58mm Bluetooth thermal printer. No cable, driver or PC needed.</p></div>${badge}</div>
    ${blocked}
    ${DKBT.optedOut() && ok ? '<div class="hint-box">Bluetooth printing is turned off on this device, so <b>Print</b> opens the normal browser print page. Tap <b>Connect printer</b> to turn it back on.</div>' : ''}
    <div class="connection-summary"><div><span>Printer</span><b>${esc(on ? DKBT.name() : (saved || 'Not selected'))}</b></div><div><span>Status</span><b>${on ? 'Connected' : 'Disconnected'}</b></div><div><span>Print channel</span><b>${esc(on ? DKBT.info() : '—')}</b></div></div>
    ${test ? `<div class="test-result ${test.ok ? 'success' : 'failure'}"><b>${test.ok ? '✓ Test receipt sent' : '⚠ Test failed'}</b><p>${esc(test.message)}</p></div>` : ''}
    <div class="mini-actions bt-actions">
      <button class="btn btn-primary" onclick="btConnectUi()" ${ok ? '' : 'disabled'}>${on ? 'Choose another printer' : 'Connect printer'}</button>
      <button class="btn" onclick="btTestUi()" ${ok ? '' : 'disabled'}>Print test receipt</button>
      ${DKBT.optedOut() ? '' : `<button class="btn" onclick="btDisconnectUi()">${on || saved ? 'Disconnect' : 'Use browser printing instead'}</button>`}
    </div>
    <div class="instruction-list">
      <div class="instruction"><span>1</span><div><b>Switch on the printer</b><small>Load paper and keep it within a metre or two. If it was connected to another phone or PC, switch Bluetooth off on that device first.</small></div></div>
      <div class="instruction"><span>2</span><div><b>Turn Bluetooth on</b><small>On Android, allow "Nearby devices" (older Android: Location) for the browser when it asks.</small></div></div>
      <div class="instruction"><span>3</span><div><b>Tap Connect printer</b><small>Pick your printer from the list (names like "PT-210", "MPT-II" or "BlueTooth Printer") and tap Pair. You do not need to pair it in the phone's Bluetooth settings first.</small></div></div>
      <div class="instruction"><span>4</span><div><b>Print a test receipt</b><small>Then use <b>Print</b> on any order. The app reconnects by itself; if it can't, tap the printer chip at the top of the screen.</small></div></div>
    </div>
    <div class="hint-box bt-tip"><b>Good to know:</b> only Bluetooth Low Energy (BLE) printers show up in a browser. A printer that is "classic Bluetooth only" won't appear in the list; use it with the Windows PC tab instead. Hindi / Marathi text prints as "?" because the printer only handles plain English letters.</div>
  </div>
  <div class="panel setup-side ${ok ? '' : 'bt-hide'}"><h2>Troubleshooting</h2>
    <details open><summary>Printer is not in the list</summary><p>Switch the printer off and on, make sure no other phone or PC is connected to it, and keep it close. Some printers show two names (one with "BLE"), so try the other. Check that Bluetooth and the Nearby devices permission are on.</p></details>
    <details><summary>Connected but nothing prints</summary><p>Check paper and the cover, then switch the printer off and on and connect again. The "Print channel" box above shows how the app is talking to the printer.</p></details>
    <details><summary>Strange characters print</summary><p>The printer may not support standard ESC/POS commands. Try the test receipt: the numbers line should print cleanly.</p></details>
    <details><summary>Stops working after a while</summary><p>Many printers go to sleep when idle. Press Print and the app reconnects, or tap the printer chip at the top of the screen.</p></details>
    <details><summary>Printing feels slow</summary><p>Bluetooth Low Energy sends data in small pieces, so a receipt takes a second or two. That is normal.</p></details>
  </div></div>`;
}

function windowsPrinterPage() {
  const p = state.printers || { printers: [], ports: [] }, s = state.settings || {}, ps = state.printerSetup || { step: 1, test: null };
  const step = ps.step || 1;
  const selected = s.printer_target || '';
  const selectedMode = s.printer_mode || 'browser';
  const steps = [
    ['1','Pair in Windows'], ['2','Detect device'], ['3','Select & configure'], ['4','Test & troubleshoot']
  ];
  return `<div class="toolbar"><div><span class="muted">58mm Bluetooth thermal printer wizard</span><div class="subtitle">Set up the printer once, test it, then use <b>Confirm & Print</b> on every order.</div></div>
    <div class="mini-actions"><button class="btn" onclick="loadPrinters()">↻ Refresh devices</button></div></div>
    <div class="setup-steps">${steps.map((x,i) => `<button class="setup-step ${step===i+1?'active':''} ${step>i+1?'done':''}" onclick="setPrinterStep(${i+1})"><span>${step>i+1?'✓':x[0]}</span><b>${x[1]}</b></button>`).join('')}</div>
    ${step === 1 ? printerPairStep() : ''}
    ${step === 2 ? printerDetectStep(p) : ''}
    ${step === 3 ? printerConfigureStep(p, s) : ''}
    ${step === 4 ? printerTestStep(p, s, ps) : ''}`;
}

function printerPairStep() {
  return `<div class="setup-grid"><div class="panel setup-main"><div class="section-head tight"><div><span class="eyebrow">STEP 1 OF 4</span><h2>Pair the printer with Windows</h2><p>The app cannot reliably pair classic Bluetooth thermal printers from the browser. Windows handles the Bluetooth pairing; this wizard guides you through it.</p></div><span class="connection-badge">Windows pairing</span></div>
    <div class="instruction-list">
      <div class="instruction"><span>1</span><div><b>Power on the 58mm printer</b><small>Keep the printer close to this Windows PC and make sure the Bluetooth light is active.</small></div></div>
      <div class="instruction"><span>2</span><div><b>Open Windows Bluetooth settings</b><small>Go to Bluetooth & devices → Add device → Bluetooth.</small></div></div>
      <div class="instruction"><span>3</span><div><b>Select the printer</b><small>Choose the printer name shown by the device. If Windows asks for a PIN, use the PIN printed on the device/manual or supplied by the vendor.</small></div></div>
      <div class="instruction"><span>4</span><div><b>Wait until Windows shows Connected</b><small>Do not start the test until Windows has finished pairing the device.</small></div></div>
    </div>
    <div class="hint-box"><b>Important:</b> Bluetooth pairing and Bluetooth printing are different. After pairing, Windows may expose the printer as a normal printer queue or as a COM port. The next steps detect both.</div>
    <div class="mini-actions"><a class="btn btn-primary" href="ms-settings:bluetooth">Open Windows Bluetooth settings</a><button class="btn" onclick="setPrinterStep(2);loadPrinters()">I've paired it → Detect device</button></div>
  </div>
  <div class="panel setup-side"><h2>Before you continue</h2><div class="checklist"><div>✓ Printer is powered on</div><div>✓ Printer is within Bluetooth range</div><div>✓ Windows Bluetooth is ON</div><div>✓ Printer is not connected to another PC/phone</div></div><h3 class="device-title">If pairing asks for a PIN</h3><p class="muted">Use the printer's supplied PIN rather than guessing. Common PINs vary by model, firmware and vendor.</p></div></div>`;
}

function printerDetectStep(p) {
  const printers = p.printers || [], ports = p.ports || [];
  return `<div class="setup-grid"><div class="panel setup-main"><div class="section-head tight"><div><span class="eyebrow">STEP 2 OF 4</span><h2>Detect the paired device</h2><p>Refresh the local computer and choose how Windows exposed the printer.</p></div><span class="connection-badge">${printers.length || ports.length ? 'Device found' : 'Waiting for device'}</span></div>
    <div class="mini-actions"><button class="btn btn-primary" onclick="loadPrinters()">↻ Scan again</button><button class="btn" onclick="setPrinterStep(1)">← Back</button></div>
    <h3 class="device-title">Windows printer queues</h3>${printers.map(x => `<div class="device-row selectable"><div><b>${esc(x.name)}</b><small>${esc(x.driver || 'Printer')} ${x.port ? '· Windows port '+esc(x.port) : ''}</small></div><button class="btn btn-small btn-primary" onclick='pickPrinter("windows",${jsArg(x.name)})'>Select</button></div>`).join('') || '<div class="empty">No Windows printer queue detected.</div>'}
    <h3 class="device-title">Bluetooth / COM ports</h3>${ports.map(x => `<div class="device-row selectable"><div><b>${esc(x.port)}</b><small>${esc(x.name)}${x.manufacturer ? ' · '+esc(x.manufacturer) : ''}</small></div><button class="btn btn-small btn-primary" onclick='pickPrinter("serial",${jsArg(x.port)})'>Select</button></div>`).join('') || '<div class="empty">No COM/serial port detected.</div>'}
  </div>
  <div class="panel setup-side"><h2>Which one should I choose?</h2><div class="choice-card"><b>Windows printer queue</b><p>Choose this when the printer appears under Windows Printers & scanners and has a printer name.</p><span>Good first choice</span></div><div class="choice-card"><b>COM port</b><p>Choose this when Device Manager shows something like COM3, COM5 or COM8 for the Bluetooth printer.</p><span>ESC/POS direct</span></div><div class="hint-box">If you only see a COM port, that is normal for some Bluetooth SPP thermal printers.</div></div></div>`;
}

function printerConfigureStep(p, s) {
  const printers = p.printers || [], ports = p.ports || [];
  return `<div class="setup-grid"><div class="panel setup-main"><div class="section-head tight"><div><span class="eyebrow">STEP 3 OF 4</span><h2>Select the printing path</h2><p>Save the device that should receive kitchen tickets.</p></div><span class="connection-badge">${selectedLabel(s)}</span></div>
    <div class="form-grid"><label>Connection type<select id="p-mode" onchange="syncPrinterTargets()"><option value="windows" ${s.printer_mode==='windows'?'selected':''}>Windows printer queue</option><option value="serial" ${s.printer_mode==='serial'?'selected':''}>Bluetooth / COM (ESC/POS)</option><option value="browser" ${s.printer_mode==='browser'?'selected':''}>Browser print fallback</option></select></label>
    <label>Printer / COM target<select id="p-target"><option value="">Select a device</option>${(printers||[]).map(x => `<option data-mode="windows" ${s.printer_mode==='serial'?'hidden':''} value="${esc(x.name)}" ${s.printer_mode==='windows'&&s.printer_target===x.name?'selected':''}>${esc(x.name)}${x.port?' · '+esc(x.port):''}</option>`).join('')}${(ports||[]).map(x => `<option data-mode="serial" ${s.printer_mode!=='serial'?'hidden':''} value="${esc(x.port)}" ${s.printer_mode==='serial'&&s.printer_target===x.port?'selected':''}>${esc(x.port)} · ${esc(x.name)}</option>`).join('')}</select></label></div>
    <div class="form-grid"><label>Displayed printer name<input id="p-name" value="${esc(s.printer_name || '58mm Mini Thermal Printer')}"></label><label>COM baud rate<select id="p-baud"><option value="9600" ${Number(s.printer_baudrate||9600)===9600?'selected':''}>9600</option><option value="19200" ${Number(s.printer_baudrate)===19200?'selected':''}>19200</option><option value="38400" ${Number(s.printer_baudrate)===38400?'selected':''}>38400</option><option value="57600" ${Number(s.printer_baudrate)===57600?'selected':''}>57600</option><option value="115200" ${Number(s.printer_baudrate)===115200?'selected':''}>115200</option></select></label></div>
    <div class="hint-box"><b>COM-port note:</b> 9600 is the starting value. If the port opens but the printer produces nothing or garbage text, check the printer documentation/vendor settings and try the correct baud rate.</div>
    <div class="mini-actions"><button class="btn" onclick="setPrinterStep(2)">← Back</button><button class="btn" onclick="loadPrinters()">↻ Refresh list</button><button class="btn btn-primary" onclick="savePrinterConfig(true)">Save & test →</button></div>
  </div>
  <div class="panel setup-side"><h2>Current configuration</h2><div class="metric-line"><span>Mode</span><b>${esc(s.printer_mode || 'browser')}</b></div><div class="metric-line"><span>Target</span><b>${esc(s.printer_target || 'Not selected')}</b></div><div class="metric-line"><span>Baud</span><b>${s.printer_baudrate || 9600}</b></div><p class="muted">The selected device is used by automatic order printing after confirmation.</p></div></div>`;
}

function selectedLabel(s) {
  if (!s.printer_target || s.printer_mode === 'browser') return 'Not configured';
  return 'Configured: ' + (s.printer_target || 'device');
}

function printerTestStep(p, s, ps) {
  const test = ps.test;
  return `<div class="setup-grid"><div class="panel setup-main"><div class="section-head tight"><div><span class="eyebrow">STEP 4 OF 4</span><h2>Test the connection</h2><p>Send a real 58mm test receipt using the selected path.</p></div><span class="connection-badge">${test ? (test.ok ? 'Test passed' : 'Test failed') : 'Ready to test'}</span></div>
    <div class="connection-summary"><div><span>Method</span><b>${esc(s.printer_mode || 'browser')}</b></div><div><span>Target</span><b>${esc(s.printer_target || 'Browser print')}</b></div><div><span>Paper</span><b>58mm</b></div></div>
    ${test ? `<div class="test-result ${test.ok?'success':'failure'}"><b>${test.ok?'✓ Connection test passed':'⚠ Connection test failed'}</b><p>${esc(test.message)}</p></div>` : '<div class="empty setup-empty">No test has been run yet.</div>'}
    <div class="mini-actions"><button class="btn" onclick="setPrinterStep(3)">← Change device</button><button class="btn btn-primary" onclick="testHardwarePrint()">${s.printer_mode==='browser'?'Open print test':'Print test receipt again'}</button><button class="btn" onclick="goToDashboardAfterPrinter()">Finish setup</button></div>
    <div class="hint-box"><b>Next:</b> Create an order and press <b>Confirm & Print</b>. The same configured printer will receive the full customer/order ticket.</div>
  </div>
  <div class="panel setup-side"><h2>Troubleshooting</h2><details open><summary>Printer does not appear</summary><p>Confirm Windows says the Bluetooth device is connected. Turn the printer off/on, then click Refresh devices. For COM printing, open Device Manager → Ports (COM & LPT) and verify a COM port exists.</p></details><details><summary>COM port is visible but print fails</summary><p>Close any other app that may be using the COM port. Recheck the selected COM number and try the baud rate specified by the printer vendor.</p></details><details><summary>Windows printer queue is visible but no paper prints</summary><p>Use Windows' own Print Test Page first. If Windows cannot print, fix the driver/queue before testing from Demo Kitchen.</p></details><details><summary>Garbage characters print</summary><p>The baud rate or printer language may be wrong for a serial connection. Verify ESC/POS support and the printer's serial settings.</p></details><details><summary>Printer paired, then disconnects</summary><p>Keep the printer powered and charged, remove old pairings from other phones/PCs, then reconnect it in Windows and scan again.</p></details></div></div>`;
}

function settingsPage() {
  const s = state.settings || {};
  const current = String(s.business_name || '').trim();
  return `<form class="settings-grid" onsubmit="return saveSettings(event)">
    <div class="panel biz-settings-panel"><h2>Business settings</h2><p>This name is shown on the Main Menu and printed on every ticket, exactly as you type it.</p>
      <label>Business name<input id="s-name" name="business_name" value="${esc(s.business_name || '')}" placeholder="Your kitchen name" autocomplete="organization" required oninput="previewBizName(this)"></label>
      <small id="s-name-preview" class="field-hint">${current ? 'Will show as: ' + esc(current) : 'Enter a name to show on the Main Menu and on tickets.'}</small>
      <button class="btn btn-primary btn-wide" type="submit" id="save-biz-btn">Save business details</button>
      <label>Address<textarea id="s-address">${esc(s.address || '')}</textarea></label>
      <label>Phone<input id="s-phone" value="${esc(s.phone || '')}"></label>
      <label>GST number<input id="s-gst" value="${esc(s.gst_number || '')}"></label>
      <label>Food license (FSSAI) number<input id="s-fssai" value="${esc(s.food_license || '')}" placeholder="e.g. 12345678901234" inputmode="numeric"><small class="field-hint">Printed on every receipt and on today's total. Leave empty to hide it.</small></label>
    </div>
    <div class="panel"><h2>Printer defaults</h2><label>Printer name<input id="s-printer" value="${esc(s.printer_name || '')}"></label><label>Paper size<select id="s-paper"><option ${s.paper_size==='58mm'?'selected':''}>58mm</option></select></label><label class="switchline"><input id="s-auto" type="checkbox" ${s.auto_print?'checked':''}> Auto-print new orders</label><button class="btn" type="button" onclick="nav('printers')">Open Printer Center</button></div>
    <div class="panel"><h2>Order settings</h2><p>Simple defaults for the trial.</p><label>Default order type<select id="s-default"><option ${s.default_order_type==='Pickup'?'selected':''}>Pickup</option><option ${s.default_order_type==='Delivery'?'selected':''}>Delivery</option></select></label><label>Tax %<input id="s-tax" type="number" value="${s.tax_percent || 0}"></label><label>Delivery fee<input id="s-fee" type="number" value="${s.delivery_fee || 0}"></label><button class="btn btn-primary" type="submit">Save settings</button></div>
    ${usersPanelHtml()}<div class="panel danger-zone"><h2>Start fresh</h2><p>Deletes every order and starts again from zero. Menu, inventory and settings are kept.</p><button class="btn btn-danger" type="button" onclick="clearAllOrders()">Clear all orders</button></div>
    <div class="settings-save-bar"><button class="btn btn-primary" type="submit">Save all settings</button></div>
  </form>`;
}
function previewBizName(el) {
  const n = String((el && el.value) || '').trim();
  const p = document.getElementById('s-name-preview');
  if (p) p.textContent = n ? ('Will show as: ' + n) : 'Enter a name to show on the Main Menu and on tickets.';
}

function categoryIcon(category) {
  const name = typeof category === 'string' ? category : category?.name;
  const cat = (state.categories || []).find(c => c.name === name);
  return cat?.icon || '';
}
// Safe to place inside a SINGLE-quoted HTML attribute (inline onclick). & must be escaped too, otherwise &quot; in the data
// would be decoded by the browser and break out of the JavaScript string.
function jsArg(value) { return JSON.stringify(String(value)).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/'/g, '&#39;'); }

function renderOrderPicker() {
  const wrap = document.getElementById('order-item-picker');
  if (!wrap) return;
  const items = state.menu.filter(i => i.available);
  const cart = state.newOrder?.cart || {};
  const cartCount = Object.values(cart).reduce((sum,q)=>sum+Number(q||0),0);
  const cartTotal = Object.entries(cart).reduce((sum,[id,q]) => { const item=items.find(i=>i.id===Number(id)); return sum+(item?item.price*Number(q||0):0); },0);
  const categoryRows = (state.categories || []).map(c => ({...c, count:items.filter(i=>(i.category||'Other')===c.name).length})).filter(c=>c.count>0);
  // Include any legacy/unknown categories so old menu data never disappears.
  const knownNames = new Set(categoryRows.map(c=>c.name));
  items.forEach(i=>{ const name=i.category||'Other'; if(!knownNames.has(name)){categoryRows.push({id:'legacy-'+name,name,icon:'',count:items.filter(x=>(x.category||'Other')===name).length}); knownNames.add(name);} });

  if (!state.newOrder.category) {
    wrap.innerHTML = `<div class="order-picker-head"><div><b>Choose a category</b><span class="muted">Tap a category to see its dishes.</span></div><div class="mini-actions"><span class="order-cart-badge">${cartCount} item${cartCount===1?'':'s'}</span></div></div>
      <div class="order-category-grid">${categoryRows.map(c => `<button type="button" class="order-category-card" onclick='selectOrderCategory(${jsArg(c.name)})'>${c.icon ? `<span class="order-category-icon">${esc(c.icon)}</span>` : ''}<span class="order-category-name">${esc(c.name)}</span><span class="order-category-count">${c.count} item${c.count===1?'':'s'}</span></button>`).join('')}</div>
      <div class="order-cart-summary ${cartCount?'has-items':''}"><span>${cartCount?`Selected: ${cartCount} item${cartCount===1?'':'s'}`:'No items selected yet'}</span><b>${money(cartTotal)}</b></div>`;
    return;
  }
  const categoryItems = items.filter(i=>(i.category||'Other')===state.newOrder.category);
  const search=String(state.newOrder.search||'').trim().toLowerCase();
  const catItems=search?categoryItems.filter(i=>String(i.name||'').toLowerCase().includes(search)):categoryItems;
  const icon=categoryIcon(state.newOrder.category);
  wrap.innerHTML = `<div class="cat-open-head"><button type="button" class="back-categories" onclick="selectOrderCategory(null)">← Back to Categories</button><div class="cat-open-title"><span class="order-category-title">${icon ? esc(icon)+' ' : ''}${esc(state.newOrder.category)}</span><span class="order-cart-badge">${cartCount} item${cartCount===1?'':'s'}</span></div></div>
    <div class="order-item-search"><span class="search-icon">⌕</span><input id="order-item-search" type="search" autocomplete="off" placeholder="Search ${esc(state.newOrder.category)}..." value="${esc(state.newOrder.search||'')}" oninput="setOrderSearch(this.value)"><button type="button" class="clear-search" onclick="setOrderSearch('')" aria-label="Clear search">×</button></div>
    <div class="order-search-meta">${search?`${catItems.length} result${catItems.length===1?'':'s'} in ${esc(state.newOrder.category)}`:`${categoryItems.length} item${categoryItems.length===1?'':'s'} in ${esc(state.newOrder.category)}`}</div>
    <div class="order-product-grid">${catItems.map(i=>{const qty=Number(cart[i.id]||0);return `<div class="order-product-card ${qty?'selected':''}"><div class="product-main"><div class="product-icon">${icon ? esc(icon) : '•'}</div><div><b>${esc(i.name)}</b><span>${qty ? `${qty} × ${money(i.price)} = <em>${money(i.price*qty)}</em>` : money(i.price)}</span></div></div><div class="product-controls">${qty?`<button type="button" class="qty-btn" onclick="changeOrderQty(${i.id},-1)">−</button><strong>${qty}</strong><button type="button" class="qty-btn" onclick="changeOrderQty(${i.id},1)">+</button>`:`<button type="button" class="btn btn-primary btn-small add-item-btn" onclick="changeOrderQty(${i.id},1)">＋ Add</button>`}</div></div>`;}).join('')||'<div class="order-search-empty">No items match your search.</div>'}</div>
    <div class="order-cart-summary ${cartCount?'has-items':''}"><span>${cartCount?`Selected: ${cartCount} item${cartCount===1?'':'s'}`:'No items selected'}</span><b>${money(cartTotal)}</b></div>`;
  // keep the coloured Back button visible just under the sticky header while scrolling a long item list
  const mh = document.querySelector('#modal .modal-head');
  if (mh) wrap.style.setProperty('--back-top', Math.max(0, mh.offsetHeight - 12) + 'px');
}
function selectOrderCategory(category) { state.newOrder.category=category; state.newOrder.search=''; renderOrderPicker(); }

function setOrderSearch(value) {
  const input = document.getElementById('order-item-search');
  const start = input ? input.selectionStart : null;
  state.newOrder.search = value;
  renderOrderPicker();
  const next = document.getElementById('order-item-search');
  if (next) {
    next.focus();
    const pos = start == null ? value.length : Math.min(start, value.length);
    next.setSelectionRange(pos, pos);
  }
}

function changeOrderQty(id, delta) {
  const current = Number(state.newOrder?.cart?.[id] || 0) + delta;
  if (current <= 0) delete state.newOrder.cart[id];
  else state.newOrder.cart[id] = current;
  renderOrderPicker();
}

function newOrderSelectedItems() {
  const availableIds = new Set(state.menu.filter(i => i.available).map(i => i.id));
  return Object.entries(state.newOrder.cart || {})
    .filter(([id, q]) => availableIds.has(Number(id)) && Number(q) > 0)
    .map(([id, quantity]) => {
      const item = state.menu.find(i => i.id === Number(id));
      return item ? {...item, quantity:Number(quantity), line_total:item.price * Number(quantity)} : null;
    }).filter(Boolean);
}
function newOrderCartTotals() {
  const items = newOrderSelectedItems();
  const subtotal = items.reduce((sum, i) => sum + i.line_total, 0);
  const delivery = Number(state.newOrder.form?.order_type === 'Delivery' ? (state.settings?.delivery_fee || 0) : 0);
  const tax = subtotal * Number(state.settings?.tax_percent || 0) / 100;
  return {items, subtotal, delivery, tax, total: subtotal + delivery + tax};
}
function captureNewOrderForm(form) {
  const f = new FormData(form);
  state.newOrder.form = {
    customer_name: String(f.get('customer_name') || ''), phone: String(f.get('phone') || ''),
    order_type: String(f.get('order_type') || 'Pickup'), payment_method: paymentMethod({ payment_method: f.get('payment_method') }), payment_status: 'Paid',
    address: String(f.get('address') || ''), special_instructions: String(f.get('special_instructions') || '')
  };
}
function orderFormHtml() {
  const f = state.newOrder.form || {};
  return `<div class="new-order-fields"><div class="form-grid"><label>Customer name <small class="opt">(optional)</small><input name="customer_name" value="${esc(f.customer_name || '')}" placeholder="Leave empty for walk-in"></label><label>Phone <small class="opt">(optional)</small><input name="phone" type="tel" inputmode="numeric" value="${esc(f.phone || '')}" placeholder="Leave empty if not needed"></label></div>
    <div class="form-grid"><label>Order type<select name="order_type"><option ${f.order_type!=='Delivery'?'selected':''}>Pickup</option><option ${f.order_type==='Delivery'?'selected':''}>Delivery</option></select></label>${paySelectHtml(f.payment_method)}</div>
    <label>Address<input name="address" value="${esc(f.address || '')}" placeholder="Optional"></label></div>`;
}
function reviewOrderHtml() {
  const {items, subtotal, delivery, tax, total} = newOrderCartTotals();
  const f = state.newOrder.form || {};
  return `<div class="review-order-wrap"><div class="review-banner"><div><span class="muted">FINAL CHECK</span><h2>Review order before billing</h2><p>Check the dishes and quantities. Name and phone are optional. Nothing is printed yet.</p></div><span class="review-total">${money(total)}</span></div>
    <div class="review-customer-grid"><div><span>Customer</span><b>${esc(f.customer_name || 'Walk-in')}</b></div><div><span>Phone</span><b>${esc(f.phone || '—')}</b></div><div><span>Order type</span><b>${esc(f.order_type || 'Pickup')}</b></div><div><span>Payment</span><b>${esc(paymentMethod(f))}</b></div><div class="full"><span>Address</span><b>${esc(f.address || '—')}</b></div></div>
    <div class="review-items"><div class="review-items-head"><h3>Selected items</h3><button type="button" class="btn btn-small" onclick="backToNewOrderEdit()">← Add / edit items</button></div>${items.map(i => `<div class="review-item"><div><b>${i.quantity}× ${esc(i.name)}</b><span>${money(i.price)} each</span></div><div class="review-item-actions"><button class="qty-btn" type="button" onclick="adjustReviewQty(${i.id},-1)">−</button><strong>${i.quantity}</strong><button class="qty-btn" type="button" onclick="adjustReviewQty(${i.id},1)">+</button><button class="icon-btn danger" type="button" onclick="removeReviewItem(${i.id})" title="Remove item">×</button><b>${money(i.line_total)}</b></div></div>`).join('') || '<div class="empty">No items selected.</div>'}</div>
    ${f.special_instructions ? `<div class="note large">Note: ${esc(f.special_instructions)}</div>` : ''}
    <div class="review-totals"><div><span>Subtotal</span><b>${money(subtotal)}</b></div><div><span>Delivery</span><b>${money(delivery)}</b></div><div><span>Tax</span><b>${money(tax)}</b></div><div class="grand"><span>Total</span><b>${money(total)}</b></div></div>
    <div class="modal-foot"><button type="button" class="btn" onclick="backToNewOrderEdit()">Back to edit</button><button type="button" class="btn btn-primary" onclick="confirmReviewedOrder()">Confirm Order</button></div></div>`;
}
function renderNewOrderReview() {
  const modal = document.querySelector('#modal .order-modal');
  if (!modal) return;
  modal.innerHTML = `<div class="modal-head"><div><span class="muted">Quick ticket</span><h2>Review New Order</h2></div><button class="icon-btn" onclick="closeModal()">×</button></div>${reviewOrderHtml()}`;
}
function reviewNewOrder(form) {
  captureNewOrderForm(form);
  const {items} = newOrderCartTotals();
  if (!items.length) return alert('Choose at least one item before reviewing the order.');
  renderNewOrderReview();
}
function backToNewOrderEdit() {
  const modal = document.querySelector('#modal .order-modal');
  if (!modal) return;
  modal.innerHTML = `<div class="modal-head"><div><span class="muted">Quick ticket</span><h2>New Order</h2></div><button class="icon-btn" onclick="closeModal()">×</button></div><form id="order-form">${orderFormHtml()}<div id="order-item-picker"></div><label>Special instructions<textarea name="special_instructions" placeholder="Less spicy, extra sauce...">${esc(state.newOrder.form?.special_instructions || '')}</textarea></label><div class="modal-foot"><button type="button" class="btn" onclick="closeModal()">Cancel</button><button class="btn btn-primary" type="submit">Review Order →</button></div></form>`;
  renderOrderPicker();
  const form = document.getElementById('order-form');
  form.onsubmit = async e => { e.preventDefault(); reviewNewOrder(e.target); };
}
function adjustReviewQty(id, delta) { changeOrderQty(id, delta); renderNewOrderReview(); }
function removeReviewItem(id) { delete state.newOrder.cart[id]; renderNewOrderReview(); }

async function confirmReviewedOrder() {
  const f = state.newOrder.form || {};
  const {items, subtotal, delivery, tax, total} = newOrderCartTotals();

  if (!items.length) return alert('Add at least one item before confirming.');

  // Prevent double-taps from creating duplicate orders.
  if (state.newOrder.submitting) return;
  state.newOrder.submitting = true;

  const selected = items.map(i => ({
    menu_item_id: i.id,
    quantity: i.quantity
  }));

  // Give immediate feedback without a popup.
  const confirmBtn = document.querySelector('#modal .modal-foot .btn-primary');
  if (confirmBtn) {
    confirmBtn.disabled = true;
    confirmBtn.textContent = 'Saving…';
  }

  try {
    // Create the order. Printing is deliberately NOT part of confirmation.
    const o = await api('/api/orders', {
      method:'POST',
      body:JSON.stringify({
        customer_name: String(f.customer_name || '').trim(),
        phone: String(f.phone || '').trim(),
        address: f.address,
        order_type: f.order_type,
        items: selected,
        special_instructions: f.special_instructions,
        payment_method: paymentMethod(f),
        payment_status: 'Paid',
        delivery_fee: 0,
        auto_print: false
      })
    });

    // Use the returned order immediately. Do NOT wait for the six-resource load().
    const currentMonth = new Date().toISOString().slice(0, 7);
    state.selectedOrder = null;
    state.page = 'dashboard';
    if (state.selectedMonth === currentMonth) {
      state.orders = [o, ...(state.orders || [])];
    } else {
      state.selectedMonth = currentMonth;
      state.inventory.month = currentMonth;
      await loadPeriodData();
    }

    // Reset the form state before rendering the dashboard.
    state.newOrder = {
      category: null,
      cart: {},
      search: '',
      form: {
        customer_name:'',
        phone:'',
        order_type:'Pickup',
        payment_method:'Cash',
        payment_status:'Paid',
        address:'',
        special_instructions:''
      },
      submitting: false
    };

    // Return to the dashboard as soon as the server confirms the insert.
    closeModal();
    render();

    // Refresh only orders in the background. This never blocks the UI.
    api('/api/orders')
      .then(rows => {
        state.orders = rows;
        if (state.page === 'dashboard' || state.page === 'orders') render();
      })
      .catch(() => {});

  } catch (err) {
    state.newOrder.submitting = false;
    const btn = document.querySelector('#modal .modal-foot .btn-primary');
    if (btn) {
      btn.disabled = false;
      btn.textContent = 'Confirm Order';
    }
    alert(err.message || 'Could not create order');
  }
}

function openNewOrder() {
  state.newOrder = {category: null, cart: {}, search: '', form: {customer_name:'', phone:'', order_type:'Pickup', payment_method:'Cash', payment_status:'Paid', address:'', special_instructions:''}};
  const html = `<div class="modal-card order-modal"><div class="modal-head"><div><span class="muted">Quick ticket</span><h2>New Order</h2></div><button class="icon-btn" onclick="closeModal()">×</button></div>
  <form id="order-form">${orderFormHtml()}<div id="order-item-picker"></div><label>Special instructions<textarea name="special_instructions" placeholder="Less spicy, extra sauce..."></textarea></label><div class="modal-foot"><button type="button" class="btn" onclick="closeModal()">Cancel</button><button class="btn btn-primary" type="submit">Review Order →</button></div></form></div>`;
  showModal(html);
  renderOrderPicker();
  document.getElementById('order-form').onsubmit = async e => { e.preventDefault(); reviewNewOrder(e.target); };
}

function showModal(html) {
  let m = document.getElementById('modal');
  if (!m) { m = document.createElement('div'); m.id = 'modal'; document.body.appendChild(m); }
  const scrollY = window.scrollY || window.pageYOffset || 0;
  document.documentElement.dataset.modalScrollY = String(scrollY);
  document.documentElement.classList.add('modal-open');
  document.body.classList.add('modal-open');
  document.body.style.top = `-${scrollY}px`;
  m.innerHTML = html;
  m.classList.add('show');
}
function closeModal() {
  const m = document.getElementById('modal');
  if (m) m.classList.remove('show');
  const scrollY = Number(document.documentElement.dataset.modalScrollY || 0);
  document.documentElement.classList.remove('modal-open');
  document.body.classList.remove('modal-open');
  document.body.style.top = '';
  delete document.documentElement.dataset.modalScrollY;
  window.scrollTo(0, scrollY);
}

// Android-safe modal touch lock: prevent page scroll/scroll chaining while
// allowing the active modal card to scroll. At the card edges, consume the
// gesture so the Inventory/Orders/Menu page underneath never moves.
let modalTouchY = 0;
document.addEventListener('touchstart', e => {
  const m = document.getElementById('modal');
  if (!m || !m.classList.contains('show')) return;
  modalTouchY = e.touches[0]?.clientY || 0;
}, {passive:true});
document.addEventListener('touchmove', e => {
  const m = document.getElementById('modal');
  if (!m || !m.classList.contains('show')) return;
  const card = e.target.closest?.('#modal.show > .modal-card');
  if (!card) { e.preventDefault(); return; }
  const y = e.touches[0]?.clientY || modalTouchY;
  // iOS Safari can otherwise hand a touch gesture back to the fixed page.
  if (card.scrollHeight <= card.clientHeight) { e.preventDefault(); return; }
  const dy = y - modalTouchY;
  const atTop = card.scrollTop <= 0;
  const atBottom = Math.ceil(card.scrollTop + card.clientHeight) >= card.scrollHeight;
  if ((atTop && dy > 0) || (atBottom && dy < 0)) e.preventDefault();
}, {passive:false});
document.addEventListener('wheel', e => {
  const m = document.getElementById('modal');
  if (!m || !m.classList.contains('show')) return;
  const card = e.target.closest?.('#modal.show > .modal-card');
  if (!card) e.preventDefault();
}, {passive:false});
async function showOrder(id) { const o = await api('/api/orders/' + id); state.selectedOrder = o; showModal(orderDetails(o)); }
function orderDetails(o) { return `<div class="modal-card wide"><div class="modal-head"><div><span class="muted">Order details</span><h2>#${o.order_number}${String(o.customer_name || '').trim() ? ' · ' + esc(o.customer_name) : ' · Walk-in'}</h2></div><button class="icon-btn" onclick="closeModal()">×</button></div><div class="detail-grid"><div><span>Status</span><b>${o.status}</b></div><div><span>Total</span><b>${money(o.total)}</b></div><div><span>Type</span><b>${o.order_type}</b></div><div><span>Payment</span><b>${paymentMethod(o)}</b></div></div><div class="detail-items">${o.items.map(i => `<div><span>${i.quantity}× ${esc(i.item_name)}</span><b>${money(i.unit_price*i.quantity)}</b></div>`).join('')}${o.phone ? `<div><span>Customer phone</span><b>${esc(o.phone)}</b></div>` : ''}${o.address?`<div><span>Address</span><b>${esc(o.address)}</b></div>`:''}</div>${o.special_instructions?`<div class="note large">${esc(o.special_instructions)}</div>`:''}<div class="status-flow">${['New','Accepted','Preparing','Ready','Completed'].map(s => `<button class="flow-btn ${o.status===s?'current':''}" onclick="moveOrder(${o.id},'${s}')">${s}</button>`).join('')}</div><div class="modal-foot"><button class="btn" onclick="editOrder(${o.id})">Edit order</button><button class="btn" onclick="printOrder(${o.id})">Print order</button><button class="btn btn-danger" onclick="deleteOrder(${o.id})">Delete</button><button class="btn btn-primary" onclick="closeModal()">Done</button></div></div>`; }
async function moveOrder(id, status) {
  const updated = await api('/api/orders/' + id, { method:'PUT', body:JSON.stringify({status}) });
  const idx = state.orders.findIndex(x => x.id === id);
  if (idx >= 0) state.orders[idx] = updated;
  closeModal(); render();
}

async function editOrder(id) {
  const o = await api('/api/orders/' + id);
  showModal(`<div class="modal-card wide"><div class="modal-head"><div><span class="muted">Order management</span><h2>Edit order #${o.order_number}</h2></div><button class="icon-btn" onclick="closeModal()">×</button></div>
    <form id="order-edit-form">
      <div class="form-grid"><label>Customer name <small class="opt">(optional)</small><input name="customer_name" value="${esc(o.customer_name || '')}"></label><label>Phone <small class="opt">(optional)</small><input name="phone" type="tel" value="${esc(o.phone || '')}"></label></div>
      <div class="form-grid"><label>Order type<select name="order_type"><option ${o.order_type==='Pickup'?'selected':''}>Pickup</option><option ${o.order_type==='Delivery'?'selected':''}>Delivery</option></select></label>${paySelectHtml(o.payment_method)}</div>
      <label>Address<input name="address" value="${esc(o.address || '')}"></label>
      <label>Special instructions<textarea name="special_instructions">${esc(o.special_instructions || '')}</textarea></label>
      <div class="section-head tight"><div><h3>Items</h3><p>Change quantities. Set quantity to 0 to remove an item.</p></div></div>
      <div class="order-edit-items">${o.items.map(i => `<div class="order-edit-item"><div><b>${esc(i.item_name)}</b><small>${money(i.unit_price)} each</small></div><input class="order-edit-qty" data-menu-id="${i.menu_item_id}" type="number" min="0" step="1" value="${i.quantity}"></div>`).join('')}</div>
      <div class="modal-foot"><button class="btn" type="button" onclick="closeModal()">Cancel</button><button class="btn btn-primary" type="submit">Save changes</button></div>
    </form></div>`);
  const form = document.getElementById('order-edit-form');
  form.onsubmit = async e => {
    e.preventDefault();
    const f = new FormData(form);
    const items = [...form.querySelectorAll('.order-edit-qty')].map(input => ({menu_item_id:Number(input.dataset.menuId), quantity:Number(input.value || 0)})).filter(x => x.quantity > 0);
    if (!items.length) return alert('Keep at least one item in the order.');
    try {
      const updated = await api('/api/orders/' + o.id, { method:'PUT', body:JSON.stringify({customer_name:String(f.get('customer_name')||'').trim(),phone:String(f.get('phone')||'').trim(),address:String(f.get('address')||''),order_type:String(f.get('order_type')||'Pickup'),payment_method:paymentMethod({payment_method:f.get('payment_method')}),payment_status:'Paid',special_instructions:String(f.get('special_instructions')||''),items}) });
      closeModal();
      const idx = state.orders.findIndex(x => x.id === updated.id);
      if (idx >= 0) state.orders[idx] = updated;
      render();
    } catch (err) { alert(err.message || 'Could not update order'); }
  };
}

function receiptHtml(o) {

  const s = state.settings || {};
  return `<html><head><title>Order #${o.order_number}</title><style>${ticketPrintCss()}</style></head><body><div class="center biz">${esc(bizName(s))}</div><div class="center">${licenseHtml(s)}<br>KITCHEN ORDER</div><div class="line"></div><div class="row"><span>Order</span><span>#${o.order_number}</span></div><div class="row"><span>Date</span><span>${fmtDate(o.created_at)}</span></div><div class="row"><span>Time</span><span>${fmtTime(o.created_at)}</span></div><div class="line"></div>${customerBlockHtml(o)}${o.items.map(i=>`<div class="row"><span>${i.quantity}× ${esc(i.item_name)}</span><span>${money(i.unit_price*i.quantity)}</span></div>`).join('')}<div class="line"></div><div class="row"><span>Subtotal</span><span>${money(o.subtotal)}</span></div><div class="row"><span>Delivery</span><span>${money(o.delivery_fee)}</span></div><div class="row"><span>Tax</span><span>${money(o.tax)}</span></div><div class="row total"><span>TOTAL</span><span>${money(o.total)}</span></div><div class="line"></div><div>Type: ${esc(o.order_type)}</div><div>Payment: ${esc(paymentMethod(o))}</div>${o.special_instructions?`<div class="line"></div><div class="note">Note: ${esc(o.special_instructions)}</div>`:''}<div class="thanks">THANK YOU</div><script>window.onload=()=>setTimeout(()=>window.print(),250)</script></body></html>`;
}
function isMobileBrowser() {
  const ua = navigator.userAgent || '';
  return /Android|iPhone|iPad|iPod|Mobi/i.test(ua) || (/Macintosh/.test(ua) && navigator.maxTouchPoints > 1);   // last part = iPad that reports as a Mac
}
function openPrintShell() {
  // Phones and tablets: print from a hidden frame in this same page instead of opening a new browser tab.
  // It exposes the same document.open/write/close and close() the callers already use.
  if (isMobileBrowser()) {
    try {
      const old = document.getElementById('dk-print-frame'); if (old) old.remove();
      const frame = document.createElement('iframe');
      frame.id = 'dk-print-frame'; frame.setAttribute('aria-hidden', 'true');
      frame.style.cssText = 'position:fixed;right:0;bottom:0;width:0;height:0;border:0;opacity:0;pointer-events:none';
      document.body.appendChild(frame);
      let html = '';
      return { document: { open() { html = ''; }, write(h) { html += h; }, close() { frame.srcdoc = html; } }, close() { frame.remove(); } };
    } catch { /* fall through to the normal popup */ }
  }
  try { const win = window.open('', '_blank', 'width=420,height=700'); if (win) { win.document.write('<html><body style="font-family:system-ui;padding:24px">Preparing ticket…</body></html>'); win.document.close(); } return win; } catch { return null; }
}
async function printOrderInto(o, win) {
  const s = state.settings || {};
  if ((s.printer_mode === 'windows' || s.printer_mode === 'serial') && s.printer_target) {
    try {
      const result = await api('/api/printers/print', { method:'POST', body:JSON.stringify({order_id:o.id}) });
      if (win) win.close();
      alert(result.message || 'Print job sent successfully.');
      return;
    } catch (err) {
      if (win) { win.document.open(); win.document.write(receiptHtml(o)); win.document.close(); }
      alert('Direct printer failed, so the browser print preview was opened instead.\n\n' + err.message);
      return;
    }
  }
  if (win) { win.document.open(); win.document.write(receiptHtml(o)); win.document.close(); }
  else window.print();
}
async function printOrder(id) {
  if (btMode()) {
    return printViaBluetooth(async () => {
      // use the order already on screen so printing starts instantly (no server round trip)
      const local = (state.orders || []).find(o => String(o.id) === String(id) && Array.isArray(o.items));
      await DKBT.printOrder(local || await api('/api/print/' + id), state.settings);
    });
  }
  const win = openPrintShell();
  try { const o = await api('/api/print/' + id); await printOrderInto(o, win); }
  catch (err) { if (win) win.close(); alert(err.message || 'Could not print order'); }
}

function batchReceiptHtml(orders) {
  const s = state.settings || {};
  const sections = orders.map((o, idx) => `<section class="ticket"><div class="center biz">${esc(bizName(s))}</div><div class="center">${licenseHtml(s)}<br>KITCHEN ORDER</div><div class="line"></div><div class="row"><span>Order</span><span>#${o.order_number}</span></div><div class="row"><span>Date</span><span>${fmtDate(o.created_at)}</span></div><div class="row"><span>Time</span><span>${fmtTime(o.created_at)}</span></div><div class="line"></div>${customerBlockHtml(o)}${o.items.map(i=>`<div class="row"><span>${i.quantity}× ${esc(i.item_name)}</span><span>${money(i.unit_price*i.quantity)}</span></div>`).join('')}<div class="line"></div><div class="row"><span>Subtotal</span><span>${money(o.subtotal)}</span></div><div class="row"><span>Delivery</span><span>${money(o.delivery_fee)}</span></div><div class="row"><span>Tax</span><span>${money(o.tax)}</span></div><div class="row total"><span>TOTAL</span><span>${money(o.total)}</span></div><div class="line"></div><div>Type: ${esc(o.order_type)}</div><div>Payment: ${esc(paymentMethod(o))}</div>${o.special_instructions?`<div class="line"></div><div class="note">Note: ${esc(o.special_instructions)}</div>`:''}<div class="thanks">THANK YOU</div></section>`).join('');
  return `<html><head><title>${esc(bizName(s))} - Print all orders</title><style>${ticketPrintCss()}.ticket{page-break-after:always;break-after:page}.ticket:last-child{page-break-after:auto;break-after:auto}</style></head><body>${sections}<script>window.onload=()=>setTimeout(()=>window.print(),250)</script></body></html>`;
}

async function printAllOrders() {
  const shown = filteredOrders();
  if (!shown.length) return alert('There are no orders to print with the current filters.');
  if (btMode()) {
    return printViaBluetooth(async () => {
      if (shown.length > 8 && !confirm(`Print ${shown.length} tickets over Bluetooth? This can take a minute.`)) return false;
      await DKBT.printOrders(shown, state.settings);
    }, `Sent ${shown.length} ticket${shown.length === 1 ? '' : 's'} to ${DKBT.name()}`);
  }
  const ids = shown.map(o => o.id);
  const s = state.settings || {};
  if ((s.printer_mode === 'windows' || s.printer_mode === 'serial') && s.printer_target) {
    try {
      await api('/api/printers/print-batch', {method:'POST', body:JSON.stringify({order_ids:ids, mode:s.printer_mode, target:s.printer_target})});
      return;
    } catch (err) {
      alert(err.message || 'Could not print the order batch.');
      return;
    }
  }
  const win = openPrintShell();
  try {
    const orders = await Promise.all(ids.map(id => api('/api/print/' + id)));
    if (!win) return alert('Allow popups to print all orders.');
    win.document.open();
    win.document.write(batchReceiptHtml(orders));
    win.document.close();
  } catch (err) {
    if (win) win.close();
    alert(err.message || 'Could not prepare the order batch.');
  }
}
async function testHardwarePrint() {
  const s = state.settings || {};
  if (!s.printer_target || !['windows','serial'].includes(s.printer_mode)) return testPrint();
  try { const result = await api('/api/printers/test', { method:'POST' }); alert(result.message || 'Test print sent.'); }
  catch (err) { alert(err.message || 'Printer test failed'); }
}
function testPrint() {
  const win = openPrintShell(); if (!win) return alert('Allow popups to use browser print.');
  const name = bizName();
  win.document.open(); win.document.write(`<html><head><title>Printer Test</title><style>@page{size:58mm auto;margin:0}body{width:58mm;padding:10px;font-family:monospace;text-align:center}hr{border:0;border-top:1px dashed #000}</style></head><body><h3>${esc(name)}</h3><p>THERMAL PRINTER TEST</p><hr><p>58mm PAPER</p><p>Customer: Test User</p><p>Phone: 98XXXXXXXX</p><hr><p>Printer connection OK</p><p>Thank you</p><script>window.onload=()=>setTimeout(()=>window.print(),250)</script></body></html>`); win.document.close();
}

function openMenuModal(item = null, presetCategory = '') {
  const currentCategory = item?.category || presetCategory || state.categories?.[0]?.name || '';
  const categoryOptions = (state.categories || []).map(c => `<option value="${esc(c.name)}" ${currentCategory===c.name?'selected':''}>${esc(c.name)}</option>`).join('');
  showModal(`<div class="modal-card"><div class="modal-head"><div><span class="muted">Menu item</span><h2>${item ? 'Edit dish' : 'Add dish'}</h2></div><button class="icon-btn" onclick="closeModal()">×</button></div>
    <p class="muted">Add the dish here, choose which category it belongs to, and it will appear under that category in New Order.</p>
    <form id="menu-form">
      <label>Item name<input name="name" required value="${esc(item?.name || '')}" placeholder="Paneer Lababdar"></label>
      <label>Category<select name="category" required>${categoryOptions || '<option value="">Create a category first</option>'}</select></label>
      <div class="form-grid"><label>Price (₹)<input name="price" type="number" min="1" step="1" required value="${item?.price || ''}" placeholder="229"></label><label class="switchline"><input name="available" type="checkbox" ${item?.available !== false ? 'checked' : ''}> Available for order</label></div>
      <div class="hint-box"><b>Tip:</b> You can move an existing dish to another category here. The New Order screen will immediately use the new structure.</div>
      <div class="modal-foot"><button class="btn" type="button" onclick="closeModal()">Cancel</button>${state.categories?.length ? '<button class="btn btn-primary">Save item</button>' : ''}</div>
    </form></div>`);
  const form = document.getElementById('menu-form');
  if (!form) return;
  form.onsubmit = async e => {
    e.preventDefault();
    const f = new FormData(e.target);
    const category = String(f.get('category') || '').trim();
    if (!category) return alert('Create a category before adding a dish.');
    try {
      const payload = { name:String(f.get('name')||'').trim(), category, price:Number(f.get('price')), available:f.get('available') === 'on' };
      await api(item ? '/api/menu-items/' + item.id : '/api/menu-items', { method:item ? 'PUT' : 'POST', body:JSON.stringify(payload) });
      closeModal(); await load();
    } catch (err) { alert(err.message || 'Could not save item'); }
  };
}

async function toggleMenu(id, available) { const i = state.menu.find(x => x.id === id); await api('/api/menu-items/' + id, { method:'PUT', body:JSON.stringify({name:i.name,category:i.category,price:i.price,available}) }); await load(); }
async function deleteMenu(id) { if (confirm('Delete this menu item?')) { await api('/api/menu-items/' + id, {method:'DELETE'}); await load(); } }

function setPrinterStep(step) { state.printerSetup.step = step; render(); if (step === 2) loadPrinters().catch(() => {}); }
async function pickPrinter(mode, target) { state.settings.printer_mode = mode; state.settings.printer_target = target; state.printerSetup.step = 3; state.printerSetup.test = null; render(); }
function syncPrinterTargets() { const mode = document.getElementById('p-mode')?.value; const target = document.getElementById('p-target'); if (!target) return; [...target.options].forEach(o => { o.hidden = o.value && o.dataset.mode && o.dataset.mode !== mode; }); const selected = [...target.options].find(o => !o.hidden && o.value === state.settings.printer_target); if (selected) target.value = selected.value; else if (mode === 'browser') target.value = ''; }
async function savePrinterConfig(goToTest = false) {
  const s = state.settings || {};
  const mode = document.getElementById('p-mode').value, target = document.getElementById('p-target').value, name = document.getElementById('p-name').value;
  const baud = Number(document.getElementById('p-baud')?.value || s.printer_baudrate || 9600);
  if (mode !== 'browser' && !target) return alert('Select a printer or COM port first.');
  state.settings = await api('/api/settings', {method:'PUT', body:JSON.stringify({...s, business_name: (s.business_name || storedBiz() || 'Demo Kitchen'), printer_mode:mode, printer_target:target, printer_name:name, printer_baudrate:baud})});
  state.printerSetup.step = goToTest ? 4 : 3;
  state.printerSetup.test = null;
  render();
  if (goToTest) setTimeout(() => testHardwarePrint(), 80);
}
async function testHardwarePrint() {
  const s = state.settings || {};
  if (!s.printer_target || !['windows','serial'].includes(s.printer_mode)) {
    state.printerSetup.test = {ok:true, message:'Browser print fallback selected. A 58mm print preview will open.'};
    render();
    testPrint();
    return;
  }
  try {
    const result = await api('/api/printers/test', { method:'POST' });
    state.printerSetup.test = {ok:true, message: result.message || 'Test print sent successfully.'};
  } catch (err) {
    state.printerSetup.test = {ok:false, message:err.message || 'Printer test failed.'};
  }
  state.printerSetup.step = 4;
  render();
}
function goToDashboardAfterPrinter() { state.printerSetup.test = null; nav('dashboard'); }

function fieldVal(id, fallback) {
  const el = document.getElementById(id);
  if (!el) return fallback;
  if (el.type === 'checkbox') return el.checked;
  return el.value;
}
async function saveSettings(e) {
  if (e && typeof e.preventDefault === 'function') e.preventDefault();
  const s = state.settings || {};
  const nameEl = document.getElementById('s-name');
  if (!nameEl) { alert('Open Settings to save business details.'); return false; }
  const business_name = String(nameEl.value || '').trim();
  if (!business_name) { alert('Please enter a business name.'); nameEl.focus(); return false; }
  const p = {
    business_name,
    address: String(fieldVal('s-address', s.address || '')),
    phone: String(fieldVal('s-phone', s.phone || '')),
    gst_number: String(fieldVal('s-gst', s.gst_number || '')),
    food_license: String(fieldVal('s-fssai', s.food_license || '')).trim(),
    printer_name: String(fieldVal('s-printer', s.printer_name || '58mm Mini Thermal Printer')),
    paper_size: String(fieldVal('s-paper', s.paper_size || '58mm')),
    auto_print: !!fieldVal('s-auto', !!s.auto_print),
    default_order_type: String(fieldVal('s-default', s.default_order_type || 'Pickup')),
    tax_percent: Number(fieldVal('s-tax', s.tax_percent || 0) || 0),
    delivery_fee: Number(fieldVal('s-fee', s.delivery_fee || 0) || 0),
    printer_mode: s.printer_mode || 'browser',
    printer_target: s.printer_target || '',
    printer_baudrate: Number(s.printer_baudrate || 9600)
  };
  const buttons = document.querySelectorAll('.settings-grid [type=submit]');
  buttons.forEach(b => { b.disabled = true; });
  try {
    const saved = await api('/api/settings', { method: 'PUT', body: JSON.stringify(p) });
    state.settings = { ...s, ...p, ...(saved || {}) };
    if (!String(state.settings.business_name || '').trim()) state.settings.business_name = business_name;
    rememberBiz(state.settings.business_name);
    nav('home');
    toast('Saved. Main Menu and prints now use “' + bizName() + '”.', 'ok');
  } catch (err) {
    buttons.forEach(b => { b.disabled = false; });
    alert(err.message || 'Could not save settings. Only the owner can change business settings.');
  }
  return false;
}
async function resetDemo() { if (confirm('Reset demo orders and inventory to the sample dataset?')) { await api('/api/demo/reset',{method:'POST'}); await load(); } }

// =====================================================================================
// Login, account, logins list (owner) — v24
// =====================================================================================
const isOwner = () => !state.user || state.user.role === 'owner';
const OLD_BACKEND_MSG = 'This server does not have the login system yet. Upload the new backend/app.py, redeploy, and try again.';
const loginProblem = err => err && err.status === 404 ? OLD_BACKEND_MSG
  : err && err.name === 'TypeError' ? 'Cannot reach the server. If it was asleep, wait a minute and try again.'
  : (err && err.message) || 'Could not sign in';
function sessionExpired() {
  if (!state.user && state.page === 'login') return;
  state.user = null; state.page = 'login'; state.loginError = 'Your session ended. Please sign in again.';
  closeModal(); render();
}
function splashHtml() { const name = bizName(); return `<div class="splash"><div class="mark">${esc(bizInitials(name))}</div><b>Starting up…</b><small>The first load can take up to a minute.</small></div>`; }
function loginPage() {
  const biz = bizName();
  return `<div class="login-wrap"><form class="login-card" onsubmit="return doLogin(event)"><div class="mark">${esc(bizInitials(biz))}</div><h1>${esc(biz)}</h1><p>Sign in to continue</p>
    ${state.loginError ? `<div class="login-error" role="alert">${esc(state.loginError)}</div>` : ''}
    <label>Username<input name="username" autocomplete="username" autocapitalize="none" autocorrect="off" spellcheck="false" required value="${esc(state.loginUser || '')}"></label>
    <label>Password<span class="pw-row"><input name="password" type="password" autocomplete="current-password" required><button type="button" class="pw-toggle" onclick="togglePw(this)">Show</button></span></label>
    <button class="btn-login" type="submit">Sign in</button></form></div>`;
}
function focusLogin() {
  setTimeout(() => { const f = document.querySelector('.login-card'); if (!f) return; const el = state.loginUser ? f.elements.password : f.elements.username; if (el) el.focus(); }, 30);
}
function togglePw(btn) { const input = btn.previousElementSibling; const show = input.type === 'password'; input.type = show ? 'text' : 'password'; btn.textContent = show ? 'Hide' : 'Show'; }
async function doLogin(ev) {
  ev.preventDefault();
  const form = ev.target, f = new FormData(form), btn = form.querySelector('.btn-login');
  const username = String(f.get('username') || '').trim();
  btn.disabled = true; btn.textContent = 'Signing in…';
  try {
    state.user = await api('/api/login', { method: 'POST', body: JSON.stringify({ username, password: String(f.get('password') || '') }) });
    state.loginError = ''; state.loginUser = ''; state.page = 'home'; render();
    await afterLogin();
  } catch (err) {
    state.user = null; state.page = 'login'; state.loginUser = username; state.loginError = loginProblem(err); render();
  }
  return false;
}
async function doLogout() {
  try { await api('/api/logout', { method: 'POST' }); } catch (e) { /* already logged out */ }
  state.user = null; state.page = 'login'; state.loginError = ''; state.loginUser = '';
  state.orders = []; state.reports = null; state.users = []; state.invItems = []; state.selectedOrder = null;
  closeModal(); render();
}
function defaultPasswordBanner() {
  return state.user && state.user.must_change_password ? `<button class="pw-banner" onclick="nav('account')">⚠️ You are using the default password. Tap here to choose your own.</button>` : '';
}
function accountBarHtml() {
  const u = state.user || {};
  const who = esc(u.name || u.username || '');
  return `<div class="account-bar"><div class="account-who"><span class="account-avatar">${esc(String(u.name || u.username || '?')[0]).toUpperCase()}</span><div><b>${who}</b><small>${u.role === 'owner' ? 'Owner' : 'Staff'}${u.auth_disabled ? ' · login is switched off' : ' · @' + esc(u.username)}</small></div></div>
    ${u.auth_disabled ? '' : `<div class="account-actions"><button class="btn" onclick="nav('account')">🔑 Change password</button><button class="btn" onclick="doLogout()">Log out</button></div>`}</div>`;
}
function ownerOnlyHtml() { return `<div class="panel"><h2>Owner only</h2><p>Settings and logins can only be changed by the owner.</p></div>`; }
function accountPage() {
  const u = state.user || {};
  return `<div class="account-page">
    <div class="panel"><h2>${esc(u.name || u.username || '')}</h2><p class="muted">${u.auth_disabled ? 'Login is switched off on this server.' : `Signed in as <b>@${esc(u.username)}</b> · ${u.role === 'owner' ? 'Owner' : 'Staff'}`}</p></div>
    ${u.auth_disabled ? '' : `<form class="panel" id="pw-form" onsubmit="return changePassword(event)"><h2>Change password</h2>
      ${u.must_change_password ? '<div class="hint-box">You are using the default password. Please choose your own.</div>' : ''}
      <label>Current password<input name="current_password" type="password" autocomplete="current-password" required></label>
      <label>New password <small class="opt">(at least 6 characters)</small><input name="new_password" type="password" autocomplete="new-password" minlength="6" required></label>
      <label>Repeat new password<input name="repeat" type="password" autocomplete="new-password" minlength="6" required></label>
      <label class="switchline"><input type="checkbox" onchange="togglePwFields(this)"> Show passwords</label>
      <button class="btn btn-primary btn-wide" type="submit">Save new password</button></form>
    <button class="btn btn-wide" onclick="doLogout()">Log out</button>`}</div>`;
}
function togglePwFields(cb) { document.querySelectorAll('#pw-form input[type=password], #pw-form input[data-pw]').forEach(i => { i.type = cb.checked ? 'text' : 'password'; i.dataset.pw = '1'; }); }
async function changePassword(ev) {
  ev.preventDefault();
  const form = ev.target, f = new FormData(form);
  const current = String(f.get('current_password') || ''), next = String(f.get('new_password') || ''), again = String(f.get('repeat') || '');
  if (next.length < 6) { alert('The new password must be at least 6 characters.'); return false; }
  if (next !== again) { alert('The two new passwords are not the same.'); return false; }
  try {
    await api('/api/me/password', { method: 'POST', body: JSON.stringify({ current_password: current, new_password: next }) });
    form.reset();
    if (state.user) state.user.must_change_password = false;
    render(); toast('Password changed. Use the new one next time.', 'ok');
  } catch (err) { alert(err.message || 'Could not change the password'); }
  return false;
}
async function loadUsers() { state.users = await api('/api/users'); }
function usersPanelHtml() {
  const me = state.user || {};
  const rows = (state.users || []).map(u => `<div class="user-row"><div><b>${esc(u.name || u.username)}</b><small>@${esc(u.username)} · ${u.role === 'owner' ? 'Owner' : 'Staff'}${u.active ? '' : ' · switched off'}${u.id === me.id ? ' · you' : ''}</small></div>
    <div class="actions"><button class="btn btn-small" type="button" onclick="resetUserPassword(${u.id})">Reset password</button>${u.id === me.id ? '' : `<button class="btn btn-small" type="button" onclick="toggleUserActive(${u.id})">${u.active ? 'Switch off' : 'Switch on'}</button><button class="btn btn-small btn-danger" type="button" onclick="removeUser(${u.id})">Remove</button>`}</div></div>`).join('');
  return `<div class="panel users-panel"><div class="section-head tight"><div><h2>Logins</h2><p>Everyone who can sign in. Owners can also change settings and logins. Staff can do everything else.</p></div><button class="btn btn-primary" type="button" onclick="openUserModal()">＋ Add login</button></div>${rows || '<div class="empty">Loading…</div>'}</div>`;
}
function openUserModal() {
  showModal(`<div class="modal-card"><div class="modal-head"><div><span class="muted">Logins</span><h2>Add login</h2></div><button class="icon-btn" onclick="closeModal()">×</button></div>
    <form id="user-form"><label>Name<input name="name" placeholder="e.g. Mom" required></label>
    <label>Username<input name="username" autocapitalize="none" autocomplete="off" spellcheck="false" required placeholder="letters or numbers, e.g. mom"></label>
    <label>Password <small class="opt">(at least 6 characters)</small><input name="password" type="text" autocomplete="off" required minlength="6"></label>
    <label>Role<select name="role"><option value="staff">Staff: takes orders, prints, everyday work</option><option value="owner">Owner: can also change settings and logins</option></select></label>
    <div class="modal-foot"><button type="button" class="btn" onclick="closeModal()">Cancel</button><button class="btn btn-primary" type="submit">Add login</button></div></form></div>`);
  document.getElementById('user-form').onsubmit = async e => {
    e.preventDefault();
    const f = new FormData(e.target), btn = e.target.querySelector('[type=submit]'); btn.disabled = true;
    try {
      await api('/api/users', { method: 'POST', body: JSON.stringify({ name: String(f.get('name') || '').trim(), username: String(f.get('username') || '').trim(), password: String(f.get('password') || ''), role: String(f.get('role') || 'staff') }) });
      await loadUsers(); closeModal(); render(); toast('Login added', 'ok');
    } catch (err) { btn.disabled = false; alert(err.message || 'Could not add the login'); }
  };
}
async function resetUserPassword(id) {
  const u = (state.users || []).find(x => x.id === id); if (!u) return;
  const pw = prompt('New password for ' + (u.name || u.username) + ' (at least 6 characters):');
  if (pw === null) return;
  try { await api('/api/users/' + id, { method: 'PUT', body: JSON.stringify({ password: pw }) }); toast('Password changed for ' + (u.name || u.username), 'ok'); }
  catch (err) { alert(err.message || 'Could not change the password'); }
}
async function toggleUserActive(id) {
  const u = (state.users || []).find(x => x.id === id); if (!u) return;
  try { await api('/api/users/' + id, { method: 'PUT', body: JSON.stringify({ active: !u.active }) }); await loadUsers(); render(); }
  catch (err) { alert(err.message || 'Could not update the login'); }
}
async function removeUser(id) {
  const u = (state.users || []).find(x => x.id === id); if (!u) return;
  if (!confirm('Remove the login for ' + (u.name || u.username) + '?\n\nThey will not be able to sign in any more.')) return;
  try { await api('/api/users/' + id, { method: 'DELETE' }); await loadUsers(); render(); toast('Login removed', 'ok'); }
  catch (err) { alert(err.message || 'Could not remove the login'); }
}
async function afterLogin() {
  try { await load(); }
  catch (e) {
    if (!state.user) return; // the session ended meanwhile, the sign-in screen is already showing
    app.innerHTML = `<div style="padding:40px;font-family:system-ui"><h1>Could not start Demo Kitchen</h1><p>${esc(e.message)}</p></div>`;
  }
}
async function start() {
  render(); // shows the start-up screen while we check the login
  try { state.user = await api('/api/me'); } catch (e) { state.user = null; if (e && (e.status === 404 || e.name === 'TypeError')) state.loginError = loginProblem(e); }
  if (!state.user || !state.user.username) { state.user = null; state.page = 'login'; render(); return; }
  state.page = 'home'; render();
  await afterLogin();
}

// =====================================================================================
// Inventory: big Add Purchase, one-tap stock buttons, "where the money goes" — v24
// =====================================================================================
const INV_ICONS = { vegetables: '🥕', veggies: '🥕', dairy: '🥛', bakery: '🍞', spices: '🌶️', grocery: '🛒', packaging: '📦', gas: '🔥', beverages: '🥤', meat: '🍗', fruits: '🍎', 'raw material': '🧺' };
const INV_CATEGORIES = ['Vegetables', 'Dairy', 'Bakery', 'Spices', 'Grocery', 'Packaging', 'Gas', 'Beverages', 'Other'];
const INV_UNITS = ['kg', 'g', 'litre', 'ml', 'pcs', 'box', 'packet'];
const invIcon = cat => INV_ICONS[String(cat || '').trim().toLowerCase()] || '📦';
const todayIso = () => { const d = new Date(); return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0'); };
function parseDay(s) { const d = new Date(String(s || '').slice(0, 10) + 'T00:00:00'); return isNaN(d) ? null : d; }
function daysAgoText(s) {
  const d = parseDay(s); if (!d) return '';
  const t = new Date(); t.setHours(0, 0, 0, 0);
  const n = Math.round((t - d) / 86400000);
  return n <= 0 ? 'today' : n === 1 ? 'yesterday' : n + ' days ago';
}
function shortDay(s) { const d = parseDay(s); return d ? d.toLocaleDateString('en-IN', { day: 'numeric', month: 'short' }) : esc(s); }
async function loadInvItems() { try { state.invItems = await api('/api/inventory/items'); } catch (e) { state.invItems = state.invItems || []; } }
function mergedSpend(inv) {
  const map = new Map();
  ((inv && inv.by_item) || []).forEach(x => {
    const name = String(x.item_name || '').trim(), key = name.toLowerCase();
    const cur = map.get(key) || { item_name: name, total: 0 };
    cur.total += Number(x.total_cost || 0); map.set(key, cur);
  });
  return [...map.values()].sort((a, b) => b.total - a.total);
}
function allTimeSpend() {
  const map = new Map();
  (state.invItems || []).forEach(x => {
    const key = String(x.item_name || '').trim().toLowerCase();
    const cur = map.get(key) || { item_name: String(x.item_name || '').trim(), total: 0 };
    cur.total += Number(x.all_time_total || 0); map.set(key, cur);
  });
  return [...map.values()].sort((a, b) => b.total - a.total);
}
function rankingHtml() {
  const inv = state.inventory || {}, monthly = state.invView !== 'all';
  const rows = monthly ? mergedSpend(inv) : allTimeSpend();
  const grand = rows.reduce((a, r) => a + r.total, 0), max = rows[0] ? rows[0].total : 0;
  return `<div class="section-head tight"><div><h2>Where the money goes</h2><p>Items ranked by how much was spent on them.</p></div><div class="seg"><button class="${monthly ? 'on' : ''}" onclick="setInvView('month')">${labelMonth(inv.month)}</button><button class="${monthly ? '' : 'on'}" onclick="setInvView('all')">All time</button></div></div>
    ${rows.map((r, i) => `<div class="rank-row ${i === 0 ? 'top' : ''}"><span class="rank-no">${i + 1}</span><div class="rank-main"><div class="rank-top"><b>${esc(r.item_name)}</b><span>${money(r.total)} <small>${grand ? Math.round(r.total / grand * 100) : 0}%</small></span></div><div class="bar"><i style="width:${max ? Math.max(3, r.total / max * 100) : 0}%"></i></div></div></div>`).join('') || '<div class="empty">No spending yet for this period.</div>'}`;
}
function setInvView(v) { state.invView = v; const el = document.getElementById('inv-ranking'); if (el) el.innerHTML = rankingHtml(); else render(); }
function invHistoryHtml() {
  const list = state.invPurchases || (state.inventory && state.inventory.purchases) || [];
  return list.map(p => `<div class="inv-hist-row"><div class="inv-hist-date">${shortDay(p.purchase_date)}</div><div class="inv-hist-main"><b>${esc(p.item_name)}</b><small>${esc(p.category)}${p.unit && p.unit !== 'batch' ? ' · ' + num(p.quantity) + ' ' + esc(p.unit) : ''}${p.notes ? ' · ' + esc(p.notes) : ''}</small></div><div class="inv-hist-amt">${money(p.total_cost)}</div><div class="actions inv-hist-actions"><button class="btn btn-small" onclick="editInventory(${p.id})">Edit</button><button class="btn btn-small btn-danger" onclick="deleteInventory(${p.id})">Delete</button></div></div>`).join('') || '<div class="empty">No purchases in this month yet.</div>';
}
function inventoryPage() {
  const inv = state.inventory || { purchases: [], by_item: [], total_spend: 0 };
  const top = mergedSpend(inv)[0];
  const groups = {};
  (state.invItems || []).forEach(it => { (groups[it.category] = groups[it.category] || []).push(it); });
  const stock = Object.keys(groups).sort((a, b) => a.localeCompare(b)).map(cat => `<div class="stock-group"><h3>${invIcon(cat)} ${esc(cat)}</h3><div class="stock-grid">${groups[cat].map(it => `<button class="stock-tile" onclick='quickStock(${jsArg(it.item_name)},${jsArg(it.category)})'><b>${esc(it.item_name)}</b><span class="stock-last">${it.last_date ? 'Last: ' + daysAgoText(it.last_date) + ' · ' + money(it.last_total) : 'Not bought yet'}</span><span class="stock-add">＋ Add stock</span></button>`).join('')}</div></div>`).join('');
  return `<div class="inv-top"><button class="big-add-purchase" onclick="openInventoryModal()"><span>＋</span> Add Purchase</button><div class="kitchen-month">${monthPicker()}</div></div>
    <div class="today-stats"><div class="tstat"><span>${labelMonth(inv.month)} Spend</span><b>${money(inv.total_spend)}</b></div><div class="tstat"><span>Purchases</span><b>${(inv.purchases || []).length}</b></div><div class="tstat warn"><span>Most Spent On</span><b class="small">${esc(top ? top.item_name : '—')}</b></div><div class="tstat"><span>Items Tracked</span><b>${(state.invItems || []).length}</b></div></div>
    <section class="panel"><div class="section-head tight"><div><h2>Quick stock</h2><p>Tap an item, type the amount you paid, done. Buying it again later? Just tap it again.</p></div></div>${stock || '<div class="empty">No items yet. Tap <b>＋ Add Purchase</b> to add the first one.</div>'}<button class="stock-new" onclick="openInventoryModal()">＋ Add a new item</button></section>
    <section class="panel" id="inv-ranking">${rankingHtml()}</section>
    <section class="panel"><div class="section-head tight"><div><h2>Purchases in ${labelMonth(inv.month)}</h2><p>Every purchase adds to the month's total.</p></div><input class="compact-search" placeholder="Search item" value="${esc(state.inventorySearch)}" oninput="invSearch(this.value)"></div><div id="inv-history">${invHistoryHtml()}</div></section>`;
}
let invSearchTimer = null;
function invSearch(v) {
  state.inventorySearch = v; clearTimeout(invSearchTimer);
  invSearchTimer = setTimeout(async () => {
    try {
      if (!v) state.invPurchases = null;
      else state.invPurchases = (await api('/api/inventory?month=' + encodeURIComponent(state.selectedMonth) + '&q=' + encodeURIComponent(v))).purchases;
    } catch (e) { return; }
    const el = document.getElementById('inv-history'); if (el) el.innerHTML = invHistoryHtml();
  }, 250);
}
function purchasePayload(f, name, category) {
  const qty = Number(f.get('quantity') || 0);
  return { item_name: name, category, amount: Number(f.get('amount')), quantity: qty > 0 ? qty : 1, unit: qty > 0 ? String(f.get('unit') || 'kg') : 'batch', purchase_date: String(f.get('purchase_date') || todayIso()), notes: String(f.get('notes') || '').trim() };
}
async function savePurchase(payload, id) {
  await api(id ? '/api/inventory/' + id : '/api/inventory', { method: id ? 'PUT' : 'POST', body: JSON.stringify(payload) });
  closeModal();
  state.selectedMonth = payload.purchase_date.slice(0, 7); state.inventory.month = state.selectedMonth; state.inventorySearch = ''; state.invPurchases = null;
  await Promise.all([loadPeriodData(), loadInvItems()]);
  render();
  toast((id ? 'Updated: ' : 'Saved: ') + payload.item_name + ' ' + money(payload.amount), 'ok');
}
const unitOptions = sel => INV_UNITS.map(u => `<option ${sel === u ? 'selected' : ''}>${u}</option>`).join('');
function quickStock(name, category) {
  const it = (state.invItems || []).find(x => x.item_name === name && x.category === category) || { item_name: name, category };
  showModal(`<div class="modal-card"><div class="modal-head"><div><span class="muted">${invIcon(category)} ${esc(category)} · add stock</span><h2>${esc(name)}</h2></div><button class="icon-btn" onclick="closeModal()">×</button></div>
    <form id="stock-form"><label>Amount paid (₹)<input class="amount-input" name="amount" type="number" inputmode="decimal" min="0" step="any" required placeholder="e.g. 2000"></label>
    ${it.last_total ? `<button type="button" class="chip" onclick="document.querySelector('#stock-form [name=amount]').value=${Number(it.last_total)}">Same as last time: ${money(it.last_total)}</button>` : ''}
    <div class="form-grid"><label>Date<input name="purchase_date" type="date" value="${todayIso()}"></label><label>Note <small class="opt">(optional)</small><input name="notes" placeholder="Supplier etc."></label></div>
    <details class="more"><summary>Add quantity (optional)</summary><div class="form-grid"><label>Quantity<input name="quantity" type="number" inputmode="decimal" step="any" min="0"></label><label>Unit<select name="unit">${unitOptions(it.last_unit && it.last_unit !== 'batch' ? it.last_unit : 'kg')}</select></label></div></details>
    <div class="modal-foot"><button type="button" class="btn" onclick="closeModal()">Cancel</button><button class="btn btn-primary btn-big" type="submit">Save</button></div></form></div>`);
  const form = document.getElementById('stock-form');
  form.onsubmit = async e => {
    e.preventDefault(); const btn = form.querySelector('[type=submit]'); btn.disabled = true;
    try { await savePurchase(purchasePayload(new FormData(form), name, category)); }
    catch (err) { btn.disabled = false; alert(err.message || 'Could not save'); }
  };
  setTimeout(() => { const a = form.elements.amount; if (a) a.focus(); }, 60);
}
function openInventoryModal(purchase = null) {
  const items = state.invItems || [];
  const names = [...new Set(items.map(i => i.item_name))];
  const cats = [...new Set([...INV_CATEGORIES, ...items.map(i => i.category)])];
  const hasQty = !!(purchase && purchase.unit && purchase.unit !== 'batch');
  showModal(`<div class="modal-card"><div class="modal-head"><div><span class="muted">Inventory</span><h2>${purchase ? 'Edit purchase' : 'Add purchase'}</h2></div><button class="icon-btn" onclick="closeModal()">×</button></div>
    <form id="inventory-form"><label>What did you buy?<input name="item_name" list="inv-names" required autocomplete="off" value="${esc(purchase ? purchase.item_name : '')}" placeholder="e.g. Paneer"></label><datalist id="inv-names">${names.map(n => `<option value="${esc(n)}">`).join('')}</datalist>
    <label>Type <small class="opt">(category)</small><input name="category" list="inv-cats" autocomplete="off" value="${esc(purchase ? purchase.category : 'Vegetables')}"></label><datalist id="inv-cats">${cats.map(c => `<option value="${esc(c)}">`).join('')}</datalist>
    <label>Amount paid (₹)<input class="amount-input" name="amount" type="number" inputmode="decimal" min="0" step="any" required value="${purchase ? purchase.total_cost : ''}" placeholder="e.g. 2000"></label>
    <div class="form-grid"><label>Date<input name="purchase_date" type="date" value="${esc(purchase ? String(purchase.purchase_date).slice(0, 10) : todayIso())}"></label><label>Note <small class="opt">(optional)</small><input name="notes" value="${esc(purchase ? purchase.notes || '' : '')}" placeholder="Supplier etc."></label></div>
    <details class="more" ${hasQty ? 'open' : ''}><summary>Add quantity (optional)</summary><div class="form-grid"><label>Quantity<input name="quantity" type="number" inputmode="decimal" step="any" min="0" value="${hasQty ? purchase.quantity : ''}"></label><label>Unit<select name="unit">${unitOptions(hasQty ? purchase.unit : 'kg')}</select></label></div></details>
    <div class="modal-foot"><button type="button" class="btn" onclick="closeModal()">Cancel</button><button class="btn btn-primary btn-big" type="submit">${purchase ? 'Save changes' : 'Save'}</button></div></form></div>`);
  const form = document.getElementById('inventory-form');
  let catTouched = !!purchase;
  form.elements.category.addEventListener('input', () => { catTouched = true; });
  form.elements.item_name.addEventListener('input', () => { // known item -> fill its type automatically
    const hit = items.find(i => i.item_name.toLowerCase() === form.elements.item_name.value.trim().toLowerCase());
    if (hit && !catTouched) form.elements.category.value = hit.category;
  });
  form.onsubmit = async e => {
    e.preventDefault(); const btn = form.querySelector('[type=submit]'); btn.disabled = true;
    const f = new FormData(form), name = String(f.get('item_name') || '').trim(), cat = String(f.get('category') || '').trim() || 'Other';
    if (!name) { btn.disabled = false; return alert('Please type what you bought.'); }
    try { await savePurchase(purchasePayload(f, name, cat), purchase ? purchase.id : null); }
    catch (err) { btn.disabled = false; alert(err.message || 'Could not save the purchase'); }
  };
  setTimeout(() => { const el = purchase ? form.elements.amount : form.elements.item_name; if (el) el.focus(); }, 60);
}
async function editInventory(id) { const list = state.invPurchases || (state.inventory && state.inventory.purchases) || []; const p = list.find(x => x.id === id); if (p) openInventoryModal(p); }
async function deleteInventory(id) {
  if (!confirm('Delete this purchase record?')) return;
  try {
    await api('/api/inventory/' + id, { method: 'DELETE' });
    state.inventorySearch = ''; state.invPurchases = null;
    await Promise.all([loadPeriodData(), loadInvItems()]);
    render(); toast('Purchase deleted', 'ok');
  } catch (err) { alert(err.message || 'Could not delete'); }
}


window.nav=nav; window.changeMonth=changeMonth; window.openNewOrder=openNewOrder; window.selectOrderCategory=selectOrderCategory; window.reviewNewOrder=reviewNewOrder; window.backToNewOrderEdit=backToNewOrderEdit; window.adjustReviewQty=adjustReviewQty; window.removeReviewItem=removeReviewItem; window.confirmReviewedOrder=confirmReviewedOrder; window.reorderCategory=reorderCategory; window.changeOrderQty=changeOrderQty; window.showOrder=showOrder; window.editOrder=editOrder; window.printOrder=printOrder; window.printAllOrders=printAllOrders; window.moveOrder=moveOrder; window.filterOrders=filterOrders; window.openMenuModal=openMenuModal; window.openCategoryModal=openCategoryModal; window.deleteCategory=deleteCategory; window.toggleMenu=toggleMenu; window.deleteMenu=deleteMenu; window.openInventoryModal=openInventoryModal; window.editInventory=editInventory; window.deleteInventory=deleteInventory; window.saveSettings=saveSettings; window.previewBizName=previewBizName; window.loadPrinters=loadPrinters; window.pickPrinter=pickPrinter; window.savePrinterConfig=savePrinterConfig; window.setPrinterStep=setPrinterStep; window.syncPrinterTargets=syncPrinterTargets; window.testHardwarePrint=testHardwarePrint; window.goToDashboardAfterPrinter=goToDashboardAfterPrinter; window.testPrint=testPrint; window.resetDemo=resetDemo; window.closeModal=closeModal; window.loadInventory=loadInventory; window.setPrinterTab=setPrinterTab; window.btConnectUi=btConnectUi; window.btTestUi=btTestUi; window.btDisconnectUi=btDisconnectUi; window.copyAppLink=copyAppLink; window.printerChipClick=printerChipClick;

DKBT.onChange(refreshBtUi);
start();
