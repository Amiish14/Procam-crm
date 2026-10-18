"""
Where each of the 134 vertical rewrites came from.

READ-ONLY. Answers four questions the workbook cannot:

  1. Was it a person at the lead edit form, or a script, import or
     bulk run? `audit_events` records `ip` and `user_agent` whenever
     there was a request behind the change, and nothing when there
     was not. A browser leaves both; a cron job, a migration and a
     shell script leave neither.

  2. Did the saves cluster? Changes seconds apart, in a run, are one
     operation somebody started — not sixty separate decisions. The
     form defect produces the opposite: scattered single saves, each
     one a person editing a lead for an unrelated reason.

  3. For DIR12010, who made 81 of the 134 — how many were the form
     and how many were a bulk run.

  4. Which of the old values are aliases rather than verticals in
     their own right. Restoring a lead to an alias writes a synonym
     the CRM maps straight back, which is what "Transportation"
     would do. The alias table is read live rather than assumed: an
     administrator may have added more since the migration.

    .venv/bin/python scripts/vertical_rewrite_provenance.py
    .venv/bin/python scripts/vertical_rewrite_provenance.py --seconds 120
"""
import argparse
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from app import app, db                                   # noqa: E402
from app.services import vertical_audit as va             # noqa: E402

#: Saves closer together than this, by the same person, are one run.
CLUSTER_SECONDS = 90


def aliases_live():
    """{alias: vertical} as the CRM holds it right now."""
    try:
        from app.models.email_admin import VerticalAlias
        return {a.alias: a.vertical for a in VerticalAlias.query.all()}
    except Exception:
        db.session.rollback()
        return {}


def managed_verticals():
    try:
        from app.models.email_admin import VerticalHead
        return sorted({v.vertical for v in VerticalHead.query.all()})
    except Exception:
        db.session.rollback()
        return []


def live_lead_verticals():
    """{vertical: how many live leads carry it} — what is in use."""
    from app import Lead
    rows = (db.session.query(Lead.procam_vertical, db.func.count(Lead.id))
            .filter(Lead.is_archived.isnot(True),
                    Lead.procam_vertical.isnot(None))
            .group_by(Lead.procam_vertical).all())
    return {v: n for v, n in rows if v}


def the_event(lead_id, since):
    """The audit row that rewrote this lead's vertical to the target."""
    from app.models.audit import AuditEvent

    rows = (AuditEvent.query
            .filter(AuditEvent.entity_type.in_(('Lead', 'lead')),
                    AuditEvent.entity_id == str(lead_id),
                    AuditEvent.occurred_at >= since)
            .order_by(AuditEvent.occurred_at.desc()).limit(60).all())
    for r in rows:
        if (r.action or '').endswith(('.create', '.delete')):
            continue
        new = r.new_value if isinstance(r.new_value, dict) else {}
        old = r.old_value if isinstance(r.old_value, dict) else {}
        if 'procam_vertical' not in new or 'procam_vertical' not in old:
            continue
        if new.get('procam_vertical') != va.TARGET:
            continue
        if not old.get('procam_vertical') or \
                old.get('procam_vertical') == va.TARGET:
            continue
        return r
    return None


def source_of(ev):
    """'form' | 'script' — and what the evidence was."""
    if ev is None:
        return 'unknown', 'no audit row'
    ua = (ev.user_agent or '').strip()
    ip = (ev.ip or '').strip()
    if ua or ip:
        return 'form', f'{ua[:40] or "no agent"} from {ip or "no ip"}'
    return 'script', 'no request behind it — a job, import or shell'


