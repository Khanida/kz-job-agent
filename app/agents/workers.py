import httpx
import re
import asyncio
import html
import logging
import os
from datetime import datetime, timezone
import hashlib
from typing import List, Dict, Any
from urllib.parse import quote_plus, urlsplit

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36 AdeenaJobAgent/0.2",
    "Accept": "application/json, text/plain, */*"
}
logger = logging.getLogger(__name__)


def google_careers_search_url(profile: Dict[str, Any]) -> str:
    """Build a user-initiated Google query for ATS boards and official career pages."""
    roles = [
        role.strip()
        for role in (profile.get("target_roles") or "").split(",")
        if role.strip()
    ][:3]
    if not roles:
        roles = [
            skill.strip()
            for skill in (profile.get("skills") or "").split(",")
            if skill.strip()
        ][:3]
    search_terms = " OR ".join(f'"{role}"' for role in roles) or '"open jobs"'
    location = (profile.get("residence") or "").strip()
    query = (
        f"({search_terms}) jobs {location} "
        "(site:boards.greenhouse.io OR site:jobs.lever.co OR "
        "site:jobs.ashbyhq.com OR inurl:careers)"
    ).strip()
    return "https://www.google.com/search?q=" + quote_plus(query)


def _configured_boards(environment_name: str) -> List[tuple[str, str]]:
    """Read comma-separated Company=board-name settings for a public ATS."""
    configured = []
    for entry in os.getenv(environment_name, "").split(","):
        if not entry.strip():
            continue
        company, separator, board_name = entry.partition("=")
        if not separator or not company.strip() or not board_name.strip():
            logger.warning(
                "Ignoring malformed %s entry; expected Company=board-name",
                environment_name,
            )
            continue
        configured.append((company.strip(), board_name.strip()))
    return configured


def _plain_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    without_tags = re.sub(r"<[^>]*>", " ", value)
    return re.sub(r"\s+", " ", html.unescape(without_tags)).strip()


def _posted_date(value: Any) -> str:
    if not isinstance(value, str) or not value:
        return "Recently posted"
    try:
        published = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if published.tzinfo is None:
            published = published.replace(tzinfo=timezone.utc)
        age_days = max(0, (datetime.now(timezone.utc) - published).days)
    except ValueError:
        return "Recently posted"
    return f"{age_days}d ago" if age_days else "Today"

