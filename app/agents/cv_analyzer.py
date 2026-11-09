import re
import json
from typing import Any, Dict


SKILL_PATTERNS = {
    "Python": (r"\bpython\b",),
    "C++": (r"(?<!\w)c\+\+(?!\w)",),
    "Java": (r"\bjava\b",),
    "JavaScript": (r"\bjavascript\b", r"\bjs\b"),
    "TypeScript": (r"\btypescript\b",),
    "C#": (r"(?<!\w)c#(?!\w)", r"\bc-sharp\b"),
    "C": (r"\bc\b",),
    "SQL": (r"\bsql\b",),
    "HTML": (r"\bhtml\b",),
    "CSS": (r"\bcss\b",),
    "React": (r"\breact(?:\.js)?\b",),
    "Flutter": (r"\bflutter\b",),
    "Dart": (r"\bdart\b",),
    "Android": (r"\bandroid\b",),
    "Kotlin": (r"\bkotlin\b",),
    "Swift": (r"\bswift\b",),
    "Firebase": (r"\bfirebase\b",),
    "Embedded Systems": (r"\bembedded systems?\b", r"\bmicrocontrollers?\b", r"\bfirmware\b"),
    "QA / Testing": (r"\bqa\b", r"\bquality assurance\b", r"\btest(?:ing| automation)?\b"),
    "Selenium": (r"\bselenium\b",),
    "Playwright": (r"\bplaywright\b",),
    "Linux": (r"\blinux\b",),
    "Networking": (r"\bnetwork(?:ing|s)?\b",),
    "IT Support": (r"\bit support\b", r"\bhelp ?desk\b", r"\btechnical support\b"),
    "Docker": (r"\bdocker\b",),
    "Kubernetes": (r"\bkubernetes\b", r"\bk8s\b"),
    "AWS": (r"\baws\b", r"\bamazon web services\b"),
    "Azure": (r"\bazure\b",),
    "Machine Learning": (r"\bmachine learning\b", r"\bml\b"),
    "Data Analysis": (r"\bdata analy(?:sis|tics)\b", r"\bpandas\b", r"\btableau\b", r"\bpower bi\b"),
    "Excel": (r"\bexcel\b", r"\bspreadsheets?\b"),
    "Windows": (r"\bwindows\b",),
    "CRM": (r"\bcrm\b",),
    "CAD": (r"\bcad\b",),
    "Cybersecurity": (r"\bcyber ?security\b", r"\bsiem\b", r"\bpenetration testing\b"),
    "Project Management": (r"\bproject management\b", r"\bagile\b", r"\bscrum\b"),
    "UI / UX Design": (r"\bui/?ux\b", r"\buser experience\b", r"\bfigma\b"),
    "Graphic Design": (r"\bgraphic design\b", r"\badobe (?:photoshop|illustrator)\b"),
    "Digital Marketing": (r"\bdigital marketing\b", r"\bseo\b", r"\bgoogle ads\b"),
    "Accounting": (r"\baccount(?:ing|antancy)\b", r"\bbookkeeping\b", r"\bquickbooks\b"),
    "Human Resources": (r"\bhuman resources\b", r"\brecruit(?:ing|ment)\b", r"\bpayroll\b"),
    "Sales": (r"\bsales\b", r"\bcustomer acquisition\b", r"\bcrm\b"),
    "Supply Chain": (r"\bsupply chain\b", r"\blogistics\b", r"\binventory management\b"),
    "Civil Engineering": (r"\bcivil engineering\b", r"\bstructural engineering\b", r"\bautocad\b"),
    "Mechanical Engineering": (r"\bmechanical engineering\b", r"\bsolidworks\b", r"\bcad\b"),
    "Electrical Engineering": (r"\belectrical engineering\b", r"\bplc\b", r"\bcircuit design\b"),
}

ROLE_EVIDENCE = {
    "Software Engineer": (
        "Python", "C++", "Java", "JavaScript", "TypeScript", "C#", "SQL", "React"
    ),
    "Frontend Developer": ("JavaScript", "TypeScript", "HTML", "CSS", "React"),
    "Mobile App Developer": ("Flutter", "Dart", "Android", "Kotlin", "Swift", "Firebase"),
    "QA Automation Engineer": ("QA / Testing", "Selenium", "Playwright", "Python", "Java"),
    "Embedded Systems Engineer": ("Embedded Systems", "C++", "C", "Python", "Electrical Engineering"),
    "IT Support Engineer": ("IT Support", "Networking", "Linux", "Windows"),
    "DevOps / Cloud Engineer": ("Docker", "Kubernetes", "AWS", "Azure", "Linux"),
    "Data Analyst": ("Data Analysis", "SQL", "Excel", "Python", "Machine Learning"),
    "Cybersecurity Analyst": ("Cybersecurity", "Linux", "Networking", "Python"),
    "Project Coordinator": ("Project Management", "Excel"),
    "UI/UX Designer": ("UI / UX Design", "Graphic Design"),
    "Digital Marketing Specialist": ("Digital Marketing", "Data Analysis", "Excel"),
    "Accountant": ("Accounting", "Excel"),
    "Human Resources Coordinator": ("Human Resources", "Excel"),
    "Sales Representative": ("Sales", "CRM"),
    "Operations Analyst": ("Supply Chain", "Project Management", "Excel", "Data Analysis"),
    "Civil Engineer": ("Civil Engineering", "Project Management"),
    "Mechanical Engineer": ("Mechanical Engineering", "Project Management"),
    "Electrical Engineer": ("Electrical Engineering", "Embedded Systems", "C++"),
}


