import unittest
import asyncio
import json
import sqlite3
import httpx
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient
from app.main import app, extract_text_from_file, get_db, esc
from app.agents.cv_analyzer import (
    analyze_cv,
    assess_candidate_constraints,
    score_cv_match,
)
from app.agents.workers import BaseWorker, CuratedEmployerScout
from app.agents.orchestrator import run_jury_deliberation_workflow
from app.agents.sub_agents import SoftwareSystemsAgent, QualityOperationsAgent, RelocationVisaAgent
from app.agents.jury import SupremeJuryAgent

class TestMultiAgentJobSystem(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_health_check(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data.get("ok"))
        self.assertEqual(data.get("service"), "adeena-job-agent")
        self.assertEqual(data.get("agent_architecture"), "hierarchical-multi-agent-jury")

    def test_home_page_and_daily_picks(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("ADEENA'S JOB AGENT", response.text)
        self.assertIn("OPPORTUNITIES PIPELINE", response.text)
        self.assertIn("Daily Selection", response.text)
        self.assertIn("Refresh Today’s Top 5", response.text)
        self.assertIn("data-agent-flow", response.text)
        self.assertIn("Workers scout", response.text)
        self.assertIn("Sub-Agents debate", response.text)
        self.assertIn("Jury decides", response.text)
        self.assertIn("flow-delivery", response.text)
        self.assertIn("Found new jobs!", response.text)
        self.assertIn("Check the link!", response.text)

    def test_dashboard_explains_when_there_are_no_daily_picks(self):
        with get_db() as c:
            previous_picks = [
                row["id"] for row in c.execute(
                    "SELECT id FROM jobs WHERE is_daily_pick=1"
                ).fetchall()
            ]
            c.execute("UPDATE jobs SET is_daily_pick=0")

        try:
            response = self.client.get("/")
            self.assertEqual(response.status_code, 200)
            self.assertIn("The Jury's Daily Selection", response.text)
            self.assertIn("No jobs currently meet your Jury preferences", response.text)
            self.assertIn("prioritizes Jeddah, Saudi Arabia", response.text)
        finally:
            with get_db() as c:
                c.executemany(
                    "UPDATE jobs SET is_daily_pick=1 WHERE id=?",
                    [(job_id,) for job_id in previous_picks],
                )

    def test_project_uses_shared_pink_blue_peach_palette(self):
        response = self.client.get("/static/style.css")

        self.assertEqual(response.status_code, 200)
        self.assertIn("--bg: #fff9fb", response.text)
        self.assertIn("--primary: #790a3d", response.text)
        self.assertIn("--accent: #5366ec", response.text)
        self.assertIn("--blue-border: #9acafa", response.text)
        self.assertIn("--gold-border: #ffd09b", response.text)

    def test_daily_refresh_button_runs_jury_workflow(self):
        with patch("app.main.run_jury_deliberation_workflow", new_callable=AsyncMock) as workflow:
            workflow.return_value = {"crowned_count": 5}
            response = self.client.post("/daily-refresh", follow_redirects=False)

        self.assertEqual(response.status_code, 303)
        self.assertIn("/?msg=", response.headers["location"])
        workflow.assert_awaited_once()

    def test_daily_pick_links_to_job_posting(self):
        job_id = "daily-pick-link-test"
        with get_db() as c:
            previous_picks = [
                row["id"] for row in c.execute("SELECT id FROM jobs WHERE is_daily_pick=1").fetchall()
            ]
            c.execute("UPDATE jobs SET is_daily_pick=0")
            c.execute("""
                INSERT OR REPLACE INTO jobs
                (id, title, company, location, url, source, score, jury_score, is_daily_pick, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 'new', ?)
            """, (
                job_id, "Link Test Engineer", "Link Test Co", "Remote",
                "https://careers.example.com/jobs/123", "Test",
                90, 90, "2026-11-10T00:00:00+00:00"
            ))

        try:
            response = self.client.get("/")
            self.assertEqual(response.status_code, 200)
            self.assertIn(f"href='/track/click/{job_id}'", response.text)
            self.assertIn("Apply / View posting", response.text)
        finally:
            with get_db() as c:
                c.execute("DELETE FROM jobs WHERE id=?", (job_id,))
                c.executemany("UPDATE jobs SET is_daily_pick=1 WHERE id=?", [(pick_id,) for pick_id in previous_picks])

    def test_dashboard_marks_unavailable_jobs_and_hides_apply_link(self):
        job_id = "expired-job-link-test"
        with get_db() as c:
            c.execute("""
                INSERT OR REPLACE INTO jobs
                (id, title, company, location, url, source, score, jury_score, liveness_status, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'expired', 'new', ?)
            """, (
                job_id, "Expired Engineer", "Old Company", "Remote",
                "https://careers.example.com/jobs/expired", "Test",
                75, 75, "2026-11-10T00:00:00+00:00"
            ))

        try:
            response = self.client.get("/")
            self.assertIn("Posting link returned 404/410", response.text)
            self.assertIn("Posting link unavailable", response.text)
            self.assertNotIn(f"href='/track/click/{job_id}'", response.text)
        finally:
            with get_db() as c:
                c.execute("DELETE FROM jobs WHERE id=?", (job_id,))

    def test_jury_room_page(self):
        response = self.client.get("/jury")
        self.assertEqual(response.status_code, 200)
        self.assertIn("The Jury Room & Multi-Agent Deliberation", response.text)
        self.assertIn("Software & Systems Agent", response.text)
        self.assertIn("Quality & Operations Agent", response.text)
        self.assertIn("Visa & Relocation Advocate", response.text)
        self.assertIn("Supreme Jury Agent", response.text)

    def test_convene_jury_endpoint(self):
        response = self.client.post("/convene-jury", follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertIn("/jury?msg=", response.headers["location"])

    def test_sub_agents_and_challenges(self):
        profile = {
            "skills": "Python, C++, Java, Flutter",
            "target_roles": "Junior Software Engineer, QA Engineer"
        }
        test_job = {
            "id": "test_123",
            "title": "Junior Software Engineer (Python)",
            "company": "Tech Innovations",
            "location": "London, UK",
            "description": "Python, C++, microservices. Full visa sponsorship and relocation support provided."
        }

        sw_agent = SoftwareSystemsAgent()
        qa_agent = QualityOperationsAgent()
        visa_agent = RelocationVisaAgent()

        sw_eval = sw_agent.evaluate_job(test_job, profile)
        self.assertGreaterEqual(sw_eval["score"], 50)
        self.assertIn("engineering", sw_eval["advocacy_pitch"].lower())

        visa_eval = visa_agent.evaluate_job(test_job, profile)
        self.assertTrue(visa_eval["has_visa"])
        self.assertGreaterEqual(visa_eval["score"], 60)

        qa_eval = qa_agent.evaluate_job(test_job, profile)
        self.assertIsInstance(qa_eval["score"], int)

        # Cross-examination challenges
        challenge = visa_agent.challenge(test_job, sw_agent.name, profile)
        self.assertIn("Endorsement", challenge["stance"])

    def test_cv_analysis_recommends_roles_and_matches_job(self):
        cv_text = (
            "Graduate Embedded Systems Engineer with C++, Python, firmware, "
            "microcontroller and hardware interfacing experience."
        )
        analysis = analyze_cv(cv_text)

        self.assertIn("C++", analysis["skills"])
        self.assertIn("Embedded Systems", analysis["skills"])
        self.assertIn("Junior Embedded Systems Engineer", analysis["roles"])

        match = score_cv_match(
            {
                "title": "Junior Embedded Systems Engineer",
                "description": "C++ firmware and microcontroller development",
            },
            {"cv_text": cv_text, "cv_analysis": analysis, "skills": "C++, Python"},
        )
        self.assertIn("C++", match["matched_skills"])
        self.assertTrue(match["matched_roles"])
        self.assertGreaterEqual(match["score"], 70)
        self.assertNotIn(
            "Cybersecurity Analyst",
            analyze_cv("Python developer with backend API experience.")["roles"],
        )

    def test_candidate_preferences_reject_senior_rust_and_backend_roles(self):
        profile = {
            "experience_level": "entry-level",
            "excluded_skills": "Rust",
            "excluded_job_terms": "backend, back-end, full-stack, full stack",
            "residence": "Jeddah, Saudi Arabia",
            "citizenship": "Pakistani",
        }
        cases = [
            (
                {"title": "Senior Back-end Developer", "description": "Experienced engineer"},
                "senior, lead",
            ),
            (
                {"title": "Tier III Service Desk Engineer", "description": "Technical support role."},
                "advanced-tier",
            ),
            (
                {"title": "Rust Software Engineer", "description": "Build production software in Rust and C++."},
                "Rust",
            ),
            (
                {"title": "Junior Backend Developer", "description": "Entry-level role with mentorship."},
                "backend",
            ),
            (
                {"title": "Full-Stack Graduate Developer", "description": "Graduate role."},
                "full-stack",
            ),
            (
                {
                    "title": "Graduate Software Engineer",
                    "description": "This role is open to Saudi nationals only.",
                },
                "restricted to Saudi nationals",
            ),
        ]
        for job, reason in cases:
            with self.subTest(job=job["title"]):
                result = assess_candidate_constraints(job, profile)
                self.assertFalse(result["eligible"])
                self.assertTrue(any(reason in item for item in result["reasons"]))

        suitable = assess_candidate_constraints(
            {
                "title": "Graduate Embedded Systems Engineer",
                "description": "Entry-level role using Python and microcontrollers; Pakistani applicants welcome.",
                "location": "Jeddah, Saudi Arabia",
            },
            profile,
        )
        self.assertTrue(suitable["eligible"])
        self.assertEqual(suitable["preferred_city"], "Jeddah")
        self.assertTrue(suitable["matches_preferred_city"])

    def test_jury_rejects_jobs_outside_explicit_candidate_preferences(self):
        profile = {
            "skills": "Python, Embedded Systems",
            "target_roles": "Junior Embedded Systems Engineer",
            "citizenship": "Pakistani",
            "residence": "Jeddah, Saudi Arabia",
            "experience_level": "entry-level",
            "excluded_skills": "Rust",
            "excluded_job_terms": "backend, back-end, full-stack, full stack",
        }
        jobs = [
            {
                "id": "senior-rust-backend",
                "title": "Senior Rust Backend Engineer",
                "company": "Example Co",
                "location": "Remote",
                "description": "Requires at least 5 years of experience building backend services in Rust.",
                "liveness_status": "live",
                "authenticity_status": "likely_official",
            },
            {
                "id": "entry-embedded",
                "title": "Graduate Embedded Systems Engineer",
                "company": "Embedded Co",
                "location": "Remote",
                "description": "Entry-level role using Python and microcontrollers.",
                "liveness_status": "live",
                "authenticity_status": "likely_official",
            },
        ]

        results = {
            job["id"]: job
            for job in SupremeJuryAgent().run_full_session(jobs, profile)["jobs"]
        }

        rejected = results["senior-rust-backend"]
        self.assertEqual(rejected["jury_score"], 0)
        self.assertEqual(rejected["is_daily_pick"], 0)
        self.assertIn("CANDIDATE EXPERIENCE MISMATCH", rejected["verdict_status"])
        constraint_argument = next(
            arg for arg in rejected["arguments"]
            if arg["type"] == "candidate_constraints"
        )
        self.assertIn("Rust", constraint_argument["statement"])
        self.assertIn("entry-level", constraint_argument["statement"])
        self.assertEqual(results["entry-embedded"]["is_daily_pick"], 1)

    def test_jury_prioritizes_preferred_city_over_higher_scoring_fallback(self):
        profile = {
            "skills": "Python, Embedded Systems",
            "target_roles": "Junior Embedded Systems Engineer",
            "citizenship": "Pakistani",
            "residence": "Jeddah, Saudi Arabia",
        }
        jobs = [
            {
                "id": "fallback-city",
                "title": "Graduate Embedded Systems Engineer",
                "company": "Fallback Co",
                "location": "Remote",
                "description": "Entry-level Python and microcontroller engineering role.",
                "liveness_status": "live",
                "authenticity_status": "likely_official",
            },
            {
                "id": "preferred-city",
                "title": "Graduate Embedded Systems Engineer",
                "company": "Jeddah Co",
                "location": "Jeddah, Saudi Arabia",
                "description": "Entry-level Python and microcontroller engineering role; Pakistani applicants welcome.",
                "liveness_status": "live",
                "authenticity_status": "likely_official",
            },
        ]

        result = SupremeJuryAgent().run_full_session(jobs, profile)

        self.assertEqual(result["jobs"][0]["id"], "preferred-city")
        self.assertTrue(result["jobs"][0]["matches_preferred_city"])
        self.assertEqual(result["jobs"][0]["is_daily_pick"], 1)
        location_argument = next(
            arg for arg in result["jobs"][0]["arguments"]
            if arg["type"] == "location_preference"
        )
        self.assertIn("prioritize", location_argument["statement"].lower())

    def test_jeddah_job_requires_expat_eligibility_evidence(self):
        profile = {
            "citizenship": "Pakistani",
            "residence": "Jeddah, Saudi Arabia",
            "experience_level": "entry-level",
            "excluded_skills": "Rust",
            "excluded_job_terms": "backend, back-end, full-stack, full stack",
        }
        no_evidence = assess_candidate_constraints(
            {
                "title": "Junior QA Engineer",
                "description": "Entry-level manual testing role.",
                "location": "Jeddah, Saudi Arabia",
            },
            profile,
        )
        self.assertFalse(no_evidence["eligible"])
        self.assertEqual(no_evidence["local_expat_status"], "unverified")
        self.assertIn("Pakistani or other non-Saudi", no_evidence["local_expat_evidence"])

        pakistani_expats = assess_candidate_constraints(
            {
                "title": "Junior QA Engineer",
                "description": "Entry-level role. Pakistani applicants are welcome.",
                "location": "Jeddah, Saudi Arabia",
            },
            profile,
        )
        self.assertTrue(pakistani_expats["eligible"])
        self.assertEqual(pakistani_expats["local_expat_status"], "evidence_found")
        self.assertIn("does not independently confirm", pakistani_expats["local_expat_evidence"])

        transferable_iqama = assess_candidate_constraints(
            {
                "title": "Junior QA Engineer",
                "description": "Entry-level role; candidates must have a transferable iqama.",
                "location": "Jeddah, Saudi Arabia",
            },
            profile,
        )
        self.assertFalse(transferable_iqama["eligible"])
        self.assertEqual(transferable_iqama["local_expat_status"], "general_signal")
        self.assertIn("does not state that Pakistani applicants are eligible", transferable_iqama["local_expat_evidence"])

        restricted = assess_candidate_constraints(
            {
                "title": "Junior QA Engineer",
                "description": "Entry-level role for Saudi nationals only.",
                "location": "Jeddah, Saudi Arabia",
            },
            profile,
        )
        self.assertFalse(restricted["eligible"])
        self.assertEqual(restricted["local_expat_status"], "restricted")

    def test_jury_debates_unverified_pakistani_eligibility_for_jeddah_job(self):
        job = {
            "id": "jeddah-expat-unverified",
            "title": "Junior QA Engineer",
            "company": "Jeddah Employer",
            "location": "Jeddah, Saudi Arabia",
            "description": "Entry-level quality testing role with structured training.",
            "liveness_status": "live",
            "authenticity_status": "likely_official",
        }
        profile = {
            "citizenship": "Pakistani",
            "residence": "Jeddah, Saudi Arabia",
            "experience_level": "entry-level",
            "excluded_skills": "Rust",
            "excluded_job_terms": "backend, back-end, full-stack, full stack",
        }

        result = SupremeJuryAgent().run_full_session([job], profile)["jobs"][0]

        self.assertEqual(result["is_daily_pick"], 0)
        self.assertEqual(result["local_expat_status"], "unverified")
        self.assertIn("LOCAL EXPAT ELIGIBILITY UNVERIFIED", result["verdict_status"])
        expat_argument = next(
            argument for argument in result["arguments"]
            if argument["type"] == "local_expat_eligibility"
        )
        self.assertIn("Pakistani or other non-Saudi", expat_argument["statement"])

    def test_cv_match_influences_jury_ranking(self):
        profile = {
            "skills": "Flutter, Dart, Android, Firebase",
            "target_roles": "Junior Mobile App Developer",
            "cv_analysis": {
                "skills": ["Flutter", "Dart", "Android", "Firebase"],
                "roles": ["Junior Mobile App Developer"],
            },
        }
        jobs = [
            {
                "id": "mobile-role",
                "title": "Junior Mobile App Developer",
                "company": "Mobile Co",
                "location": "Remote",
                "description": "Flutter, Dart, Android, Firebase development",
                "liveness_status": "live",
            },
            {
                "id": "qa-role",
                "title": "QA Automation Engineer",
                "company": "QA Co",
                "location": "Remote",
                "description": "Java, Selenium, test automation",
                "liveness_status": "live",
            },
        ]

        result = SupremeJuryAgent().run_full_session(jobs, profile)

        self.assertEqual(result["jobs"][0]["id"], "mobile-role")
        cv_argument = next(
            arg for arg in result["jobs"][0]["arguments"] if arg["type"] == "cv_match"
        )
        self.assertIn("Flutter", cv_argument["statement"])

    def test_docx_cv_text_extraction(self):
        import io
        import zipfile

        document_xml = (
            b'<?xml version="1.0" encoding="UTF-8"?>'
            b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            b'<w:body><w:p><w:r><w:t>Python engineer</w:t></w:r></w:p></w:body></w:document>'
        )
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("word/document.xml", document_xml)

        self.assertEqual(extract_text_from_file("resume.docx", buffer.getvalue()), "Python engineer")

    def test_profile_save_analyzes_cv_and_updates_recommended_roles(self):
        with get_db() as c:
            previous_profile = dict(c.execute("SELECT * FROM profile WHERE id=1").fetchone())

        payload = {
            "name": "CV Analysis Test",
            "citizenship": "Test",
            "residence": "Remote",
            "degree": "Computer Engineering",
            "skills": "Manual Skills",
            "target_roles": "Manual Role",
            "cv_text": (
                "Graduate Embedded Systems Engineer with C++, Python, firmware, "
                "microcontroller and hardware interfacing experience."
            ),
        }
        try:
            with patch("app.main.refresh_daily_selection_if_due", new_callable=AsyncMock):
                response = self.client.post("/profile", data=payload, follow_redirects=False)
            self.assertEqual(response.status_code, 303)

            with get_db() as c:
                updated = c.execute(
                    "SELECT skills, target_roles, cv_analysis FROM profile WHERE id=1"
                ).fetchone()
                analysis = json.loads(updated["cv_analysis"])
            self.assertIn("Junior Embedded Systems Engineer", updated["target_roles"])
            self.assertIn("Embedded Systems", updated["skills"])
            self.assertEqual(analysis["roles"], updated["target_roles"].split(", "))
        finally:
            with get_db() as c:
                c.execute(
                    """UPDATE profile
                       SET name=?, citizenship=?, residence=?, degree=?, skills=?, target_roles=?,
                           cv_text=?, cv_analysis=?, experience_level=?, excluded_skills=?,
                           excluded_job_terms=?, updated_at=?
                       WHERE id=1""",
                    (
                        previous_profile["name"], previous_profile["citizenship"],
                        previous_profile["residence"], previous_profile["degree"],
                        previous_profile["skills"], previous_profile["target_roles"],
                        previous_profile["cv_text"], previous_profile.get("cv_analysis"),
                        previous_profile["experience_level"], previous_profile["excluded_skills"],
                        previous_profile["excluded_job_terms"],
                        previous_profile["updated_at"],
                    ),
                )

    def test_supreme_jury_deliberation(self):
        profile = {
            "skills": "Python, C++, Java, Flutter",
            "target_roles": "Junior Software Engineer, QA Engineer"
        }
        candidate_jobs = [
            {
                "id": "job_a",
                "title": "Graduate Systems Engineer",
                "company": "Top Global Corp",
                "location": "London, UK",
                "description": "Python, C++, distributed architecture. Complete visa sponsorship and relocation package.",
                "url": "https://example.com/job_a",
                "liveness_status": "live"
            },
            {
                "id": "job_b",
                "title": "Senior Staff Architect (10+ years)",
                "company": "Old Corp",
                "location": "Berlin, Germany",
                "description": "10+ years experience required. Must already have right to work, no sponsorship.",
                "url": "https://example.com/job_b",
                "liveness_status": "live"
            }
        ]

        jury = SupremeJuryAgent()
        session = jury.run_full_session(candidate_jobs, profile, max_daily_picks=1)
        self.assertEqual(len(session["jobs"]), 2)
        
        # High fit with sponsorship should rank first
        top_job = session["jobs"][0]
        self.assertEqual(top_job["id"], "job_a")
        self.assertEqual(top_job["is_daily_pick"], 1)
        self.assertGreaterEqual(top_job["jury_score"], 65)

        # Job B with blocker should be penalized
        blocked_job = session["jobs"][1]
        self.assertEqual(blocked_job["id"], "job_b")
        self.assertEqual(blocked_job["is_daily_pick"], 0)
        self.assertIn("REJECTED", blocked_job["verdict_status"])

    def test_majority_approved_live_job_is_crowned_as_daily_pick(self):
        majority_approval = {
            "consensus_score": 55,
            "verdict_status": "APPROVED BY MAJORITY VOTE",
            "summary": "Majority-approved candidate.",
            "recommendation": "Review the posting and apply.",
            "advocate_agent": "Test Advocate",
            "visa_evidence": "Not verified",
            "housing_evidence": "Not verified",
            "arguments": [],
            "preferred_city": "",
            "matches_preferred_city": False,
            "local_expat_status": "not_applicable",
            "local_expat_evidence": "",
        }
        with patch.object(
            SupremeJuryAgent, "deliberate_job", return_value=majority_approval
        ):
            session = SupremeJuryAgent().run_full_session(
                [{
                    "id": "majority-live-job",
                    "title": "Junior Software Engineer",
                    "company": "Example Co",
                    "location": "Remote",
                    "liveness_status": "live",
                    "authenticity_status": "unverified",
                }],
                {},
            )

        self.assertEqual(session["crowned_count"], 1)
        self.assertEqual(session["jobs"][0]["is_daily_pick"], 1)

    def test_worker_scout(self):
        scout = CuratedEmployerScout()
        jobs = asyncio.run(scout.fetch_jobs())
        self.assertGreaterEqual(len(jobs), 5)
        first = jobs[0]
        self.assertIn("title", first)
        self.assertIn("company", first)
        self.assertIn("url", first)
        self.assertEqual(first["liveness_status"], "unverified")

    def test_listing_check_detects_404_and_closed_role(self):
        async def verify(url, status_code, body):
            async def handler(request):
                return httpx.Response(status_code, text=body)

            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                return await BaseWorker().check_liveness(client, url)

        self.assertEqual(
            asyncio.run(verify("https://example.com/missing", 404, "")),
            "expired"
        )
        self.assertEqual(
            asyncio.run(verify("https://example.com/closed", 200, "This job is no longer available")),
            "unavailable"
        )

    def test_listing_authenticity_uses_domain_signals_without_claiming_proof(self):
        assessor = BaseWorker()

        self.assertEqual(
            assessor.assess_authenticity(
                "https://jobs.example-company.com/opening", "Example Company"
            )["status"],
            "likely_official",
        )
        platform_result = assessor.assess_authenticity(
            "https://boards.greenhouse.io/example", "Example Company"
        )
        self.assertEqual(platform_result["status"], "likely_official")
        self.assertIn("confirm the employer", platform_result["evidence"])
        self.assertEqual(
            assessor.assess_authenticity(
                "https://unknown-host.invalid/opening", "Example Company"
            )["status"],
            "unverified",
        )
        self.assertEqual(
            assessor.assess_authenticity(
                "http://example-company.com/opening", "Example Company"
            )["status"],
            "suspicious",
        )

    def test_listing_authenticity_checks_final_redirect_destination(self):
        async def verify():
            async def handler(request):
                if request.url.path == "/redirect":
                    return httpx.Response(
                        302,
                        headers={"Location": "https://jobs.example-company.com/careers"},
                        request=request,
                    )
                return httpx.Response(200, text="Current opportunities", request=request)

            async with httpx.AsyncClient(
                transport=httpx.MockTransport(handler), follow_redirects=True
            ) as client:
                return await BaseWorker().check_listing(
                    client, "https://short.example/redirect", "Example Company"
                )

        result = asyncio.run(verify())
        self.assertEqual(result["liveness_status"], "live")
        self.assertEqual(result["authenticity_status"], "likely_official")

    def test_jury_rejects_suspicious_posting_destination(self):
        job = {
            "id": "suspicious-link",
            "title": "Graduate Software Engineer",
            "company": "Example Company",
            "location": "Remote",
            "url": "http://example-company.com/apply",
            "description": "Python and C++ engineering role with visa sponsorship and relocation",
            "liveness_status": "live",
            "authenticity_status": "suspicious",
            "authenticity_evidence": "The posting URL does not use a valid HTTPS destination.",
        }

        result = SupremeJuryAgent().run_full_session(
            [job],
            {"skills": "Python, C++", "target_roles": "Junior Software Engineer"},
        )["jobs"][0]

        self.assertEqual(result["is_daily_pick"], 0)
        self.assertEqual(result["jury_score"], 0)
        self.assertIn("SUSPICIOUS POSTING LINK", result["verdict_status"])
        authenticity_argument = next(
            arg for arg in result["arguments"] if arg["type"] == "authenticity_check"
        )
        self.assertIn("do not enter personal information", authenticity_argument["statement"])

    def test_jury_workflow_persists_authenticity_evidence(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute("""
            CREATE TABLE jobs (
                id TEXT PRIMARY KEY, title TEXT, company TEXT, location TEXT,
                url TEXT, source TEXT, description TEXT, visa_evidence TEXT,
                housing_evidence TEXT, score INTEGER, jury_score INTEGER,
                is_daily_pick INTEGER, advocate_agent TEXT, jury_verdict TEXT,
                liveness_status TEXT, liveness_checked_at TEXT,
                authenticity_status TEXT, authenticity_evidence TEXT,
                posted_date TEXT, status TEXT, created_at TEXT
            )
        """)
        job = {
            "id": "authenticity-persistence-test",
            "title": "Graduate Software Engineer",
            "company": "Example Company",
            "location": "Remote",
            "url": "https://careers.example-company.com/jobs/123",
            "source": "Test",
            "description": "Python and C++ engineering role",
            "status": "new",
        }

        async def mock_verification(jobs):
            for candidate in jobs:
                candidate.update({
                    "liveness_status": "live",
                    "authenticity_status": "likely_official",
                    "authenticity_evidence": "Employer-name domain match.",
                })

        try:
            with (
                patch(
                    "app.agents.orchestrator.run_discovery_workers",
                    new=AsyncMock(return_value=[job]),
                ),
                patch(
                    "app.agents.orchestrator.verify_listing_availability",
                    new=AsyncMock(side_effect=mock_verification),
                ),
            ):
                asyncio.run(run_jury_deliberation_workflow(
                    connection,
                    {"skills": "Python, C++", "target_roles": "Junior Software Engineer"},
                ))

            saved_job = connection.execute(
                "SELECT authenticity_status, authenticity_evidence FROM jobs WHERE id=?",
                (job["id"],),
            ).fetchone()
            self.assertEqual(saved_job["authenticity_status"], "likely_official")
            self.assertEqual(saved_job["authenticity_evidence"], "Employer-name domain match.")
            saved_argument = connection.execute(
                "SELECT argument_type FROM agent_arguments WHERE job_id=? AND argument_type='authenticity_check'",
                (job["id"],),
            ).fetchone()
            self.assertIsNotNone(saved_argument)
        finally:
            connection.close()

    def test_jury_excludes_unverified_listing_from_daily_picks(self):
        profile = {"skills": "Python, C++", "target_roles": "Junior Software Engineer"}
        jobs = [{
            "id": "unverified-job",
            "title": "Graduate Software Engineer",
            "company": "Example Co",
            "location": "Remote",
            "url": "https://example.com/job",
            "description": "Python and C++ engineering role with visa sponsorship and relocation",
            "liveness_status": "unverified"
        }]

        session = SupremeJuryAgent().run_full_session(jobs, profile)

        result = session["jobs"][0]
        self.assertEqual(result["is_daily_pick"], 0)
        self.assertIn("AVAILABILITY UNVERIFIED", result["verdict_status"])
        self.assertTrue(any(arg["type"] == "availability_check" for arg in result["arguments"]))

    def test_profile_save_and_re_debate(self):
        payload = {
            "name": "Adeena Khanzada",
            "citizenship": "Pakistani",
            "residence": "Jeddah, Saudi Arabia",
            "degree": "B.E. Computer Systems Engineering, MUET",
            "skills": "Python, C++, Java, Flutter, Firebase, FastAPI, QA, Embedded Systems",
            "target_roles": "Junior Software Engineer, QA Engineer, Embedded Systems Engineer",
            "cv_text": "Experienced junior engineer specializing in Python and embedded systems."
        }
        response = self.client.post("/profile", data=payload, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertIn("msg=", response.headers["location"])

    def test_settings_page(self):
        response = self.client.get("/settings")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Agent Architecture & Settings", response.text)
        self.assertIn("Convene Supreme Jury", response.text)

    def test_job_status_workflow(self):
        with get_db() as c:
            job = c.execute("SELECT id FROM jobs LIMIT 1").fetchone()
            self.assertIsNotNone(job, "Database should have at least one seed or scanned job")
            jid = job["id"]

        # Shortlist
        res = self.client.post(f"/job/{jid}/status", data={"status": "shortlisted"}, follow_redirects=False)
        self.assertEqual(res.status_code, 303)

        with get_db() as c:
            updated = c.execute("SELECT status FROM jobs WHERE id=?", (jid,)).fetchone()
            self.assertEqual(updated["status"], "shortlisted")

        # Mark applied
        res = self.client.post(f"/job/{jid}/status", data={"status": "applied"}, follow_redirects=False)
        self.assertEqual(res.status_code, 303)

        with get_db() as c:
            updated = c.execute("SELECT status FROM jobs WHERE id=?", (jid,)).fetchone()
            self.assertEqual(updated["status"], "applied")

    def test_cv_upload_text_file(self):
        cv_content = b"Adeena Khanzada - Computer Systems Engineering, MUET. Skills: Python, C++, Docker, Embedded Systems."
        files = {
            "cv_file": ("adeena_cv.txt", cv_content, "text/plain")
        }
        data = {
            "name": "Adeena Khanzada",
            "citizenship": "Pakistani",
            "residence": "Jeddah, Saudi Arabia",
            "degree": "B.E. Computer Systems Engineering",
            "skills": "Python, C++, Docker",
            "target_roles": "Software Engineer",
            "cv_text": ""
        }
        res = self.client.post("/profile", data=data, files=files, follow_redirects=False)
        self.assertEqual(res.status_code, 303)

        with get_db() as c:
            p = c.execute("SELECT cv_filename, cv_text FROM profile WHERE id=1").fetchone()
            self.assertEqual(p["cv_filename"], "adeena_cv.txt")
            self.assertIn("Computer Systems Engineering", p["cv_text"])

    def test_cv_upload_pdf_file(self):
        import pypdf, io
        # Generate a small valid PDF in memory
        writer = pypdf.PdfWriter()
        page = writer.add_blank_page(width=300, height=300)
        # Note: blank page is valid PDF
        buf = io.BytesIO()
        writer.write(buf)
        pdf_bytes = buf.getvalue()

        files = {
            "cv_file": ("test_resume.pdf", pdf_bytes, "application/pdf")
        }
        data = {
            "name": "Adeena Khanzada",
            "citizenship": "Pakistani",
            "residence": "Jeddah, Saudi Arabia",
            "degree": "B.E. Computer Systems Engineering",
            "skills": "Python, C++",
            "target_roles": "Software Engineer",
            "cv_text": "Experienced Python Engineer"
        }
        res = self.client.post("/profile", data=data, files=files, follow_redirects=False)
        self.assertEqual(res.status_code, 303)

        with get_db() as c:
            p = c.execute("SELECT cv_filename FROM profile WHERE id=1").fetchone()
            self.assertEqual(p["cv_filename"], "test_resume.pdf")

    def test_cv_download(self):
        # Upload a CV first to ensure download target exists
        files = {
            "cv_file": ("download_test.txt", b"Downloadable CV text content", "text/plain")
        }
        self.client.post("/profile", data={"name": "Adeena"}, files=files)

        res = self.client.get("/profile/cv/download")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.headers.get("content-type"), "application/octet-stream")
        self.assertEqual(res.content, b"Downloadable CV text content")

    def test_cookie_session_tracking(self):
        res = self.client.get("/")
        self.assertEqual(res.status_code, 200)
        # Verify cookie is set
        self.assertIn("agent_session", res.headers.get("set-cookie", ""))

    def test_click_tracking_and_training(self):
        with get_db() as c:
            job = c.execute("SELECT id, url FROM jobs LIMIT 1").fetchone()
            self.assertIsNotNone(job)
            jid = job["id"]
            expected_url = job["url"]

        # Track click
        res = self.client.get(f"/track/click/{jid}", follow_redirects=False)
        self.assertEqual(res.status_code, 302)
        self.assertEqual(res.headers.get("location"), expected_url)

        # Verify event was logged in learner database
        with get_db() as c:
            event = c.execute("SELECT * FROM interaction_events WHERE job_id=? AND event_type='click'", (jid,)).fetchone()
            self.assertIsNotNone(event, "Click event must be logged")

    def test_status_selection_learning(self):
        with get_db() as c:
            job = c.execute("SELECT id FROM jobs LIMIT 1").fetchone()
            jid = job["id"]

        # Shortlist triggers positive reinforcement
        res = self.client.post(f"/job/{jid}/status", data={"status": "shortlisted"}, follow_redirects=False)
        self.assertEqual(res.status_code, 303)

        with get_db() as c:
            event = c.execute("SELECT * FROM interaction_events WHERE job_id=? AND event_type='shortlist'", (jid,)).fetchone()
            self.assertIsNotNone(event)
            weights = c.execute("SELECT COUNT(*) FROM learned_weights WHERE score_delta > 0").fetchone()[0]
            self.assertGreater(weights, 0, "Learned weights should update on shortlist")

    def test_reset_training(self):
        res = self.client.post("/training/reset", follow_redirects=False)
        self.assertEqual(res.status_code, 303)

        with get_db() as c:
            count = c.execute("SELECT COUNT(*) FROM interaction_events").fetchone()[0]
            self.assertEqual(count, 0)
            weights = c.execute("SELECT COUNT(*) FROM learned_weights").fetchone()[0]
            self.assertEqual(weights, 0)

    def test_esc_helper(self):
        self.assertEqual(esc(0), "0")
        self.assertEqual(esc("hello <world>"), "hello &lt;world&gt;")
        self.assertEqual(esc(None), "")

if __name__ == "__main__":
    unittest.main()
