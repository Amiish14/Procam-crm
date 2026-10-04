"""Adopting the files the ingest saved and never recorded.

On production this is 2,847 files and 905 MB, so the two things that
matter are that it claims what it should and refuses what it should:
it must not invent a lead, and it must not offer the same document
twice in the drawer.
"""
import importlib.util
import os
import sys
import tempfile

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['URL_PREFIX'] = ''
os.environ.setdefault('SECRET_KEY', 'adopt-test-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'AdoptTest12345')
os.environ.setdefault(
    'DATABASE_URL',
    'sqlite:///' + os.path.join(tempfile.mkdtemp(), 'adopt.db'))

import importlib                                          # noqa: E402
_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app, db = _main.app, _main.db
Lead, LeadAttachment = _main.Lead, _main.LeadAttachment


def _load():
    path = os.path.join(_ROOT, 'scripts', 'adopt_orphan_attachments.py')
    spec = importlib.util.spec_from_file_location('adopt_orphans', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


TAG = 'ADO-'


def _wipe():
    ids = [l.id for l in Lead.query.filter(Lead.company.like(TAG + '%')).all()]
    if ids:
        LeadAttachment.query.filter(LeadAttachment.lead_id.in_(ids)).delete(
            synchronize_session=False)
        Lead.query.filter(Lead.id.in_(ids)).delete(synchronize_session=False)
    db.session.commit()


@pytest.fixture()
def world(tmp_path, monkeypatch):
    from email_ingest import attachments as att_mod
    monkeypatch.setattr(att_mod, 'STORAGE_ROOT', str(tmp_path))
    with flask_app.app_context():
        db.create_all()
        _wipe()
        lead = Lead(company=TAG + 'Customer', stage='New Opportunity',
                    source='email')
        db.session.add(lead)
        db.session.commit()
        yield lead, tmp_path
        _wipe()


def _put(root, lead_id, name, data=b'x' * 4096):
    folder = os.path.join(str(root), str(lead_id))
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, name)
    with open(path, 'wb') as fh:
        fh.write(data)
    return path


def _scan(root, exclude=frozenset(), min_bytes=0):
    return _load()._scan(str(root), set(exclude), min_bytes)


def test_a_file_with_no_row_is_adoptable(world):
    lead, root = world
    _put(root, lead.id, 'BOQ.pdf')
    adoptable, _skipped = _scan(root)
    assert [n for _l, _p, n, _s in adoptable] == ['BOQ.pdf']


def test_a_file_that_already_has_a_row_is_left_alone(world):
    lead, root = world
    path = _put(root, lead.id, 'BOQ.pdf')
    db.session.add(LeadAttachment(lead_id=lead.id, filename='BOQ.pdf',
                                  storage_path=path, size_bytes=4096,
                                  source='email'))
    db.session.commit()
    adoptable, skipped = _scan(root)
    assert adoptable == []
    assert skipped['already recorded'] == 1


def test_a_numbered_copy_of_a_recorded_file_is_not_offered_twice(world):
    """BOQ-1.pdf beside a recorded BOQ.pdf of the same size is the same
    drawing under another name — adopting it puts the same document in
    the drawer twice."""
    lead, root = world
    path = _put(root, lead.id, 'BOQ.pdf')
    _put(root, lead.id, 'BOQ-1.pdf')
    db.session.add(LeadAttachment(lead_id=lead.id, filename='BOQ.pdf',
                                  storage_path=path, size_bytes=4096,
                                  source='email'))
    db.session.commit()
    adoptable, skipped = _scan(root)
    assert adoptable == []
    assert skipped['a numbered copy of a recorded file'] == 1


def test_a_numbered_file_of_a_different_size_is_a_different_document(world):
    lead, root = world
    path = _put(root, lead.id, 'BOQ.pdf')
    _put(root, lead.id, 'BOQ-1.pdf', data=b'y' * 9000)
    db.session.add(LeadAttachment(lead_id=lead.id, filename='BOQ.pdf',
                                  storage_path=path, size_bytes=4096,
                                  source='email'))
    db.session.commit()
    adoptable, _skipped = _scan(root)
    assert [n for _l, _p, n, _s in adoptable] == ['BOQ-1.pdf']


def test_a_directory_whose_lead_is_gone_is_reported_not_adopted(world):
    _lead, root = world
    _put(root, 99999123, 'Orphan.pdf')
    adoptable, skipped = _scan(root)
    assert adoptable == []
    assert skipped['the lead no longer exists'] == 1


def test_the_originals_directory_is_left_to_its_own_table(world):
    lead, root = world
    folder = os.path.join(str(root), str(lead.id), 'original')
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, 'a.eml'), 'wb') as fh:
        fh.write(b'Subject: x')
    adoptable, _skipped = _scan(root)
    assert adoptable == []


def test_extensions_and_a_size_floor_can_be_excluded(world):
    lead, root = world
    _put(root, lead.id, 'signature.gif', data=b'g' * 100)
    _put(root, lead.id, 'BOQ.pdf')
    adoptable, _s = _scan(root, exclude={'.gif'})
    assert [n for _l, _p, n, _s2 in adoptable] == ['BOQ.pdf']
    adoptable, _s = _scan(root, min_bytes=2048)
    assert [n for _l, _p, n, _s2 in adoptable] == ['BOQ.pdf']
