# Developer Guide

For the Development Team: setting up, the conventions every change
follows, and how to prove a change is safe.

## Set up

```bash
git clone <repository-url> procam-crm && cd procam-crm
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env            # fill SECRET_KEY; SESSION_COOKIE_SECURE=false for http://localhost
.venv/bin/python app.py         # http://localhost:5000
.venv/bin/python -m pytest -q -p no:warnings
```

A fresh database seeds the `PCM001` administrator from
`ADMIN_INITIAL_PASSWORD`. Employee seed data is never in the repository;
put an export in `data/private/seed_employees.csv` (git-ignored) if you
need realistic users locally.

## Where things go

See [Architecture](ARCHITECTURE.md). In short: a new feature is a
blueprint package under `app/` (routes, service) registered in the list
at the end of `app.py`; shared logic in `app/services/`; models for new
tables in `app/models/`.

## The rules

1. **Access**: every route has a gate (`require('<permission>')` or the
   module's wrapper); every query goes through `app/access/scope.py` or
   `records.py`; by-id routes answer 404 outside scope. Never test the
   role name. See [RBAC](RBAC.md).
2. **Audit**: changes to watched models are audited automatically. Add
   new important fields to `WATCH` in `app/services/audit_listener.py`;
   call `audit.record()` for anything that is not an ORM field change
   (bulk SQL, external actions, exports). Never pass secrets to it.
3. **Schema**: additive, with boot autoheal and a migration script. See
   [Database](DATABASE.md).
4. **API**: JSON in and out; errors are `{ok: false, error}` with the
   right status (the global handlers do this for raised HTTP errors).
   Do not rename or remove fields; add new ones.
5. **Security**: validate uploads with `app/utils/uploads.save_upload`
   or `file_validation.validate`; write exports through
   `app/utils/spreadsheet_safe`; escape every value you interpolate into
   HTML in JavaScript; send `X-CSRFToken` on writes; no new external
   script origins without updating the CSP (a test checks).
6. **AI**: the Copilot answers only through catalogue intents with
   scoped queries; models never produce SQL; no public AI host.
7. **Data**: never modify business data automatically; batch changes
   are previewed, confirmed, reasoned and audited.
8. **Repository hygiene**: no personal names, personal emails or phone
   numbers (use role terms and `example.com` placeholders); no author
   signatures; secrets only in `.env`; customer data only in git-ignored
   folders (`data/private/`, `reports/`, `backups/`).
9. **Style**: match the surrounding code. Comments say *why*, in plain
   English. Commit messages explain what changed and why, and what was
   verified.

## Proving a change

- **Behavioural tests** for the change (pytest, `tests/`). Tests share
  one SQLite database per run and SQLite reuses deleted ids: clean up
  what you create and prefer before/after deltas over absolute counts.
- **Leak tests** for anything touching visibility: an Own-scope user
  must not reach another owner's record, and the legitimate allowances
  must still work.
- **Mutation check** each guard you add: break it, confirm a test fails,
  restore it. Hold the original file in memory while doing so; never
  restore with `git checkout` (it discards your uncommitted work).
- **Full suite** passes before every commit — read the last line.
- **Reference docs**: `scripts/generate_reference_docs.py` after adding
  routes or tables; `tests/test_every_route_has_a_gate.py` must pass.
- **Preflight** against a copy of a backup for schema changes.

## Useful scripts

| Script | Purpose |
|---|---|
| `production_preflight.py` | configuration, schema drift, database, ops (read-only) |
| `ops_status.py` | health checks for monitoring (read-only) |
| `data_quality_report.py` | data-quality CSVs (read-only) |
| `perf_benchmark.py` | performance on seeded data or a backup copy |
| `generate_reference_docs.py` | API and schema reference |
| `backup_database.py` | verified online backup |
| `audit_default_passwords.py` | guessable-password audit (read-only) |
| `build_copilot_index.py` | rebuild Copilot search passages |

## Release

Follow the [Deployment Guide](operations/DEPLOYMENT_GUIDE.md): the
production server is pull-only, migrations run with `--check` first,
and every deploy starts with a verified backup.
