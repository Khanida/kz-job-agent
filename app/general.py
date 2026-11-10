"""Multi-user job discovery app, separate from the personalized app.main entry point."""

from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timedelta, timezone
import asyncio
import hashlib
import hmac
import html
import logging
import os
from pathlib import Path
import re
import secrets
import sqlite3
from typing import Optional
from urllib.parse import quote_plus
import uuid

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.agents.cv_analyzer import analyze_cv
from app.agents.learner import init_learner_db, record_interaction
from app.agents.orchestrator import verify_listing_availability
from app.agents.workers import ArbeitnowScout, RemotiveScout

APP_DIR = Path(__file__).resolve().parent
AUTH_DB_PATH = Path(os.getenv("GENERAL_AUTH_DATABASE_PATH", str(APP_DIR.parent / "general_auth.db")))
USER_DATA_DIR = Path(os.getenv("GENERAL_USER_DATA_DIR", str(APP_DIR.parent / "user_data")))
SESSION_COOKIE = "job_agent_session"
CSRF_COOKIE = "job_agent_csrf"
SESSION_MAX_AGE = 60 * 60 * 24 * 14
PASSWORD_ITERATIONS = 310_000
refresh_lock = asyncio.Lock()
logger = logging.getLogger(__name__)


def esc(value: object) -> str:
    return html.escape(str(value or ""), quote=True)


@contextmanager
def get_auth_db():
    AUTH_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(AUTH_DB_PATH), timeout=25.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_auth_db():
    with get_auth_db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
          id TEXT PRIMARY KEY,
          email TEXT NOT NULL UNIQUE COLLATE NOCASE,
          name TEXT NOT NULL,
          password_hash TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS auth_sessions (
          token_hash TEXT PRIMARY KEY,
          user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          csrf_token TEXT NOT NULL,
          expires_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS auth_sessions_expiry_idx ON auth_sessions(expires_at);
        """)


@contextmanager
def get_user_db(user_id: str):
    USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    db_path = USER_DATA_DIR / f"{user_id}.db"
    conn = sqlite3.connect(str(db_path), timeout=25.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS jobs (
          id TEXT PRIMARY KEY, title TEXT, company TEXT, location TEXT,
          url TEXT, source TEXT, description TEXT, visa_evidence TEXT,
          housing_evidence TEXT, score INTEGER DEFAULT 0, jury_score INTEGER DEFAULT 0,
          is_daily_pick INTEGER DEFAULT 0, advocate_agent TEXT, jury_verdict TEXT,
          liveness_status TEXT DEFAULT 'unverified', liveness_checked_at TEXT,
          authenticity_status TEXT DEFAULT 'unverified', authenticity_evidence TEXT,
          posted_date TEXT DEFAULT 'Recently posted', status TEXT DEFAULT 'new',
          worker_scout TEXT DEFAULT '',
          created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS profile (
          id INTEGER PRIMARY KEY CHECK(id=1), name TEXT NOT NULL DEFAULT '',
          citizenship TEXT DEFAULT '', residence TEXT DEFAULT '', degree TEXT DEFAULT '',
          skills TEXT DEFAULT '', target_roles TEXT DEFAULT '', cv_text TEXT DEFAULT '',
          cv_analysis TEXT DEFAULT '', experience_level TEXT DEFAULT 'any',
          excluded_skills TEXT DEFAULT '', excluded_job_terms TEXT DEFAULT '',
          updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS deliberations (
          id TEXT PRIMARY KEY, session_date TEXT, summary TEXT,
          top_picks_count INTEGER, created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS agent_arguments (
          id INTEGER PRIMARY KEY AUTOINCREMENT, deliberation_id TEXT, job_id TEXT,
          agent_name TEXT, agent_role TEXT, argument_type TEXT, statement TEXT,
          score INTEGER, created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS jury_verdicts (
          id INTEGER PRIMARY KEY AUTOINCREMENT, deliberation_id TEXT, job_id TEXT,
          verdict TEXT, consensus_score INTEGER, strengths TEXT, risks TEXT,
          recommendation TEXT, is_selected INTEGER DEFAULT 0, created_at TEXT
        );
        """)
        job_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()
        }
        if "worker_scout" not in job_columns:
            conn.execute("ALTER TABLE jobs ADD COLUMN worker_scout TEXT DEFAULT ''")
        conn.execute(
            """INSERT OR IGNORE INTO profile
               (id, name, citizenship, residence, degree, skills, target_roles,
                cv_text, experience_level, excluded_skills, excluded_job_terms, updated_at)
               VALUES (1, '', '', '', '', '', '', '', 'any', '', '', ?)""",
            (datetime.now(timezone.utc).isoformat(),),
        )
        init_learner_db(conn)
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def hash_password(password: str, salt: Optional[bytes] = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS
    )
    return f"{salt.hex()}:{digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        salt_hex, digest_hex = encoded.split(":", 1)
        actual = hash_password(password, bytes.fromhex(salt_hex)).split(":", 1)[1]
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, digest_hex)


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def set_auth_cookie(response, token: str, request: Request):
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="lax",
        path="/",
    )


def auth_page(title: str, action: str, csrf_token: str, message: str = "") -> HTMLResponse:
    account_link = (
        "<a href='/login'>Sign in</a>" if action == "/register"
        else "<a href='/register'>Create an account</a>"
    )
    email_field = "<label>Email<input type='email' name='email' autocomplete='email' required maxlength='254'></label>"
    name_field = (
        "<label>Your name<input name='name' autocomplete='name' required maxlength='100'></label>"
        if action == "/register" else ""
    )
    password_help = "Use at least 12 characters." if action == "/register" else ""
    notice = f"<div class='notice notice-info'>{esc(message)}</div>" if message else ""
    body = f"""
    {notice}
    <section class='hero auth-hero'>
      <p class='eyebrow'>YOUR JOB SEARCH, YOUR PROFILE</p>
      <h1>{esc(title)}</h1>
      <p>Create a private profile to get job recommendations for your skills, experience, and location.</p>
    </section>
    <form class='panel auth-panel' method='post' action='{action}'>
      <input type='hidden' name='csrf_token' value='{esc(csrf_token)}'>
      {name_field}
      {email_field}
      <label>Password<input type='password' name='password' autocomplete='{'new-password' if action == '/register' else 'current-password'}' required minlength='{'12' if action == '/register' else '1'}' maxlength='128'></label>
      <small class='muted'>{password_help}</small>
      <button class='primary' type='submit'>{'Create account' if action == '/register' else 'Sign in'}</button>
      <p class='muted'>{account_link}</p>
    </form>
    """
    return HTMLResponse(page_layout(body, show_nav=False))


