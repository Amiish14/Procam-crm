"""
What the Lost dialog asks, now that two tasks no longer do.

`deal.lost.debrief` and `competitor.log` were raised on every lost
deal — 1,156 of each — and neither could ever be closed. One had no
completion state at all; the other waited on an Opportunity state
('Competitor Logged') that is not in the vocabulary. 2,312 rows of
work nobody could finish, asking two questions nobody answered.

The questions were good. The mechanism was wrong. A task says "go and
find out later"; the Lost dialog catches the person at the moment
they already know, and costs them one dropdown and one line of text.
Both optional: a lost reason is mandatory and these are not, because
a required field people cannot answer is how you get 4,738 leads with
no lost reason at all.

`resolve_competitor` takes either an id from the list or a new name,
because the competitor list is empty — both tables that could hold
one have no rows, and `/competitors` redirects to a Company filter on
a column Company does not have. So every name is a new name until the
list builds itself.
"""
from __future__ import annotations

from app import db
from app.models.competitor import CompetitorMaster

#: A debrief is a note, not an essay. Longer than this and it belongs
#: in the lead's notes, where it is searchable and versioned.
MAX_NOTE = 2000


class LostCaptureError(ValueError):
    """Something in the dialog cannot be stored as given."""


def competitor_choices(active_only=True, limit=500):
    """The list the dialog offers, alphabetically."""
    q = CompetitorMaster.query
    if active_only:
        q = q.filter(CompetitorMaster.is_active.is_(True))
    return [{'id': c.id, 'name': c.name}
            for c in q.order_by(CompetitorMaster.name.asc()).limit(limit)]


def resolve_competitor(competitor_id=None, name=None, actor=None):
    """A CompetitorMaster from an id or a name, or None.

    A name that already exists — case-insensitively — returns the
    existing row rather than a second one. "Add new" that quietly
    creates 'DHL', 'dhl' and 'Dhl' is how a picklist stops being a
    picklist.
    """
    if competitor_id not in (None, '', 0, '0'):
        try:
            row = db.session.get(CompetitorMaster, int(competitor_id))
        except (TypeError, ValueError):
            raise LostCaptureError('That competitor id is not a number.')
        if row is None:
            raise LostCaptureError('No competitor with that id.')
        return row

    probe = (name or '').strip()
    if not probe:
        return None
    if len(probe) > 240:
        raise LostCaptureError('Competitor name is too long.')
    existing = next(
        (c for c in CompetitorMaster.query.all()
         if (c.name or '').strip().lower() == probe.lower()), None)
    if existing is not None:
        return existing
    # Created straight from the Lost dialog: anybody losing a deal may
    # name who took it. Curating the list afterwards is admin work —
    # see `_can_edit` in app/competitor/routes.py.
    row = CompetitorMaster(name=probe, is_active=True,
                           created_by_id=actor)
    db.session.add(row)
    db.session.flush()
    return row


def apply(lead, data, actor=None):
    """Write the two optional answers onto the lead.

    Returns what was recorded, for the caller to report back. Runs in
    the caller's transaction: the decision and what was learnt from it
    belong to the same save.
    """
    note = (data.get('debrief_note') or '').strip()
    if len(note) > MAX_NOTE:
        raise LostCaptureError(
            f'Keep the debrief under {MAX_NOTE} characters — anything '
            f'longer belongs in the lead notes.')

    competitor = resolve_competitor(
        competitor_id=data.get('lost_to_competitor_id'),
        name=data.get('lost_to_competitor_name'), actor=actor)

    if competitor is not None:
        lead.lost_to_competitor_id = competitor.id
    if note:
        lead.debrief_note = note
    return {'lost_to_competitor_id': (competitor.id if competitor
                                      else None),
            'lost_to_competitor': (competitor.name if competitor
                                   else ''),
            'debrief_note': note}