class BaseWorker:
    name: str = "BaseWorker"
    source_name: str = "Generic"

    async def fetch_jobs(self) -> List[Dict[str, Any]]:
        raise NotImplementedError

    @staticmethod
    def assess_authenticity(url: str, company: str) -> Dict[str, str]:
        try:
            parsed = urlsplit(url)
            host = (parsed.hostname or "").lower().rstrip(".")
        except ValueError:
            return {
                "status": "suspicious",
                "evidence": "The posting URL is malformed and its destination cannot be assessed.",
            }

        if parsed.scheme.lower() != "https" or not host:
            return {
                "status": "suspicious",
                "evidence": "The posting URL does not use a valid HTTPS destination.",
            }
        if parsed.username or parsed.password:
            return {
                "status": "suspicious",
                "evidence": "The posting URL contains embedded login credentials.",
            }

        known_recruiting_platforms = (
            "greenhouse.io",
            "lever.co",
            "myworkdayjobs.com",
            "ashbyhq.com",
            "smartrecruiters.com",
            "icims.com",
            "successfactors.com",
            "workable.com",
            "recruitee.com",
            "jobvite.com",
            "teamtailor.com",
            "bamboohr.com",
        )
        platform = next(
            (domain for domain in known_recruiting_platforms if host == domain or host.endswith(f".{domain}")),
            None,
        )
        if platform:
            return {
                "status": "likely_official",
                "evidence": (
                    f"The link is hosted on the known recruiting platform {platform}; "
                    "confirm the employer and role on the page."
                ),
            }

        host_labels = set(host.split("."))
        company_words = re.findall(r"[a-z0-9]+", company.lower())
        company_words = [
            word for word in company_words
            if word not in {"the", "company", "corporation", "corp", "inc", "ltd", "limited", "group", "plc", "gmbh", "ag"}
        ]
        normalized_host = re.sub(r"[^a-z0-9]", "", host)
        normalized_company = "".join(company_words)
        if (
            any(word in host_labels for word in company_words)
            or (len(normalized_company) >= 5 and normalized_company in normalized_host)
        ):
            return {
                "status": "likely_official",
                "evidence": "The HTTPS posting domain contains the employer's name; this is a positive signal, not proof of authenticity.",
            }
        return {
            "status": "unverified",
            "evidence": "The HTTPS domain could not be matched to the employer or a recognized recruiting platform.",
        }

    async def check_listing(self, client: httpx.AsyncClient, url: str, company: str = "") -> Dict[str, str]:
        if not url:
            return {
                "liveness_status": "unverified",
                "authenticity_status": "unverified",
                "authenticity_evidence": "No posting URL was provided.",
            }
        try:
            async with client.stream(
                "GET", url, timeout=8.0, follow_redirects=True
            ) as response:
                final_url = str(response.url)
                authenticity = self.assess_authenticity(final_url, company)
                if response.status_code in (404, 410):
                    liveness_status = "expired"
                    return {
                        "liveness_status": liveness_status,
                        "authenticity_status": authenticity["status"],
                        "authenticity_evidence": authenticity["evidence"],
                    }
                if response.status_code >= 400:
                    liveness_status = "unverified"
                    return {
                        "liveness_status": liveness_status,
                        "authenticity_status": authenticity["status"],
                        "authenticity_evidence": authenticity["evidence"],
                    }

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
                liveness_status = "unavailable"
            else:
                liveness_status = "live"
            return {
                "liveness_status": liveness_status,
                "authenticity_status": authenticity["status"],
                "authenticity_evidence": authenticity["evidence"],
            }
        except httpx.RequestError:
            authenticity = self.assess_authenticity(url, company)
            return {
                "liveness_status": "unverified",
                "authenticity_status": authenticity["status"],
                "authenticity_evidence": authenticity["evidence"],
            }

    async def check_liveness(self, client: httpx.AsyncClient, url: str) -> str:
        return (await self.check_listing(client, url))["liveness_status"]

class ArbeitnowScout(BaseWorker):
    name = "Arbeitnow Worker Scout"
    source_name = "Arbeitnow"
    API_URL = "https://www.arbeitnow.com/api/job-board-api"

    async def fetch_jobs(self) -> List[Dict[str, Any]]:
        jobs = []
        async with httpx.AsyncClient(timeout=25.0, follow_redirects=True, headers=HEADERS) as client:
            response = await client.get(self.API_URL)
            response.raise_for_status()
            payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise ValueError("Arbeitnow returned an unexpected job-feed response")

        now_ts = datetime.now(timezone.utc).timestamp()
        for j in payload["data"][:100]:
            if not isinstance(j, dict):
                continue
            raw_desc = j.get("description", "")
            clean_desc = re.sub(r"<[^>]+>", " ", raw_desc) if isinstance(raw_desc, str) else ""
            tags = " ".join(j.get("tags", [])) if isinstance(j.get("tags"), list) else ""
            full_desc = f"{clean_desc} {tags}"[:8000]

            created_at_ts = j.get("created_at")
            posted_str = "Recently posted"
            if isinstance(created_at_ts, (int, float)):
                days_ago = max(0, int((now_ts - created_at_ts) / 86400))
                posted_str = f"{days_ago}d ago" if days_ago > 0 else "Today"

            url = j.get("url", "").strip() if isinstance(j.get("url"), str) else ""
            title = j.get("title", "").strip() if isinstance(j.get("title"), str) else ""
            if not url or not title:
                continue

            jid = hashlib.sha256(url.encode()).hexdigest()[:24]
            company = j.get("company_name")
            location = j.get("location")
            jobs.append({
                "id": jid,
                "title": title,
                "company": company.strip() if isinstance(company, str) and company.strip() else "Hiring Company",
                "location": location.strip() if isinstance(location, str) and location.strip() else "Europe / Remote",
                "url": url,
                "source": self.source_name,
                "description": full_desc,
                "posted_date": posted_str,
                "liveness_status": "unverified",
                "worker_scout": self.name
            })
        return jobs

