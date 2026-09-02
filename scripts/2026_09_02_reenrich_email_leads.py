#!/usr/bin/env python
"""
v2026-09-02 — Re-run the parser + AI enricher over existing email leads.

Leads ingested while the AI extractor was unavailable (Anthropic out of
credit, or before a GROQ_API_KEY was configured) are regex-only: no cargo
type, weight, dimensions, target date or AI summary. This script re-fetches
each message from the leads mailbox and rebuilds the enriched fields in
place. It never creates or deletes leads.

Two write modes:

    (default)   SAFE MERGE — fill fields that are empty, and always refresh
                email_extracted_json / opp_notes. Anything a human has
                already typed is left alone.

    --overwrite ALSO replace company / pic / email / phone from the fresh
                extraction. Use this to correct leads that were built by an
                older code path (e.g. ones showing an internal Procam
                address as the contact).

Selection:
    --only-regex   only leads whose opp_notes says ai_used=false (default)
    --all          every source='email' lead
    --ids 1,2,3    exactly these
    --since DATE   created on/after YYYY-MM-DD
    --limit N

Examples:
    python scripts/2026_09_02_reenrich_email_leads.py                 # preview
    python scripts/2026_09_02_reenrich_email_leads.py --apply
    python scripts/2026_09_02_reenrich_email_leads.py --apply --overwrite
"""
import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app, db, Lead                              # noqa: E402
from email_ingest import service as mail_service           # noqa: E402
from email_ingest import parser as email_parser            # noqa: E402
from email_ingest import enrich as enrich_mod              # noqa: E402
from email_ingest import ai_router                         # noqa: E402
from email_ingest.graph_client import GraphClient          # noqa: E402
from email_ingest.webhook import _get_message              # noqa: E402

# Fields --overwrite is allowed to replace. Everything else is merge-only.
_OVERWRITE_FIELDS = ('company', 'pic', 'designation_pic', 'email', 'phone',
                     'email2', 'phone2', 'procam_vertical')
_ALWAYS = ('email_extracted_json', 'opp_notes')


def ai_used(lead) -> bool:
    try:
        return bool(json.loads(lead.opp_notes or '{}').get('ai_used'))
    except Exception:
        return False


def select(args):
    q = Lead.query.filter(Lead.source == 'email',
                          Lead.email_message_id.isnot(None))
    if args.since:
        try:
            q = q.filter(Lead.created_at >= datetime.strptime(args.since, '%Y-%m-%d'))
        except ValueError:
            sys.exit(f'--since must be YYYY-MM-DD, got {args.since!r}')
    rows = q.order_by(Lead.id).all()
    if args.ids:
        want = {int(x) for x in args.ids.replace(' ', '').split(',') if x}
        rows = [l for l in rows if l.id in want]
    elif not args.all:
        rows = [l for l in rows if not ai_used(l)]
    return rows[:args.limit] if args.limit else rows


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--apply', action='store_true', help='write the changes')
    ap.add_argument('--overwrite', action='store_true',
                    help='also replace company/pic/email/phone')
    ap.add_argument('--all', action='store_true', help='not just ai_used=false')
    ap.add_argument('--ids')
    ap.add_argument('--since', metavar='YYYY-MM-DD')
    ap.add_argument('--limit', type=int)
    args = ap.parse_args()

    mailbox = mail_service.crm_inbox_email()
    print('mailbox     :', mailbox)
    print('AI available:', ai_router.is_enabled())
    if not ai_router.is_enabled():
        print('\n!! No AI extractor is configured (GROQ_API_KEY / '
              'ANTHROPIC_API_KEY). Re-enriching now would produce the same '
              'regex-only result. Configure a key first.')
        return

    with app.app_context():
        leads = select(args)
        print('leads       :', len(leads))
        if not leads:
            return
        if not args.apply:
            print('\nPREVIEW — nothing will be written.\n')

        graph = GraphClient()
        out = Counter()

        for lead in leads:
            try:
                msg = _get_message(graph, mailbox, lead.email_message_id)
            except Exception as e:                          # noqa: BLE001
                out['fetch failed'] += 1
                print(f'#{lead.id:<6} FETCH-FAIL  {str(e)[:90]}')
                continue

            extracted = email_parser.extract_lead(msg)
            if not extracted:
                out['parser bailed'] += 1
                print(f'#{lead.id:<6} PARSER-BAIL')
                continue

            sender = (extracted.get('email') or '').strip().lower()
            try:
                kwargs = enrich_mod.build_enriched_lead_kwargs(
                    msg, extracted, sender_email=sender,
                    sender_domain=sender.split('@', 1)[1] if '@' in sender else '',
                    forwarded_by=extracted.get('forwarded_by'))
            except Exception as e:                          # noqa: BLE001
                out['enricher failed'] += 1
                print(f'#{lead.id:<6} ENRICH-FAIL  {str(e)[:90]}')
                continue

            touched = []
            for k, v in kwargs.items():
                cur = getattr(lead, k, None)
                allowed = (k in _ALWAYS
                           or not cur
                           or (args.overwrite and k in _OVERWRITE_FIELDS))
                if v and allowed and v != cur:
                    if args.apply:
                        setattr(lead, k, v)
                    touched.append(k)

            merged = json.loads(kwargs['email_extracted_json'])
            out['ai' if merged.get('_ai_model') else 'regex only'] += 1
            summary = (merged.get('one_line_summary') or '')[:54]
            print('#%-6s %-30s %-22s %s' % (
                lead.id, (kwargs.get('email') or '(no contact)')[:30],
                ','.join(t for t in touched if t not in _ALWAYS)[:22] or '-',
                summary))

            if args.apply:
                db.session.commit()

        print('\n--- summary ---')
        for k, n in out.most_common():
            print(f'  {n:>5}  {k}')
        if not args.apply:
            print('\nPreview only. Add --apply to write '
                  '(and --overwrite to correct contact fields).')


if __name__ == '__main__':
    main()
