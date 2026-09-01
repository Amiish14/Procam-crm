#!/usr/bin/env python
"""Historical / backfill email → lead ingest.

v2026-08-31: leads@procamgroup.in is the sole live source, and messages
land in real time via the /api/email/webhook path (Microsoft Graph push).
The daily 09:00 IST poll this script used to power is RETIRED — its
systemd timer should be disabled (`sudo systemctl disable --now
procam-crm-email-ingest.timer`).

Kept around only for one-off backfills. To force a run, invoke:

    EMAIL_INGESTION_MODE=azure_api  python scripts/email_ingest.py --dry-run

Otherwise the script no-ops safely on today's default mailbox mode.
"""
import sys, os, json, logging
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from email_ingest.pipeline import run_ingest
from email_ingest import service as _mail

if __name__ == '__main__':
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    )
    if not _mail.poll_should_run():
        print(json.dumps({
            'mode': _mail.current_mode(),
            'note': ('Poll disabled — mailbox/webhook mode is authoritative. '
                     'To force a one-off backfill, run with '
                     'EMAIL_INGESTION_MODE=azure_api set in the env.'),
            'mailbox': _mail.crm_inbox_email(),
        }, indent=2))
        sys.exit(0)
    lookback = int(os.environ.get('EMAIL_INGEST_LOOKBACK_HOURS', '26'))
    dry_run  = '--dry-run' in sys.argv
    stats = run_ingest(lookback_hours=lookback, dry_run=dry_run)
    print(json.dumps(stats, indent=2))
    sys.exit(0 if stats.get('errors', 0) == 0 else 1)
