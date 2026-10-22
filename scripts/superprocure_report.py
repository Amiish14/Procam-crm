"""
CRM-SUPERPROCURE-01 — what came through the portal, and what we did
with it.

    .venv/bin/python scripts/superprocure_report.py --check

READ ONLY. Writes one workbook and changes nothing.

SuperProcure is an e-procurement portal. Its mail arrives from
noreply@alerts.superprocure.com and each one is a load posted by a
shipper: route, vehicle type, tonnage, loading date, often an
auction time. They are enquiries, and the CRM has been treating
them as junk.

The parsing is best-effort and says so
    The posting is a table flattened into an email, so the columns
    run together: a source address ends in a six-digit pincode and
    the destination begins at the next capital letter. That is the
    boundary this reads, and where it cannot find one the row says
    `(unparsed)` rather than guessing a lane. Counting a wrong
    lane would be worse than counting none.
"""
import argparse
import os
import re
import sys
from collections import Counter
from datetime import date, datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from app import Lead, app, db                             # noqa: E402

SENDER = '%superprocure%'

#: "#25236064LIVE15 Sep 8:00 AM…" — the id, then the type.
RX_ID = re.compile(r'#(\d{6,12})\s*(LIVE|E-AUCTION|SPOT|[A-Z-]{3,12})?')
#: A pincode immediately followed by a capital letter is where the
#: source address stops and the destination starts.
RX_SPLIT = re.compile(r'(?<=\d{6})(?=[A-Z])')
RX_VEHICLES = re.compile(r'Vehicles?\s*Required\s*:\s*(\d+)', re.I)
RX_QTY = re.compile(r'Qty\s*/\s*Vehicle\s*:\s*([\d.]+)\s*(MT|KG|TON)',
                    re.I)
RX_DIST = re.compile(r'(\d{2,5}\.\d{1,2})(?=Vehicles?\s*Required)', re.I)
RX_SHIPPER = re.compile(
    r'(?:Load posted by|Auction starting for|Load Post for)\s+(.+?)\s*$',
    re.I | re.M)
RX_HEADER = re.compile(r'^\s*(.+?)\s*\|\s*(Load Post|Auction Start)',
                       re.I | re.M)
#: "15 Sep 8:00 AM" / "9 Sep" / "15 Sep 2026"
RX_LOADING = re.compile(
    r'\b(\d{1,2})\s*(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)'
    r'(?:\s+(\d{4}))?', re.I)
MONTHS = {m: i for i, m in enumerate(
    ('jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep',
     'oct', 'nov', 'dec'), start=1)}
#: Vehicle wording, in the shipper's own words.
RX_VEHICLE = re.compile(
    r'(III?\s*axle[^0-9]{0,24}|Trailer\s*[–-]\s*[^,]{0,40}|'
    r'Hydraulic\s*Axle[^0-9]{0,20}|Flat\s*Bed[^0-9]{0,20}|'
    r'Open\s*Body[^0-9]{0,20}|Container[^0-9]{0,20}|'
    r'LCV[^0-9]{0,12}|Truck[^0-9]{0,16})', re.I)


def _body(lead):
    return re.sub(r'[ \t]+', ' ',
                  (lead.original_email_body or lead.notes or ''))


def _shipper(body, lead):
    for rx in (RX_SHIPPER, RX_HEADER):
        hit = rx.search(body)
        if hit:
            name = hit.group(1).strip(' .|')
            if 2 < len(name) < 90:
                return name
    return (lead.company or '').strip() or '(unknown)'


def _lane(body):
    """(source, destination) or ('(unparsed)', '(unparsed)')."""
    hit = RX_ID.search(body)
    if not hit:
        return ('(unparsed)', '(unparsed)')
    tail = body[hit.end():]
    # Drop the loading date that sits between the type and the
    # source, so the address starts the segment.
    date_hit = RX_LOADING.search(tail[:60])
    if date_hit:
        tail = tail[date_hit.end():]
        tail = re.sub(r'^\s*\d{1,2}:\d{2}\s*(AM|PM)?', '', tail,
                      flags=re.I)
    segment = tail[:600]
    parts = RX_SPLIT.split(segment, maxsplit=2)
    if len(parts) < 2:
        return ('(unparsed)', '(unparsed)')
    src = parts[0].strip(' ,-')
    dst = parts[1]
    # The destination address ends at its own pincode; what follows
    # is the vehicle and the distance, run together by the table
    # flattening. Cut there rather than hoping the vehicle pattern
    # fires — it did not, and lanes came out as "Haryana → 76501590
    # Feet Trailer".
    stop = re.search(r'(?:Pin-?\s*)?\d{6}', dst)
    if stop:
        dst = dst[:stop.end()]
    else:
        veh = RX_VEHICLE.search(dst)
        if veh:
            dst = dst[:veh.start()]
    return (src[:120] or '(unparsed)',
            dst.strip(' ,-')[:120] or '(unparsed)')


