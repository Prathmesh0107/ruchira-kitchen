// Run with:  node --test tests/frontend/
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const ROOT = path.join(__dirname, '..', '..');
const actions = require(path.join(ROOT, 'frontend', 'actions.js'));
const appSrc = fs.readFileSync(path.join(ROOT, 'frontend', 'app.js'), 'utf8');

// Load the real esc() and jsArg() out of app.js, without running the rest of the app.
const escLine = appSrc.split('\n').find(l => l.startsWith('const esc = '));
const jsArgLine = appSrc.split('\n').find(l => l.startsWith('function jsArg('));
const helpers = vm.runInNewContext(`${escLine}\n${jsArgLine}\n({esc, jsArg})`);

// What a browser does to an attribute value: decode the HTML entities esc() produces.
const htmlDecode = s => s.replace(/&quot;/g, '"').replace(/&#039;|&#39;/g, "'").replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&');

const HOSTILE = [
  `&quot;+alert(1)+&quot;`,                       // the payload from the audit
  `"+alert(1)+"`,
  `'); alert(1); ('`,
  `"); alert(document.cookie); ("`,
  `</script><img src=x onerror=alert(1)>`,
  `\\"); fetch('/api/orders/clear',{method:'POST'}); ("`,
  `नॉनव्हेज सूप 'हाफ' "फुल"`,                     // real Marathi text with quotes must survive
  `back\\slash and \u2028 line sep`,
  `a`.repeat(300),
];

test('esc() escapes every HTML-significant character', () => {
  assert.equal(helpers.esc(`<img src=x onerror="a('b')">&`), '&lt;img src=x onerror=&quot;a(&#039;b&#039;)&quot;&gt;&amp;');
  assert.equal(helpers.esc(null), '');
  assert.equal(helpers.esc(12), '12');
});

test('jsArg() output is safe inside single- AND double-quoted attributes', () => {
  for (const h of HOSTILE) {
    const enc = helpers.jsArg(h);
    assert.ok(!/["'<>]/.test(enc), 'raw quote/angle bracket leaked: ' + enc.slice(0, 60));
  }
});

test('a hostile name stays ONE inert string argument after browser decoding', () => {
  const registry = { openMenuModal: (a, b) => ({ a, b }) };
  for (const h of HOSTILE) {
    for (const quote of ['"', "'"]) {
      const attr = `openMenuModal(null, ${helpers.jsArg(h)})`;
      const decoded = htmlDecode(attr);   // exactly what getAttribute() returns
      const calls = actions.parse(decoded);
      assert.equal(calls.length, 1, 'payload created a second statement');
      assert.equal(calls[0].fn, 'openMenuModal');
      assert.equal(calls[0].args[1].v, h, 'argument was altered');
      assert.deepEqual(actions.run(calls, registry, {}, {}), undefined);
    }
  }
});

test('payloads that are NOT valid single calls are rejected, never executed', () => {
  let ran = false;
  const registry = { nav() { ran = true; }, ok() {} };
  for (const bad of [
    `alert(1)`, `nav('x');alert(1)`, `window.alert(1)`, `nav('x').then(1)`, `nav(alert(1))`, `nav(a+b)`,
    `nav('x')//`, `eval('1')`, `nav(this.parentNode)`, `nav(this.innerHTML)`, `constructor('x')`, `__proto__()`,
    `nav('unterminated`, `nav(1,)`, `nav(,)`, `nav('a' 'b')`, `nav(\`tpl\`)`, ``, `   `, `return`, `nav`, `nav('x') nav('y')`,
  ]) {
    assert.throws(() => actions.run(actions.parse(bad), registry, {}, {}), /data-on\* rejected/, 'accepted: ' + bad);
  }
  assert.equal(ran, false);
});

test('registry is an allow-list: prototype members are not callable', () => {
  assert.throws(() => actions.run(actions.parse('toString()'), {}, {}, {}), /not a registered action/);
  assert.throws(() => actions.run(actions.parse('constructor()'), {}, {}, {}), /not a registered action/);
});

test('supported forms from the real app parse and bind arguments correctly', () => {
  const seen = [];
  const reg = new Proxy({}, { has: () => true, get: (_, k) => (...a) => { seen.push([k, ...a]); return k === 'saveSettings' ? false : undefined; } });
  const r = {}; ['nav', 'showOrder', 'reorderCategory', 'moveOrder', 'togglePw', 'setOrderSearchText', 'saveSettings', 'toggleMenu', 'pickPrinter'].forEach(k => r[k] = reg[k]);
  const el = { value: 'typed', checked: true };
  actions.run(actions.parse(`nav('home')`), r, el, {});
  actions.run(actions.parse(`showOrder(42)`), r, el, {});
  actions.run(actions.parse(`reorderCategory(7,-1)`), r, el, {});
  actions.run(actions.parse(`moveOrder(9,'Ready')`), r, el, {});
  actions.run(actions.parse(`togglePw(this)`), r, el, {});
  actions.run(actions.parse(`setOrderSearchText(this.value)`), r, el, {});
  actions.run(actions.parse(`toggleMenu(3,false)`), r, el, {});
  actions.run(actions.parse(`pickPrinter("windows","My \\"Quoted\\" Printer")`), r, el, {});
  assert.deepEqual(seen, [['nav', 'home'], ['showOrder', 42], ['reorderCategory', 7, -1], ['moveOrder', 9, 'Ready'], ['togglePw', el],
    ['setOrderSearchText', 'typed'], ['toggleMenu', 3, false], ['pickPrinter', 'windows', 'My "Quoted" Printer']]);
  const rv = actions.run(actions.parse(`return saveSettings(event)`), r, el, { ev: 1 });
  assert.equal(rv, false);
});

// ---- coverage: every data-on* body that exists in app.js must be accepted by the parser ----
test('every data-on* handler in app.js parses and calls a registered action', () => {
  const bodyRe = /data-on(?:click|change|input|submit)\s*=\s*(?:"([^"]*)"|'([^']*)')/g;
  const registryNames = new Set(((appSrc.match(/const DK_ACTIONS = \{([^}]*)\}/) || [])[1] || '').split(',').map(s => s.trim()).filter(Boolean));
  assert.ok(registryNames.size > 40, 'DK_ACTIONS registry not found');
  let n = 0;
  for (const m of appSrc.matchAll(bodyRe)) {
    let body = m[1] !== undefined ? m[1] : m[2];
    // template placeholders: jsArg(...) -> a hostile string argument, anything else -> a number
    body = body.replace(/\$\{jsArg\([^}]*\)\}/g, () => helpers.jsArg(`x"');alert(1);('`))
               .replace(/\$\{[^}]*\}/g, '5');
    const calls = actions.parse(htmlDecode(body));
    for (const c of calls) assert.ok(registryNames.has(c.fn), `${c.fn} is used in a handler but missing from DK_ACTIONS`);
    n++;
  }
  assert.ok(n > 100, 'expected 100+ handlers, saw ' + n);
});

test('app.js has no inline event-handler attributes left (CSP-safe)', () => {
  const inline = appSrc.match(/[\s"'`]on(?:click|change|input|submit|load|error|keydown|keyup|focus|blur|mouseover)\s*=\s*["']/g) || [];
  assert.deepEqual(inline, []);
  assert.ok(!/<script[\s>]/i.test(appSrc.replace(/document\.createElement\('script'\)/g, '')), 'inline <script> text found in templates');
  assert.ok(!/JSON\.stringify\([a-z]+\)\.replace\(/.test(appSrc), 'serialised object in an attribute');
  assert.ok(!/javascript:/i.test(appSrc));
});

test('every function in DK_ACTIONS exists in app.js', () => {
  const names = ((appSrc.match(/const DK_ACTIONS = \{([^}]*)\}/) || [])[1] || '').split(',').map(s => s.trim()).filter(Boolean);
  for (const n of names) assert.ok(new RegExp(`(function\\s+${n}\\b|const\\s+${n}\\b)`).test(appSrc) || /^(btConnectUi|btTestUi|btDisconnectUi)$/.test(n), 'undefined action: ' + n);
});
