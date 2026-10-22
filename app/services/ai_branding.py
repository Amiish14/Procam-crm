"""
What the assistant is called where somebody can read it.

The CRM calls a vendor's model. A user talks to Procam AI. Those
are two different facts and both are worth keeping: the stored
value says how a record was actually made, which matters when a
draft reads oddly and somebody asks what wrote it, and the
displayed value says whose product this is.

So nothing is renamed in the database. `display()` is called at
the moment a value would otherwise reach a screen, an export or a
JSON response, and that is the only place the substitution
happens.
"""
from __future__ import annotations

PRODUCT = 'Procam AI'

#: Any of these in a stored model id means it came from a model we
#: do not put a vendor name to in front of a user. Matched loosely
#: on purpose: `claude-haiku-4-5-20251001`, `anthropic/claude` and
#: whatever the next version is called all read the same way.
VENDOR_MARKERS = ('claude', 'anthropic', 'gpt-', 'openai', 'groq',
                  'llama', 'qwen', 'mistral', 'gemini')


def display(value, *, default=PRODUCT):
    """The product name wherever a model id would have shown.

    An empty value gives the product name too: a draft with no
    model recorded was still written by the assistant, and a blank
    chip says nothing useful.
    """
    text = str(value or '').strip()
    if not text:
        return default
    low = text.lower()
    if any(marker in low for marker in VENDOR_MARKERS):
        return PRODUCT
    # Something that is not a model id at all — a human name on an
    # old record, say — is left as it is rather than overwritten.
    return text
