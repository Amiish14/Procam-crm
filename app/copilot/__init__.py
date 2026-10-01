"""
Procam AI — the Copilot package.

Intent modules register themselves by import side-effect: importing
`app.copilot.queries` runs its `@intent(...)` decorators and the
catalogue in `app/copilot/intents.py` gains those rows. `service.py`
imports the modules it was shipped with for exactly that reason.

Release 6 (Group L) adds `workbench_intents`, and may not edit
`service.py` to say so. Importing it here achieves the same thing one
step earlier: this file runs before any submodule of the package, so by
the time anything asks the catalogue a question, Group L's intents and
its routing rules are in it. `install_patterns()` is idempotent and
skips any rule whose intent `service.py` already routes, so moving
those rules into `_PATTERNS` later needs no change here.
"""
from app.copilot import workbench_intents as _workbench_intents  # noqa: F401

_workbench_intents.install_patterns()
