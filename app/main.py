from fastapi import FastAPI, HTTPException, Request, Response, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Optional, Tuple
import sqlite3, os, json, re, hashlib, html
import asyncio
import logging
import io
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus

from app.agents.orchestrator import run_jury_deliberation_workflow, run_discovery_workers
from app.agents.jury import SupremeJuryAgent
from app.agents.learner import record_interaction, compute_learned_boost, get_training_profile, init_learner_db
from app.agents.cv_analyzer import analyze_cv, get_cv_analysis, score_cv_match

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
          liveness_status TEXT DEFAULT 'unverified', liveness_checked_at TEXT,
          authenticity_status TEXT DEFAULT 'unverified', authenticity_evidence TEXT,
          posted_date TEXT DEFAULT 'Recently posted',
          status TEXT DEFAULT 'new', created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS profile (
          id INTEGER PRIMARY KEY CHECK(id=1), name TEXT, citizenship TEXT,
          residence TEXT, degree TEXT, skills TEXT, target_roles TEXT,
          cv_text TEXT, cv_filename TEXT, cv_uploaded_at TEXT, cv_analysis TEXT,
          experience_level TEXT DEFAULT 'entry-level',
          excluded_skills TEXT DEFAULT 'Rust',
          excluded_job_terms TEXT DEFAULT 'backend, back-end, full-stack, full stack',
          updated_at TEXT
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
            ("liveness_status", "TEXT DEFAULT 'unverified'"),
            ("liveness_checked_at", "TEXT"),
            ("authenticity_status", "TEXT DEFAULT 'unverified'"),
            ("authenticity_evidence", "TEXT"),
            ("posted_date", "TEXT DEFAULT 'Recently posted'")
        ]:
            try:
                c.execute(f"ALTER TABLE jobs ADD COLUMN {col} {col_def}")
            except Exception:
                pass

        for col, col_def in [
            ("cv_filename", "TEXT"),
            ("cv_uploaded_at", "TEXT"),
            ("cv_analysis", "TEXT"),
            ("experience_level", "TEXT DEFAULT 'entry-level'"),
            ("excluded_skills", "TEXT DEFAULT 'Rust'"),
            ("excluded_job_terms", "TEXT DEFAULT 'backend, back-end, full-stack, full stack'")
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

        profile_row = c.execute("SELECT * FROM profile WHERE id=1").fetchone()
        if profile_row and profile_row["cv_text"] and not profile_row["cv_analysis"]:
            cv_analysis = analyze_cv(profile_row["cv_text"])
            c.execute(
                """UPDATE profile
                   SET cv_analysis=?, target_roles=?, skills=?
                   WHERE id=1""",
                (
                    json.dumps(cv_analysis),
                    ", ".join(cv_analysis["roles"]),
                    ", ".join(cv_analysis["skills"]) or profile_row["skills"],
                ),
            )
        
        init_learner_db(c)


async def refresh_daily_selection_if_due(force: bool = False):
    async with daily_refresh_lock:
        with get_db() as c:
            today = datetime.now(timezone.utc).date().isoformat()
            latest = c.execute(
                "SELECT session_date, top_picks_count FROM deliberations ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            if not force and latest and latest["session_date"] == today:
                unchecked_jobs = c.execute(
                    """SELECT COUNT(*) FROM jobs
                       WHERE status != 'dismissed'
                       AND (liveness_checked_at IS NULL OR substr(liveness_checked_at, 1, 10) != ?)""",
                    (today,),
                ).fetchone()[0]
                if unchecked_jobs == 0:
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
            import pypdf
            reader = pypdf.PdfReader(io.BytesIO(content))
            texts = []
            for idx, page in enumerate(reader.pages):
                extracted = page.extract_text()
                if extracted:
                    texts.append(extracted)
            return "\n".join(texts).strip()
        except Exception as e:
            return f"Error extracting PDF text: {str(e)}"
    elif ext == ".docx":
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                document = ET.fromstring(archive.read("word/document.xml"))
            namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
            return " ".join(
                node.text for node in document.iter(f"{namespace}t") if node.text
            ).strip()
        except (KeyError, OSError, ET.ParseError, zipfile.BadZipFile) as e:
            return f"Error extracting DOCX text: {str(e)}"
    elif ext == ".doc":
        return "Error extracting DOC text: legacy .doc files are not supported. Convert to PDF, DOCX, TXT, or Markdown."
    else:
        # Default fallback attempt to decode as UTF-8
        try:
            return content.decode("utf-8", errors="ignore")
        except Exception:
            return ""

def esc(v):
    return html.escape(str(v) if v is not None else "")


def liveness_badge(status):
    if status == "live":
        return "<span class='pill pill-live'>🟢 Posting page reachable</span>"
    if status == "expired":
        return "<span class='pill pill-expired'>🔴 Posting link returned 404/410</span>"
    if status == "unavailable":
        return "<span class='pill pill-expired'>🔴 Role appears closed</span>"
    return "<span class='pill pill-unverified'>🟡 Availability unverified</span>"


def authenticity_badge(status):
    if status == "likely_official":
        return "<span class='pill pill-live'>Domain signal: likely official</span>"
    if status == "suspicious":
        return "<span class='pill pill-expired'>Suspicious link</span>"
    return "<span class='pill pill-unverified'>Employer authenticity unverified</span>"


def posting_link(job_id, status):
    if status in ("expired", "unavailable"):
        label = "Posting link unavailable" if status == "expired" else "Role appears closed"
        return f"<span class='posting-unavailable'>{label}</span>"
    if status == "live":
        label = "Apply / View posting ↗"
    else:
        label = "Check posting (availability unverified) ↗"
    return (
        f"<a class='apply-link' target='_blank' rel='noopener noreferrer' "
        f"href='/track/click/{esc(job_id)}'>{label}</a>"
    )


def generate_application_materials(profile: dict, job: dict) -> Tuple[str, str]:
    """Create locally tailored drafts using only facts in the saved profile and job."""
    job_title = (job.get("title") or "the advertised role").strip()
    company = (job.get("company") or "the company").strip()
    name = (profile.get("name") or "Candidate").strip()
    residence = (profile.get("residence") or "").strip()
    degree = (profile.get("degree") or "").strip()
    cv_text = (profile.get("cv_text") or "").strip()
    match = score_cv_match(profile, job)
    matched_skills = match["matched_skills"]
    profile_skills = [
        skill.strip()
        for skill in (profile.get("skills") or "").split(",")
        if skill.strip()
    ]
    relevant_skills = matched_skills or profile_skills[:8]

    summary_parts = []
    if degree:
        summary_parts.append(f"Background: {degree}.")
    if relevant_skills:
        summary_parts.append(
            "Relevant skills listed in the candidate profile: "
            + ", ".join(relevant_skills)
            + "."
        )
    if not summary_parts:
        summary_parts.append(
            "Candidate profile details are limited; add education and skills before using this draft."
        )

    resume_sections = [
        name,
        residence,
        f"RESUME TAILORED FOR {job_title} — {company}",
        "",
        "PROFESSIONAL SUMMARY",
        f"Applicant for {job_title} at {company}. " + " ".join(summary_parts),
    ]
    if relevant_skills:
        resume_sections.extend([
            "",
            "RELEVANT SKILLS",
            *[f"- {skill}" for skill in relevant_skills],
        ])
    if degree:
        resume_sections.extend(["", "EDUCATION", degree])
    resume_sections.extend([
        "",
        "ORIGINAL CV DETAILS",
        cv_text or "No CV or experience details have been added to the profile yet.",
    ])
    tailored_cv = "\n".join(resume_sections).strip()

    skill_phrase = ", ".join(matched_skills)
    if skill_phrase:
        fit_paragraph = (
            f"My profile lists skills including {skill_phrase}, which are relevant "
            "to the role described in your posting."
        )
    else:
        fit_paragraph = (
            "I was interested in the responsibilities described for this role and "
            "would welcome the opportunity to discuss how my background may fit."
        )
    background = f"My educational background includes {degree}." if degree else ""
    cover_letter = "\n".join([
        "Dear Hiring Team,",
        "",
        f"I am writing to apply for the {job_title} position at {company}.",
        background,
        fit_paragraph,
        "",
        "I would welcome the opportunity to discuss my application and how I could "
        "contribute to your team. Thank you for your time and consideration.",
        "",
        "Sincerely,",
        name,
    ])
    return tailored_cv, cover_letter


def make_docx(content: str) -> bytes:
    """Build a minimal Word document with readable title and section headings."""
    word_ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    rel_ns = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    package_rel_ns = "http://schemas.openxmlformats.org/package/2006/relationships"
    content_type_ns = "http://schemas.openxmlformats.org/package/2006/content-types"
    xml_ns = "http://www.w3.org/XML/1998/namespace"
    ET.register_namespace("w", word_ns)
    ET.register_namespace("r", rel_ns)
    ET.register_namespace("", package_rel_ns)

    document = ET.Element(f"{{{word_ns}}}document")
    body = ET.SubElement(document, f"{{{word_ns}}}body")
    section_headings = {
        "PROFESSIONAL SUMMARY", "RELEVANT SKILLS", "EDUCATION",
        "ORIGINAL CV DETAILS",
    }
    for index, line in enumerate(content.splitlines()):
        paragraph = ET.SubElement(body, f"{{{word_ns}}}p")
        if not line:
            continue
        run = ET.SubElement(paragraph, f"{{{word_ns}}}r")
        if index == 0 or line in section_headings:
            properties = ET.SubElement(run, f"{{{word_ns}}}rPr")
            ET.SubElement(properties, f"{{{word_ns}}}b")
            size = "32" if index == 0 else "24"
            ET.SubElement(properties, f"{{{word_ns}}}sz", {f"{{{word_ns}}}val": size})
        text = ET.SubElement(run, f"{{{word_ns}}}t")
        text.set(f"{{{xml_ns}}}space", "preserve")
        text.text = line

    page_section = ET.SubElement(body, f"{{{word_ns}}}sectPr")
    ET.SubElement(
        page_section,
        f"{{{word_ns}}}pgSz",
        {f"{{{word_ns}}}w": "12240", f"{{{word_ns}}}h": "15840"},
    )
    ET.SubElement(
        page_section,
        f"{{{word_ns}}}pgMar",
        {
            f"{{{word_ns}}}top": "1440",
            f"{{{word_ns}}}right": "1440",
            f"{{{word_ns}}}bottom": "1440",
            f"{{{word_ns}}}left": "1440",
        },
    )

    content_types = ET.Element(f"{{{content_type_ns}}}Types")
    ET.SubElement(
        content_types,
        f"{{{content_type_ns}}}Default",
        {"Extension": "rels", "ContentType": "application/vnd.openxmlformats-package.relationships+xml"},
    )
    ET.SubElement(
        content_types,
        f"{{{content_type_ns}}}Default",
        {"Extension": "xml", "ContentType": "application/xml"},
    )
    ET.SubElement(
        content_types,
        f"{{{content_type_ns}}}Override",
        {
            "PartName": "/word/document.xml",
            "ContentType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml",
        },
    )
    relationships = ET.Element(f"{{{package_rel_ns}}}Relationships")
    ET.SubElement(
        relationships,
        f"{{{package_rel_ns}}}Relationship",
        {
            "Id": "rId1",
            "Type": "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument",
            "Target": "word/document.xml",
        },
    )

    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", ET.tostring(content_types, encoding="utf-8", xml_declaration=True))
        archive.writestr("_rels/.rels", ET.tostring(relationships, encoding="utf-8", xml_declaration=True))
        archive.writestr("word/document.xml", ET.tostring(document, encoding="utf-8", xml_declaration=True))
    return output.getvalue()


def store_application_materials(c: sqlite3.Connection, job: dict, profile: dict) -> None:
    tailored_cv, cover_letter = generate_application_materials(profile, job)
    existing = c.execute(
        "SELECT id FROM applications WHERE job_id=? ORDER BY id DESC LIMIT 1",
        (job["id"],),
    ).fetchone()
    if existing:
        c.execute(
            """UPDATE applications
               SET status=?, tailored_cv=?, cover_letter=? WHERE id=?""",
            (job.get("status") or "new", tailored_cv, cover_letter, existing["id"]),
        )
    else:
        c.execute(
            """INSERT INTO applications
               (job_id, status, tailored_cv, cover_letter, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (
                job["id"], job.get("status") or "new", tailored_cv, cover_letter,
                datetime.now(timezone.utc).isoformat(),
            ),
        )


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
        "Workers are gathering job files and carrying them to the Sub-Agents…",
        "Sub-Agents are debating each job before passing finalists to the Jury…",
        "The Jury is hearing the arguments and choosing today’s top five…"
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
      window.setInterval(advance, 3200);
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
        pref_chips.append(f"<span class='pill' style='background:#f1f4ff; color:#4057d4; font-size:11px;'>★ {esc(kw['feature_value'])} (+{kw['score_delta']})</span>")
    for loc in top_locs[:3]:
        pref_chips.append(f"<span class='pill' style='background:#fdf2e9; color:#9c4700; font-size:11px;'>📍 {esc(loc['feature_value'].capitalize())} (+{loc['score_delta']})</span>")
    
    pref_chips_html = (" ".join(pref_chips)) if pref_chips else "<span class='muted' style='font-size:12px;'>Interact with vacancies (click, shortlist, apply) to train your agents on your exact preferences.</span>"

    training_banner_html = f"""
    <section class='training-banner'>
      <div style='display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:16px;'>
        <div style='display:flex; align-items:center; gap:14px;'>
          <div style='font-size:28px; background:#e9f3ff; border-radius:12px; width:48px; height:48px; display:flex; align-items:center; justify-content:center;'>🧠</div>
          <div>
            <div style='display:flex; align-items:center; gap:8px;'>
              <span class='pill' style='background:#f1f4ff; color:#4057d4; font-weight:800; font-size:11px;'>LEARNED REINFORCEMENT</span>
              <strong style='font-size:14px; color:#3e1026;'>Model Training: {esc(training_info['stage'])}</strong>
            </div>
            <p style='margin:3px 0 0; color:#735d69; font-size:13px;'>{esc(training_info['stage_desc'])}</p>
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
      <div style='display:flex; gap:8px; align-items:center; flex-wrap:wrap; margin-top:14px; padding-top:12px; border-top:1px solid #cbd2ff; font-size:12px;'>
        <b style='color:#4057d4;'>Top Learned Preferences:</b>
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
            dp_liveness = dp["liveness_status"] or "unverified"
            dp_authenticity = dp["authenticity_status"] or "unverified"

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
                  <span class='score-badge' style='background:#fff0df; color:#790a3d;'>{dp_score}/100 Jury Score</span>
                </div>
                <h3 style='margin:8px 0 4px;'>{dp_title}</h3>
                <p class='company-loc'>{dp_company} · {dp_location}</p>
                <div style='font-size:12px; color:#735d69; margin-bottom:14px;'>
                  <b>Advocate:</b> {dp_advocate}<br>
                  <b>Posting check:</b> {liveness_badge(dp_liveness)}<br>
                  <b>Employer link check:</b> {authenticity_badge(dp_authenticity)}<br>
                  <b>Listed:</b> {esc(dp['posted_date'] or 'Recent')}
                </div>
              </div>
              <div class='job-footer'>
                <div class='daily-card-links'>
                  {posting_link(dp_id, dp_liveness)}
                  <a href='/jury#{dp_id}' style='font-size:12px; color:#790a3d; font-weight:700;'>Read Debate & Verdict →</a>
                </div>
                <div class='job-actions'>
                  {action_btn}
                  {f"<a class='btn btn-secondary btn-sm' href='/job/{dp_id}/application'>Resume &amp; cover letter</a>" if dp_status in ('shortlisted', 'applied') else ""}
                </div>
              </div>
            </div>
            """)

        daily_shelf_html = f"""
        <section class='daily-section'>
          <div class='daily-header'>
            <div>
              <p class='eyebrow' style='color:#790a3d;'>SUPREME JURY DECREE</p>
              <h2 style='margin:4px 0 0;'>The Jury's Daily Selection</h2>
            </div>
            <a href='/jury' style='font-size:13px; font-weight:700; color:#790a3d;'>Open Jury Deliberation Room →</a>
          </div>
          <div class='daily-grid'>
            {"".join(pick_cards)}
          </div>
        </section>
        """
    else:
        daily_shelf_html = f"""
        <section class='daily-section'>
          <div class='daily-header'>
            <div>
              <p class='eyebrow' style='color:#790a3d;'>SUPREME JURY DECREE</p>
              <h2 style='margin:4px 0 0;'>The Jury's Daily Selection</h2>
            </div>
            <a href='/jury' style='font-size:13px; font-weight:700; color:#790a3d;'>Open Jury Deliberation Room →</a>
          </div>
          <div class='empty-state'>
            <h3>No jobs currently meet your Jury preferences</h3>
            <p>The Jury considers qualified jobs in all locations, prioritizing {esc(p['residence'] or 'your preferred city')} but not limiting picks to it. It excludes senior and advanced-tier roles, Rust and backend-focused jobs, unavailable listings, and roles restricted to Saudi nationals. Update your role preferences in your <a href='/profile'>profile</a> or refresh after new postings arrive.</p>
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
        j_score = j['jury_score'] or j['score'] or 0
        j_status = j['status'] or "new"
        j_advocate = esc(j['advocate_agent'] or "")
        j_posted = esc(j['posted_date'] or "Recently posted")
        j_liveness = j["liveness_status"] or "unverified"
        j_authenticity = j["authenticity_status"] or "unverified"

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
              <a class='btn btn-secondary btn-sm' href='/job/{j_id}/application'>Resume &amp; cover letter</a>
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
              <a class='btn btn-secondary btn-sm' href='/job/{j_id}/application'>Resume &amp; cover letter</a>
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

        advocate_badge = f"<span class='pill' style='background:#f6e8ee;'>{j_advocate}</span>" if j_advocate else ""
        boost, reasons = job_boosts.get(j["id"], (0, []))
        learned_pill = f"<span class='pill' style='background:#e9f3ff; color:#4057d4; font-weight:750;'>🧠 {'+' if boost > 0 else ''}{boost} Learned Fit</span>" if boost != 0 else ""

        cards.append(f"""
        <article class='{job_card_class}'>
          <div>
            <div class='job-header'>
              <div class='pills'>
                <span class='pill'>{j_source}</span>
                {liveness_badge(j_liveness)}
                {authenticity_badge(j_authenticity)}
                {status_pill}
                {learned_pill}
              </div>
              <span class='score-badge'>{j_score}/100 Match</span>
            </div>
            <h3>{j_title}</h3>
            <p class='company-loc'>{j_company} · {j_location} · <small>{j_posted}</small></p>
            <div class='evidence-box'>
              <div><b>Visa:</b> <span class='{visa_class}'>{visa_txt}</span></div>
              <div><b>Relocation &amp; accommodation:</b> <span class='{housing_class}'>{housing_txt}</span></div>
            </div>
            {f"<div style='margin-bottom:12px; font-size:12px;'><b>Advocate:</b> {advocate_badge}</div>" if advocate_badge else ""}
          </div>
          <div class='job-footer'>
            {posting_link(j_id, j_liveness)}
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
        j_score = j["jury_score"] or j["score"] or 0
        j_daily = j["is_daily_pick"] == 1
        j_verdict = esc(j["jury_verdict"] or j["final_verdict"] or "Pending Deliberation")
        j_rec = esc(j["final_rec"] or "Review application scope.")
        j_advocate = esc(j["advocate_agent"] or "Sub-Agent Consensus")
        j_status = j["status"] or "new"
        j_liveness = j["liveness_status"] or "unverified"
        j_authenticity = j["authenticity_status"] or "unverified"

        # Debate feed
        debate_speeches = []
        job_args = args_by_job.get(j["id"], [])
        if job_args:
            for arg in job_args:
                agent_name = esc(arg["agent_name"])
                agent_role = esc(arg["agent_role"])
                arg_type = esc(arg["argument_type"])
                statement = esc(arg["statement"])
                
                avatar = (
                    "🛂" if "Local Expat Eligibility" in agent_name else
                    "📍" if "City Preference" in agent_name else
                    "🛡️" if "Authenticity" in agent_name else
                    "🔎" if "Availability" in agent_name else
                    ("📄" if "CV & Target Role" in agent_name else
                    ("💻" if "Software" in agent_name else
                     ("🎯" if "Quality" in agent_name else
                      ("🌍" if "Visa" in agent_name else "⚖️"))))
                )
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

        crown_badge = "<span class='verdict-tag' style='background:#fff0df; color:#790a3d; border-color:#ffd09b;'>👑 Daily Selection</span>" if j_daily else ""
        
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
                {liveness_badge(j_liveness)}
                {authenticity_badge(j_authenticity)}
                <span class='pill' style='background:#e9f3ff;'>Advocate: {j_advocate}</span>
              </div>
              <h3 style='margin:0 0 4px; font-size:18px;'>{j_title}</h3>
              <p class='company-loc' style='margin:0;'>{j_company} · {j_location}</p>
            </div>
            <div style='text-align:right;'>
              <span class='score-badge' style='font-size:14px; padding:6px 14px; background:#e7f2ff; color:#354dba;'>{j_score}/100 Consensus</span>
            </div>
          </div>

          <div class='debate-feed'>
            <div style='font-weight:750; font-size:12px; color:#735d69; text-transform:uppercase; letter-spacing:0.08em; margin-bottom:4px;'>
              Court Debate & Cross-Examination
            </div>
            {"".join(debate_speeches)}
          </div>

          <div class='verdict-box'>
            <div class='verdict-header'>
              <span>⚖️ SUPREME JURY VERDICT:</span>
              <span style='text-transform:uppercase; color:#790a3d;'>{j_verdict}</span>
            </div>
            <p class='verdict-summary'>{esc((j['description'] or '')[:280])}...</p>
            <p class='verdict-action'><b>Recommendation for Candidate:</b> {j_rec}</p>
          </div>

          <div class='job-footer' style='margin-top:16px;'>
            {posting_link(jid, j_liveness)}
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

    <section class='agent-flow' data-agent-flow aria-label='Daily job selection process'>
      <div class='flow-heading'>
        <div>
          <p class='eyebrow'>TODAY’S SELECTION, IN MOTION</p>
          <h2>Daily Selection: From job files to the Jury’s top five</h2>
        </div>
        <span class='flow-live'><span></span> DAILY REVIEW</span>
      </div>
      <div class='flow-stages'>
        <span class='flow-delivery' aria-hidden='true'>📄</span>
        <div class='flow-stage flow-scene-stage is-active' data-flow-stage='0'>
          <div class='flow-cartoon flow-worker-cartoon' aria-hidden='true'>
            <span class='flow-character'>🧑‍💼</span>
            <span class='flow-character'>👩‍💼</span>
            <span class='flow-cartoon-files'><i>▤</i><i>▤</i><i>▤</i></span>
            <span class='flow-speech'>Found new jobs!</span>
          </div>
          <strong>Workers scout</strong>
          <small>Collect files from job sites</small>
        </div>
        <span class='flow-arrow' aria-hidden='true'>→</span>
        <div class='flow-stage flow-scene-stage' data-flow-stage='1'>
          <div class='flow-cartoon flow-agent-cartoon' aria-hidden='true'>
            <span class='flow-character'>💻</span>
            <span class='flow-character'>🎯</span>
            <span class='flow-character'>🌍</span>
            <span class='flow-speech'>“Strong fit!”<br>“Check the link!”</span>
          </div>
          <strong>Sub-Agents debate</strong>
          <small>Challenge, check, and pass finalists on</small>
        </div>
        <span class='flow-arrow' aria-hidden='true'>→</span>
        <div class='flow-stage flow-scene-stage flow-jury' data-flow-stage='2'>
          <div class='flow-cartoon flow-jury-cartoon' aria-hidden='true'>
            <span class='flow-character'>👩‍⚖️</span>
            <span class='flow-jury-scales'>⚖️</span>
            <span class='flow-speech'>The top five!</span>
          </div>
          <strong>Jury decides</strong>
          <small>Choose the best jobs to apply for today</small>
          <span class='flow-top-five' aria-label='Five daily selections'>01&nbsp; 02&nbsp; 03&nbsp; 04&nbsp; 05</span>
        </div>
      </div>
      <p class='flow-status' data-flow-status>Workers are collecting fresh job files…</p>
      <p class='flow-footnote'>Your ranked selections appear below. Applications are always reviewed and submitted by you.</p>
    </section>

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

      <div class='agent-card' style='border-color:var(--gold-border); background:#fff8ef;'>
        <div class='agent-card-header'>
          <div class='agent-avatar' style='background:#ffd09b;'>⚖️</div>
          <div>
            <div class='agent-name' style='color:#790a3d;'>Supreme Jury Agent</div>
            <div class='agent-role'>Chief Career Arbiter</div>
          </div>
        </div>
        <p class='agent-desc'>Cross-examines Sub-Agents, reconciles trade-offs, assigns consensus scores, and issues the daily final selection roster.</p>
      </div>
    </div>

    <!-- Active Session Banner -->
    <div class='panel' style='background:#fff9fb; border-left:4px solid #790a3d;'>
      <div style='display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:12px;'>
        <div>
          <span class='pill' style='background:#fff0df; color:#790a3d; font-weight:800;'>SESSION {session_id}</span>
          <h3 style='margin:8px 0 4px;'>Latest Court Deliberation Summary</h3>
          <p style='color:#59414c; margin:0; font-size:14px;'>{delib_summary}</p>
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
    
    profile_data = dict(p)
    cv_analysis = get_cv_analysis(profile_data)
    analysis_skills = cv_analysis.get("skills", [])
    analysis_roles = cv_analysis.get("roles", [])
    analysis_skills_html = " ".join(
        f"<span class='pill'>{esc(skill)}</span>" for skill in analysis_skills
    ) or "<span class='muted'>No CV skills detected yet.</span>"
    analysis_roles_html = " ".join(
        f"<span class='pill pill-amber'>{esc(role)}</span>" for role in analysis_roles
    ) or "<span class='muted'>Add CV details to generate role recommendations.</span>"

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

    <section class='cv-analysis panel' aria-labelledby='cv-analysis-heading'>
      <p class='eyebrow'>CV-BASED MATCHING</p>
      <h2 id='cv-analysis-heading'>Your CV analysis</h2>
      <p>{esc(cv_analysis.get('summary', 'Add CV details to begin analysis.'))}</p>
      <div class='cv-analysis-group'>
        <strong>Detected skills</strong>
        <div class='pills'>{analysis_skills_html}</div>
      </div>
      <div class='cv-analysis-group'>
        <strong>Recommended target roles</strong>
        <div class='pills'>{analysis_roles_html}</div>
      </div>
      <p class='muted' style='margin-bottom:0;'>Recommendations are generated from CV text, editable below, and used by the Jury when ranking daily jobs.</p>
    </section>
    
    <form class='panel' method='post' action='/profile' enctype='multipart/form-data'>
      <div class='cv-upload-zone'>
        <div style='display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:12px;'>
          <div>
            <label style='margin:0; font-size:15px; font-weight:750;'>Upload Updated CV / Resume</label>
            <p class='muted' style='margin:4px 0 0; font-size:12px;'>Supported formats: <b>PDF</b>, <b>DOCX</b>, <b>TXT</b>, <b>Markdown</b> (max 10MB). Text will be extracted and used to suggest skills and target roles.</p>
          </div>
          {current_cv_badge}
        </div>
        <div style='margin-top:14px;'>
          <input type='file' name='cv_file' accept='.pdf,.txt,.md,.docx' class='file-input'>
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
        <small class='muted'>Automatically suggested when CV text changes; you can edit these.</small>
        <textarea name='target_roles' rows='3'>{esc(p['target_roles'])}</textarea>
      </label>
      <div class='cv-analysis-group'>
        <strong>Job fit preferences used by the Jury</strong>
        <small class='muted'>Your current city is a preference, not a location restriction: qualified jobs in other cities or remote roles can still be selected. For Jeddah jobs, the Jury requires posting evidence that Pakistani applicants are eligible (for example, “Pakistani applicants welcome”); generic visa or iqama language is not enough. This does not prove past hiring history.</small>
      </div>
      <label>Experience level
        <select name='experience_level'>
          <option value='entry-level' {'selected' if (p['experience_level'] or 'entry-level') == 'entry-level' else ''}>Entry-level only</option>
          <option value='any' {'selected' if (p['experience_level'] or 'entry-level') == 'any' else ''}>Any level</option>
        </select>
      </label>
      <label>Skills to exclude from recommendations (comma-separated)
        <input name='excluded_skills' value='{esc(p['excluded_skills'] or 'Rust')}' placeholder='e.g. Rust'>
      </label>
      <label>Role types to avoid (comma-separated)
        <input name='excluded_job_terms' value='{esc(p['excluded_job_terms'] or 'backend, back-end, full-stack, full stack')}' placeholder='e.g. backend, full-stack'>
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
    experience_level: str = Form("entry-level"),
    excluded_skills: str = Form("Rust"),
    excluded_job_terms: str = Form("backend, back-end, full-stack, full stack"),
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

            if extracted_text and not extracted_text.startswith("Error extracting"):
                cv_text = extracted_text

    with get_db() as c:
        previous_profile = c.execute(
            "SELECT cv_text, cv_analysis FROM profile WHERE id=1"
        ).fetchone()
        cv_changed = cv_text.strip() != (previous_profile["cv_text"] or "").strip()
        cv_analysis_json = previous_profile["cv_analysis"]
        if cv_changed:
            cv_analysis = analyze_cv(cv_text)
            cv_analysis_json = json.dumps(cv_analysis)
            if cv_text.strip():
                target_roles = ", ".join(cv_analysis["roles"])
                if cv_analysis["skills"]:
                    skills = ", ".join(cv_analysis["skills"])

        if uploaded_filename:
            c.execute(
                """UPDATE profile SET name=?, citizenship=?, residence=?, degree=?, skills=?,
                   target_roles=?, cv_text=?, cv_filename=?, cv_uploaded_at=?, cv_analysis=?,
                   experience_level=?, excluded_skills=?, excluded_job_terms=?, updated_at=?
                   WHERE id=1""",
                (
                    name, citizenship, residence, degree, skills, target_roles, cv_text,
                    uploaded_filename, now_iso, cv_analysis_json, experience_level,
                    excluded_skills, excluded_job_terms, now_iso,
                ),
            )
        else:
            c.execute(
                """UPDATE profile SET name=?, citizenship=?, residence=?, degree=?, skills=?,
                   target_roles=?, cv_text=?, cv_analysis=?, experience_level=?,
                   excluded_skills=?, excluded_job_terms=?, updated_at=? WHERE id=1""",
                (
                    name, citizenship, residence, degree, skills, target_roles, cv_text,
                    cv_analysis_json, experience_level, excluded_skills, excluded_job_terms,
                    now_iso,
                ),
            )
        
    await refresh_daily_selection_if_due(force=True)

    if cv_file and cv_file.filename and extracted_text.startswith("Error extracting"):
        upload_msg = f"CV '{uploaded_filename}' uploaded, but text extraction failed: {extracted_text}. "
    elif cv_file and cv_file.filename and not extracted_text.strip():
        upload_msg = (
            f"CV '{uploaded_filename}' uploaded, but no readable text was extracted. "
            "Paste its text into the CV field or try another supported format. "
        )
    else:
        upload_msg = f"CV '{uploaded_filename}' uploaded and analyzed! " if uploaded_filename else ""
    role_msg = "CV analysis generated target-role recommendations. " if cv_changed and cv_text.strip() else ""
    msg = f"{upload_msg}{role_msg}Profile saved! Supreme Jury re-convened and updated the daily roster."
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


@app.get("/job/{job_id}/application", response_class=HTMLResponse)
def application_page(job_id: str, msg: str = ""):
    with get_db() as c:
        job = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not job:
            return RedirectResponse("/?msg=" + quote_plus("Job not found."), status_code=303)
        application = c.execute(
            """SELECT * FROM applications WHERE job_id=?
               ORDER BY id DESC LIMIT 1""",
            (job_id,),
        ).fetchone()

    resume_text = application["tailored_cv"] if application and application["tailored_cv"] else ""
    cover_text = application["cover_letter"] if application and application["cover_letter"] else ""
    downloads = ""
    if resume_text and cover_text:
        downloads = f"""
        <div style='display:flex; gap:10px; flex-wrap:wrap; margin-bottom:16px;'>
          <a class='btn btn-secondary btn-sm' href='/job/{esc(job_id)}/application/download/resume'>Download tailored resume (.docx)</a>
          <a class='btn btn-secondary btn-sm' href='/job/{esc(job_id)}/application/download/cover-letter'>Download cover letter (.docx)</a>
        </div>
        """
    flash_banner = f"<div class='notice notice-info'>{esc(msg)}</div>" if msg else ""
    return layout(f"""
    {flash_banner}
    <div class='sectionhead'>
      <div>
        <p class='eyebrow'>APPLICATION MATERIALS</p>
        <h1>{esc(job['title'])} · {esc(job['company'])}</h1>
      </div>
      <a href='/' class='btn btn-secondary btn-sm'>Back to jobs</a>
    </div>
    <p class='muted'>Drafts are generated locally from your saved profile and this job posting. Review every detail for accuracy before using them.</p>
    {downloads}
    <div style='display:flex; gap:10px; flex-wrap:wrap; margin-bottom:16px;'>
      <form method='post' action='/job/{esc(job_id)}/application/generate' style='margin:0;'>
        <button type='submit' class='btn btn-gold'>{'Regenerate from profile' if resume_text or cover_text else 'Generate drafts'}</button>
      </form>
    </div>
    <form method='post' action='/job/{esc(job_id)}/application/save' class='panel'>
      <label>Tailored resume
        <textarea name='tailored_cv' rows='18' required>{esc(resume_text)}</textarea>
      </label>
      <label>Cover letter
        <textarea name='cover_letter' rows='12' required>{esc(cover_text)}</textarea>
      </label>
      <div style='display:flex; gap:10px; flex-wrap:wrap;'>
        <button type='submit' class='primary'>Save edits</button>
      </div>
    </form>
    """)


@app.post("/job/{job_id}/application/generate")
def regenerate_application_materials(job_id: str):
    with get_db() as c:
        job = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        profile = c.execute("SELECT * FROM profile WHERE id=1").fetchone()
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        if not profile:
            raise HTTPException(status_code=500, detail="Candidate profile is not configured")
        store_application_materials(c, dict(job), dict(profile))
    return RedirectResponse(
        f"/job/{quote_plus(job_id)}/application?msg="
        + quote_plus("Drafts regenerated from your saved profile."),
        status_code=303,
    )


@app.post("/job/{job_id}/application/save")
def save_application_materials(
    job_id: str,
    tailored_cv: str = Form(...),
    cover_letter: str = Form(...),
):
    with get_db() as c:
        job = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        application = c.execute(
            """SELECT id FROM applications WHERE job_id=?
               ORDER BY id DESC LIMIT 1""",
            (job_id,),
        ).fetchone()
        if application:
            c.execute(
                """UPDATE applications SET tailored_cv=?, cover_letter=?
                   WHERE id=?""",
                (tailored_cv, cover_letter, application["id"]),
            )
        else:
            c.execute(
                """INSERT INTO applications
                   (job_id, status, tailored_cv, cover_letter, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    job_id, job["status"] or "new", tailored_cv, cover_letter,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
    return RedirectResponse(
        f"/job/{quote_plus(job_id)}/application?msg=" + quote_plus("Draft edits saved."),
        status_code=303,
    )


@app.get("/job/{job_id}/application/download/{document_type}")
def download_application_material(job_id: str, document_type: str):
    if document_type not in {"resume", "cover-letter"}:
        raise HTTPException(status_code=404, detail="Document type not found")
    field = "tailored_cv" if document_type == "resume" else "cover_letter"
    with get_db() as c:
        job = c.execute("SELECT title, company FROM jobs WHERE id=?", (job_id,)).fetchone()
        application = c.execute(
            f"""SELECT {field} FROM applications WHERE job_id=?
                ORDER BY id DESC LIMIT 1""",
            (job_id,),
        ).fetchone()
    if not job or not application or not application[field]:
        raise HTTPException(status_code=404, detail="Application draft not found")
    base_name = re.sub(
        r"[^a-zA-Z0-9]+",
        "-",
        f"{job['company'] or ''}-{job['title'] or ''}",
    ).strip("-").lower() or "application"
    filename = f"{base_name}-{document_type}.docx"
    return Response(
        content=make_docx(application[field]),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
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
            event_type = {
                "shortlisted": "shortlist",
                "applied": "apply",
                "dismissed": "dismiss",
            }[status]
            record_interaction(c, sid, job_id, event_type, dict(job))

        c.execute("UPDATE jobs SET status=? WHERE id=?", (status, job_id))
        if job and status == "shortlisted":
            profile = c.execute("SELECT * FROM profile WHERE id=1").fetchone()
            if not profile:
                raise HTTPException(status_code=500, detail="Candidate profile is not configured")
            job_data = dict(job)
            job_data["status"] = status
            store_application_materials(c, job_data, dict(profile))
        elif job and status == "applied":
            application = c.execute(
                """SELECT id FROM applications WHERE job_id=?
                   ORDER BY id DESC LIMIT 1""",
                (job_id,),
            ).fetchone()
            if application:
                c.execute(
                    "UPDATE applications SET status='applied' WHERE id=?",
                    (application["id"],),
                )
            else:
                c.execute(
                    """INSERT INTO applications (job_id, status, created_at)
                       VALUES (?, 'applied', ?)""",
                    (job_id, datetime.now(timezone.utc).isoformat()),
                )

    if status == "shortlisted":
        message = "Job shortlisted; resume and cover letter drafts are ready to review."
    else:
        message = f"Job marked as {status}! Agents trained on your selection."
    return RedirectResponse(
        f"/?status={status}&msg=" + quote_plus(message),
        status_code=303,
    )

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
        job = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        profile = c.execute("SELECT * FROM profile WHERE id=1").fetchone()
        if job and not profile:
            raise HTTPException(status_code=500, detail="Candidate profile is not configured")
        if job and profile:
            store_application_materials(c, dict(job), dict(profile))
    return RedirectResponse(
        "/?status=shortlisted&msg="
        + quote_plus("Job added to your shortlist; resume and cover letter drafts are ready to review."),
        status_code=303,
    )

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
    
    <div class='panel' style='background:#f1f4ff; border-color:#cbd2ff;'>
      <div style='display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:12px;'>
        <div>
          <span class='pill' style='background:#f1f4ff; color:#4057d4; font-weight:800; font-size:11px;'>BEHAVIORAL REINFORCEMENT LEARNING</span>
          <h3 style='margin:8px 0 4px;'>Continuous Agent Training & Click Tracking</h3>
          <p style='color:#735d69; margin:0; font-size:14px;'>Agents and Workers dynamically train on your clicks, shortlists, and application choices via persistent session cookies. Status: <b>{esc(training_info['stage'])}</b> ({training_info['total_events']} events logged).</p>
        </div>
        <form method='post' action='/training/reset' onsubmit='return confirm("Reset learned agent memory back to initial calibration?");'>
          <button type='submit' class='btn-subtle btn-sm' style='color:#790a3d; border-color:#d7afbf;'>Reset Agent Memory</button>
        </form>
      </div>
      <div style='display:flex; gap:12px; margin-top:16px; flex-wrap:wrap;'>
        <div style='background:#fff; border:1px solid #cbd2ff; padding:12px 18px; border-radius:12px; min-width:120px;'>
          <strong style='font-size:20px; color:#4057d4;'>{training_info['clicks']}</strong>
          <small style='display:block; color:#777;'>Vacancy Clicks</small>
        </div>
        <div style='background:#fff; border:1px solid #cbd2ff; padding:12px 18px; border-radius:12px; min-width:120px;'>
          <strong style='font-size:20px; color:#4057d4;'>{training_info['shortlists']}</strong>
          <small style='display:block; color:#777;'>Shortlists Learned</small>
        </div>
        <div style='background:#fff; border:1px solid #cbd2ff; padding:12px 18px; border-radius:12px; min-width:120px;'>
          <strong style='font-size:20px; color:#4057d4;'>{training_info['applies']}</strong>
          <small style='display:block; color:#777;'>Applied Decisions</small>
        </div>
        <div style='background:#fff; border:1px solid #cbd2ff; padding:12px 18px; border-radius:12px; min-width:120px;'>
          <strong style='font-size:20px; color:#4057d4;'>{training_info['dismisses']}</strong>
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
