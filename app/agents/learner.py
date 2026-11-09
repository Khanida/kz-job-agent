import sqlite3
import re
import json
from datetime import datetime, timezone
from typing import Dict, Any, List, Tuple

EVENT_WEIGHTS = {
    "click": 1.0,
    "view_debate": 1.5,
    "shortlist": 3.5,
    "apply": 6.0,
    "dismiss": -3.5
}

STOP_WORDS = {
    "and", "the", "with", "for", "our", "you", "your", "will", "are", "have", "that", "this",
    "from", "work", "join", "team", "looking", "seeking", "role", "position", "jobs", "hire",
    "about", "also", "into", "more", "their", "what", "which", "who", "whom"
}

def init_learner_db(conn: sqlite3.Connection):
    # Use individual execute() calls (not executescript) to avoid implicit
    # commit that would break an open WAL-mode transaction on the same conn.
    conn.execute("""
    CREATE TABLE IF NOT EXISTS interaction_events (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      session_id TEXT,
      job_id TEXT,
      event_type TEXT,
      metadata TEXT,
      created_at TEXT
    )""")
    conn.execute("""
    CREATE TABLE IF NOT EXISTS learned_weights (
      feature_type TEXT,
      feature_value TEXT,
      score_delta REAL DEFAULT 0.0,
      positive_hits INTEGER DEFAULT 0,
      negative_hits INTEGER DEFAULT 0,
      updated_at TEXT,
      PRIMARY KEY (feature_type, feature_value)
    )""")

def extract_features_from_job(job: Dict[str, Any]) -> List[Tuple[str, str]]:
    """Extracts granular features (keywords, locations, companies, sources) from a job."""
    features = []
    
    # 1. Company
    company = (job.get("company") or "").strip().lower()
    if company and company not in ["hiring company", "confidential"]:
        features.append(("company", company))
        
    # 2. Location tokens
    location = (job.get("location") or "").lower()
    for loc_token in ["london", "berlin", "amsterdam", "remote", "germany", "uk", "netherlands", "australia", "singapore", "worldwide"]:
        if loc_token in location:
            features.append(("location", loc_token))
            
    # 3. Source
    source = (job.get("source") or "").strip().lower()
    if source:
        features.append(("source", source))

    # 4. Tech / Title Keywords
    text = f"{job.get('title', '')} {job.get('description', '')}".lower()
    tech_keywords = [
        "python", "c++", "java", "flutter", "firebase", "embedded", "qa", "testing",
        "automation", "support", "distributed", "backend", "cloud", "api", "systems",
        "linux", "docker", "react", "fastapi", "junior", "graduate", "entry"
    ]
    for kw in tech_keywords:
        if kw == "c++":
            if "c++" in text:
                features.append(("keyword", kw))
        else:
            if re.search(r'\b' + re.escape(kw) + r'\b', text):
                features.append(("keyword", kw))

    return list(set(features))

def record_interaction(conn: sqlite3.Connection, session_id: str, job_id: str, event_type: str, job_data: Dict[str, Any]):
    """Records an interaction event and incrementally trains agent weights."""
    init_learner_db(conn)
    weight_delta = EVENT_WEIGHTS.get(event_type, 1.0)
    now_iso = datetime.now(timezone.utc).isoformat()

    # Log raw event
    conn.execute("""
    INSERT INTO interaction_events (session_id, job_id, event_type, metadata, created_at)
    VALUES (?, ?, ?, ?, ?)
    """, (session_id, job_id, event_type, json.dumps({
        "title": job_data.get("title", ""),
        "company": job_data.get("company", ""),
        "location": job_data.get("location", "")
    }), now_iso))

    # Update learned weights for each extracted feature
    features = extract_features_from_job(job_data)
    is_positive = weight_delta > 0

    for f_type, f_val in features:
        row = conn.execute("SELECT score_delta, positive_hits, negative_hits FROM learned_weights WHERE feature_type=? AND feature_value=?",
                           (f_type, f_val)).fetchone()
        if row:
            new_delta = round(row[0] + weight_delta, 2)
            pos = row[1] + (1 if is_positive else 0)
            neg = row[2] + (0 if is_positive else 1)
            conn.execute("""
            UPDATE learned_weights
            SET score_delta=?, positive_hits=?, negative_hits=?, updated_at=?
            WHERE feature_type=? AND feature_value=?
            """, (new_delta, pos, neg, now_iso, f_type, f_val))
        else:
            conn.execute("""
            INSERT INTO learned_weights (feature_type, feature_value, score_delta, positive_hits, negative_hits, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """, (f_type, f_val, round(weight_delta, 2), 1 if is_positive else 0, 0 if is_positive else 1, now_iso))