def page_layout(body: str, csrf_token: str = "", active_nav: str = "", show_nav: bool = True) -> str:
    nav = ""
    if show_nav:
        nav_items = [
            ("/dashboard", "Job matches", "dashboard"),
            ("/jury", "Jury Room", "jury"),
            ("/profile", "My profile", "profile"),
        ]
        nav = "".join(
            f"<a href='{href}' class='{'active' if active_nav == key else ''}'>{label}</a>"
            for href, label, key in nav_items
        )
        nav += f"""
        <form method='post' action='/logout' class='nav-logout'>
          <input type='hidden' name='csrf_token' value='{esc(csrf_token)}'>
          <button type='submit' class='btn-subtle btn-sm'>Sign out</button>
        </form>
        """
    return f"""<!doctype html>
<html lang='en'>
<head>
  <meta charset='utf-8'>
  <meta name='viewport' content='width=device-width, initial-scale=1'>
  <title>Job Scout — Find your next role</title>
  <link rel='stylesheet' href='/static/style.css'>
</head>
<body>
  <header>
    <div><div class='brand'>✦ JOB SCOUT</div><small>Find work that fits you</small></div>
    <nav>{nav}</nav>
  </header>
  <main>{body}</main>
  <footer>Your profile and job-search activity are stored separately from other accounts.</footer>
  <script>
    (() => {{
      const workflow = document.querySelector("[data-job-workflow]");
      if (workflow) {{
        const stages = [...workflow.querySelectorAll("[data-job-stage]")];
        const status = workflow.querySelector("[data-job-workflow-status]");
        const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
        const messages = [
          "Scouts look across public job boards.",
          "Reviewers compare roles with your profile.",
          "The Jury weighs the evidence and ranks matches."
        ];
        let active = 0;
        const advance = () => {{
          stages.forEach((stage, index) => {{
            stage.classList.toggle("is-active", index === active);
            stage.classList.toggle("is-complete", index < active);
          }});
          status.textContent = messages[active];
          active = (active + 1) % stages.length;
        }};
        if (stages.length && status) {{
          if (reducedMotion) {{
            status.textContent = "Scouts find postings, reviewers check fit, and the Jury ranks your matches.";
          }} else {{
            advance();
            window.setInterval(advance, 2600);
          }}
        }}
      }}

      const refreshForm = document.querySelector("[data-job-refresh]");
      if (refreshForm) {{
        refreshForm.addEventListener("submit", () => {{
          const button = refreshForm.querySelector("button[type='submit']");
          const status = refreshForm.querySelector("[data-refresh-status]");
          if (button) {{
            button.disabled = true;
            button.textContent = "Searching and reviewing jobs…";
          }}
          if (status) status.textContent = "Searching job feeds and preparing your Jury review. This may take a little while.";
        }});
      }}
    }})();
  </script>
</body>
</html>"""


def user_context(request: Request) -> tuple[dict, str]:
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="Sign in to continue.")
    return user, request.state.csrf_token


def validate_csrf(request: Request, submitted: str):
    expected = getattr(request.state, "csrf_token", "")
    if not expected or not submitted or not hmac.compare_digest(expected, submitted):
        raise HTTPException(status_code=403, detail="Your form expired. Reload the page and try again.")


def job_link(job_id: str, status: str) -> str:
    label = "View job & apply ↗" if status == "live" else "Check job posting ↗"
    return f"<a class='apply-link' href='/job/{esc(job_id)}/visit'>{label}</a>"


