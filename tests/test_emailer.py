import unittest
from unittest.mock import patch

from app.emailer import format_daily_picks, send_daily_picks_email


class TestDailyPicksEmail(unittest.TestCase):
    def test_sends_daily_picks_over_authenticated_starttls(self):
        settings = {
            "SMTP_HOST": "smtp.example.com",
            "SMTP_PORT": "587",
            "SMTP_USERNAME": "mailer@example.com",
            "SMTP_PASSWORD": "private-password",
            "SMTP_FROM_EMAIL": "mailer@example.com",
            "DAILY_PICKS_EMAIL": "candidate@example.com",
        }
        pick = {
            "title": "Junior Engineer",
            "company": "Example Co",
            "location": "Remote",
            "url": "https://example.com/jobs/1",
            "jury_score": 91,
            "score": 88,
        }
        smtp = patch("app.emailer.smtplib.SMTP")
        with patch.dict("os.environ", settings, clear=True), smtp as smtp_class:
            smtp_instance = smtp_class.return_value.__enter__.return_value

            sent = send_daily_picks_email([pick], "2026-11-15")

        self.assertTrue(sent)
        smtp_class.assert_called_once_with("smtp.example.com", 587, timeout=30)
        smtp_instance.starttls.assert_called_once()
        smtp_instance.login.assert_called_once_with("mailer@example.com", "private-password")
        message = smtp_instance.send_message.call_args.args[0]
        self.assertEqual(message["To"], "candidate@example.com")
        self.assertIn("Your daily job picks for 2026-11-15", message.get_content())
        self.assertIn("https://example.com/jobs/1", message.get_content())

    def test_sends_using_implicit_tls_when_configured(self):
        settings = {
            "SMTP_HOST": "smtp.example.com",
            "SMTP_PORT": "465",
            "SMTP_USE_SSL": "true",
            "SMTP_FROM_EMAIL": "mailer@example.com",
            "DAILY_PICKS_EMAIL": "candidate@example.com",
        }
        with patch.dict("os.environ", settings, clear=True), patch(
            "app.emailer.smtplib.SMTP_SSL"
        ) as smtp_class:
            smtp_instance = smtp_class.return_value.__enter__.return_value

            sent = send_daily_picks_email([], "2026-11-15")

        self.assertTrue(sent)
        smtp_class.assert_called_once()
        smtp_instance.starttls.assert_not_called()
        smtp_instance.login.assert_not_called()
        self.assertIn("no verified daily picks", smtp_instance.send_message.call_args.args[0].get_content())

    def test_missing_settings_do_not_attempt_delivery(self):
        with patch.dict("os.environ", {}, clear=True), patch(
            "app.emailer.smtplib.SMTP"
        ) as smtp_class:
            sent = send_daily_picks_email([], "2026-11-15")

        self.assertFalse(sent)
        smtp_class.assert_not_called()

    def test_email_body_lists_each_pick(self):
        content = format_daily_picks(
            [{
                "title": "QA Engineer",
                "company": "Example Co",
                "location": "Jeddah",
                "url": "https://example.com/jobs/2",
                "jury_score": 82,
            }],
            "2026-11-15",
        )

        self.assertIn("QA Engineer — Example Co", content)
        self.assertIn("Match score: 82/100", content)
        self.assertIn("https://example.com/jobs/2", content)


if __name__ == "__main__":
    unittest.main()