def fields_changed(ev):
    """How many fields moved in the same save."""
    new = ev.new_value if isinstance(ev.new_value, dict) else {}
    return sorted(new.keys())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--since', default='2026-09-01')
    ap.add_argument('--seconds', type=int, default=CLUSTER_SECONDS)
    args = ap.parse_args()
    since = datetime.fromisoformat(args.since)

    with app.app_context():
        print(f'database: {db.engine.url}')
        print('MODE:     READ-ONLY — nothing is written\n')

        # ── 4. the alias table, live ──
        alias = aliases_live()
        managed = managed_verticals()
        in_use = live_lead_verticals()
        print('=' * 72)
        print('ALIASES AND VERTICALS, AS THE CRM HOLDS THEM NOW')
        print('=' * 72)
        print(f'  managed verticals: {", ".join(managed) or "(none)"}')
        print(f'  alias rows ({len(alias)}):')
        for a, v in sorted(alias.items()):
            print(f'      {a:<24} -> {v}')
        if not alias:
            print('      (none)')

        data = va.survey(since=args.since)
        recorded = data['recorded']
        print(f'\n  old values on the {len(recorded)} recorded rewrites, '
              f'and what each one is:')
        verdicts = {}
        for value, n in Counter(r['restore_to'] for r in recorded).most_common():
            if value in alias:
                verdict = f'ALIAS of {alias[value]} — do not restore'
            elif value in managed:
                verdict = 'a managed vertical — restore'
            elif value in in_use:
                verdict = (f'a vertical in live use ({in_use[value]} leads), '
                           f'not an alias — restore')
            else:
                verdict = 'not managed and not in use — needs a decision'
            verdicts[value] = verdict
            print(f'      {value:<22} {n:>4}   {verdict}')

        # ── 1 and 2. provenance and clustering ──
        print('\n' + '=' * 72)
        print('WHERE EACH REWRITE CAME FROM')
        print('=' * 72)
        rows = []
        for r in recorded:
            ev = the_event(r['lead_id'], since)
            src, why = source_of(ev)
            rows.append({**r, 'event': ev, 'source': src, 'why': why,
                         'at': ev.occurred_at if ev else None,
                         'fields': fields_changed(ev) if ev else []})

        print(f'  {"actor":<14} {"form":>6} {"script":>8} {"unknown":>9}')
        by_actor = defaultdict(Counter)
        for r in rows:
            by_actor[r['rewritten_by']][r['source']] += 1
        for actor, c in sorted(by_actor.items(),
                               key=lambda kv: -sum(kv[1].values())):
            print(f'  {actor:<14} {c["form"]:>6} {c["script"]:>8} '
                  f'{c["unknown"]:>9}')

        print('\n  what else moved in the same save:')
        alone = sum(1 for r in rows if r['fields'] == ['procam_vertical'])
        print(f'      vertical ALONE              {alone:>4}   '
              f'a deliberate change, most likely')
        print(f'      vertical WITH other fields  {len(rows) - alone:>4}   '
              f'the defect\'s signature')
        other = Counter()
        for r in rows:
            for f in r['fields']:
                if f != 'procam_vertical':
                    other[f] += 1
        for f, n in other.most_common(8):
            print(f'          alongside {f:<26} {n:>4}')

        # ── clustering ──
        print(f'\n  runs — saves by one person within {args.seconds}s '
              f'of each other:')
        timed = sorted((r for r in rows if r['at']),
                       key=lambda r: (r['rewritten_by'], r['at']))
        clusters, current = [], []
        for r in timed:
            if current and r['rewritten_by'] == current[-1]['rewritten_by'] \
                    and (r['at'] - current[-1]['at']).total_seconds() \
                    <= args.seconds:
                current.append(r)
            else:
                if len(current) > 1:
                    clusters.append(current)
                current = [r]
        if len(current) > 1:
            clusters.append(current)

        in_a_run = sum(len(c) for c in clusters)
        print(f'      {in_a_run} of {len(rows)} are in a run; '
              f'{len(rows) - in_a_run} are on their own')
        for c in sorted(clusters, key=lambda c: -len(c))[:12]:
            span = (c[-1]['at'] - c[0]['at']).total_seconds()
            print(f'      {c[0]["rewritten_by"]:<12} {len(c):>3} leads in '
                  f'{span:>6.0f}s from {str(c[0]["at"])[:16]} '
                  f'({c[0]["source"]})')

        # ── 3. DIR12010 ──
        print('\n' + '=' * 72)
        print('DIR12010')
        print('=' * 72)
        mine = [r for r in rows if r['rewritten_by'] == 'DIR12010']
        mine_run = {id(r) for c in clusters for r in c
                    if r['rewritten_by'] == 'DIR12010'}
        print(f'  {len(mine)} rewrites')
        print(f'      from the form        '
              f'{sum(1 for r in mine if r["source"] == "form"):>4}')
        print(f'      from a script/job    '
              f'{sum(1 for r in mine if r["source"] == "script"):>4}')
        print(f'      inside a run         {len(mine_run):>4}')
        print(f'      single, on their own {len(mine) - len(mine_run):>4}')
        print(f'      vertical alone       '
              f'{sum(1 for r in mine if r["fields"] == ["procam_vertical"]):>4}')

        # ── the final count ──
        print('\n' + '=' * 72)
        print('WHAT STAGE B WOULD RESTORE')
        print('=' * 72)
        restore = [r for r in recorded
                   if r['restore_to'] not in alias]
        held = [r for r in recorded if r['restore_to'] in alias]
        print(f'  restore            {len(restore):>4}')
        print(f'  held back (alias)  {len(held):>4}')
        for value, n in Counter(r['restore_to'] for r in held).most_common():
            print(f'      {value:<22} {n:>4} -> {alias[value]} '
                  f'(already the value they carry)')
        print(f'\n  FINAL RESTORE COUNT: {len(restore)}')
        print('\nREAD-ONLY. Nothing was written.')


if __name__ == '__main__':
    main()
