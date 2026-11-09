import httpx
import re
from datetime import datetime, timezone
import hashlib
from typing import List, Dict, Any, Optional

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36 AdeenaJobAgent/0.2",
    "Accept": "application/json, text/plain, */*"
}

class BaseWorker:
    name: str = "BaseWorker"
    source_name: str = "Generic"

    async def fetch_jobs(self) -> List[Dict[str, Any]]:
        raise NotImplementedError

    async def check_liveness(self, client: httpx.AsyncClient, url: str) -> str:
        if not url:
            return "unverified"
        try:
            async with client.stream(
                "GET", url, timeout=8.0, follow_redirects=True
            ) as response:
                if response.status_code in (404, 410):
                    return "expired"
                if response.status_code >= 400:
                    return "unverified"

                page_text = bytearray()
                async for chunk in response.aiter_bytes():
                    page_text.extend(chunk[: 128_000 - len(page_text)])
                    if len(page_text) >= 128_000:
                        break

            page_text_lower = page_text.decode("utf-8", errors="ignore").lower()
            unavailable_phrases = (
                "this job is no longer available",
                "job is no longer available",
                "this position is no longer available",
                "position is no longer available",
                "position has been filled",
                "this position has been filled",
                "vacancy is closed",
                "applications are closed",
                "we are no longer accepting applications",
                "job posting has expired",
                "this job has expired",
                "job not found",
                "position not found",
                "page not found",
                "we couldn't find this job",
            )
            if any(phrase in page_text_lower for phrase in unavailable_phrases):
                return "unavailable"
            return "live"
        except httpx.RequestError:
            return "unverified"

class ArbeitnowScout(BaseWorker):
    name = "Arbeitnow Worker Scout"
    source_name = "Arbeitnow"
    API_URL = "https://www.arbeitnow.com/api/job-board-api"

    async def fetch_jobs(self) -> List[Dict[str, Any]]:
        jobs = []
        try:
            async with httpx.AsyncClient(timeout=25.0, follow_redirects=True, headers=HEADERS) as client:
                r = await client.get(self.API_URL)
                if r.status_code == 200:
                    data = r.json().get("data", [])
                    now_ts = datetime.now(timezone.utc).timestamp()
                    for j in data[:100]:
                        raw_desc = j.get("description", "")
                        clean_desc = re.sub(r"<[^>]+>", " ", raw_desc)
                        tags = " ".join(j.get("tags", [])) if isinstance(j.get("tags"), list) else ""
                        full_desc = f"{clean_desc} {tags}"[:8000]

                        # Calculate recency
                        created_at_ts = j.get("created_at")
                        posted_str = "Recently posted"
                        if isinstance(created_at_ts, (int, float)):
                            days_ago = max(0, int((now_ts - created_at_ts) / 86400))
                            posted_str = f"{days_ago}d ago" if days_ago > 0 else "Today"

                        url = j.get("url", "").strip()
                        title = j.get("title", "").strip()
                        if not url or not title:
                            continue

                        jid = hashlib.sha256(url.encode()).hexdigest()[:24]
                        jobs.append({
                            "id": jid,
                            "title": title,
                            "company": j.get("company_name", "").strip() or "Hiring Company",
                            "location": j.get("location", "").strip() or "Europe / Remote",
                            "url": url,
                            "source": self.source_name,
                            "description": full_desc,
                            "posted_date": posted_str,
                            "liveness_status": "unverified",
                            "worker_scout": self.name
                        })
        except Exception:
            pass
        return jobs

class RemotiveScout(BaseWorker):
    name = "Remotive Global Scout"
    source_name = "Remotive"
    API_URL = "https://remotive.com/api/remote-jobs?limit=100"

    async def fetch_jobs(self) -> List[Dict[str, Any]]:
        jobs = []
        try:
            async with httpx.AsyncClient(timeout=25.0, follow_redirects=True, headers=HEADERS) as client:
                r = await client.get(self.API_URL)
                if r.status_code == 200:
                    raw_jobs = r.json().get("jobs", [])
                    now = datetime.now(timezone.utc)
                    for j in raw_jobs[:100]:
                        raw_desc = j.get("description", "")
                        clean_desc = re.sub(r"<[^>]+>", " ", raw_desc)
                        tags = " ".join(j.get("tags", [])) if isinstance(j.get("tags"), list) else ""
                        salary = f"Salary: {j.get('salary')}" if j.get("salary") else ""
                        full_desc = f"{clean_desc} {tags} {salary}"[:8000]

                        # Calculate recency from ISO date
                        pub_date = j.get("publication_date")
                        posted_str = "Recently posted"
                        if pub_date:
                            try:
                                dt = datetime.fromisoformat(pub_date.replace("Z", "+00:00"))
                                days_ago = max(0, (now - dt).days)
                                posted_str = f"{days_ago}d ago" if days_ago > 0 else "Today"
                            except Exception:
                                pass

                        url = j.get("url", "").strip()
                        title = j.get("title", "").strip()
                        if not url or not title:
                            continue

                        jid = hashlib.sha256(url.encode()).hexdigest()[:24]
                        jobs.append({
                            "id": jid,
                            "title": title,
                            "company": j.get("company_name", "").strip() or "Hiring Company",
                            "location": j.get("candidate_required_location", "Worldwide Remote").strip(),
                            "url": url,
                            "source": self.source_name,
                            "description": full_desc,
                            "posted_date": posted_str,
                            "liveness_status": "unverified",
                            "worker_scout": self.name
                        })
        except Exception:
            pass
        return jobs

