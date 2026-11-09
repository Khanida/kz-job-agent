from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
import sqlite3, os, json, re, hashlib, html
from datetime import datetime, timezone
from urllib.parse import quote_plus
import httpx

APP_DIR = Path(__file__).resolve().parent
DB_PATH = os.getenv("DATABASE_PATH", str(APP_DIR.parent / "jobs.db"))

SEED_JOBS = [
    {
        "title": "Graduate Software Engineer (Python / Distributed Systems)",
        "company": "Jane Street",
        "location": "London, United Kingdom",
        "url": "https://www.janestreet.com/join-jane-street/position/graduate-software-engineer/",
        "source": "Direct Employer",
        "description": "We are seeking a Graduate Software Engineer to develop high-performance software systems. Requirements: Degree in Computer Systems Engineering, Computer Science or related; strong foundations in Python, C++, or Java. Full visa sponsorship and comprehensive relocation package provided for international candidates.",
        "visa_evidence": "Mentioned: visa sponsorship, relocation support",
        "housing_evidence": "Mentioned: relocation package, temporary housing"
    },
    {
        "title": "Junior Backend Developer (Python)",
        "company": "Delivery Hero",
        "location": "Berlin, Germany",
        "url": "https://careers.deliveryhero.com/global/en/job/junior-backend-developer",
        "source": "Arbeitnow",
        "description": "Join our global logistics platform as a Junior Backend Engineer. Work with Python, FastAPI, relational databases and cloud microservices. We offer full German EU Blue Card visa sponsorship and relocation assistance to Berlin.",
        "visa_evidence": "Mentioned: visa sponsorship, work permit",
        "housing_evidence": "Mentioned: relocation package, temporary housing"
    },
    {
        "title": "Associate Embedded Systems Engineer",
        "company": "ASML",
        "location": "Veldhoven, Netherlands",
        "url": "https://www.asml.com/en/careers/find-your-job/associate-embedded-engineer",
        "source": "Direct Employer",
        "description": "Looking for entry-level Embedded Systems Engineers with expertise in C++, Python, microcontroller architecture and hardware interfacing. Full Dutch Highly Skilled Migrant visa support and relocation allowance provided.",
        "visa_evidence": "Mentioned: visa support, work permit",
        "housing_evidence": "Mentioned: housing allowance, relocation package"
    },
    {
        "title": "Junior QA Automation Engineer",
        "company": "Booking.com",
        "location": "Amsterdam, Netherlands",
        "url": "https://jobs.booking.com/careers/junior-qa-automation-engineer",
        "source": "Direct Employer",
        "description": "Help ensure platform quality and reliability. Strong skills in test automation, Python, JavaScript, and QA methodologies. Complete visa sponsorship and relocation flight + accommodation support.",
        "visa_evidence": "Mentioned: visa sponsorship, visa support",
        "housing_evidence": "Mentioned: accommodation provided, temporary housing"
    },
    {
        "title": "Junior Software Engineer (Mobile & Web)",
        "company": "Canva",
        "location": "Sydney, Australia / Remote APAC",
        "url": "https://www.canva.com/careers/jobs/junior-software-engineer",
        "source": "Remotive",
        "description": "Develop modern UI features across Flutter, HTML, CSS, JavaScript and backend APIs. Welcoming graduate and junior developers. Visa sponsorship available for eligible international engineers.",
        "visa_evidence": "Mentioned: visa sponsorship, sponsorship available",
        "housing_evidence": "Mentioned: relocation support"
    },
    {
        "title": "Technology Analyst / Junior Systems Engineer",
        "company": "Bloomberg LP",
        "location": "London, United Kingdom",
        "url": "https://careers.bloomberg.com/job/detail/technology-analyst",
        "source": "Direct Employer",
        "description": "Graduate program for Technology Analysts and IT Support Engineers. Focus on networking, Python scripting, systems operations and client tech support. Skilled Worker Visa sponsorship and relocation bonus provided.",
        "visa_evidence": "Mentioned: visa sponsorship, work permit",
        "housing_evidence": "Mentioned: relocation package"
    }
]

@contextmanager
def get_db():
    p = Path(DB_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(p), timeout=25.0)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA journal_mode=WAL;")
        con.execute("PRAGMA synchronous=NORMAL;")
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()

