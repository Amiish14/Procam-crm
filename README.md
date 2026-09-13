# Procam CRM

The customer relationship management system for Procam's logistics
business: leads from the enquiry mailbox, accounts and contacts, RFQs,
quotations and won-deal handovers, pipeline and reporting, and the
Procam AI Copilot — with access governed by one Access Matrix and every
important change audited.

## Capabilities

| Area | What it does |
|---|---|
| Lead management | Pipeline by stage, owners and secondary owners with reasons, follow-ups, notes with version history, the customer email trail, notes search |
| Email intake | Leads mailbox via Microsoft Graph; rules-first classifier (new lead, reply, quote, rate sourcing, internal, duplicate, non-business), duplicate scoring with reasons, review queue, learning from corrections, Vendor Master |
| Accounts and contacts | Company 360, contacts, account ownership and teams, overseas agents, business-card scanning |
| Deals | RFQs and rate sourcing, quotations with approval, won-deal handover with PO capture |
| Intelligence and reporting | Dashboards, action/competitor/account reports, funnels, market intelligence, classification intelligence |
| Procam AI Copilot | Plain-language questions answered from the CRM within each person's access; permission-filtered search; private model only |
| Data quality | Scoped checks with suggestions, previewed and audited batch correction, daily trends |
| Administration | Access Matrix, employees, master data, bulk tools, audit trail, operations status, Academy |

## Documentation

| For | Start here |
|---|---|
| Staff using the CRM | [User Guide](docs/USER_GUIDE.md) |
| CRM Administrators | [Administrator Guide](docs/operations/ADMINISTRATOR_GUIDE.md) · [Classification](docs/operations/CLASSIFICATION_GUIDE.md) · [Data Quality](docs/operations/DATA_QUALITY_GUIDE.md) · [AI Configuration](docs/operations/AI_CONFIGURATION_GUIDE.md) |
| Operations / Deployment Team | [Operations index](docs/operations/README.md) — runbook, deployment, backup, rollback, disaster recovery, monitoring, troubleshooting |
| IT (Microsoft 365) | [Graph Setup Guide](docs/operations/GRAPH_SETUP_GUIDE.md) |
| Security Team | [Security](docs/SECURITY.md) · [RBAC](docs/RBAC.md) |
| Development Team | [Developer Guide](docs/DEVELOPER_GUIDE.md) · [Architecture](docs/ARCHITECTURE.md) · [Database](docs/DATABASE.md) · [API reference](docs/reference/API.md) · [Schema reference](docs/reference/SCHEMA.md) |
| Sign-off | [Production Readiness Report](docs/operations/PRODUCTION_READINESS_REPORT.md) |

## Quick start (development)

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env        # set SECRET_KEY and ADMIN_INITIAL_PASSWORD; SESSION_COOKIE_SECURE=false locally
.venv/bin/python app.py     # http://localhost:5000
.venv/bin/python -m pytest -q -p no:warnings
```

On an empty database the `PCM001` administrator is created from
`ADMIN_INITIAL_PASSWORD` and must change it at first sign-in. New and
reset accounts receive temporary passwords from an administrator; no
account uses a predictable default.

## Production

Azure VM, gunicorn under systemd behind nginx at `/CRM`, SQLite, with
scheduled backups and health checks. Follow the
[Deployment Guide](docs/operations/DEPLOYMENT_GUIDE.md); the server is
pull-only and every deploy starts with a verified backup.

## Repository layout

```
app.py              application, core models, authentication, core APIs, boot
app/                feature packages (access, intake, copilot, rfq, quote, handover,
                    company360, data_quality, ops, reports, …) and shared services
presales/           account development and projects
email_ingest/       Microsoft Graph client, webhook and ingest pipeline
templates/, static/ pages; templates/app.html is the main application
scripts/            migrations (YYYY_MM_DD_*.py) and operational tools
docs/               user, administrator, operations, security and developer docs
tests/              pytest suite
```
