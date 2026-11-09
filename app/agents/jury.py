from typing import List, Dict, Any, Tuple
from app.agents.sub_agents import SoftwareSystemsAgent, QualityOperationsAgent, RelocationVisaAgent
from app.agents.cv_analyzer import assess_candidate_constraints, score_cv_match
from datetime import datetime, timezone

class SupremeJuryAgent:
    name = "Supreme Jury Agent"
    role = "Chief Career Arbiter & Final Decision Maker"
    avatar = "⚖️"

    def __init__(self):
        self.software_agent = SoftwareSystemsAgent()
        self.qa_agent = QualityOperationsAgent()
        self.visa_agent = RelocationVisaAgent()

    def deliberate_job(self, job: Dict[str, Any], profile: Dict[str, Any], learned_boost: int = 0, learned_reasons: List[str] = None) -> Dict[str, Any]:
        """Runs multi-agent cross-examination, incorporating learned behavioral preferences, and issues a jury verdict."""
        # Step 1: Sub-Agents evaluate the role
        sw_eval = self.software_agent.evaluate_job(job, profile)
        qa_eval = self.qa_agent.evaluate_job(job, profile)
        cv_match = score_cv_match(job, profile)
        preference_check = assess_candidate_constraints(job, profile)
        visa_eval = self.visa_agent.evaluate_job(job, profile)

        # Step 2: Debate Round - Agents challenge and comment on each other's domain perspectives
        arguments = []
        liveness_status = job.get("liveness_status", "unverified")
        if liveness_status == "live":
            availability_statement = (
                "The posting URL responded successfully and no closure message was detected. "
                "The page is reachable, but this check cannot guarantee the employer is still accepting applications."
            )
        elif liveness_status == "expired":
            availability_statement = (
                "The posting URL returned HTTP 404/410. The listing appears removed, so the Jury must not recommend applying."
            )
        elif liveness_status == "unavailable":
            availability_statement = (
                "The posting page says the role is closed, filled, expired, or no longer accepting applications. "
                "The Jury must not recommend applying."
            )
        else:
            availability_statement = (
                "The posting could not be verified (for example, the site blocked the check or did not respond). "
                "The Jury cannot confirm availability and will not include it in today's apply roster."
            )

        arguments.append({
            "agent_name": "Listing Availability Examiner",
            "agent_role": "Posting Link & Availability Verification",
            "avatar": "🔎",
            "type": "availability_check",
            "statement": availability_statement,
            "score": 100 if liveness_status == "live" else 0
        })

        authenticity_status = job.get("authenticity_status", "unverified")
        authenticity_evidence = job.get(
            "authenticity_evidence",
            "The employer and posting domain could not be verified.",
        )
        if authenticity_status == "likely_official":
            authenticity_statement = (
                f"Positive domain signal: {authenticity_evidence} "
                "A matching domain or recruiting platform does not prove that the employer or vacancy is genuine."
            )
        elif authenticity_status == "suspicious":
            authenticity_statement = (
                f"OBJECTION: {authenticity_evidence} The Jury rejects this link; do not enter personal information or apply through it."
            )
        else:
            authenticity_statement = (
                f"Authenticity remains unverified: {authenticity_evidence} "
                "Check the employer's official careers page before sharing personal information."
            )
        arguments.append({
            "agent_name": "Employer Authenticity Examiner",
            "agent_role": "Employer Domain & Recruiting Platform Review",
            "avatar": "🛡️",
            "type": "authenticity_check",
            "statement": authenticity_statement,
            "score": 100 if authenticity_status == "likely_official" else (0 if authenticity_status == "suspicious" else 50),
        })

        # Software Pitch
        arguments.append({
            "agent_name": self.software_agent.name,
            "agent_role": self.software_agent.role,
            "avatar": self.software_agent.avatar,
            "type": "pitch",
            "statement": sw_eval["advocacy_pitch"],
            "score": sw_eval["score"]
        })

        # Visa Challenge or Endorsement
        visa_critique = self.visa_agent.challenge(job, self.software_agent.name, profile)
        arguments.append({
            "agent_name": self.visa_agent.name,
            "agent_role": self.visa_agent.role,
            "avatar": self.visa_agent.avatar,
            "type": "challenge" if "Dissent" in visa_critique["stance"] or "OBJECTION" in visa_critique["argument"] else "endorsement",
            "statement": f"[{visa_critique['stance']}] {visa_critique['argument']}",
            "score": visa_eval["score"]
        })

        # QA / Operational Assessment
        qa_critique = self.qa_agent.challenge(job, self.software_agent.name, profile)
        arguments.append({
            "agent_name": self.qa_agent.name,
            "agent_role": self.qa_agent.role,
            "avatar": self.qa_agent.avatar,
            "type": "assessment",
            "statement": f"[{qa_critique['stance']}] {qa_critique['argument']}",
            "score": qa_eval["score"]
        })

        matched_cv_signals = []
        if cv_match["matched_skills"]:
            matched_cv_signals.append(f"CV skills: {', '.join(cv_match['matched_skills'])}")
        if cv_match["matched_roles"]:
            matched_cv_signals.append(f"target roles: {', '.join(cv_match['matched_roles'])}")
        cv_match_statement = (
            f"Candidate profile match is {cv_match['score']}/100. "
            + ("Matched " + "; ".join(matched_cv_signals) + "." if matched_cv_signals
               else "No direct CV-skill or target-role evidence was found in this listing.")
        )
        arguments.append({
            "agent_name": "CV & Target Role Analyst",
            "agent_role": "Candidate Experience and Role Alignment",
            "avatar": "📄",
            "type": "cv_match",
            "statement": cv_match_statement,
            "score": cv_match["score"]
        })

        preference_statement = (
            "OBJECTION: " + " ".join(preference_check["reasons"])
            if preference_check["reasons"]
            else (
                "The listing does not conflict with the candidate's entry-level preference, "
                "stated skill exclusions, or avoided role types."
            )
        )
        arguments.append({
            "agent_name": "Candidate Experience Fit Examiner",
            "agent_role": "Seniority, Skills & Role Preference Review",
            "avatar": "🧭",
            "type": "candidate_constraints",
            "statement": preference_statement,
            "score": 100 if preference_check["eligible"] else 0,
        })

        if preference_check["preferred_city"] and preference_check["matches_preferred_city"]:
            location_statement = (
                f"The posting is in {preference_check['preferred_city']}, the candidate's preferred city. "
                "Prioritize it over otherwise comparable roles in other locations."
            )
        elif preference_check["preferred_city"]:
            location_statement = (
                f"The posting is not listed in {preference_check['preferred_city']}. "
                "Keep it as a fallback if it otherwise qualifies, but rank matching-city jobs first."
            )
        else:
            location_statement = (
                "No preferred city is saved in the candidate profile; location is neutral."
            )
        arguments.append({
            "agent_name": "City Preference Examiner",
            "agent_role": "Candidate Location Priority",
            "avatar": "📍",
            "type": "location_preference",
            "statement": location_statement,
            "score": 100 if preference_check["matches_preferred_city"] else 50,
        })
        if (
            preference_check["preferred_city"]
            and not preference_check["matches_preferred_city"]
        ):
            mobility_status = (
                "The role is remote/worldwide, so relocation and accommodation are not required."
                if visa_eval["relocation_status"] == "not_required"
                else (
                    "For this out-of-city role, mobility evidence is: "
                    f"{visa_eval['relocation_evidence']}; "
                    f"{visa_eval['accommodation_evidence']}. "
                    "Missing details are unverified, not confirmation that support is unavailable; "
                    "confirm the package with the employer."
                )
            )
            mobility_score = (
                100 if visa_eval["relocation_status"] == "not_required"
                else 80 if (
                    visa_eval["relocation_status"] == "supported"
                    and visa_eval["accommodation_status"] == "supported"
                )
                else 60 if (
                    visa_eval["relocation_status"] == "supported"
                    or visa_eval["accommodation_status"] == "supported"
                )
                else 30
            )
            arguments.append({
                "agent_name": "Relocation & Accommodation Examiner",
                "agent_role": "Out-of-City Mobility Support Review",
                "avatar": "🏠",
                "type": "relocation_accommodation",
                "statement": mobility_status,
                "score": mobility_score,
            })
        if preference_check["local_expat_status"] == "evidence_found":
            expat_statement = preference_check["local_expat_evidence"]
            expat_score = 80
        elif preference_check["local_expat_status"] == "restricted":
            expat_statement = "OBJECTION: " + preference_check["local_expat_evidence"]
            expat_score = 0
        elif preference_check["local_expat_status"] == "general_signal":
            expat_statement = "OBJECTION: " + preference_check["local_expat_evidence"]
            expat_score = 20
        elif preference_check["local_expat_status"] == "unverified":
            expat_statement = "OBJECTION: " + preference_check["local_expat_evidence"]
            expat_score = 0
        else:
            expat_statement = (
                "Not a posting in the candidate's preferred city; local Pakistani-expat eligibility check does not apply."
            )
            expat_score = 50
        arguments.append({
            "agent_name": "Local Expat Eligibility Examiner",
            "agent_role": "Pakistani Applicant & Saudi Work-Authorization Review",
            "avatar": "🛂",
            "type": "local_expat_eligibility",
            "statement": expat_statement,
            "score": expat_score,
        })

        # Step 3: Learned Preferences Agent Feedback
        if learned_reasons:
            arguments.append({
                "agent_name": "Agent Training Engine",
                "agent_role": "Behavioral Reinforcement Learner",
                "avatar": "🧠",
                "type": "learned_preference",
                "statement": f"Trained on your clicks & selections: {'; '.join(learned_reasons)} (Score delta: {'+' if learned_boost >= 0 else ''}{learned_boost}).",
                "score": max(min(50 + learned_boost * 2, 100), 10)
            })

        # Step 4: Jury Weighs the Debate & Learned Preference Signals
        has_blocker = visa_eval.get("has_blocker", False)
        listing_available = liveness_status == "live"
        sw_score = sw_eval["score"]
        visa_score = visa_eval["score"]
        qa_score = qa_eval["score"]

        # Include CV and target-role alignment directly in the Jury's weighted decision.
        base_score = int(
            (sw_score * 0.30)
            + (visa_score * 0.25)
            + (qa_score * 0.20)
            + (cv_match["score"] * 0.25)
        )

        if (
            liveness_status in ("expired", "unavailable")
            or authenticity_status == "suspicious"
            or not preference_check["eligible"]
        ):
            consensus_score = 0
        elif has_blocker:
            consensus_score = min(base_score, 20)
        else:
            # Apply learned user reinforcement boost
            consensus_score = min(max(base_score + learned_boost, 10), 100)

        if not listing_available and liveness_status not in ("expired", "unavailable"):
            consensus_score = min(consensus_score, 59)

        # Determine primary advocate
        scores = [
            (self.software_agent.name, sw_score),
            (self.visa_agent.name, visa_score),
            (self.qa_agent.name, qa_score)
        ]
        scores.sort(key=lambda x: x[1], reverse=True)
        primary_advocate = scores[0][0]

        # Step 5: Issue Formal Jury Verdict
        if liveness_status == "expired":
            verdict_status = "REJECTED BY JURY (POSTING LINK RETURNED 404/410)"
            summary = "The Jury found that the job posting URL is no longer available (HTTP 404/410)."
            recommendation = "Do not apply through this link. The role was excluded from today's apply roster."
        elif liveness_status == "unavailable":
            verdict_status = "REJECTED BY JURY (ROLE NO LONGER AVAILABLE)"
            summary = "The posting page indicates that this role is closed, filled, expired, or no longer accepting applications."
            recommendation = "Do not apply. The role was excluded from today's apply roster."
        elif authenticity_status == "suspicious":
            verdict_status = "REJECTED BY JURY (SUSPICIOUS POSTING LINK)"
            summary = "The Jury found a clear security red flag in the posting destination."
            recommendation = "Do not apply through this link or enter personal information. Find the role through the employer's official careers page."
        elif preference_check["local_expat_status"] == "restricted":
            verdict_status = "REJECTED BY JURY (NON-SAUDI APPLICANTS RESTRICTED)"
            summary = preference_check["local_expat_evidence"]
            recommendation = "Do not prioritize this local role because the posting excludes non-Saudi applicants."
        elif preference_check["local_expat_status"] in ("unverified", "general_signal"):
            verdict_status = "DEFERRED BY JURY (LOCAL EXPAT ELIGIBILITY UNVERIFIED)"
            summary = preference_check["local_expat_evidence"]
            recommendation = "Contact the employer to confirm that Pakistani applicants are eligible before applying."
        elif not preference_check["eligible"]:
            verdict_status = "REJECTED BY JURY (CANDIDATE EXPERIENCE MISMATCH)"
            summary = "The posting conflicts with the candidate's stated experience level, skill exclusions, or preferred role types."
            recommendation = (
                "Do not prioritize this role. "
                + " ".join(preference_check["reasons"])
            )
        elif not listing_available:
            verdict_status = "DEFERRED BY JURY (AVAILABILITY UNVERIFIED)"
            summary = "The Jury could not verify that the posting page is reachable and available."
            recommendation = "Check the employer careers page manually before applying. This role is excluded from today's apply roster until verified."
        elif has_blocker:
            verdict_status = "REJECTED BY JURY (IMMIGRATION BARRIER)"
            summary = "The Jury rejects this vacancy. Despite potential CV alignment, the employer explicitly excludes applicants requiring work sponsorship."
            recommendation = "Do not apply. Avoid burning valuable candidate pipeline on legally restricted vacancies."
        elif consensus_score >= 70:
            verdict_status = "UNANIMOUS JURY APPROVAL — DAILY ROSTER"
            summary = (
                f"Strong consensus achieved. The listing aligns with the candidate's CV and target roles, "
                f"with technical scope praised by {self.software_agent.name} and international viability "
                f"assessed by {self.visa_agent.name}."
            )
            recommendation = "High priority: Tailor your CV to the role's matching requirements and apply promptly."
        elif consensus_score >= 50:
            verdict_status = "APPROVED BY MAJORITY VOTE"
            summary = f"The Jury approves this role by majority vote. Competitive engineering alignment, though specific relocation terms require verification."
            recommendation = "Recommended: Suitable for your secondary application queue. Inquire regarding visa support during initial recruiter contact."
        else:
            verdict_status = "DEFERRED / ARCHIVED"
            summary = "The Jury withholds recommendation due to insufficient skill overlap or unconfirmed sponsorship evidence."
            recommendation = "Archive for now. Prioritize higher-consensus opportunities."

        strengths = []
        if sw_eval.get("matched_keywords"):
            strengths.append(f"Stack fit: {', '.join(sw_eval['matched_keywords'][:4])}")
        if visa_eval.get("has_visa"):
            strengths.append("Confirmed visa sponsorship support")
        if visa_eval.get("has_housing"):
            strengths.append("Relocation or accommodation package offered")
        if not strengths:
            strengths.append("General alignment with engineering analytical competencies")

        risks = sw_eval.get("concerns", []) + visa_eval.get("concerns", []) + qa_eval.get("concerns", [])
        if not risks:
            risks.append("Competitive international candidate pool")

        return {
            "job_id": job.get("id"),
            "consensus_score": consensus_score,
            "verdict_status": verdict_status,
            "summary": summary,
            "strengths": "; ".join(strengths),
            "risks": "; ".join(risks[:2]),
            "recommendation": recommendation,
            "advocate_agent": primary_advocate,
            "visa_evidence": visa_eval.get("visa_evidence", "Not verified"),
            "housing_evidence": visa_eval.get("housing_evidence", "Not verified"),
            "relocation_status": visa_eval["relocation_status"],
            "accommodation_status": visa_eval["accommodation_status"],
            "relocation_evidence": visa_eval["relocation_evidence"],
            "accommodation_evidence": visa_eval["accommodation_evidence"],
            "arguments": arguments,
            "preferred_city": preference_check["preferred_city"],
            "matches_preferred_city": preference_check["matches_preferred_city"],
            "local_expat_status": preference_check["local_expat_status"],
            "local_expat_evidence": preference_check["local_expat_evidence"],
            "is_selected": (
                consensus_score >= 65
                and not has_blocker
                and listing_available
                and authenticity_status != "suspicious"
                and preference_check["eligible"]
            )
        }

    def run_full_session(self, candidate_jobs: List[Dict[str, Any]], profile: Dict[str, Any], max_daily_picks: int = 5, learned_boosts: Dict[str, Tuple[int, List[str]]] = None) -> Dict[str, Any]:
        """Deliberates over all candidate jobs and returns session outcomes with top daily picks."""
        import uuid
        session_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
        deliberated_jobs = []

        for job in candidate_jobs:
            jid = job.get("id")
            boost, reasons = (learned_boosts.get(jid, (0, []))) if learned_boosts else (0, [])
            delib = self.deliberate_job(job, profile, learned_boost=boost, learned_reasons=reasons)
            # Merge back into job dict
            job_copy = dict(job)
            job_copy["jury_score"] = delib["consensus_score"]
            job_copy["verdict_status"] = delib["verdict_status"]
            job_copy["jury_summary"] = delib["summary"]
            job_copy["jury_recommendation"] = delib["recommendation"]
            job_copy["advocate_agent"] = delib["advocate_agent"]
            job_copy["visa_evidence"] = delib["visa_evidence"]
            job_copy["housing_evidence"] = delib["housing_evidence"]
            job_copy["relocation_status"] = delib["relocation_status"]
            job_copy["accommodation_status"] = delib["accommodation_status"]
            job_copy["relocation_evidence"] = delib["relocation_evidence"]
            job_copy["accommodation_evidence"] = delib["accommodation_evidence"]
            job_copy["arguments"] = delib["arguments"]
            job_copy["preferred_city"] = delib["preferred_city"]
            job_copy["matches_preferred_city"] = delib["matches_preferred_city"]
            job_copy["local_expat_status"] = delib["local_expat_status"]
            job_copy["local_expat_evidence"] = delib["local_expat_evidence"]
            job_copy["is_daily_pick"] = 0
            deliberated_jobs.append(job_copy)

        # Sort by consensus jury score descending
        deliberated_jobs.sort(
            key=lambda x: (x["matches_preferred_city"], x["jury_score"]),
            reverse=True,
        )

        # Crown the highest-ranked majority-approved live jobs, up to the daily limit.
        crowned_count = 0
        for j in deliberated_jobs:
            if (
                crowned_count < max_daily_picks
                and j["jury_score"] >= 50
                and j["verdict_status"] in (
                    "UNANIMOUS JURY APPROVAL — DAILY ROSTER",
                    "APPROVED BY MAJORITY VOTE",
                )
                and j.get("liveness_status") == "live"
                and j.get("authenticity_status", "unverified") != "suspicious"
            ):
                j["is_daily_pick"] = 1
                crowned_count += 1

        session_summary = (
            f"Jury Session {session_id} adjourned: Evaluated {len(candidate_jobs)} candidates. "
            f"Sub-Agents completed debate across technical depth, operational scope, and immigration feasibility. "
            f"Selected {crowned_count} top vacancies for the candidate's Daily Roster."
        )

        return {
            "session_id": session_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "summary": session_summary,
            "crowned_count": crowned_count,
            "jobs": deliberated_jobs
        }
