import re
from typing import Dict, Any, List

class BaseSubAgent:
    name: str = "Base Sub-Agent"
    role: str = "Evaluator"
    avatar: str = "🤖"

    def evaluate_job(self, job: Dict[str, Any], profile: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError

    def challenge(self, target_job: Dict[str, Any], opponent_agent_name: str, profile: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError


class SoftwareSystemsAgent(BaseSubAgent):
    name = "Software & Systems Agent"
    role = "Lead Engineering Screener"
    avatar = "💻"

    CORE_TECH = ["python", "c++", "java", "flutter", "firebase", "embedded", "c", "backend", "full-stack", "software", "developer", "engineer"]

    def evaluate_job(self, job: Dict[str, Any], profile: Dict[str, Any]) -> Dict[str, Any]:
        text = f"{job.get('title', '')} {job.get('description', '')}".lower()
        title = job.get("title", "").lower()

        matched_tech = []
        for tech in self.CORE_TECH:
            if tech in ["c++", "c"]:
                if tech in text:
                    matched_tech.append(tech)
            else:
                if re.search(r'\b' + re.escape(tech) + r'\b', text):
                    matched_tech.append(tech)

        is_engineer = any(term in title for term in ["software", "developer", "engineer", "systems", "embedded", "backend", "full stack", "mobile"])
        is_junior_or_grad = any(term in title or term in text for term in ["graduate", "junior", "entry", "associate", "trainee", "early career", "0-2 years"])

        score = min(80, len(matched_tech) * 12) + (15 if is_engineer else 0) + (10 if is_junior_or_grad else 0)
        score = min(max(score, 15), 100)

        # Build pitch
        tech_str = ", ".join(matched_tech[:4]) if matched_tech else "core engineering foundations"
        if is_engineer and is_junior_or_grad:
            pitch = f"Strong engineering nomination: Directly aligned with the candidate's engineering profile and stack evidence on {tech_str}."
        elif is_engineer:
            pitch = f"Technically ambitious engineering role utilizing {tech_str}. High growth potential."
        else:
            pitch = f"Engineering relevant vacancy matching technical facets ({tech_str})."

        concerns = []
        if any(term in title for term in ["senior", "lead", "principal", "staff"]):
            concerns.append("Seniority title may require significant prior enterprise leadership experience.")
        if len(matched_tech) == 0:
            concerns.append("Lacks explicit evidence of the candidate's primary programming skills.")

        return {
            "score": score,
            "advocacy_pitch": pitch,
            "concerns": concerns,
            "matched_keywords": matched_tech,
            "is_preferred": score >= 50
        }

    def challenge(self, target_job: Dict[str, Any], opponent_agent_name: str, profile: Dict[str, Any]) -> Dict[str, Any]:
        title = target_job.get("title", "").lower()
        desc = target_job.get("description", "").lower()

        if any(term in title for term in ["senior", "lead", "principal", "head"]):
            return {
                "stance": "Dissent / Caution",
                "argument": f"I caution the Jury: {target_job.get('title')} at {target_job.get('company')} is tagged as a Senior/Lead position. Applicants without matching senior experience may face immediate screening rejection."
            }
        if not any(term in desc for term in ["python", "c++", "java", "code", "programming", "software", "git", "engineering"]):
            return {
                "stance": "Concern",
                "argument": f"The technical substance in {target_job.get('company')}'s listing appears sparse on software development depth. We should prioritize roles that cultivate her core engineering degree."
            }
        return {
            "stance": "Endorsement",
            "argument": f"I concur with {opponent_agent_name}: The engineering stack at {target_job.get('company')} is solid and offers practical code ownership."
        }


class QualityOperationsAgent(BaseSubAgent):
    name = "Quality & Operations Agent"
    role = "QA & Reliability Specialist"
    avatar = "🎯"

    QA_TECH = ["qa", "quality", "test", "testing", "automation", "support", "it support", "analyst", "systems", "troubleshooting", "hardware", "diagnostics"]

    def evaluate_job(self, job: Dict[str, Any], profile: Dict[str, Any]) -> Dict[str, Any]:
        text = f"{job.get('title', '')} {job.get('description', '')}".lower()
        title = job.get("title", "").lower()

        matched_qa = []
        for term in self.QA_TECH:
            if re.search(r'\b' + re.escape(term) + r'\b', text):
                matched_qa.append(term)

        is_qa_or_support = any(term in title for term in ["qa", "quality", "test", "support", "analyst", "operations", "systems engineer"])
        is_accessible = any(term in title or term in text for term in ["graduate", "junior", "entry", "associate", "analyst", "support engineer"])

        score = min(75, len(matched_qa) * 14) + (15 if is_qa_or_support else 0) + (10 if is_accessible else 0)
        score = min(max(score, 10), 95)

        matches_str = ", ".join(matched_qa[:4]) if matched_qa else "reliability and diagnostics"
        if is_qa_or_support and is_accessible:
            pitch = f"Exceptional operational opportunity: Directly aligns with the candidate's QA, IT Support, and Systems Engineering objectives ({matches_str})."
        elif is_qa_or_support:
            pitch = f"Solid test and quality analysis position touching {matches_str}."
        else:
            pitch = f"Offers practical operational crossover in {matches_str}."

        concerns = []
        if not is_qa_or_support and not is_accessible:
            concerns.append("May lean toward non-standard responsibilities outside core systems operations.")

        return {
            "score": score,
            "advocacy_pitch": pitch,
            "concerns": concerns,
            "matched_keywords": matched_qa,
            "is_preferred": score >= 45
        }

    def challenge(self, target_job: Dict[str, Any], opponent_agent_name: str, profile: Dict[str, Any]) -> Dict[str, Any]:
        title = target_job.get("title", "").lower()
        desc = target_job.get("description", "").lower()

        if "support" in title or "analyst" in title or "qa" in title:
            return {
                "stance": "Strong Endorsement",
                "argument": f"I strongly back this role. Positions like {target_job.get('title')} often have clearer onboarding ramps, practical test suites, and lower initial gatekeeping than ultra-competitive pure dev roles."
            }
        if any(term in desc for term in ["10+ years", "8+ years", "extensive track record"]):
            return {
                "stance": "Dissent",
                "argument": f"The job requirements at {target_job.get('company')} indicate a high experience barrier. We should protect the candidate's application time from likely automatic filtering."
            }
        return {
            "stance": "Neutral",
            "argument": f"Acceptable operational scope, provided {target_job.get('company')} provides structured mentorship."
        }


class RelocationVisaAgent(BaseSubAgent):
    name = "Visa & Relocation Advocate"
    role = "Immigration & Mobility Counsel"
    avatar = "🌍"

    VISA_KEYWORDS = [
        "visa sponsorship", "sponsorship available", "work permit",
        "visa support", "visa provided", "tier 2", "blue card", "skilled worker", "sponsorship"
    ]
    RELOCATION_KEYWORDS = [
        "relocation support", "relocation assistance", "relocation package",
        "relocation allowance", "relocation bonus", "moving assistance",
        "moving allowance", "flight allowance",
    ]
    HOUSING_KEYWORDS = [
        "accommodation provided", "accommodation allowance", "temporary accommodation",
        "housing provided", "housing allowance", "temporary housing",
    ]
    RELOCATION_BLOCKERS = (
        "no relocation support", "no relocation assistance", "relocation support is not available",
        "relocation assistance is not available", "relocation is not provided",
    )
    ACCOMMODATION_BLOCKERS = (
        "no accommodation", "accommodation is not provided", "accommodation not provided",
        "no housing", "housing is not provided", "housing not provided",
    )
    BLOCKER_KEYWORDS = [
        "no sponsorship", "must have right to work", "must be an eu citizen",
        "eu passport required", "cannot sponsor", "no visa", "us citizen only"
    ]

    def evaluate_job(self, job: Dict[str, Any], profile: Dict[str, Any]) -> Dict[str, Any]:
        text = f"{job.get('title', '')} {job.get('description', '')}".lower()
        loc = job.get("location", "").lower()

        visa_hits = [k for k in self.VISA_KEYWORDS if k in text]
        relocation_hits = [k for k in self.RELOCATION_KEYWORDS if k in text]
        housing_hits = [k for k in self.HOUSING_KEYWORDS if k in text]
        blockers = [k for k in self.BLOCKER_KEYWORDS if k in text]
        relocation_blockers = [k for k in self.RELOCATION_BLOCKERS if k in text]
        accommodation_blockers = [k for k in self.ACCOMMODATION_BLOCKERS if k in text]

        has_blocker = len(blockers) > 0
        has_visa = len(visa_hits) > 0
        has_relocation = len(relocation_hits) > 0
        has_housing = len(housing_hits) > 0
        is_remote = any(term in loc or term in text for term in ["remote", "worldwide", "anywhere", "global"])
        relocation_not_required = any(
            term in loc for term in ["remote", "worldwide", "anywhere", "global"]
        )

        score = 20
        if has_visa:
            score += 45
        if has_relocation or has_housing:
            score += 25
        if is_remote and not has_blocker:
            score += 20
        if has_blocker:
            score = max(5, score - 60)

        score = min(max(score, 5), 100)

        # Evidence texts
        visa_ev = ("Mentioned: " + ", ".join(visa_hits)) if has_visa else ("BLOCKED: " + ", ".join(blockers) if has_blocker else "Not verified")
        if relocation_not_required:
            relocation_status = "not_required"
            accommodation_status = "not_required"
        else:
            relocation_status = (
                "not_offered" if relocation_blockers
                else "supported" if has_relocation
                else "unverified"
            )
            accommodation_status = (
                "not_offered" if accommodation_blockers
                else "supported" if has_housing
                else "unverified"
            )

        relocation_ev = (
            "Not offered: " + ", ".join(relocation_blockers)
            if relocation_blockers else
            "Mentioned: " + ", ".join(relocation_hits)
            if has_relocation else
            "Not required for remote/worldwide location"
            if relocation_not_required else "Not verified"
        )
        accommodation_ev = (
            "Not offered: " + ", ".join(accommodation_blockers)
            if accommodation_blockers else
            "Mentioned: " + ", ".join(housing_hits)
            if has_housing else
            "Not required for remote/worldwide location"
            if relocation_not_required else "Not verified"
        )
        housing_ev = f"Relocation: {relocation_ev}; Accommodation: {accommodation_ev}"

        concerns = []
        if has_blocker:
            concerns.append(f"Legal barrier identified: '{blockers[0]}'. Employer likely rejects non-resident applicants.")
        elif not has_visa and not is_remote:
            concerns.append("No explicit visa sponsorship verified. High risk of candidate disqualification.")

        if has_visa and has_relocation and has_housing:
            pitch = (
                f"Strong international mobility evidence: visa support ({', '.join(visa_hits)}), "
                f"relocation ({', '.join(relocation_hits)}), and accommodation ({', '.join(housing_hits)})."
            )
        elif has_relocation or has_housing:
            supported = []
            if has_relocation:
                supported.append("relocation: " + ", ".join(relocation_hits))
            if has_housing:
                supported.append("accommodation: " + ", ".join(housing_hits))
            pitch = "Partial mobility evidence: " + "; ".join(supported) + ". Verify any missing support with the employer."
        elif has_visa:
            pitch = f"Verified international mobility: Explicit support for {', '.join(visa_hits)}."
        elif is_remote:
            pitch = "Global / Remote availability: Eliminates immediate work visa prerequisites."
        else:
            pitch = "Visa sponsorship not explicitly documented in the job text; requires manual confirmation."

        return {
            "score": score,
            "advocacy_pitch": pitch,
            "concerns": concerns,
            "visa_evidence": visa_ev,
            "housing_evidence": housing_ev,
            "has_blocker": has_blocker,
            "has_visa": has_visa,
            "has_relocation": has_relocation,
            "has_housing": has_housing,
            "relocation_status": relocation_status,
            "accommodation_status": accommodation_status,
            "relocation_evidence": relocation_ev,
            "accommodation_evidence": accommodation_ev,
            "is_preferred": (has_visa or is_remote) and not has_blocker
        }

    def challenge(self, target_job: Dict[str, Any], opponent_agent_name: str, profile: Dict[str, Any]) -> Dict[str, Any]:
        text = f"{target_job.get('title', '')} {target_job.get('description', '')}".lower()
        blockers = [k for k in self.BLOCKER_KEYWORDS if k in text]
        if blockers:
            citizenship = profile.get("citizenship") or "the candidate's"
            residence = profile.get("residence") or "current location"
            return {
                "stance": "Strict Veto / Block",
                "argument": f"OBJECTION TO {opponent_agent_name}: {target_job.get('company')} states '{blockers[0]}'. This may prevent a candidate with citizenship '{citizenship}' currently based in '{residence}' from qualifying."
            }
        has_visa = any(k in text for k in self.VISA_KEYWORDS)
        is_remote = any(k in target_job.get("location", "").lower() for k in ["remote", "worldwide", "global"])
        if has_visa:
            return {
                "stance": "Strong Endorsement",
                "argument": f"Full endorsement: {target_job.get('company')} provides relocation pathways that may make this role accessible to international candidates."
            }
        elif is_remote:
            return {
                "stance": "Favorable",
                "argument": f"Approved on remote terms: {target_job.get('company')} permits worldwide or regional remote engagement, bypassing immigration bureaucracy."
            }
        else:
            return {
                "stance": "Caution",
                "argument": f"Proceed with eyes open: {target_job.get('company')} does not advertise visa sponsorship. Unless an exceptional referral is available, international approval is uncertain."
            }
