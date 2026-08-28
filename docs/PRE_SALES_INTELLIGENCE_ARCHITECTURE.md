# Pre-Sales Intelligence Layer · Architecture Assessment

**Feature:** Add a structured pre-sales layer to the Procam CRM that captures
Account Development and Project Intelligence *before* an RFQ exists. When
an RFQ appears, it flows into the existing Lead / Opportunity / RFQ pipeline
without rebuilding any of that logic.

**Assessment based on inspection of the live CRM (`app.py`, 3 142 LOC)
performed 27-Aug-2026.**

---

## 1. What already exists (reusable as-is)

| Concept | Where it lives | Reuse strategy |
|---|---|---|
| **Employee + role + vertical + vertical-head hierarchy** | `Employee` model, `role` field (`admin`/`sales`/`presales`/`user`), `vertical`, `is_vertical_head`, `vertical_head_id` | Account PIC + Project PIC both reference `Employee.emp_code`. All new role-scoped filtering piggybacks on the existing pattern in `_apply_dashboard_filters()`. |
| **Lead + StageHistory + Activity** | `Lead`, `LeadStageHistory`, `LeadActivity`. Stage machine `STAGES_PIPELINE`, `STAGE_NEXT`, `STAGE_DIRECT_WIN` | Untouched. Every RFQ still flows through this. |
| **Company (proto-Account)** | `Company` model at `app.py:391` — has name/industry/website/country/state/city/tier/notes | Extended into `Account` via additive columns (not a new table). See §4. |
| **Contact** | `Contact` model at `app.py:322` — has agent_type + assigned_to | Extended with `account_id` FK. Keeps overseas-agent semantics. |
| **NewsItem** | Already tracks news snippets by industry/state | Becomes an intelligence source for `ProjectUpdate`. |
| **Opportunity** | `Opportunity` at line 454 | New `source_type` + FKs (`source_account_id`, `source_project_id`) added — no schema break. |
| **Dashboard filter pipeline** | `_apply_dashboard_filters()` at line 853 | Extended to accept `account_id` / `project_id` filters. |
| **Migration script pattern** | `scripts/YYYY_MM_DD_*.py` — additive-only, dry-run + `--apply` | Every new phase gets one migration file. |
| **Employee onboarding CLI** | `scripts/add_employee.py` | Untouched. |

## 2. What existed but needs extension

- **`Company`** → grows into the Account concept by adding: `account_type` list, `relationship_types` (JSON tags), `dev_stage`, `pic_emp_code`, `strategic_flag`, `priority`, `last_activity_at`, `next_action_at`, `parent_account_id`. Legacy Company rows still work — new columns default to safe values.
- **`Contact`** → gains `account_id` FK (nullable, since a contact may exist without being tied to an Account yet), `is_active`, `relationship_strength`, `decision_role`. Existing overseas-agent rows continue to load.
- **`Opportunity`** → gains `source_type` (Direct/Account/Project/Referral/Overseas/etc.), `source_account_id`, `source_project_id`. Nullable. Zero effect on existing Opportunity rows.
- **Dashboard filter pipeline** — reads new query params `account_id`, `project_id`.
- **Sidebar / topnav** — adds Accounts + Projects entries under Workspace + Administration, per role.

## 3. Genuinely new entities

Ten new tables, all additive:

```
account_relationship_types    (many-to-many tag: Customer / EPC / Vendor / …)
account_activities            (parallel to LeadActivity — pre-sales activity)
account_assignments           (PIC change history)
account_stage_history         (dev-stage change history)

projects                      (Project Intelligence master)
project_updates               (chronological intelligence timeline)
project_accounts              (many-to-many with role: Owner / EPC / Consultant …)
project_contacts              (link Contact to Project)
project_stage_history         (stage change history)

opportunity_source_links      (attribution: this RFQ came from Account X / Project Y)
```

Total new columns on existing tables: **12** (all nullable, all with sensible defaults).

## 4. Migration impact

