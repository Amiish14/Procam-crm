#!/usr/bin/env python
"""Run the email → lead ingest. Called by systemd timer daily at 09:00 IST."""
import sys, os, json, logging
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.email_ingest.pipeline import run_ingest

if __name__ == '__main__':
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    )
    lookback = int(os.environ.get('EMAIL_INGEST_LOOKBACK_HOURS', '26'))
    dry_run  = '--dry-run' in sys.argv
    stats = run_ingest(lookback_hours=lookback, dry_run=dry_run)
    print(json.dumps(stats, indent=2))
    sys.exit(0 if stats.get('errors', 0) == 0 else 1)
