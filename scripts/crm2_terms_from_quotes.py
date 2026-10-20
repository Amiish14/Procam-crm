"""
What Procam's quotes actually say, read off the quotes themselves.

    .venv/bin/python scripts/crm2_terms_from_quotes.py --check

READ ONLY. No apply, no publish. It writes one workbook and
nothing else; `quote_terms.publish()` is a separate, deliberate act.

Why read the attachments rather than ask
    The six term sets seeded in R5 are placeholders I wrote. The
    real wording is in the quotes that have gone out for years, as
    PDFs attached to mail the CRM already ingested. This reads them.

How a Procam quote is told from everything else
    A thread carries the client's RFQ, a carrier's offer and our
    quote in the same folder. A document counts as ours when it
    names Procam as the party offering — not merely mentions the
    word, which a client's RFQ addressed to us does too. The score
    and the evidence are written into the sheet so the judgement can
    be checked rather than trusted.

What it cannot read
    python-docx is not installed on this server, so the 18 .doc and
    .docx attachments are listed and skipped rather than silently
    omitted. Scanned PDFs with no text layer come back empty and are
    counted separately: "no clauses found" and "could not read the
    file" are different answers and the sheet keeps them apart.
"""
import argparse
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import date

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from app import Lead, app, db                              # noqa: E402
from app.services import attachment_text                   # noqa: E402

PER_VERTICAL = 10

#: Verticals we are proposing terms for.
VERTICALS = ('Freight Forwarding', 'Project Freight',
             'General Transport', 'Heavy Transport',
             'Installation', 'Warehousing')

#: Procam writing as the party making the offer. The first run of
#: this script scored on "mentions Procam" and produced a proposal
#: built almost entirely out of TMIL's tender conditions and L&T's
#: LSP contract — documents where Procam is the supplier being
#: bound, not the one quoting. Those name Procam on every page.
#:
#: So the test is authorship, not mention: we are pleased to quote,
#: our charges, our offer. Plus the letterhead, which a client's
#: tender does not carry.
AUTHOR = (
    r'we\s+(?:are\s+pleased\s+to\s+)?(?:quote|offer|submit)',
    r'our\s+(?:best\s+)?(?:offer|quotation|quote|charges|rates|'
    r'proposal)',
    r'thank\s+you\s+for\s+your\s+(?:enquiry|rfq|query)',
    r'procam\s+logistics\s+(?:pvt|private)',
    r'procam\s+logitech',
    r'for\s+procam\s+logi',
)

#: Phrases that mean somebody is dictating terms *to* a supplier.
#: Any one of these and the document is not our quote, whatever
#: else it says. A client's RFQ is addressed to us and mentions us
#: constantly, which is why mention was never going to work.
CLIENT_SIDE = (
    r'\bLSP\s+shall\b', r'\bthe\s+LSP\b',
    r'\byou\s+are\s+to\s+quote\b',
    r'\bsubmit\s+your\s+offer\b', r'\byour\s+offer\b',
    r'\bbidders?\b', r'\btenderer\b', r'\btender\s+document\b',
    r'\bsealed\s+envelope\b',
    r'\bvendor\s+shall\b', r'\bsupplier\s+shall\b',
    r'\bright\s+to\s+(?:accept|reject)\s+(?:any|offers)\b',
    r'\brequest\s+for\s+quotation\b',
)

SUPPORT = (
    r'\bvalidity\b', r'\bpayment\s+terms\b', r'\bincoterm',
    r'\bper\s+(?:mt|ton|trip|container)\b',
    r'\bgst\s+(?:extra|@|as)\b',
    r'\bexcl(?:uding|usive)\b', r'\bincl(?:uding|usive)\b',
)

#: Where the terms start in a quote. Everything after one of these,
#: to the end or to the next obvious section, is the clause block.
TERMS_HEADINGS = (
    r'terms\s*(?:&|and)?\s*conditions',
    r'general\s+terms',
    r'standard\s+terms',
    r'terms\s+of\s+(?:business|trade)',
    r'^\s*(?:notes?|conditions)\s*[:\-]',
    r'important\s+notes?',
)

#: A clause is a sentence or a numbered line. Lines shorter than
#: this are headings, page numbers and table debris.
MIN_CLAUSE = 25
MAX_CLAUSE = 400

#: Filenames that are not quotes whatever they contain. An invoice
#: and an arrival notice both carry our letterhead and the word
#: "charges", and neither states the terms we quote under.
NOT_A_QUOTE_FILE = (
    'invoice', 'proforma', 'packing list', 'arrival notice',
    'debit note', 'credit note', 'mawb', 'hawb', 'bill of lading',
    'boe', 'delivery order', 'po2', 'purchase order',
)

