import logging
import os
import smtplib
import ssl
from datetime import datetime, timezone
from email.message import EmailMessage

logger = logging.getLogger(__name__)


def send_daily_picks_email(picks: list[dict], session_date: str) -> bool:
    required = {
        "SMTP_HOST": os.getenv("SMTP_HOST", "").strip(),
        "DAILY_PICKS_EMAIL": os.getenv("DAILY_PICKS_EMAIL", "").strip(),
        "SMTP_FROM_EMAIL": os.getenv("SMTP_FROM_EMAIL", "").strip(),
    }
    missing = [key for key, value in required.items() if not value]
    if missing:
        logger.warning(
            "Daily job-picks email not sent; configure %s.",
            ", ".join(missing),
        )
        return False

    username = os.getenv("SMTP_USERNAME", "").strip()
    password = os.getenv("SMTP_PASSWORD", "")
    if bool(username) != bool(password):
        logger.error("Daily job-picks email not sent; configure both SMTP_USERNAME and SMTP_PASSWORD.")
        return False

    try:
        port = int(os.getenv("SMTP_PORT", "587"))
        use_ssl = os.getenv("SMTP_USE_SSL", "").strip().lower() in {"1", "true", "yes"}
        message = EmailMessage()
        message["Subject"] = f"Your daily job picks for {session_date}"
        message["From"] = required["SMTP_FROM_EMAIL"]
        message["To"] = required["DAILY_PICKS_EMAIL"]
        message.set_content(format_daily_picks(picks, session_date))

        if use_ssl:
            with smtplib.SMTP_SSL(
                required["SMTP_HOST"],
                port,
                timeout=30,
                context=ssl.create_default_context(),
            ) as smtp:
                if username:
                    smtp.login(username, password)
                smtp.send_message(message)
        else:
            with smtplib.SMTP(required["SMTP_HOST"], port, timeout=30) as smtp:
                smtp.starttls(context=ssl.create_default_context())
                if username:
                    smtp.login(username, password)
                smtp.send_message(message)
    except (OSError, smtplib.SMTPException, ValueError):
        logger.exception("Could not send daily job-picks email.")
        return False

    logger.info("Sent daily job-picks email for %s.", session_date)
    return True


def format_daily_picks(picks: list[dict], session_date: str) -> str:
    lines = [f"Your daily job picks for {session_date}", ""]
    if not picks:
        lines.append("There are no verified daily picks today. Check the dashboard for updates.")
        return "\n".join(lines) + "\n"

    for index, job in enumerate(picks, start=1):
        lines.extend([
            f"{index}. {job.get('title') or 'Untitled role'} — {job.get('company') or 'Company not listed'}",
            f"   Location: {job.get('location') or 'Not listed'}",
            f"   Match score: {job.get('jury_score') or job.get('score') or 0}/100",
            f"   Apply: {job.get('url') or 'Open the dashboard to view this job'}",
            "",
        ])
    return "\n".join(lines)


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()
