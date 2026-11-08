"""Who is on a lead, and by what right.

CRM-FINAL-CLOSEOUT-01 §6: "Lead Team on each lead (owner + members by
role, no names in code)."

WHY A LEAD NEEDS A TEAM AND NOT AN OWNER

`leads.assigned_to` holds one code, and every screen reads it as "the
person responsible". It is not the only person involved and never
was: the secondary owner covers leave, the vertical head answers for
the number, the account's own owner has the relationship, whoever was
handed a task is doing the work this week, and the RFQ coordinator
registers the request. Each of those is already somewhere in the
database and none of them is visible on the lead, so the question
"who do I talk to about this" is answered by asking in a group chat.

PEOPLE ARE DATA

Not one employee name or code appears in this module. Every member is
resolved through something a person maintains:

    the owner and the second owner   the lead's own columns
    the vertical head and deputy     `vertical_heads`, from the
                                     vertical master
    the account's owners             the account record
    whoever holds a task             `task_instances`
    the RFQ coordinator              an admin setting

So a leaver stops appearing because somebody changed a record, not
because somebody edited this file and deployed it.

ONE ANSWER, NOT A SECOND OPINION

Access already computes most of this: `app.access.scope` lets a
vertical head see their vertical's leads and lets a task holder open
a lead they were given. This module resolves the same relationships
through the same services, so the team shown on a lead and the people
who can open it cannot drift apart. Where the two would differ it is
reported rather than hidden — a member who cannot actually open the
lead is listed with `can_open: False`, because that is a real
misconfiguration somebody should see rather than a row to suppress.

MEMBERSHIP IS NOT PERMISSION

Being listed here grants nothing. The team is a directory: it answers
who to ask. Every gate in the CRM still asks `access`/`scope`, and
nothing in this module is imported by one.
"""
from __future__ import annotations

#: Role keys, in the order they are shown. The order is the order a
#: person would try them: the owner first, the people who cover for
#: the owner next, the relationship after that, then the people doing
#: this week's work, then the desks.
OWNER = 'owner'
SECOND = 'second_owner'
VERTICAL_HEAD = 'vertical_head'
VERTICAL_DEPUTY = 'vertical_deputy'
ACCOUNT_OWNER = 'account_owner'
ACCOUNT_SECOND = 'account_second_owner'
ACCOUNT_BACKUP = 'account_backup'
TASK_HOLDER = 'task_holder'
RFQ_DESK = 'rfq_coordinator'

ROLE_ORDER = (OWNER, SECOND, VERTICAL_HEAD, VERTICAL_DEPUTY,
              ACCOUNT_OWNER, ACCOUNT_SECOND, ACCOUNT_BACKUP,
              TASK_HOLDER, RFQ_DESK)

ROLE_LABELS = {
    OWNER: 'Owner',
    SECOND: 'Second owner',
    VERTICAL_HEAD: 'Vertical head',
    VERTICAL_DEPUTY: 'Vertical deputy',
    ACCOUNT_OWNER: 'Account owner',
    ACCOUNT_SECOND: 'Account second owner',
    ACCOUNT_BACKUP: 'Account backup',
    TASK_HOLDER: 'Holds a task',
    RFQ_DESK: 'RFQ desk',
}

#: Why each role is on the team, in words a salesperson would use.
ROLE_WHY = {
    OWNER: 'responsible for this lead',
    SECOND: 'covers the owner',
    VERTICAL_HEAD: 'answers for this vertical\'s number',
    VERTICAL_DEPUTY: 'stands in for the head',
    ACCOUNT_OWNER: 'owns the relationship with this customer',
    ACCOUNT_SECOND: 'second owner on the customer',
    ACCOUNT_BACKUP: 'backup on the customer',
    TASK_HOLDER: 'has work open on this lead',
    RFQ_DESK: 'registers the RFQ',
}

#: Roles that make somebody accountable rather than merely involved.
#: The screen leads with these and a lead with none of them is
#: reported as unowned, which is the state half the book is in.
ACCOUNTABLE = (OWNER, SECOND)


def _code(value):
    return (str(value or '').strip().upper()) or ''


def _employees(codes):
    """{code: Employee} for the codes we need, in one query."""
    from app import Employee

    wanted = sorted({_code(c) for c in codes if _code(c)})
    if not wanted:
        return {}
    try:
        rows = Employee.query.filter(Employee.emp_code.in_(wanted)).all()
    except Exception:                                     # noqa: BLE001
        return {}
    return {_code(e.emp_code): e for e in rows}


# ── where each role comes from ───────────────────────────────────────
def _from_lead(lead):
    """The two roles the lead itself names."""
    out = []
    owner = _code(getattr(lead, 'assigned_to', None))
    if owner:
        out.append((OWNER, owner, 'named on the lead'))
    second = _code(getattr(lead, 'secondary_owner', None))
    if second and second != owner:
        out.append((SECOND, second, 'named on the lead'))
    return out


def _from_vertical(lead):
    """The head and deputy of the lead's vertical.

    Through `vertical_heads`, which reads the vertical master and
    falls back the way the rest of the CRM does — so the team names
    whoever the escalation would actually reach, not a different
    person computed here.
    """
    try:
        from app.services import vertical_heads
        codes = vertical_heads.heads_for_vertical(
            getattr(lead, 'procam_vertical', None))
    except Exception:                                     # noqa: BLE001
        return []
    out = []
    for position, code in enumerate(codes or []):
        role = VERTICAL_HEAD if position == 0 else VERTICAL_DEPUTY
        out.append((role, _code(code), 'from the vertical master'))
    return out


