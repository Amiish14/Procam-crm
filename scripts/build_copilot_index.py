"""
Build the §4 retrieval index — one chunk per readable passage.

Reads what the CRM already holds (the original enquiry, notes, and the
email trail), splits it into overlapping chunks, and stamps each with
the permission metadata the scope filter matches on. Nothing leaves the
machine unless an internal embedder is configured.

Re-runnable. Each lead is rebuilt rather than appended to, so a lead
that changed hands gets chunks carrying its new owner — an index holding
yesterday's permissions is the failure this design exists to avoid.

    python scripts/build_copilot_index.py --check
    python scripts/build_copilot_index.py                # all leads
    python scripts/build_copilot_index.py --since 90     # recent only
    python scripts/build_copilot_index.py --embed        # add vectors
"""
import argparse
import os
import sys
from datetime import datetime, timedelta

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

from app import app, db, Lead                          # noqa: E402
from app.copilot import retrieval                      # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--since', type=int, default=0,
                    help='only leads updated in the last N days')
    ap.add_argument('--embed', action='store_true',
                    help='also compute vectors (needs PROCAM_AI_EMBED_URL)')
    ap.add_argument('--batch', type=int, default=200)
    args = ap.parse_args()

    with app.app_context():
        from app.models.copilot import CopilotChunk

        q = Lead.query
        if args.since:
            q = q.filter(Lead.updated_at >= datetime.utcnow()
                         - timedelta(days=args.since))
        leads = q.all()
        before = CopilotChunk.query.count()

        print(f'  leads to index : {len(leads)}')
        print(f'  chunks now     : {before}')
        print(f'  embedder       : '
              f'{"available" if retrieval.embeddings_available() else "none — lexical only"}')

        if args.check:
            print('\n== DRY RUN — nothing written ==')
            sample = 0
            for lead in leads[:50]:
                sample += sum(len(retrieval.split(f'{s}\n{t}'))
                              for _src, s, t in
                              retrieval.chunks_for_lead(lead))
            rate = sample / max(min(len(leads), 50), 1)
            print(f'  WOULD write roughly {int(rate * len(leads))} chunk(s)')
            print('  Nothing is sent anywhere: chunks are rows in this '
                  'database.')
            return

        written = 0
        for n, lead in enumerate(leads, 1):
            written += retrieval.index_lead(lead, commit=False)
            if n % args.batch == 0:
                db.session.commit()
                print(f'    {n}/{len(leads)} leads · {written} chunks')
        db.session.commit()
        print(f'\n  {written} chunk(s) written for {len(leads)} lead(s).')

        if args.embed:
            if not retrieval.embeddings_available():
                print('  --embed asked for, but no internal embedder is '
                      'configured. Set PROCAM_AI_EMBED_URL. Nothing was '
                      'sent anywhere.')
                return
            import json
            todo = CopilotChunk.query.filter(
                CopilotChunk.embedding.is_(None)).all()
            print(f'  embedding {len(todo)} chunk(s)…')
            done = 0
            for i in range(0, len(todo), 64):
                block = todo[i:i + 64]
                vectors = retrieval.embed([c.text for c in block])
                if not vectors:
                    print('  embedder stopped responding — the lexical '
                          'backend still answers. Re-run to continue.')
                    break
                for chunk, vec in zip(block, vectors):
                    chunk.embedding = json.dumps(vec)
                db.session.commit()
                done += len(block)
                print(f'    {done}/{len(todo)}')
            print(f'  {done} chunk(s) embedded.')

        print(f'\n  {retrieval.stats()}')


if __name__ == '__main__':
    main()
