"""
One lead, on one line, from target account to handover.

RFQ-DEMO-PARITY. The demo shows every list — the cockpit, Leads,
the chain board — as the same table: the lead, its owner, a strip
of twenty squares for the eight pre-RFQ stages and the twelve
chain steps, what is happening now, and the last value quoted.

That row was being built three times in three templates, each
with its own idea of which square is which. It is built here
once, so a lead that reads "overdue" on the board cannot read
"on track" on the cockpit.

Nothing in this module writes. It reads the chain through
`rfq_chain`, the pre-RFQ lane through `lead_journey`, and the
Access Matrix is applied by the caller — a service that narrows
its own queries would narrow them twice.
"""
from __future__ import annotations

#: The square that separates the two lanes in the strip.
LANES = 8

def _cache():
    """The name map, for this request only.

    A process-wide dict was wrong twice over: a deployment runs for
    days and an employee can be renamed in that time, and the test
    suite recreates the same employee codes with different names in
    one process, so the second module read the first module's
    people. Outside a request there is no cache at all — a script
    reading a hundred rows can afford a hundred lookups more
    easily than it can afford a wrong name.
    """
    try:
        from flask import g, has_request_context
        if not has_request_context():
            return {}
        if not hasattr(g, '_rfq_names'):
            g._rfq_names = {}
        return g._rfq_names
    except Exception:                                     # noqa: BLE001
        return {}


def clear_names():
    """Forget the name map. For tests."""
    try:
        from flask import g, has_request_context
        if has_request_context() and hasattr(g, '_rfq_names'):
            del g._rfq_names
    except Exception:                                     # noqa: BLE001
        pass


def name_of(emp_code):
    """A person's name, falling back to their code.

    The board showed `PAR_SALES` where the demo shows "Vijay T V".
    A code is not a name, and the one person who can read it is
    the one who does not need the row.
    """
    code = (emp_code or '').strip()
    if not code:
        return ''
    cache = _cache()
    if code in cache:
        return cache[code]
    try:
        from app import Employee
        row = (Employee.query.with_entities(Employee.name)
               .filter_by(emp_code=code).first())
        found = (row[0] if row and row[0] else code)
    except Exception:                                     # noqa: BLE001
        found = code
    cache[code] = found
    return found


def names_for(codes):
    """Warm the cache for a page's worth of rows in one query."""
    cache = _cache()
    wanted = {c for c in (codes or ()) if c and c not in cache}
    if not wanted:
        return
    try:
        from app import Employee
        for code, name in (Employee.query
                           .with_entities(Employee.emp_code,
                                          Employee.name)
                           .filter(Employee.emp_code.in_(wanted))
                           .all()):
            cache[code] = name or code
    except Exception:                                     # noqa: BLE001
        pass
    for code in wanted:
        cache.setdefault(code, code)


# ── the strip ────────────────────────────────────────────────────────
def strip(lead, rfq=None, *, board=None):
    """Twenty cells: eight before the RFQ, twelve after.

    Each cell is {'cls': ..., 'title': ...}. `cls` is what the
    stylesheet reads: '' not reached, 'd' done, 's' skipped or not
    needed, 'a g|o|r|h|w' the one that is live and its timer
    colour. The demo's reading exactly, so the two can be held
    side by side.
    """
    from app.services import lead_journey as lj, rfq_chain as rc
    from presales.models import ACCOUNT_DEV_STAGES

    cells = []
    here = lj.stage_rank(_dev_stage(lead))
    direct = rfq is not None and here < 0
    for i, stage in enumerate(ACCOUNT_DEV_STAGES[:LANES]):
        if direct:
            cells.append({'cls': 's',
                          'title': f'{stage} — skipped, the RFQ came '
                                   f'direct'})
        elif rfq is not None or i < here:
            cells.append({'cls': 'd', 'title': stage})
        elif i == here:
            cells.append({'cls': 'a g', 'title': stage + ' — now'})
        else:
            cells.append({'cls': '', 'title': stage})

    rows = board if board is not None else (
        rc.board(rfq) if rfq is not None else [])
    by_no = {b['step_no']: b for b in rows}
    for step in rc.STEPS:
        box = by_no.get(step['no'])
        if box is None:
            tail = ' — not needed' if rfq is not None else ''
            cells.append(
                {'cls': 's' if rfq is not None else '',
                 'title': f"{step['no']}. {step['title']}{tail}"})
            continue
        title = f"{box['step_no']}. {box['title']}"
        if box['state'] == 'done':
            cells.append({'cls': 'd', 'title': title + ' — done'})
        elif box['state'] == 'current':
            cells.append({'cls': 'a ' + _letter(box['colour']),
                          'title': title + ' — ' + (box['words'] or '')})
        else:
            cells.append({'cls': '', 'title': title})
    return cells


def _letter(colour):
    """The demo's one-letter timer classes."""
    return {'green': 'g', 'orange': 'o', 'red': 'r',
            'purple': 'h', 'blue': 'w'}.get(colour, '')


def _dev_stage(lead):
    from app import Company, db
    if not getattr(lead, 'company_id', None):
        return ''
    account = db.session.get(Company, lead.company_id)
    return (getattr(account, 'dev_stage', '') or '')