#: Clause-level vetoes. A genuine Procam quote often repeats the
#: client's conditions back inside it — an LD table, a loading plan
#: they demand — and those are their words in our document. The
#: giveaway is the second person: our own terms say what we will do
#: or exclude, theirs say what "you shall" do.
CLIENT_CLAUSE = (
    r'^\s*you\b', r'\byou\s+(?:should|shall|must|are\s+requested|'
    r'are\s+to)\b', r'\byour\s+(?:offer|quotation|bid|vehicle|'
    r'vehicles)\b',
    r'\bLD\b', r'\bliquidated\s+damages\b',
    r'\bpenalty\s+for\s+delay', r'\bLSP\b', r'\bbidder',
    # Roles a client uses for us. "The Contractor shall not
    # transship" is the owner binding the transporter; it is in our
    # folder because they sent it to us, not because we wrote it.
    r'\b(?:the\s+)?contractor\s+(?:shall|must|will)\b',
    r'\btransporter\s+(?:shall|must|will)\b',
    r'\bOWNER\b',
    r'\bwill\s+be\s+recovered\s+from\b',
    r'\bpenalty\s+shall\b',
    r'\bper\s+day\s+Rs\b',
)


def _score(text):
    """Is Procam the author of this offer? (score, evidence, why_not).

    A negative marker vetoes outright rather than subtracting. A
    tender can carry our letterhead on a covering page and still be
    the client's document, and a score that let three positives
    outvote one "the LSP shall" is how the first run filled a
    proposal with other companies' contracts.
    """
    body = text or ''
    low = body.lower()
    against = [rx for rx in CLIENT_SIDE
               if re.search(rx, body, re.I)]
    if against:
        return 0, [], _label(against[0])

    hits, score = [], 0
    for rx in AUTHOR:
        if re.search(rx, low):
            score += 3
            hits.append(_label(rx))
    if not hits:
        return 0, [], 'nothing says Procam wrote it'
    for rx in SUPPORT:
        if re.search(rx, low):
            score += 1
    return score, hits, ''


def _label(rx):
    return re.sub(r'[\\](?:s\+|b)|[()?:\[\]]', ' ',
                  rx).replace('  ', ' ').strip()


def _terms_block(text):
    """The part of the document that carries the clauses."""
    low = (text or '').lower()
    start = None
    for rx in TERMS_HEADINGS:
        m = re.search(rx, low, re.M)
        if m and (start is None or m.start() < start):
            start = m.start()
    if start is None:
        return ''
    return text[start:start + 6000]


def _is_clause(line):
    """A sentence of terms, not a table row or a signature block."""
    if not (MIN_CLAUSE <= len(line) <= MAX_CLAUSE):
        return False
    words = re.findall(r'[A-Za-z]{3,}', line)
    if len(words) < 5:
        # "Axles lines (upto 10) with" and "4921 / 4923 and
        # equivalent 1500 3000 4500" are rate tables that survived
        # the PDF's column layout, not clauses.
        return False
    digits = sum(c.isdigit() for c in line)
    if digits > len(line) * 0.25:
        return False
    if re.search(r'procam\s+logistics\s+(?:pvt|private)[\s.,]*$',
                 line, re.I):
        return False                      # the signature block
    for rx in CLIENT_CLAUSE:
        if re.search(rx, line, re.I):
            return False
    return True


def _clauses(block):
    """Numbered lines or sentences, tidied."""
    out = []
    # Numbered or bulleted lines first — most quotes write terms
    # that way, and splitting those on full stops mangles them.
    for raw in re.split(r'\n+', block or ''):
        line = re.sub(r'\s+', ' ', raw).strip(' .\t|-–—•*')
        if not line:
            continue
        line = re.sub(r'^\(?\d+[.)]\s*', '', line).strip()
        if _is_clause(line):
            out.append(line)
    return out


def _norm(clause):
    """A clause's fingerprint, for grouping near-identical wording."""
    low = re.sub(r'[^a-z0-9 ]+', ' ', (clause or '').lower())
    words = [w for w in low.split() if len(w) > 2]
    # Numbers vary between quotes (30 days, 45 days) and the clause
    # is the same clause, so they are dropped from the fingerprint
    # while the printed text keeps them.
    words = [w for w in words if not w.isdigit()]
    return ' '.join(sorted(set(words))[:14])