- **Zero destructive changes.** No table dropped, no column renamed, no constraint tightened.
- Every new table is created with `IF NOT EXISTS`.
- Every column added to `Company` / `Contact` / `Opportunity` is nullable.
- Downgrade path: drop the ten new tables and reset the twelve nullable columns to NULL. Old code paths continue to work.
- Migration scripts follow existing `YYYY_MM_DD_*.py` pattern with `--apply` flag.
- Deploy window: no downtime. Restart `procam-crm` after each phase's migration.

## 5. Role / access impact

- **Admin** — sees everything (unchanged).
- **Sales / Presales** — sees Accounts and Projects assigned to their `emp_code`. Vertical heads see their team's records via `vertical_head_id` chain (same pattern already used by `_apply_dashboard_filters`).
- **User** — sees only their own assignments.
- **Reassignment** — the previous PIC keeps read access for 30 days by default (via `account_assignments` history) so handovers don't cut off knowledge overnight. Admin-configurable.
- **All access decisions live in the existing role model.** No new role names required.

## 6. Phased delivery — 7 phases mapped to the spec

| Phase | Scope | Deploy artefact |
|---|---|---|
| **1** | Account Master extension + Assignment history + Activity timeline. Read + write API. Minimum UI (`/api/accounts`, list + detail modal). | Ready — this commit. |
| **2** | Project Intelligence master + Project timeline. Read + write API + list/detail UI. | Next commit. |
| **3** | Account ↔ Project mapping (`project_accounts`, `project_contacts`, many-to-many role tags). | Third commit. |
| **4** | Convert-to-Opportunity flow (`opportunity_source_links` + carry-forward of Account/Project/Contacts/PIC). Existing Lead pipeline unchanged. | Fourth commit. |
| **5** | Dashboard blocks + interactive filters (Account and Project sections in the existing dashboard, cross-filterable). | Fifth commit. |
| **6** | Reports + KPI integration (extend existing KPI Settings, new Excel exports). | Sixth commit. |
| **7** | Historical data mapping — safe deduplication tools to link existing Leads/Companies to Accounts without creating duplicates. | Seventh commit. |

Each phase ships behind a feature flag (`ENABLE_PRE_SALES=true`) so we can promote per role without impacting the live sales team.

## 7. Data-model summary (Phase 1 subset)

```
Employee (existing)
    │  emp_code
    │
    ▼  pic_emp_code
Account (was Company + new columns)
    ├── AccountRelationshipType     (M:N tag)
    ├── AccountAssignmentHistory    (audit)
    ├── AccountStageHistory         (audit)
    └── AccountActivity             (timeline)
        │
        ▼
    Contact (existing + account_id)
```

Phase 2 adds `Project`, `ProjectUpdate`, `ProjectStageHistory` off the same
`Employee.emp_code` PIC. Phase 3 wires `ProjectAccount(role)`,
`ProjectContact`. Phase 4 wires `OpportunitySourceLink(source_type,
account_id, project_id)`.

## 8. What is deliberately NOT done

- No parallel RFQ pipeline. All commercial pipeline logic stays in `Lead` / `Opportunity`.
- No duplicated Company table. `Company` becomes Account by additive extension.
- No fresh Contact table. Existing `Contact` gets an `account_id` FK.
- No parallel role system. Existing `Employee.role` + `is_vertical_head` chain is reused.
- No mock/hardcoded values anywhere. Every field on the UI resolves from a live DB query.

## 9. Acceptance mapping

Every one of the 21 acceptance criteria at the end of the spec is either:

- Already delivered by the existing CRM (criteria 7, 13, 21) — **no work needed**.
- Delivered by Phase 1 (criteria 1-6) — this commit.
- Delivered by Phase 2 (criteria 8-11) — next.
- Delivered by Phase 3 (criterion 11 sub-clause on EPC/Owner/Consultant/Supplier linking).
- Delivered by Phase 4 (criterion 12-13 sub-clause on conversion).
- Delivered by Phase 5-6 (criteria 14-20 — dashboards + reports).

No criterion depends on rewriting existing functionality.
