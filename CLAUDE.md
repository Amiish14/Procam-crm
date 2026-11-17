# Procam CRM — working notes for Claude

Flask + Flask-SQLAlchemy. `app.py` is both the application and the
parent package; blueprints are registered from the list near the
bottom of it. Live at `/var/www/procam-crm` (SQLite `procam_crm.db`,
`.venv`); deploy is `git pull` then
`sudo systemctl restart procam-crm`.

Tests: `SECRET_KEY=x ADMIN_INITIAL_PASSWORD=Test-123456789
.venv/bin/python -m pytest`. Dev extras (pytest-xdist) are in
`requirements-dev.txt`.

## Speed rules (Nilesh)

- Run ONLY the tests for the files you changed: `.venv/bin/python -m pytest -x -q -n auto <files>`. Never wait on the full suite before pushing; if it's needed, run it in the background after pushing and report only failures.
- Add pytest-xdist to dev requirements if missing.
- Before writing code, check that every model field you use exists. Don't re-check things already verified in this session.
- One small commit per item, pushed as soon as its tests pass. Never `git add -A`; add only your files.
- Replies: HASH, restart or migration, commands in run order, max 5 lines of explanation, then a Chrome UAT checklist.
- If something is ambiguous, ask ONE question and continue with the rest meanwhile.

## Standing constraints

- Commit as `Tech team <nilesh.sinha@procamlogistics.com>`, no other attribution.
- No employee names or codes in code, seeds or tests.
- Never hard-delete: archive or merge reversibly, with an audit row.
- Every data change: dry run first, then `--apply`, with `--undo --batch`.
- The actor for scripts is `DIR12010` (PCM001 does not exist in the CRM).
- A write without a CSRF token silently does nothing — include `_csrf_fetch.html` on any page that posts.
- No AI or vendor names on any screen.
