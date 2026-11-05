/*
 * actions.js: CSP-safe replacement for inline event handlers.
 *
 * The app's HTML used to contain things like  onclick="showOrder(12)".  Inline handlers are exactly what
 * a Content-Security-Policy has to forbid, and they were also the way a dish name could turn into code.
 * Now the markup carries  data-onclick="showOrder(12)"  (same text) and this file runs it:
 *
 *   - the text is TOKENISED, never evaluated (no eval / new Function / inline script),
 *   - only function names listed in the registry passed to install() can be called,
 *   - arguments can only be literals (JSON strings, numbers, true/false/null), `this`, `this.value`,
 *     `this.checked` or `event`. Anything else is rejected and logged.
 *
 * Because a string argument is a complete JSON literal, text typed by a user (dish names, categories...)
 * can never end the argument early or become a second call.
 */
(function (root, factory) {
  const api = factory();
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.DKActions = api;
})(typeof window !== 'undefined' ? window : globalThis, function () {
  'use strict';

  const IDENT = /[A-Za-z_$][A-Za-z0-9_$]*/y;
  const NUMBER = /-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?/y;

  function fail(msg) { throw new Error('data-on* rejected: ' + msg); }

  function readString(src, i) {
    const quote = src[i];
    let j = i + 1, out = '';
    while (j < src.length) {
      const ch = src[j];
      if (ch === '\\') {
        const next = src[j + 1];
        if (next === undefined) fail('dangling backslash');
        if (next === 'u') {
          const hex = src.slice(j + 2, j + 6);
          if (!/^[0-9a-fA-F]{4}$/.test(hex)) fail('bad \\u escape');
          out += String.fromCharCode(parseInt(hex, 16)); j += 6; continue;
        }
        const map = { n: '\n', t: '\t', r: '\r', b: '\b', f: '\f', '/': '/', '\\': '\\', '"': '"', "'": "'" };
        if (!(next in map)) fail('bad escape');
        out += map[next]; j += 2; continue;
      }
      if (ch === quote) return { value: out, end: j + 1 };
      if (ch === '\n' || ch === '\r') fail('newline inside string');
      out += ch; j++;
    }
    return fail('unterminated string');
  }

  function skipWs(src, i) { while (i < src.length && /\s/.test(src[i])) i++; return i; }

  function readIdent(src, i) {
    IDENT.lastIndex = i;
    const m = IDENT.exec(src);
    return m ? { value: m[0], end: i + m[0].length } : null;
  }

  function readArg(src, i) {
    const ch = src[i];
    if (ch === '"' || ch === "'") { const r = readString(src, i); return { arg: { t: 'lit', v: r.value }, end: r.end }; }
    if (ch === '-' || (ch >= '0' && ch <= '9')) {
      NUMBER.lastIndex = i;
      const m = NUMBER.exec(src);
      if (!m) fail('bad number');
      return { arg: { t: 'lit', v: Number(m[0]) }, end: i + m[0].length };
    }
    const id = readIdent(src, i);
    if (!id) fail('unexpected character "' + ch + '"');
    const w = id.value;
    if (w === 'true') return { arg: { t: 'lit', v: true }, end: id.end };
    if (w === 'false') return { arg: { t: 'lit', v: false }, end: id.end };
    if (w === 'null') return { arg: { t: 'lit', v: null }, end: id.end };
    if (w === 'undefined') return { arg: { t: 'lit', v: undefined }, end: id.end };
    if (w === 'event') return { arg: { t: 'event' }, end: id.end };
    if (w === 'this') {
      if (src[id.end] === '.') {
        const prop = readIdent(src, id.end + 1);
        if (!prop || (prop.value !== 'value' && prop.value !== 'checked')) fail('only this.value / this.checked are allowed');
        return { arg: { t: 'prop', name: prop.value }, end: prop.end };
      }
      return { arg: { t: 'this' }, end: id.end };
    }
    return fail('identifier "' + w + '" is not allowed as an argument');
  }

  /** "return nav('home'); foo(1, "x")"  ->  [{fn:'nav', args:[...], ret:true}, {fn:'foo', ...}] */
  function parse(src) {
    src = String(src);
    if (src.length > 2000) fail('too long');
    const calls = [];
    let i = skipWs(src, 0);
    while (i < src.length) {
      let ret = false;
      let id = readIdent(src, i);
      if (!id) fail('expected a function name at ' + i);
      if (id.value === 'return') { ret = true; i = skipWs(src, id.end); id = readIdent(src, i); if (!id) fail('expected a function name after return'); }
      const fn = id.value;
      i = skipWs(src, id.end);
      if (src[i] !== '(') fail('expected "(" after ' + fn + (src[i] === '.' ? ' (member calls are not allowed)' : ''));
      i = skipWs(src, i + 1);
      const args = [];
      if (src[i] === ')') { i++; }
      else {
        for (;;) {
          const r = readArg(src, i);
          args.push(r.arg);
          i = skipWs(src, r.end);
          if (src[i] === ',') { i = skipWs(src, i + 1); continue; }
          if (src[i] === ')') { i++; break; }
          fail('expected "," or ")"');
        }
      }
      calls.push({ fn, args, ret });
      i = skipWs(src, i);
      if (i < src.length) {
        if (src[i] !== ';') fail('unexpected "' + src[i] + '"');
        i = skipWs(src, i + 1);
      }
    }
    if (!calls.length) fail('empty');
    return calls;
  }

  function run(calls, registry, el, ev) {
    // Check EVERY call before running ANY of them, so a rejected statement can never leave a half-run handler behind.
    for (const c of calls) {
      if (!Object.prototype.hasOwnProperty.call(registry, c.fn) || typeof registry[c.fn] !== 'function') fail('"' + c.fn + '" is not a registered action');
    }
    let result;
    for (const c of calls) {
      const args = c.args.map(a => a.t === 'lit' ? a.v : a.t === 'this' ? el : a.t === 'event' ? ev : el[a.name]);
      const r = registry[c.fn].apply(null, args);
      if (c.ret) result = r;
    }
    return result;
  }

  const cache = new Map();   // attribute text -> parsed calls
  function parsed(src) {
    let calls = cache.get(src);
    if (!calls) { calls = parse(src); if (cache.size > 500) cache.clear(); cache.set(src, calls); }
    return calls;
  }

  function install(registry, doc) {
    const TYPES = ['click', 'change', 'input', 'submit'];
    TYPES.forEach(type => {
      const attr = 'data-on' + type;
      doc.addEventListener(type, ev => {
        // inline handlers bubble: a handler on a parent still runs when a child is clicked
        for (let el = ev.target; el && el !== doc; el = el.parentNode) {
          if (!el.getAttribute || !el.hasAttribute(attr)) { continue; }
          try {
            const result = run(parsed(el.getAttribute(attr)), registry, el, ev);
            if (result === false) ev.preventDefault();
          } catch (err) { console.error(err); }
          if (type === 'submit') break;
        }
      }, false);
    });
  }

  return { parse, run, install };
});
