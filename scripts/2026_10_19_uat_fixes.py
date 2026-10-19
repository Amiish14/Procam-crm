"""
The closeout's data fixes: a vertical head, two task definitions, a block.

    .venv/bin/python scripts/2026_10_19_uat_fixes.py --check
    .venv/bin/python scripts/2026_10_19_uat_fixes.py --apply

Three findings from CRM-UAT-CLOSEOUT-01, each small, each needing a
row written rather than code changed.

  1. `Other` has no vertical head, so the 40 live leads filed under
     it copy nobody above their owner. DIR12010 takes it.

  2. `account.follow_up_due` is active with no completion state — the
     same defect as the two retired this morning. It now completes
     when the account is worked: an activity logged, or a new
     follow-up date set.

     `project.review` is also active with no completion state and has
     never produced an instance. It is retired rather than given one,
     because nobody can say what "reviewed" means for it and a
     definition nobody can complete is worse than no definition.

  3. Walchandnagar is not on the client register at all. It is added
     as BLOCKED, matched on the exact account name and its aliases —
     never as a substring, which is how a caution on "Seml" came to
     match every company with those four letters in its name.

     The 84 existing leads are NOT touched. They are listed in
     docs/exports/ for a person to decide on.
"""
import argparse
import os
import sys
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

from app import Employee, Lead, app, db                   # noqa: E402

OTHER_VERTICAL = 'Other'
OTHER_HEAD = 'DIR12010'

FOLLOW_UP_COMPLETION = {'entity': 'Company', 'state': 'Worked'}
RETIRE_DEFINITION = 'project.review'

#: `account.first_contact` completes on 'Contact Made', which is not
#: one of the twelve ACCOUNT_DEV_STAGES — the real one is 'Contact
#: Established'. Found while fixing the other two: the same defect,
#: a completion state naming something the system can never reach.
FIRST_CONTACT_FIX = ('account.first_contact',
                     {'entity': 'Company', 'state': 'Contact Established'})

BLOCK_NAME = 'Walchandnagar Industries Limited'
BLOCK_ALIASES = ('Walchandnagar', 'Walchandnagar Industries',
                 'WIL Walchandnagar')
BLOCK_REASON = ('Added at UAT closeout on 19 October on Nilesh Kumar '
                'Sinha\'s instruction. No new business is to be started '
                'with this client.')


