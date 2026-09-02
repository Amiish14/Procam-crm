#!/usr/bin/env python
"""
v2026-09-02 — Assess and purge irrelevant leads from the CRM.

Ingestion deliberately captures every message in leads@procamgroup.in, so
newsletters, executive-education mailers, hiring ads, bank statements and
LinkedIn notifications land as Leads alongside real enquiries. This script
cleans them out AFTER the fact, where a human can see exactly what goes.

Three modes, preview always first:

    (default)   REPORT — group source='email' leads by sender domain, show
                counts and sample subjects, and mark which domains match
                the built-in junk list. Read this before deleting anything.

    --preview   List exactly which leads --apply would remove.

    --apply     Delete them. Add --soft to mark relevance='Not Relevant'
                instead of deleting (reversible; they drop out of the
                pipeline but stay in the database).

Selection (combine as needed; defaults to the built-in junk list):
    --domains a.com,b.org   purge these sender domains instead
    --ids 101,102           purge exactly these lead ids
    --since YYYY-MM-DD      restrict to leads created on/after this date
    --include-internal      ALSO purge internal Procam threads that were
                            forwarded in (an employee's own outbound mail —
                            never a customer lead). Off by default because
                            it is a judgement call, not junk mail.

Examples:
    python scripts/2026_09_02_purge_irrelevant_leads.py                  # see what's there
    python scripts/2026_09_02_purge_irrelevant_leads.py --preview        # what would go
    python scripts/2026_09_02_purge_irrelevant_leads.py --apply --soft   # reversible
    python scripts/2026_09_02_purge_irrelevant_leads.py --apply          # delete for real
"""
import argparse
import os
import sys
from collections import defaultdict
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app, db, Lead, LeadAttachment, EmailEvent      # noqa: E402
from email_ingest import parser as email_parser                # noqa: E402

# Sender domains that are never a logistics lead. Matched on the full
# domain or any parent (mail.exed.hbs.edu matches hbs.edu).
JUNK_DOMAINS = {
    # Executive education / business schools / online course sellers
    'hbs.edu', 'exed.hbs.edu', 'harvard.edu', 'hbr.org', 'aihr.com',
    'shrm.org', 'e.shrm.org', 'emeritus.org', 'coursera.org', 'edx.org',
    'upgrad.com', 'greatlearning.in', 'simplilearn.com', 'imarticus.org',
    # Certification / membership bodies
    'ascm.org', 'apics.org',
    # Consultancies & research "insights" mailers
    'mckinsey.com', 'email.mckinsey.com', 'mckinsey.us', 'bcg.com',
    'bain.com', 'deloitte.com', 'gartner.com', 'forrester.com',
    'kpmg.com', 'pwc.com', 'marketsandmarkets.com', 'mnmreports.com',
    # Social / networking / events
    'linkedin.com', 'em.linkedin.com', 'meetup.com', 'email.meetup.com',
    # Job boards, hiring and staffing
    'naukri.com', 'indeed.com', 'monster.com', 'timesjobs.com',
    'foundit.in', 'workindia.in', 'hirect.com', 'instahyre.com', 'apna.co',
    # Trade press, newsletters, magazines, market reports
    'peerlessmedia-news.com', 'freightos.com', 'projectstoday.net',
    'economist.com', 'breakbulk.news', 'joc.com', 'heavyliftpfi.com',
    'railanalysisindia.com', 'metrorailnews.in', 'nbmcw.in',
    'hindustantimes.com', 'projectcargonetwork.com', 'ibef.org',
    'indiashippingnews.com', 'sagarsandesh.in',
    # Banking / statements / payments
    'bank.in', 'hdfcbank.bank.in', 'sbi.bank.in', 'icici.bank.in',
    'axis.bank.in', 'kotak.bank.in', 'sbicard.com', 'americanexpress.com',
    'paytm.com', 'razorpay.com', 'phonepe.com', 'hdfcergo.com',
    # Bulk-mail infrastructure
    'mailchimp.com', 'mailchimpapp.com', 'sendgrid.net', 'mailgun.org',
    'constantcontact.com', 'hubspotemail.net', 'mktomail.com',
    'marketo.com', 'eloqua.com', 'substack.com', 'amazonses.com',
    # Travel
    'makemytrip.com', 'booking.com', 'cleartrip.com', 'goibibo.com',
    'yatra.com', 'agoda.com', 'expedia.com',
}