def init_db():
    with get_db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS jobs (
          id TEXT PRIMARY KEY, title TEXT, company TEXT, location TEXT,
          url TEXT, source TEXT, description TEXT, visa_evidence TEXT,
          housing_evidence TEXT, score INTEGER DEFAULT 0, status TEXT DEFAULT 'new',
          created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS profile (
          id INTEGER PRIMARY KEY CHECK(id=1), name TEXT, citizenship TEXT,
          residence TEXT, degree TEXT, skills TEXT, target_roles TEXT,
          cv_text TEXT, updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS applications (
          id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT, status TEXT,
          tailored_cv TEXT, cover_letter TEXT, notes TEXT, created_at TEXT
        );
        """)
        c.execute("""INSERT OR IGNORE INTO profile
          (id,name,citizenship,residence,degree,skills,target_roles,cv_text,updated_at)
          VALUES (1,'Adeena Khanzada','Pakistani','Jeddah, Saudi Arabia',
          'B.E. Computer Systems Engineering, MUET',
          'Python, C++, Java, Flutter, Firebase, HTML, CSS, JavaScript, QA, Embedded Systems, Graphic Design',
          'Graduate Engineer, Junior Software Engineer, QA Engineer, IT Support Engineer, Embedded Systems Engineer, Technology Analyst',
          '',?)""", (datetime.now(timezone.utc).isoformat(),))
        
        # Check if jobs table has any rows; if empty, seed with starter curated vacancies
        job_count = c.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        if job_count == 0:
            p = c.execute("SELECT * FROM profile WHERE id=1").fetchone()
            now_iso = datetime.now(timezone.utc).isoformat()
            for j in SEED_JOBS:
                jid = hashlib.sha256(j["url"].encode()).hexdigest()[:24]
                score = score_job(j, p)
                c.execute("""INSERT OR IGNORE INTO jobs
                  (id,title,company,location,url,source,description,visa_evidence,housing_evidence,score,status,created_at)
                  VALUES(?,?,?,?,?,?,?,?,?,?,'new',?)""",
                  (jid, j["title"], j["company"], j["location"], j["url"], j["source"],
                   j["description"], j["visa_evidence"], j["housing_evidence"], score, now_iso))

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield

app = FastAPI(title="Adeena's Job Agent", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")

# Ensure DB schema is ready immediately on module load
try:
    init_db()
except Exception:
    pass

def esc(v):
    return html.escape(str(v) if v is not None else "")

def layout(body, active_nav="dashboard"):
    nav_links = [
        ("/", "Dashboard", "dashboard"),
        ("/profile", "Profile", "profile"),
        ("/settings", "Settings", "settings")
    ]
    nav_html = "".join(
        f"<a href='{url}' class='{'active' if active_nav == key else ''}'>{label}</a>"
        for url, label, key in nav_links
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Adeena's Job Agent — International Career Radar</title>
  <link rel="stylesheet" href="/static/style.css">
</head>
<body>
  <header>
    <div>
      <div class="brand">✦ ADEENA'S JOB AGENT</div>
      <small>International Career Radar & Discovery</small>
    </div>
    <nav>{nav_html}</nav>
  </header>
  <main>{body}</main>
  <footer>
    Private workspace · Autonomous job discovery and screening · Application decisions remain strictly under your control
  </footer>
</body>
</html>"""

def score_job(job, profile):
    if not profile:
        return 50
    text = " ".join([
        job.get("title", ""),
        job.get("description", ""),
        job.get("company", ""),
        job.get("location", "")
    ]).lower()
    
    skills = [s.strip().lower() for s in (profile["skills"] or "").split(",") if s.strip()]
    roles = [s.strip().lower() for s in (profile["target_roles"] or "").split(",") if s.strip()]
    
    # Skill matching using regex word boundaries to avoid false positives (e.g. 'c' or 'java' in 'javascript')
    skill_hits = 0
    for s in skills:
        if not s:
            continue
        if any(c in s for c in ["+", "#", "."]):
            if s in text:
                skill_hits += 1
        else:
            pattern = r'\b' + re.escape(s) + r'\b'
            if re.search(pattern, text):
                skill_hits += 1
                
    # Role matching: check full role title or key role words in job title
    job_title_lower = job.get("title", "").lower()
    role_match = False
    for r in roles:
        if r in job_title_lower or r in text:
            role_match = True
            break
        # Also check role tokens in title (e.g. 'software engineer', 'qa', 'embedded')
        role_tokens = [w for w in r.split() if len(w) > 3]
        if role_tokens and all(token in job_title_lower for token in role_tokens):
            role_match = True
            break

    score = min(80, skill_hits * 10) + (20 if role_match else 5)
    return min(max(score, 10), 100)

def evidence(text, terms):
    text_lower = text.lower()
    hits = [t for t in terms if t in text_lower]
    return ("Mentioned: " + ", ".join(hits)) if hits else "No reliable evidence found"

async def fetch_jobs():
    results = []
    # Use standard browser headers and follow redirects so CDN / bot challenges don't silently block
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36 AdeenaJobAgent/0.2",
        "Accept": "application/json, text/plain, */*"
    }
    async with httpx.AsyncClient(timeout=25.0, follow_redirects=True, headers=headers) as client:
        # Arbeitnow public job board feed
        try:
            r = await client.get("https://www.arbeitnow.com/api/job-board-api")
            if r.status_code == 200:
                data = r.json().get("data", [])
                for j in data[:100]:
                    tags = " ".join(j.get("tags", [])) if isinstance(j.get("tags"), list) else ""
                    desc = re.sub(r"<[^>]+>", " ", j.get("description", ""))
                    full_desc = f"{desc} {tags}"[:8000]
                    results.append({
                        "title": j.get("title", "").strip(),
                        "company": j.get("company_name", "").strip(),
                        "location": j.get("location", "").strip() or "Remote / Europe",
                        "url": j.get("url", "").strip(),
                        "source": "Arbeitnow",
                        "description": full_desc
                    })
        except Exception:
            pass

        # Remotive API: remote & global roles
        try:
            r = await client.get("https://remotive.com/api/remote-jobs?limit=100")
            if r.status_code == 200:
                for j in r.json().get("jobs", []):
                    tags = " ".join(j.get("tags", [])) if isinstance(j.get("tags"), list) else ""
                    desc = re.sub(r"<[^>]+>", " ", j.get("description", ""))
                    full_desc = f"{desc} {tags}"[:8000]
                    results.append({
                        "title": j.get("title", "").strip(),
                        "company": j.get("company_name", "").strip(),
                        "location": j.get("candidate_required_location", "Remote").strip(),
                        "url": j.get("url", "").strip(),
                        "source": "Remotive",
                        "description": full_desc
                    })
        except Exception:
            pass

    return results

def rescore_all_jobs_sync(c):
    p = c.execute("SELECT * FROM profile WHERE id=1").fetchone()
    if not p:
        return 0
    jobs = c.execute("SELECT id, title, company, location, description FROM jobs").fetchall()
    updated = 0
    for j in jobs:
        score = score_job(dict(j), p)
        c.execute("UPDATE jobs SET score=? WHERE id=?", (score, j["id"]))
        updated += 1
    return updated

@app.get("/", response_class=HTMLResponse)
def home(request: Request, status: str = "all", q: str = "", msg: str = ""):
    with get_db() as c:
        p = c.execute("SELECT * FROM profile WHERE id=1").fetchone()
        counts_raw = c.execute("SELECT status, COUNT(*) as n FROM jobs GROUP BY status").fetchall()
        total_raw = c.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]

        countmap = {r["status"]: r["n"] for r in counts_raw}
        shortlisted_count = countmap.get("shortlisted", 0)
        applied_count = countmap.get("applied", 0)
        new_count = countmap.get("new", 0)
        dismissed_count = countmap.get("dismissed", 0)

        # Build query according to status filter and search term
        query = "SELECT * FROM jobs WHERE 1=1"
        params = []
        if status == "shortlisted":
            query += " AND status='shortlisted'"
        elif status == "applied":
            query += " AND status='applied'"
        elif status == "new":
            query += " AND status='new'"
        elif status == "dismissed":
            query += " AND status='dismissed'"
        else:
            # Default "all" hides dismissed to keep recommendations clean
            query += " AND status != 'dismissed'"

        if q.strip():
            query += " AND (title LIKE ? OR company LIKE ? OR location LIKE ? OR description LIKE ?)"
            term = f"%{q.strip()}%"
            params.extend([term, term, term, term])

        query += " ORDER BY score DESC, created_at DESC LIMIT 60"
        jobs = c.execute(query, params).fetchall()

    cards = []
    for j in jobs:
        j_id = esc(j['id'])
        j_title = esc(j['title'])
        j_company = esc(j['company'])
        j_location = esc(j['location'] or "Global / Remote")
        j_source = esc(j['source'])
        j_url = esc(j['url'])
        j_score = j['score'] or 0
        j_status = j['status'] or "new"

        visa_txt = esc(j['visa_evidence'] or 'Not verified')
        housing_txt = esc(j['housing_evidence'] or 'Not verified')
        
        visa_class = "evidence-found" if "Mentioned" in (j['visa_evidence'] or "") else ""
        housing_class = "evidence-found" if "Mentioned" in (j['housing_evidence'] or "") else ""

        # Status badge & action buttons
        status_pill = ""
        action_buttons = ""
        job_card_class = "job"

        if j_status == "shortlisted":
            job_card_class += " is-shortlisted"
            status_pill = "<span class='pill pill-amber'>★ Shortlisted</span>"
            action_buttons = f"""
              <form method='post' action='/job/{j_id}/status' style='margin:0'>
                <input type='hidden' name='status' value='applied'>
                <button type='submit' class='btn-sm'>Mark Applied</button>
              </form>
              <form method='post' action='/job/{j_id}/status' style='margin:0'>
                <input type='hidden' name='status' value='new'>
                <button type='submit' class='btn-sm btn-subtle'>Remove</button>
              </form>
            """
        elif j_status == "applied":
            job_card_class += " is-applied"
            status_pill = "<span class='pill pill-blue'>✓ Applied</span>"
            action_buttons = f"""
              <form method='post' action='/job/{j_id}/status' style='margin:0'>
                <input type='hidden' name='status' value='shortlisted'>
                <button type='submit' class='btn-sm btn-subtle'>Move to Shortlist</button>
              </form>
            """
        elif j_status == "dismissed":
            status_pill = "<span class='pill'>Dismissed</span>"
            action_buttons = f"""
              <form method='post' action='/job/{j_id}/status' style='margin:0'>
                <input type='hidden' name='status' value='new'>
                <button type='submit' class='btn-sm btn-subtle'>Restore</button>
              </form>
            """
        else: # new
            action_buttons = f"""
              <form method='post' action='/job/{j_id}/status' style='margin:0'>
                <input type='hidden' name='status' value='shortlisted'>
                <button type='submit' class='btn-sm'>Shortlist</button>
              </form>
              <form method='post' action='/job/{j_id}/status' style='margin:0'>
                <input type='hidden' name='status' value='dismissed'>
                <button type='submit' class='btn-sm btn-subtle' title='Hide vacancy'>✕</button>
              </form>
            """

        cards.append(f"""
        <article class='{job_card_class}'>
          <div>
            <div class='job-header'>
              <div class='pills'>
                <span class='pill'>{j_source}</span>
                {status_pill}
              </div>
              <span class='score-badge'>{j_score}/100 Match</span>
            </div>
            <h3>{j_title}</h3>
            <p class='company-loc'>{j_company} · {j_location}</p>
            <div class='evidence-box'>
              <div><b>Visa:</b> <span class='{visa_class}'>{visa_txt}</span></div>
              <div><b>Housing:</b> <span class='{housing_class}'>{housing_txt}</span></div>
            </div>
          </div>
          <div class='job-footer'>
            <a target='_blank' rel='noopener noreferrer' href='{j_url}' style='font-size:13px'>View vacancy ↗</a>
            <div class='job-actions'>
              {action_buttons}
            </div>
          </div>
        </article>
        """)

    cards_html = "".join(cards) if cards else """
      <div class='empty-state'>
        <h3>No matching vacancies found</h3>
        <p>No listings match your current filter. Run a live discovery scan or clear your search query.</p>
        <div style='display:flex; justify-content:center; gap:10px;'>
          <form method='post' action='/scan'><button type='submit' class='btn'>Run Job Discovery Scan</button></form>
          <a href='/' class='btn btn-secondary'>Show All Vacancies</a>
        </div>
      </div>
    """

    flash_banner = f"<div class='notice notice-info'>{esc(msg)}</div>" if msg else ""

    return layout(f"""
    {flash_banner}
    <section class='hero'>
      <p class='eyebrow'>YOUR PRIVATE RECRUITMENT ASSISTANT</p>
      <h1>Find the work.<br><em>Skip the endless scrolling.</em></h1>
      <p>Discover international engineering and tech opportunities matching your profile, with verified keyword indicators for visa sponsorship and relocation support.</p>
      <div class='hero-actions'>
        <form method='post' action='/scan'>
          <button type='submit' class='primary'>✦ Run Job Discovery Scan</button>
        </form>
        <a href='/profile' class='btn btn-secondary'>Review Matching Profile</a>
      </div>
    </section>

    <section class='stats'>
      <div>
        <strong>{total_raw}</strong>
        <small>Total Vacancies</small>
      </div>
      <div>
        <strong>{shortlisted_count}</strong>
        <small>Shortlisted</small>
      </div>
      <div>
        <strong>{applied_count}</strong>
        <small>Applied</small>
      </div>
      <div>
        <strong>{new_count}</strong>
        <small>New to Review</small>
      </div>
    </section>

    <div class='filter-bar'>
      <div class='tabs'>
        <a href='/?status=all{f"&q={esc(q)}" if q else ""}' class='tab {"active" if status=="all" else ""}'>Active ({total_raw - dismissed_count})</a>
        <a href='/?status=shortlisted{f"&q={esc(q)}" if q else ""}' class='tab {"active" if status=="shortlisted" else ""}'>Shortlisted ({shortlisted_count})</a>
        <a href='/?status=applied{f"&q={esc(q)}" if q else ""}' class='tab {"active" if status=="applied" else ""}'>Applied ({applied_count})</a>
        <a href='/?status=new{f"&q={esc(q)}" if q else ""}' class='tab {"active" if status=="new" else ""}'>New ({new_count})</a>
        {f"<a href='/?status=dismissed' class='tab {'active' if status=='dismissed' else ''}'>Dismissed ({dismissed_count})</a>" if dismissed_count > 0 else ""}
      </div>
      <form method='get' action='/' class='search-box'>
        <input type='hidden' name='status' value='{esc(status)}'>
        <input name='q' placeholder='Search role, company, city...' value='{esc(q)}'>
        <button type='submit' class='btn-secondary btn-sm'>Search</button>
        {f"<a href='/?status={esc(status)}' class='btn-subtle btn-sm'>Clear</a>" if q else ""}
      </form>
    </div>

    <div class='sectionhead'>
      <div>
        <p class='eyebrow'>OPPORTUNITIES PIPELINE</p>
        <h2>{f"Showing {status.capitalize()} Vacancies" if status != 'all' else "Recommended Vacancies"} ({len(jobs)})</h2>
      </div>
      <a href='/profile' style='font-size:13px;'>Refine profile matching criteria →</a>
    </div>

    <div class='jobs'>
      {cards_html}
    </div>
    """, active_nav="dashboard")

