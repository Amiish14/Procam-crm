"""
Customer PO capture — the gate between a Won deal and TMS.

Process change (Sept 2026).  The old order was:

    Won  →  create Project + Job in TMS  →  raise the PO against them

which let one PO carry several projects, and let a project exist with no
customer commitment behind it at all.  The order is now:

    Won  →  capture Customer PO / Sales Order / Contract  →  create
            the Project and Job against that PO

Because a handover is what becomes one Project and one Job in TMS, and a
PO reference can be attached to only one handover, the sequence itself
guarantees `One PO = One Project and Job`.

Uniqueness is scoped to the customer, not the whole database: two
customers can each legitimately raise `PO/2026/001`, but the same
customer raising it twice is a duplicate.
"""
import re
from datetime import date, datetime

from app.models.tms_handover import WonHandover, HandoverStatus, PoType


def norm_ref(ref):
    """Fold a PO reference for comparison.

    Customers write the same PO number many ways — `PO-2026/001`,
    `po 2026 001`, `PO/2026-001`.  Comparing the raw string would let an
    obvious duplicate through, so separators and case are folded away.
    Only for matching; the reference is always *stored* as typed.
    """
    return re.sub(r'[^a-z0-9]', '', (ref or '').lower())


def _customer_key(row):
    """What 'the same customer' means for uniqueness.

    account_id when the handover is linked to a Company Master record;
    otherwise the folded account name, so unlinked rows still collide
    with their own duplicates instead of with everyone else's.
    """
    if row.account_id:
        return ('id', row.account_id)
    return ('name', norm_ref(row.account_name))


def find_duplicate(ref, account_id=None, account_name=None, exclude_id=None):
    """Return the handover already holding this PO for this customer.

    None when the reference is free to use.
    """
    key = norm_ref(ref)
    if not key:
        return None
    probe = WonHandover(account_id=account_id, account_name=account_name)
    want = _customer_key(probe)

    q = WonHandover.query.filter(WonHandover.po_ref.isnot(None),
                                 WonHandover.status != HandoverStatus.CANCELLED)
    if exclude_id:
        q = q.filter(WonHandover.id != exclude_id)
    # po_ref is indexed but the comparison is on the folded form, so the
    # index can't do the work — the candidate set is small (one customer's
    # POs), and correctness beats a scan here.
    for other in q.all():
        if norm_ref(other.po_ref) == key and _customer_key(other) == want:
            return other
    return None


def _parse_date(v):
    if not v:
        return None
    if isinstance(v, date):
        return v
    try:
        return datetime.strptime(str(v)[:10], '%Y-%m-%d').date()
    except ValueError:
        return None


def capture(row, data, actor=None):
    """Record the customer commitment on a handover.

    Returns (ok, error).  On success the row is advanced out of
    `Awaiting PO` and is ready for TMS — but nothing is committed; the
    caller owns the transaction.
    """
    ref = (data.get('po_ref') or '').strip()
    if not ref:
        return False, 'A PO / Sales Order / Contract reference is required.'

    po_type = (data.get('po_type') or '').strip() or PoType.PO
    if po_type not in PoType.CHOICES:
        return False, (f'po_type must be one of: '
                       f'{", ".join(PoType.CHOICES)}.')

    clash = find_duplicate(ref, account_id=row.account_id,
                           account_name=row.account_name,
                           exclude_id=row.id)
    if clash is not None:
        return False, (
            f'{po_type} "{ref}" is already recorded on handover #{clash.id} '
            f'({clash.account_name or "unnamed account"}). One PO means one '
            f'Project and one Job — raise a separate PO, or amend that '
            f'handover instead.')

    row.po_ref = ref
    row.po_type = po_type
    row.po_date = _parse_date(data.get('po_date'))
    row.po_currency = (data.get('po_currency') or 'INR').strip()[:6] or 'INR'
    try:
        row.po_value = float(data.get('po_value') or 0) or None
    except (TypeError, ValueError):
        return False, 'po_value must be a number.'

    if not row.po_captured_at:
        row.po_captured_by = actor
        row.po_captured_at = datetime.utcnow()

    if row.status == HandoverStatus.AWAITING_PO:
        row.status = HandoverStatus.PENDING
    return True, None


def blocks_tms(row):
    """Why TMS creation is not allowed yet, or None when it is allowed.

    This is the whole point of the change: no Project, no Job, no TMS ids
    until the customer has committed in writing.
    """
    if not row.po_ref:
        return ('This deal has no Customer PO / Sales Order / Contract yet. '
                'Capture it first — the Project and Job are created against '
                'the PO, not before it.')
    return None
