"""
Can this task ever be finished?

Two task definitions put 2,312 immortal rows on the board and nobody
noticed for two months. `deal.lost.debrief` had no completion state
at all; `competitor.log` waited for an Opportunity to reach
'Competitor Logged', which is not one of the six opportunity stages.
A third, `account.first_contact`, waited on 'Contact Made' while the
account stage is 'Contact Established'.

The second kind is the dangerous one, because it *looks* fixed: the
field is filled in, and a check that asks only "is there a completion
state?" passes. A state no code path emits is the same as no state.

This module is the rule, separated from both the database and the
screen so it can be tested with definitions made up for the purpose —
the live catalogue is seeded by a migration and absent from the test
database, so a check that reads the table passes vacuously there.
"""
from __future__ import annotations

#: Entities whose state vocabulary we can enumerate. A definition
#: about an RFQ or a Quote is reported as unknown rather than wrong:
#: guessing a vocabulary and failing on the guess is worse than
#: saying "not checked".
KNOWN_ENTITIES = ('lead', 'company')

#: Strings the engine is fired with that are not stages. `__created__`
#: fires on creation; `*` matches anything; `Worked` is what the
#: account-activity route emits to mean "somebody touched this today",
#: which is what closes `account.follow_up_due`.
NOT_STAGES = ('__created__', '*', 'Worked')


def reachable_states():
    """Every state string the system can actually produce."""
    out = set(NOT_STAGES)
    try:
        from app import STAGES_ALL
        out |= set(STAGES_ALL)
    except Exception:                                     # noqa: BLE001
        pass
    try:
        from presales.models import ACCOUNT_DEV_STAGES
        out |= set(ACCOUNT_DEV_STAGES)
    except Exception:                                     # noqa: BLE001
        pass
    return {s.lower() for s in out}


def problems(definitions, reachable=None):
    """[(task_key, kind, detail)] for every definition that cannot work.

    `definitions` is anything with `.task_key`, `.is_active`,
    `.start_state` and `.completion_state` — the model, or a stand-in.
    Only active ones are judged: a retired definition that can never
    close is retired, which is the point of retiring it.
    """
    reachable = reachable_states() if reachable is None else reachable
    found = []
    for d in definitions:
        if not getattr(d, 'is_active', True):
            continue
        key = getattr(d, 'task_key', '?')
        if not d.completion_state:
            found.append((key, 'no completion state',
                          'every task it raises is immortal'))
            continue
        for which, rule in (('start', d.start_state),
                            ('completion', d.completion_state)):
            if not isinstance(rule, dict):
                continue
            entity = str(rule.get('entity', '')).lower()
            state = str(rule.get('state', ''))
            if entity not in KNOWN_ENTITIES:
                continue
            if state.lower() not in reachable:
                found.append((key, f'unreachable {which} state',
                              f'{entity}/{state!r} is not something any '
                              f'code path emits'))
    return found