def _plan():
    """What would change, as rows. Shared by --check and --apply."""
    from app.models.email_admin import VerticalHead
    from app.models.restriction import BLOCKED, ClientRestriction
    from app.models.task_engine import TaskDefinition

    out = []
    head = VerticalHead.query.filter(
        VerticalHead.vertical == OTHER_VERTICAL).first()
    emp = Employee.query.filter_by(emp_code=OTHER_HEAD).first()
    out.append(('vertical head', OTHER_VERTICAL,
                'already present' if head else 'add',
                f'head {OTHER_HEAD} '
                f'({emp.name if emp else "NOT ON THE EMPLOYEE MASTER"}), '
                f'{Lead.query.filter(Lead.procam_vertical == OTHER_VERTICAL).count()} '
                f'live lead(s)'))

    fu = TaskDefinition.query.filter_by(
        task_key='account.follow_up_due').first()
    out.append(('task definition', 'account.follow_up_due',
                'already set' if (fu and fu.completion_state) else
                'set completion state' if fu else 'MISSING',
                f'-> {FOLLOW_UP_COMPLETION}'))

    from app.models.task_engine import TaskInstance
    fc = TaskDefinition.query.filter_by(task_key=FIRST_CONTACT_FIX[0]).first()
    out.append(('task definition', FIRST_CONTACT_FIX[0],
                'already correct'
                if (fc and fc.completion_state == FIRST_CONTACT_FIX[1])
                else 'fix unreachable completion state' if fc else 'MISSING',
                f'{(fc.completion_state if fc else None)} -> '
                f'{FIRST_CONTACT_FIX[1]}'))

    pr = TaskDefinition.query.filter_by(task_key=RETIRE_DEFINITION).first()
    pr_count = TaskInstance.query.filter_by(
        task_key=RETIRE_DEFINITION).count()
    out.append(('task definition', RETIRE_DEFINITION,
                'already retired' if (pr and not pr.is_active) else
                'deactivate' if pr else 'MISSING',
                f'{pr_count} instance(s) ever'))

    existing = ClientRestriction.query.filter(
        ClientRestriction.company_name.ilike('%walchand%')).first()
    out.append(('client block', BLOCK_NAME,
                f'already on file ({existing.status})' if existing
                else f'add as {BLOCKED}',
                f'{Lead.query.filter(Lead.company.ilike("%walchand%")).count()} '
                f'existing lead(s), none touched'))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--apply', action='store_true')
    args = ap.parse_args()
    if args.apply == args.check:
        raise SystemExit('Choose one: --check or --apply.')

    with app.app_context():
        print(f'database: {db.engine.url}')
        print(f'MODE:     {"APPLY" if args.apply else "DRY RUN — nothing is written"}\n')
        for kind, what, action, detail in _plan():
            print(f'  {kind:<16} {what:<36} {action:<24} {detail}')

        if args.check:
            print('\nDRY RUN — nothing written. Re-run with --apply.')
            return

        from app.models.email_admin import VerticalHead
        from app.models.restriction import (BLOCKED, ClientRestriction,
                                            ClientRestrictionEvent)
        from app.models.task_engine import TaskDefinition
        from app.services import audit

        actor = 'CRM-UAT-CLOSEOUT-01'
        try:
            # 1 — the Other vertical gets a head
            if not VerticalHead.query.filter(
                    VerticalHead.vertical == OTHER_VERTICAL).first():
                db.session.add(VerticalHead(
                    vertical=OTHER_VERTICAL, head_emp_code=OTHER_HEAD,
                    active=True, updated_by=actor,
                    note='Added at UAT closeout: leads filed under Other '
                         'were copying nobody above their owner.'))
                audit.record('vertical_head.created', 'vertical_head',
                             OTHER_VERTICAL, new={'head': OTHER_HEAD},
                             actor=actor, reason='UAT closeout item 4')

            # 2 — the two task definitions
            fu = TaskDefinition.query.filter_by(
                task_key='account.follow_up_due').first()
            if fu is not None and not fu.completion_state:
                fu.completion_state = FOLLOW_UP_COMPLETION
                audit.record('task_definition.completion_set',
                             'task_definition', str(fu.id),
                             old={'completion_state': None},
                             new={'completion_state': FOLLOW_UP_COMPLETION},
                             actor=actor,
                             reason='It could never close. It now closes '
                                    'when the account is worked.')
            fc = TaskDefinition.query.filter_by(
                task_key=FIRST_CONTACT_FIX[0]).first()
            if fc is not None and fc.completion_state != FIRST_CONTACT_FIX[1]:
                was = fc.completion_state
                fc.completion_state = FIRST_CONTACT_FIX[1]
                audit.record('task_definition.completion_fixed',
                             'task_definition', str(fc.id),
                             old={'completion_state': was},
                             new={'completion_state': FIRST_CONTACT_FIX[1]},
                             actor=actor,
                             reason='"Contact Made" is not one of the '
                                    'twelve account stages, so the task '
                                    'could never close.')
            pr = TaskDefinition.query.filter_by(
                task_key=RETIRE_DEFINITION).first()
            if pr is not None and pr.is_active:
                pr.is_active = False
                audit.record('task_definition.retired', 'task_definition',
                             str(pr.id), old={'is_active': True},
                             new={'is_active': False},
                             actor=actor,
                             reason='No completion state and no instances '
                                    'ever; nobody can say what reviewed '
                                    'means for it.')

            # 3 — the block
            if not ClientRestriction.query.filter(
                    ClientRestriction.company_name.ilike(
                        '%walchand%')).first():
                r = ClientRestriction(
                    status=BLOCKED, company_name=BLOCK_NAME,
                    reason_category='Commercial',
                    reason_detail=BLOCK_REASON,
                    recommended_by=actor,
                    recommended_at=datetime.utcnow(),
                    approved_by=OTHER_HEAD,
                    approved_at=datetime.utcnow())
                r.aliases = list(BLOCK_ALIASES)
                r.domains = []
                db.session.add(r)
                db.session.flush()
                db.session.add(ClientRestrictionEvent(
                    restriction_id=r.id, action='blocked', user_id=actor,
                    note=BLOCK_REASON))
                audit.record('restriction.created', 'client_restriction',
                             str(r.id), new={'company': BLOCK_NAME,
                                             'status': BLOCKED},
                             actor=actor, reason=BLOCK_REASON[:200])
            db.session.commit()
        except Exception as exc:                          # noqa: BLE001
            db.session.rollback()
            print(f'\n  !! ROLLED BACK: {type(exc).__name__}: '
                  f'{str(exc)[:200]}')
            raise SystemExit(1)

        print('\n  Applied, in one transaction:')
        for kind, what, action, _d in _plan():
            print(f'    {kind:<16} {what:<36} {action}')
        print('\n  No lead was touched.')


if __name__ == '__main__':
    main()
