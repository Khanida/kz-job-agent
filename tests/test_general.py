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
        self.assertIn("Your recommendations will appear here", dashboard.text)

    def test_accounts_have_separate_profiles_and_job_lists(self):
        self.assertEqual(self.register(self.client, "Jordan Lee", "jordan@example.com").status_code, 303)
        jordan_id = self.user_id("jordan@example.com")
        with general.get_user_db(jordan_id) as conn:
            conn.execute(
                """INSERT INTO jobs (id, title, company, location, url, source, description,
                   status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                ("private-job", "Private role", "Jordan's company", "Remote",
                 "https://example.com/job", "Test", "", "new", "2026-11-10"),
            )

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

        with patch.object(general, "run_discovery_workers", new=AsyncMock(return_value=discovered)):
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


if __name__ == "__main__":
    unittest.main()
