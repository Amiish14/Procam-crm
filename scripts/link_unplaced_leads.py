"""
Link leads filed under a placeholder to the account they belong to.

CRM-LEAD-ACCOUNT-01 §2.6, and §5's one-off: the BEML tender saved
as "To review" on 29 Oct 2026 needs joining to BEML LIMITED.

Read-only by default. It prints what it would do and changes
nothing:

    .venv/bin/python scripts/link_unplaced_leads.py
    .venv/bin/python scripts/link_unplaced_leads.py --lead 12437

The default report lists leads whose *name* is a placeholder. That
is six leads, and it is not the backlog: 293 open leads have no
account and most carry a real company name, so nothing flags them.
`--unlinked` sweeps all of those and groups them by customer,
merging spellings of one name, so eight Indo Tech Transformers
leads are one decision rather than eight:

    .venv/bin/python scripts/link_unplaced_leads.py --unlinked

Applying needs the leads named, the account named, and an actor —
no "link everything it guessed", because a wrong link is harder to
find afterwards than an unlinked lead:

    .venv/bin/python scripts/link_unplaced_leads.py \\
        --ids 12437 --account 318 --actor DIR12010 --apply

Nothing is deleted and nothing is closed. A lead that is already
linked is skipped and said so.
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))


def candidates(lead_ids=()):
    """Every unplaced open lead, with what the matcher suggests."""
    from app import Lead, db
    from app.services import lead_account as la

    rows = []
    if lead_ids:
        leads = [db.session.get(Lead, int(x)) for x in lead_ids]
        leads = [x for x in leads if x is not None]
    else:
        from app.services import sales_rules
        leads = (Lead.query
                 .filter(Lead.company_id.is_(None),
                         Lead.is_archived.isnot(True),
                         Lead.stage.in_(sales_rules.open_stages()))
                 .order_by(Lead.id.desc()).limit(500).all())
    for lead in leads:
        why = la.is_placeholder(lead.company)
        if not why and not lead_ids:
            continue
        rows.append({
            'lead': lead,
            'why': why or '(named, but no account linked)',
            'suggestions': la.suggest(lead, limit=3),
        })
    return rows


def report(rows):
    if not rows:
        return 'Nothing unplaced. Every open lead has an account.'
    out = [f'{len(rows)} lead(s) with no account:', '']
    for row in rows:
        lead = row['lead']
        out.append(f'  #{lead.id}  {lead.company or "(blank)"}')
        out.append(f'      {row["why"]}')
        out.append(f'      from {lead.email or "(no sender)"}'
                   f'  ·  {lead.stage}')
        if row['suggestions']:
            for found in row['suggestions']:
                out.append(f'      suggests: {found["name"]} '
                           f'(id {found["account_id"]}) — '
                           f'{found["rule"]}')
        else:
            out.append('      suggests: nothing — create the account')
        out.append('')
    out.append('Nothing was changed. To link, name the leads and the '
               'account:')
    out.append('  --ids <lead ids> --account <id> --actor <EMP> '
               '--apply')
    return '\n'.join(out)


def apply(lead_ids, *, account_id, actor):
    """Link each named lead to the named account. (done, refused)."""
    from app import Company, Lead, db
    from app.services import lead_account as la

    account = db.session.get(Company, int(account_id))
    if account is None:
        return [], [(None, f'no account {account_id}')]

    done, refused = [], []
    for raw in lead_ids:
        lead = db.session.get(Lead, int(raw))
        if lead is None:
            refused.append((raw, 'no such lead'))
            continue
        if lead.company_id:
            refused.append((lead.id, f'already linked to '
                                     f'{lead.company}'))
            continue
        ok, detail = la.link(lead, account, actor=actor,
                             reason='one-off clean-up')
        if not ok:
            refused.append((lead.id, detail))
            continue
        done.append((lead.id, account.name))
    db.session.commit()
    return done, refused


# ── the wider sweep: every unlinked open lead, grouped ───────────────
#
# `candidates()` above lists leads whose *name* is a placeholder.
# That was six leads. It missed the real backlog — on 29 October
# 2026, 293 open leads had no account and most carried a perfectly
# good company name, so nothing flagged them: the salesperson sees
# the lead on their own list and never notices the account is
# missing, while every by-account figure quietly undercounts.
#
# They arrive one enquiry at a time, so the same customer appears
# many times over under many spellings — Indo Tech Transformers
# eight times, ANDRITZ in four. Linking them one by one is the same
# decision taken eight times, so the sweep groups first and asks
# once.

#: Dropped from a name before two names are compared. A customer
#: writes "Pvt Ltd" one day and "Private Limited" the next; neither
#: says anything about who they are.
NOISE = {
    'pvt', 'private', 'ltd', 'limited', 'llp', 'inc', 'incorporated',
    'corp', 'corporation', 'co', 'company', 'gmbh', 'bv', 'spa',
    'plc', 'sa', 'ag', 'pte', 'sdn', 'bhd', 'kg', 'as', 'nv', 'oy',
    'srl', 'india', 'indian', 'group', 'holdings', 'the', 'and',
}


def stem(name):
    """A company name reduced to what distinguishes it.

    "ANDRITZ HYDRO Private Limited" and "Andritz Hydro Pvt. Ltd."
    both become "andritz hydro". "Indo-Tech" becomes "indo tech",
    which is a prefix of "indo tech transformers" — that is what
    lets the two merge below, and why the stem keeps word order.
    """
    raw = ''.join(c.lower() if c.isalnum() else ' '
                  for c in (name or ''))
    words = [w for w in raw.split() if w]
    kept = [w for w in words if w not in NOISE and len(w) > 1]
    if not kept:
        kept = [w for w in words if w not in NOISE] or words
    return ' '.join(kept)


def _merge_keys(keys):
    """{stem: the stem it belongs under}.

    A short name joins a longer one when the longer one simply
    continues it — "indo tech" into "indo tech transformers". Only
    when exactly one longer name continues it: "andritz" continues
    into both "andritz hydro" and "andritz schuler", and those are
    two different companies. Where it is ambiguous the short name
    keeps its own group and the ambiguity is reported, because a
    wrong merge is invisible afterwards.
    """
    keys = sorted(set(k for k in keys if k))
    home = {k: k for k in keys}
    for key in keys:
        tokens = key.split()
        longer = [k for k in keys
                  if k != key and k.split()[:len(tokens)] == tokens]
        if len(longer) == 1:
            home[key] = longer[0]
    # One hop is not enough: "indo" -> "indo tech" -> "indo tech
    # transformers" must land on the last of those.
    for key in keys:
        seen, at = {key}, home[key]
        while home.get(at, at) != at and at not in seen:
            seen.add(at)
            at = home[at]
        home[key] = at
    return home


OBVIOUS = 'OBVIOUS'
CHOOSE = 'CHOOSE'
NEW_ACCOUNT = 'NEW ACCOUNT NEEDED'
GROUP_VERDICTS = (OBVIOUS, CHOOSE, NEW_ACCOUNT)


def unlinked(lead_ids=()):
    """Every open lead with no account — owned or not."""
    from app import Lead, db
    from app.services import sales_rules

    if lead_ids:
        leads = [db.session.get(Lead, int(x)) for x in lead_ids]
        return [x for x in leads if x is not None]
    return (Lead.query
            .filter(Lead.company_id.is_(None),
                    Lead.is_archived.isnot(True),
                    Lead.stage.in_(sales_rules.open_stages()))
            .order_by(Lead.id.desc()).all())


def groups(lead_ids=()):
    """The unlinked backlog, one entry per customer rather than per
    lead, each with a verdict."""
    from app.services import lead_account as la

    leads = unlinked(lead_ids)
    read = []
    for lead in leads:
        read.append({'lead': lead,
                     'stem': stem(lead.company) or f'#{lead.id}',
                     'suggestions': la.suggest(lead, limit=3)})

    home = _merge_keys([r['stem'] for r in read])
    buckets = {}
    for row in read:
        key = home.get(row['stem'], row['stem'])
        buckets.setdefault(key, []).append(row)

    out = []
    for key, rows in buckets.items():
        accounts, spellings = {}, []
        for row in rows:
            name = (row['lead'].company or '').strip()
            if name and name not in spellings:
                spellings.append(name)
            for found in row['suggestions']:
                accounts.setdefault(found['account_id'], found)
        picked = list(accounts.values())
        # A resemblance is not a match. The matcher says so itself:
        # "these two strings are 60% alike" is useful to show a
        # person and no evidence at all. It reads identically to a
        # real name match in the rule text — both say "company
        # name" — so the first live run offered "Balaji Wafers" for
        # a lead read as "Balaji Power Tech", and called it OBVIOUS
        # because it was the only answer. One answer is not the same
        # as a confident one.
        solid = [a for a in picked if a.get('kind') != 'resemblance']
        if not picked:
            verdict = NEW_ACCOUNT
        elif len(picked) == 1 and solid:
            verdict = OBVIOUS
        else:
            verdict = CHOOSE
        out.append({
            'stem': key,
            'spellings': spellings,
            'lead_ids': sorted(r['lead'].id for r in rows),
            'leads': [r['lead'] for r in rows],
            'accounts': picked,
            # Named only when the verdict is OBVIOUS: anything
            # else is a question, and a question with an answer
            # already filled in gets agreed to rather than read.
            'account_id': (picked[0]['account_id']
                           if verdict == OBVIOUS else None),
            'name': (picked[0]['name'] if verdict == OBVIOUS
                     else (spellings[0] if spellings else key)),
            # Two accounts answering to one customer's name is a
            # duplicate-account question. It is listed, never acted
            # on: merging accounts is not this script's business.
            'duplicate_candidates': picked if len(picked) > 1 else [],
            # Nothing but resemblances: a person still has to look,
            # but this is not two accounts competing for one name.
            # The two need different words or the real duplicates
            # are lost among the guesses.
            'resemblance_only': bool(picked) and not solid,
            'verdict': verdict,
        })
    out.sort(key=lambda g: (GROUP_VERDICTS.index(g['verdict']),
                            -len(g['lead_ids']), g['stem']))
    return out


def group_counts(found):
    counts = {name: 0 for name in GROUP_VERDICTS}
    leads = {name: 0 for name in GROUP_VERDICTS}
    for group in found:
        counts[group['verdict']] += 1
        leads[group['verdict']] += len(group['lead_ids'])
    return counts, leads


def group_report(found):
    if not found:
        return 'Nothing unlinked. Every open lead has an account.'

    total = sum(len(g['lead_ids']) for g in found)
    out = [f'{total} open lead(s) with no account, in '
           f'{len(found)} customer(s):', '']
    for verdict in GROUP_VERDICTS:
        chosen = [g for g in found if g['verdict'] == verdict]
        if not chosen:
            continue
        out.append(f'── {verdict} ── {len(chosen)} customer(s), '
                   f'{sum(len(g["lead_ids"]) for g in chosen)} lead(s)')
        for group in chosen:
            out.append(f'  {group["name"]}  ·  '
                       f'{len(group["lead_ids"])} lead(s)')
            if len(group['spellings']) > 1:
                out.append('      written as: '
                           + '; '.join(group['spellings']))
            out.append('      leads: '
                       + ' '.join(str(i) for i in group['lead_ids']))
            if verdict == OBVIOUS:
                found_one = group['accounts'][0]
                out.append(f'      account: {found_one["name"]} '
                           f'(id {found_one["account_id"]}) — '
                           f'{found_one["rule"]}')
                out.append(f'      link:    --ids '
                           + ' '.join(str(i) for i in group['lead_ids'])
                           + f' --account {found_one["account_id"]} '
                             f'--actor <EMP> --apply')
            elif verdict == CHOOSE:
                for found_one in group['accounts']:
                    mark = (' [resemblance only]'
                            if found_one.get('kind') == 'resemblance'
                            else '')
                    out.append(f'      candidate: {found_one["name"]} '
                               f'(id {found_one["account_id"]}) — '
                               f'{found_one["rule"]}{mark}')
                if group['resemblance_only']:
                    out.append('      nothing but a resemblance — the '
                               'names are merely alike. Probably a new '
                               'account, not one of these.')
                else:
                    out.append('      more than one account answers to '
                               'this name. Pick one, or they are '
                               'duplicates — nothing here merges '
                               'accounts.')
            else:
                out.append('      no account matches — create one from '
                           'the lead sheet, then link the rest')
        out.append('')

    counts, leads = group_counts(found)
    out.append('By verdict:')
    for name in GROUP_VERDICTS:
        out.append(f'  {name:<20} {counts[name]:>4} customer(s)  '
                   f'{leads[name]:>4} lead(s)')
    out.append('')
    out.append('Nothing was changed. To link a group, copy its link '
               'line above.')
    return '\n'.join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--lead', action='append', default=[],
                    help='look at one lead, placeholder or not')
    ap.add_argument('--ids', nargs='*', default=[],
                    help='the leads to link, with --apply')
    ap.add_argument('--account', help='the account id to link to')
    ap.add_argument('--actor', help='who is doing this (emp code)')
    ap.add_argument('--apply', action='store_true',
                    help='write the links')
    ap.add_argument('--unlinked', action='store_true',
                    help='every open lead with no account, grouped by '
                         'customer — not only placeholder names')
    args = ap.parse_args()

    import app as main_app
    with main_app.app.app_context():
        if not args.apply:
            if args.unlinked:
                print(group_report(groups(args.lead or args.ids)))
            else:
                print(report(candidates(args.lead or args.ids)))
            return 0

        if not args.ids or not args.account or not args.actor:
            print('--apply needs --ids, --account and --actor. '
                  'Nothing was changed.')
            return 2
        done, refused = apply(args.ids, account_id=args.account,
                              actor=args.actor)
        for lead_id, name in done:
            print(f'linked  #{lead_id} -> {name}')
        for lead_id, why in refused:
            print(f'refused #{lead_id}: {why}')
        print(f'\n{len(done)} linked, {len(refused)} refused.')
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
