"""
Turning corrections into rules — §11, Phase 3.

Every rejection and reclassification in the review queue is a labelled
example. This reads them and proposes rules: a domain rejected three
times as rate sourcing is probably a supplier; a phrase recurring in
quote-submission rejections is probably quote wording.

Nothing applies itself. Proposals go to an Admin screen and wait, because
an auto-learned rule that quietly starts dropping a real customer's mail
is the worst outcome available here — worse than the noise it removes.

    proposals()          what the corrections suggest
    apply_proposal()     a person says yes
    dismiss_proposal()   a person says no, and it stops being suggested
"""
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta

from app import db
from app.services import lead_intake as li


#: How many independent corrections before something is worth proposing.
#: Two is coincidence; three is a pattern. Configurable because the right
#: number depends on how much mail a domain sends.
MIN_EVIDENCE = 3

#: Rejection reasons that say "this sender is a supplier".
_VENDOR_REASONS = {
    'Vendor Rate Sourcing', 'Shipping Line Rate Sourcing',
    'Transporter Rate Sourcing', 'Vendor / Supplier Communication',
}

#: Reasons that say "this sender never sends business".
_BLOCK_REASONS = {'Spam / Marketing', 'Job Application / HR Email',
                  'Test Email'}


def _corrections(days=180):
    from app import EmailClassification
    since = datetime.utcnow() - timedelta(days=days)
    return (EmailClassification.query
            .filter(EmailClassification.corrected_at.isnot(None),
                    EmailClassification.corrected_at >= since)
            .all())


def proposals(days=180, min_evidence=MIN_EVIDENCE):
    """What the corrections so far suggest, strongest evidence first."""
    rows = _corrections(days)
    out = []
    out += _vendor_proposals(rows, min_evidence)
    out += _block_proposals(rows, min_evidence)
    out += _phrase_proposals(rows, min_evidence)
    out += _misclass_proposals(rows, min_evidence)
    out.sort(key=lambda p: -p['evidence'])
    return [p for p in out if not _is_dismissed(p['key'])]


def _vendor_proposals(rows, min_evidence):
    from app import VendorDomain

    by_domain = defaultdict(list)
    for r in rows:
        if (r.correction_reason or '') in _VENDOR_REASONS and r.from_domain:
            by_domain[r.from_domain].append(r)

    known = {v.domain for v in VendorDomain.query.all()}
    out = []
    for domain, hits in by_domain.items():
        if domain in known or len(hits) < min_evidence:
            continue
        reasons = Counter(h.correction_reason for h in hits)
        kind = _vendor_type(reasons.most_common(1)[0][0])
        out.append({
            'key': f'vendor:{domain}',
            'kind': 'vendor_domain',
            'title': f'Treat {domain} as a supplier',
            'detail': (f'Rejected {len(hits)} times as '
                       f'{reasons.most_common(1)[0][0]}. Mail from this '
                       f'domain would stop creating leads and be filed as '
                       f'rate sourcing instead.'),
            'evidence': len(hits),
            'payload': {'domain': domain, 'vendor_type': kind},
            'examples': [h.subject or '' for h in hits[:3]],
        })
    return out


def _vendor_type(reason):
    r = (reason or '').lower()
    if 'shipping line' in r:
        return 'shipping line'
    if 'transporter' in r:
        return 'transporter'
    return 'other'


def _block_proposals(rows, min_evidence):
    by_domain = defaultdict(list)
    for r in rows:
        if (r.correction_reason or '') in _BLOCK_REASONS and r.from_domain:
            by_domain[r.from_domain].append(r)

    out = []
    for domain, hits in by_domain.items():
        if len(hits) < min_evidence:
            continue
        out.append({
            'key': f'block:{domain}',
            'kind': 'blocked_domain',
            'title': f'Stop reading mail from {domain}',
            'detail': (f'Rejected {len(hits)} times as '
                       f'{Counter(h.correction_reason for h in hits).most_common(1)[0][0]}. '
                       f'It would be added to the sender blocklist and '
                       f'skipped before classification.'),
            'evidence': len(hits),
            'payload': {'domain': domain},
            'examples': [h.subject or '' for h in hits[:3]],
        })
    return out


def _phrase_proposals(rows, min_evidence):
    """Wording that keeps turning up in one kind of rejection.

    Only phrases the classifier does not already know — proposing what it
    already does would be noise pretending to be learning.
    """
    known = {p.lower() for p in
             (li._QUOTE_PHRASES + li._RATE_REQUEST_PHRASES)}
    buckets = defaultdict(Counter)
    for r in rows:
        target = None
        if (r.correction_reason or '') == 'Quote Submission' \
                or r.corrected_to == li.Klass.QUOTE:
            target = 'quote'
        elif r.corrected_to == li.Klass.RATE_SOURCING:
            target = 'rate'
        if not target:
            continue
        body = ((r.payload or {}).get('body') or '') + ' ' + (r.subject or '')
        for phrase in _candidate_phrases(body):
            if phrase not in known:
                buckets[target][phrase] += 1

    out = []
    for target, counts in buckets.items():
        for phrase, n in counts.most_common(5):
            if n < min_evidence:
                continue
            label = ('quotation wording' if target == 'quote'
                     else 'rate-request wording')
            out.append({
                'key': f'phrase:{target}:{phrase}',
                'kind': 'phrase',
                'title': f'Recognise "{phrase}" as {label}',
                'detail': (f'Appeared in {n} emails corrected to '
                           f'{"Quote Submission" if target == "quote" else "Rate Sourcing"}. '
                           f'Adding it would catch the next one '
                           f'automatically.'),
                'evidence': n,
                'payload': {'phrase': phrase, 'target': target},
                'examples': [],
            })
    return out