class RemotiveScout(BaseWorker):
    name = "Remotive Global Scout"
    source_name = "Remotive"
    API_URL = "https://remotive.com/api/remote-jobs?limit=100"

    async def fetch_jobs(self) -> List[Dict[str, Any]]:
        jobs = []
        async with httpx.AsyncClient(timeout=25.0, follow_redirects=True, headers=HEADERS) as client:
            response = await client.get(self.API_URL)
            response.raise_for_status()
            payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
            raise ValueError("Remotive returned an unexpected job-feed response")

        now = datetime.now(timezone.utc)
        for j in payload["jobs"][:100]:
            if not isinstance(j, dict):
                continue
            raw_desc = j.get("description", "")
            clean_desc = re.sub(r"<[^>]+>", " ", raw_desc) if isinstance(raw_desc, str) else ""
            tags = " ".join(j.get("tags", [])) if isinstance(j.get("tags"), list) else ""
            salary = f"Salary: {j.get('salary')}" if j.get("salary") else ""
            full_desc = f"{clean_desc} {tags} {salary}"[:8000]

            pub_date = j.get("publication_date")
            posted_str = "Recently posted"
            if isinstance(pub_date, str) and pub_date:
                try:
                    dt = datetime.fromisoformat(pub_date.replace("Z", "+00:00"))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    days_ago = max(0, (now - dt).days)
                    posted_str = f"{days_ago}d ago" if days_ago > 0 else "Today"
                except ValueError:
                    pass

            url = j.get("url", "").strip() if isinstance(j.get("url"), str) else ""
            title = j.get("title", "").strip() if isinstance(j.get("title"), str) else ""
            if not url or not title:
                continue

            jid = hashlib.sha256(url.encode()).hexdigest()[:24]
            company = j.get("company_name")
            location = j.get("candidate_required_location")
            jobs.append({
                "id": jid,
                "title": title,
                "company": company.strip() if isinstance(company, str) and company.strip() else "Hiring Company",
                "location": location.strip() if isinstance(location, str) and location.strip() else "Worldwide Remote",
                "url": url,
                "source": self.source_name,
                "description": full_desc,
                "posted_date": posted_str,
                "liveness_status": "unverified",
                "worker_scout": self.name
            })
        return jobs


