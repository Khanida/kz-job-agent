# Adeena's Job Agent — starter

A cloud-ready starter for an international job discovery and matching dashboard.

## What is included
- FastAPI web dashboard and candidate profile editor
- Job discovery from Arbeitnow and Remotive public feeds
- Transparent keyword-based CV/role relevance score
- Separate evidence fields for visa sponsorship and accommodation
- Shortlisting and application pipeline foundation
- Render Blueprint with persistent disk

## Important limitations
This is a starter, not a finished autonomous applicant. The current scanner is a discovery prototype. Public feeds are incomplete, keyword matching is not semantic, and visa/housing evidence is not verified. The application does **not** submit applications, create accounts, answer legal questions, or send personal data to employers.

Before production:
1. Add authentication and per-user authorization.
2. Replace SQLite with managed PostgreSQL (Supabase or Render Postgres).
3. Add employer ATS integrations (Greenhouse/Lever) and source-specific terms/rate limits.
4. Add an evidence-backed verification pipeline with source URLs and timestamps.
5. Add encrypted CV storage and secrets management.
6. Add scheduled worker and email notifications.
7. Add an application preparation/review queue. Submission should require explicit approval for each employer/application until a safe, accurate authorization model is established.

## Run locally
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```
Open http://127.0.0.1:8000

## Run tests
```bash
python -m unittest tests/test_main.py
```

## Deploy on Render
Push this repository to GitHub, then create a Render Blueprint from `render.yaml`.
The Blueprint provisions a web service and persistent disk. For production, connect PostgreSQL and configure secrets in Render's dashboard.

## Environment
- `DATABASE_PATH`: SQLite path; defaults to `jobs.db` in project root.