_PHRASE_RE = re.compile(r'\b(?:please|kindly|find|our|your|attached|'
                        r'enclosed|submit\w*|revert|quote|quotation|offer|'
                        r'rate|rates|best|lowest|share|provide)\b')


def _candidate_phrases(text, window=4):
    """Short word runs built around commercial vocabulary.

    Only runs containing one of those words are considered: mining every
    n-gram in an email body would surface signatures and disclaimers,
    which recur far more reliably than anything meaningful.
    """
    words = re.findall(r"[a-z']+", (text or '').lower())
    seen = set()
    for i in range(len(words) - window + 1):
        run = words[i:i + window]
        phrase = ' '.join(run)
        if len(phrase) < 12 or phrase in seen:
            continue
        if not _PHRASE_RE.search(phrase):
            continue
        seen.add(phrase)
        yield phrase


def _misclass_proposals(rows, min_evidence):
    """A step that is regularly overruled is a rule worth revisiting.

    This proposes no change — nobody should auto-tune a threshold — but
    it names the rule so a person can look at it.
    """
    wrong = Counter()
    for r in rows:
        if r.was_wrong and r.decided_by:
            wrong[(r.decided_by, r.classification, r.corrected_to)] += 1

    out = []
    for (step, was, should), n in wrong.most_common(6):
        if n < min_evidence:
            continue
        out.append({
            'key': f'misclass:{step}:{was}:{should}',
            'kind': 'observation',
            'title': (f'{step} called {n} emails '
                      f'{li.Klass.LABELS.get(was, was)} when they were '
                      f'{li.Klass.LABELS.get(should, should)}'),
            'detail': ('No rule is proposed — a threshold should not tune '
                       'itself. This is here so the rule can be looked at.'),
            'evidence': n,
            'payload': {'step': step, 'was': was, 'should': should},
            'examples': [],
        })
    return out


# ─── acting on a proposal ────────────────────────────────────────────────
def _dismissed_key(key):
    return f'intake_learning_dismissed:{key}'[:200]


def _is_dismissed(key):
    from app.models.master_data import MasterItem
    try:
        return bool(MasterItem.query.filter_by(
            list_key='intake_learning_dismissed', code=key[:200]).first())
    except Exception:
        return False


def dismiss_proposal(key, *, actor=None):
    """Say no once and stop being asked."""
    from app.master_data import service as md
    try:
        md.add_item('intake_learning_dismissed', key[:200],
                    label=key[:200], actor=actor)
        return True, None
    except Exception as exc:
        return False, str(exc)


def apply_proposal(key, *, actor=None):
    """(ok, message). Only ever called because a person clicked yes."""
    from app import VendorDomain

    if key.startswith('vendor:'):
        domain = key.split(':', 1)[1]
        if VendorDomain.query.filter_by(domain=domain).first():
            return True, f'{domain} was already a known supplier'
        db.session.add(VendorDomain(
            domain=domain, vendor_type='other',
            learned_from='learned_from_rejections',
            rejection_count=0, is_active=True, added_by=actor))
        return True, f'{domain} will now be treated as a supplier'

    if key.startswith('block:'):
        domain = key.split(':', 1)[1]
        ok, msg = _append_blocklist(domain)
        return ok, msg

    if key.startswith('phrase:'):
        # Deliberately not automated. A phrase list lives in source, and
        # a rule that edits its own source is not a rule anyone can
        # review — the proposal exists so a developer adds it knowingly.
        return False, ('Phrase rules are added in code review, not from '
                       'this screen. The proposal records the evidence.')

    if key.startswith('misclass:'):
        return False, 'This is an observation, not a change to apply.'

    return False, f'Unknown proposal {key}'


def _append_blocklist(domain):
    """The blocklist is a file the ingest already reads."""
    import os
    from email_ingest import blocklist
    try:
        path = blocklist.list_file()
        existing = ''
        if os.path.exists(path):
            with open(path) as fh:
                existing = fh.read()
        if domain in existing:
            return True, f'{domain} was already blocked'
        with open(path, 'a') as fh:
            fh.write(f'\n{domain}\n')
        return True, f'{domain} added to the sender blocklist'
    except Exception as exc:
        return False, f'could not write the blocklist: {exc}'
