# Procam ProConnect CRM v3.0
**Full-stack pre-sales CRM for Procam Group — Sales, Pre-Sales, Admin access tiers**

## Features
- Employee Master with emp code as default password + forced change on first login
- Lead pipeline: New → Call Done → Profile Sent → Appointment → Visit Done → RFQ Generated → Won/Lost
- Activity date tracking (phone call, intro mail, meeting, RFQ) with onboarded date stamp
- ProConnect Opportunity number assignment when RFQ is received
- Market Intelligence: ETManufacturing / Projects Today newsletter parsing from Nilesh's Outlook
- Global CRM: People, Companies, Overseas Agents with country/city/website
- AI Outreach: Claude-powered email generation with project context
- Excel upload with fuzzy column detection (any format)
- Role-based access: Admin sees all, Pre-Sales sees only their own leads
- Procam brand colors: Red #C72435, Charcoal #474447

## Default Login
- **Admin**: emp_code `PCM001` / password `admin@Procam25`
- All other employees: emp_code (e.g. `PCM101`) / password = emp_code in lowercase (`pcm101`)
- First login forces password change

## Local Development
```bash
cd procam_crm
pip install -r requirements.txt
python app.py
# Open http://localhost:5000
```

## Deploy on Render (like PRERNA)
1. Push this folder to a GitHub repository
2. Go to render.com → New Web Service → Connect GitHub repo
3. Build command: `pip install -r requirements.txt`
4. Start command: `gunicorn app:app --workers 2 --bind 0.0.0.0:$PORT --timeout 120`
5. Add environment variable: `SECRET_KEY` (any random string)
6. Add PostgreSQL database → copy connection string → set `DATABASE_URL`
7. Deploy — app initializes DB automatically on first run

## Database
- Local: SQLite (`procam_crm.db` created automatically)
- Production: PostgreSQL (Render provides free tier, same stack as PRERNA)

## News Intelligence (Outlook Integration)
The news fetch uses Microsoft Graph API. The MS365 Claude integration is already active.
For the daily 7am auto-fetch, either:
- Schedule a Render Cron Job to hit `/api/news/fetch` (POST) with admin session
- Or use the "Seed from Outlook" button manually in the Intelligence tab

## Employee Roles
| Role | Access |
|------|--------|
| admin | Everything — all leads, team, assign, employees, news |
| presales | Own leads only, outreach, contacts |
| user | Own leads only, outreach, contacts |

## File Structure
```
procam_crm/
├── app.py              # Flask app + all API routes + models
├── requirements.txt    # Python dependencies
├── Procfile           # Gunicorn start command
├── render.yaml        # Render.com deployment config
├── .env.example       # Environment variable template
├── templates/
│   ├── login.html     # Login page (Procam branding)
│   ├── app.html       # Main SPA application
│   └── change_password.html  # First-login password change
└── static/            # Static assets (CSS/JS embedded in templates)
```
