import asyncio
from datetime import datetime, timezone
from typing import Dict, Any, List
import sqlite3

from app.agents.workers import ArbeitnowScout, RemotiveScout, CuratedEmployerScout
from app.agents.jury import SupremeJuryAgent
from app.agents.learner import compute_learned_boost, init_learner_db

async def run_discovery_workers() -> List[Dict[str, Any]]:
    """Runs all Level-3 Worker Scouts concurrently to discover vacancies and verify liveness."""
    scouts = [
        CuratedEmployerScout(),
        ArbeitnowScout(),
        RemotiveScout()
    ]
    # Gather jobs from each worker
    tasks = [scout.fetch_jobs() for scout in scouts]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    all_jobs = []
    seen_urls = set()
    for res in results:
        if isinstance(res, list):
            for job in res:
                url = job.get("url", "")
                if url and url not in seen_urls:
                    seen_urls.add(url)
                    all_jobs.append(job)

    return all_jobs

async def run_jury_deliberation_workflow(db_conn: sqlite3.Connection, profile: Dict[str, Any]) -> Dict[str, Any]:
    """
    Executes the entire hierarchical Multi-Agent workflow:
    1. Worker Scouts discover fresh live vacancies.
    2. Candidate jobs are assembled.
    3. Supreme Jury conducts Sub-Agent debate and cross-examination.
    4. Verdicts are reached and Daily Roster is selected.
    5. Deliberations, arguments, and verdicts are persisted to SQLite.
    """
    # 1. Workers Scout
    scouted_jobs = await run_discovery_workers()

    # Also load existing un-dismissed jobs from DB so they can participate in the jury session
    existing_rows = db_conn.execute("SELECT * FROM jobs WHERE status != 'dismissed'").fetchall()
    existing_map = {r["url"]: dict(r) for r in existing_rows}

    # Merge scouted jobs with existing
    candidates_pool = []
    seen_urls = set()

    # Prioritize scouted jobs
    for j in scouted_jobs:
        u = j.get("url", "")
        if u and u not in seen_urls:
            seen_urls.add(u)
            # If already in db, keep its status
            if u in existing_map:
                j["status"] = existing_map[u].get("status", "new")
            else:
                j["status"] = "new"
            candidates_pool.append(j)

    # Add existing DB jobs that weren't in scouted batch
    for u, row_dict in existing_map.items():
        if u not in seen_urls:
            seen_urls.add(u)
            candidates_pool.append(row_dict)

    # 2. Compute Learned Behavioral Preferences for each candidate
    init_learner_db(db_conn)
    learned_boosts = {}
    for c_job in candidates_pool:
        boost, reasons = compute_learned_boost(db_conn, c_job)
        learned_boosts[c_job.get("id")] = (boost, reasons)

    # 3. Convene Supreme Jury with Trained Weights
    jury = SupremeJuryAgent()
    session_result = jury.run_full_session(candidates_pool, profile, max_daily_picks=5, learned_boosts=learned_boosts)

    session_id = session_result["session_id"]
    now_iso = datetime.now(timezone.utc).isoformat()

    # 3. Persist to DB
    # Create deliberations table if missing
    db_conn.executescript("""
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

    # Record deliberation session
    db_conn.execute("""
    INSERT OR REPLACE INTO deliberations (id, session_date, summary, top_picks_count, created_at)
    VALUES (?, ?, ?, ?, ?)
    """, (session_id, now_iso[:10], session_result["summary"], session_result["crowned_count"], now_iso))

    # Reset existing daily picks first
    db_conn.execute("UPDATE jobs SET is_daily_pick = 0")

    # Save each job, arguments, and verdicts
    for job in session_result["jobs"]:
        jid = job["id"]
        jury_score = job.get("jury_score", 0)
        is_daily_pick = job.get("is_daily_pick", 0)
        verdict_status = job.get("verdict_status", "")
        advocate = job.get("advocate_agent", "")
        visa_ev = job.get("visa_evidence", "Not verified")
        housing_ev = job.get("housing_evidence", "Not verified")
        liveness = job.get("liveness_status", "live")
        posted = job.get("posted_date", "Recently posted")
        status = job.get("status", "new")

        # Insert or update job
        db_conn.execute("""
        INSERT INTO jobs (id, title, company, location, url, source, description,
                          visa_evidence, housing_evidence, score, jury_score, is_daily_pick,
                          advocate_agent, jury_verdict, liveness_status, posted_date, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
          jury_score=excluded.jury_score,
          score=excluded.score,
          is_daily_pick=excluded.is_daily_pick,
          advocate_agent=excluded.advocate_agent,
          jury_verdict=excluded.jury_verdict,
          visa_evidence=excluded.visa_evidence,
          housing_evidence=excluded.housing_evidence,
          liveness_status=excluded.liveness_status,
          posted_date=excluded.posted_date
        """, (
            jid, job.get("title", ""), job.get("company", ""), job.get("location", ""),
            job.get("url", ""), job.get("source", "Scout"), job.get("description", ""),
            visa_ev, housing_ev, jury_score, jury_score, is_daily_pick,
            advocate, verdict_status, liveness, posted, status, now_iso
        ))

        # Save arguments for this job
        for arg in job.get("arguments", []):
            db_conn.execute("""
            INSERT INTO agent_arguments (deliberation_id, job_id, agent_name, agent_role, argument_type, statement, score, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (session_id, jid, arg["agent_name"], arg["agent_role"], arg["type"], arg["statement"], arg["score"], now_iso))

        # Save verdict
        db_conn.execute("""
        INSERT INTO jury_verdicts (deliberation_id, job_id, verdict, consensus_score, recommendation, is_selected, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (session_id, jid, verdict_status, jury_score, job.get("jury_recommendation", ""), is_daily_pick, now_iso))

    return session_result

if __name__ == "__main__":
    import os
    from pathlib import Path
    db_path = os.getenv("DATABASE_PATH", str(Path(__file__).resolve().parent.parent.parent / "jobs.db"))
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        p = con.execute("SELECT * FROM profile WHERE id=1").fetchone()
        if not p:
            print("No profile found. Please initialize DB first.")
        else:
            print("Convening Supreme Jury via CLI...")
            result = asyncio.run(run_jury_deliberation_workflow(con, dict(p)))
            con.commit()
            print(f"Jury session complete! Crowned {result.get('crowned_count', 0)} daily picks.")
    finally:
        con.close()