#: Not a town. A lane reading "Maharashtra → Pin" is no lane.
_NOT_A_PLACE = re.compile(
    r'\A(pin|p\.?o\.?|dist|taluka|tal|village|near|opp|plot|'
    r'gat|survey|khasra|unit|phase|sector|gstin.*|india)\Z', re.I)
_STATES = {
    'maharashtra', 'haryana', 'gujarat', 'karnataka', 'tamil nadu',
    'uttar pradesh', 'west bengal', 'andhra pradesh', 'telangana',
    'rajasthan', 'punjab', 'kerala', 'odisha', 'orissa', 'bihar',
    'madhya pradesh', 'jharkhand', 'chhattisgarh', 'assam', 'goa',
    'uttarakhand', 'himachal pradesh', 'delhi', 'new delhi',
}


def _place(address):
    """The town, not the postal address.

    These read "Company, Town, State – pincode, Town, State,
    Pin-xxxxxx", so the town is the second meaningful part. Taking
    the last one gave "Pin" and the state, and a lane of two
    states is not a lane anybody can act on.
    """
    if not address or address.startswith('('):
        return '(unparsed)'
    bits = []
    for raw in re.split(r'[,–]|\s-\s', address):
        bit = raw.strip(' .-')
        if not bit or len(bit) > 48:
            continue
        if re.fullmatch(r'(Pin-?\s*)?\d{3,6}', bit):
            continue
        if _NOT_A_PLACE.match(bit):
            continue
        # A trailing code in brackets: (1000004069) a vendor
        # number, (SS-00275) or (JB-01338) a project. Both are the
        # shipper's own references and neither is part of the
        # town, which is what a lane is read on.
        bit = re.sub(r'\s*\([A-Z]{0,4}-?\d+\)\s*$', '', bit,
                     flags=re.I)
        bit = re.sub(r'\s*Pin-?\s*\d{5,6}\s*$', '', bit).strip()
        if bit:
            bits.append(bit)
    if not bits:
        return '(unparsed)'
    # The first part is the company; the next non-state part is the
    # town. Fall back to the state only when there is nothing else.
    for bit in bits[1:]:
        if bit.lower() not in _STATES:
            return bit[:34]
    return bits[-1][:34]


def _loading_date(body, received):
    """The date the load wants a vehicle, as a real date.

    The posting gives a day and a month with no year. The year is
    the one that makes the date fall on or after the email, which
    is how a human reads it and the only reading that is ever
    right: a load posted on 28 December for 3 January is next
    year's.
    """
    hit = RX_ID.search(body)
    tail = body[hit.end():] if hit else body
    found = RX_LOADING.search(tail[:80]) or RX_LOADING.search(body[:600])
    if not found:
        return None
    day = int(found.group(1))
    month = MONTHS[found.group(2).lower()[:3]]
    if found.group(3):
        try:
            return date(int(found.group(3)), month, day)
        except ValueError:
            return None
    base = (received.date() if isinstance(received, datetime)
            else received) or date.today()
    for year in (base.year, base.year + 1):
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue
        if candidate >= base:
            return candidate
    return None