def compute_learned_boost(conn: sqlite3.Connection, job: Dict[str, Any]) -> Tuple[int, List[str]]:
    """Calculates score boost and learning rationale based on historical interactions."""
    init_learner_db(conn)
    features = extract_features_from_job(job)
    if not features:
        return 0, []

    total_boost = 0.0
    explanations = []

    for f_type, f_val in features:
        row = conn.execute("SELECT score_delta, positive_hits, negative_hits FROM learned_weights WHERE feature_type=? AND feature_value=?",
                           (f_type, f_val)).fetchone()
        if row and abs(row[0]) >= 2.0:
            delta = row[0]
            # Normalize influence
            contribution = max(min(delta * 0.8, 10.0), -10.0)
            total_boost += contribution
            
            if delta > 3.0:
                explanations.append(f"Trained match on '{f_val}' (+{int(contribution)}) from your previous selections")
            elif delta < -2.0:
                explanations.append(f"Trained penalty on '{f_val}' ({int(contribution)}) based on previous dismissals")

    clamped_boost = int(max(min(total_boost, 20.0), -20.0))
    return clamped_boost, explanations[:3]

def get_training_profile(conn: sqlite3.Connection) -> Dict[str, Any]:
    """Summarizes current agent training progress and learned user preferences."""
    init_learner_db(conn)
    total_events = conn.execute("SELECT COUNT(*) FROM interaction_events").fetchone()[0]
    clicks = conn.execute("SELECT COUNT(*) FROM interaction_events WHERE event_type='click'").fetchone()[0]
    shortlists = conn.execute("SELECT COUNT(*) FROM interaction_events WHERE event_type='shortlist'").fetchone()[0]
    applies = conn.execute("SELECT COUNT(*) FROM interaction_events WHERE event_type='apply'").fetchone()[0]
    dismisses = conn.execute("SELECT COUNT(*) FROM interaction_events WHERE event_type='dismiss'").fetchone()[0]

    top_keywords = conn.execute("""
    SELECT feature_value, score_delta, positive_hits FROM learned_weights
    WHERE feature_type='keyword' AND score_delta > 0
    ORDER BY score_delta DESC LIMIT 6
    """).fetchall()

    top_locations = conn.execute("""
    SELECT feature_value, score_delta, positive_hits FROM learned_weights
    WHERE feature_type='location' AND score_delta > 0
    ORDER BY score_delta DESC LIMIT 4
    """).fetchall()

    disliked = conn.execute("""
    SELECT feature_value, score_delta, negative_hits FROM learned_weights
    WHERE score_delta < 0
    ORDER BY score_delta ASC LIMIT 4
    """).fetchall()

    # Determine Agent Training Stage
    if total_events >= 20:
        stage = "Expert Trained"
        stage_desc = "Agents and Workers are closely attuned to your exact application, click, and role preferences."
    elif total_events >= 5:
        stage = "Adaptive Learning"
        stage_desc = "Agents are actively adapting scoring and debate pitches from your recent shortlists and clicks."
    else:
        stage = "Initial Calibration"
        stage_desc = "Collecting baseline click and shortlist signals. Interact with jobs to train your agents."

    return {
        "stage": stage,
        "stage_desc": stage_desc,
        "total_events": total_events,
        "clicks": clicks,
        "shortlists": shortlists,
        "applies": applies,
        "dismisses": dismisses,
        "top_keywords": [dict(r) for r in top_keywords],
        "top_locations": [dict(r) for r in top_locations],
        "disliked": [dict(r) for r in disliked]
    }