def domain_of(lead) -> str:
    e = (lead.email or '').strip().lower()
    return e.split('@', 1)[1] if '@' in e else ''


def is_junk_domain(domain: str, denylist: set) -> bool:
    if not domain:
        return False
    return any(domain == d or domain.endswith('.' + d) for d in denylist)


def is_internal_thread(lead) -> bool:
    """An internal Procam thread relayed into the leads inbox: either the
    contact is a Procam address, or the ingest could not find any external
    sender because the whole thread was internal."""
    e = (lead.email or '').strip().lower()
    if e and '@' in e:
        return e.split('@', 1)[1] in email_parser._skip_domains()
    if e:
        return False
    # No contact at all — check why the parser gave up.
    try:
        import json
        tag = (json.loads(lead.opp_notes or '{}').get('triage_tag') or '')
    except Exception:
        tag = ''
    return tag.startswith('internal domain')


def is_bulk_sender(lead) -> bool:
    """no-reply@, newsletter@, updates@, marketing@ … — bulk regardless
    of which domain sent it."""
    e = (lead.email or '').strip().lower()
    if '@' not in e:
        return False
    local = e.split('@', 1)[0]
    return bool(email_parser._BULK_LOCAL_RE.match(local)
                or email_parser._NOREPLY_RE.search(e))


def base_query(args):
    q = Lead.query.filter(Lead.source == 'email')
    if args.since:
        try:
            cutoff = datetime.strptime(args.since, '%Y-%m-%d')
        except ValueError:
            sys.exit(f'--since must be YYYY-MM-DD, got {args.since!r}')
        q = q.filter(Lead.created_at >= cutoff)
    return q.order_by(Lead.id)


def select_targets(args, denylist):
    leads = base_query(args).all()
    if args.ids:
        want = {int(x) for x in args.ids.replace(' ', '').split(',') if x}
        return [l for l in leads if l.id in want], leads
    targets = [l for l in leads
               if is_junk_domain(domain_of(l), denylist) or is_bulk_sender(l)
               or (args.include_internal and is_internal_thread(l))]
    return targets, leads


def report(leads, denylist):
    groups = defaultdict(list)
    for l in leads:
        groups[domain_of(l) or '(no sender)'].append(l)

    print('%-38s %6s  %-5s %s' % ('sender domain', 'leads', 'junk?', 'sample subject'))
    print('-' * 108)
    junk_total = 0
    for dom, rows in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        junk = (is_junk_domain(dom, denylist)
                or all(is_bulk_sender(l) for l in rows))
        internal = all(is_internal_thread(l) for l in rows)
        if junk:
            junk_total += len(rows)
        sample = ''
        for l in rows:
            sample = (l.notes or '').strip().splitlines()[0] if l.notes else ''
            if l.company and l.company != 'Unknown':
                sample = l.company + ' — ' + sample
            break
        flag = 'JUNK' if junk else ('INTL' if internal else '')
        print('%-38s %6d  %-5s %s' % (dom[:38], len(rows), flag, sample[:52]))
    print('-' * 108)
    print('%d leads across %d domains; %d match the junk list'
          % (len(leads), len(groups), junk_total))
    print("JUNK = removed by --apply.  INTL = internal Procam thread, "
          "only removed with --include-internal.")


def _child_models():
    """Every model with a lead_id FK. Imported lazily and defensively so a
    schema that predates one of them still works."""
    import app as _app
    out = []
    for name in ('LeadActivity', 'LeadStageHistory', 'OutreachDraft',
                 'Opportunity', 'Competitor'):
        m = getattr(_app, name, None)
        if m is not None and hasattr(m, 'lead_id'):
            out.append(m)
    return out


def count_orphans() -> dict:
    """Rows in child tables pointing at a lead that no longer exists."""
    live = {row[0] for row in db.session.query(Lead.id).all()}
    found = {}
    for model in _child_models():
        try:
            bad = [r.id for r in model.query.all()
                   if getattr(r, 'lead_id', None) not in live]
        except Exception:
            continue
        if bad:
            found[model.__name__] = bad
    return found


