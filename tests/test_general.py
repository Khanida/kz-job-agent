import asyncio
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app import general


class TestGeneralJobApp(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        general.AUTH_DB_PATH = root / "auth.db"
        general.USER_DATA_DIR = root / "users"
        self.client = TestClient(general.app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.temp_dir.cleanup()

    def csrf_token(self, client, path="/register"):
        response = client.get(path)
        self.assertEqual(response.status_code, 200)
        match = re.search(r"name='csrf_token' value='([^']+)'", response.text)
        self.assertIsNotNone(match)
        return match.group(1)

    def register(self, client, name, email):
        csrf = self.csrf_token(client)
        return client.post("/register", data={
            "name": name,
            "email": email,
            "password": "a-long-test-password",
            "csrf_token": csrf,
        }, follow_redirects=False)

    def user_id(self, email):
        with general.get_auth_db() as conn:
            row = conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
        return row["id"]

    def test_guest_is_sent_to_login_and_registration_creates_account(self):
        response = self.client.get("/dashboard", follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/login")

        response = self.register(self.client, "Jordan Lee", "jordan@example.com")
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/dashboard")
        dashboard = self.client.get("/dashboard")
        self.assertIn("Find a job that fits", dashboard.text)
        self.assertIn("Jordan Lee", dashboard.text)
        self.assertIn("Jury Room", dashboard.text)
        self.assertIn("Set up my job preferences", dashboard.text)
        self.assertNotIn("action='/refresh' data-job-refresh", dashboard.text)
        self.assertNotIn("Scouts search", dashboard.text)
        self.assertNotIn("Recommended jobs", dashboard.text)

    def test_accounts_have_separate_profiles_and_job_lists(self):
        self.assertEqual(self.register(self.client, "Jordan Lee", "jordan@example.com").status_code, 303)
        jordan_id = self.user_id("jordan@example.com")
        with general.get_user_db(jordan_id) as conn:
            conn.execute(
                """INSERT INTO jobs (id, title, company, location, url, source, description,
                   score, jury_score, is_daily_pick, authenticity_status, status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                ("private-job", "Private role", "Jordan's company", "Remote",
                 "https://example.com/job", "Test", "", 80, 80, 1, "likely_official",
                 "new", "2026-11-10"),
            )

        self.assertIn("Private role", self.client.get("/dashboard").text)
        other_client = TestClient(general.app)
        response = self.register(other_client, "Taylor Chen", "taylor@example.com")
        self.assertEqual(response.status_code, 303)
        taylor_dashboard = other_client.get("/dashboard")
        self.assertIn("Taylor Chen", taylor_dashboard.text)
        self.assertNotIn("Private role", taylor_dashboard.text)
        self.assertNotIn("Jordan's company", taylor_dashboard.text)

        csrf = self.csrf_token(other_client, "/profile")
        denied = other_client.post(
            "/job/private-job/status",
            data={"status": "shortlisted", "csrf_token": csrf},
        )
        self.assertEqual(denied.status_code, 404)

    def test_registration_requires_csrf_and_strong_password(self):
        missing_csrf = self.client.post("/register", data={
            "name": "Jordan", "email": "jordan@example.com",
            "password": "a-long-test-password", "csrf_token": "wrong",
        })
        self.assertEqual(missing_csrf.status_code, 403)

        csrf = self.csrf_token(self.client)
        weak_password = self.client.post("/register", data={
            "name": "Jordan", "email": "jordan@example.com",
            "password": "short", "csrf_token": csrf,
        })
        self.assertEqual(weak_password.status_code, 200)
        self.assertIn("at least 12 characters", weak_password.text)

    def test_user_can_save_a_job_in_their_own_list(self):
        self.register(self.client, "Jordan Lee", "jordan@example.com")
        user_id = self.user_id("jordan@example.com")
        with general.get_user_db(user_id) as conn:
            conn.execute(
                """INSERT INTO jobs (id, title, company, url, source, status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                ("saveable-job", "Junior Analyst", "Example Co",
                 "https://example.com/job", "Test", "new", "2026-11-10"),
            )
        csrf = self.csrf_token(self.client, "/dashboard")
        response = self.client.post(
            "/job/saveable-job/status",
            data={"status": "shortlisted", "csrf_token": csrf},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        with general.get_user_db(user_id) as conn:
            job = conn.execute("SELECT status FROM jobs WHERE id=?", ("saveable-job",)).fetchone()
            event = conn.execute(
                "SELECT event_type FROM interaction_events WHERE job_id=?",
                ("saveable-job",),
            ).fetchone()
        self.assertEqual(job["status"], "shortlisted")
        self.assertEqual(event["event_type"], "shortlist")

    def test_profile_saves_preferences_and_login_restores_account(self):
        self.register(self.client, "Jordan Lee", "jordan@example.com")
        csrf = self.csrf_token(self.client, "/profile")
        response = self.client.post("/profile", data={
            "csrf_token": csrf,
            "name": "Jordan Lee",
            "residence": "Toronto, Canada",
            "skills": "Excel, SQL",
            "target_roles": "Data Analyst",
        }, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        user_id = self.user_id("jordan@example.com")
        with general.get_user_db(user_id) as conn:
            profile = conn.execute("SELECT * FROM profile WHERE id=1").fetchone()
        self.assertEqual(profile["residence"], "Toronto, Canada")
        self.assertEqual(profile["target_roles"], "Data Analyst")

        csrf = self.csrf_token(self.client, "/profile")
        response = self.client.post("/logout", data={"csrf_token": csrf}, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        csrf = self.csrf_token(self.client, "/login")
        response = self.client.post("/login", data={
            "email": "jordan@example.com",
            "password": "a-long-test-password",
            "csrf_token": csrf,
        }, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/dashboard")
        self.assertIn("Jordan Lee", self.client.get("/dashboard").text)

    def test_general_recommendations_follow_profile_and_exclusions(self):
        profile = {
            "target_roles": "Data Analyst",
            "skills": "Excel, SQL",
            "residence": "Toronto, Canada",
            "experience_level": "any",
            "excluded_skills": "",
            "excluded_job_terms": "backend",
            "cv_text": "",
        }
        discovered = [
            {
                "id": "analyst-job", "title": "Data Analyst", "company": "Example",
                "location": "Toronto, Canada", "url": "https://example.com/analyst",
                "source": "Test", "description": "Uses Excel and SQL.",
            },
            {
                "id": "backend-job", "title": "Backend Developer", "company": "Example",
                "location": "Remote", "url": "https://example.com/backend",
                "source": "Test", "description": "Uses SQL.",
            },
        ]

        async def mark_live(jobs):
            for job in jobs:
                job["liveness_status"] = "live"
                job["authenticity_status"] = "verified"

        with patch.object(general, "run_general_discovery_workers", new=AsyncMock(return_value=discovered)):
            with patch.object(general, "verify_listing_availability", new=AsyncMock(side_effect=mark_live)):
                with general.get_user_db("test-user") as conn:
                    result = asyncio.run(general.run_general_refresh(conn, profile))
                    jobs = conn.execute(
                        "SELECT id, jury_score, is_daily_pick FROM jobs ORDER BY id"
                    ).fetchall()

        self.assertEqual(result["crowned_count"], 1)
        by_id = {job["id"]: dict(job) for job in jobs}
        self.assertEqual(by_id["analyst-job"]["is_daily_pick"], 1)
        self.assertGreater(by_id["analyst-job"]["jury_score"], by_id["backend-job"]["jury_score"])
        self.assertEqual(by_id["backend-job"]["jury_score"], 0)

    def test_general_scouts_use_independent_public_feeds(self):
        class FakeScout:
            def __init__(self, jobs):
                self.jobs = jobs
                self.name = "Fake public scout"

            async def fetch_jobs(self):
                return self.jobs

        with patch.object(general, "PublicJobBoardScout", return_value=FakeScout([
            {"id": "board-1", "url": "https://example.com/1"},
            {"id": "shared", "url": "https://example.com/shared"},
        ])):
            with patch.object(general, "RemoteOpportunityScout", return_value=FakeScout([
                {"id": "remote-1", "url": "https://example.com/2"},
                {"id": "duplicate", "url": "https://example.com/shared"},
            ])):
                jobs = asyncio.run(general.run_general_discovery_workers())

        self.assertEqual([job["id"] for job in jobs], ["board-1", "shared", "remote-1"])
        self.assertEqual(jobs[0]["worker_scout"], "Fake public scout")
        self.assertEqual(jobs[2]["worker_scout"], "Fake public scout")

    def test_jury_room_shows_saved_debate_and_reviewer_panel(self):
        self.register(self.client, "Jordan Lee", "jordan@example.com")
        user_id = self.user_id("jordan@example.com")
        discovered = [{
            "id": "jury-job", "title": "Data Analyst", "company": "Example Co",
            "location": "Toronto", "url": "https://example.com/data-role",
            "source": "Board", "worker_scout": "International Job Board Scout",
            "description": "SQL and Excel experience preferred.",
        }]

        async def mark_live(jobs):
            for job in jobs:
                job["liveness_status"] = "live"
                job["authenticity_status"] = "likely_official"
                job["authenticity_evidence"] = "Test domain signal."

        with patch.object(general, "run_general_discovery_workers", new=AsyncMock(return_value=discovered)):
            with patch.object(general, "verify_listing_availability", new=AsyncMock(side_effect=mark_live)):
                with general.get_user_db(user_id) as conn:
                    profile = dict(conn.execute("SELECT * FROM profile WHERE id=1").fetchone())
                    profile["target_roles"] = "Data Analyst"
                    profile["skills"] = "SQL, Excel"
                    profile["residence"] = "Toronto"
                    result = asyncio.run(general.run_general_refresh(conn, profile))

        self.assertEqual(result["crowned_count"], 1)
        response = self.client.get("/jury")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Jury Room", response.text)
        self.assertIn("data-job-workflow", response.text)
        self.assertIn("Scouts search", response.text)
        self.assertIn("Reviewers compare", response.text)
        self.assertIn("The Jury ranks", response.text)
        self.assertIn("International Job Board Scout", response.text)
        self.assertIn("Remote Opportunity Scout", response.text)
        self.assertIn("Role &amp; Skills Advocate", response.text)
        self.assertIn("Experience &amp; Preferences Reviewer", response.text)
        self.assertIn("Posting Quality Reviewer", response.text)
        self.assertIn("Independent Cross-Examiner", response.text)
        self.assertIn("POSSIBLE MATCH — REVIEW DETAILS", response.text)
        self.assertIn("data analyst", response.text.lower())

    def test_jury_room_requires_login_and_debates_are_account_private(self):
        guest = TestClient(general.app)
        response = guest.get("/jury", follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/login")

        self.register(self.client, "Jordan Lee", "jordan@example.com")
        jordan_id = self.user_id("jordan@example.com")
        with general.get_user_db(jordan_id) as conn:
            conn.execute(
                """INSERT INTO jobs (id, title, company, status, jury_score, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                ("private-debate", "Private Analyst Role", "Private Co", "new", 80, "2026-11-10"),
            )
            conn.execute(
                "UPDATE jobs SET worker_scout='Private Scout' WHERE id='private-debate'"
            )
            conn.execute(
                """INSERT INTO deliberations (id, session_date, summary, top_picks_count, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                ("private-round", "2026-11-10", "Private debate summary", 0, "2026-11-10T00:00:00Z"),
            )
            conn.execute(
                """INSERT INTO jury_verdicts
                   (deliberation_id, job_id, verdict, consensus_score, recommendation, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                ("private-round", "private-debate", "PRIVATE VERDICT", 80, "Private recommendation", "2026-11-10"),
            )
        self.assertIn("PRIVATE VERDICT", self.client.get("/jury").text)

        other_client = TestClient(general.app)
        self.register(other_client, "Taylor Chen", "taylor@example.com")
        response = other_client.get("/jury")
        self.assertNotIn("Private Analyst Role", response.text)
        self.assertNotIn("Private debate summary", response.text)


if __name__ == "__main__":
    unittest.main()
