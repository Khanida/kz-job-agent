from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path
import sqlite3, os, json, re, hashlib
from datetime import datetime, timezone
from urllib.parse import urlparse
import httpx

APP_DIR = Path(__file__).parent
DB_PATH = os.getenv("DATABASE_PATH", str(APP_DIR.parent / "jobs.db"))
app = FastAPI(title="Adeena's Job Agent")
app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")

def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    with db() as c:
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
          'Python,C++,Java,Flutter,Firebase,HTML,CSS,JavaScript,graphic design',
          'Graduate Engineer, Junior Software Engineer, QA Engineer, IT Support Engineer, Embedded Systems Engineer, Technology Analyst',
          '',?)""", (datetime.now(timezone.utc).isoformat(),))

@app.on_event("startup")
def startup():
    init_db()

def esc(v):
    return (v or "").replace("&","&amp;").replace("<","&lt;").replace(">","&gt;").replace('"',"&quot;")

def layout(body):
    return f"""<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
    <title>Adeena's Job Agent</title><link rel='stylesheet' href='/static/style.css'></head>
    <body><header><div><b>✦ ADEENA'S JOB AGENT</b><small>International career radar</small></div>
    <nav><a href='/'>Dashboard</a><a href='/profile'>Profile</a><a href='/settings'>Settings</a></nav></header>
    <main>{body}</main><footer>Private workspace · Job discovery and screening · Applications remain under your control</footer></body></html>"""

@app.get("/", response_class=HTMLResponse)
def home():
    with db() as c:
        jobs = c.execute("SELECT * FROM jobs ORDER BY score DESC, created_at DESC LIMIT 50").fetchall()
        counts = c.execute("SELECT status,COUNT(*) n FROM jobs GROUP BY status").fetchall()
        profile = c.execute("SELECT * FROM profile WHERE id=1").fetchone()
    countmap = {r["status"]:r["n"] for r in counts}
    cards = "".join(f"""<article class='job'><div class='row'><span class='pill'>{esc(j['source'])}</span><b class='score'>{j['score']}/100 match</b></div>
      <h3>{esc(j['title'])}</h3><p>{esc(j['company'])} · {esc(j['location'])}</p>
      <p class='evidence'><b>Visa:</b> {esc(j['visa_evidence'] or 'Not verified')}<br><b>Housing:</b> {esc(j['housing_evidence'] or 'Not verified')}</p>
      <div class='row'><a target='_blank' rel='noopener' href='{esc(j['url'])}'>View vacancy ↗</a><form method='post' action='/shortlist/{esc(j['id'])}'><button>Shortlist</button></form></div></article>""" for j in jobs)
    if not cards: cards = "<p class='muted'>No vacancies yet. Run your first discovery scan from Settings.</p>"
    return layout(f"""<section class='hero'><p class='eyebrow'>YOUR PRIVATE RECRUITMENT ASSISTANT</p><h1>Find the work.<br><em>Skip the endless scrolling.</em></h1>
    <p>Discover international roles, compare them with your profile, and surface sponsorship and relocation evidence.</p>
    <form method='post' action='/scan'><button class='primary'>Run job discovery</button></form></section>
    <section class='stats'><div><strong>{len(jobs)}</strong><small>Recent matches</small></div><div><strong>{countmap.get('shortlisted',0)}</strong><small>Shortlisted</small></div><div><strong>{countmap.get('applied',0)}</strong><small>Applied</small></div></section>
    <div class='sectionhead'><div><p class='eyebrow'>LIVE PIPELINE</p><h2>Recommended vacancies</h2></div><a href='/profile'>Edit matching profile →</a></div><div class='jobs'>{cards}</div>""")

@app.get("/profile", response_class=HTMLResponse)
def profile_page():
    with db() as c: p=c.execute("SELECT * FROM profile WHERE id=1").fetchone()
    return layout(f"""<h1>Your candidate profile</h1><p class='muted'>The agent uses these fields to score vacancies. Paste CV text or upload/extract it in a later setup step.</p>
    <form class='panel' method='post' action='/profile'>
    <label>Name<input name='name' value='{esc(p['name'])}'></label>
    <label>Citizenship<input name='citizenship' value='{esc(p['citizenship'])}'></label>
    <label>Current residence<input name='residence' value='{esc(p['residence'])}'></label>
    <label>Degree<input name='degree' value='{esc(p['degree'])}'></label>
    <label>Skills (comma separated)<textarea name='skills'>{esc(p['skills'])}</textarea></label>
    <label>Target roles (comma separated)<textarea name='target_roles'>{esc(p['target_roles'])}</textarea></label>
    <label>CV text<textarea name='cv_text' rows='12'>{esc(p['cv_text'])}</textarea></label><button class='primary'>Save profile</button></form>""")

@app.post("/profile")
def save_profile(name:str=Form(...), citizenship:str=Form(...), residence:str=Form(...), degree:str=Form(...), skills:str=Form(...), target_roles:str=Form(...), cv_text:str=Form("")):
    with db() as c:
        c.execute("""UPDATE profile SET name=?,citizenship=?,residence=?,degree=?,skills=?,target_roles=?,cv_text=?,updated_at=? WHERE id=1""",
        (name,citizenship,residence,degree,skills,target_roles,cv_text,datetime.now(timezone.utc).isoformat()))
    return RedirectResponse("/profile",status_code=303)

def score_job(job, profile):
    text = " ".join([job.get("title",""),job.get("description",""),job.get("company","")]).lower()
    skills = [s.strip().lower() for s in (profile["skills"] or "").split(",") if s.strip()]
    roles = [s.strip().lower() for s in (profile["target_roles"] or "").split(",") if s.strip()]
    matched = sum(1 for s in skills if s in text)
    role_match = any(r in text for r in roles)
    score = min(85, matched * 9) + (15 if role_match else 0)
    return min(score,100)

def evidence(text, terms):
    hits = [t for t in terms if t in text.lower()]
    return ("Mentioned: " + ", ".join(hits)) if hits else "No reliable evidence found"

async def fetch_jobs():
    results=[]
    async with httpx.AsyncClient(timeout=20, headers={"User-Agent":"AdeenaJobAgent/0.1"}) as client:
        # Arbeitnow public job board feed; treat eligibility/visa as unverified unless explicitly stated.
        try:
            r=await client.get("https://www.arbeitnow.com/api/job-board-api")
            r.raise_for_status()
            data=r.json().get("data",[])
            for j in data[:100]:
                results.append({"title":j.get("title",""),"company":j.get("company_name",""),
                  "location":j.get("location",""),"url":j.get("url",""),"source":"Arbeitnow",
                  "description":re.sub("<[^>]+>"," ",j.get("description",""))[:8000]})
        except Exception:
            pass
        # Remotive API: remote roles only, useful as a supplementary source.
        try:
            r=await client.get("https://remotive.com/api/remote-jobs?limit=100")
            r.raise_for_status()
            for j in r.json().get("jobs",[]):
                results.append({"title":j.get("title",""),"company":j.get("company_name",""),
                  "location":j.get("candidate_required_location","Remote"),"url":j.get("url",""),
                  "source":"Remotive","description":re.sub("<[^>]+>"," ",j.get("description",""))[:8000]})
        except Exception:
            pass
    return results

@app.post("/scan")
async def scan():
    jobs=await fetch_jobs()
    with db() as c:
        p=c.execute("SELECT * FROM profile WHERE id=1").fetchone()
        inserted=0
        for j in jobs:
            if not j["url"] or not j["title"]: continue
            jid=hashlib.sha256(j["url"].encode()).hexdigest()[:24]
            txt=(j["title"]+" "+j["description"]).lower()
            j["score"]=score_job(j,p)
            j["visa_evidence"]=evidence(txt,["visa sponsorship","sponsorship available","work permit","relocation support","visa support"])
            j["housing_evidence"]=evidence(txt,["accommodation provided","housing allowance","relocation package","temporary housing"])
            try:
                c.execute("""INSERT OR IGNORE INTO jobs(id,title,company,location,url,source,description,visa_evidence,housing_evidence,score,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)""",(jid,j["title"],j["company"],j["location"],j["url"],j["source"],j["description"],j["visa_evidence"],j["housing_evidence"],j["score"],datetime.now(timezone.utc).isoformat()))
                inserted+=c.total_changes>0
            except Exception: pass
    return RedirectResponse("/",status_code=303)

@app.post("/shortlist/{job_id}")
def shortlist(job_id:str):
    with db() as c: c.execute("UPDATE jobs SET status='shortlisted' WHERE id=?",(job_id,))
    return RedirectResponse("/",status_code=303)

@app.get("/settings", response_class=HTMLResponse)
def settings():
    return layout("""<h1>Agent settings</h1><div class='panel'><h3>Current discovery sources</h3><p>Arbeitnow public feed and Remotive remote jobs API.</p>
    <p><b>Next integrations:</b> employer career pages (Greenhouse/Lever), additional job boards, verified visa and relocation research, email notifications and scheduled scans.</p>
    <h3>Automation boundaries</h3><p>The starter does not submit applications, create accounts, accept legal declarations or send personal data to employers. Those actions require an explicit review/approval workflow.</p>
    <p>Visa and accommodation labels are keyword evidence only, not a guarantee. Every claim must be checked against the employer's official vacancy page.</p></div>""")

@app.get("/health")
def health():
    return {"ok":True,"service":"adeena-job-agent"}
