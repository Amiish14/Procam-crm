"""Archive one lead, and put it back.

CRM-USER-ISSUES-02NOV §1: "There's no delete/archive option, even for
super admin."

WHAT WAS ALREADY HERE, AND WHAT WAS NOT

The columns have existed since §58: `is_archived`, `archived_at`,
`archived_by`, `archive_reason`. So has the writer — `bulk_admin`'s
`archive()` and `unarchive()` — and the twenty-six modules that hide
archived leads from lists, dashboards and reports. What was missing was
a way for a person to reach any of it: the only entrance was the bulk
admin screen, which takes a list of ids and is not where anybody works.
So the data model is not extended here and no second writer is
introduced; this module is the per-lead door onto the one that exists.

That matters for more than tidiness. A second writer would set the four
columns without the `DeletionAudit` row and the batch event that
`bulk_admin` writes, and the audit trail would then be complete for a
bulk archive and silent for the archive a person actually performed.

WHO MAY

The owner, the vertical head of the lead's vertical, or a CRM admin —
which is the rule `junk_delete.may_delete` already states and already
tests. It is imported rather than restated: two copies of a permission
rule drift, and the copy that drifts is the one that lets somebody
through. A super admin is an admin by role, so §1's "even for super
admin" is covered by the same clause.

THE REASON IS NOT OPTIONAL

Required here rather than only in the browser: a reason enforced in
JavaScript is a reason absent from every other caller. MIN_REASON is
what stops "x" and "." counting as an explanation.

A CATEGORY, AND THEN THE DETAIL (CRM-WA-01 §1)

This started as free text, on the reasoning that archiving is not a
closed set of outcomes — "client went quiet", "budget pulled". The PFM
team asked for four named reasons instead: duplicate, junk, test, and
lost before qualifying. Both are right about something. A category is
what makes the archive countable — "how many were duplicates" has no
answer when every row is a sentence — and the sentence is what makes
one row explicable: "duplicate" does not say duplicate of what.

So a category is required and a note is optional, and the two are
stored in the one `archive_reason` column as `category: note`. No
migration: the rows archived before this read back as uncategorised,
which is what they are, rather than being assigned a category nobody
chose.

NOTHING IS DELETED

CRM-NO-DELETE-01 holds. `is_archived` is a flag; the row, its notes,
its quotes and its mail trail all stay. Restoring clears the four
columns and writes its own audit row, so a lead that went and came back
reads as two events rather than as never having left.
"""
from __future__ import annotations

#: Shorter than this is not a reason. Five characters admits "spam"
#: and "dupe" — both of which a person means — and refuses the stray
#: keypress that a mandatory field otherwise collects.
MIN_REASON = 4

#: The column is 200 characters. Truncating silently would file half a
#: sentence as the explanation, so a longer reason is refused and the
#: person is told the limit rather than discovering it afterwards.
MAX_REASON = 200

#: The four reasons CRM-WA-01 §1 names, as (code, label, what it means).
#: Codes rather than labels in the stored value, so renaming a label
#: on the screen does not orphan every row filed under the old words.
CATEGORIES = (
    ('duplicate', 'Duplicate',
     'the same enquiry is already on the board under another number'),
    ('junk', 'Junk',
     'a newsletter, a circular, or mail that is not an enquiry'),
    ('test', 'Test',
     'a record somebody made to try the system'),
    ('lost_before_qualifying', 'Lost before qualifying',
     'it never became a real opportunity — no scope, no budget, no '
     'reply'),
)

CATEGORY_CODES = tuple(code for code, _label, _what in CATEGORIES)
CATEGORY_LABELS = {code: label for code, label, _what in CATEGORIES}

#: What an archive filed before §1 reads as. Not a category — it is
#: the absence of one, and calling it "other" would invent an answer.
UNCATEGORISED = ''


class Refused(ValueError):
    """Why this archive or restore may not happen, in a sentence."""


def is_archived(lead):
    return bool(getattr(lead, 'is_archived', False))


def may_archive(lead, emp_code):
    """Whether this person may archive or restore this lead.

    The same three-way rule as the junk delete, imported rather than
    restated. See the module docstring.
    """
    if lead is None or not emp_code:
        return False
    try:
        from app.services import junk_delete
        return bool(junk_delete.may_delete(lead, emp_code))
    except Exception:                                     # noqa: BLE001
        return False


def why_not(lead, emp_code):
    """The sentence a screen shows when the button is not available.

    Returns '' when the person may act, so a caller can use it as the
    whole answer rather than asking twice.
    """
    if lead is None:
        return 'That lead does not exist.'
    if not emp_code:
        return 'Sign in first.'
    if may_archive(lead, emp_code):
        return ''
    return ('Only the lead\'s owner, the head of its vertical, or a CRM '
            'administrator can archive it.')