def _from_account(lead):
    """The account's own owners, where the lead has an account."""
    from app import Company

    account_id = getattr(lead, 'company_id', None)
    if not account_id:
        return []
    try:
        account = Company.query.get(account_id)
    except Exception:                                     # noqa: BLE001
        account = None
    if account is None:
        return []
    pairs = ((ACCOUNT_OWNER, getattr(account, 'pic_emp_code', None)),
             (ACCOUNT_SECOND,
              getattr(account, 'secondary_pic_emp_code', None)),
             (ACCOUNT_BACKUP, getattr(account, 'backup_pic_emp_code', None)))
    return [(role, _code(code), f'from the account {account.name}')
            for role, code in pairs if _code(code)]


def _from_tasks(lead):
    """Whoever holds a task on this lead.

    Open tasks only. A closed task means somebody *did* work on this
    lead, which belongs in the history and not in the list of people
    to ask today — `access.scope` counts closed ones too, on purpose,
    because being able to open a lead you once worked is different
    from being on its team now.
    """
    from app.models.task_engine import TaskInstance, TaskInstanceStatus

    try:
        rows = (TaskInstance.query
                .filter(TaskInstance.entity_type == 'Lead',
                        TaskInstance.entity_id == lead.id,
                        TaskInstance.status.in_(TaskInstanceStatus.OPEN))
                .all())
    except Exception:                                     # noqa: BLE001
        return []
    out = []
    seen = set()
    for row in rows:
        code = _code(getattr(row, 'owner_user_id', None))
        if not code or code in seen:
            continue
        seen.add(code)
        out.append((TASK_HOLDER, code,
                    f'has {row.task_key or "a task"} open'))
    return out


def _from_desks(lead):
    """Company-wide desks that touch every lead of this shape.

    One setting, read by name. The code it holds is data: a director
    changes it on the Admin screen and the team changes with it.
    """
    try:
        from app.services import admin_settings
        code = _code(admin_settings.get('rfq_coordinator'))
    except Exception:                                     # noqa: BLE001
        return []
    if not code:
        return []
    return [(RFQ_DESK, code, 'the RFQ coordinator setting')]


SOURCES = (_from_lead, _from_vertical, _from_account, _from_tasks,
           _from_desks)


# ── the team ─────────────────────────────────────────────────────────
def for_lead(lead, *, check_access=False):
    """Everybody on this lead, strongest role first.

    One row per person. Somebody who is both the owner and the
    vertical head appears once, under Owner, with the other role
    listed beside it — two rows for one person reads as two people,
    and the first thing anybody does with this list is count it.
    """
    if lead is None:
        return []

    found = []
    for source in SOURCES:
        try:
            found.extend(source(lead))
        except Exception:                                 # noqa: BLE001
            # A source that cannot answer leaves its role out. It
            # must not take the rest of the team with it: a lead
            # screen that shows nobody because one table is missing
            # is worse than one that shows the owner.
            continue

    people = _employees(code for _role, code, _why in found)

    by_code = {}
    for role, code, why in found:
        if not code:
            continue
        entry = by_code.get(code)
        if entry is None:
            emp = people.get(code)
            by_code[code] = {
                'emp_code': code,
                # Unknown rather than blank: a code with no employee
                # row is a real problem and hiding it as an empty
                # name makes it look like a display bug.
                'name': (getattr(emp, 'name', None) or '')
                        or f'{code} — no employee record',
                'email': (getattr(emp, 'email', None) or ''),
                'vertical': (getattr(emp, 'vertical', None) or ''),
                'is_active': bool(getattr(emp, 'is_active', False))
                if emp is not None else False,
                'known': emp is not None,
                'roles': [],
            }
            entry = by_code[code]
        if role not in [r['key'] for r in entry['roles']]:
            entry['roles'].append({
                'key': role, 'label': ROLE_LABELS[role],
                'why': ROLE_WHY[role], 'detail': why,
            })

    def _rank(entry):
        best = min(ROLE_ORDER.index(r['key']) for r in entry['roles'])
        return (best, entry['emp_code'])

    team = sorted(by_code.values(), key=_rank)
    for entry in team:
        entry['roles'].sort(key=lambda r: ROLE_ORDER.index(r['key']))
        entry['role'] = entry['roles'][0]['label']
        entry['role_key'] = entry['roles'][0]['key']
        entry['accountable'] = any(r['key'] in ACCOUNTABLE
                                   for r in entry['roles'])
    if check_access:
        _mark_access(lead, team)
    return team


def _mark_access(lead, team):
    """Whether each member can actually open the lead they are on.

    Asked of `access.scope`, the same chokepoint every board and
    report goes through, so this cannot invent a second answer. A
    False here is a misconfiguration worth seeing, not a row to drop:
    a vertical head who cannot open their own vertical's lead is the
    sort of thing that is discovered when they are asked to approve
    something at six o'clock.
    """
    from app import Lead
    from app.access import scope

    for entry in team:
        reachable = None
        try:
            sc = scope.for_employee(entry['emp_code'])
            reachable = bool(
                scope.leads(Lead.query.filter(Lead.id == lead.id), sc)
                .first() is not None)
        except Exception:                                 # noqa: BLE001
            reachable = None
        entry['can_open'] = reachable


def summary(lead, *, check_access=False):
    """The team plus the two things a screen says about it."""
    team = for_lead(lead, check_access=check_access)
    accountable = [m for m in team if m['accountable']]
    return {
        'team': team,
        'size': len(team),
        'accountable': len(accountable),
        #: The state half the book is in, said plainly rather than
        #: shown as an empty row.
        'unowned': not accountable,
        'inactive': [m['emp_code'] for m in team if not m['is_active']],
        'unknown_codes': [m['emp_code'] for m in team if not m['known']],
    }
