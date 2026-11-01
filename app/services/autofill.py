"""
The CRM fills it in; the person confirms it.

CRM-COCKPIT-02 §5. The finding behind this one is the sharpest in the
whole prompt: `email_extracted` has held origin and destination for
about 400 leads, cargo type for 420, weight for 197 and dimensions for
122 since the extractor shipped — and `leads.origin`,
`leads.destination`, `industry`, `state` and `city` were empty on all
915 open leads. People were being asked to type what the CRM had
already read and thrown away.

Three rules, and the third is the one that keeps this honest:

  1. **copy, do not overwrite.** A field a person has filled is never
     replaced by a machine's reading of a mail. Autofill only ever
     writes into a blank.
  2. **the account carries industry, state and city** (§5.2). Those
     describe the customer, not the enquiry, so they belong on the
     account and every new lead inherits them. A lead that disagrees
     with its account about which state the customer is in is a
     reporting bug waiting to be found.
  3. **everything filled this way is marked.** `auto_filled_json`
     lists the fields nobody has agreed with yet, and the screen shows
     "auto — confirm" beside each. A figure the CRM read and a figure
     its owner typed are not the same thing, and a CRM that cannot
     tell them apart teaches people to distrust both.

Confirming is per field, and confirming is just removing the field
from that list — so "the owner checked the weight" survives the next
mail arriving on the same lead.
"""
from __future__ import annotations

import json
import logging

log = logging.getLogger(__name__)

#: extracted key → lead attribute. Only these: the extractor also
#: returns a company name, a contact and a vertical, and those three
#: have their own gates (CRM-LEAD-ACCOUNT-01, the contact service,
#: CRM-VERTICAL-MASTER-01) which this must not go around.
FROM_MAIL = {
    'origin': 'origin',
    'destination': 'destination',
    'cargo_type': 'cargo_type',
    'cargo_qty': 'cargo_qty',
    'cargo_weight_mt': 'cargo_weight_mt',
    'cargo_dimensions': 'cargo_dimensions',
    'urgency': 'urgency',
    'special_requirements': 'special_requirements',
}

#: §5.2 — what the account decides, not the mail.
FROM_ACCOUNT = {'industry': 'industry', 'state': 'state', 'city': 'city'}

#: §5.4 — what the quote reader fills. Not written by this module,
#: but marked by it, so the confirm endpoint accepts them and the
#: screen shows "auto — confirm" against a figure read off a PDF.
FROM_QUOTE_DOCUMENT = ('quote_no', 'quote_value_num', 'quote_date',
                       'quote_validity_date')

#: Everything that can be marked auto, for the screen's benefit.
FIELDS = (tuple(FROM_MAIL.values()) + tuple(FROM_ACCOUNT.values())
          + FROM_QUOTE_DOCUMENT)


def _extracted(lead):
    raw = getattr(lead, 'email_extracted_json', None)
    if not raw:
        return {}
    try:
        out = json.loads(raw)
        return out if isinstance(out, dict) else {}
    except Exception:                                     # noqa: BLE001
        return {}


def auto_fields(lead):
    """The fields on this lead the CRM filled and nobody confirmed."""
    raw = getattr(lead, 'auto_filled_json', None)
    if not raw:
        return []
    try:
        out = json.loads(raw)
        return [f for f in out if isinstance(f, str)]
    except Exception:                                     # noqa: BLE001
        return []


def _set_auto_fields(lead, names):
    names = sorted({n for n in names if n})
    lead.auto_filled_json = json.dumps(names) if names else None


def mark_auto(lead, field):
    fields = auto_fields(lead)
    if field not in fields:
        fields.append(field)
        _set_auto_fields(lead, fields)


def confirm(lead, field=None):
    """A person agrees with one auto-filled field, or with all of them.

    Returns what is still unconfirmed.
    """
    fields = auto_fields(lead)
    if field is None:
        _set_auto_fields(lead, [])
        return []
    _set_auto_fields(lead, [f for f in fields if f != field])
    return auto_fields(lead)


