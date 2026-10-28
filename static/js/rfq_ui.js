/* ═══════════════════════════════════════════════════════════════════
   RFQ CHAIN — the shared renderers.
   ───────────────────────────────────────────────────────────────────
   RFQ-DEMO-PARITY. The journey strip, the chips and the leads table
   appear on the chain board, the lead record and the reports. They
   were written three times, so a square meant "done" on one screen
   and "skipped" on another.

   Every function here takes already-escaped-nothing and escapes it
   itself. Nothing fetches: the pages own their own calls, because
   each one carries its own url_prefix.
   ═══════════════════════════════════════════════════════════════════ */
(function (global) {
  'use strict';

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  /* Twenty squares: eight before the RFQ, a gap, then the twelve. */
  function strip(cells) {
    if (!cells || !cells.length) return '';
    var out = '<span class="rfq-js" aria-hidden="true">';
    for (var i = 0; i < cells.length; i++) {
      if (i === 8) out += '<b></b>';
      out += '<i class="' + esc(cells[i].cls) + '" title="'
           + esc(cells[i].title) + '"></i>';
    }
    return out + '</span>';
  }

  function chip(text, cls) {
    if (!text) return '';
    return '<span class="rfq-chip ' + (cls || '') + '">'
         + esc(text) + '</span>';
  }

  var PRIORITY = {Critical: 'rfq-c-red', High: 'rfq-c-amb',
                  Medium: 'rfq-c-info', Low: ''};

  function priorityChip(p) {
    if (!p) return '';
    return chip(p === 'Low' ? 'Low / Auction' : p, PRIORITY[p] || '');
  }

  var STATUS = {hold: ['On hold', 'rfq-c-hold'], won: ['Won', 'rfq-c-ok'],
                lost: ['Lost', ''], pre: ['Before RFQ', 'rfq-c-brand']};

  function statusChip(state) {
    var s = STATUS[state];
    return s ? chip(s[0], s[1]) : '';
  }

  /* "With Dipanka Talukder · Site / Route Survey" + the timer line. */
  function nowCell(now) {
    if (!now) return '';
    var head = now.with
      ? '<div class="rfq-now">With <b>' + esc(now.with) + '</b>'
        + (now.what ? ' · ' + esc(now.what) : '') + '</div>'
      : '<div class="rfq-now">' + esc(now.what || '') + '</div>';
    var timer = now.words
      ? '<span class="rfq-rag ' + esc(now.tone) + '">'
        + esc(now.words) + '</span>'
      : '';
    return head + timer;
  }

  /* The leads table every list shows. `opts.spend` adds a column. */
  function leadsTable(rows, opts) {
    opts = opts || {};
    if (!rows || !rows.length) {
      return '<p class="rfq-empty">Nothing here for these '
           + 'filters.</p>';
    }
    var head = '<tr><th>Lead</th><th>Owner</th>'
      + '<th>Journey (before RFQ | RFQ to order)</th>'
      + '<th>Now</th><th class="n">Quoted</th></tr>';
    var body = rows.map(function (r) {
      var quoted = (r.quoted && r.quoted.words)
        ? esc(r.quoted.words)
          + (r.quoted.rev ? '<div class="rfq-sub">'
             + esc(r.quoted.rev) + '</div>' : '')
        : '–';
      return '<tr class="row" data-lead="' + r.lead_id + '">'
        + '<td><button class="rfq-lnk" type="button" data-lead="'
        + r.lead_id + '">' + esc(r.ref) + '</button>'
        + '<div class="rfq-sub">' + esc(r.company)
        + (r.vertical ? ' · ' + esc(r.vertical) : '') + '</div></td>'
        + '<td>' + esc(r.owner || '—')
        + (r.channel ? '<div class="rfq-sub">' + esc(r.channel)
           + '</div>' : '') + '</td>'
        + '<td>' + strip(r.strip) + ' ' + priorityChip(r.priority)
        + ' ' + statusChip(r.status) + '</td>'
        + '<td>' + nowCell(r.now) + '</td>'
        + '<td class="n">' + quoted + '</td></tr>';
    }).join('');
    return '<div class="rfq-tw"><table><thead>' + head
         + '</thead><tbody>' + body + '</tbody></table></div>';
  }

  /* "Where RFQs are sitting now" — one stacked bar per step. */
  var SEGMENTS = [['r', '--p-danger'], ['o', '--p-warning'],
                  ['g', '--p-success'], ['w', '--p-info'],
                  ['h', '--p-hold']];

  function sitting(bars, bottleneck, filtered) {
    if (!bars || !bars.length) return '';
    var most = Math.max.apply(null, [1].concat(
      bars.map(function (b) { return b.total; })));
    var rows = bars.map(function (b) {
      var fill = SEGMENTS.map(function (seg) {
        var n = b.counts[seg[0]];
        return n ? '<i style="width:' + (n / most * 100)
                 + '%;background:var(' + seg[1] + ')"></i>' : '';
      }).join('');
      var hot = bottleneck && bottleneck.step_no === b.step_no
        ? ' hot' : '';
      var on = filtered === b.step_no ? ' aria-pressed="true"' : '';
      return '<button type="button" class="rfq-bar' + hot
        + '" data-step="' + b.step_no + '"' + on + '>'
        + '<span class="rfq-lbl">' + b.step_no + '. ' + esc(b.title)
        + '</span><span class="rfq-track">' + fill + '</span>'
        + '<span class="rfq-sub">'
        + (b.total ? b.total + (b.counts.r ? ' · ' + b.counts.r
                                + ' late' : '') : '–')
        + '</span></button>';
    }).join('');
    var legend = '<div class="rfq-legend">'
      + '<span><i style="background:var(--p-danger)"></i>Overdue</span>'
      + '<span><i style="background:var(--p-warning)"></i>Due soon</span>'
      + '<span><i style="background:var(--p-success)"></i>On track</span>'
      + '<span><i style="background:var(--p-info)"></i>Customer clock</span>'
      + '<span><i style="background:var(--p-hold)"></i>On hold</span>'
      + '</div>';
    var call = bottleneck
      ? '<p class="rfq-call" style="margin-top:10px">Bottleneck now: '
        + '<b>' + esc(bottleneck.title) + '</b> — ' + bottleneck.late
        + ' of ' + bottleneck.total + ' past the timer.</p>'
      : '';
    return '<div class="rfq-bars">' + rows + '</div>' + legend + call;
  }

  function tile(label, value, sub, cls, attrs) {
    return '<button type="button" class="rfq-tile ' + (cls || '')
      + '" ' + (attrs || '') + '><span class="l">' + esc(label)
      + '</span><span class="v">' + esc(value) + '</span>'
      + '<span class="s">' + esc(sub || '') + '</span></button>';
  }

  /* 14h 20m, 3d 4h — the demo's reading of a duration in hours. */
  function hours(v) {
    if (v == null) return '—';
    var m = Math.round(v * 60);
    if (m < 60) return m + 'm';
    var h = Math.floor(m / 60), mm = m % 60;
    if (h < 24) return h + 'h' + (mm ? ' ' + mm + 'm' : '');
    var d = Math.floor(h / 24), hh = h % 24;
    return d + 'd' + (hh ? ' ' + hh + 'h' : '');
  }

  global.RFQ = {
    esc: esc, strip: strip, chip: chip, priorityChip: priorityChip,
    statusChip: statusChip, nowCell: nowCell, leadsTable: leadsTable,
    sitting: sitting, tile: tile, hours: hours
  };
}(window));
