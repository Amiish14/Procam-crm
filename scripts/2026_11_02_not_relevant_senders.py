"""Fifteen named senders to the Not-relevant queue. DRY RUN BY DEFAULT.

PROCAM-DECISIONS-02NOV, 2 Nov:

    "a dry-run script to mark these as not relevant (reversible, via
     mark_not_relevant, skipping any lead with a quote, outgoing mail
     or human activity) … Add their sender domains to the Not-relevant
     classes so future mail goes there."

    .venv/bin/python scripts/2026_11_02_not_relevant_senders.py
    .venv/bin/python scripts/2026_11_02_not_relevant_senders.py --show 200
    .venv/bin/python scripts/2026_11_02_not_relevant_senders.py --apply \
          --actor DIR12010

THE DOMAINS ARE READ, NOT GUESSED

The decision names fifteen senders by NAME. A domain invented from a
name is a rule that either never fires or fires on somebody else:
"MeetCleanBase" is not obviously `meetcleanbase.com`, and
"Akmglobal-Mailer" is a mailer for a firm whose own domain it may not
share. So this finds the leads first, reports the sender domains they
actually arrived from, and writes those.

Every name that matches no lead is reported and nothing is written for
it. A domain nobody has seen mail from is not a rule worth having.

WHAT IT SKIPS, AND WHY THAT IS THE POINT

`junk_delete.blockers` is asked of every lead and not bypassed: a lead
with a quote, an outgoing mail or any human activity on it is REFUSED
and listed, because such a lead is work somebody has done, whatever its
sender looks like. One of these fifteen having a real enquiry behind it
is exactly the case the skip exists for.

REVERSIBLE

`mark_not_relevant` is the existing path and the only one with a
`restore`. Nothing is deleted: `junk_deleted` goes true, the row stays,
and the Restore button on the Not-relevant screen puts it back exactly
as it was.

THE SENDER IS NOT BLOCKED

`block_sender=False` throughout, and the list written is the
Not-relevant list, not the ingest block list. A blocked sender's mail
never becomes a lead at all, so a real enquiry from one of these — a
client's buyer mailing from a portal account — would be invisible
rather than merely filed in the right queue. See
`app/services/not_relevant_senders.py`.
"""
import argparse
import os
import sys
from collections import Counter

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from app import Lead, app, db                              # noqa: E402

ACTOR = 'PROCAM-DECISIONS-02NOV'

#: The fifteen, as the decision spells them. Matched on the squashed
#: form, so "Akmglobal-Mailer" finds "AKM Global Mailer" and
#: "akmglobal mailer" alike.
NAMED = (
    'All India Info-Database',
    'Procurengine',
    'Abiresearch',
    'Akmglobal-Mailer',
    'Bricsamail',
    'Gastechevent',
    'Luxuryescapes',
    'Marineinsight',
    'MeetCleanBase',
    'Opterix',
    'PeakPure Digital',
    'Promedia',
    'Itemsecure',
    'Futurebattery',
    'Dreamincubator',
)

#: Why each lead is being filed. One sentence, in the audit trail.
WHY = ('PROCAM-DECISIONS-02NOV: named on 2 Nov as a sender whose mail '
       'is not an enquiry')


def _squash(text):
    return ''.join(c for c in (text or '').lower() if c.isalnum())


def _domain(address):
    address = (address or '').strip().lower()
    return address.rsplit('@', 1)[-1] if '@' in address else ''


def find(names):
    """{name: [lead]} — every open lead that looks like one of these.

    Matched three ways, because the same sender appears differently
    depending on how the lead was made:

      * the typed company name;
      * the sender address;
      * the sender's domain.

    All three on the squashed form, so punctuation and spacing do not
    decide whether a rule fires.
    """
    from app.services import junk_delete as jd

    wanted = {_squash(n): n for n in names}
    found = {n: [] for n in names}
    seen = set()

    leads = (Lead.query
             .filter(Lead.junk_deleted.isnot(True))
             .order_by(Lead.id).all())
    for lead in leads:
        sender = jd.sender_of(lead)
        haystacks = (_squash(lead.company), _squash(sender),
                     _squash(_domain(sender)))
        for key, name in wanted.items():
            if not key:
                continue
            if any(h and key in h for h in haystacks):
                if lead.id not in seen:
                    seen.add(lead.id)
                    found[name].append(lead)
                break
    return found


