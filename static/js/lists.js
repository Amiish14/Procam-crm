/* ═══════════════════════════════════════════════════════════════════
   PROCAM CRM — what makes a table a card list.
   CRM-UI-01 Stage 3.

   TWO JOBS, AND NEITHER TOUCHES A RENDER FUNCTION

   1. Every `<td>` is stamped with `data-label`, taken from its own
      column's `<th>`. lists.css prints that label beside the value
      on a phone. Doing it here rather than in the forty render
      functions means a list added tomorrow gets the card view for
      free, and a column renamed stays right in both views.

   2. The filter bar's controls are MOVED into a bottom sheet on a
      phone and moved back on a desktop. Moved, not copied: the same
      elements keep their ids, so every `g('lSt').value` in the app
      still reads the same node and no filter needs rewiring.

   Rows are re-rendered constantly here — `innerHTML = …` on a tbody
   — so the stamping is driven by a MutationObserver rather than
   called once. The observer is the only way this survives a list
   that refreshes itself every thirty seconds.
   ═══════════════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  var PHONE = '(max-width: 767px)';

  function headersFor(table) {
    var row = table.querySelector('thead tr');
    if (!row) return [];
    return Array.prototype.map.call(row.children, function (th) {
      return (th.textContent || '').trim();
    });
  }

  function stamp(table) {
    var labels = headersFor(table);
    if (!labels.length) return;
    /* The card's heading is the first REAL column — not the row
       number and not the checkbox, which are desktop conveniences
       the card does not show. */
    var skip = {'#': 1, 'no': 1, 'no.': 1, 'sr': 1, 'sr.': 1, 's.no': 1};
    var first = labels.findIndex(function (l, i) {
      return l !== '' && !skip[l.toLowerCase()];
    });
    if (first < 0) first = labels.findIndex(function (l) {
      return l !== '';
    });
    Array.prototype.forEach.call(
      table.querySelectorAll('tbody tr'), function (tr) {
        Array.prototype.forEach.call(tr.children, function (td, i) {
          var label = labels[i] === undefined ? '' : labels[i];
          td.setAttribute('data-label', label);
          /* The first labelled cell is the card's heading; the last
             one, if it holds only controls, is its footer. */
          td.classList.toggle('is-title', i === first);
          var only = td.querySelector('button, a.btn, select, input');
          td.classList.toggle('is-actions',
            label === '' && i > first && !!only);
          var text = (td.textContent || '').trim();
          td.classList.toggle('is-empty',
            i !== first && !only && (text === '' || text === '—'));
        });
      });
  }

  function stampAll(root) {
    (root || document).querySelectorAll('.tw table').forEach(stamp);
  }

  /* A tbody whose rows are replaced is stamped again. */
  var watching = new WeakSet();
  function watch(table) {
    var body = table.querySelector('tbody');
    if (!body || watching.has(body)) return;
    watching.add(body);
    new MutationObserver(function () { stamp(table); })
      .observe(body, {childList: true});
  }

  function start() {
    document.querySelectorAll('.tw table').forEach(function (table) {
      stamp(table);
      watch(table);
    });
    buildSheets();
  }

  /* ── The filter sheet ─────────────────────────────────────────── */
  function sheet() {
    var el = document.getElementById('crmFilterSheet');
    if (el) return el;
    var scrim = document.createElement('div');
    scrim.id = 'crmFilterScrim';
    scrim.addEventListener('click', function () { openSheet(false); });
    document.body.appendChild(scrim);

    el = document.createElement('div');
    el.id = 'crmFilterSheet';
    el.setAttribute('role', 'dialog');
    el.setAttribute('aria-label', 'Filters');
    el.innerHTML =
      '<div class="sheet-grip"></div>' +
      '<div class="sheet-hd"><strong>Filters</strong>' +
      '<button class="btn btn-ghost" type="button" ' +
      'style="min-height:var(--tap-min);padding:0 16px">Done</button></div>' +
      '<div class="sheet-body"></div>' +
      '<div class="sheet-ft"><button class="btn btn-ghost" type="button">' +
      'Clear all</button></div>';
    el.querySelector('.sheet-hd button')
      .addEventListener('click', function () { openSheet(false); });
    el.querySelector('.sheet-ft button')
      .addEventListener('click', clearAll);
    document.body.appendChild(el);
    return el;
  }

  function openSheet(open) {
    var el = sheet();
    var scrim = document.getElementById('crmFilterScrim');
    el.classList.toggle('on', open !== false);
    if (scrim) scrim.classList.toggle('on', open !== false);
  }

  /* Where each control came from, so it goes back to the right bar
     when the window is widened. */
  var homes = new WeakMap();

  function controlsOf(bar) {
    return Array.prototype.filter.call(
      bar.children, function (el) {
        return el.classList.contains('fi')
          && !el.classList.contains('fi-srch');
      });
  }

  function buildSheets() {
    document.querySelectorAll('.fbar').forEach(function (bar) {
      if (bar.dataset.filters === '1') return;
      if (!controlsOf(bar).length) return;
      bar.dataset.filters = '1';
      var open = document.createElement('button');
      open.type = 'button';
      open.className = 'btn filters-open';
      open.innerHTML = '<svg viewBox="0 0 24 24" width="16" height="16" '
        + 'fill="none" stroke="currentColor" stroke-width="1.8" '
        + 'stroke-linecap="round"><path d="M3 5h18"/><path d="M6 12h12"/>'
        + '<path d="M10 19h4"/></svg>Filters<span class="count"></span>';
      open.addEventListener('click', function () { openSheet(true); });
      bar.appendChild(open);
      bar.addEventListener('change', function () { countOn(bar); });
      bar.addEventListener('input', function () { countOn(bar); });
    });
    place();
  }

  /* How many filters are actually on, on the button. A sheet that
     hides three active filters is how somebody spends ten minutes
     wondering where their leads went. */
  function countOn(bar) {
    var on = controlsOf(bar).filter(function (el) {
      return (el.value || '').trim() !== '';
    }).length;
    var btn = bar.querySelector('.filters-open .count');
    if (btn) btn.textContent = on ? String(on) : '';
  }

  function place() {
    var phone = window.matchMedia(PHONE).matches;
    var body = sheet().querySelector('.sheet-body');
    document.querySelectorAll('.fbar').forEach(function (bar) {
      controlsOf(bar).forEach(function (el) {
        if (!homes.has(el)) homes.set(el, {bar: bar, next: el.nextSibling});
      });
      countOn(bar);
    });
    if (phone) {
      /* Only the visible page's filters: two pages' worth of
         controls in one sheet would be a list of strangers. */
      document.querySelectorAll('.page.on .fbar').forEach(function (bar) {
        controlsOf(bar).forEach(function (el) { body.appendChild(el); });
      });
    } else {
      Array.prototype.slice.call(body.children).forEach(function (el) {
        var home = homes.get(el);
        if (!home) return;
        if (home.next && home.next.parentNode === home.bar) {
          home.bar.insertBefore(el, home.next);
        } else {
          home.bar.appendChild(el);
        }
      });
      openSheet(false);
    }
  }

  window.addEventListener('resize', place);
  document.addEventListener('keydown', function (ev) {
    if (ev.key === 'Escape') openSheet(false);
  });

  /* `nav()` swaps which page is on; the sheet follows it. */
  var navFn = window.nav;
  if (typeof navFn === 'function') {
    window.nav = function () {
      var out = navFn.apply(this, arguments);
      setTimeout(function () { stampAll(); place(); }, 60);
      return out;
    };
  }

  function clearAll() {
    var body = sheet().querySelector('.sheet-body');
    Array.prototype.forEach.call(body.children, function (el) {
      el.value = '';
      el.dispatchEvent(new Event(el.tagName === 'SELECT'
        ? 'change' : 'input', {bubbles: true}));
    });
    document.querySelectorAll('.fbar').forEach(countOn);
    openSheet(false);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }
  /* The app fills its lists after sign-in, which is after this runs. */
  setTimeout(start, 1200);

  window.CRMLists = {stamp: stampAll, place: place, openSheet: openSheet};
})();