def _blank(value):
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    return False


def _clean(key, value):
    """A value from the extractor, as the column wants it."""
    if value is None:
        return None
    if key == 'special_requirements' and isinstance(value, (list, tuple)):
        value = '; '.join(str(v).strip() for v in value if str(v).strip())
    if key == 'cargo_weight_mt':
        try:
            return float(str(value).replace(',', '').strip() or 0) or None
        except (TypeError, ValueError):
            return None
    if key == 'urgency':
        text = str(value).strip().capitalize()
        return text[:20] if text in ('High', 'Medium', 'Low') else None
    text = str(value).strip()
    if not text or text.lower() in ('none', 'null', 'unknown', 'n/a'):
        return None
    return text[:200]


def _write(lead, field, value, *, log=None, dry=False):
    """Fill one blank. `log` makes it reversible, `dry` makes it a
    question rather than a change.

    One function for every write in this module, so a back-fill and
    the live intake cannot fill a field in two different ways — and so
    the reversible log (§8) cannot be bypassed by whichever path is
    written next.
    """
    if dry:
        return True
    if log is not None:
        log.set(lead, field, value)
    else:
        setattr(lead, field, value)
    mark_auto(lead, field)
    return True


def from_mail(lead, *, log=None, dry=False):
    """§5.1 — copy what the extractor read into the lead's own fields.

    Returns {field: value} for what it filled. Blanks only.
    """
    data = _extracted(lead)
    if not data:
        return {}
    filled = {}
    for key, field in FROM_MAIL.items():
        if not _blank(getattr(lead, field, None)):
            continue        # a person's value, or an earlier fill
        value = _clean(key, data.get(key))
        if value is None:
            continue
        _write(lead, field, value, log=log, dry=dry)
        filled[field] = value
    return filled


def from_account(lead, account=None, *, log=None, dry=False):
    """§5.2 — industry, state and city come from the account."""
    if account is None:
        account_id = getattr(lead, 'company_id', None)
        if not account_id:
            return {}
        try:
            from app import Company, db
            account = db.session.get(Company, account_id)
        except Exception:                                 # noqa: BLE001
            return {}
    if account is None:
        return {}
    filled = {}
    for field, source in FROM_ACCOUNT.items():
        if not _blank(getattr(lead, field, None)):
            continue
        value = _clean(field, getattr(account, source, None))
        if value is None:
            continue
        _write(lead, field, value, log=log, dry=dry)
        filled[field] = value
    return filled


def apply(lead, *, account=None, log=None, dry=False):
    """Both, in the order that matters.

    The account goes first: where the mail and the account disagree
    about a customer's city, the account is the record of the customer
    and the mail is one enquiry's signature block.

    `log` is a `bulk_log` batch, which makes every field it writes
    undoable. `dry` answers what it would fill and changes nothing,
    which is what a --check run needs.
    """
    filled = {}
    filled.update(from_account(lead, account, log=log, dry=dry))
    filled.update(from_mail(lead, log=log, dry=dry))
    return filled


# ── §5.2 the account's own gaps ──────────────────────────────────────
#: What an account needs before its leads can inherit anything.
ACCOUNT_FIELDS = ('industry', 'state', 'city')


def account_gaps(account):
    """Which of industry, state and city this account is missing."""
    return [f for f in ACCOUNT_FIELDS
            if _blank(getattr(account, f, None))]