def survey(found, tolerate=()):
    """What would happen, per name. Writes nothing.

    `tolerate` names blocker keys the run has been authorised to
    overlook — only ever from --allow on the command line, and never
    one of `junk_delete.WORK_KEYS`, which that module refuses outright.
    """
    from app.services import junk_delete as jd

    tolerate = set(tolerate or ())

    out = []
    for name, leads in found.items():
        eligible, refused = [], []
        domains, held_domains = Counter(), Counter()
        for lead in leads:
            domain = _domain(jd.sender_of(lead))
            why = [(k, w) for k, w in jd.blockers(lead)
                   if k not in tolerate]
            work = [k for k, _w in jd.blockers(lead)
                    if k in jd.WORK_KEYS]
            if why:
                refused.append({
                    'lead': lead,
                    'keys': [k for k, _w in why],
                    'why': '; '.join(w for _k, w in why),
                    'work': work,
                    'notes': notes_on(lead),
                })
                # A domain is still worth a rule when the only thing in
                # the way is soft — a note, or a stage. It is not when
                # there is sales work on the lead: that is a live
                # enquiry, and filing its sender would file the client's
                # next mail as junk.
                if domain and not work:
                    domains[domain] += 1
                elif domain:
                    held_domains[domain] += 1
            else:
                eligible.append(lead)
                if domain:
                    domains[domain] += 1
        out.append({'name': name, 'leads': leads, 'eligible': eligible,
                    'refused': refused, 'domains': domains,
                    'held_domains': held_domains})
    return out


def notes_on(lead, limit=2):
    """What the notes on this lead actually say.

    Printed because the live run came back with "somebody wrote a note
    on it" against all fifteen senders, and on a marketing lead that
    note is very often the word "junk" — somebody reading it, deciding,
    and having no button to press. Treating that as evidence the lead
    matters is exactly backwards, and nobody can tell which it is
    without reading them.
    """
    try:
        from app import LeadNote
        rows = (LeadNote.query
                .filter(LeadNote.lead_id == lead.id,
                        LeadNote.is_deleted.isnot(True))
                .order_by(LeadNote.id).limit(limit).all())
        return [(r.note_text or '').strip().replace('\n', ' ')[:70]
                for r in rows]
    except Exception:                                     # noqa: BLE001
        return []


def report(rows, show):
    from app.services import junk_delete as jd
    from app.services import not_relevant_senders as nrs

    WORK = set(jd.WORK_KEYS)

    total = sum(len(r['leads']) for r in rows)
    ready = sum(len(r['eligible']) for r in rows)
    held = sum(len(r['refused']) for r in rows)
    missing = [r['name'] for r in rows if not r['leads']]

    print(f'\n── {len(rows)} name(s), {total} lead(s) ──')
    print(f'  {ready:>4}  would be marked not relevant')
    print(f'  {held:>4}  skipped — a quote, an outgoing mail or human '
          f'activity on them')
    print(f'  {len(missing):>4}  name(s) match no lead at all')

    print('\n── name by name ──')
    for row in rows:
        if not row['leads']:
            print(f'  {row["name"]:<26} no lead matches this name — '
                  f'nothing written, no rule added')
            continue
        shown = ', '.join(f'{d} ({n})'
                          for d, n in row['domains'].most_common(3))
        print(f'  {row["name"]:<26} {len(row["eligible"]):>3} ready, '
              f'{len(row["refused"]):>2} skipped   {shown}')

    if held:
        by_key = Counter()
        for row in rows:
            for item in row['refused']:
                for key in item['keys']:
                    by_key[key] += 1
        print('\n── why each skipped lead is skipped, per rule ──')
        for key, count in by_key.most_common():
            mark = '  (sales work)' if key in WORK else '  (soft)'
            print(f'  {count:>4}  {key}{mark}')
        print('\n  A lead with SALES WORK on it is not junk, whatever '
              'its sender looks\n  like — one of these fifteen having a '
              'real enquiry behind it is exactly\n  what that check is '
              'for, and no flag can overrule it.')
        soft = sorted(k for k in by_key if k not in WORK)
        if soft:
            print(f'\n  The soft ones — {", ".join(soft)} — can be '
                  f'overlooked with --allow, once\n  you have read the '
                  f'notes printed below and decided they say nothing.')

        print('\n── the skipped ones, and what is on them ──')
        for row in rows:
            for item in row['refused'][:show]:
                lead = item['lead']
                print(f'    #{lead.id:<7} {row["name"][:22]:<22} '
                      f'{item["why"]}')
                for text in item['notes']:
                    print(f'                 note: {text!r}')

    print('\n── the sender rules that would be added ──')
    rules = plan_rules(rows)
    if not rules:
        print('  (none — no domain was observed)')
    for pattern, why in rules:
        refused = nrs.why_not_a_pattern(pattern)
        mark = '!!' if refused else '  '
        print(f'  {mark} {pattern:<42} {refused or why}')
    held_back = held_back_rules(rows)
    if held_back:
        print(f'\n── {len(held_back)} domain(s) deliberately NOT added ──')
        print('  Every lead from each of these carries sales work — an '
              'RFQ, a quote, an\n  outgoing mail, a logged call or an '
              'opportunity. A domain whose only\n  evidence is a live '
              'enquiry is not evidence of anything, and adding it\n'
              '  would file that client\'s next mail as junk.')
        for pattern, name, count in held_back:
            print(f'    {pattern:<42} {name} — {count} skipped lead(s) '
                  f'only')

    print('\n  These go on the Not-relevant list, not the ingest block '
          'list: the mail\n  still becomes a lead and the lead lands in '
          'the Not-relevant queue. A\n  blocked sender never becomes a '
          'lead at all, so a real enquiry from one\n  of these would be '
          'invisible rather than filed in the right place.')
    return ready, held


