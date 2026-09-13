"""
The intent catalogue — the matrix from §14.1, as code.

Every question Procam AI can answer is one of these. An intent names
the permission it needs, the parameters it accepts, and the function
that answers it; nothing else in the Copilot may reach the database.

Why a catalogue rather than letting the model write queries
    Because then the answer's quality is decided before the model is
    called — by which table was queried and which filter was applied —
    and because a model that cannot compose a query cannot compose one
    that escapes the caller's scope. §9's injection defence is
    structural for exactly this reason: there is no path from the model
    to a query, only from an intent to a template.

Adding an intent
    Register it here with @intent(...). Give it the permission from the
    matrix, write the handler to take (scope, params) and return a
    Result, and add its questions to the library. The handler receives
    an already-resolved scope and must pass it to every query helper —
    it never resolves one itself.
"""
from __future__ import annotations

from dataclasses import dataclass, field


#: Every registered intent, by key.
REGISTRY = {}


@dataclass
class Result:
    """What an intent hands back. Never prose — prose is the narrator's
    job, and it is given only this."""
    #: One line stating the answer. Rendered as-is; the model may
    #: rephrase around it but the figures here are final.
    headline: str = ''
    #: Column headers for `rows`, in order.
    columns: list = field(default_factory=list)
    #: Rows of primitives. Each may carry a '_chip' key naming a record
    #: the UI should make clickable: {'type': 'lead', 'id': 42}.
    rows: list = field(default_factory=list)
    #: Named figures the narrator may quote verbatim and nothing else.
    figures: dict = field(default_factory=dict)
    #: Where each material fact came from, for §3.5 citations.
    sources: list = field(default_factory=list)
    #: True when the honest answer is "not in the CRM".
    empty: bool = False
    #: Set when the data exists but the viewer may not have the detail —
    #: §6.6's safe form rather than an access-denied error.
    restricted: bool = False
    #: Free-text caveats: incomplete data, an assumption, a window.
    notes: list = field(default_factory=list)
    #: True when the rows are suggestions rather than facts — the panel
    #: styles them apart so "call this lead" is never read as a record.
    recommendation: bool = False
    #: §3.5 record-level citations: [{type, id, label, source?, date?}].
    #: When a handler leaves this empty the service derives it from the
    #: rows' chips, so every row-level fact still links to its record.
    citations: list = field(default_factory=list)
    #: The filters the handler actually applied, in words — for the
    #: "how I answered" line. Values, never the query that used them.
    filters: dict = field(default_factory=dict)
    #: Set when the honest answer is a question back: the name matched
    #: several accounts, or a needed value is missing.
    #: {'question': str, 'options': [{'label': str, 'question': str}]}
    clarification: dict | None = None

    def to_dict(self):
        return {
            'headline': self.headline, 'columns': self.columns,
            'rows': self.rows, 'figures': self.figures,
            'sources': self.sources, 'empty': self.empty,
            'restricted': self.restricted, 'notes': self.notes,
            'recommendation': self.recommendation,
            'citations': self.citations, 'filters': self.filters,
            'clarification': self.clarification,
        }


@dataclass
class Intent:
    key: str
    #: What a person would call it, for the suggested-question chips.
    label: str
    #: The Access-Matrix permission required, or None when the answer is
    #: safe at every scope (ownership routing, per §6.6).
    permission: str | None
    #: Parameters the classifier may extract, name → description. The
    #: descriptions go into the classifier prompt.
    params: dict
    handler: object
    #: Which persona chips this appears under.
    personas: tuple = ()
    #: Phase it belongs to, so a phase can be enabled as a unit.
    phase: int = 1
    #: Example questions — used by the classifier and by the library.
    examples: tuple = ()

    def to_dict(self):
        return {'key': self.key, 'label': self.label,
                'permission': self.permission,
                'params': self.params, 'personas': list(self.personas),
                'phase': self.phase, 'examples': list(self.examples)}


def intent(key, label, *, permission=None, params=None, personas=(),
           phase=1, examples=()):
    def register(fn):
        REGISTRY[key] = Intent(key=key, label=label, permission=permission,
                               params=params or {}, handler=fn,
                               personas=tuple(personas), phase=phase,
                               examples=tuple(examples))
        return fn
    return register


def get(key):
    return REGISTRY.get(key)


def available(scope, *, max_phase=None):
    """Intents this viewer could actually use.

    Filtered by entitlement so the suggested-question chips never offer
    something that will refuse — §6.2 says the chips are role-aware, and
    a chip that always fails teaches people the Copilot is broken.
    """
    out = []
    for it in REGISTRY.values():
        if max_phase is not None and it.phase > max_phase:
            continue
        if it.permission and not scope.can(it.permission):
            continue
        out.append(it)
    return sorted(out, key=lambda i: (i.phase, i.label))


def catalogue(*, max_phase=None):
    """The whole catalogue, for the classifier prompt and for docs."""
    return [i.to_dict() for i in sorted(
        REGISTRY.values(), key=lambda i: (i.phase, i.key))
        if max_phase is None or i.phase <= max_phase]