def check_reason(reason):
    """The reason, cleaned — or Refused saying what is wrong with it.

    Returned rather than silently corrected, because the reason is read
    later by a person deciding whether the archive was right.
    """
    text = (reason or '').strip()
    if not text:
        raise Refused('Say why this lead is being archived.')
    if len(text) < MIN_REASON:
        raise Refused(f'That is too short to be a reason — '
                      f'{MIN_REASON} characters or more.')
    if len(text) > MAX_REASON:
        raise Refused(f'That reason is {len(text)} characters; '
                      f'{MAX_REASON} is the most that is kept.')
    return text


def check_category(category):
    """One of the four, or Refused naming them.

    Refused rather than defaulted. A default category would be
    assigned to every archive nobody thought about, and the count it
    produces would then be read as a finding.
    """
    code = (category or '').strip().lower().replace('-', '_')
    code = code.replace(' ', '_')
    if not code:
        raise Refused('Choose why this lead is being archived: '
                      + ', '.join(CATEGORY_LABELS[c]
                                  for c in CATEGORY_CODES) + '.')
    if code not in CATEGORY_CODES:
        raise Refused(f'{category!r} is not one of the reasons. '
                      f'Choose from: '
                      + ', '.join(CATEGORY_CODES) + '.')
    return code


def compose_reason(category, note=''):
    """The one string the `archive_reason` column holds.

    `category: note`, or the category alone. One column rather than
    two because the column already exists and already holds every
    archive filed before §1 — a second column would leave the old
    rows answering one question and the new rows the other.
    """
    code = check_category(category)
    detail = (note or '').strip()
    text = f'{code}: {detail}' if detail else code
    if len(text) > MAX_REASON:
        raise Refused(f'That note is too long — the category and the '
                      f'note together must fit {MAX_REASON} '
                      f'characters, and this is {len(text)}.')
    return text


def split_reason(reason):
    """(category, note) back out of a stored reason.

    An archive filed before §1 has no category, and this says so with
    an empty one rather than guessing from the words — "duplicate of
    10291" looks like the duplicate category and might equally have
    been written by somebody who meant something else by it.
    """
    text = (reason or '').strip()
    if not text:
        return UNCATEGORISED, ''
    head, _, tail = text.partition(':')
    code = head.strip().lower().replace('-', '_').replace(' ', '_')
    if code in CATEGORY_CODES:
        return code, tail.strip()
    return UNCATEGORISED, text


def archive(lead, *, actor, category=None, note='', reason=None):
    """Archive one lead. Returns what bulk_admin reports.

    Refused when the person may not, when the reason is not one, and
    when the lead is already archived — the last because an archive of
    an archived lead would overwrite the first reason and the first
    date, losing why it actually went.

    Called either with `category` (and an optional `note`), which is
    what §1 asks for, or with `reason` as one string, which is what
    every caller written before §1 passes. The second form is kept
    working rather than migrated: a bulk tool or a script that archives
    with a sentence should not start failing because the lead screen
    grew a dropdown.
    """
    if not may_archive(lead, actor):
        raise Refused(why_not(lead, actor))
    if is_archived(lead):
        raise Refused(f'Lead #{lead.id} is already archived.')
    if category:
        text = compose_reason(category, note)
    else:
        text = check_reason(reason)

    from app.bulk_admin import service as bulk
    out = bulk.archive([lead.id], text, actor)
    # bulk.archive() filters on is_archived, so a lead that was
    # archived between the check above and the write comes back as
    # zero. Saying so is better than reporting an archive that did
    # not happen.
    if not out.get('archived'):
        raise Refused(f'Lead #{lead.id} was not archived — somebody '
                      f'may have archived it a moment ago.')
    return out


def restore(lead, *, actor):
    """Put an archived lead back into the active pipeline."""
    if not may_archive(lead, actor):
        raise Refused(why_not(lead, actor))
    if not is_archived(lead):
        raise Refused(f'Lead #{lead.id} is not archived.')

    from app.bulk_admin import service as bulk
    out = bulk.unarchive([lead.id], actor)
    if not out.get('restored'):
        raise Refused(f'Lead #{lead.id} was not restored.')
    return out


def state_for(lead, emp_code):
    """What a screen needs to draw the button, in one call."""
    stored = (getattr(lead, 'archive_reason', '') or '')
    code, note = split_reason(stored)
    return {
        'archived': is_archived(lead),
        'may': may_archive(lead, emp_code),
        'why_not': why_not(lead, emp_code),
        'reason': stored,
        'category': code,
        'category_label': CATEGORY_LABELS.get(code, ''),
        'note': note,
        'archived_by': (getattr(lead, 'archived_by', '') or ''),
        'archived_at': (str(getattr(lead, 'archived_at', '') or '')[:16]),
        'min_reason': MIN_REASON,
        'max_reason': MAX_REASON,
        #: So the dropdown is drawn from one list rather than a copy
        #: in the template that drifts from the one enforced here.
        'categories': [{'code': c, 'label': l, 'what': w}
                       for c, l, w in CATEGORIES],
    }
