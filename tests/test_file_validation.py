"""Uploads must be what their name says, and safe to open."""
import io
import os
import sys
import tempfile
import zipfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'test')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'UploadTestOnly12345')
os.environ.setdefault('DATABASE_URL', 'sqlite:///' + os.path.join(
    tempfile.mkdtemp(), 'uploads.db'))

from app.utils.file_validation import (UploadRejected, validate,     # noqa
                                       validate_bytes, MAX_ZIP_RATIO)

PNG = b'\x89PNG\r\n\x1a\n' + b'\x00' * 64
PDF = b'%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<<>>\nendobj\n'
EXE = b'MZ\x90\x00\x03\x00\x00\x00' + b'\x00' * 64


def _xlsx(extra=None):
    import openpyxl
    wb = openpyxl.Workbook()
    wb.active['A1'] = 'Company'
    buf = io.BytesIO()
    wb.save(buf)
    if not extra:
        return buf.getvalue()
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(buf.getvalue())) as src, \
            zipfile.ZipFile(out, 'w') as dst:
        for item in src.infolist():
            dst.writestr(item, src.read(item.filename))
        for name, data in extra.items():
            dst.writestr(name, data)
    return out.getvalue()


def _zip(entries, compression=zipfile.ZIP_DEFLATED):
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w', compression) as z:
        for name, data in entries.items():
            z.writestr(name, data)
    return out.getvalue()


@pytest.mark.parametrize('data,ext', [(PNG, '.png'), (PDF, '.pdf'),
                                      (b'name,email\nA,a@x.com\n', '.csv')])
def test_genuine_files_pass(data, ext):
    validate_bytes(data, ext)


def test_a_real_workbook_passes():
    validate_bytes(_xlsx(), '.xlsx')


@pytest.mark.parametrize('data,ext', [(EXE, '.pdf'), (PDF, '.png'),
                                      (PNG, '.xlsx'), (EXE, '.jpg')])
def test_a_renamed_file_is_refused(data, ext):
    with pytest.raises(UploadRejected, match='do not match'):
        validate_bytes(data, ext)


def test_an_empty_file_is_refused():
    with pytest.raises(UploadRejected, match='empty'):
        validate_bytes(b'', '.pdf')


def test_a_zip_bomb_is_refused_without_being_extracted():
    bomb = _zip({'[Content_Types].xml': '<x/>',
                 'xl/sheet.xml': b'\x00' * (40 * 1024 * 1024)})
    assert len(bomb) < 1024 * 1024        # tiny on disk
    with pytest.raises(UploadRejected, match='crash servers|expands'):
        validate_bytes(bomb, '.xlsx')


def test_a_workbook_with_macros_is_refused():
    with pytest.raises(UploadRejected, match='macros'):
        validate_bytes(_xlsx({'xl/vbaProject.bin': b'\x01' * 100}), '.xlsx')


def test_an_archive_with_unsafe_paths_is_refused():
    with pytest.raises(UploadRejected, match='unsafe'):
        validate_bytes(_zip({'../../etc/cron.d/x': 'boom'}), '.zip')


def test_a_zip_pretending_to_be_office_is_refused():
    with pytest.raises(UploadRejected, match='not a valid Office'):
        validate_bytes(_zip({'readme.txt': 'hello'}), '.docx')


def test_binary_in_a_csv_is_refused():
    with pytest.raises(UploadRejected):
        validate_bytes(b'a,b\n\x00\x00binary', '.csv')


def test_the_stream_is_rewound():
    s = io.BytesIO(PDF)
    validate(s, '.pdf')
    assert s.tell() == 0


def test_the_lead_import_refuses_a_disguised_file():
    from app import app as flask_app
    flask_app.config['WTF_CSRF_ENABLED'] = False
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s.update(emp_code='UPLTEST', name='x', role='user', vertical='All')
    r = c.post('/api/leads/import/preview',
               data={'file': (io.BytesIO(EXE), 'leads.xlsx')},
               content_type='multipart/form-data')
    assert r.status_code == 400
    assert 'do not match' in r.get_json()['error']


def test_save_upload_refuses_a_disguised_file():
    from werkzeug.datastructures import FileStorage
    from werkzeug.exceptions import BadRequest
    from app import app as flask_app
    from app.utils.uploads import save_upload
    with flask_app.test_request_context():
        flask_app.config['UPLOAD_ROOT'] = tempfile.mkdtemp()
        fs = FileStorage(stream=io.BytesIO(EXE), filename='drawing.pdf')
        with pytest.raises(BadRequest):
            save_upload(fs, 'rfq/1')
        ok = FileStorage(stream=io.BytesIO(PDF), filename='drawing.pdf')
        path, _name = save_upload(ok, 'rfq/1')
        assert open(path, 'rb').read() == PDF


def test_every_export_writes_formula_safe_cells():
    """Static guard: the four export paths pass cells through the guard."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for path, needle in (
            ('presales/routes_dashboard.py', 'safe_row(r)'),
            ('app/reports_v2/routes.py', 'safe_row([row.get'),
            ('app/excel_io/service.py', 'safe_row([r['),
            ('app/bulk_admin/routes.py', 'safe_row(['),
            ('scripts/data_quality_report.py', "\"'\" + v")):
        assert needle in open(os.path.join(root, path)).read(), path


def test_the_data_quality_csv_neutralises_formulas(tmp_path):
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), 'scripts'))
    import data_quality_report as dq
    path = tmp_path / 'x.csv'
    dq._write(str(path), ['name', 'value'],
              [{'name': '=HYPERLINK("http://x")', 'value': '-1500'}])
    text = path.read_text()
    assert '\'=HYPERLINK' in text and ',-1500' in text