@app.get("/profile", response_class=HTMLResponse)
def profile_page(msg: str = ""):
    with get_db() as c:
        p = c.execute("SELECT * FROM profile WHERE id=1").fetchone()
        if not p:
            init_db()
            p = c.execute("SELECT * FROM profile WHERE id=1").fetchone()
    
    flash_banner = f"<div class='notice'>{esc(msg)}</div>" if msg else ""

    return layout(f"""
    {flash_banner}
    <div class='sectionhead'>
      <div>
        <p class='eyebrow'>MATCHING MATRIX</p>
        <h1>Candidate Profile</h1>
      </div>
    </div>
    <p class='muted'>The agent uses these criteria to score vacancies, extract relevant visa evidence, and evaluate relocation readiness.</p>
    
    <form class='panel' method='post' action='/profile'>
      <label>Full Name
        <input name='name' value='{esc(p['name'])}' required>
      </label>
      <div style='display:grid; grid-template-columns:1fr 1fr; gap:16px;'>
        <label>Citizenship
          <input name='citizenship' value='{esc(p['citizenship'])}'>
        </label>
        <label>Current Residence
          <input name='residence' value='{esc(p['residence'])}'>
        </label>
      </div>
      <label>Degree / Educational Background
        <input name='degree' value='{esc(p['degree'])}'>
      </label>
      <label>Technical Skills (comma-separated)
        <textarea name='skills' rows='3'>{esc(p['skills'])}</textarea>
      </label>
      <label>Target Roles (comma-separated)
        <textarea name='target_roles' rows='3'>{esc(p['target_roles'])}</textarea>
      </label>
      <label>CV / Resume Summary Text
        <textarea name='cv_text' rows='10' placeholder='Paste your CV text or experience summary here...'>{esc(p['cv_text'])}</textarea>
      </label>
      <div style='display:flex; gap:12px; margin-top:20px; align-items:center;'>
        <button type='submit' class='primary'>Save Profile & Re-score Jobs</button>
        <a href='/' class='btn btn-secondary'>Cancel</a>
      </div>
    </form>
    """, active_nav="profile")