def accounts_missing_details(sc=None, limit=None):
    """§5.2's "Account details missing" list, for the owner's screen.

    Accounts with at least one open lead, because an account nobody is
    selling to is not a gap worth anybody's morning.
    """
    from app import Company, Lead, db
    from app.services import sales_rules as rules

    q = (db.session.query(Company, db.func.count(Lead.id))
         .join(Lead, Lead.company_id == Company.id)
         .filter(Lead.is_archived.isnot(True),
                 Lead.stage.in_(rules.open_stages()))
         .group_by(Company.id))
    out = []
    for account, leads in q.all():
        gaps = account_gaps(account)
        if not gaps:
            continue
        out.append({'id': account.id, 'name': account.name or '',
                    'missing': gaps, 'open_leads': leads,
                    'vertical': account.vertical or ''})
    out.sort(key=lambda r: (-r['open_leads'], r['name']))
    return out[:limit] if limit else out


# ── §5.2 filling an account in from what we hold ─────────────────────
#: The first two digits of an Indian GSTIN are the state code. This is
#: the whole list, because an account's state read off its GST number
#: is right more often than one typed by whoever created the account.
GSTIN_STATES = {
    '01': 'Jammu and Kashmir', '02': 'Himachal Pradesh',
    '03': 'Punjab', '04': 'Chandigarh', '05': 'Uttarakhand',
    '06': 'Haryana', '07': 'Delhi', '08': 'Rajasthan',
    '09': 'Uttar Pradesh', '10': 'Bihar', '11': 'Sikkim',
    '12': 'Arunachal Pradesh', '13': 'Nagaland', '14': 'Manipur',
    '15': 'Mizoram', '16': 'Tripura', '17': 'Meghalaya',
    '18': 'Assam', '19': 'West Bengal', '20': 'Jharkhand',
    '21': 'Odisha', '22': 'Chhattisgarh', '23': 'Madhya Pradesh',
    '24': 'Gujarat', '25': 'Daman and Diu', '26': 'Dadra and Nagar Haveli',
    '27': 'Maharashtra', '28': 'Andhra Pradesh', '29': 'Karnataka',
    '30': 'Goa', '31': 'Lakshadweep', '32': 'Kerala',
    '33': 'Tamil Nadu', '34': 'Puducherry', '35': 'Andaman and Nicobar',
    '36': 'Telangana', '37': 'Andhra Pradesh', '38': 'Ladakh',
    '97': 'Other Territory',
}


def state_from_gstin(gstin):
    code = (gstin or '').strip()[:2]
    return GSTIN_STATES.get(code)


def guess_account_details(account):
    """{field: (value, how)} for an account's missing details.

    Three sources, strongest first: the GSTIN's state code, the
    address, and the leads already filed against the account. Nothing
    is guessed from the name — "Gujarat Ambuja" is a cement company in
    Maharashtra.
    """
    from app import Lead, db

    out = {}
    gaps = account_gaps(account)
    if 'state' in gaps:
        state = state_from_gstin(getattr(account, 'gstin', ''))
        if state:
            out['state'] = (state, 'the GSTIN state code')

    address = (getattr(account, 'address', '') or '').strip()
    if address:
        if 'city' in gaps and 'city' not in out:
            # The last comma-separated part that is not a PIN code.
            parts = [p.strip() for p in address.replace('\n', ',').split(',')
                     if p.strip()]
            for part in reversed(parts):
                digits = part.replace(' ', '')
                if digits.isdigit() or len(part) < 3:
                    continue
                if part.lower() in ('india',):
                    continue
                out['city'] = (part[:80], 'the address')
                break

    # What the leads say. Only where they agree: two leads naming two
    # cities is not an answer, it is a question for the owner.
    try:
        for field in ('industry', 'state', 'city'):
            if field not in gaps or field in out:
                continue
            rows = (db.session.query(getattr(Lead, field),
                                     db.func.count(Lead.id))
                    .filter(Lead.company_id == account.id,
                            getattr(Lead, field).isnot(None),
                            getattr(Lead, field) != '')
                    .group_by(getattr(Lead, field)).all())
            rows = [(v, n) for v, n in rows if (v or '').strip()]
            if len(rows) == 1:
                out[field] = (rows[0][0][:80], 'its leads all say so')
    except Exception:                                     # noqa: BLE001
        pass
    return out
