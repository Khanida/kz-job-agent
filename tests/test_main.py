import unittest
import asyncio
import json
import httpx
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient
from app.main import app, get_db, esc
from app.agents.cv_analyzer import analyze_cv, score_cv_match
from app.agents.workers import BaseWorker, CuratedEmployerScout
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
        self.assertIn("Agents debate", response.text)
        self.assertIn("Jury decides", response.text)

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
                           cv_text=?, cv_analysis=?, updated_at=?
                       WHERE id=1""",
                    (
                        previous_profile["name"], previous_profile["citizenship"],
                        previous_profile["residence"], previous_profile["degree"],
                        previous_profile["skills"], previous_profile["target_roles"],
                        previous_profile["cv_text"], previous_profile.get("cv_analysis"),
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
