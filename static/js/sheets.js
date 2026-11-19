/* ═══════════════════════════════════════════════════════════════════
   PROCAM CRM — a way out of every dialog.
   UI sweep 19 Nov.

   Each of the ten <dialog> elements on the module pages puts Cancel
   at the BOTTOM of its form. On a phone that form is a full-height
   sheet, so getting out of one meant scrolling to the end of it.

   WHY A SCRIPT AND NOT TEN TEMPLATES

   The button is the same button every time, and the eleventh dialog
   somebody adds should have it without being told. Written here, it
   is one definition; written into the templates it is ten copies to
   keep in step, and the eleventh is the one that goes without.

   It is added to the dialog's FIRST ELEMENT CHILD -- the form or the
   wrapper -- not to the dialog itself, because that child is the
   scrolling box on a phone and a sticky button has to be inside the
   thing that scrolls.

   The second job is `body.modal-open`, which shell.css already reads:
   it takes the bottom tab bar and the floating chat button out of the
   way while a dialog is open, so a tap meant for Save cannot land on
   Leads. The single-page app does this for its own `.mob` dialogs;
   the module pages use a native <dialog>, which has `close` and a
   `open` attribute to watch, so it needs no guessing.
   ═══════════════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  function addClose(dlg) {
    if (dlg.querySelector(':scope > * > .sheet-x')
        || dlg.querySelector(':scope > .sheet-x')) return;
    var host = dlg.firstElementChild || dlg;
    var x = document.createElement('button');
    x.type = 'button';
    x.className = 'sheet-x';
    x.setAttribute('aria-label', 'Close');
    x.innerHTML = '&#10005;';
    x.addEventListener('click', function () { close(dlg); });
    host.insertBefore(x, host.firstChild);
  }

  /* `method="dialog"` forms close themselves on submit, but a button
     added by script has to say so. `close()` on a dialog that was
     opened with `show()` rather than `showModal()` works the same. */
  function close(dlg) {
    try {
      dlg.close();
    } catch (e) {
      dlg.removeAttribute('open');
    }
    sync();
  }

  function anyOpen() {
    return Array.prototype.some.call(
      document.querySelectorAll('dialog'), function (d) {
        return d.hasAttribute('open');
      });
  }

  function sync() {
    document.body.classList.toggle('modal-open', anyOpen());
  }

  /* `showModal()` and `close()` are wrapped so the class is set in
     the SAME TICK as the dialog opening. A MutationObserver is a
     microtask later, which is a frame in which the chat button is
     still drawn over the sheet -- and the first thing the sheet shows
     a reader is that frame. The observer below stays as the backstop
     for a page that sets the `open` attribute itself rather than
     calling the method. */
  function hook(dlg, name) {
    var original = dlg[name];
    if (typeof original !== 'function') return;
    dlg[name] = function () {
      var out = original.apply(dlg, arguments);
      sync();
      return out;
    };
  }

  function start() {
    var dialogs = document.querySelectorAll('dialog');
    if (!dialogs.length) return;
    Array.prototype.forEach.call(dialogs, function (dlg) {
      addClose(dlg);
      ['showModal', 'show', 'close'].forEach(function (name) {
        hook(dlg, name);
      });
      new MutationObserver(sync)
        .observe(dlg, {attributes: true, attributeFilter: ['open']});
      dlg.addEventListener('close', sync);
    });
    sync();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }

  window.CRMSheets = {close: close, sync: sync};
})();
