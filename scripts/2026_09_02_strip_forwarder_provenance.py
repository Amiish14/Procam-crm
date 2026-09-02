#!/usr/bin/env python
"""
v2026-09-02 — Remove all "forwarded to CRM by ..." provenance from leads.

Earlier ingestion prefixed each email lead's notes with

    [Forwarded to CRM by procamadmin@procamgroup.in on 2026-09-01 15:13 UTC]
    [Their note: ...]

    --- Original message ---

and recorded `forwarded_by` / `forward_note` inside opp_notes. Who relayed
a lead internally is not part of the customer record and must not appear
anywhere in the portal, so this strips it from every existing row. Ingestion
no longer writes it.

Only the header is removed — the original customer message underneath is
left exactly as it was.

Examples:
    python scripts/2026_09_02_strip_forwarder_provenance.py            # preview
    python scripts/2026_09_02_strip_forwarder_provenance.py --apply
"""
import argparse
import json
import os
import re
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env'))
except ImportError:                                              # pragma: no cover
    pass

from app import app, db, Lead                                # noqa: E402

# "[Forwarded to CRM by x@y on 2026-09-01 15:13 UTC]" plus an optional
# "[Their note: ...]" line, plus the "--- Original message ---" separator
# and any blank lines between them.
_HEADER_RE = re.compile(
    r"^\s*\[Forwarded to CRM by[^\]]*\]\s*"
    r"(?:\n\s*\[Their note:[^\]]*\]\s*)?"
    r"(?:\n\s*-{2,}\s*Original message\s*-{2,}\s*)?\n*",
    re.IGNORECASE,
)
# Belt and braces: any stray occurrence anywhere in the body.
_ANYWHERE_RE = re.compile(r"\[Forwarded to CRM by[^\]]*\]\s*", re.IGNORECASE)
_THEIR_NOTE_RE = re.compile(r"\[Their note:[^\]]*\]\s*", re.IGNORECASE)

_OPP_KEYS = ('forwarded_by', 'forward_note', 'outer_subject')


def clean_notes(notes: str) -> str:
    if not notes:
        return notes
    out = _HEADER_RE.sub('', notes, count=1)
    out = _ANYWHERE_RE.sub('', out)
    out = _THEIR_NOTE_RE.sub('', out)
    out = re.sub(r"^\s*-{2,}\s*Original message\s*-{2,}\s*\n*", '', out,
                 count=1, flags=re.IGNORECASE)
    return out.lstrip('\n')


def clean_opp(opp: str):
    """Strip provenance keys from opp_notes JSON. Returns None if unchanged."""
    if not opp:
        return None
    try:
        data = json.loads(opp)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    if not any(k in data for k in _OPP_KEYS):
        return None
    for k in _OPP_KEYS:
        data.pop(k, None)
    return json.dumps(data, default=str)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--apply', action='store_true', help='write the changes')
    args = ap.parse_args()

    with app.app_context():
        leads = Lead.query.order_by(Lead.id).all()
        out = Counter()
        shown = 0
        for lead in leads:
            new_notes = clean_notes(lead.notes or '')
            notes_changed = new_notes != (lead.notes or '')
            new_opp = clean_opp(lead.opp_notes)
            if not notes_changed and new_opp is None:
                continue

            out['leads cleaned'] += 1
            if notes_changed:
                out['notes header removed'] += 1
            if new_opp is not None:
                out['opp_notes keys removed'] += 1

            if shown < 15:
                shown += 1
                before = (lead.notes or '').split('\n', 1)[0][:70]
                after = (new_notes or '').split('\n', 1)[0][:70]
                print(f'#{lead.id}')
                print(f'   before: {before!r}')
                print(f'   after : {after!r}')

            if args.apply:
                if notes_changed:
                    lead.notes = new_notes
                if new_opp is not None:
                    lead.opp_notes = new_opp

        if args.apply:
            db.session.commit()

        print('\n--- summary ---')
        for k, n in out.most_common():
            print(f'  {n:>5}  {k}')
        if not out:
            print('  nothing to clean')
        elif not args.apply:
            print('\nPreview only. Add --apply to write.')
        else:
            print('\nDone. No lead carries forwarder provenance any more.')


if __name__ == '__main__':
    main()
