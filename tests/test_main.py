import unittest
from fastapi.testclient import TestClient
from app.main import app, get_db, score_job, evidence, esc

class TestJobAgent(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_health_check(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data.get("ok"))
        self.assertEqual(data.get("service"), "adeena-job-agent")

    def test_home_page(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("ADEENA'S JOB AGENT", response.text)
        self.assertIn("OPPORTUNITIES PIPELINE", response.text)

    def test_profile_page_get(self):
        response = self.client.get("/profile")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Candidate Profile", response.text)
        self.assertIn("Adeena Khanzada", response.text)

    def test_profile_save_and_rescore(self):
        payload = {
            "name": "Adeena Khanzada",
            "citizenship": "Pakistani",
            "residence": "Jeddah, Saudi Arabia",
            "degree": "B.E. Computer Systems Engineering, MUET",
            "skills": "Python, C++, Java, Flutter, Firebase, FastAPI",
            "target_roles": "Junior Software Engineer, QA Engineer, Embedded Systems Engineer",
            "cv_text": "Experienced junior engineer specializing in Python and embedded systems."
        }
        response = self.client.post("/profile", data=payload, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertIn("/profile?msg=", response.headers["location"])

    def test_settings_page(self):
        response = self.client.get("/settings")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Agent Settings & Automation", response.text)
        self.assertIn("Run Live Discovery Scan", response.text)

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
            app_rec = c.execute("SELECT * FROM applications WHERE job_id=?", (jid,)).fetchone()
            self.assertIsNotNone(app_rec)

    def test_score_job(self):
        profile = {
            "skills": "Python, C++, Flutter",
            "target_roles": "Junior Software Engineer"
        }
        job = {
            "title": "Junior Software Engineer (Python)",
            "company": "Tech Corp",
            "description": "We are seeking a junior python engineer with C++ knowledge.",
            "location": "Berlin"
        }
        score = score_job(job, profile)
        self.assertGreaterEqual(score, 30)

    def test_evidence_extraction(self):
        txt = "Full visa sponsorship and comprehensive relocation package provided."
        res = evidence(txt, ["visa sponsorship", "work permit"])
        self.assertIn("visa sponsorship", res)

        housing_res = evidence(txt, ["relocation package", "temporary housing"])
        self.assertIn("relocation package", housing_res)

    def test_esc_helper(self):
        self.assertEqual(esc(0), "0")
        self.assertEqual(esc("hello <world>"), "hello &lt;world&gt;")
        self.assertEqual(esc(None), "")

if __name__ == "__main__":
    unittest.main()
