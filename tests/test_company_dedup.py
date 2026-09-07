"""Company de-duplication — §51, §65, §88.

Merging is the one migration that can silently destroy commercial history,
so the properties that matter are: nothing is deleted, references follow
the survivor, the survivor is chosen on merit, and a merge can be undone.
"""
import os
import sys
import tempfile

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'test')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'DedupTestOnly12345')
os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(
    tempfile.mkdtemp(), 'dedup.db')

import importlib                                          # noqa: E402
_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app, db = _main.app, _main.db
Company, Opportunity = _main.Company, _main.Opportunity

sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
from app.models.data_mapping import CompanyMergeLog          # noqa: E402
dedup = importlib.import_module('2026_09_11_company_dedup')


@pytest.fixture()
def dupes():
    with flask_app.app_context():
        db.create_all()
        # Merge logs persist across tests otherwise, so a later test picks
        # up an earlier one's row and reverts the wrong merge.
        CompanyMergeLog.query.delete()
        Opportunity.query.delete()
        # Relationship tags reference companies; other test modules create
        # them, and deleting companies underneath leaves orphans that
        # break the next merge.
        try:
            from presales.models import AccountRelationshipTag
            AccountRelationshipTag.query.delete()
        except Exception:
            pass
        Company.query.delete()
        db.session.commit()

        # Same company, three spellings.  The middle one is richest.
        thin = Company(name='Adani', is_active=True)
        rich = Company(name='Adani Ltd.', industry='Power',
                       website='adani.com', city='Ahmedabad', is_active=True)
        other = Company(name='Adani Private Limited', country='India',
                        is_active=True)
        unrelated = Company(name='Siemens Energy', is_active=True)
        db.session.add_all([thin, rich, other, unrelated])
        db.session.flush()

        for n in range(3):
            db.session.add(Opportunity(opp_number=f'R-{n}',
                                       company_id=rich.id, stage='Won',
                                       value_inr=100))
        db.session.add(Opportunity(opp_number='T-1', company_id=thin.id,
                                   stage='Won', value_inr=50))
        db.session.commit()
        return {'thin': thin.id, 'rich': rich.id, 'other': other.id,
                'unrelated': unrelated.id}


def test_normalisation_groups_legal_suffixes(dupes):
    assert dedup.norm('Adani') == dedup.norm('Adani Ltd.')
    assert dedup.norm('Adani') == dedup.norm('Adani Private Limited')
    assert dedup.norm('Tata Steel') != dedup.norm('Tata Motors')


def test_normalisation_refuses_to_guess_abbreviations():
    """BHEL and Bharat Heavy Electricals are probably the same company.

    'Probably' is exactly what §65 says not to act on — these must reach a
    human, not be merged automatically.
    """
    assert dedup.norm('BHEL') != dedup.norm('Bharat Heavy Electricals')


def test_preview_changes_nothing(dupes):
    with flask_app.app_context():
        before = Company.query.count()
        groups = dedup.find_groups()
        assert 'adani' in groups
        for rows in groups.values():
            ordered = sorted(rows, key=dedup._richness, reverse=True)
            for loser in ordered[1:]:
                dedup.merge(ordered[0], loser, dry=True)
        db.session.rollback()
        assert Company.query.count() == before
        assert Company.query.filter_by(is_active=False).count() == 0


def test_the_richest_record_survives(dupes):
    with flask_app.app_context():
        rows = dedup.find_groups()['adani']
        winner = sorted(rows, key=dedup._richness, reverse=True)[0]
        assert winner.id == dupes['rich'], \
            'the record with the most opportunities should survive'


def test_merge_moves_references_and_keeps_history(dupes):
    with flask_app.app_context():
        rows = sorted(dedup.find_groups()['adani'],
                      key=dedup._richness, reverse=True)
        keep, losers = rows[0], rows[1:]
        for loser in losers:
            dedup.merge(keep, loser, dry=False)
        db.session.commit()

        # every opportunity now points at the survivor
        assert Opportunity.query.filter_by(company_id=keep.id).count() == 4
        for loser in losers:
            assert Opportunity.query.filter_by(
                company_id=loser.id).count() == 0

        # nothing was deleted
        assert Company.query.count() == 4
        for loser in losers:
            row = Company.query.get(loser.id)
            assert row is not None, 'a duplicate was destroyed'
            assert row.is_active is False
            assert 'merged into' in row.name

        # the unrelated company is untouched
        assert Company.query.get(dupes['unrelated']).is_active is True


def test_merge_fills_blanks_on_the_survivor(dupes):
    with flask_app.app_context():
        rows = sorted(dedup.find_groups()['adani'],
                      key=dedup._richness, reverse=True)
        keep = rows[0]
        assert not keep.country
        for loser in rows[1:]:
            dedup.merge(keep, loser, dry=False)
        db.session.commit()
        # 'Adani Private Limited' carried country=India
        assert Company.query.get(keep.id).country == 'India'


def test_every_merge_is_logged_and_revertible(dupes):
    with flask_app.app_context():
        rows = sorted(dedup.find_groups()['adani'],
                      key=dedup._richness, reverse=True)
        for loser in rows[1:]:
            dedup.merge(rows[0], loser, dry=False)
        db.session.commit()

        logs = CompanyMergeLog.query.all()
        assert len(logs) == 2
        assert all(l.kept_id == rows[0].id for l in logs)
        assert any(l.moved for l in logs), 'the log records what moved'

        target = logs[0]
        dedup.revert(target.id)

        restored = Company.query.get(target.merged_id)
        assert restored.is_active is True
        assert 'merged into' not in restored.name
        assert CompanyMergeLog.query.get(target.id).reverted_at is not None


def test_survivor_name_is_tidied(dupes):
    """The survivor is chosen on data, not on how neatly it was typed.

    A real row in the live database is "LLOYDS METALS & ENERGY LTD," —
    that trailing comma would otherwise appear in every report.
    """
    with flask_app.app_context():
        messy = Company(name='Lloyds Metals & Energy Ltd,', is_active=True)
        clean = Company(name='Lloyds Metals & Energy Ltd', is_active=True)
        db.session.add_all([messy, clean])
        db.session.flush()
        db.session.add(Opportunity(opp_number='L-1', company_id=messy.id,
                                   stage='Won', value_inr=10))
        db.session.commit()

        rows = sorted(dedup.find_groups()['lloyds metals energy'],
                      key=dedup._richness, reverse=True)
        keep = rows[0]
        assert keep.id == messy.id, 'the richer record should win'
        for loser in rows[1:]:
            dedup.merge(keep, loser, dry=False)
        db.session.commit()
        assert Company.query.get(keep.id).name == 'Lloyds Metals & Energy Ltd'