def purge(targets, soft):
    ids = [l.id for l in targets]
    if not ids:
        return
    if soft:
        for l in targets:
            l.relevance = 'Not Relevant'
        db.session.commit()
        print(f'\nMarked {len(ids)} leads relevance=\'Not Relevant\'. '
              f'Nothing was deleted.')
        return

    # Clear every table that points at leads.id before deleting the rows.
    # Use the ORM models rather than raw SQL — a text() query with
    # `IN :ids` needs an expanding bindparam and silently fails without one.
    att = LeadAttachment.query.filter(
        LeadAttachment.lead_id.in_(ids)).delete(synchronize_session=False)
    evt = EmailEvent.query.filter(EmailEvent.lead_id.in_(ids)).update(
        {EmailEvent.lead_id: None, EmailEvent.status: 'lead_deleted'},
        synchronize_session=False)
    child_rows = 0
    for model in _child_models():
        try:
            child_rows += model.query.filter(
                model.lead_id.in_(ids)).delete(synchronize_session=False) or 0
        except Exception as e:                                   # noqa: BLE001
            db.session.rollback()
            print(f'  note: could not clean {model.__name__}: {str(e)[:90]}')
    deleted = Lead.query.filter(Lead.id.in_(ids)).delete(synchronize_session=False)
    db.session.commit()
    print(f'\nDeleted {deleted} leads, {att} attachment rows, {child_rows} '
          f'child rows; {evt} email events marked lead_deleted.')
    print('Attachment FILES on disk were left in place — remove them '
          'manually from EMAIL_INGEST_STORAGE_ROOT if you want the space.')


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--preview', action='store_true',
                    help='list the leads --apply would remove')
    ap.add_argument('--apply', action='store_true', help='actually purge')
    ap.add_argument('--soft', action='store_true',
                    help="with --apply: mark 'Not Relevant' instead of deleting")
    ap.add_argument('--domains', help='comma-separated sender domains to purge')
    ap.add_argument('--ids', help='comma-separated lead ids to purge')
    ap.add_argument('--since', metavar='YYYY-MM-DD')
    ap.add_argument('--include-internal', action='store_true',
                    help='also purge internal Procam threads forwarded in')
    ap.add_argument('--clean-orphans', action='store_true',
                    help='delete child rows left behind by an earlier purge')
    args = ap.parse_args()

    denylist = set(JUNK_DOMAINS)
    if args.domains:
        denylist = {d.strip().lower() for d in args.domains.split(',') if d.strip()}

    with app.app_context():
        if args.clean_orphans:
            orphans = count_orphans()
            if not orphans:
                print('No orphaned child rows — nothing to clean.')
                return
            for name, ids in orphans.items():
                print(f'  {name}: {len(ids)} orphaned rows')
            if not args.apply:
                print('\nPreview only. Add --apply to delete them.')
                return
            for model in _child_models():
                if model.__name__ in orphans:
                    model.query.filter(
                        model.id.in_(orphans[model.__name__])).delete(
                        synchronize_session=False)
            db.session.commit()
            print('\nOrphaned rows deleted.')
            return

        if not (args.preview or args.apply):
            report(base_query(args).all(), denylist)
            print('\nRe-run with --preview to see exactly which leads would go.')
            return

        targets, all_leads = select_targets(args, denylist)
        print('%d of %d email leads selected for purge\n' % (len(targets), len(all_leads)))
        print('%-7s %-24s %-34s %s' % ('id', 'company', 'sender', 'subject'))
        print('-' * 108)
        for l in targets:
            subj = ''
            try:
                import json
                subj = (json.loads(l.opp_notes or '{}')
                        .get('source_subject') or '')
            except Exception:
                pass
            print('%-7s %-24s %-34s %s' % (
                l.id, (l.company or '?')[:24], (l.email or '-')[:34], subj[:40]))

        if not args.apply:
            print('\nPREVIEW only — nothing written. Add --apply to purge '
                  '(or --apply --soft to mark Not Relevant instead).')
            return
        purge(targets, args.soft)


if __name__ == '__main__':
    main()