def survey():
    leads = (Lead.query
             .filter(Lead.email.ilike(SENDER))
             .order_by(Lead.id.asc()).all())
    ids = [l.id for l in leads]
    audits = _audit_map(ids)
    touched = _touched(ids)

    rows = []
    today = date.today()
    for lead in leads:
        body = _body(lead)
        hit = RX_ID.search(body)
        src, dst = _lane(body)
        qty = RX_QTY.search(body)
        veh = RX_VEHICLE.search(body)
        dist = RX_DIST.search(body)
        vehicles = RX_VEHICLES.search(body)
        loading = _loading_date(body, lead.created_at)
        closed_by, closed_at, closed_why = audits.get(lead.id,
                                                      ('', '', ''))
        rows.append({
            'lead': lead.id,
            'posted': str(lead.created_at or '')[:10],
            'posting_id': hit.group(1) if hit else '',
            'type': (hit.group(2) or 'LOAD') if hit else '',
            'shipper': _shipper(body, lead),
            'from': _place(src), 'to': _place(dst),
            'from_full': src, 'to_full': dst,
            'vehicle': (veh.group(1).strip()[:48] if veh else ''),
            'vehicles_reqd': (int(vehicles.group(1))
                              if vehicles else None),
            'mt_per_vehicle': (float(qty.group(1)) if qty else None),
            'km': float(dist.group(1)) if dist else None,
            'loading_date': str(loading) if loading else '',
            'still_live': 'yes' if (loading and loading >= today)
                          else '',
            'stage': lead.stage or '',
            'owner': lead.assigned_to or '',
            'closed_by': closed_by, 'closed_at': closed_at,
            'closed_as': closed_why,
            'any_action': 'yes' if lead.id in touched else '',
            'lost_reason': (lead.lost_reason or '')[:120],
        })
    return rows


def _audit_map(ids):
    """{lead_id: (who, when, what)} — the deletion, where there was
    one, because that is what actually happened to these."""
    from app.models.audit import AuditEvent

    out = {}
    if not ids:
        return out
    for ev in (AuditEvent.query
               .filter(AuditEvent.entity_type == 'lead',
                       AuditEvent.action.in_(
                           ('lead.delete', 'lead.stage_change',
                            'lead.closed_lost')))
               .order_by(AuditEvent.id.asc()).all()):
        try:
            lid = int(ev.entity_id)
        except (TypeError, ValueError):
            continue
        if lid in ids:
            out[lid] = (ev.actor or '',
                        str(ev.occurred_at or '')[:19],
                        (ev.reason or ev.action or '')[:90])
    return out


def _touched(ids):
    """Leads where somebody did something before it was closed."""
    seen = set()
    if not ids:
        return seen
    for table, column in (('lead_activities', 'lead_id'),
                          ('lead_notes', 'lead_id'),
                          ('outreach_drafts', 'lead_id'),
                          ('rfqs', 'lead_id'), ('quotes', 'lead_id')):
        try:
            rows = db.session.execute(db.text(
                f'SELECT DISTINCT {column} FROM {table} '
                f'WHERE {column} IN ({",".join(str(i) for i in ids)})'
            )).fetchall()
            seen.update(r[0] for r in rows if r[0])
        except Exception:                                 # noqa: BLE001
            continue
    return seen