def analyze_cv(cv_text: str) -> Dict[str, Any]:
    """Derive explainable skill and role recommendations from CV text without external services."""
    text = re.sub(r"\s+", " ", cv_text or "").strip()
    lowered = text.lower()
    skills = [
        skill for skill, patterns in SKILL_PATTERNS.items()
        if any(re.search(pattern, lowered) for pattern in patterns)
    ]

    scored_roles = []
    for role, evidence in ROLE_EVIDENCE.items():
        matched = [skill for skill in evidence if skill in skills]
        if matched:
            scored_roles.append((role, len(matched), matched))
    scored_roles.sort(key=lambda result: (-result[1], result[0]))

    seniority = "unspecified"
    years = [
        int(match.group(1))
        for match in re.finditer(r"\b(\d{1,2})\+?\s+years?\b", lowered)
    ]
    if any(term in lowered for term in ("senior", "lead engineer", "principal engineer")) or any(year >= 5 for year in years):
        seniority = "experienced"
    elif any(term in lowered for term in ("graduate", "entry-level", "entry level", "junior", "intern", "student", "fresh graduate")) or any(year <= 2 for year in years):
        seniority = "early-career"

    roles = []
    for role, _, _ in scored_roles[:5]:
        if seniority == "early-career" and role not in ("Accountant", "Sales Representative"):
            role = f"Junior {role}"
        elif seniority == "experienced" and role not in ("Accountant", "Sales Representative"):
            role = f"Senior {role}"
        roles.append(role)

    if skills and roles:
        summary = f"Found {len(skills)} skills and matched them to {len(roles)} potential role paths."
    elif skills:
        summary = f"Found {len(skills)} skills, but there is not enough role-specific evidence to recommend target roles yet."
    elif text:
        summary = "CV text was analyzed, but no supported skills or role signals were detected. Add clearer skills or experience details."
    else:
        summary = "Add or upload CV text to analyze skills and suggest target roles."

    return {
        "skills": skills,
        "roles": roles,
        "seniority": seniority,
        "summary": summary,
    }


def get_cv_analysis(profile: Dict[str, Any]) -> Dict[str, Any]:
    """Read persisted analysis, or derive it for older profiles without saved analysis."""
    raw_analysis = profile.get("cv_analysis")
    if isinstance(raw_analysis, dict):
        return raw_analysis
    if isinstance(raw_analysis, str) and raw_analysis:
        try:
            parsed = json.loads(raw_analysis)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
    return analyze_cv(profile.get("cv_text") or "")


def score_cv_match(job: Dict[str, Any], profile: Dict[str, Any]) -> Dict[str, Any]:
    analysis = get_cv_analysis(profile)
    text = f"{job.get('title', '')} {job.get('description', '')}".lower()
    candidate_skills = list(dict.fromkeys(
        list(analysis.get("skills", []))
        + [skill.strip() for skill in (profile.get("skills") or "").split(",") if skill.strip()]
    ))
    matched_skills = [
        skill for skill in candidate_skills
        if re.search(r"(?<!\w)" + re.escape(skill.lower()) + r"(?!\w)", text)
    ]

    job_title = re.sub(r"[^a-z0-9+#]+", " ", (job.get("title") or "").lower()).strip()
    target_roles = analysis.get("roles") or [
        role.strip() for role in (profile.get("target_roles") or "").split(",") if role.strip()
    ]
    role_matches = [
        role for role in target_roles
        if any(
            token in job_title
            for token in re.sub(r"[^a-z0-9+#]+", " ", role.lower()).split()
            if token not in {"junior", "senior", "graduate", "engineer", "developer", "specialist", "analyst"}
        )
    ]
    if any(role.lower() in job_title for role in target_roles):
        role_matches = list(target_roles)

    score = 30 + min(len(matched_skills) * 12, 48) + (22 if role_matches else 0)
    return {
        "score": min(score, 100),
        "matched_skills": matched_skills[:6],
        "matched_roles": role_matches[:3],
        "recommended_roles": analysis.get("roles", []),
    }