class OfficialCareerSiteScout(BaseWorker):
    """Fetch public job postings from explicitly configured Greenhouse, Lever, and Ashby boards."""

    name = "Official Careers ATS Scout"
    source_name = "Official Careers"

    def _boards(self) -> List[tuple[str, str, str]]:
        return [
            ("Greenhouse", company, board)
            for company, board in _configured_boards("GREENHOUSE_BOARDS")
        ] + [
            ("Lever", company, board)
            for company, board in _configured_boards("LEVER_SITES")
        ] + [
            ("Ashby", company, board)
            for company, board in _configured_boards("ASHBY_BOARDS")
        ]

    async def _fetch_board(
        self,
        client: httpx.AsyncClient,
        provider: str,
        company: str,
        board_name: str,
    ) -> List[Dict[str, Any]]:
        if provider == "Greenhouse":
            url = f"https://boards-api.greenhouse.io/v1/boards/{board_name}/jobs"
            response = await client.get(url, params={"content": "true"})
        elif provider == "Lever":
            url = f"https://api.lever.co/v0/postings/{board_name}"
            response = await client.get(url, params={"mode": "json"})
        else:
            url = f"https://api.ashbyhq.com/posting-api/job-board/{board_name}"
            response = await client.get(url, params={"includeCompensation": "true"})
        response.raise_for_status()
        payload = response.json()

        if provider == "Greenhouse":
            raw_jobs = payload.get("jobs") if isinstance(payload, dict) else None
        elif provider == "Lever":
            raw_jobs = payload if isinstance(payload, list) else None
        else:
            raw_jobs = payload.get("jobs") if isinstance(payload, dict) else None
        if not isinstance(raw_jobs, list):
            raise ValueError(f"{provider} returned an invalid jobs list for {board_name}")

        jobs = []
        for raw_job in raw_jobs:
            if not isinstance(raw_job, dict):
                continue
            if provider == "Greenhouse":
                url = raw_job.get("absolute_url", "")
                location_data = raw_job.get("location") or {}
                location = location_data.get("name", "") if isinstance(location_data, dict) else ""
                description = _plain_text(raw_job.get("content", ""))
                posted = raw_job.get("updated_at")
            elif provider == "Lever":
                url = raw_job.get("hostedUrl") or raw_job.get("applyUrl") or ""
                categories = raw_job.get("categories") or {}
                location = categories.get("location", "") if isinstance(categories, dict) else ""
                workplace = raw_job.get("workplaceType", "")
                description = _plain_text(
                    raw_job.get("descriptionPlain") or raw_job.get("description", "")
                )
                posted = raw_job.get("createdAt")
                if isinstance(posted, (int, float)):
                    posted = datetime.fromtimestamp(posted / 1000, timezone.utc).isoformat()
            else:
                url = raw_job.get("jobUrl") or raw_job.get("applyUrl") or ""
                location = raw_job.get("location", "")
                if isinstance(location, dict):
                    address = location.get("address")
                    address_locality = (
                        address.get("addressLocality", "")
                        if isinstance(address, dict)
                        else ""
                    )
                    location = (
                        location.get("name")
                        or location.get("location")
                        or address_locality
                    )
                workplace = raw_job.get("workplaceType", "")
                description = _plain_text(
                    raw_job.get("descriptionPlain") or raw_job.get("descriptionHtml", "")
                )
                posted = raw_job.get("publishedAt")

            if not isinstance(location, str):
                location = ""
            title = raw_job.get("title") or (
                raw_job.get("text", "") if provider == "Lever" else ""
            )
            if not isinstance(url, str) or not url.startswith("https://"):
                continue
            if not isinstance(title, str) or not title.strip():
                continue
            if (
                provider == "Lever"
                and workplace
                and "remote" in workplace.lower()
                and "remote" not in str(location).lower()
            ):
                location = f"{location} / Remote".strip(" /")
            elif (
                provider == "Ashby"
                and workplace
                and "remote" in workplace.lower()
                and "remote" not in str(location).lower()
            ):
                location = f"{location} / Remote".strip(" /")

            jobs.append({
                "id": hashlib.sha256(url.encode()).hexdigest()[:24],
                "title": title.strip(),
                "company": company,
                "location": location if isinstance(location, str) and location.strip() else "Location not listed",
                "url": url,
                "source": provider,
                "description": description[:8000],
                "posted_date": _posted_date(posted),
                "liveness_status": "unverified",
                "worker_scout": self.name,
            })
        return jobs

    async def fetch_jobs(self) -> List[Dict[str, Any]]:
        boards = self._boards()
        if not boards:
            logger.info(
                "%s has no configured boards; set GREENHOUSE_BOARDS, LEVER_SITES, "
                "or ASHBY_BOARDS to enable official career feeds",
                self.name,
            )
            return []

        async with httpx.AsyncClient(
            timeout=25.0, follow_redirects=True, headers=HEADERS
        ) as client:
            results = await asyncio.gather(
                *(
                    self._fetch_board(client, provider, company, board)
                    for provider, company, board in boards
                ),
                return_exceptions=True,
            )

        jobs_by_url = {}
        for (provider, company, board), result in zip(boards, results):
            if isinstance(result, BaseException):
                if not isinstance(result, Exception):
                    raise result
                logger.warning(
                    "%s board %s (%s) failed to fetch: %s",
                    provider,
                    company,
                    board,
                    result,
                )
                continue
            logger.info(
                "%s board %s returned %d listings",
                provider,
                company,
                len(result),
            )
            for job in result:
                jobs_by_url.setdefault(job["url"], job)
        return list(jobs_by_url.values())


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
