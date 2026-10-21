"""
A review task on a restored lead somebody should look at.

    .venv/bin/python scripts/restore_review_tasks.py --check \
        --company SIEMENS
    .venv/bin/python scripts/restore_review_tasks.py --apply \
        --company SIEMENS

The restore deliberately creates no tasks: 830 leads arriving as
Lost must not generate 830 pieces of work about deals that ended
weeks ago. A handful are different. Four SIEMENS leads were at
Proposal Due and Negotiation Due when somebody removed them, and
435 of the restored carry no reason at all — if any of those four
were deleted in error, Lost is not where they belong.

So the task is raised by name, for the leads a person has looked
at and asked about. Owner first; where the lead has none, the head
of its vertical, because an unowned task is a task nobody does.
"""
import argparse
import os
import sys
from datetime import datetime, timedelta

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from app import Lead, app, db                             # noqa: E402

TASK_KEY = 'lead.restored_review'
TITLE = 'Restored after deletion — reopen if still live'
#: Long enough not to be noise, short enough to still matter.
DUE_DAYS = 7
#: The audit row the restore writes. Only leads it put back are
#: eligible: a task on a lead nobody restored would make no sense.
RESTORED = 'lead.restored_after_deletion'


def restored_ids():
    from app.models.audit import AuditEvent

    out = set()
    for ev in AuditEvent.query.filter_by(
            action=RESTORED, entity_type='lead').all():
        try:
            out.add(int(ev.entity_id))
        except (TypeError, ValueError):
            continue
    return out


def targets(company=None, ids=None):
    q = Lead.query.filter(Lead.id.in_(restored_ids() or {0}))
    if ids:
        q = q.filter(Lead.id.in_(ids))
    if company:
        q = q.filter(Lead.company.ilike(f'%{company}%'))
    return q.order_by(Lead.id.asc()).all()


def owner_for(lead):
    """Whose task this is, and why. (emp_code, how)."""
    code = (getattr(lead, 'assigned_to', None) or '').strip()
    if code:
        return code, 'the lead’s owner'
    try:
        from app.services import vertical_heads
        heads = vertical_heads.heads_for_vertical(
            getattr(lead, 'procam_vertical', None))
        if heads:
            first = heads[0]
            code = getattr(first, 'emp_code', None) or str(first)
            return code, 'the head of its vertical'
    except Exception:                                     # noqa: BLE001
        pass
    return None, 'nobody — it has no owner and no vertical head'


def existing(lead_id):
    from app.models.task_engine import TaskInstance, TaskInstanceStatus

    return (TaskInstance.query
            .filter(TaskInstance.task_key == TASK_KEY,
                    TaskInstance.entity_type == 'Lead',
                    TaskInstance.entity_id == lead_id,
                    TaskInstance.status.in_(TaskInstanceStatus.OPEN))
            .first())


def make(lead, owner, *, actor='CRM-RESTORE'):
    from app.models.task_engine import TaskInstance, TaskInstanceStatus
    from app.services import audit

    row = TaskInstance(
        task_key=TASK_KEY, entity_type='Lead', entity_id=lead.id,
        entity_display=(lead.company or f'Lead #{lead.id}')[:120],
        owner_user_id=owner, status=TaskInstanceStatus.PENDING,
        priority=2,
        action_route=f'/?lead={lead.id}',
        due_at=datetime.utcnow() + timedelta(days=DUE_DAYS),
        notes=(f'{TITLE}. It was removed at stage '
               f'"{lead.lost_reason or ""}" and brought back as '
               f'Lost. If the enquiry is still live, reopen it; if '
               f'not, leave it closed.'))
    db.session.add(row)
    db.session.flush()
    audit.record('task.created', 'task', str(row.id),
                 new={'task_key': TASK_KEY, 'lead_id': lead.id,
                      'owner': owner},
                 actor=actor,
                 reason='a restored lead that was late-stage when it '
                        'was deleted')
    return row


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--company', default=None)
    ap.add_argument('--ids', default=None,
                    help='comma-separated lead ids')
    ap.add_argument('--actor', default='CRM-RESTORE')
    args = ap.parse_args()
    if args.check == args.apply:
        raise SystemExit('Choose one: --check or --apply.')
    if not (args.company or args.ids):
        raise SystemExit(
            'Name who: --company SIEMENS, or --ids 1,2,3. This does '
            'not raise tasks on every restored lead, on purpose.')

    ids = None
    if args.ids:
        ids = [int(x) for x in args.ids.split(',') if x.strip()]

    with app.app_context():
        print(f'database: {db.engine.url}')
        print(f'MODE:     {"APPLY" if args.apply else "DRY RUN"}\n')
        rows = targets(args.company, ids)
        if not rows:
            print('  Nothing matches among the restored leads.')
            return
        made, skipped = [], []
        for lead in rows:
            owner, how = owner_for(lead)
            already = existing(lead.id)
            flag = ('already has one' if already
                    else '' if owner else 'NO OWNER')
            print(f'  #{lead.id:<7}{(lead.company or "")[:30]:<32}'
                  f'{owner or "—":<12}{how:<26}{flag}')
            if already or not owner:
                skipped.append((lead, flag))
                continue
            if args.apply:
                made.append(make(lead, owner, actor=args.actor))

        if args.check:
            print(f'\n  {len(rows) - len(skipped)} task(s) would be '
                  f'created. DRY RUN — nothing written.')
            return
        db.session.commit()
        print(f'\n  {len(made)} task(s) created, due in {DUE_DAYS} '
              f'days, each with an audit row.')
        for lead, why in skipped:
            print(f'  #{lead.id} skipped: {why}')


if __name__ == '__main__':
    main()