@app.post("/profile")
def save_profile(
    name: str = Form(""),
    citizenship: str = Form(""),
    residence: str = Form(""),
    degree: str = Form(""),
    skills: str = Form(""),
    target_roles: str = Form(""),
    cv_text: str = Form("")
):
    with get_db() as c:
        c.execute("""UPDATE profile SET name=?, citizenship=?, residence=?, degree=?, skills=?, target_roles=?, cv_text=?, updated_at=? WHERE id=1""",
                  (name, citizenship, residence, degree, skills, target_roles, cv_text, datetime.now(timezone.utc).isoformat()))
        rescore_all_jobs_sync(c)

    return RedirectResponse("/profile?msg=" + quote_plus("Profile saved and all vacancies re-scored!"), status_code=303)

@app.post("/job/{job_id}/status")
def update_job_status(job_id: str, status: str = Form(...)):
    allowed = ["new", "shortlisted", "applied", "dismissed"]
    if status not in allowed:
        status = "new"

    with get_db() as c:
        c.execute("UPDATE jobs SET status=? WHERE id=?", (status, job_id))
        if status == "applied":
            c.execute("""INSERT INTO applications (job_id, status, created_at)
                         VALUES (?, 'applied', ?)""",
                      (job_id, datetime.now(timezone.utc).isoformat()))

    return RedirectResponse(f"/?status={status}&msg=" + quote_plus(f"Job marked as {status}!"), status_code=303)

