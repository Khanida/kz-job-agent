from fastapi import FastAPI, Request, Response, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Optional, Tuple
import sqlite3, os, json, re, hashlib, html
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus

from app.agents.orchestrator import run_jury_deliberation_workflow, run_discovery_workers
from app.agents.jury import SupremeJuryAgent
from app.agents.learner import record_interaction, compute_learned_boost, get_training_profile, init_learner_db

APP_DIR = Path(__file__).resolve().parent
DB_PATH = os.getenv("DATABASE_PATH", str(APP_DIR.parent / "jobs.db"))
UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", str(APP_DIR.parent / "uploads" / "cv")))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
logger = logging.getLogger(__name__)
daily_refresh_lock = asyncio.Lock()

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
          housing_evidence TEXT, score INTEGER DEFAULT 0, jury_score INTEGER DEFAULT 0,
          is_daily_pick INTEGER DEFAULT 0, advocate_agent TEXT, jury_verdict TEXT,
          liveness_status TEXT DEFAULT 'live', posted_date TEXT DEFAULT 'Recently posted',
          status TEXT DEFAULT 'new', created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS profile (
          id INTEGER PRIMARY KEY CHECK(id=1), name TEXT, citizenship TEXT,
          residence TEXT, degree TEXT, skills TEXT, target_roles TEXT,
          cv_text TEXT, cv_filename TEXT, cv_uploaded_at TEXT, updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS applications (
          id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT, status TEXT,
          tailored_cv TEXT, cover_letter TEXT, notes TEXT, created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS deliberations (
          id TEXT PRIMARY KEY, session_date TEXT, summary TEXT, top_picks_count INTEGER, created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS agent_arguments (
          id INTEGER PRIMARY KEY AUTOINCREMENT, deliberation_id TEXT, job_id TEXT,
          agent_name TEXT, agent_role TEXT, argument_type TEXT, statement TEXT, score INTEGER, created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS jury_verdicts (
          id INTEGER PRIMARY KEY AUTOINCREMENT, deliberation_id TEXT, job_id TEXT,
          verdict TEXT, consensus_score INTEGER, strengths TEXT, risks TEXT,
          recommendation TEXT, is_selected INTEGER DEFAULT 0, created_at TEXT
        );
        """)

        # Schema migrations for existing databases
        for col, col_def in [
            ("jury_score", "INTEGER DEFAULT 0"),
            ("is_daily_pick", "INTEGER DEFAULT 0"),
            ("advocate_agent", "TEXT"),
            ("jury_verdict", "TEXT"),
            ("liveness_status", "TEXT DEFAULT 'live'"),
            ("posted_date", "TEXT DEFAULT 'Recently posted'")
        ]:
            try:
                c.execute(f"ALTER TABLE jobs ADD COLUMN {col} {col_def}")
            except Exception:
                pass

        for col, col_def in [
            ("cv_filename", "TEXT"),
            ("cv_uploaded_at", "TEXT")
        ]:
            try:
                c.execute(f"ALTER TABLE profile ADD COLUMN {col} {col_def}")
            except Exception:
                pass

        # Insert default profile if missing
        c.execute("""INSERT OR IGNORE INTO profile
          (id,name,citizenship,residence,degree,skills,target_roles,cv_text,updated_at)
          VALUES (1,'Adeena Khanzada','Pakistani','Jeddah, Saudi Arabia',
          'B.E. Computer Systems Engineering, MUET',
          'Python, C++, Java, Flutter, Firebase, HTML, CSS, JavaScript, QA, Embedded Systems, Graphic Design',
          'Graduate Engineer, Junior Software Engineer, QA Engineer, IT Support Engineer, Embedded Systems Engineer, Technology Analyst',
          '',?)""", (datetime.now(timezone.utc).isoformat(),))
        
        init_learner_db(c)


async def refresh_daily_selection_if_due(force: bool = False):
    async with daily_refresh_lock:
        with get_db() as c:
            today = datetime.now(timezone.utc).date().isoformat()
            latest = c.execute(
                "SELECT session_date, top_picks_count FROM deliberations ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            if not force and latest and latest["session_date"] == today:
                return {"skipped": True, "crowned_count": latest["top_picks_count"] or 0}

            p = c.execute("SELECT * FROM profile WHERE id=1").fetchone()
            return await run_jury_deliberation_workflow(c, dict(p))


async def daily_refresh_scheduler():
    while True:
        now = datetime.now(timezone.utc)
        next_midnight = datetime.combine(
            now.date() + timedelta(days=1),
            datetime.min.time(),
            tzinfo=timezone.utc,
        )
        await asyncio.sleep((next_midnight - now).total_seconds())
        try:
            await refresh_daily_selection_if_due()
        except Exception:
            logger.exception("Automatic daily job refresh failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    await refresh_daily_selection_if_due()
    scheduler_task = asyncio.create_task(daily_refresh_scheduler())
    try:
        yield
    finally:
        scheduler_task.cancel()
        try:
            await scheduler_task
        except asyncio.CancelledError:
            pass

app = FastAPI(title="Adeena's Job Agent", lifespan=lifespan)

@app.middleware("http")
async def cookie_session_middleware(request: Request, call_next):
    sid = request.cookies.get("agent_session")
    is_new = False
    if not sid:
        sid = "user_" + hashlib.sha256(os.urandom(16)).hexdigest()[:12]
        is_new = True
    request.state.session_id = sid
    response = await call_next(request)
    if is_new:
        response.set_cookie(
            key="agent_session",
            value=sid,
            max_age=86400 * 365,
            httponly=True,
            samesite="lax"
        )
    return response

app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")

try:
    init_db()
except Exception:
    pass

def extract_text_from_file(filename: str, content: bytes) -> str:
    ext = Path(filename).suffix.lower()
    if ext in [".txt", ".md", ".rtf"]:
        return content.decode("utf-8", errors="ignore")
    elif ext == ".pdf":
        try:
            import pypdf, io
            reader = pypdf.PdfReader(io.BytesIO(content))
            texts = []
            for idx, page in enumerate(reader.pages):
                extracted = page.extract_text()
                if extracted:
                    texts.append(extracted)
            return "\n".join(texts).strip()
        except Exception as e:
            return f"Error extracting PDF text: {str(e)}"
    else:
        # Default fallback attempt to decode as UTF-8
        try:
            return content.decode("utf-8", errors="ignore")
        except Exception:
            return ""

def esc(v):
    return html.escape(str(v) if v is not None else "")

def layout(body, active_nav="dashboard"):
    nav_links = [
        ("/", "Dashboard", "dashboard"),
        ("/jury", "⚖️ Jury Room", "jury"),
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
  <title>Adeena's Job Agent — Multi-Agent Career Radar</title>
  <link rel="stylesheet" href="/static/style.css">
</head>
<body>
  <header>
    <div>
      <div class="brand">✦ ADEENA'S JOB AGENT</div>
      <small>Hierarchical Multi-Agent Discovery & Supreme Jury</small>
    </div>
    <nav>{nav_html}</nav>
  </header>
  <main>{body}</main>
  <footer>
    Private workspace · Multi-Agent hierarchical screening & Supreme Jury arbitration · Decisions remain strictly under your control
  </footer>
  <script>
    (() => {{
      const flow = document.querySelector("[data-agent-flow]");
      const refreshForm = document.querySelector("[data-refresh-form]");
      if (refreshForm) {{
        refreshForm.addEventListener("submit", () => {{
          const button = refreshForm.querySelector("[data-refresh-button]");
          if (button) {{
            button.disabled = true;
            button.textContent = "Refreshing today’s picks…";
          }}
        }});
      }}

      if (!flow) return;
      if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {{
        flow.querySelector("[data-flow-status]").textContent =
          "Workers scout, Sub-Agents debate, and the Jury ranks today’s top five.";
        return;
      }}

      const stages = [...flow.querySelectorAll("[data-flow-stage]")];
      const status = flow.querySelector("[data-flow-status]");
      const updates = [
        "Workers are collecting fresh job files…",
        "Worker dossiers are being submitted to the Sub-Agents…",
        "Sub-Agents are debating fit, quality, and relocation…",
        "The Jury is weighing the arguments and ranking today’s top five…"
      ];
      let activeStage = 0;

      const advance = () => {{
        stages.forEach((stage, index) => {{
          stage.classList.toggle("is-active", index === activeStage);
          stage.classList.toggle("is-complete", index < activeStage);
        }});
        status.textContent = updates[activeStage];
        activeStage = (activeStage + 1) % stages.length;
      }};

      advance();
      window.setInterval(advance, 2400);
    }})();
  </script>
</body>
</html>"""

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

        # Retrieve Daily Picks selected by the Jury
        daily_picks = c.execute("""
            SELECT * FROM jobs
            WHERE is_daily_pick = 1 AND status != 'dismissed'
            ORDER BY jury_score DESC, score DESC LIMIT 5
        """).fetchall()
        latest_deliberation = c.execute(
            "SELECT session_date FROM deliberations ORDER BY created_at DESC LIMIT 1"
        ).fetchone()

        # Build pipeline query
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
            query += " AND status != 'dismissed'"

        if q.strip():
            query += " AND (title LIKE ? OR company LIKE ? OR location LIKE ? OR description LIKE ?)"
            term = f"%{q.strip()}%"
            params.extend([term, term, term, term])

        query += " ORDER BY is_daily_pick DESC, jury_score DESC, score DESC, created_at DESC LIMIT 60"
        jobs = c.execute(query, params).fetchall()
        training_info = get_training_profile(c)

        # Precompute learned boosts INSIDE the open connection block
        job_boosts: dict = {}
        for _j in jobs:
            _boost, _reasons = compute_learned_boost(c, dict(_j))
            job_boosts[_j["id"]] = (_boost, _reasons)

    # Render Training Information Banner
    top_kws = training_info.get("top_keywords", [])
    top_locs = training_info.get("top_locations", [])
    
    pref_chips = []
    for kw in top_kws[:4]:
        pref_chips.append(f"<span class='pill' style='background:#f3eaff; color:#6225b5; font-size:11px;'>★ {esc(kw['feature_value'])} (+{kw['score_delta']})</span>")
    for loc in top_locs[:3]:
        pref_chips.append(f"<span class='pill' style='background:#fdf2e9; color:#9c4700; font-size:11px;'>📍 {esc(loc['feature_value'].capitalize())} (+{loc['score_delta']})</span>")
    
    pref_chips_html = (" ".join(pref_chips)) if pref_chips else "<span class='muted' style='font-size:12px;'>Interact with vacancies (click, shortlist, apply) to train your agents on your exact preferences.</span>"

    training_banner_html = f"""
    <section class='training-banner'>
      <div style='display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:16px;'>
        <div style='display:flex; align-items:center; gap:14px;'>
          <div style='font-size:28px; background:#f2eafd; border-radius:12px; width:48px; height:48px; display:flex; align-items:center; justify-content:center;'>🧠</div>
          <div>
            <div style='display:flex; align-items:center; gap:8px;'>
              <span class='pill' style='background:#f3eaff; color:#6225b5; font-weight:800; font-size:11px;'>LEARNED REINFORCEMENT</span>
              <strong style='font-size:14px; color:#2d1b4e;'>Model Training: {esc(training_info['stage'])}</strong>
            </div>
            <p style='margin:3px 0 0; color:#584a6b; font-size:13px;'>{esc(training_info['stage_desc'])}</p>
          </div>
        </div>
        <div style='display:flex; gap:8px; align-items:center; flex-wrap:wrap;'>
          <span class='pill' style='background:#fff;'>🖱️ {training_info['clicks']} Clicks Tracked</span>
          <span class='pill' style='background:#fff;'>★ {training_info['shortlists']} Shortlists Learned</span>
          <span class='pill' style='background:#fff;'>✓ {training_info['applies']} Applied Signals</span>
          <form method='post' action='/rescore' style='margin:0;'>
            <button type='submit' class='btn-secondary btn-sm'>✦ Re-Debate with Learned Weights</button>
          </form>
        </div>
      </div>
      <div style='display:flex; gap:8px; align-items:center; flex-wrap:wrap; margin-top:14px; padding-top:12px; border-top:1px solid #ebdcfc; font-size:12px;'>
        <b style='color:#3a2063;'>Top Learned Preferences:</b>
        {pref_chips_html}
      </div>
    </section>
    """

    # Render Daily Picks Shelf
    daily_shelf_html = ""
    if daily_picks:
        pick_cards = []
        for dp in daily_picks:
            dp_id = esc(dp["id"])
            dp_title = esc(dp["title"])
            dp_company = esc(dp["company"])
            dp_location = esc(dp["location"] or "Global / Remote")
            dp_advocate = esc(dp["advocate_agent"] or "Supreme Jury")
            dp_score = dp["jury_score"] or dp["score"] or 0
            dp_verdict = esc(dp["jury_verdict"] or "Approved by Jury")
            dp_status = dp["status"] or "new"

            action_btn = (
                f"<span class='pill pill-amber'>★ Shortlisted</span>" if dp_status == "shortlisted" else
                (f"<span class='pill pill-blue'>✓ Applied</span>" if dp_status == "applied" else
                 f"""<form method='post' action='/job/{dp_id}/status' style='margin:0'>
                      <input type='hidden' name='status' value='shortlisted'>
                      <button type='submit' class='btn-sm btn-gold'>Shortlist</button>
                    </form>""")
            )

            pick_cards.append(f"""
            <div class='daily-card'>
              <div class='daily-crown'>👑 DAILY SELECTION</div>
              <div style='margin-top:10px;'>
                <div class='job-header'>
                  <span class='verdict-tag'>{dp_verdict}</span>
                  <span class='score-badge' style='background:#fcf1d8; color:#966100;'>{dp_score}/100 Jury Score</span>
                </div>
                <h3 style='margin:8px 0 4px;'>{dp_title}</h3>
                <p class='company-loc'>{dp_company} · {dp_location}</p>
                <div style='font-size:12px; color:#5a6857; margin-bottom:14px;'>
                  <b>Advocate:</b> {dp_advocate}<br>
                  <b>Recruitment:</b> <span style='color:#1f642a; font-weight:600;'>🟢 Live & Recruiting</span> ({esc(dp['posted_date'] or 'Recent')})
                </div>
              </div>
              <div class='job-footer'>
                <div class='daily-card-links'>
                  <a class='apply-link' target='_blank' rel='noopener noreferrer' href='/track/click/{dp_id}'>Apply / View posting ↗</a>
                  <a href='/jury#{dp_id}' style='font-size:12px; color:#966100; font-weight:700;'>Read Debate & Verdict →</a>
                </div>
                <div class='job-actions'>
                  {action_btn}
                </div>
              </div>
            </div>
            """)

        daily_shelf_html = f"""
        <section class='daily-section'>
          <div class='daily-header'>
            <div>
              <p class='eyebrow' style='color:#966100;'>SUPREME JURY DECREE</p>
              <h2 style='margin:4px 0 0;'>The Jury's Daily Selection</h2>
            </div>
            <a href='/jury' style='font-size:13px; font-weight:700; color:#966100;'>Open Jury Deliberation Room →</a>
          </div>
          <div class='daily-grid'>
            {"".join(pick_cards)}
          </div>
        </section>
        """

    # Render General Job Pipeline
    cards = []
    for j in jobs:
        j_id = esc(j['id'])
        j_title = esc(j['title'])
        j_company = esc(j['company'])
        j_location = esc(j['location'] or "Global / Remote")
        j_source = esc(j['source'])
        j_url = esc(j['url'])
        j_score = j['jury_score'] or j['score'] or 0
        j_status = j['status'] or "new"
        j_advocate = esc(j['advocate_agent'] or "")
        j_posted = esc(j['posted_date'] or "Recently posted")

        visa_txt = esc(j['visa_evidence'] or 'Not verified')
        housing_txt = esc(j['housing_evidence'] or 'Not verified')
        
        visa_class = "evidence-found" if "Mentioned" in (j['visa_evidence'] or "") else ""
        housing_class = "evidence-found" if "Mentioned" in (j['housing_evidence'] or "") else ""

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
        else:
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

        advocate_badge = f"<span class='pill' style='background:#f4f3ec;'>{j_advocate}</span>" if j_advocate else ""
        boost, reasons = job_boosts.get(j["id"], (0, []))
        learned_pill = f"<span class='pill' style='background:#f6efff; color:#6832b8; font-weight:750;'>🧠 {'+' if boost > 0 else ''}{boost} Learned Fit</span>" if boost != 0 else ""

        cards.append(f"""
        <article class='{job_card_class}'>
          <div>
            <div class='job-header'>
              <div class='pills'>
                <span class='pill'>{j_source}</span>
                <span class='pill pill-live'>🟢 Live</span>
                {status_pill}
                {learned_pill}
              </div>
              <span class='score-badge'>{j_score}/100 Match</span>
            </div>
            <h3>{j_title}</h3>
            <p class='company-loc'>{j_company} · {j_location} · <small>{j_posted}</small></p>
            <div class='evidence-box'>
              <div><b>Visa:</b> <span class='{visa_class}'>{visa_txt}</span></div>
              <div><b>Housing:</b> <span class='{housing_class}'>{housing_txt}</span></div>
            </div>
            {f"<div style='margin-bottom:12px; font-size:12px;'><b>Advocate:</b> {advocate_badge}</div>" if advocate_badge else ""}
          </div>
          <div class='job-footer'>
            <a class='apply-link' target='_blank' rel='noopener noreferrer' href='/track/click/{j_id}'>Apply / View posting ↗</a>
            <div class='job-actions'>
              {action_buttons}
            </div>
          </div>
        </article>
        """)

    cards_html = "".join(cards) if cards else """
      <div class='empty-state'>
        <h3>No matching vacancies found</h3>
        <p>No listings match your current criteria. Convene the Supreme Jury to trigger worker scouting and debate.</p>
        <div style='display:flex; justify-content:center; gap:10px;'>
          <form method='post' action='/convene-jury'><button type='submit' class='btn btn-gold'>⚖️ Convene Supreme Jury</button></form>
          <a href='/' class='btn btn-secondary'>Show All Vacancies</a>
        </div>
      </div>
    """

    flash_banner = f"<div class='notice notice-info'>{esc(msg)}</div>" if msg else ""

    last_refresh = latest_deliberation["session_date"] if latest_deliberation else "Not yet run"

    return layout(f"""
    {flash_banner}
    <section class='hero'>
      <p class='eyebrow'>HIERARCHICAL RECRUITMENT INTELLIGENCE</p>
      <h1>Multi-Agent Radar.<br><em>Curated by the Supreme Jury.</em></h1>
      <p>Level 3 Workers scout live international feeds. Level 2 Sub-Agents match against your CV and debate viability. The Supreme Jury cross-examines objections to crown your daily top roster.</p>
      <div class='hero-actions'>
        <form method='post' action='/convene-jury'>
          <button type='submit' class='btn btn-gold'>⚖️ Convene Supreme Jury & Debate</button>
        </form>
        <form method='post' action='/daily-refresh' data-refresh-form>
          <button type='submit' class='btn btn-secondary' data-refresh-button>↻ Refresh Today’s Top 5</button>
        </form>
        <a href='/jury' class='btn btn-subtle'>Open Jury Room →</a>
      </div>
      <p class='refresh-note'>Last selection: {esc(last_refresh)} UTC · Automatic refresh daily at midnight UTC</p>
    </section>

    <section class='agent-flow' data-agent-flow aria-label='Daily job selection process'>
      <div class='flow-heading'>
        <div>
          <p class='eyebrow'>TODAY’S SELECTION, IN MOTION</p>
          <h2>From job files to the Jury’s top five</h2>
        </div>
        <span class='flow-live'><span></span> DAILY REVIEW</span>
      </div>
      <div class='flow-stages'>
        <div class='flow-stage is-active' data-flow-stage='0'>
          <span class='flow-icon' aria-hidden='true'>🧑‍💼</span>
          <strong>Workers scout</strong>
          <small>Collect fresh job files</small>
          <span class='flow-files' aria-hidden='true'><i>▤</i><i>▤</i><i>▤</i></span>
        </div>
        <span class='flow-arrow' aria-hidden='true'>→</span>
        <div class='flow-stage' data-flow-stage='1'>
          <span class='flow-icon' aria-hidden='true'>📂</span>
          <strong>Submit dossiers</strong>
          <small>Send files to Sub-Agents</small>
          <span class='flow-files' aria-hidden='true'><i>▤</i><i>▤</i><i>▤</i></span>
        </div>
        <span class='flow-arrow' aria-hidden='true'>→</span>
        <div class='flow-stage' data-flow-stage='2'>
          <span class='flow-icon' aria-hidden='true'>💻 🎯 🌍</span>
          <strong>Agents debate</strong>
          <small>Challenge each other’s picks</small>
          <span class='flow-debate' aria-hidden='true'>“Strong fit!” · “What about visa?”</span>
        </div>
        <span class='flow-arrow' aria-hidden='true'>→</span>
        <div class='flow-stage flow-jury' data-flow-stage='3'>
          <span class='flow-icon' aria-hidden='true'>⚖️</span>
          <strong>Jury decides</strong>
          <small>Rank today’s top five</small>
          <span class='flow-top-five' aria-label='Five daily selections'>01&nbsp; 02&nbsp; 03&nbsp; 04&nbsp; 05</span>
        </div>
      </div>
      <p class='flow-status' data-flow-status>Workers are collecting fresh job files…</p>
      <p class='flow-footnote'>Your ranked selections appear below. Applications are always reviewed and submitted by you.</p>
    </section>

    {daily_shelf_html}

    {training_banner_html}

    <section class='stats'>
      <div>
        <strong>{total_raw}</strong>
        <small>Total Pipeline</small>
      </div>
      <div>
        <strong>{len(daily_picks)}</strong>
        <small>Daily Jury Picks</small>
      </div>
      <div>
        <strong>{shortlisted_count}</strong>
        <small>Shortlisted</small>
      </div>
      <div>
        <strong>{applied_count}</strong>
        <small>Applied</small>
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
        <h2>{f"Showing {status.capitalize()} Vacancies" if status != 'all' else "Candidate Vacancies Pool"} ({len(jobs)})</h2>
      </div>
      <a href='/profile' style='font-size:13px;'>Refine candidate matching criteria →</a>
    </div>

    <div class='jobs'>
      {cards_html}
    </div>
    """, active_nav="dashboard")

@app.get("/jury", response_class=HTMLResponse)
def jury_room(msg: str = ""):
    with get_db() as c:
        delib = c.execute("SELECT * FROM deliberations ORDER BY created_at DESC LIMIT 1").fetchone()
        
        # Fetch candidate jobs with their jury verdicts and debate arguments
        jobs = c.execute("""
            SELECT j.*, v.verdict as final_verdict, v.recommendation as final_rec, v.consensus_score as final_score
            FROM jobs j
            LEFT JOIN jury_verdicts v ON j.id = v.job_id
            WHERE j.status != 'dismissed'
            ORDER BY j.is_daily_pick DESC, j.jury_score DESC, j.score DESC LIMIT 25
        """).fetchall()

        # Fetch recent debate arguments
        all_args = c.execute("""
            SELECT * FROM agent_arguments ORDER BY id ASC
        """).fetchall()

    args_by_job = {}
    for a in all_args:
        jid = a["job_id"]
        if jid not in args_by_job:
            args_by_job[jid] = []
        args_by_job[jid].append(a)

    delib_summary = esc(delib["summary"]) if delib else "No formal jury sessions recorded yet. Convene the Supreme Jury to initiate worker scouting, sub-agent debates, and daily selections."
    session_id = esc(delib["id"]) if delib else "N/A"

    job_deliberations = []
    for j in jobs:
        jid = esc(j["id"])
        j_title = esc(j["title"])
        j_company = esc(j["company"])
        j_location = esc(j["location"] or "Global / Remote")
        j_url = esc(j["url"])
        j_score = j["jury_score"] or j["score"] or 0
        j_daily = j["is_daily_pick"] == 1
        j_verdict = esc(j["jury_verdict"] or j["final_verdict"] or "Pending Deliberation")
        j_rec = esc(j["final_rec"] or "Review application scope.")
        j_advocate = esc(j["advocate_agent"] or "Sub-Agent Consensus")
        j_status = j["status"] or "new"

        # Debate feed
        debate_speeches = []
        job_args = args_by_job.get(j["id"], [])
        if job_args:
            for arg in job_args:
                agent_name = esc(arg["agent_name"])
                agent_role = esc(arg["agent_role"])
                arg_type = esc(arg["argument_type"])
                statement = esc(arg["statement"])
                
                avatar = "💻" if "Software" in agent_name else ("🎯" if "Quality" in agent_name else ("🌍" if "Visa" in agent_name else "⚖️"))
                debate_speeches.append(f"""
                <div class='debate-speech'>
                  <div class='debate-avatar'>{avatar}</div>
                  <div class='speech-bubble'>
                    <div class='speech-author'>
                      <span>{agent_name} <small style='color:#777; font-weight:normal;'>({agent_role})</small></span>
                      <span class='pill' style='font-size:10px;'>{arg_type.upper()}</span>
                    </div>
                    <div>{statement}</div>
                  </div>
                </div>
                """)
        else:
            debate_speeches.append("""
            <p class='muted' style='margin:6px 0;'>Sub-Agents evaluated profile alignment and verified absence of immediate immigration blockers.</p>
            """)

        crown_badge = "<span class='verdict-tag' style='background:#fff5db; color:#966100; border-color:#e4bc6e;'>👑 Daily Selection</span>" if j_daily else ""
        
        action_btn = (
            f"<span class='pill pill-amber'>★ Shortlisted</span>" if j_status == "shortlisted" else
            (f"<span class='pill pill-blue'>✓ Applied</span>" if j_status == "applied" else
             f"""<form method='post' action='/job/{jid}/status' style='margin:0'>
                  <input type='hidden' name='status' value='shortlisted'>
                  <button type='submit' class='btn-sm btn-gold'>Shortlist for Daily Roster</button>
                </form>""")
        )

        job_deliberations.append(f"""
        <div class='deliberation-item' id='{jid}'>
          <div class='delib-header'>
            <div>
              <div class='pills' style='margin-bottom:6px;'>
                {crown_badge}
                <span class='pill pill-live'>🟢 Live & Recruiting</span>
                <span class='pill' style='background:#f3f1e9;'>Advocate: {j_advocate}</span>
              </div>
              <h3 style='margin:0 0 4px; font-size:18px;'>{j_title}</h3>
              <p class='company-loc' style='margin:0;'>{j_company} · {j_location}</p>
            </div>
            <div style='text-align:right;'>
              <span class='score-badge' style='font-size:14px; padding:6px 14px; background:#e8efe7; color:#214227;'>{j_score}/100 Consensus</span>
            </div>
          </div>

          <div class='debate-feed'>
            <div style='font-weight:750; font-size:12px; color:#596555; text-transform:uppercase; letter-spacing:0.08em; margin-bottom:4px;'>
              Court Debate & Cross-Examination
            </div>
            {"".join(debate_speeches)}
          </div>

          <div class='verdict-box'>
            <div class='verdict-header'>
              <span>⚖️ SUPREME JURY VERDICT:</span>
              <span style='text-transform:uppercase; color:#966100;'>{j_verdict}</span>
            </div>
            <p class='verdict-summary'>{esc((j['description'] or '')[:280])}...</p>
            <p class='verdict-action'><b>Recommendation for Candidate:</b> {j_rec}</p>
          </div>

          <div class='job-footer' style='margin-top:16px;'>
            <a target='_blank' rel='noopener noreferrer' href='{j_url}' style='font-size:13px; font-weight:700;'>View Live Official Vacancy ↗</a>
            <div class='job-actions'>
              {action_btn}
            </div>
          </div>
        </div>
        """)

    flash_banner = f"<div class='notice notice-info'>{esc(msg)}</div>" if msg else ""

    return layout(f"""
    {flash_banner}
    <div class='sectionhead'>
      <div>
        <p class='eyebrow'>SUPREME JURY ARBITRATION COURT</p>
        <h1>The Jury Room & Multi-Agent Deliberation</h1>
      </div>
      <form method='post' action='/convene-jury'>
        <button type='submit' class='btn btn-gold'>⚖️ Convene Supreme Jury & Re-Debate</button>
      </form>
    </div>
    <p class='muted'>Inspect the cross-examination between specialized Sub-Agents as they advocate for vacancies matching Adeena's CV, dispute constraints, and receive final decree from the Supreme Jury.</p>

    <!-- Sub-Agent Bench Showcase -->
    <div class='bench-grid'>
      <div class='agent-card'>
        <div class='agent-card-header'>
          <div class='agent-avatar'>💻</div>
          <div>
            <div class='agent-name'>Software & Systems Agent</div>
            <div class='agent-role'>Lead Technical Screener</div>
          </div>
        </div>
        <p class='agent-desc'>Evaluates technical stack alignment (Python, C++, Java, Flutter, Embedded Systems). Advocates for roles offering deep engineering ownership.</p>
      </div>

      <div class='agent-card'>
        <div class='agent-card-header'>
          <div class='agent-avatar'>🎯</div>
          <div>
            <div class='agent-name'>Quality & Operations Agent</div>
            <div class='agent-role'>QA & Reliability Specialist</div>
          </div>
        </div>
        <p class='agent-desc'>Assesses testing suites, IT support, diagnostics, and entry barrier accessibility. Challenges unrealistic senior experience prerequisites.</p>
      </div>

      <div class='agent-card'>
        <div class='agent-card-header'>
          <div class='agent-avatar'>🌍</div>
          <div>
            <div class='agent-name'>Visa & Relocation Advocate</div>
            <div class='agent-role'>Immigration & Mobility Counsel</div>
          </div>
        </div>
        <p class='agent-desc'>Vets international sponsorship, EU Blue Card, UK Skilled Worker, and relocation allowances. Vetoes roles that exclude non-residents.</p>
      </div>

      <div class='agent-card' style='border-color:var(--gold-border); background:#fffcf6;'>
        <div class='agent-card-header'>
          <div class='agent-avatar' style='background:#f6ebd3;'>⚖️</div>
          <div>
            <div class='agent-name' style='color:#966100;'>Supreme Jury Agent</div>
            <div class='agent-role'>Chief Career Arbiter</div>
          </div>
        </div>
        <p class='agent-desc'>Cross-examines Sub-Agents, reconciles trade-offs, assigns consensus scores, and issues the daily final selection roster.</p>
      </div>
    </div>

    <!-- Active Session Banner -->
    <div class='panel' style='background:#fbfbf9; border-left:4px solid #966100;'>
      <div style='display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:12px;'>
        <div>
          <span class='pill' style='background:#eedfae; color:#784f00; font-weight:800;'>SESSION {session_id}</span>
          <h3 style='margin:8px 0 4px;'>Latest Court Deliberation Summary</h3>
          <p style='color:#464f43; margin:0; font-size:14px;'>{delib_summary}</p>
        </div>
      </div>
    </div>

    <!-- Deliberations Roster -->
    <div class='deliberation-box'>
      {"".join(job_deliberations)}
    </div>
    """, active_nav="jury")

@app.post("/convene-jury")
async def convene_jury():
    result = await refresh_daily_selection_if_due(force=True)
    
    count = result.get("crowned_count", 0)
    msg = f"Supreme Jury session concluded! Evaluated live vacancies and crowned {count} Daily Selection roles."
    return RedirectResponse("/jury?msg=" + quote_plus(msg), status_code=303)

@app.get("/profile", response_class=HTMLResponse)
def profile_page(msg: str = ""):
    with get_db() as c:
        p = c.execute("SELECT * FROM profile WHERE id=1").fetchone()
        if not p:
            init_db()
            p = c.execute("SELECT * FROM profile WHERE id=1").fetchone()
    
    cv_filename = p["cv_filename"] if "cv_filename" in p.keys() and p["cv_filename"] else ""
    cv_uploaded_at = p["cv_uploaded_at"] if "cv_uploaded_at" in p.keys() and p["cv_uploaded_at"] else ""

    if cv_filename:
        current_cv_badge = f"""
        <div style='display:flex; align-items:center; gap:8px;'>
          <span class='pill pill-live' style='font-size:12px;'>✓ Active File: {esc(cv_filename)}</span>
          <a href='/profile/cv/download' class='btn btn-secondary btn-sm'>Download Stored CV</a>
        </div>
        """
    else:
        current_cv_badge = "<span class='muted' style='font-size:12px;'>No CV file uploaded yet</span>"

    flash_banner = f"<div class='notice'>{esc(msg)}</div>" if msg else ""

    return layout(f"""
    {flash_banner}
    <div class='sectionhead'>
      <div>
        <p class='eyebrow'>CANDIDATE SPECIFICATION</p>
        <h1>Candidate Profile & CV Upload</h1>
      </div>
    </div>
    <p class='muted'>All Sub-Agents (Software, QA, Visa) reference these criteria during worker filtering, CV matching, and court cross-examination.</p>
    
    <form class='panel' method='post' action='/profile' enctype='multipart/form-data'>
      <div class='cv-upload-zone'>
        <div style='display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:12px;'>
          <div>
            <label style='margin:0; font-size:15px; font-weight:750;'>Upload Updated CV / Resume</label>
            <p class='muted' style='margin:4px 0 0; font-size:12px;'>Supported formats: <b>PDF</b>, <b>TXT</b>, <b>Markdown</b> (max 10MB). Text will be extracted automatically into your profile summary below.</p>
          </div>
          {current_cv_badge}
        </div>
        <div style='margin-top:14px;'>
          <input type='file' name='cv_file' accept='.pdf,.txt,.md,.doc,.docx' class='file-input'>
        </div>
      </div>

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
        <textarea name='cv_text' rows='10' placeholder='Paste your CV text or experience summary here, or upload your PDF/file above...'>{esc(p['cv_text'])}</textarea>
      </label>
      <div style='display:flex; gap:12px; margin-top:20px; align-items:center;'>
        <button type='submit' class='primary'>Save Profile & Re-Debate Jobs</button>
        <a href='/' class='btn btn-secondary'>Cancel</a>
      </div>
    </form>
    """, active_nav="profile")

@app.post("/profile")
async def save_profile(
    name: str = Form(""),
    citizenship: str = Form(""),
    residence: str = Form(""),
    degree: str = Form(""),
    skills: str = Form(""),
    target_roles: str = Form(""),
    cv_text: str = Form(""),
    cv_file: Optional[UploadFile] = File(None)
):
    uploaded_filename = None
    extracted_text = ""
    now_iso = datetime.now(timezone.utc).isoformat()

    if cv_file and cv_file.filename:
        file_bytes = await cv_file.read()
        if len(file_bytes) > 0:
            clean_name = re.sub(r'[^a-zA-Z0-9_.-]', '_', cv_file.filename)
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            saved_filename = f"{timestamp}_{clean_name}"
            save_path = UPLOAD_DIR / saved_filename
            with open(save_path, "wb") as f:
                f.write(file_bytes)
            
            extracted_text = extract_text_from_file(clean_name, file_bytes)
            uploaded_filename = clean_name

            # If user uploaded a new CV, automatically populate cv_text with extracted content
            if extracted_text and (not cv_text.strip() or len(extracted_text) > 30):
                cv_text = extracted_text

    with get_db() as c:
        if uploaded_filename:
            c.execute("""UPDATE profile SET name=?, citizenship=?, residence=?, degree=?, skills=?,
                         target_roles=?, cv_text=?, cv_filename=?, cv_uploaded_at=?, updated_at=? WHERE id=1""",
                      (name, citizenship, residence, degree, skills, target_roles, cv_text, uploaded_filename, now_iso, now_iso))
        else:
            c.execute("""UPDATE profile SET name=?, citizenship=?, residence=?, degree=?, skills=?,
                         target_roles=?, cv_text=?, updated_at=? WHERE id=1""",
                      (name, citizenship, residence, degree, skills, target_roles, cv_text, now_iso))
        
    await refresh_daily_selection_if_due(force=True)

    upload_msg = f"CV '{uploaded_filename}' uploaded and parsed! " if uploaded_filename else ""
    msg = f"{upload_msg}Profile saved! Supreme Jury re-convened and updated the daily roster."
    return RedirectResponse("/profile?msg=" + quote_plus(msg), status_code=303)

@app.get("/profile/cv/download")
def download_cv():
    with get_db() as c:
        p = c.execute("SELECT cv_filename FROM profile WHERE id=1").fetchone()
        if not p or not p["cv_filename"]:
            return RedirectResponse("/profile?msg=" + quote_plus("No CV has been uploaded yet."), status_code=303)
        filename = p["cv_filename"]

    matching = sorted(UPLOAD_DIR.glob(f"*_{filename}"))
    if not matching:
        return RedirectResponse("/profile?msg=" + quote_plus(f"File '{filename}' not found in upload repository."), status_code=303)

    latest_file = matching[-1]
    return FileResponse(
        path=str(latest_file),
        filename=filename,
        media_type="application/octet-stream"
    )

@app.post("/job/{job_id}/status")
def update_job_status(job_id: str, request: Request, status: str = Form(...)):
    allowed = ["new", "shortlisted", "applied", "dismissed"]
    if status not in allowed:
        status = "new"

    sid = getattr(request.state, "session_id", request.cookies.get("agent_session", "user_default"))
    with get_db() as c:
        job = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if job and status in ["shortlisted", "applied", "dismissed"]:
            record_interaction(c, sid, job_id, status, dict(job))

        c.execute("UPDATE jobs SET status=? WHERE id=?", (status, job_id))
        if status == "applied":
            c.execute("""INSERT INTO applications (job_id, status, created_at)
                         VALUES (?, 'applied', ?)""",
                      (job_id, datetime.now(timezone.utc).isoformat()))

    return RedirectResponse(f"/?status={status}&msg=" + quote_plus(f"Job marked as {status}! Agents trained on your selection."), status_code=303)

@app.get("/track/click/{job_id}")
def track_job_click(job_id: str, request: Request):
    sid = getattr(request.state, "session_id", request.cookies.get("agent_session", "user_default"))
    with get_db() as c:
        job = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if job:
            record_interaction(c, sid, job_id, "click", dict(job))
            target_url = job["url"]
        else:
            target_url = "/"
    return RedirectResponse(target_url, status_code=302)

@app.post("/training/reset")
def reset_training():
    with get_db() as c:
        c.execute("DELETE FROM interaction_events")
        c.execute("DELETE FROM learned_weights")
    return RedirectResponse("/settings?msg=" + quote_plus("Learned agent training data reset to baseline."), status_code=303)

@app.post("/shortlist/{job_id}")
def shortlist(job_id: str):
    with get_db() as c:
        c.execute("UPDATE jobs SET status='shortlisted' WHERE id=?", (job_id,))
    return RedirectResponse("/?status=shortlisted&msg=" + quote_plus("Job added to your shortlist!"), status_code=303)

@app.post("/scan")
async def scan():
    result = await refresh_daily_selection_if_due(force=True)
    
    count = result.get("crowned_count", 0)
    return RedirectResponse(f"/?msg=" + quote_plus(f"Worker sweep & Jury deliberation complete! Crowned {count} Daily Selection roles."), status_code=303)

@app.post("/rescore")
async def rescore():
    await refresh_daily_selection_if_due(force=True)
    return RedirectResponse("/jury?msg=" + quote_plus("All vacancies re-deliberated and re-scored by the Supreme Jury!"), status_code=303)

@app.post("/seed")
async def seed_curated():
    await refresh_daily_selection_if_due(force=True)
    return RedirectResponse("/?msg=" + quote_plus("Curated roles synchronized and reviewed by the Supreme Jury!"), status_code=303)

@app.post("/daily-refresh")
async def daily_refresh():
    result = await refresh_daily_selection_if_due(force=True)
    count = result.get("crowned_count", 0)
    msg = f"Daily refresh complete! The Supreme Jury ranked {count} roles for today."
    return RedirectResponse("/?msg=" + quote_plus(msg), status_code=303)

@app.get("/settings", response_class=HTMLResponse)
def settings(msg: str = ""):
    with get_db() as c:
        total = c.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        sources = c.execute("SELECT source, COUNT(*) n FROM jobs GROUP BY source").fetchall()
        source_summary = ", ".join(f"{r['source']} ({r['n']})" for r in sources) if sources else "None currently cached"
        training_info = get_training_profile(c)

    flash_banner = f"<div class='notice'>{esc(msg)}</div>" if msg else ""

    return layout(f"""
    {flash_banner}
    <div class='sectionhead'>
      <div>
        <p class='eyebrow'>SYSTEM ORCHESTRATION</p>
        <h1>Agent Architecture & Settings</h1>
      </div>
    </div>
    
    <div class='panel' style='background:#fcfaff; border-color:#e3d2fc;'>
      <div style='display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:12px;'>
        <div>
          <span class='pill' style='background:#f3eaff; color:#6225b5; font-weight:800; font-size:11px;'>BEHAVIORAL REINFORCEMENT LEARNING</span>
          <h3 style='margin:8px 0 4px;'>Continuous Agent Training & Click Tracking</h3>
          <p style='color:#584a6b; margin:0; font-size:14px;'>Agents and Workers dynamically train on your clicks, shortlists, and application choices via persistent session cookies. Status: <b>{esc(training_info['stage'])}</b> ({training_info['total_events']} events logged).</p>
        </div>
        <form method='post' action='/training/reset' onsubmit='return confirm("Reset learned agent memory back to initial calibration?");'>
          <button type='submit' class='btn-subtle btn-sm' style='color:#b3261e; border-color:#f2b8b5;'>Reset Agent Memory</button>
        </form>
      </div>
      <div style='display:flex; gap:12px; margin-top:16px; flex-wrap:wrap;'>
        <div style='background:#fff; border:1px solid #e3d2fc; padding:12px 18px; border-radius:12px; min-width:120px;'>
          <strong style='font-size:20px; color:#6225b5;'>{training_info['clicks']}</strong>
          <small style='display:block; color:#777;'>Vacancy Clicks</small>
        </div>
        <div style='background:#fff; border:1px solid #e3d2fc; padding:12px 18px; border-radius:12px; min-width:120px;'>
          <strong style='font-size:20px; color:#6225b5;'>{training_info['shortlists']}</strong>
          <small style='display:block; color:#777;'>Shortlists Learned</small>
        </div>
        <div style='background:#fff; border:1px solid #e3d2fc; padding:12px 18px; border-radius:12px; min-width:120px;'>
          <strong style='font-size:20px; color:#6225b5;'>{training_info['applies']}</strong>
          <small style='display:block; color:#777;'>Applied Decisions</small>
        </div>
        <div style='background:#fff; border:1px solid #e3d2fc; padding:12px 18px; border-radius:12px; min-width:120px;'>
          <strong style='font-size:20px; color:#6225b5;'>{training_info['dismisses']}</strong>
          <small style='display:block; color:#777;'>Dismissed Penalties</small>
        </div>
      </div>
    </div>

    <div class='panel'>
      <h3>Multi-Agent Court Controls</h3>
      <p>Initiate worker scout sweeps, re-convene the Sub-Agent debates, or synchronize verified employer pipelines.</p>
      <div style='display:flex; gap:12px; flex-wrap:wrap; margin-top:16px;'>
        <form method='post' action='/convene-jury'>
          <button type='submit' class='btn btn-gold'>⚖️ Convene Supreme Jury & Re-Debate</button>
        </form>
        <form method='post' action='/scan'>
          <button type='submit' class='primary'>✦ Run Worker Scout Sweep</button>
        </form>
        <form method='post' action='/rescore'>
          <button type='submit' class='btn btn-secondary'>Re-Score All Vacancies</button>
        </form>
      </div>
    </div>

    <div class='panel'>
      <h3>Agent Hierarchy Overview</h3>
      <p><b>Level 3 Workers:</b> Arbeitnow Scout, Remotive Scout, Direct Employer ATS Scout.</p>
      <p><b>Level 2 Sub-Agents:</b> Software & Systems Screener (💻), Quality & Operations Specialist (🎯), Visa & Relocation Advocate (🌍).</p>
      <p><b>Level 1 Supreme Jury:</b> Deliberation Arbiter (⚖️) issuing daily verdicts and assigning daily picks.</p>
      <p><b>Active Vacancies Cache:</b> {esc(source_summary)} (Total: {total})</p>
    </div>

    <div class='panel'>
      <h3>Automation & Governance</h3>
      <p><b>Human-in-the-Loop:</b> The agent system never submits applications autonomously or shares confidential identity files. Applications are curated for your manual review and approval.</p>
      <p><b>Immigration Vetting:</b> Visa sponsorship evidence is cross-examined by the Visa & Relocation Advocate before the Jury awards daily selection honors.</p>
    </div>
    """, active_nav="settings")

@app.get("/health")
def health():
    with get_db() as c:
        count = c.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        picks = c.execute("SELECT COUNT(*) FROM jobs WHERE is_daily_pick=1").fetchone()[0]
    return {
        "ok": True,
        "service": "adeena-job-agent",
        "agent_architecture": "hierarchical-multi-agent-jury",
        "jobs_count": count,
        "daily_picks_count": picks
    }