def plan_rules(rows):
    """[(pattern, why)] from the domains observed on ELIGIBLE leads only.

    A domain is only written where it was seen on a lead that is
    actually being filed, and `*@` is prepended because a bare domain
    never matches a sender address.

    Eligible only, and this is the correction that matters. The first
    version counted every domain it saw, including the ones on leads it
    had just refused to touch — so a sender with one live quote behind
    it had its domain added anyway, and every future mail from the
    client who sent that quote would have been filed as junk. A domain
    whose only evidence is a lead somebody is working is not evidence
    of anything.
    """
    out, seen = [], set()
    for row in rows:
        for domain, count in row['domains'].most_common():
            pattern = f'*@{domain}'
            if pattern in seen:
                continue
            seen.add(pattern)
            out.append((pattern,
                        f'{row["name"]} — {count} lead(s) arrived from '
                        f'{domain}; named 2 Nov'))
    return out


def held_back_rules(rows):
    """Domains seen ONLY on leads this run will not touch.

    Reported so the exclusion is visible. A domain here is one where
    the only mail from that sender is a lead with a quote, an outgoing
    mail or human activity on it — which is a reason to leave the
    sender alone, not a reason to file it silently.
    """
    writing = {p for p, _w in plan_rules(rows)}
    out = []
    for row in rows:
        for domain, count in row['held_domains'].most_common():
            pattern = f'*@{domain}'
            if pattern in writing:
                continue
            out.append((pattern, row['name'], count))
    return out


def apply_it(rows, actor, tolerate=()):
    from app.services import junk_delete as jd
    from app.services import not_relevant_senders as nrs

    marked, problems = 0, []
    for row in rows:
        for lead in row['eligible']:
            try:
                jd.mark_not_relevant(
                    lead, reason='spam',
                    note=f'{WHY} ({row["name"]})',
                    actor=actor,
                    # The sender is NOT blocked. See the docstring.
                    block_sender=False, commit=False,
                    ignore=tolerate)
                marked += 1
            except ValueError as exc:
                problems.append((lead.id, str(exc)))

    added, skipped = 0, []
    for pattern, why in plan_rules(rows):
        outcome = nrs.add(pattern, why=why, actor=actor)
        if outcome == 'added':
            added += 1
        elif outcome == 'already':
            skipped.append((pattern, 'already on the list'))
        else:
            skipped.append((pattern, outcome))
    db.session.commit()
    return marked, problems, added, skipped


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--apply', action='store_true',
                    help='mark them. Default is a dry run.')
    ap.add_argument('--actor', help='employee code of the person '
                                    'deciding. Required with --apply.')
    ap.add_argument('--show', type=int, default=25)
    ap.add_argument('--name', action='append',
                    help='one name instead of the fifteen; repeatable')
    ap.add_argument('--allow', action='append', default=None,
                    metavar='KEY',
                    help='overlook a SOFT blocker — note, task, stage. '
                         'Repeatable. Never a sales-work blocker: '
                         'junk_delete refuses those outright. Read the '
                         'notes the dry run prints before using it.')
    args = ap.parse_args()

    names = tuple(args.name) if args.name else NAMED
    tolerate = tuple(args.allow or ())

    with app.app_context():
        print(f'database: {db.engine.url}')
        print('PROCAM-DECISIONS-02NOV — senders whose mail is not an '
              'enquiry.' + ('' if args.apply else ' DRY RUN.'))

        rows = survey(find(names), tolerate=tolerate)
        if tolerate:
            print(f'  overlooking: {", ".join(tolerate)}')
        ready, _held = report(rows, args.show)

        if not args.apply:
            db.session.rollback()
            print('\n' + '─' * 64)
            print(f'DRY RUN ONLY. NOTHING CHANGED. {ready} lead(s) would '
                  f'be marked not relevant.')
            print('Send these counts to Nilesh (rule 0.3), then --apply '
                  '--actor <code>.')
            return 0

        if not args.actor:
            print('\n  --apply needs --actor <employee code>. Filing '
                  'somebody\'s lead is a\n  change with a name on it.')
            db.session.rollback()
            return 1

        marked, problems, added, skipped = apply_it(
            rows, args.actor, tolerate=tolerate)
        print('\n' + '─' * 64)
        print(f'  marked not relevant: {marked}')
        print(f'  sender rules added:  {added}')
        for pattern, why in skipped:
            print(f'    [skip] {pattern}: {why}')
        if problems:
            print(f'  refused while writing: {len(problems)}')
            for lead_id, why in problems[:20]:
                print(f'    #{lead_id:<7} {why}')
        print('\n  Nothing was deleted. Restore any of them with the '
              'Restore button on\n  the Not-relevant screen. The sender '
              'rules are on Master Data →\n  Not-relevant senders, where '
              'they can be switched off.')
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