def score_job_for_profile(job: dict, profile: dict) -> int:
    title = (job.get("title") or "").lower()
    description = (job.get("description") or "").lower()
    location = (job.get("location") or "").lower()
    posting = f"{title} {description}"
    score = 0

    cv_analysis = analyze_cv(profile.get("cv_text") or "") if profile.get("cv_text") else {}
    configured_roles = profile.get("target_roles") or ""
    configured_skills = profile.get("skills") or ""
    roles = [
        part.strip().lower()
        for part in (configured_roles or ", ".join(cv_analysis.get("roles", []))).split(",")
        if part.strip()
    ]
    for role in roles:
        if role in title:
            score += 26
        elif role in description:
            score += 12

    skills = [
        part.strip().lower()
        for part in (configured_skills or ", ".join(cv_analysis.get("skills", []))).split(",")
        if part.strip()
    ]
    skill_matches = sum(
        bool(re.search(r"(?<!\w)" + re.escape(skill) + r"(?!\w)", posting))
        for skill in skills
    )
    score += min(skill_matches * 8, 40)

    preferred_city = (profile.get("residence") or "").split(",", 1)[0].strip().lower()
    if score > 0:
        if preferred_city and preferred_city in location:
            score += 8
        elif "remote" in location or "worldwide" in location:
            score += 4

    if profile.get("experience_level") == "entry-level" and score > 0:
        if any(term in title for term in ("junior", "entry level", "graduate", "intern", "associate")):
            score += 6
        if re.search(r"\b(senior|staff|principal|lead|director|head of)\b", title):
            return 0

    excluded_skills = [
        term.strip().lower()
        for term in (profile.get("excluded_skills") or "").split(",")
        if term.strip()
    ]
    excluded_roles = [
        term.strip().lower()
        for term in (profile.get("excluded_job_terms") or "").split(",")
        if term.strip()
    ]
    if any(re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", posting) for term in excluded_skills):
        return 0
    if any(term in title for term in excluded_roles):
        return 0
    return max(0, min(100, score))


class PublicJobBoardScout:
    """Collects general job-board listings, without candidate-specific curated jobs."""

    name = "International Job Board Scout"

    async def fetch_jobs(self) -> list[dict]:
        return await ArbeitnowScout().fetch_jobs()


class RemoteOpportunityScout:
    """Collects remote listings from a separate public job feed."""

    name = "Remote Opportunity Scout"

    async def fetch_jobs(self) -> list[dict]:
        return await RemotiveScout().fetch_jobs()


async def run_general_discovery_workers() -> list[dict]:
    scouts = [PublicJobBoardScout(), RemoteOpportunityScout()]
    results = await asyncio.gather(
        *(scout.fetch_jobs() for scout in scouts), return_exceptions=True
    )
    jobs_by_url: dict[str, dict] = {}
    for scout, result in zip(scouts, results):
        if isinstance(result, BaseException):
            logger.warning("%s failed to fetch job listings: %s", scout.name, result)
            continue
        for job in result:
            url = job.get("url", "")
            if url and url not in jobs_by_url:
                job["worker_scout"] = getattr(scout, "name", scout.__class__.__name__)
                jobs_by_url[url] = job
    return list(jobs_by_url.values())


class RoleAlignmentAgent:
    name = "Role & Skills Advocate"
    role = "Profile Alignment Reviewer"
    avatar = "🎯"

    def evaluate(self, job: dict, profile: dict) -> dict:
        score = score_job_for_profile(job, profile)
        if score:
            evidence = (
                f"Profile overlap produced a {score}/100 match from the roles, skills, "
                "experience preference, and location entered in the profile."
            )
        else:
            evidence = (
                "No clear match was found for the profile's target roles or skills, "
                "or the posting matches an excluded skill or role type."
            )
        return {"score": score, "evidence": evidence}


class ExperiencePreferencesAgent:
    name = "Experience & Preferences Reviewer"
    role = "Seniority and Exclusions Check"
    avatar = "🧭"

    def evaluate(self, job: dict, profile: dict) -> dict:
        title = (job.get("title") or "").lower()
        description = (job.get("description") or "").lower()
        if profile.get("experience_level") == "entry-level":
            seniority_terms = ("senior", "staff", "principal", "lead", "director", "head of")
            years = re.search(r"\b(?:[8-9]|\d{2,})\+?\s+years?\b", description)
            if any(re.search(r"\b" + re.escape(term) + r"\b", title) for term in seniority_terms):
                return {"score": 0, "eligible": False, "evidence": "The title indicates a seniority level outside the entry-level preference."}
            if years:
                return {"score": 0, "eligible": False, "evidence": f"The listing asks for {years.group(0)}, which conflicts with the entry-level preference."}

        exclusions = [
            term.strip().lower()
            for term in (profile.get("excluded_job_terms") or "").split(",")
            if term.strip()
        ]
        excluded_skills = [
            term.strip().lower()
            for term in (profile.get("excluded_skills") or "").split(",")
            if term.strip()
        ]
        for term in exclusions:
            if term in title:
                return {"score": 0, "eligible": False, "evidence": f"The title matches the excluded role type “{term}”."}
        for term in excluded_skills:
            if re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", f"{title} {description}"):
                return {"score": 0, "eligible": False, "evidence": f"The listing mentions the excluded skill “{term}”."}
        return {
            "score": 85,
            "eligible": True,
            "evidence": "No conflict was found with the experience level or exclusions in the profile.",
        }


class PostingQualityAgent:
    name = "Posting Quality Reviewer"
    role = "Availability and Employer-Link Evidence"
    avatar = "🔎"

    def evaluate(self, job: dict) -> dict:
        liveness = job.get("liveness_status", "unverified")
        authenticity = job.get("authenticity_status", "unverified")
        if liveness != "live":
            return {
                "score": 0,
                "eligible": False,
                "evidence": f"The posting availability is {liveness.replace('_', ' ')}; it cannot be recommended as a confirmed opening.",
            }
        if authenticity == "suspicious":
            return {
                "score": 0,
                "eligible": False,
                "evidence": "The posting URL has a suspicious destination and should not be used.",
            }
        if authenticity == "likely_official":
            return {
                "score": 95,
                "eligible": True,
                "evidence": "The posting is reachable and its domain has a positive employer/recruiting-platform signal; this is not proof of authenticity.",
            }
        return {
            "score": 65,
            "eligible": True,
            "evidence": "The posting is reachable, but the employer link is unverified. Confirm it on the employer's official careers page before sharing personal information.",
        }


class GeneralSupremeJury:
    name = "Job Match Jury"
    role = "Evidence-Based Recommendation Panel"

    def __init__(self):
        self.agents = (
            RoleAlignmentAgent(),
            ExperiencePreferencesAgent(),
            PostingQualityAgent(),
        )

    def deliberate(self, job: dict, profile: dict) -> dict:
        role_fit, preferences, posting = (
            self.agents[0].evaluate(job, profile),
            self.agents[1].evaluate(job, profile),
            self.agents[2].evaluate(job),
        )
        arguments = [
            {
                "agent_name": self.agents[0].name,
                "agent_role": self.agents[0].role,
                "argument_type": "role_fit",
                "statement": role_fit["evidence"],
                "score": role_fit["score"],
            },
            {
                "agent_name": self.agents[1].name,
                "agent_role": self.agents[1].role,
                "argument_type": "candidate_preferences",
                "statement": preferences["evidence"],
                "score": preferences["score"],
            },
            {
                "agent_name": self.agents[2].name,
                "agent_role": self.agents[2].role,
                "argument_type": "posting_quality",
                "statement": posting["evidence"],
                "score": posting["score"],
            },
        ]
        if not preferences["eligible"]:
            cross_exam = (
                "The profile-preference objection takes priority over a title or skill overlap: "
                + preferences["evidence"]
            )
        elif not posting["eligible"]:
            cross_exam = (
                "A role match cannot override posting-quality concerns: "
                + posting["evidence"]
            )
        elif role_fit["score"]:
            cross_exam = (
                "The match is based on text signals, not a full assessment of qualifications. "
                "Check required credentials, responsibilities, location, and employer details."
            )
        else:
            cross_exam = (
                "The role-fit reviewer found no configured role or skill overlap. "
                "If your profile is incomplete, update it before ruling this job out."
            )
        arguments.append({
            "agent_name": "Independent Cross-Examiner",
            "agent_role": "Challenge Round",
            "argument_type": "cross_examination",
            "statement": cross_exam,
            "score": min(role_fit["score"], preferences["score"], posting["score"]),
        })
        selected = role_fit["score"] > 0 and preferences["eligible"] and posting["eligible"]
        consensus = (
            round(role_fit["score"] * 0.55 + preferences["score"] * 0.25 + posting["score"] * 0.20)
            if selected else 0
        )
        if not posting["eligible"]:
            verdict = "NOT RECOMMENDED — POSTING NOT VERIFIED"
        elif not preferences["eligible"]:
            verdict = "NOT RECOMMENDED — PROFILE PREFERENCE MISMATCH"
        elif not role_fit["score"]:
            verdict = "NOT RECOMMENDED — LOW ROLE MATCH"
        elif consensus >= 70:
            verdict = "STRONG MATCH"
        else:
            verdict = "POSSIBLE MATCH — REVIEW DETAILS"
        arguments.append({
            "agent_name": self.name,
            "agent_role": self.role,
            "argument_type": "jury_verdict",
            "statement": (
                f"{verdict}. The panel weighs role alignment (55%), profile preferences (25%), "
                "and posting evidence (20%). The score is a guide, not a guarantee of hiring eligibility."
            ),
            "score": consensus,
        })
        return {
            "score": consensus,
            "selected": selected,
            "verdict": verdict,
            "recommendation": (
                "Review the full requirements, confirm the opening on the employer's careers page, "
                "and apply directly if it is still a fit."
                if selected else "Use the reviewer notes to decide whether to update your profile or skip this listing."
            ),
            "arguments": arguments,
        }


async def run_general_refresh(conn: sqlite3.Connection, profile: dict) -> dict:
    discovered = await run_general_discovery_workers()
    existing_rows = conn.execute("SELECT * FROM jobs WHERE status!='dismissed'").fetchall()
    existing_by_url = {row["url"]: dict(row) for row in existing_rows if row["url"]}
    candidates: list[dict] = []
    seen_urls: set[str] = set()

    for job in discovered:
        url = job.get("url", "")
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        previous = existing_by_url.get(url)
        job["status"] = previous.get("status", "new") if previous else "new"
        candidates.append(job)
    for url, previous in existing_by_url.items():
        if url not in seen_urls:
            candidates.append(previous)

    await verify_listing_availability(candidates)
    jury = GeneralSupremeJury()
    eligible = []
    for job in candidates:
        decision = jury.deliberate(job, profile)
        job["jury_score"] = decision["score"]
        job["score"] = job["jury_score"]
        job["is_daily_pick"] = int(decision["selected"])
        job["jury_verdict"] = decision["verdict"]
        job["jury_recommendation"] = decision["recommendation"]
        job["jury_arguments"] = decision["arguments"]
        if decision["selected"]:
            eligible.append(job)
    eligible.sort(key=lambda job: job["jury_score"], reverse=True)
    ranked_recommendations = [job for job in eligible if job["jury_score"] > 0][:5]
    selected_ids = {job.get("id") for job in ranked_recommendations}

    now = datetime.now(timezone.utc).isoformat()
    session_id = f"{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    conn.execute("UPDATE jobs SET is_daily_pick=0")
    for job in candidates:
        job_id = job.get("id")
        if not job_id:
            continue
        is_pick = int(job_id in selected_ids)
        conn.execute(
            """INSERT INTO jobs (
                 id, title, company, location, url, source, description, visa_evidence,
                 housing_evidence, score, jury_score, is_daily_pick, advocate_agent,
                 jury_verdict, liveness_status, liveness_checked_at, authenticity_status,
                 authenticity_evidence, posted_date, status, created_at, worker_scout
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                 title=excluded.title, company=excluded.company, location=excluded.location,
                 url=excluded.url, source=excluded.source, description=excluded.description,
                 visa_evidence=excluded.visa_evidence, housing_evidence=excluded.housing_evidence,
                 score=excluded.score, jury_score=excluded.jury_score,
                 is_daily_pick=excluded.is_daily_pick, jury_verdict=excluded.jury_verdict,
                 liveness_status=excluded.liveness_status,
                 liveness_checked_at=excluded.liveness_checked_at,
                 authenticity_status=excluded.authenticity_status,
                 authenticity_evidence=excluded.authenticity_evidence,
                 posted_date=excluded.posted_date, worker_scout=excluded.worker_scout""",
            (
                job_id, job.get("title", ""), job.get("company", ""), job.get("location", ""),
                job.get("url", ""), job.get("source", "Scout"), job.get("description", ""),
                job.get("visa_evidence", "Not verified"), job.get("housing_evidence", "Not verified"),
                job["score"], job["jury_score"], is_pick, "", job["jury_verdict"],
                job.get("liveness_status", "unverified"), job.get("liveness_checked_at"),
                job.get("authenticity_status", "unverified"),
                job.get("authenticity_evidence", "Not verified"),
                job.get("posted_date", "Recently posted"), job.get("status", "new"), now,
                job.get("worker_scout", ""),
            ),
        )
        for argument in job["jury_arguments"]:
            conn.execute(
                """INSERT INTO agent_arguments
                   (deliberation_id, job_id, agent_name, agent_role, argument_type,
                    statement, score, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    session_id, job_id, argument["agent_name"], argument["agent_role"],
                    argument["argument_type"], argument["statement"], argument["score"], now,
                ),
            )
        conn.execute(
            """INSERT INTO jury_verdicts
               (deliberation_id, job_id, verdict, consensus_score, recommendation,
                is_selected, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                session_id, job_id, job["jury_verdict"], job["jury_score"],
                job["jury_recommendation"], is_pick, now,
            ),
        )

    count = len(selected_ids)
    conn.execute(
        """INSERT INTO deliberations (id, session_date, summary, top_picks_count, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (session_id, now[:10], f"Refreshed and ranked {len(candidates)} jobs for this profile.", count, now),
    )
    return {"session_id": session_id, "crowned_count": count, "jobs": candidates}


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_auth_db()
    yield


app = FastAPI(title="Job Scout", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")


@app.middleware("http")
async def load_user_session(request: Request, call_next):
    request.state.user = None
    request.state.csrf_token = request.cookies.get(CSRF_COOKIE, "")
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        with get_auth_db() as conn:
            session = conn.execute(
                """SELECT s.csrf_token, s.expires_at, u.id, u.email, u.name
                   FROM auth_sessions s JOIN users u ON u.id=s.user_id
                   WHERE s.token_hash=?""",
                (token_digest(token),),
            ).fetchone()
            if session:
                if session["expires_at"] > datetime.now(timezone.utc).isoformat():
                    request.state.user = {
                        "id": session["id"], "email": session["email"], "name": session["name"]
                    }
                    request.state.csrf_token = session["csrf_token"]
                else:
                    conn.execute(
                        "DELETE FROM auth_sessions WHERE token_hash=?", (token_digest(token),)
                    )
    public_paths = {"/", "/login", "/register", "/health"}
    if not request.state.user and request.url.path not in public_paths and not request.url.path.startswith("/static/"):
        return RedirectResponse("/login", status_code=303)
    return await call_next(request)


@app.get("/", response_class=HTMLResponse)
def root(request: Request):
    return RedirectResponse("/dashboard" if request.state.user else "/login", status_code=303)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if request.state.user:
        return RedirectResponse("/dashboard", status_code=303)
    csrf_token = request.state.csrf_token or secrets.token_urlsafe(32)
    response = auth_page("Welcome back", "/login", csrf_token)
    response.set_cookie(CSRF_COOKIE, csrf_token, httponly=True, secure=request.url.scheme == "https",
                        samesite="lax", path="/")
    return response


@app.post("/login")
def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    csrf_token: str = Form(...),
):
    validate_csrf(request, csrf_token)
    with get_auth_db() as conn:
        user = conn.execute("SELECT * FROM users WHERE email=? COLLATE NOCASE", (email.strip(),)).fetchone()
    if not user or not verify_password(password, user["password_hash"]):
        return auth_page(
            "Welcome back", "/login", request.state.csrf_token,
            "Email or password is incorrect.",
        )
    session_token = secrets.token_urlsafe(48)
    csrf = secrets.token_urlsafe(32)
    expires = (datetime.now(timezone.utc) + timedelta(seconds=SESSION_MAX_AGE)).isoformat()
    with get_auth_db() as conn:
        conn.execute(
            "INSERT INTO auth_sessions (token_hash, user_id, csrf_token, expires_at) VALUES (?, ?, ?, ?)",
            (token_digest(session_token), user["id"], csrf, expires),
        )
    response = RedirectResponse("/dashboard", status_code=303)
    set_auth_cookie(response, session_token, request)
    response.delete_cookie(CSRF_COOKIE, path="/")
    return response


@app.get("/register", response_class=HTMLResponse)
def register_page(request: Request):
    if request.state.user:
        return RedirectResponse("/dashboard", status_code=303)
    csrf_token = request.state.csrf_token or secrets.token_urlsafe(32)
    response = auth_page("Create your account", "/register", csrf_token)
    response.set_cookie(CSRF_COOKIE, csrf_token, httponly=True, secure=request.url.scheme == "https",
                        samesite="lax", path="/")
    return response


@app.post("/register")
def register(
    request: Request,
    name: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    csrf_token: str = Form(...),
):
    validate_csrf(request, csrf_token)
    clean_name = name.strip()
    clean_email = email.strip().lower()
    if not clean_name or len(clean_name) > 100 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", clean_email):
        return auth_page("Create your account", "/register", request.state.csrf_token,
                         "Enter your name and a valid email address.")
    if len(password) < 12:
        return auth_page("Create your account", "/register", request.state.csrf_token,
                         "Choose a password with at least 12 characters.")
    if len(password) > 128:
        return auth_page("Create your account", "/register", request.state.csrf_token,
                         "Choose a password with no more than 128 characters.")

    user_id = str(uuid.uuid4())
    with get_auth_db() as conn:
        try:
            conn.execute(
                "INSERT INTO users (id, email, name, password_hash, created_at) VALUES (?, ?, ?, ?, ?)",
                (user_id, clean_email, clean_name, hash_password(password),
                 datetime.now(timezone.utc).isoformat()),
            )
        except sqlite3.IntegrityError:
            return auth_page("Create your account", "/register", request.state.csrf_token,
                             "An account with that email already exists.")

    with get_user_db(user_id) as conn:
        conn.execute("UPDATE profile SET name=? WHERE id=1", (clean_name,))
    session_token = secrets.token_urlsafe(48)
    csrf = secrets.token_urlsafe(32)
    expires = (datetime.now(timezone.utc) + timedelta(seconds=SESSION_MAX_AGE)).isoformat()
    with get_auth_db() as conn:
        conn.execute(
            "INSERT INTO auth_sessions (token_hash, user_id, csrf_token, expires_at) VALUES (?, ?, ?, ?)",
            (token_digest(session_token), user_id, csrf, expires),
        )
    response = RedirectResponse("/dashboard", status_code=303)
    set_auth_cookie(response, session_token, request)
    response.delete_cookie(CSRF_COOKIE, path="/")
    return response


@app.post("/logout")
def logout(request: Request, csrf_token: str = Form(...)):
    user, _ = user_context(request)
    validate_csrf(request, csrf_token)
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        with get_auth_db() as conn:
            conn.execute("DELETE FROM auth_sessions WHERE token_hash=?", (token_digest(token),))
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")
    return response


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, status: str = "all", q: str = "", msg: str = ""):
    user, csrf = user_context(request)
    with get_user_db(user["id"]) as conn:
        profile = conn.execute("SELECT * FROM profile WHERE id=1").fetchone()
        profile_ready = any(
            (profile[field] or "").strip()
            for field in ("target_roles", "skills", "cv_text")
        )
        counts = {
            row["status"]: row["count"]
            for row in conn.execute(
                """SELECT status, COUNT(*) AS count FROM jobs
                   WHERE jury_score>0 AND authenticity_status!='suspicious'
                   GROUP BY status"""
            )
        }
        picks = conn.execute(
            """SELECT * FROM jobs WHERE is_daily_pick=1 AND status!='dismissed'
               AND authenticity_status!='suspicious'
               ORDER BY jury_score DESC LIMIT 5"""
        ).fetchall()
        query = """SELECT * FROM jobs WHERE status!='dismissed' AND jury_score>0
                   AND authenticity_status!='suspicious'"""
        params: list[str] = []
        if status in {"new", "shortlisted", "applied"}:
            query += " AND status=?"
            params.append(status)
        if q.strip():
            query += " AND (title LIKE ? OR company LIKE ? OR location LIKE ? OR description LIKE ?)"
            term = f"%{q.strip()}%"
            params.extend([term] * 4)
        query += " ORDER BY is_daily_pick DESC, jury_score DESC, created_at DESC LIMIT 60"
        jobs = conn.execute(query, params).fetchall()
        latest = conn.execute(
            "SELECT session_date FROM deliberations ORDER BY created_at DESC LIMIT 1"
        ).fetchone()

    flash = f"<div class='notice notice-info'>{esc(msg)}</div>" if msg else ""
    pick_html = "".join(
        f"""<article class='daily-card'>
          <div class='daily-crown'>RECOMMENDED JOB</div>
          <h3>{esc(job['title'])}</h3>
          <p class='company-loc'>{esc(job['company'])} · {esc(job['location'] or 'Location not listed')}</p>
          <p><span class='score-badge'>{job['jury_score'] or job['score'] or 0}/100 match</span>
             Posting: {esc(job['liveness_status'] or 'unverified').replace('_', ' ').title()} ·
             Employer: {esc(job['authenticity_status'] or 'unverified').replace('_', ' ').title()}</p>
          <a href='/jury#{esc(job['id'])}'>Read Jury notes →</a>
          <div class='job-footer'>{job_link(job['id'], job['liveness_status'])}
            {status_action(job['id'], job['status'], csrf)}</div>
        </article>"""
        for job in picks
    )
    if not picks:
        pick_html = """<div class='empty-state'><h3>Your recommendations will appear here</h3>
          <p>Add your skills and the roles you want in your profile, then refresh to find matching jobs.</p>
          <a class='btn btn-secondary' href='/profile'>Set up my profile</a></div>"""

    job_cards = "".join(render_job(job, csrf) for job in jobs)
    if not job_cards:
        job_cards = "<div class='empty-state'><h3>No matching jobs yet</h3><p>Try a different search or refresh your job matches.</p></div>"
    show_results = profile_ready or bool(picks or jobs or counts)
    if profile_ready:
        hero_actions = f"""
          <form method='post' action='/refresh' data-job-refresh>
            <input type='hidden' name='csrf_token' value='{esc(csrf)}'>
            <button type='submit' class='btn btn-gold'>Find and refresh jobs</button>
            <span class='sr-only' data-refresh-status aria-live='polite'></span>
          </form>
          <a href='/profile' class='btn btn-secondary'>Update my profile</a>
        """
        refresh_note = f"Last refreshed: {esc(latest['session_date'] if latest else 'Not yet refreshed')}"
    else:
        hero_actions = "<a href='/profile' class='btn btn-gold'>Set up my job preferences</a>"
        refresh_note = "Add a target role, skill, or resume details to start your search."

    if show_results:
        recommendations_html = f"""
          <section class='daily-section'>
            <div class='daily-header'><div><p class='eyebrow'>PERSONALIZED FOR YOU</p><h2>Recommended jobs</h2></div></div>
            <div class='daily-grid'>{pick_html}</div>
          </section>
        """
        activity_html = f"""
          <section class='stats'>
            <div><strong>{sum(counts.values())}</strong><small>Jobs in your list</small></div>
            <div><strong>{counts.get('shortlisted', 0)}</strong><small>Saved</small></div>
            <div><strong>{counts.get('applied', 0)}</strong><small>Applied</small></div>
          </section>
          <div class='filter-bar'>
            <div class='tabs'>
              <a class='tab {'active' if status == 'all' else ''}' href='/dashboard'>All</a>
              <a class='tab {'active' if status == 'new' else ''}' href='/dashboard?status=new'>New</a>
              <a class='tab {'active' if status == 'shortlisted' else ''}' href='/dashboard?status=shortlisted'>Saved</a>
              <a class='tab {'active' if status == 'applied' else ''}' href='/dashboard?status=applied'>Applied</a>
            </div>
            <form class='search-box' method='get' action='/dashboard'>
              <input type='hidden' name='status' value='{esc(status)}'>
              <input name='q' value='{esc(q)}' placeholder='Search jobs, companies, or locations'>
              <button type='submit' class='btn-secondary btn-sm'>Search</button>
            </form>
          </div>
          <div class='sectionhead'><div><p class='eyebrow'>JOB LISTINGS</p><h2>All matching jobs</h2></div></div>
          <div class='jobs'>{job_cards}</div>
        """
    else:
        recommendations_html = ""
        activity_html = ""
    return HTMLResponse(page_layout(f"""
      {flash}
      <section class='hero'>
        <p class='eyebrow'>YOUR NEXT OPPORTUNITY</p>
        <h1>Find a job that fits <em>{esc(profile['name'])}</em></h1>
        <p>Recommendations use the skills, roles, location, and experience you choose. Review every posting and apply directly with the employer.</p>
        <div class='hero-actions'>{hero_actions}</div>
        <p class='refresh-note'>{refresh_note}</p>
      </section>
      {recommendations_html}
      {activity_html}
    """, csrf, "dashboard"))


@app.get("/jury", response_class=HTMLResponse)
def jury_room(request: Request):
    user, csrf = user_context(request)
    with get_user_db(user["id"]) as conn:
        round_row = conn.execute(
            "SELECT * FROM deliberations ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        if round_row:
            session_id = round_row["id"]
            rows = conn.execute(
                """SELECT j.*, v.verdict AS final_verdict, v.consensus_score,
                          v.recommendation, v.is_selected
                   FROM jury_verdicts v
                   JOIN jobs j ON j.id=v.job_id
                   WHERE v.deliberation_id=?
                   ORDER BY v.is_selected DESC, v.consensus_score DESC, j.title COLLATE NOCASE""",
                (session_id,),
            ).fetchall()
            arguments = conn.execute(
                """SELECT * FROM agent_arguments WHERE deliberation_id=?
                   ORDER BY id ASC""",
                (session_id,),
            ).fetchall()
            sources = conn.execute(
                """SELECT j.source, j.worker_scout, COUNT(*) AS count
                   FROM jury_verdicts v JOIN jobs j ON j.id=v.job_id
                   WHERE v.deliberation_id=?
                   GROUP BY j.source, j.worker_scout
                   ORDER BY count DESC""",
                (session_id,),
            ).fetchall()
        else:
            rows = []
            arguments = []
            sources = []

    args_by_job: dict[str, list[sqlite3.Row]] = {}
    for argument in arguments:
        args_by_job.setdefault(argument["job_id"], []).append(argument)

    source_counts = {
        source["worker_scout"]: source["count"]
        for source in sources if source["worker_scout"]
    }
    source_summary = "".join(
        f"<span class='pill'>{esc(name)}: {source_counts.get(name, 0)} listings</span>"
        for name in (PublicJobBoardScout.name, RemoteOpportunityScout.name)
    )
    if not round_row:
        source_summary += "<p class='muted'>No worker results yet. Add a target role or skill to your profile, then refresh job matches.</p>"

    agent_cards = [
        (RoleAlignmentAgent(), "Compares your chosen target roles and skills with each job description."),
        (ExperiencePreferencesAgent(), "Checks your experience level and honors the exclusions you set."),
        (PostingQualityAgent(), "Checks posting availability and reports whether the employer link is verified."),
    ]
    agent_html = "".join(
        f"""<div class='agent-card'>
          <div class='agent-card-header'><div class='agent-avatar'>{agent.avatar}</div>
            <div><div class='agent-name'>{esc(agent.name)}</div>
              <div class='agent-role'>{esc(agent.role)}</div></div></div>
          <p class='agent-desc'>{esc(description)}</p>
        </div>"""
        for agent, description in agent_cards
    )

    debate_cards = []
    for job in rows:
        job_id = esc(job["id"])
        speeches = "".join(
            f"""<div class='debate-speech'>
              <div class='debate-avatar'>{'⚖️' if argument['argument_type'] == 'jury_verdict' else '🧑‍💼'}</div>
              <div class='speech-bubble'>
                <div class='speech-author'><span>{esc(argument['agent_name'])}
                  <small>({esc(argument['agent_role'])})</small></span>
                  <span class='pill'>{esc(argument['argument_type'].replace('_', ' ').title())} · {argument['score']}/100</span>
                </div>
                <div>{esc(argument['statement'])}</div>
              </div>
            </div>"""
            for argument in args_by_job.get(job["id"], [])
        )
        selected = job["is_selected"] == 1
        debate_cards.append(f"""
        <article class='deliberation-item general-deliberation' id='{job_id}'>
          <div class='delib-header'>
            <div>
              <div class='pills'>
                <span class='verdict-tag'>{'Recommended' if selected else 'Reviewed'}</span>
                <span class='pill'>{esc(job['worker_scout'] or job['source'])}</span>
                <span class='pill'>{esc(job['liveness_status'] or 'unverified').replace('_', ' ').title()}</span>
                <span class='pill'>Employer {esc(job['authenticity_status'] or 'unverified').replace('_', ' ')}</span>
              </div>
              <h3>{esc(job['title'])}</h3>
              <p class='company-loc'>{esc(job['company'])} · {esc(job['location'] or 'Location not listed')}</p>
            </div>
            <span class='score-badge'>{job['consensus_score'] or 0}/100 match</span>
          </div>
          <div class='debate-feed'>
            <strong>Reviewer discussion</strong>
            {speeches or '<p class="muted">No reviewer notes were recorded for this job.</p>'}
          </div>
          <div class='verdict-box'>
            <div class='verdict-header'><span>JURY VERDICT</span><span>{esc(job['final_verdict'])}</span></div>
            <p class='verdict-action'><b>Next step:</b> {esc(job['recommendation'])}</p>
          </div>
          <div class='job-footer'>{job_link(job['id'], job['liveness_status'])}
            {status_action(job['id'], job['status'], csrf)}</div>
        </article>""")

    latest = ""
    if round_row:
        latest = f"""<section class='deliberation-box'>
          <p class='eyebrow'>LATEST REVIEW · {esc(round_row['session_date'])}</p>
          <p>{esc(round_row['summary'])}</p>
          <p><b>{round_row['top_picks_count']} recommended</b> from {len(rows)} reviewed postings.</p>
        </section>"""
    else:
        latest = """<section class='empty-state'>
          <h2>No Jury review yet</h2>
          <p>Complete your job-search profile, then refresh job matches. The worker scouts and review agents will create your first session.</p>
          <a class='btn btn-secondary' href='/profile'>Set up my profile</a>
        </section>"""

    review_html = "".join(debate_cards) or ""
    return HTMLResponse(page_layout(f"""
      <section class='sectionhead'>
        <div><p class='eyebrow'>YOUR JOB SEARCH REVIEW</p><h1>Jury Room</h1></div>
        <a class='btn btn-secondary' href='/dashboard'>Back to job matches</a>
      </section>
      <p class='muted'>Independent reviewers explain role fit, profile preferences, and posting quality. Their scores are guidance—not a promise of eligibility or hiring.</p>
      <section class='job-workflow' data-job-workflow aria-labelledby='job-workflow-heading'>
        <div class='job-workflow-heading'>
          <div><p class='eyebrow'>HOW MATCHES ARE MADE</p><h2 id='job-workflow-heading'>From job search to your shortlist</h2></div>
          <span class='flow-live'><span></span> REVIEW PROCESS</span>
        </div>
        <div class='job-workflow-stages'>
          <div class='job-workflow-stage' data-job-stage='0'>
            <span class='job-workflow-icon' aria-hidden='true'>⌕</span>
            <strong>Scouts search</strong>
            <small>Look across public job boards</small>
          </div>
          <span class='job-workflow-arrow' aria-hidden='true'>→</span>
          <div class='job-workflow-stage' data-job-stage='1'>
            <span class='job-workflow-icon' aria-hidden='true'>◎</span>
            <strong>Reviewers compare</strong>
            <small>Check roles against your profile</small>
          </div>
          <span class='job-workflow-arrow' aria-hidden='true'>→</span>
          <div class='job-workflow-stage' data-job-stage='2'>
            <span class='job-workflow-icon' aria-hidden='true'>⚖</span>
            <strong>The Jury ranks</strong>
            <small>Weigh evidence and explain matches</small>
          </div>
        </div>
        <p class='job-workflow-status' data-job-workflow-status aria-live='polite'>Scouts find postings, reviewers check fit, and the Jury ranks your matches.</p>
      </section>
      <section class='deliberation-box'>
        <h2>Job scouts</h2>
        <p>Each refresh checks independent public feeds. Candidate-specific curated listings are not used in this general app.</p>
        <div class='pills'>{source_summary}</div>
      </section>
      <h2>Review panel</h2>
      <div class='bench-grid'>{agent_html}</div>
      {latest}
      <section class='deliberation-box'>
        <h2>Debates &amp; verdicts</h2>
        {review_html or "<p class='muted'>Your posting-by-posting debate and verdicts will appear here after the first refresh.</p>"}
      </section>
    """, csrf, "jury"))


def status_action(job_id: str, status: str, csrf: str) -> str:
    if status == "shortlisted":
        return "<span class='pill pill-amber'>Saved</span>"
    if status == "applied":
        return "<span class='pill pill-blue'>Applied</span>"
    return f"""<form method='post' action='/job/{esc(job_id)}/status'>
      <input type='hidden' name='csrf_token' value='{esc(csrf)}'>
      <input type='hidden' name='status' value='shortlisted'>
      <button class='btn-sm btn-gold' type='submit'>Save job</button></form>"""


def render_job(job: sqlite3.Row, csrf: str) -> str:
    status = job["status"] or "new"
    actions = status_action(job["id"], status, csrf)
    if status == "shortlisted":
        actions = f"""<span class='pill pill-amber'>Saved</span>
          <form method='post' action='/job/{esc(job['id'])}/status'>
            <input type='hidden' name='csrf_token' value='{esc(csrf)}'>
            <input type='hidden' name='status' value='new'>
            <button class='btn-sm btn-subtle' type='submit'>Remove saved</button></form>
          <form method='post' action='/job/{esc(job['id'])}/status'>
            <input type='hidden' name='csrf_token' value='{esc(csrf)}'>
            <input type='hidden' name='status' value='applied'>
            <button class='btn-sm' type='submit'>Mark applied</button></form>"""
    return f"""<article class='job'>
      <div class='job-header'>
        <div class='pills'><span class='pill'>{esc(job['source'])}</span>
          <span class='pill'>{esc(job['liveness_status'] or 'unverified').replace('_', ' ').title()}</span>
          <span class='pill'>Employer {esc(job['authenticity_status'] or 'unverified').replace('_', ' ')}</span>
          <span class='pill'>{esc(status).title()}</span></div>
        <span class='score-badge'>{job['jury_score'] or job['score'] or 0}/100 match</span>
      </div>
      <h3>{esc(job['title'])}</h3>
      <p class='company-loc'>{esc(job['company'])} · {esc(job['location'] or 'Location not listed')}</p>
      <p>{esc((job['description'] or '')[:360])}</p>
      <p><a href='/jury#{esc(job['id'])}'>Read Jury notes →</a></p>
      <div class='job-footer'>{job_link(job['id'], job['liveness_status'])}
        <div class='job-actions'>{actions}
          <form method='post' action='/job/{esc(job['id'])}/status'>
            <input type='hidden' name='csrf_token' value='{esc(csrf)}'>
            <input type='hidden' name='status' value='dismissed'>
            <button class='btn-sm btn-subtle' type='submit' title='Hide job'>Hide</button>
          </form>
        </div>
      </div>
    </article>"""


@app.get("/profile", response_class=HTMLResponse)
def profile_page(request: Request, msg: str = ""):
    user, csrf = user_context(request)
    with get_user_db(user["id"]) as conn:
        profile = dict(conn.execute("SELECT * FROM profile WHERE id=1").fetchone())
    analysis = analyze_cv(profile["cv_text"] or "") if profile["cv_text"] else {"skills": [], "roles": [], "summary": "Add CV details to see suggested skills and roles."}
    flash = f"<div class='notice notice-info'>{esc(msg)}</div>" if msg else ""
    return HTMLResponse(page_layout(f"""
      {flash}
      <section class='sectionhead'><div><p class='eyebrow'>YOUR PREFERENCES</p><h1>My job-search profile</h1></div></section>
      <p class='muted'>This profile belongs to your account. You control what to include and can change it at any time.</p>
      <section class='cv-analysis panel'><p class='eyebrow'>CV SUGGESTIONS</p>
        <h2>Suggested roles and skills</h2><p>{esc(analysis['summary'])}</p>
        <p><b>Skills:</b> {esc(', '.join(analysis['skills']) or 'No skills detected')}</p>
        <p><b>Roles:</b> {esc(', '.join(analysis['roles']) or 'No roles detected')}</p>
      </section>
      <form class='panel' method='post' action='/profile'>
        <input type='hidden' name='csrf_token' value='{esc(csrf)}'>
        <label>Name<input name='name' value='{esc(profile['name'])}' required maxlength='100'></label>
        <label>Location you prefer<input name='residence' value='{esc(profile['residence'])}' placeholder='City, country'></label>
        <label>Skills (comma-separated)<textarea name='skills' rows='3' placeholder='e.g. customer service, Python, project management'>{esc(profile['skills'])}</textarea></label>
        <label>Roles you want (comma-separated)<textarea name='target_roles' rows='3' placeholder='e.g. support specialist, junior developer'>{esc(profile['target_roles'])}</textarea></label>
        <label>Experience level<select name='experience_level'>
          <option value='any' {'selected' if profile['experience_level'] == 'any' else ''}>Any level</option>
          <option value='entry-level' {'selected' if profile['experience_level'] == 'entry-level' else ''}>Entry level</option>
        </select></label>
        <label>Skills to exclude (comma-separated)<input name='excluded_skills' value='{esc(profile['excluded_skills'])}' placeholder='Optional'></label>
        <label>Role types to exclude (comma-separated)<input name='excluded_job_terms' value='{esc(profile['excluded_job_terms'])}' placeholder='Optional'></label>
        <label>Resume text (optional)<textarea name='cv_text' rows='8' placeholder='Paste resume text to get local skill and role suggestions'>{esc(profile['cv_text'])}</textarea></label>
        <p class='muted'>Resume suggestions are generated locally from keywords; check them before relying on recommendations.</p>
        <button class='primary' type='submit'>Save my profile</button>
      </form>
    """, csrf, "profile"))


@app.post("/profile")
def save_profile(
    request: Request,
    csrf_token: str = Form(...),
    name: str = Form(...),
    residence: str = Form(""),
    citizenship: str = Form(""),
    degree: str = Form(""),
    skills: str = Form(""),
    target_roles: str = Form(""),
    experience_level: str = Form("any"),
    excluded_skills: str = Form(""),
    excluded_job_terms: str = Form(""),
    cv_text: str = Form(""),
):
    user, _ = user_context(request)
    validate_csrf(request, csrf_token)
    clean_name = name.strip()
    if not clean_name or len(clean_name) > 100:
        raise HTTPException(status_code=400, detail="Enter a name of 1 to 100 characters.")
    if experience_level not in {"any", "entry-level"}:
        raise HTTPException(status_code=400, detail="Choose a valid experience level.")
    with get_user_db(user["id"]) as conn:
        conn.execute(
            """UPDATE profile SET name=?, residence=?, citizenship=?, degree=?, skills=?,
               target_roles=?, experience_level=?, excluded_skills=?, excluded_job_terms=?,
               cv_text=?, cv_analysis=?, updated_at=? WHERE id=1""",
            (clean_name, residence.strip(), citizenship.strip(), degree.strip(), skills.strip(),
             target_roles.strip(), experience_level, excluded_skills.strip(), excluded_job_terms.strip(),
             cv_text[:100_000], "", datetime.now(timezone.utc).isoformat()),
        )
    with get_auth_db() as conn:
        conn.execute("UPDATE users SET name=? WHERE id=?", (clean_name, user["id"]))
    return RedirectResponse(
        "/profile?msg=" + quote_plus("Profile saved. Refresh job matches to apply your new preferences."),
        status_code=303,
    )


@app.post("/refresh")
async def refresh_jobs(request: Request, csrf_token: str = Form(...)):
    user, _ = user_context(request)
    validate_csrf(request, csrf_token)
    async with refresh_lock:
        with get_user_db(user["id"]) as conn:
            profile = dict(conn.execute("SELECT * FROM profile WHERE id=1").fetchone())
            if not any((profile.get("target_roles"), profile.get("skills"), profile.get("cv_text"))):
                return RedirectResponse(
                    "/profile?msg=" + quote_plus("Add a target role or skill first so jobs can be matched to you."),
                    status_code=303,
                )
            result = await run_general_refresh(conn, profile)
    count = result.get("crowned_count", 0)
    message = f"Refresh complete: found {count} recommended jobs."
    return RedirectResponse("/dashboard?msg=" + quote_plus(message), status_code=303)


@app.post("/job/{job_id}/status")
def update_job_status(
    job_id: str,
    request: Request,
    status: str = Form(...),
    csrf_token: str = Form(...),
):
    user, _ = user_context(request)
    validate_csrf(request, csrf_token)
    allowed = {"new", "shortlisted", "applied", "dismissed"}
    if status not in allowed:
        raise HTTPException(status_code=400, detail="Choose a valid job status.")
    with get_user_db(user["id"]) as conn:
        job = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not job:
            raise HTTPException(status_code=404, detail="Job not found in your account.")
        conn.execute("UPDATE jobs SET status=? WHERE id=?", (status, job_id))
        if status in {"shortlisted", "applied", "dismissed"}:
            event = {
                "shortlisted": "shortlist",
                "applied": "apply",
                "dismissed": "dismiss",
            }[status]
            record_interaction(conn, user["id"], job_id, event, dict(job))
    return RedirectResponse("/dashboard?status=" + status, status_code=303)


@app.get("/job/{job_id}/visit")
def visit_job(job_id: str, request: Request):
    user, _ = user_context(request)
    with get_user_db(user["id"]) as conn:
        job = conn.execute("SELECT url, liveness_status FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not job:
            raise HTTPException(status_code=404, detail="Job not found in your account.")
        if job["url"] and job["url"].startswith(("https://", "http://")):
            record_interaction(conn, user["id"], job_id, "click", dict(job))
            target = job["url"]
        else:
            target = "/dashboard?msg=" + quote_plus("This listing does not have a valid posting link.")
    return RedirectResponse(target, status_code=302)


@app.get("/health")
def health():
    return {"ok": True, "service": "job-scout", "multi_user": True}