def survey():
    """{vertical: {'quotes': [...], 'clauses': Counter-ish}}"""
    rows = db.session.execute(db.text(
        'SELECT a.id, a.lead_id, a.filename, a.storage_path, '
        '       a.uploaded_at, l.procam_vertical, l.company '
        '  FROM lead_attachments a JOIN leads l ON l.id = a.lead_id '
        ' WHERE l.procam_vertical IS NOT NULL '
        ' ORDER BY a.id DESC')).fetchall()

    found = defaultdict(list)
    skipped = Counter()
    unreadable = []
    #: The same PDF is often attached to several leads — a client's
    #: terms annexure forwarded twice. Counting it twice makes every
    #: clause in it look corroborated by two quotes, which is how
    #: one document became 34 "proposed standard" clauses.
    seen_docs = {}
    for (aid, lead_id, filename, path, uploaded, vertical,
         company) in rows:
        vertical = (vertical or '').strip()
        if vertical not in VERTICALS:
            continue
        if len(found[vertical]) >= PER_VERTICAL:
            continue
        name = (filename or '').lower()
        if any(bad in name for bad in NOT_A_QUOTE_FILE):
            skipped['an invoice, PO or shipping document'] += 1
            continue
        ext = os.path.splitext(filename or '')[1].lower()
        if ext in ('.doc', '.docx'):
            skipped['Word file — python-docx is not installed'] += 1
            unreadable.append((vertical, aid, filename,
                               'python-docx not installed'))
            continue
        if ext != '.pdf':
            continue
        text = attachment_text.extract(path, max_chars=40000)
        if not text.strip():
            skipped['PDF with no text layer (scan or image)'] += 1
            unreadable.append((vertical, aid, filename,
                               'no text layer'))
            continue
        import hashlib
        fingerprint = hashlib.sha1(
            re.sub(r'\s+', ' ', text).strip()[:4000].encode(
                'utf-8', 'ignore')).hexdigest()
        if fingerprint in seen_docs:
            skipped['the same document, already read on another '
                    'lead'] += 1
            continue
        seen_docs[fingerprint] = aid

        score, evidence, why_not = _score(text)
        if score < 4:
            skipped[why_not or 'too little to call it our quote'] += 1
            continue
        block = _terms_block(text)
        clauses = _clauses(block)
        if not clauses:
            skipped['Procam quote with no terms section found'] += 1
        found[vertical].append({
            'attachment_id': aid, 'lead_id': lead_id,
            'filename': filename, 'uploaded': uploaded,
            'company': company, 'score': score,
            'evidence': ', '.join(evidence[:3]),
            'clauses': clauses,
        })
    return found, skipped, unreadable


def group(found):
    """{vertical: [(clause, times, [sources])]} commonest first."""
    out = {}
    for vertical, quotes in found.items():
        seen = defaultdict(list)
        longest = {}
        for q in quotes:
            for c in set(q['clauses']):
                key = _norm(c)
                if not key:
                    continue
                seen[key].append(q)
                # Keep the fullest wording of the group; a truncated
                # copy is usually a table that wrapped.
                if len(c) > len(longest.get(key, '')):
                    longest[key] = c
        rows = [(longest[k], len(v), v) for k, v in seen.items()]
        rows.sort(key=lambda r: (-r[1], -len(r[0])))
        out[vertical] = rows
    return out