# ── one row ──────────────────────────────────────────────────────────
def row(lead, rfq=None):
    """Everything the leads table shows for one lead."""
    from app.services import rfq_chain as rc

    board = rc.board(rfq) if rfq is not None else []
    state = status_of(lead, rfq)
    # A lead recorded as Won or Lost reads as closed even if a
    # step was left open: the outcome is the fact, and a board
    # that says "with Sales · Handover" under a Won chip is the
    # kind of disagreement nobody trusts the board after.
    live = (None if state in ('won', 'lost')
            else next((b for b in board
                       if b['state'] == 'current'), None))
    quoted = _last_quote(lead)
    held = bool(getattr(rfq, 'held_at', None))

    if live is not None:
        colour = 'purple' if held else live['colour']
        now = {'with': name_of(live['owner']),
               'what': f"{live['step_no']}. {live['title']}",
               'words': ('on hold — '
                         + (getattr(rfq, 'hold_reason', '') or 'paused')
                         if held else live['words']),
               'tone': _tone(colour)}
    else:
        now = {'with': name_of(getattr(lead, 'assigned_to', '')),
               'what': _closed_words(lead, rfq),
               'words': '', 'tone': _tone('grey')}

    return {
        'lead_id': lead.id,
        'ref': _ref(lead, rfq),
        'company': getattr(lead, 'company', '') or '',
        'vertical': getattr(lead, 'procam_vertical', '') or '',
        'owner': name_of(getattr(lead, 'assigned_to', '')),
        'channel': getattr(lead, 'lead_channel', '') or '',
        'priority': (getattr(rfq, 'priority', '') or ''),
        'status': state,
        'strip': strip(lead, rfq, board=board),
        'now': now,
        'quoted': quoted,
        'step_no': live['step_no'] if live else None,
        'holder': (live['owner'] if live
                   else getattr(lead, 'assigned_to', '')) or '',
        'held': held,
        'journey': f'/leads/{lead.id}/journey',
    }


def status_of(lead, rfq=None):
    """'pre', 'rfq', 'hold', 'won' or 'lost' — the demo's words."""
    stage = (getattr(lead, 'stage', '') or '')
    if stage == 'Won':
        return 'won'
    if stage in ('Lost', 'Closed Lost'):
        return 'lost'
    if rfq is None:
        return 'pre'
    if getattr(rfq, 'held_at', None):
        return 'hold'
    return 'rfq'


def _closed_words(lead, rfq):
    state = status_of(lead, rfq)
    if state == 'won':
        return 'Won · handed over'
    if state == 'lost':
        return 'Lost'
    if rfq is None:
        return 'Before the RFQ'
    return 'Nothing open'


def _ref(lead, rfq):
    number = (getattr(rfq, 'rfq_number', '') or '')
    return f'#{lead.id}' + (f' · {number}' if number else '')


def _tone(colour):
    return {'red': 'rfq-t-red', 'orange': 'rfq-t-amb',
            'green': 'rfq-t-ok', 'blue': 'rfq-t-info',
            'purple': 'rfq-t-hold'}.get(colour, 'rfq-t-grey')


def _last_quote(lead):
    """The newest quote that is not a draft, as text, or ''."""
    try:
        from app.models.quote import Quote
        from app.services import quote_rev
        quote = (Quote.query
                 .filter(Quote.lead_id == lead.id,
                         Quote.status != 'Draft')
                 .order_by(Quote.id.desc()).first())
        if quote is None:
            return {'words': '', 'rev': ''}
        return {'words': money(quote.total_amount, quote.currency),
                'rev': quote_rev.display_number(quote).split(' / ')[-1]}
    except Exception:                                     # noqa: BLE001
        return {'words': '', 'rev': ''}


def money(amount, currency='INR'):
    """₹12.40 L — the demo's shortening, for Indian figures."""
    try:
        value = float(amount or 0)
    except (TypeError, ValueError):
        return ''
    if not value:
        return ''
    if (currency or 'INR') != 'INR':
        return f'{currency} {value:,.0f}'
    if value >= 1e7:
        return f'₹{value / 1e7:.2f} Cr'
    if value >= 1e5:
        return f'₹{value / 1e5:.2f} L'
    return f'₹{value:,.0f}'


# ── where RFQs are sitting now ───────────────────────────────────────
def sitting(rows):
    """Per-step counts split by timer colour, and the bottleneck.

    The live board drew one grey column per step and a count on
    top, which answers "how many" and not "how many are late" —
    the question the panel exists for.
    """
    from app.services import rfq_chain as rc

    bars = []
    for step in rc.STEPS:
        counts = {'r': 0, 'o': 0, 'g': 0, 'w': 0, 'h': 0}
        for item in rows:
            if item.get('step_no') != step['no']:
                continue
            letter = _letter('purple' if item.get('held')
                             else _colour_of(item))
            if letter:
                counts[letter] += 1
        total = sum(counts.values())
        bars.append({'step_no': step['no'], 'title': step['title'],
                     'counts': counts, 'total': total})
    live = [b for b in bars if b['step_no'] > 1]
    worst = max(live, key=lambda b: b['counts']['r'], default=None)
    bottleneck = None
    if worst is not None and worst['counts']['r']:
        bottleneck = {'title': worst['title'],
                      'late': worst['counts']['r'],
                      'total': worst['total'],
                      'step_no': worst['step_no']}
    return {'bars': live, 'bottleneck': bottleneck}


def _colour_of(item):
    tone = (item.get('now') or {}).get('tone', '')
    return {'rfq-t-red': 'red', 'rfq-t-amb': 'orange',
            'rfq-t-ok': 'green', 'rfq-t-info': 'blue',
            'rfq-t-hold': 'purple'}.get(tone, 'grey')
