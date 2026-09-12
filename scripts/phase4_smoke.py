"""
Does the Phase 4 model actually help? — read-only.

Replays real classifications that landed in the consult band back
through the model and prints what it would have said. Nothing is
written: this is here so the model can be judged before it is trusted,
rather than a month after.

Where a human has already corrected a row, that correction is the
answer, and the run scores the rules and the model against it.

    python3 scripts/phase4_smoke.py                 # 25 recent
    python3 scripts/phase4_smoke.py --limit 100
    python3 scripts/phase4_smoke.py --corrected     # only judged rows
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault('LEAD_INTAKE_AI', 'on')

from app import app, EmailClassification                       # noqa: E402
from app.services import lead_intake as li                     # noqa: E402
from app.services import lead_intake_ai as ai                  # noqa: E402


def as_message(row):
    """The stored payload back into the shape the classifier reads."""
    p = row.payload or {}
    return {
        'subject': row.subject or '',
        'body': {'content': p.get('body') or '', 'contentType': 'text'},
        'from': {'emailAddress': {'address': row.from_addr or ''}},
        'toRecipients': [{'emailAddress': {'address': a}}
                         for a in (p.get('to') or [])],
        'ccRecipients': [{'emailAddress': {'address': a}}
                         for a in (p.get('cc') or [])],
        'attachments': [{'name': n} for n in (p.get('attachments') or [])],
        '_resolved_sender': p.get('resolved_sender') or '',
        '_forward_resolved': bool(p.get('forward_resolved')),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--limit', type=int, default=25)
    ap.add_argument('--corrected', action='store_true',
                    help='only rows a human has already ruled on')
    args = ap.parse_args()

    with app.app_context():
        if not ai.is_enabled():
            print('Phase 4 is off. Needs LEAD_INTAKE_AI=on and a key.')
            return 1

        q = (EmailClassification.query
             .filter(EmailClassification.confidence.isnot(None),
                     EmailClassification.confidence >= ai.CONSULT_ABOVE,
                     EmailClassification.confidence < ai.CONSULT_BELOW))
        if args.corrected:
            q = q.filter(EmailClassification.corrected_to.isnot(None))
        rows = q.order_by(EmailClassification.id.desc()).limit(args.limit).all()

        if not rows:
            print('Nothing in the 25–79 band to replay.')
            return 0

        print(f'Replaying {len(rows)} classifications through the model.\n')
        agree = asked = 0
        rules_right = model_right = judged = 0

        for r in rows:
            op = ai.opinion(as_message(r))
            asked += 1
            if op is None:
                print(f'  #{r.id:<6} {"no answer":<14} {(r.subject or "")[:58]}')
                continue

            same = op.klass == r.classification
            agree += same
            applied = op.confidence >= 70 and not same
            mark = '=' if same else ('→' if applied else '·')
            print(f'  #{r.id:<6} {r.classification:<14} {mark} '
                  f'{op.klass:<14} {op.confidence:>3}%  '
                  f'{(r.subject or "")[:44]}')
            if not same:
                print(f'         {op.reason[:96]}')

            if r.corrected_to:
                judged += 1
                rules_right += (r.classification == r.corrected_to)
                # only an applied opinion would have changed the outcome
                final = op.klass if applied else r.classification
                model_right += (final == r.corrected_to)

        print(f'\n  asked          {asked}')
        print(f'  agreed         {agree}  ({100 * agree // max(asked, 1)}%)')
        print('  "→" would have changed the outcome; "·" recorded only.')
        if judged:
            print(f'\n  Against {judged} human decisions:')
            print(f'    rules alone      {rules_right}/{judged}')
            print(f'    rules + model    {model_right}/{judged}')
            if model_right < rules_right:
                print('\n  The model is doing harm here. Take '
                      'LEAD_INTAKE_AI back out of .env.')
        else:
            print('\n  No corrected rows in this sample, so nothing is '
                  'scored — only agreement is shown, and agreement is '
                  'not accuracy.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