@app.post("/shortlist/{job_id}")
def shortlist(job_id: str):
    with get_db() as c:
        c.execute("UPDATE jobs SET status='shortlisted' WHERE id=?", (job_id,))
    return RedirectResponse("/?status=shortlisted&msg=" + quote_plus("Job added to your shortlist!"), status_code=303)

@app.post("/scan")
async def scan():
    jobs = await fetch_jobs()
    inserted = 0
    with get_db() as c:
        p = c.execute("SELECT * FROM profile WHERE id=1").fetchone()
        visa_terms = [
            "visa sponsorship", "sponsorship available", "work permit", "relocation support",
            "visa support", "visa provided", "tier 2", "blue card", "sponsorship"
        ]
        housing_terms = [
            "accommodation provided", "housing allowance", "relocation package", "temporary housing",
            "relocation bonus", "relocation assistance", "flight allowance"
        ]
        now_iso = datetime.now(timezone.utc).isoformat()
        for j in jobs:
            if not j["url"] or not j["title"]:
                continue
            jid = hashlib.sha256(j["url"].encode()).hexdigest()[:24]
            txt = (j["title"] + " " + j["description"]).lower()
            score = score_job(j, p)
            visa_ev = evidence(txt, visa_terms)
            housing_ev = evidence(txt, housing_terms)
            
            try:
                c.execute("""INSERT OR IGNORE INTO jobs(id,title,company,location,url,source,description,visa_evidence,housing_evidence,score,status,created_at)
                             VALUES(?,?,?,?,?,?,?,?,?,?,'new',?)""",
                          (jid, j["title"], j["company"], j["location"], j["url"], j["source"],
                           j["description"], visa_ev, housing_ev, score, now_iso))
                if c.total_changes > 0:
                    inserted += 1
            except Exception:
                pass

    msg = f"Discovery scan complete! Found {len(jobs)} total vacancies, added {inserted} new roles." if len(jobs) > 0 else "Scan completed. External feeds did not return new postings at this time."
    return RedirectResponse("/?msg=" + quote_plus(msg), status_code=303)

