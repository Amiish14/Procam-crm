# Procam CRM — Deployment Guide (v3.1)

Two supported deploy targets:

- **Render** (SaaS, using `render.yaml`)
- **Same Azure VM as the TMS** (nginx proxies `procamlogictech.com/CRM/` → gunicorn on `127.0.0.1:8001`)

---

## What v3.1 adds

- `PCM001` Super Admin seeded from `ADMIN_INITIAL_PASSWORD` env var (forced change on first login)
- Persistent `Opportunity` model with unique `opp_number` (no more stateless counter)
- Global CRM: `Company` and `OverseasAgent` models + CRUD APIs
- `LeadActivity` + `LeadStageHistory` — activity/timeline never lost across stage moves
- AI Outreach via Claude — `POST /api/outreach/generate` (requires `ANTHROPIC_API_KEY`)
- Smarter import — `POST /api/leads/import/preview` (fuzzy header mapping, duplicate flagging) → `POST /api/leads/import/commit`
- `ProxyFix(..., x_prefix=1)` so URLs render with `/CRM` prefix when behind nginx

### New env vars

```
ADMIN_INITIAL_PASSWORD   # First-boot password for PCM001. NOT stored in source.
ANTHROPIC_API_KEY        # Enables AI Outreach. Route returns 503 if unset.
ANTHROPIC_MODEL          # Optional. Default: claude-sonnet-4-5-20250929
URL_PREFIX               # Set to /CRM when behind procamlogictech.com/CRM/
```

---

## Option A — Render

1. Push the repo (see below).
2. In Render: **New +** → **Blueprint** → point at `github.com/Amiish14/Procam-crm`.
3. Render reads `render.yaml`, creates the `procam-crm-db` Postgres and `procam-crm` web service.
4. Under the service's **Environment** tab, add:
   - `ADMIN_INITIAL_PASSWORD` = `admin@Procam25`
   - `ANTHROPIC_API_KEY` = (from console.anthropic.com)
5. First deploy will seed 115 employees + PCM001. Log in as `PCM001` / `admin@Procam25` → forced password change.

---

## Option B — Same VM as TMS (procamlogictech.com/CRM/)

**Assumes** the TMS is already running on `127.0.0.1:8000` behind nginx and the repo is checked out at `/var/www/procam-crm`.

### 1. Clone the CRM alongside the TMS

```bash
ssh procam-app
sudo mkdir -p /var/www/procam-crm
sudo chown procamapp:procamapp /var/www/procam-crm
cd /var/www/procam-crm
git clone https://github.com/Amiish14/Procam-crm.git .
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 2. `.env`

```bash
sudo -u procamapp tee /var/www/procam-crm/.env >/dev/null <<'EOF'
SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")
DATABASE_URL=postgresql://procam:PASSWORD@localhost:5432/procam_crm
ADMIN_INITIAL_PASSWORD=admin@Procam25
ANTHROPIC_API_KEY=sk-ant-xxxxxxxxxxxxx
ANTHROPIC_MODEL=claude-sonnet-4-5-20250929
URL_PREFIX=/CRM
PORT=8001
EOF
```

Create the DB:

```bash
sudo -u postgres psql <<'EOF'
CREATE USER procam WITH PASSWORD 'PASSWORD';
CREATE DATABASE procam_crm OWNER procam;
GRANT ALL PRIVILEGES ON DATABASE procam_crm TO procam;
EOF
```

### 3. systemd unit

```bash
sudo tee /etc/systemd/system/procam-crm.service >/dev/null <<'EOF'
[Unit]
Description=Procam CRM (Flask + gunicorn on 8001)
After=network.target postgresql.service

[Service]
User=procamapp
Group=www-data
WorkingDirectory=/var/www/procam-crm
EnvironmentFile=/var/www/procam-crm/.env
ExecStart=/var/www/procam-crm/.venv/bin/gunicorn \
    app:app --workers 2 --bind 127.0.0.1:8001 --timeout 120 \
    --access-logfile /var/log/procam-crm/access.log \
    --error-logfile /var/log/procam-crm/error.log
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo mkdir -p /var/log/procam-crm
sudo chown procamapp:procamapp /var/log/procam-crm
sudo systemctl daemon-reload
sudo systemctl enable procam-crm
sudo systemctl start procam-crm
sudo systemctl status procam-crm
```

### 4. Nginx

The `/CRM/` proxy block is already in `hub/nginx-procamlogitech.conf`. Push the updated conf and reload:

```bash
scp app/../hub/nginx-procamlogitech.conf procam-app:/etc/nginx/sites-available/procamlogitech
ssh procam-app 'sudo nginx -t && sudo systemctl reload nginx'
```

### 5. Update the hub landing page

```bash
scp hub/index.html procam-app:/var/www/procam-lr/hub/index.html
```

### 6. Test

Open `https://procamlogictech.com/` → the **CRM** tile should be visible next to TMS / PMS / Verifleet. Click it → CRM login screen. Log in as `PCM001` / `admin@Procam25` → forced password change → dashboard.

---

## Pushing to GitHub

```bash
cd ~/Desktop/Procam-crm-main
git remote -v                         # confirm origin points at github.com/Amiish14/Procam-crm
git add app.py requirements.txt .env.example DEPLOY.md
git commit -m "v3.1: PCM001 admin + Opportunity + Company/Agent + AI outreach + fuzzy import + activity log"
git push origin main
```

Also push the hub changes from the LR repo (TMS repo):

```bash
cd ~/Desktop/Procam-lr-main
git add hub/index.html hub/nginx-procamlogitech.conf
git commit -m "Hub: add CRM tile + nginx /CRM/ proxy block"
git push origin main
```

---

## First-login checklist

1. Log in as `PCM001` / `admin@Procam25`
2. System forces a password change — pick something strong
3. Verify the sidebar shows: Dashboard · Leads · Opportunities · Companies · People · Employees · Intelligence · Outreach · Imports · Settings
4. Test import: upload a sample Excel to `/api/leads/import/preview`, review the JSON, POST the batch_id to `/api/leads/import/commit`
5. Test Opportunity: create one — verify the `opp_number` is `OPP-2026-0001`
6. Test AI Outreach: pick a lead, hit `/api/outreach/generate` with `{"lead_id": <id>, "channel": "email"}` — Claude drafts a subject+body

## Rollback

```bash
sudo systemctl stop procam-crm
sudo systemctl disable procam-crm
# nginx: comment out the /CRM/ location block, nginx -t && reload
# DB: leave it — no data loss
```
