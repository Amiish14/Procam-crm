/* CRM-UI-01 — one look for every call and activity entry.

   Lead notes, the account timeline, the contact card and the company
   page each drew a logged call their own way, so the same call looked
   like four different things. Every one of them now goes through
   callEntry(), and the server-rendered pages through the Jinja macro
   templates/_call_entry.html, which writes the same markup:

     date-time · who            [kind]
     Contact   …
     Outcome   …
     Notes     …
     Next      … (by date)

   A field that is empty shows "—" rather than disappearing, so every
   entry has the same shape and the eye finds the outcome in the same
   place each time. Display only: nothing here posts or changes data. */
(function(){
  function esc(v){
    return String(v == null ? '' : v).replace(/[&<>"']/g, function(c){
      return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];
    });
  }
  function line(label, value){
    return '<div class="pc-call-row"><span class="pc-call-k">' + label
      + '</span><span class="pc-call-v">' + (value ? esc(value) : '—')
      + '</span></div>';
  }
  window.callEntry = function(x){
    x = x || {};
    var next = x.next_action
      ? x.next_action + (x.next_action_at ? ' (by ' + x.next_action_at + ')' : '')
      : '';
    return '<div class="pc-call">'
      + '<div class="pc-call-hd"><span class="pc-call-when">'
        + esc(x.when || '—') + '</span>'
        + '<span class="pc-call-who">' + esc(x.who || '') + '</span>'
        + (x.kind ? '<span class="pc-call-kind">' + esc(x.kind) + '</span>' : '')
        + (x.extra || '')
      + '</div>'
      + line('Contact', x.contact)
      + line('Outcome', x.outcome)
      + line('Notes', x.notes)
      + line('Next', next)
      + '</div>';
  };
})();