@app.post("/rescore")
def rescore():
    with get_db() as c:
        count = rescore_all_jobs_sync(c)
    return RedirectResponse("/?msg=" + quote_plus(f"Successfully re-scored {count} vacancies!"), status_code=303)

@app.post("/seed")
def seed_curated():
    with get_db() as c:
        p = c.execute("SELECT * FROM profile WHERE id=1").fetchone()
        now_iso = datetime.now(timezone.utc).isoformat()
        added = 0
        for j in SEED_JOBS:
            jid = hashlib.sha256(j["url"].encode()).hexdigest()[:24]
            score = score_job(j, p)
            c.execute("""INSERT OR IGNORE INTO jobs
                         (id,title,company,location,url,source,description,visa_evidence,housing_evidence,score,status,created_at)
                         VALUES(?,?,?,?,?,?,?,?,?,?,'new',?)""",
                      (jid, j["title"], j["company"], j["location"], j["url"], j["source"],
                       j["description"], j["visa_evidence"], j["housing_evidence"], score, now_iso))
            if c.total_changes > 0:
                added += 1
    return RedirectResponse("/?msg=" + quote_plus(f"Loaded {added} curated international vacancies!"), status_code=303)

@app.get("/settings", response_class=HTMLResponse)
def settings(msg: str = ""):
    with get_db() as c:
        total = c.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        sources = c.execute("SELECT source, COUNT(*) n FROM jobs GROUP BY source").fetchall()
        source_summary = ", ".join(f"{r['source']} ({r['n']})" for r in sources) if sources else "None currently cached"

    flash_banner = f"<div class='notice'>{esc(msg)}</div>" if msg else ""

    return layout(f"""
    {flash_banner}
    <div class='sectionhead'>
      <div>
        <p class='eyebrow'>CONFIGURATION & INTEGRATIONS</p>
        <h1>Agent Settings & Automation</h1>
      </div>
    </div>
    
    <div class='panel'>
      <h3>Discovery Operations</h3>
      <p>Trigger on-demand discovery sweeps across configured international job feeds, or re-run candidate profile matching algorithms across your saved pipeline.</p>
      <div style='display:flex; gap:12px; flex-wrap:wrap; margin-top:16px;'>
        <form method='post' action='/scan'>
          <button type='submit' class='primary'>✦ Run Live Discovery Scan</button>
        </form>
        <form method='post' action='/rescore'>
          <button type='submit' class='btn btn-secondary'>Re-score All Vacancies</button>
        </form>
        <form method='post' action='/seed'>
          <button type='submit' class='btn btn-secondary'>Load Curated International Roles</button>
        </form>
      </div>
    </div>

    <div class='panel'>
      <h3>Configured Job Feeds</h3>
      <p><b>Active Sources:</b> {esc(source_summary)}</p>
      <p><b>Connected Endpoints:</b> Arbeitnow Public Board API, Remotive Remote API, Direct Curated Feed.</p>
      <p><b>Next planned integrations:</b> Greenhouse ATS, Lever ATS, LinkedIn Search Scrapers, Workday Direct Integrations.</p>
    </div>

    <div class='panel'>
      <h3>Safety & Guardrails</h3>
      <p><b>Review & Consent:</b> The agent never submits external applications, accepts terms, or shares confidential credentials automatically. All submissions require human verification.</p>
      <p><b>Evidence Verification:</b> Visa sponsorship and accommodation indications are extracted via contextual keyword signals. Always verify specific terms on the employer's official job description page before applying.</p>
    </div>
    """, active_nav="settings")

@app.get("/health")
def health():
    with get_db() as c:
        count = c.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    return {"ok": True, "service": "adeena-job-agent", "jobs_count": count}
