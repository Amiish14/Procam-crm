// Runs the Copilot panel's script against a stub DOM, so a runtime error
// in a flow the browser exercises — open, context detection, ask with the
// stream refused and the JSON fallback, render, New, setContext — fails
// the build instead of silently breaking the panel on every screen.
// Driven by tests/test_copilot_panel.py; needs only node.
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
const js = src.slice(src.indexOf('<script>') + 8, src.lastIndexOf('</script>'))
  .replace('{{ url_prefix }}', '/CRM');

function el(id) {
  return {
    id, innerHTML: '', textContent: '', hidden: true, value: '',
    style: {}, dataset: {}, disabled: false,
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    focus() {}, appendChild(c) { this.children = (this.children || []).concat(c); },
    insertBefore() {}, addEventListener() {},
    querySelector(sel) { return sel === '.pai-a' ? (this._a = this._a || el('a')) : null; },
    querySelectorAll(sel) {
      // hand back fake buttons for the follow-up chips so their handler runs
      if (sel === '[data-next]' && this.nextButtons) return this.nextButtons;
      return [];
    },
    get firstChild() { return { nextSibling: null }; },
  };
}
const els = {};
const calls = [];
global.window = global;
global.requestAnimationFrame = (f) => f();
global.location = { pathname: '/CRM/companies/12', search: '', hash: '', href: '' };
global.document = {
  cookie: 'csrf_token=abc',
  getElementById(id) { return els[id] = els[id] || el(id); },
  querySelector() { return null; },
  createElement(tag) { return el(tag); },
  addEventListener() {},
};
const answer = {
  ok: true, intent: 'account_health', prose: 'Health <b>70</b>',
  confidence: 'high', how_answered: 'Answered with “Account health”',
  follow_ups: ['who knows <x>'], citations: [{ type: 'company', id: 12, label: 'Acme', source: 'note', date: '2026-01-01' }],
  clarification: null, conversation_id: 'c5.aaaaaaaaaaaaaaaaaaaaaaaa',
  context: { type: 'company', id: 12, label: 'Acme', visible: true },
  rows: [], columns: [], figures: { score: 70 }, sources: ['x'], notes: [], log_id: 5,
};
global.fetch = (url, opts) => {
  calls.push({ url, body: opts && opts.body });
  if (url.indexOf('/stream') >= 0) return Promise.reject(new Error('no stream'));
  if (url.indexOf('/brief') >= 0) return Promise.resolve({ json: () => ({ ok: true, brief: null, pinned: [] }) });
  if (url.indexOf('/suggestions') >= 0) return Promise.resolve({ json: () => ({ ok: true, suggestions: [], scope_note: 's', model: { available: false } }) });
  return Promise.resolve({ json: () => answer });
};
global.TextDecoder = function () {};
global.prompt = () => null;

eval(js);

(async () => {
  document.getElementById('paiFab').onclick();          // open → firstRun
  const ctx = document.getElementById('paiCtxWrap').innerHTML;
  if (ctx.indexOf('company #12') < 0) throw new Error('context not detected: ' + ctx);
  const input = document.getElementById('paiInput');
  input.value = 'summarise this';
  document.getElementById('paiSend').onclick();
  await new Promise((r) => setTimeout(r, 20));
  const ask = calls.find((c) => c.url.endsWith('/api/copilot/ask'));
  const payload = JSON.parse(ask.body);
  if (!payload.context || payload.context.type !== 'company' || payload.context.id !== 12)
    throw new Error('context not sent ' + ask.body);
  const turn = document.getElementById('paiBody').children
    .filter((c) => c.className === 'pai-turn').slice(-1)[0];
  const html = turn._a.innerHTML;
  for (const want of ['High confidence', 'Answered with', 'data-next="0"', 'data-cite="0"', '&lt;x&gt;', 'Health &lt;b&gt;70&lt;/b&gt;'])
    if (html.indexOf(want) < 0) throw new Error('missing ' + want + ' in ' + html);
  if (document.getElementById('paiCtxWrap').innerHTML.indexOf('Acme') < 0)
    throw new Error('context label not updated from the server');
  // second ask carries the conversation id
  input.value = 'what about last month';
  document.getElementById('paiSend').onclick();
  await new Promise((r) => setTimeout(r, 20));
  const second = calls.filter((c) => c.url.endsWith('/api/copilot/ask')).slice(-1)[0];
  if (JSON.parse(second.body).conversation_id !== answer.conversation_id)
    throw new Error('conversation id not sent back');
  // New forgets it
  document.getElementById('paiNew').onclick();
  input.value = 'my day';
  document.getElementById('paiSend').onclick();
  await new Promise((r) => setTimeout(r, 20));
  const third = calls.filter((c) => c.url.endsWith('/api/copilot/ask')).slice(-1)[0];
  // the answer stub hands the id straight back, so New must have cleared
  // it before this ask was built
  if (JSON.parse(third.body).conversation_id) throw new Error('New did not reset');
  // setContext hook
  window.ProcamAI.setContext({ type: 'lead', id: 7, label: 'Lead seven' });
  if (document.getElementById('paiCtxWrap').innerHTML.indexOf('Lead seven') < 0)
    throw new Error('setContext did not update the chip');
  console.log('PANEL_SMOKE_OK');
})().catch((e) => { console.error(e); process.exit(1); });
