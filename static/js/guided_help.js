/*
 * "What happened, why, and what to do" — never only "no".
 *
 * CRM-LEAD-FLOW-01 §5. One helper, used by every refusal on every
 * screen, so a message cannot ship as a bare alert() again. The
 * wording comes from the server catalogue, which an administrator
 * edits from Admin → Help texts; this file only draws it and wires
 * the buttons.
 *
 *   showGuide('STAGE_FIELDS_MISSING', {missing: ['Quote no.'],
 *                                      lead_id: 12345})
 *
 * A response that carries `guide` draws itself:
 *
 *   fetch(...).then(r => r.json()).then(d => {
 *     if (!d.ok) return showGuideFrom(d);
 *   })
 */
(function (w) {
  'use strict';
  if (w.showGuide) return;

  var PREFIX = (w.URL_PREFIX != null) ? w.URL_PREFIX : '';
  var cache = {};

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  function box() {
    var el = document.getElementById('crmGuide');
    if (el) return el;
    el = document.createElement('div');
    el.id = 'crmGuide';
    el.setAttribute('role', 'dialog');
    el.setAttribute('aria-modal', 'true');
    el.hidden = true;
    el.innerHTML =
      '<div class="cg-back" onclick="closeGuide()"></div>' +
      '<div class="cg-card">' +
      '  <h2 id="cgTitle"></h2>' +
      '  <p id="cgWhy"></p>' +
      '  <ol id="cgSteps"></ol>' +
      '  <div id="cgActions" class="cg-actions"></div>' +
      '  <button type="button" class="cg-close" onclick="closeGuide()">' +
      '    Close</button>' +
      '</div>';
    document.body.appendChild(el);
    return el;
  }

  function closeGuide() {
    var el = document.getElementById('crmGuide');
    if (el) el.hidden = true;
  }

  /* The buttons. Each one has to land the person somewhere real —
     a guide whose button does nothing is worse than no button. */
  function run(guide, action, ctx) {
    var done = false;
    var target = action.target || '';
    if (action.action === 'tab' && w.leadTab) {
      w.leadTab(target); done = true;
    } else if (action.action === 'focus') {
      var name = (target === 'first_missing' && ctx && ctx.missing)
        ? (ctx.missing[0] || '') : target;
      var field = document.getElementById(name)
        || document.querySelector('[data-field="' + name + '"]');
      if (field) {
        if (w.leadTab && field.dataset && field.dataset.tab) {
          w.leadTab(field.dataset.tab);
        }
        field.scrollIntoView({block: 'center'});
        field.classList.add('cg-point');
        setTimeout(function () { field.classList.remove('cg-point'); },
                   2400);
        try { field.focus(); } catch (e) { /* not focusable */ }
        done = true;
      }
    } else if (action.action === 'open' && ctx && ctx[target + '_url']) {
      w.location.href = ctx[target + '_url']; done = true;
    } else if (action.action === 'mail' && w.draftGuideMail) {
      w.draftGuideMail(target, ctx); done = true;
    } else if (action.action === 'help' && w.askForHelp) {
      w.askForHelp(guide.code, ctx); done = true;
    }

    fetch(PREFIX + '/api/help/guides/'
          + encodeURIComponent(guide.code) + '/clicked', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({label: action.label || '',
                            lead_id: (ctx && ctx.lead_id) || null,
                            page: (ctx && ctx.page) || ''})
    }).catch(function () { /* logging must never block the help */ });

    if (done) closeGuide();
    return done;
  }

  function draw(guide, ctx) {
    ctx = ctx || {};
    var el = box();
    document.getElementById('cgTitle').textContent = guide.title || '';
    document.getElementById('cgWhy').textContent = guide.why || '';

    var steps = document.getElementById('cgSteps');
    steps.innerHTML = '';
    (guide.steps || []).forEach(function (step) {
      var li = document.createElement('li');
      li.textContent = step;
      steps.appendChild(li);
    });
    steps.hidden = !(guide.steps || []).length;

    var acts = document.getElementById('cgActions');
    acts.innerHTML = '';
    (guide.actions || []).forEach(function (action) {
      var b = document.createElement('button');
      b.type = 'button';
      b.className = 'btn';
      b.textContent = action.label || 'Go';
      b.onclick = function () { run(guide, action, ctx); };
      acts.appendChild(b);
    });

    /* Always a way out that does not need the Tech team's phone
       number. */
    var ask = document.createElement('button');
    ask.type = 'button';
    ask.className = 'btn cg-ghost';
    ask.textContent = 'Ask for help';
    ask.onclick = function () {
      run(guide, {label: 'Ask for help', action: 'help',
                  target: 'generic'}, ctx);
    };
    acts.appendChild(ask);

    el.hidden = false;
  }

  function showGuide(code, ctx) {
    ctx = ctx || {};
    if (cache[code]) { draw(cache[code], ctx); return; }
    var q = '?page=' + encodeURIComponent(ctx.page || '')
          + (ctx.lead_id ? '&lead_id=' + encodeURIComponent(ctx.lead_id) : '');
    fetch(PREFIX + '/api/help/guides/' + encodeURIComponent(code) + q)
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!d || !d.guide) return;
        cache[code] = d.guide;
        draw(d.guide, ctx);
      })
      .catch(function () {
        /* Even the help failing must say something. */
        draw({code: code,
              title: 'That did not work, and the explanation could '
                     + 'not be loaded either.',
              why: 'The CRM could not be reached just now.',
              steps: ['Try again in a moment.'],
              actions: []}, ctx);
      });
  }

  /* A refusal that already carries its guide draws without a
     second round trip. */
  function showGuideFrom(body, ctx) {
    if (body && body.guide) {
      cache[body.guide.code] = body.guide;
      draw(body.guide, Object.assign({}, body.guide.context || {},
                                     ctx || {}));
      return true;
    }
    return false;
  }

  w.showGuide = showGuide;
  w.showGuideFrom = showGuideFrom;
  w.closeGuide = closeGuide;
})(window);
