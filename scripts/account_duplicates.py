"""
Accounts that are the same customer twice.

FINISH-TRACKER-CRM §0.

Why this blocks everything else
    `scripts/link_unplaced_leads.py --unlinked` can place 17 of 293
    unlinked leads. 218 of them wait on CHOOSE, and CHOOSE is almost
    never an ambiguous lead — it is an ambiguous account master.
    Praj has three accounts, Hacklin four, Meiden four, L&T three,
    Tmilltd three. The matcher is explicit about it: `_by_name`
    returns nothing at all when two active accounts normalise alike,
    and logs "matches 2 accounts — leaving it to a person". Until a
    person decides, every lead from that customer stays unplaced.

How a cluster is found
    Three kinds of evidence, any one of which joins two accounts:
    the same GSTIN, a shared email domain, or names that normalise
    to the same string under the CRM's own `company_match.norm`.
    Clusters are transitive: A shares a domain with B, B normalises
    like C, so all three are one customer.

    Placeholder accounts are left out. "Unknown" and "Email" are not
    a customer written twice, and one of them claims our own domain,
    which would drag every account into one cluster.

Read-only by default:

    .venv/bin/python scripts/account_duplicates.py
    .venv/bin/python scripts/account_duplicates.py --account 200

Merging needs the survivor, the losers, an actor and --apply, all
together. There is no "merge everything it suggested": the suggested
survivor is evidence, not a decision, and a merge is the hardest
thing here to see afterwards.

    .venv/bin/python scripts/account_duplicates.py \\
        --survivor 205 --merge 870 1059 --actor DIR12010 --apply

What a merge does, and does not
    Everything pointing at the loser is re-pointed at the survivor —
    leads, contacts, vertical owners, activities, quotes, RFQs,
    tags, every table with a foreign key to an account — and the
    loser's name, aliases and email domains are added to the
    survivor's aliases, so the old spelling still finds the right
    customer.

    The loser row is kept and marked inactive. Nothing is deleted,
    ever. Inactive is what makes the matcher work again: `_by_name`
    and `_similar_names` both look at active accounts only, so the
    name that matched two rows now matches one.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))

#: Why two accounts are in one cluster, in the order they are tried.
BY_GSTIN = 'same GSTIN'
BY_DOMAIN = 'shared email domain'
BY_NAME = 'names normalise alike'

MERGE_REASON = 'Merged into the surviving account'


def _norm(name):
    from app.services import company_match
    return company_match.norm(name or '')


def _domains(account):
    from app.services import account_match
    out = []
    for raw in (getattr(account, 'email_domains', None) or []):
        domain = str(raw).strip().lower().lstrip('@')
        if domain and not account_match.is_free_mail(domain):
            out.append(domain)
    return out


def _gstin(account):
    return (getattr(account, 'gstin', '') or '').strip().upper()


def _counts(account_id):
    """(open leads, won leads, all leads) for one account."""
    from app import Lead, db
    from app.services import sales_rules
    rows = (db.session.query(Lead.stage)
            .filter(Lead.company_id == account_id).all())
    stages = [r[0] or '' for r in rows]
    opened = sum(1 for s in stages if sales_rules.is_open(s))
    won = sum(1 for s in stages if s == 'Won')
    return opened, won, len(stages)


def _owners(account_id):
    """{vertical: emp_code} from AccountVerticalOwner."""
    from app.models.assignment import AccountVerticalOwner
    try:
        rows = (AccountVerticalOwner.query
                .filter_by(account_id=account_id).all())
    except Exception:
        from app import db
        db.session.rollback()
        return {}
    return {(r.vertical or ''): (r.emp_code or '') for r in rows}


def _read(account):
    opened, won, total = _counts(account.id)
    return {
        'account': account,
        'id': account.id,
        'name': account.name or '',
        'norm': _norm(account.name),
        'gstin': _gstin(account),
        'domains': _domains(account),
        'owner': (getattr(account, 'pic_emp_code', '') or '').strip(),
        'owners': _owners(account.id),
        'vertical': getattr(account, 'vertical', '') or '',
        'open_leads': opened,
        'won_leads': won,
        'leads': total,
        'active': bool(getattr(account, 'is_active', True)),
    }


def _survivor(rows):
    """The account the others should fold into, and why.

    Weight of history first: the row that holds the most leads is
    the one whose absence would be noticed. Wins break a tie,
    because a won deal is the hardest thing to re-create. Then the
    identifiers, then the lower id — the original row.
    """
    def rank(row):
        return (-row['leads'], -row['won_leads'],
                0 if row['gstin'] else 1,
                0 if row['domains'] else 1,
                row['id'])

    best = sorted(rows, key=rank)[0]
    why = []
    if best['leads']:
        why.append(f'{best["leads"]} lead(s)')
    if best['won_leads']:
        why.append(f'{best["won_leads"]} won')
    if best['gstin']:
        why.append('has a GSTIN')
    if best['domains']:
        why.append('has an email domain')
    if not why:
        why.append('the original row')
    return best, ', '.join(why)


def clusters(account_ids=()):
    """Accounts grouped into the customer they are, duplicates only."""
    from app import Company
    from app.services import account_match

    accounts = Company.query.filter(
        Company.is_active.is_(True)).order_by(Company.id.asc()).all()
    accounts = [a for a in accounts
                if not account_match.not_a_customer(a)]
    rows = {a.id: _read(a) for a in accounts}

    # Union-find over the three kinds of evidence.
    parent = {i: i for i in rows}

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    why = {}

    def join(a, b, reason):
        union(a, b)
        key = (min(a, b), max(a, b))
        why.setdefault(key, reason)

    by_key = {}
    for row in rows.values():
        keys = []
        if row['gstin']:
            keys.append((BY_GSTIN, row['gstin']))
        for domain in row['domains']:
            keys.append((BY_DOMAIN, domain))
        if len(row['norm']) >= 3:
            keys.append((BY_NAME, row['norm']))
        for key in keys:
            first = by_key.setdefault(key, row['id'])
            if first != row['id']:
                join(first, row['id'], key[0])

    grouped = {}
    for account_id in rows:
        grouped.setdefault(find(account_id), []).append(account_id)

    out = []
    for ids in grouped.values():
        if len(ids) < 2:
            continue
        if account_ids and not any(int(x) in ids for x in account_ids):
            continue
        members = [rows[i] for i in sorted(ids)]
        reasons = sorted({r for pair, r in why.items()
                          if pair[0] in ids and pair[1] in ids})
        keeper, because = _survivor(members)
        out.append({
            'accounts': members,
            'reasons': reasons or [BY_NAME],
            'survivor': keeper,
            'survivor_why': because,
            'losers': [m for m in members if m['id'] != keeper['id']],
            'leads': sum(m['leads'] for m in members),
            'open_leads': sum(m['open_leads'] for m in members),
        })
    out.sort(key=lambda c: (-c['open_leads'], -c['leads'],
                            c['survivor']['name'].lower()))
    return out


def counts(found):
    """(clusters, accounts, leads, open leads) across the whole lot."""
    return (len(found),
            sum(len(c['accounts']) for c in found),
            sum(c['leads'] for c in found),
            sum(c['open_leads'] for c in found))


def report(found):
    if not found:
        return 'No duplicate accounts. Every customer is one row.'

    n_clusters, n_accounts, n_leads, n_open = counts(found)
    out = [f'{n_clusters} duplicate cluster(s): {n_accounts} accounts '
           f'that are {n_clusters} customers, holding {n_leads} '
           f'lead(s) ({n_open} open).', '']
    for cluster in found:
        keeper = cluster['survivor']
        out.append(f'── {keeper["name"]}  ·  '
                   f'{len(cluster["accounts"])} accounts  ·  '
                   f'{cluster["leads"]} lead(s)')
        out.append(f'      joined by: {", ".join(cluster["reasons"])}')
        for row in cluster['accounts']:
            mark = '  KEEP' if row['id'] == keeper['id'] else '  fold'
            out.append(f'   {mark}  #{row["id"]:<5} {row["name"]}')
            bits = [f'{row["leads"]} lead(s)',
                    f'{row["open_leads"]} open',
                    f'{row["won_leads"]} won']
            if row['gstin']:
                bits.append('GSTIN ' + row['gstin'])
            if row['domains']:
                bits.append('@' + ', @'.join(row['domains']))
            out.append('           ' + '  ·  '.join(bits))
            if row['owners']:
                owners = '; '.join(f'{v or "(none)"}: {c}'
                                   for v, c in sorted(row['owners'].items()))
                out.append(f'           owners — {owners}')
            elif row['owner']:
                out.append(f'           owner — {row["owner"]} '
                           f'(account PIC, no vertical rows)')
            else:
                out.append('           owner — nobody')
        out.append(f'      suggested survivor: #{keeper["id"]} '
                   f'({cluster["survivor_why"]})')
        losers = ' '.join(str(r['id']) for r in cluster['losers'])
        out.append(f'      merge:  --survivor {keeper["id"]} '
                   f'--merge {losers} --actor <EMP> --apply')
        out.append('')

    out.append(f'By cluster size:')
    sizes = {}
    for cluster in found:
        sizes[len(cluster['accounts'])] = sizes.get(
            len(cluster['accounts']), 0) + 1
    for size in sorted(sizes):
        out.append(f'  {size} accounts      {sizes[size]:>4} cluster(s)')
    out.append('')
    out.append('Nothing was changed. The suggested survivor is '
               'evidence, not a decision — check it before merging.')
    return '\n'.join(out)


# ── the merge ────────────────────────────────────────────────────────
def _account_fks():
    """[(table, column)] for every foreign key to an account.

    Walked from the metadata rather than listed by hand. There are
    30 of them — leads, contacts, activities, quotes, RFQs, tags,
    handovers, competitors, projects — and a merge that re-points
    three leaves the rest pointing at a row marked inactive.
    """
    from app import db
    out = []
    for table in db.metadata.sorted_tables:
        for column in table.columns:
            for fk in column.foreign_keys:
                if fk.column.table.name == 'companies':
                    out.append((table, column))
    return out


def _unique_keys(table, column):
    """Column groups that must stay unique and include `column`."""
    keys = []
    for constraint in table.constraints:
        cols = [c.name for c in getattr(constraint, 'columns', [])]
        if (constraint.__class__.__name__ == 'UniqueConstraint'
                and column.name in cols and len(cols) > 1):
            keys.append([c for c in cols if c != column.name])
    for index in getattr(table, 'indexes', ()):
        if not index.unique:
            continue
        cols = [c.name for c in index.columns]
        if column.name in cols and len(cols) > 1:
            keys.append([c for c in cols if c != column.name])
    return keys


def _repoint(table, column, loser_id, survivor_id):
    """Move this table's rows. Returns (moved, left behind).

    A unique constraint on (account, something) can make a move
    impossible: the survivor may already own the Heavy Transport
    owner row, or the same tag. Those rows stay on the loser and are
    counted, because the alternative is deleting one of them and
    nothing here deletes.
    """
    from sqlalchemy import text

    from app import db

    params = {'survivor': survivor_id, 'loser': loser_id}
    move = (f'UPDATE {table.name} SET {column.name} = :survivor '
            f'WHERE {column.name} = :loser')
    keys = _unique_keys(table, column)
    if not keys:
        return (db.session.execute(text(move), params).rowcount or 0), 0

    # SQLite's IS compares NULLs equal, which is what a unique
    # constraint does too.
    clauses = []
    for other in keys:
        same = ' AND '.join(f'twin.{c} IS {table.name}.{c}'
                            for c in other)
        clauses.append(
            f'EXISTS (SELECT 1 FROM {table.name} twin WHERE '
            f'twin.{column.name} = :survivor AND {same})')
    clash = ' OR '.join(clauses)

    stayed = db.session.execute(
        text(f'SELECT COUNT(*) FROM {table.name} WHERE '
             f'{column.name} = :loser AND ({clash})'), params).scalar()
    moved = db.session.execute(
        text(f'{move} AND NOT ({clash})'), params).rowcount
    return (moved or 0), (stayed or 0)


def _absorb_names(survivor, loser):
    """The loser's name and spellings become the survivor's aliases."""
    aliases = list(getattr(survivor, 'aliases', None) or [])
    known = {str(a).strip().lower() for a in aliases}
    for name in ([loser.name] + list(getattr(loser, 'aliases', None)
                                     or [])):
        text_name = str(name or '').strip()
        if text_name and text_name.lower() not in known:
            aliases.append(text_name)
            known.add(text_name.lower())
    survivor.aliases = aliases

    domains = list(getattr(survivor, 'email_domains', None) or [])
    seen = {str(d).strip().lower() for d in domains}
    for domain in (getattr(loser, 'email_domains', None) or []):
        one = str(domain or '').strip().lower()
        if one and one not in seen:
            domains.append(one)
            seen.add(one)
    survivor.email_domains = domains
    return aliases, domains


def merge(survivor_id, loser_ids, *, actor):
    """Fold the losers into the survivor. (done, refused)."""
    from app import Company, db
    from app.services import audit

    survivor = db.session.get(Company, int(survivor_id))
    if survivor is None:
        return [], [(survivor_id, 'no such account')]
    if not getattr(survivor, 'is_active', True):
        return [], [(survivor_id, 'the survivor is inactive — it has '
                                  'already been merged into something')]

    done, refused = [], []
    for raw in loser_ids:
        loser = db.session.get(Company, int(raw))
        if loser is None:
            refused.append((raw, 'no such account'))
            continue
        if loser.id == survivor.id:
            refused.append((loser.id, 'that is the survivor'))
            continue
        if not getattr(loser, 'is_active', True):
            refused.append((loser.id, 'already merged (inactive)'))
            continue

        moved, left = {}, {}
        for table, column in _account_fks():
            if table.name == 'companies' and column.name == 'id':
                continue
            count, stayed = _repoint(table, column, loser.id,
                                     survivor.id)
            if count:
                moved[f'{table.name}.{column.name}'] = count
            if stayed:
                left[f'{table.name}.{column.name}'] = stayed

        aliases, domains = _absorb_names(survivor, loser)
        was_name = loser.name
        loser.is_active = False
        # The row stays, readable, saying what happened to it. A
        # name nobody can find is worse than a name marked merged.
        loser.name = f'{was_name} (merged into #{survivor.id})'

        audit.record('account.merged', 'company', loser.id,
                     old={'name': was_name, 'is_active': True},
                     new={'name': loser.name, 'is_active': False,
                          'survivor': survivor.id,
                          'moved': moved, 'left_behind': left},
                     reason=MERGE_REASON, actor=actor)
        audit.record('account.absorbed', 'company', survivor.id,
                     old={'aliases_before': len(aliases) - 1},
                     new={'absorbed': loser.id, 'name': was_name,
                          'aliases': aliases, 'email_domains': domains,
                          'moved': moved},
                     reason=f'Absorbed #{loser.id} {was_name}',
                     actor=actor)
        done.append({'loser': loser.id, 'was': was_name,
                     'survivor': survivor.id, 'moved': moved,
                     'left_behind': left})
    db.session.commit()
    return done, refused


def merge_preview(survivor_id, loser_ids):
    """What a merge would move, counted, writing nothing."""
    from app import Company, db

    survivor = db.session.get(Company, int(survivor_id))
    if survivor is None:
        return f'No account #{survivor_id}. Nothing would change.'
    out = [f'Survivor: #{survivor.id} {survivor.name}', '']
    for raw in loser_ids:
        loser = db.session.get(Company, int(raw))
        if loser is None:
            out.append(f'  #{raw}: no such account')
            continue
        if loser.id == survivor.id:
            out.append(f'  #{loser.id}: that is the survivor')
            continue
        row = _read(loser)
        out.append(f'  #{loser.id} {loser.name}')
        out.append(f'      {row["leads"]} lead(s), '
                   f'{row["open_leads"]} open, {row["won_leads"]} won')
        out.append(f'      would become: "{loser.name} '
                   f'(merged into #{survivor.id})", inactive, kept')
        out.append(f'      "{loser.name}" becomes an alias of '
                   f'#{survivor.id}')
        for table, column in _account_fks():
            from sqlalchemy import text
            count = db.session.execute(
                text(f'SELECT COUNT(*) FROM {table.name} '
                     f'WHERE {column.name} = :loser'),
                {'loser': loser.id}).scalar() or 0
            if count:
                out.append(f'      {table.name}.{column.name}: '
                           f'{count} row(s) move')
    out.append('')
    out.append('Nothing was changed. Add --actor <EMP> --apply to '
               'merge.')
    return '\n'.join(out)


# ── the command line ─────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--account', nargs='*', default=[],
                    help='only clusters containing these accounts')
    ap.add_argument('--survivor', help='the account to keep')
    ap.add_argument('--merge', nargs='*', default=[],
                    help='the accounts to fold into the survivor')
    ap.add_argument('--actor', help='who is doing this (emp code)')
    ap.add_argument('--apply', action='store_true',
                    help='write the merge')
    args = ap.parse_args()

    import app as main_app
    with main_app.app.app_context():
        if args.survivor or args.merge:
            if not args.survivor or not args.merge:
                print('A merge needs both --survivor and --merge. '
                      'Nothing was changed.')
                return 2
            if not args.apply or not args.actor:
                print(merge_preview(args.survivor, args.merge))
                return 0
            done, refused = merge(args.survivor, args.merge,
                                  actor=args.actor)
            for row in done:
                total = sum(row['moved'].values())
                print(f'merged  #{row["loser"]} {row["was"]} -> '
                      f'#{row["survivor"]}  ({total} row(s) moved)')
                for where, count in sorted(row['moved'].items()):
                    print(f'            {where}: {count}')
                for where, count in sorted(row['left_behind'].items()):
                    print(f'            left behind (would clash) '
                          f'{where}: {count}')
            for account_id, why in refused:
                print(f'refused #{account_id}: {why}')
            print(f'\n{len(done)} merged, {len(refused)} refused.')
            return 0

        print(report(clusters(args.account)))
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
