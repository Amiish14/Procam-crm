"""
Leads whose vertical was silently rewritten to Heavy Transport.

The lead edit form offered four verticals. Leads carry eleven. Opening
a Freight Forwarding or Customs lead and pressing Save — even to
change only the follow-up date — posted option zero and rewrote the
vertical to Heavy Transport, with no warning and no confirmation.

REPORT ONLY. This writes nothing. It produces a CSV for a person to
read and decide on, because which vertical a lead belongs to is a
business judgement and a script that guessed would be making the same
mistake the form made, in the other direction.

    .venv/bin/python scripts/vertical_rewrite_report.py
    .venv/bin/python scripts/vertical_rewrite_report.py --csv /tmp/vr.csv
    .venv/bin/python scripts/vertical_rewrite_report.py --since 2026-09-01

How a lead is suspected
    It is on Heavy Transport now, and its own words say otherwise:
    the original email subject or body, the project, or the notes
    mention customs, freight forwarding, FCL/LCL, air waybill and so
    on. Confidence is low, medium or high by how much agrees.

    The audit trail is used where there is one — a recorded change of
    `procam_vertical` to Heavy Transport by a person is much stronger
    evidence than keywords, and is marked as such.
"""
import argparse
import csv
import os
import re
import sys
from datetime import date, datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from app import Lead, app, db                             # noqa: E402

TARGET = 'Heavy Transport'
DEFAULT_SINCE = date(2026, 9, 1)

#: Words that say which vertical a lead really belongs to. Deliberately
#: narrow: a false positive here costs somebody a careful read of a
#: lead that was fine.
HINTS = (
    ('Customs', (r'\bcustoms?\b', r'\bclearance\b', r'\bbill of entry\b',
                 r'\bBOE\b', r'\bshipping bill\b', r'\bICEGATE\b')),
    ('Freight Forwarding', (r'\bfreight forward', r'\bforwarder\b',
                            r'\bCHA\b', r'\bconsolidat')),
    ('Sea Freight', (r'\bFCL\b', r'\bLCL\b', r'\bCBM\b', r'\bbill of lading\b',
                     r'\bB/?L\b', r'\bHBL\b', r'\bMBL\b', r'\bnhava sheva\b',
                     r'\bport of loading\b', r'\bPOL\b', r'\bPOD\b',
                     r'\bDAP\b', r'\bCIF\b', r'\bFOB\b')),
    ('Air Freight', (r'\bair freight\b', r'\bairfreight\b', r'\bAWB\b',
                     r'\bairway ?bill\b')),
    ('Warehousing', (r'\bwarehous', r'\bgodown\b', r'\bstorage\b',
                     r'\bpallet')),
)

#: Words that genuinely mean Heavy Transport. Their presence does not
#: clear a lead, but it lowers the confidence — a job can be both.
HEAVY = (r'\bODC\b', r'\btrailer\b', r'\blow ?bed\b', r'\bcrane\b',
         r'\bhydraulic axle\b', r'\bSPMT\b', r'\bover ?dimension')


def _text(lead):
    parts = []
    for attr in ('project', 'original_email_subject', 'original_email_body',
                 'notes', 'products', 'opp_notes'):
        value = getattr(lead, attr, None)
        if value:
            parts.append(str(value))
    return ' \n '.join(parts)[:20000]


def _hits(text):
    found = {}
    for vertical, patterns in HINTS:
        n = sum(1 for p in patterns
                if re.search(p, text, re.I))
        if n:
            found[vertical] = n
    heavy = sum(1 for p in HEAVY if re.search(p, text, re.I))
    return found, heavy


def _audited(lead_id, since):
    """Was this lead's vertical changed to Heavy Transport by a person?"""
    try:
        from app.models.audit import AuditEvent
        rows = (AuditEvent.query
                .filter(AuditEvent.entity_type.in_(('Lead', 'lead')),
                        AuditEvent.entity_id == str(lead_id),
                        AuditEvent.occurred_at >= since)
                .order_by(AuditEvent.occurred_at.desc()).limit(40).all())
    except Exception:
        db.session.rollback()
        return None
    for r in rows:
        new = r.new_value if isinstance(r.new_value, dict) else {}
        old = r.old_value if isinstance(r.old_value, dict) else {}
        if (new.get('procam_vertical') == TARGET
                and old.get('procam_vertical')
                and old.get('procam_vertical') != TARGET):
            return {'when': str(r.occurred_at)[:16], 'by': r.actor or '',
                    'from': old.get('procam_vertical')}
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--since', default=str(DEFAULT_SINCE))
    ap.add_argument('--csv')
    ap.add_argument('--limit', type=int, default=5000)
    args = ap.parse_args()
    since = datetime.fromisoformat(args.since)

    with app.app_context():
        print(f'database: {db.engine.url}')
        print(f'leads on {TARGET}, updated since {since.date()}\n')

        leads = (Lead.query
                 .filter(Lead.procam_vertical == TARGET,
                         Lead.is_archived.isnot(True),
                         Lead.updated_at >= since)
                 .order_by(Lead.updated_at.desc())
                 .limit(args.limit).all())
        print(f'{len(leads)} to examine')

        rows = []
        for lead in leads:
            found, heavy = _hits(_text(lead))
            audit = _audited(lead.id, since)
            if not found and not audit:
                continue
            best = max(found.items(), key=lambda kv: kv[1])[0] if found else ''
            score = sum(found.values())
            if audit:
                confidence = 'high — recorded change'
            elif score >= 3 and not heavy:
                confidence = 'high'
            elif score >= 2 or (score and not heavy):
                confidence = 'medium'
            else:
                confidence = 'low'
            rows.append({
                'lead_id': lead.id,
                'company': (lead.company or '')[:60],
                'stage': lead.stage or '',
                'pic': lead.assigned_to or '',
                'updated_at': str(lead.updated_at)[:16],
                'now': TARGET,
                'looks_like': best,
                'keyword_hits': score,
                'heavy_transport_hits': heavy,
                'confidence': confidence,
                'changed_from': (audit or {}).get('from', ''),
                'changed_by': (audit or {}).get('by', ''),
                'changed_at': (audit or {}).get('when', ''),
            })

        rows.sort(key=lambda r: (r['confidence'].startswith('low'),
                                 -r['keyword_hits']))
        print(f'{len(rows)} suspected\n')
        print(f'  {"lead":<8} {"company":<32} {"looks like":<20} '
              f'{"conf":<22} changed')
        for r in rows[:80]:
            changed = (f'{r["changed_from"]} -> {TARGET} by '
                       f'{r["changed_by"]} on {r["changed_at"]}'
                       if r['changed_from'] else '')
            print(f'  {r["lead_id"]:<8} {r["company"][:32]:<32} '
                  f'{r["looks_like"][:20]:<20} {r["confidence"]:<22} '
                  f'{changed}')
        if len(rows) > 80:
            print(f'  ... and {len(rows) - 80} more (use --csv for all)')

        if args.csv and rows:
            with open(args.csv, 'w', newline='', encoding='utf-8') as fh:
                w = csv.DictWriter(fh, fieldnames=list(rows[0]))
                w.writeheader()
                w.writerows(rows)
            print(f'\n  written to {args.csv}')

        print('\nREPORT ONLY — no lead was changed. Which vertical a lead '
              'belongs to is a business decision.')


if __name__ == '__main__':
    main()