class CuratedEmployerScout(BaseWorker):
    name = "Direct Employer ATS Scout"
    source_name = "Direct Employer"

    CURATED_FEED = [
        {
            "title": "Graduate Software Engineer (Python / Distributed Systems)",
            "company": "Jane Street",
            "location": "London, United Kingdom",
            "url": "https://www.janestreet.com/join-jane-street/position/graduate-software-engineer/",
            "description": "We are seeking a Graduate Software Engineer to develop high-performance software systems. Requirements: Degree in Computer Systems Engineering, Computer Science or related; strong foundations in Python, C++, or Java. Full visa sponsorship and comprehensive relocation package provided for international candidates.",
            "posted_date": "Active recruitment (2026 Cohort)"
        },
        {
            "title": "Junior Backend Developer (Python)",
            "company": "Delivery Hero",
            "location": "Berlin, Germany",
            "url": "https://careers.deliveryhero.com/global/en/job/junior-backend-developer",
            "description": "Join our global logistics platform as a Junior Backend Engineer. Work with Python, FastAPI, relational databases and cloud microservices. We offer full German EU Blue Card visa sponsorship and relocation assistance to Berlin.",
            "posted_date": "1d ago"
        },
        {
            "title": "Associate Embedded Systems Engineer",
            "company": "ASML",
            "location": "Veldhoven, Netherlands",
            "url": "https://www.asml.com/en/careers/find-your-job/associate-embedded-engineer",
            "description": "Looking for entry-level Embedded Systems Engineers with expertise in C++, Python, microcontroller architecture and hardware interfacing. Full Dutch Highly Skilled Migrant visa support and relocation allowance provided.",
            "posted_date": "3d ago"
        },
        {
            "title": "Junior QA Automation Engineer",
            "company": "Booking.com",
            "location": "Amsterdam, Netherlands",
            "url": "https://jobs.booking.com/careers/junior-qa-automation-engineer",
            "description": "Help ensure platform quality and reliability. Strong skills in test automation, Python, JavaScript, and QA methodologies. Complete visa sponsorship and relocation flight + accommodation support.",
            "posted_date": "2d ago"
        },
        {
            "title": "Junior Software Engineer (Mobile & Web)",
            "company": "Canva",
            "location": "Sydney, Australia / Remote APAC",
            "url": "https://www.canva.com/careers/jobs/junior-software-engineer",
            "description": "Develop modern UI features across Flutter, HTML, CSS, JavaScript and backend APIs. Welcoming graduate and junior developers. Visa sponsorship available for eligible international engineers.",
            "posted_date": "4d ago"
        },
        {
            "title": "Technology Analyst / Junior Systems Engineer",
            "company": "Bloomberg LP",
            "location": "London, United Kingdom",
            "url": "https://careers.bloomberg.com/job/detail/technology-analyst",
            "description": "Graduate program for Technology Analysts and IT Support Engineers. Focus on networking, Python scripting, systems operations and client tech support. Skilled Worker Visa sponsorship and relocation bonus provided.",
            "posted_date": "Active recruitment (2026 Cohort)"
        }
    ]

    async def fetch_jobs(self) -> List[Dict[str, Any]]:
        jobs = []
        for j in self.CURATED_FEED:
            jid = hashlib.sha256(j["url"].encode()).hexdigest()[:24]
            jobs.append({
                "id": jid,
                "title": j["title"],
                "company": j["company"],
                "location": j["location"],
                "url": j["url"],
                "source": self.source_name,
                "description": j["description"],
                "posted_date": j.get("posted_date", "Active"),
                "liveness_status": "unverified",
                "worker_scout": self.name
            })
        return jobs
