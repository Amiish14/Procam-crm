"""
A lead does not go past RFQ without a number on it.

CRM-VALUE-01.

Why
    912 of 945 open leads carry no value of any kind — not a quote,
    not an estimate. Every pipeline total, every forecast and the win
    rate are therefore floors rather than figures, and no amount of
    reporting fixes that: the number has to be asked for at the
    moment somebody knows it.

Where the line is
    At **RFQ Generated**. Before that a lead is a conversation and
    pretending to price it invents data. From RFQ Generated onward
    somebody has asked us for a price, so a value exists in the
    world even if it is a rough one, and `Won` requires it most of
    all — a win with no value is invisible to every report that
    matters.

Where the line is not
    `Lost`, `On Hold` and `Not Interested` are deliberately exempt.
    A deal can be lost before it was ever priced, and refusing to
    let somebody record that would turn an honest Lost into a lead
    that sits open forever. The gate exists to stop work moving
    *forward* without a number, not to trap records.

One rule, every path
    The lead form, the stage stepper, the Decide dialog, the bulk
    action and the integration API all go through `check()`. A rule
    enforced in four places out of five is a rule people learn to
    route around.
"""
from __future__ import annotations

#: Pipeline stages from which a value is required, plus Won. Spelt
#: out rather than derived from an index into STAGES_PIPELINE: a
#: stage inserted into the middle of that list should not silently
#: change what the business requires.
REQUIRES_VALUE = ('RFQ Generated', 'Quoted', 'Under Negotiation', 'Won')

#: The same rule for the tile and the report, which count *open*
#: leads. `Won` is terminal: it needs a value to be entered, but a won
#: deal is not something anybody can still go and work, so counting it
#: as outstanding would put a number on the Workbench that nobody can
#: take off it. The gate and the backlog are the same rule asked at
#: two different moments, and conflating them made the report list
#: leads the tile could never show.
OPEN_REQUIRES_VALUE = tuple(s for s in REQUIRES_VALUE if s != 'Won')

#: Said once, so every screen gives the same sentence.
MESSAGE = ('{stage} needs an estimated value. A lead past RFQ with no '
           'number on it is invisible to every forecast — enter the '
           'best estimate you have; it can be corrected later.')


def requires_value(stage):
    return (stage or '') in REQUIRES_VALUE


def has_value(lead):
    """Whether this lead already carries a value worth the name."""
    from app.services import lead_value
    return lead_value.value_inr(lead) is not None


def _incoming(data):
    """A value submitted alongside the stage change, if any.

    The dialogs send different names for the same thing — the Won
    dialog `value`, the stepper `quoted_amount`, the lead form
    `estimated_value`. Any of them satisfies the gate, because the
    point is that a number arrives, not which box it came from.
    """
    if not isinstance(data, dict):
        return None
    for key in ('estimated_value', 'estimated_value_inr', 'value',
                'quoted_amount', 'quoted_amount_inr', 'opportunity_value'):
        raw = data.get(key)
        if raw in (None, ''):
            continue
        try:
            if float(str(raw).replace(',', '')) > 0:
                return key
        except (TypeError, ValueError):
            continue
    return None


def check(lead, new_stage, data=None):
    """None if the move is allowed, else the sentence to show.

    Satisfied by a value already on the lead *or* one arriving in the
    same request, so the dialog can ask for it inline and the save
    goes through in one step rather than refusing and making the
    person start again.
    """
    if not requires_value(new_stage):
        return None
    if lead is not None and has_value(lead):
        return None
    if _incoming(data):
        return None
    return MESSAGE.format(stage=new_stage)


def missing_value_leads(sc=None):
    """Open leads at RFQ Generated or later carrying no value.

    The population behind the Workbench tile and the report. Scoped
    through the Access Matrix like everything else, so a rep sees
    their own and a head sees their vertical.
    """
    from app import Lead
    from app.access import scope as sc_mod
    from app.services import lead_value

    q = Lead.query.filter(Lead.is_archived.isnot(True),
                          Lead.stage.in_(OPEN_REQUIRES_VALUE))
    q = sc_mod.leads(q, sc=sc_mod.current() if sc is None else sc)
    return [l for l in q.all() if lead_value.value_inr(l) is None]