def write_workbook(found, grouped, skipped, unreadable, out_dir=None):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    root = out_dir or os.path.join(os.path.dirname(_HERE),
                                   'docs', 'exports')
    os.makedirs(root, exist_ok=True)
    path = os.path.join(
        root, f'quote_terms_v2_proposal_{date.today().isoformat()}.xlsx')

    wb = Workbook()
    head = Font(bold=True, color='FFFFFF')
    fill = PatternFill('solid', fgColor='1F2937')
    wrap = Alignment(wrap_text=True, vertical='top')

    ws = wb.active
    ws.title = 'Read me'
    for line in (
        'Proposed v2 standard terms, read off Procam quotes already '
        'in the CRM.',
        '',
        'NOTHING IS PUBLISHED. This workbook is a proposal. The term '
        'sets live in quote_term_sets and are written only by '
        'quote_terms.publish().',
        '',
        'How a clause got here: it appeared in the terms section of '
        'a PDF that reads as a Procam quote. The "Seen in" column '
        'counts how many of that vertical\'s quotes carried it, and '
        '"Sources" names them. A clause seen once is one quote\'s '
        'wording, not a standard.',
        '',
        'What was not read: Word attachments (python-docx is not '
        'installed on this server) and PDFs with no text layer. Both '
        'are listed on the "Not read" sheet. They are not absent '
        'because they said nothing.',
        '',
        'The proposed body on each vertical sheet is the clauses '
        'seen in two or more quotes, in order of how often. Read it '
        'as evidence of what we already promise, not as approved '
        'wording — nobody has checked these against what Procam '
        'intends to promise, and that is the point of the review.',
    ):
        ws.append([line])
        ws.cell(ws.max_row, 1).alignment = wrap
    ws.column_dimensions['A'].width = 110
    ws['A1'].font = Font(bold=True, size=13)

    sm = wb.create_sheet('Summary')
    sm.append(['Vertical', 'Quotes read', 'Distinct clauses',
               'Proposed (seen 2+)', 'Seen once only'])
    for cell in sm[1]:
        cell.font, cell.fill = head, fill
    for vertical in VERTICALS:
        rows = grouped.get(vertical, [])
        sm.append([vertical, len(found.get(vertical, [])), len(rows),
                   sum(1 for r in rows if r[1] >= 2),
                   sum(1 for r in rows if r[1] == 1)])
    for col, w in (('A', 24), ('B', 13), ('C', 17), ('D', 19),
                   ('E', 15)):
        sm.column_dimensions[col].width = w

    for vertical in VERTICALS:
        rows = grouped.get(vertical, [])
        s = wb.create_sheet(vertical[:28])
        s.append([f'{vertical} — proposed v2'])
        s['A1'].font = Font(bold=True, size=13)
        s.append([f'{len(found.get(vertical, []))} quote(s) read. '
                  f'Clauses seen in two or more are proposed as '
                  f'standard; the rest are listed below them as '
                  f'candidates for a special condition.'])
        s.cell(s.max_row, 1).alignment = wrap
        s.append([])
        s.append(['Propose as', 'Seen in', 'Clause', 'Sources'])
        for cell in s[4]:
            cell.font, cell.fill = head, fill
        for clause, times, sources in rows:
            s.append([
                'standard' if times >= 2 else 'special / one-off',
                times, clause,
                '; '.join(f'#{q["lead_id"]} {q["filename"][:40]}'
                          for q in sources[:4])])
            s.cell(s.max_row, 3).alignment = wrap
            s.cell(s.max_row, 4).alignment = wrap
        for col, w in (('A', 18), ('B', 9), ('C', 78), ('D', 46)):
            s.column_dimensions[col].width = w
        s.freeze_panes = 'A5'

    q = wb.create_sheet('Quotes read')
    q.append(['Vertical', 'Lead', 'Company', 'File', 'Score',
              'Matched on', 'Clauses'])
    for cell in q[1]:
        cell.font, cell.fill = head, fill
    for vertical in VERTICALS:
        for item in found.get(vertical, []):
            q.append([vertical, item['lead_id'],
                      (item['company'] or '')[:40], item['filename'],
                      item['score'], item['evidence'],
                      len(item['clauses'])])
    for col, w in (('A', 22), ('B', 8), ('C', 34), ('D', 50),
                   ('E', 7), ('F', 34), ('G', 9)):
        q.column_dimensions[col].width = w

    nr = wb.create_sheet('Not read')
    nr.append(['Vertical', 'Attachment', 'File', 'Why not'])
    for cell in nr[1]:
        cell.font, cell.fill = head, fill
    for row in unreadable:
        nr.append(list(row))
    nr.append([])
    nr.append(['Counts across everything scanned:'])
    for why, n in skipped.most_common():
        nr.append(['', '', why, n])
    for col, w in (('A', 22), ('B', 13), ('C', 50), ('D', 46)):
        nr.column_dimensions[col].width = w

    wb.save(path)
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true', default=True)
    ap.add_argument('--out-dir', dest='out_dir', default=None)
    args = ap.parse_args()

    with app.app_context():
        print(f'database: {db.engine.url}')
        print('MODE:     READ ONLY — nothing is published\n')
        if not attachment_text.pdf_supported():
            raise SystemExit('pypdf is not installed; no PDF can be '
                             'read and the proposal would be empty.')

        found, skipped, unreadable = survey()
        grouped = group(found)

        print(f'  {"vertical":<22}{"quotes":>8}{"clauses":>9}'
              f'{"proposed":>10}')
        for vertical in VERTICALS:
            rows = grouped.get(vertical, [])
            print(f'  {vertical:<22}{len(found.get(vertical, [])):>8}'
                  f'{len(rows):>9}'
                  f'{sum(1 for r in rows if r[1] >= 2):>10}')

        print('\n  not read:')
        for why, n in skipped.most_common():
            print(f'    {n:>5}  {why}')

        path = write_workbook(found, grouped, skipped, unreadable,
                              out_dir=args.out_dir)
        print(f'\n  {path}')
        print('  Nothing was published. quote_term_sets is unchanged.')


if __name__ == '__main__':
    main()