def write(rows, out_dir=None):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    root = out_dir or os.path.join(os.path.dirname(_HERE),
                                   'docs', 'exports')
    os.makedirs(root, exist_ok=True)
    path = os.path.join(
        root, f'superprocure_{date.today().isoformat()}.xlsx')

    wb = Workbook()
    head = Font(bold=True, color='FFFFFF')
    fill = PatternFill('solid', fgColor='1F2937')
    wrap = Alignment(wrap_text=True, vertical='top')

    live = [r for r in rows if r['still_live']]
    ws = wb.active
    ws.title = 'Read me'
    for line in (
        'SuperProcure load postings, and what the CRM did with them.',
        '',
        'READ ONLY. Nothing has been reopened or changed.',
        '',
        f'{len(rows)} postings. {len(live)} have a loading date '
        f'today or later.',
        '',
        'These arrive from an e-procurement portal. Each is a load '
        'a shipper has posted: route, vehicle type, tonnage, '
        'loading date, sometimes an auction time. They are '
        'enquiries.',
        '',
        '225 of them were deleted as junk — 138 in the 24 September '
        'Excel review of unassigned leads, 87 with no reason '
        'recorded — and they exist again only because of the '
        'restore on 21 October. They are at Lost because the '
        'restore put them there, not because anybody judged them.',
        '',
        'The parsing is best-effort. The posting is a table '
        'flattened into an email, so a source address ends in a '
        'pincode and the destination begins at the next capital '
        'letter. Where that boundary is not found the row says '
        '(unparsed) rather than guessing a lane.',
    ):
        ws.append([line])
        ws.cell(ws.max_row, 1).alignment = wrap
    ws.column_dimensions['A'].width = 96
    ws['A1'].font = Font(bold=True, size=13)

    cols = ('lead', 'posted', 'posting_id', 'type', 'shipper',
            'from', 'to', 'vehicle', 'vehicles_reqd',
            'mt_per_vehicle', 'km', 'loading_date', 'still_live',
            'stage', 'owner', 'closed_by', 'closed_at', 'closed_as',
            'any_action', 'from_full', 'to_full', 'lost_reason')
    s = wb.create_sheet('Postings')
    s.append(list(cols))
    for c in s[1]:
        c.font, c.fill = head, fill
    for r in rows:
        s.append([r.get(c) for c in cols])
    for i, c in enumerate(cols, start=1):
        letter = s.cell(1, i).column_letter
        s.column_dimensions[letter].width = {
            'shipper': 34, 'from': 22, 'to': 22, 'vehicle': 26,
            'closed_as': 40, 'from_full': 44, 'to_full': 44,
            'lost_reason': 44}.get(c, 13)
    s.freeze_panes = 'A2'

    if live:
        ls = wb.create_sheet('Still live')
        ls.append(['loading_date', 'lead', 'shipper', 'from', 'to',
                   'vehicle', 'vehicles_reqd', 'mt_per_vehicle',
                   'km'])
        for c in ls[1]:
            c.font, c.fill = head, fill
        for r in sorted(live, key=lambda x: x['loading_date']):
            ls.append([r['loading_date'], r['lead'], r['shipper'],
                       r['from'], r['to'], r['vehicle'],
                       r['vehicles_reqd'], r['mt_per_vehicle'],
                       r['km']])
        for col, w in (('A', 13), ('B', 8), ('C', 34), ('D', 22),
                       ('E', 22), ('F', 26), ('G', 13), ('H', 15),
                       ('I', 10)):
            ls.column_dimensions[col].width = w

    def tab(name, counter, label):
        t = wb.create_sheet(name)
        t.append([label, 'postings'])
        for c in t[1]:
            c.font, c.fill = head, fill
        for key, n in counter.most_common():
            t.append([key, n])
        t.column_dimensions['A'].width = 46
        t.column_dimensions['B'].width = 10

    tab('By month', Counter(r['posted'][:7] for r in rows), 'month')
    tab('By shipper', Counter(r['shipper'] for r in rows), 'shipper')
    tab('By lane', Counter(f"{r['from']} → {r['to']}" for r in rows),
        'lane')
    tab('By vehicle', Counter(r['vehicle'] or '(unparsed)'
                              for r in rows), 'vehicle type')
    tab('How it was closed',
        Counter(r['closed_as'] or '(no audit row)' for r in rows),
        'closed as')
    wb.save(path)
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true', default=True)
    ap.add_argument('--out-dir', dest='out_dir', default=None)
    args = ap.parse_args()

    with app.app_context():
        print(f'database: {db.engine.url}')
        print('MODE:     READ ONLY — nothing is reopened\n')
        rows = survey()
        live = [r for r in rows if r['still_live']]
        acted = [r for r in rows if r['any_action']]

        print(f'  postings                      {len(rows)}')
        print(f'  loading date today or later   {len(live)}')
        print(f'  any action before closing     {len(acted)}')
        parsed = sum(1 for r in rows if r['from'] != '(unparsed)')
        print(f'  lane parsed                   {parsed}')
        tonnes = sum((r['mt_per_vehicle'] or 0)
                     * (r['vehicles_reqd'] or 1) for r in rows)
        print(f'  tonnage across all postings   {tonnes:,.0f} MT')

        def show(title, counter, limit=8):
            print(f'\n  {title}')
            for key, n in counter.most_common(limit):
                print(f'    {n:>4}  {key}')

        show('by month', Counter(r['posted'][:7] for r in rows))
        show('top shippers', Counter(r['shipper'] for r in rows))
        show('top lanes',
             Counter(f"{r['from']} → {r['to']}" for r in rows
                     if r['from'] != '(unparsed)'))
        show('vehicle types',
             Counter(r['vehicle'] for r in rows if r['vehicle']))
        show('how each was closed',
             Counter(r['closed_as'] or '(no audit row)'
                     for r in rows), 6)

        if live:
            print(f'\n  STILL LIVE — loading today or later:')
            for r in sorted(live,
                            key=lambda x: x['loading_date'])[:15]:
                print(f'    {r["loading_date"]}  #{r["lead"]:<7}'
                      f'{r["shipper"][:28]:<30}'
                      f'{r["from"][:18]:<20}-> {r["to"][:18]}')

        path = write(rows, args.out_dir)
        print(f'\n  {path}')
        print('  READ ONLY — nothing reopened, nothing changed.')


if __name__ == '__main__':
    main()
