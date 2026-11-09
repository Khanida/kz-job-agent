# Khanida's Job Agent — Multi-Agent Career Radar

An intelligent, hierarchical multi-agent job discovery system that scouts live vacancies, validates recruitment recency, stages cross-examination debates between specialized Sub-Agents, and convenes a Supreme Jury to arbitrate and crown your final daily selection.

## Agent Architecture Hierarchy

1. **Level 3: Worker Scouts (Discovery & Liveness Verification)**
   - `ArbeitnowScout`: Queries European and international tech listings, verifying recruitment freshness.
   - `RemotiveScout`: Surfaces worldwide remote engineering vacancies with salary metrics.
   - `DirectEmployerScout`: Curates verified international employer pipelines (Jane Street, ASML, Booking.com, Delivery Hero, Canva, Bloomberg).
   - Verifies liveness and active recruitment status on employer career pages.

2. **Level 2: Domain Sub-Agents (Specialized Advocates)**
   - 💻 **Software & Systems Agent**: Screens deep stack alignment against Adeena's Computer Systems Engineering background (Python, C++, Java, Flutter, Firebase, Embedded Systems).
   - 🎯 **Quality & Operations Agent**: Assesses QA automation, test suites, IT support, and systems diagnostics. Challenges unrealistic senior barriers.
   - 🌍 **Visa & Relocation Advocate**: Vets international sponsorship, EU Blue Card, UK Skilled Worker, and relocation allowances for a Pakistani national in KSA. Vetoes roles that exclude non-residents.

3. **Level 1: The Supreme Jury Agent (Chief Career Arbiter)**
   - Receives nominations from all Sub-Agents.
   - Presides over the **Debate Round**, where Sub-Agents pitch, challenge, and cross-examine each other's candidate vacancies.
   - Weighs technical excellence (40%), immigration viability (35%), and operational accessibility (25%).
   - Issues formal **Jury Verdicts** and crowns the **Daily Top Selection** on your dashboard.

## Features Included
- **Jury Room UI** (`/jury`): Interactive court viewer showing live agent speeches, cross-examination challenges, and the Supreme Jury's verdicts.
- **Daily Selection Shelf**: Highlights up to five of the day's winning roles on the main dashboard.
- **Daily refresh**: The app reruns the worker, Sub-Agent, and Jury workflow at midnight UTC and catches up on startup if the service was offline. Use **Refresh Today’s Top 5** on the dashboard to run it manually.
- **Posting availability debate**: Each refresh checks the job page, records whether it is reachable, closed, expired, or unverified, and adds that evidence to the Jury debate. Only reachable postings can enter the daily top five; expired and unverified links are not presented as confirmed openings.
- **CV-based role matching**: Text extracted from PDF, DOCX, TXT, or Markdown CVs is analyzed locally for skills and likely target roles. The suggestions are editable in the profile and feed a dedicated CV-alignment score in every Jury decision and daily selection.
- **Candidate fit safeguards**: Profile preferences can restrict recommendations to entry-level roles, exclude skills and job types the candidate does not want (such as Rust or backend/full-stack), and reject postings explicitly limited to another nationality. The preferred city is a ranking preference, not a location filter; qualified jobs in other cities and remote roles remain eligible.
- **Local expat eligibility check**: For a Pakistani candidate's preferred city in Saudi Arabia, local picks require explicit posting evidence that Pakistani/non-Saudi applicants are eligible. Generic visa-sponsorship or iqama language is only a signal and keeps a role deferred until eligibility is confirmed; no listing text proves the employer's past hiring history.
- **Animated workflow**: The dashboard illustrates workers collecting job files, Sub-Agents debating, and the Jury ranking the daily top five whenever the page loads.
- **Live Liveness & Recency Indicators**: Real-time tags (`🟢 Live on Website`, `📅 Recently posted`).
- **Pipeline Workflow**: Status transitions (`New`, `Shortlisted`, `Applied`, `Dismissed`) logged in SQLite.
- **Candidate Profile Matrix** (`/profile`): Custom matching criteria, skills, and CV summary.
- **Render Ready**: Blueprint config with persistent disk support.

## Important limitations
This is a starter, not a finished autonomous applicant. The current scanner is a discovery prototype. Public feeds are incomplete, keyword matching is not semantic, and visa/housing evidence is not verified. The application does **not** submit applications, create accounts, answer legal questions, or send personal data to employers.
CV skills and role suggestions use local keyword heuristics, not an external AI service; review the suggested roles before relying on them.

Before production:
1. Add authentication and per-user authorization.
2. Replace SQLite with managed PostgreSQL (Supabase or Render Postgres).
3. Add employer ATS integrations (Greenhouse/Lever) and source-specific terms/rate limits.
4. Add an evidence-backed verification pipeline with source URLs and timestamps.
5. Add encrypted CV storage and secrets management.
6. Move scheduled work to a dedicated worker if the web service is scaled to multiple instances.
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
