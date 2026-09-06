# Procam CRM — Phase 12-15 Acceptance Test Checklist

Walk through this list after each deploy.  Each row: `[ ]` PASS / `[F]` FAIL / `[N]` N/A.

## Step 0 — Automated route walk (do this first)

`scripts/smoke_test_routes.py` opens every GET route in-process, impersonating
any employee you name, and reports which ones return 5xx.  It needs no browser,
no password, and no other user to be logged in — which matters, because during
rollout the admin is the only account in use.

    # as an admin
    sudo -u procamapp bash -c 'cd /var/www/procam-crm && \
        source .venv/bin/activate && \
        python scripts/smoke_test_routes.py'

    # as a salesperson — the view the team actually gets
    sudo -u procamapp bash -c 'cd /var/www/procam-crm && \
        source .venv/bin/activate && \
        python scripts/smoke_test_routes.py --as EMP372011'

    # full traceback for anything that failed
    ... python scripts/smoke_test_routes.py --verbose --only '/reports/'

It is **read-only**: GET only, and any endpoint whose name looks mutating
(delete / send / purge / sweep / reset / sync …) is skipped by design.

Exit code = number of routes returning 5xx, so it can gate a deploy.

Legend: `OK` fine · `AUTH` permission check fired · `404` no such row (not a
crash) · `CFG` dependency not configured on this host · `FAIL` real 5xx.

- [ ] Run as admin — 0 FAIL.
- [ ] Run as a non-admin — 0 FAIL.
- [ ] `python -m pytest tests/ -q` — all green.

Only then walk the manual checks below, which cover the things a route walk
cannot see: layout, camera access, offline behaviour, and whether the numbers
on the page are actually right.

## Preconditions

- [ ] `python scripts/2026_09_07_crm_final.py --check` reports the
      3 new tables + ~16 pending help article seeds without error.
- [ ] `python scripts/2026_09_07_crm_final.py` applies successfully.
- [ ] Systemd/gunicorn restarted; `/` returns 200 for a logged-in user.

## Phase 12 — Business Card Scanning (spec §25-27)

- [ ] `/business-cards/scan` renders on desktop.
- [ ] Same page renders on a mobile browser and offers the device camera.
- [ ] Uploading a photo of a card returns extracted fields within ~10 s.
      (If `ANTHROPIC_API_KEY` is unset, the page shows an "OCR warning"
      banner and still lets the user type the fields in.)
- [ ] Duplicate matches for an existing Company by name are shown.
- [ ] "Save as new" creates a Company + Contact and hides the card.
- [ ] "Link to existing" attaches the card to the picked Account/Contact.
- [ ] "Discard" marks the card and removes it from the queue.
- [ ] Upload of a `.exe` is rejected with a 400.
- [ ] Upload of a > 8 MB file is rejected with a 400.

## Phase 13 — Self-Help / User Manual (spec §54-56)

- [ ] `/help` renders and lists the 16 seeded skeleton articles.
- [ ] Filtering by role shows only the appropriate articles.
- [ ] `/help/<slug>` renders a single article.
- [ ] `/admin/help` (admin only) allows create / delete of articles.
- [ ] `/admin/help` allows create / delete of tooltips.
- [ ] `GET /api/help/tooltips?page=X` returns JSON keyed by element_key.
- [ ] Non-admin users hitting `/admin/help` receive a 403.

## Phase 14 — Mobile / PWA (spec §23-24)

- [ ] `/static/manifest.json` returns valid JSON.
- [ ] `/sw.js` returns the service-worker JS with `Content-Type: application/javascript`.
- [ ] Chrome DevTools → Application → Manifest lists Procam CRM.
- [ ] Chrome DevTools → Application → Service Workers shows the SW registered.
- [ ] Turning DevTools "Offline" mode on and reloading a previously loaded
      page shows the `/offline` page rather than the browser error.
- [ ] Adding to Home Screen on iOS / Android installs an icon.
- [ ] `< 768px` viewport collapses navigation into a burger.
- [ ] Floating action buttons on Account detail expose Call/Email/WhatsApp.

## Phase 15 — Reports + Regression (spec §59-62)

Action Management
- [ ] `/reports` hub renders with the 3 categories.
- [ ] Each Action Management report renders (may show empty state).
- [ ] `?format=xlsx` on any report downloads a valid `.xlsx`.
- [ ] `?owner=<emp_code>` filter narrows Open Tasks correctly.
- [ ] SLA reports compute hit % correctly against `TaskDefinition.sla_hours`.

Competitor
- [ ] Competitor Register lists CompetitorMaster rows.
- [ ] Competitor Intelligence Log lists CompetitorIntelligence rows.
- [ ] Win/Loss by Competitor tallies OpportunityCompetitor rows.

Account Development
- [ ] Accounts-by-PIC totals reconcile with the Companies table.
- [ ] Won Value by Account reconciles with `SUM(value_inr)` on Won opps.
- [ ] Dormant Accounts uses `?days=` (defaults 90).

## Regression

- [ ] `/my-work` still renders.
- [ ] `/rfqs`, `/quotes`, `/competitors`, `/funnel` still render.
- [ ] `/notifications` still renders.
- [ ] `python -m compileall app app.py scripts` finishes with 0 errors.
- [ ] `python -c "import ast; ast.parse(open('app.py').read())"` succeeds.
