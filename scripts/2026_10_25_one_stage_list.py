"""
Move the leads that sat on stages the CRM no longer has.

    .venv/bin/python scripts/2026_10_25_one_stage_list.py --check
    .venv/bin/python scripts/2026_10_25_one_stage_list.py --apply
    .venv/bin/python scripts/2026_10_25_one_stage_list.py --down --yes

Two populations, decided 25 October:

  * the alias stages — Profile Sent, Appointment, Visit Done — fold
    onto the journey stage they already sat beside (stage_map.ALIASES).
    Nobody moves forward or back; the lead page drew them at that
    point on the line already.

  * "Not Interested" becomes Lost with reason "Not interested", the
    same conversion R2 made. Whether these three are leftovers or
    were created afterwards is reported by --check rather than
    assumed, because the answer changes whether the ingest still
    writes that stage.

An audit row each, and --down puts every stage back from the audit
trail, so this is reversible rather than merely careful.
"""
import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from app import Lead, app, db                             # noqa: E402
from app.services import stage_map                        # noqa: E402

ACTION = 'lead.stage_list_unified'
RETIRED_STAGE = 'Not Interested'
LOST_REASON = 'Not interested'


def plan():
    """[(lead, new_stage, why)] for everything that has to move."""
    out = []
    for stage, lands_on in stage_map.ALIASES.items():
        for lead in Lead.query.filter(Lead.stage == stage).all():
            out.append((lead, lands_on,
                        f'{stage} folds onto {lands_on} — the lead '
                        f'page already drew it there'))
    for lead in Lead.query.filter(Lead.stage == RETIRED_STAGE).all():
        out.append((lead, 'Lost',
                    'Not Interested is Lost with a reason, as decided '
                    'in R2'))
    return out


def migration_date():
    """When the R2 Not-Interested conversion ran, or None.

    Read from the audit trail rather than hardcoded: "were these
    created after the migration" is a question about what actually
    happened, and a date typed into this file would answer it with
    my memory instead of the record.
    """
    from app.models.audit import AuditEvent
    row = (AuditEvent.query
           .filter(AuditEvent.action.like('%not_interested%'))
           .order_by(AuditEvent.id.desc()).first())
    return getattr(row, 'created_at', None)


def apply_plan(rows, *, actor='CRM-ACCOUNT-FIRST-01'):
    from app.services import audit

    for lead, new_stage, why in rows:
        was = lead.stage
        lead.stage = new_stage
        changes = {'stage': new_stage}
        if new_stage == 'Lost' and not (lead.lost_reason or '').strip():
            lead.lost_reason = LOST_REASON
            changes['lost_reason'] = LOST_REASON
        audit.record(ACTION, 'lead', lead.id, old={'stage': was},
                     new=changes, actor=actor, reason=why)
    return rows


def undo(*, actor='CRM-ACCOUNT-FIRST-01'):
    from app.models.audit import AuditEvent
    from app.services import audit

    back = 0
    for ev in AuditEvent.query.filter(
            AuditEvent.action == ACTION,
            AuditEvent.entity_type == 'lead').all():
        try:
            lead = db.session.get(Lead, int(ev.entity_id))
        except (TypeError, ValueError):
            continue
        if lead is None:
            continue
        was = (ev.old_value or {}).get('stage')
        if not was:
            continue
        lead.stage = was
        if (ev.new_value or {}).get('lost_reason') == LOST_REASON:
            lead.lost_reason = None
        audit.record('lead.stage_list_reverted', 'lead', lead.id,
                     new={'stage': was}, actor=actor,
                     reason='stage-list unification reverted')
        back += 1
    return back


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--down', action='store_true')
    ap.add_argument('--yes', action='store_true')
    args = ap.parse_args()
    if len([a for a in (args.check, args.apply, args.down) if a]) != 1:
        raise SystemExit('Choose one: --check, --apply or --down.')

    with app.app_context():
        print(f'database: {db.engine.url}')
        if args.down:
            if not args.yes:
                raise SystemExit('--down needs --yes.')
            print(f'  {undo()} lead(s) put back')
            db.session.commit()
            return

        rows = plan()
        print(f'  MODE: {"APPLY" if args.apply else "DRY RUN"}\n')
        print(f'  leads to move: {len(rows)}')
        for lead, new_stage, _why in rows:
            print(f'    #{lead.id:<7}{(lead.company or "")[:26]:<28}'
                  f'{lead.stage:<18} -> {new_stage}')

        retired = [r for r in rows if r[0].stage == RETIRED_STAGE]
        if retired:
            when = migration_date()
            print(f'\n  the {len(retired)} on {RETIRED_STAGE}:')
            if when is None:
                print('    no audit row for the R2 conversion, so '
                      'whether these pre-date it cannot be read off '
                      'the record. Not guessed at.')
            else:
                for lead, _s, _w in retired:
                    made = lead.created_at
                    after = made is not None and made > when
                    print(f'    #{lead.id} created {str(made)[:16]} — '
                          f'{"AFTER" if after else "before"} the '
                          f'conversion on {str(when)[:16]}')
                if any(l.created_at and l.created_at > when
                       for l, _s, _w in retired):
                    print('    At least one was created after the '
                          'conversion, so something still writes '
                          'this stage — worth finding before it '
                          'writes more.')

        if args.check:
            print('\nDRY RUN — nothing written.')
            return

        apply_plan(rows)
        db.session.commit()
        print(f'\n  {len(rows)} moved, an audit row each. '
              f'`--down --yes` puts them back.')


if __name__ == '__main__':
    main()
