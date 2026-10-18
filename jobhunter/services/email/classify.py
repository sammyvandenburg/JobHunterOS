"""Deterministic email classification. No model involved.

Kinds: receipt (automated application acknowledgement), rejection, interview,
offer, recruiter (a human message about a role), alert (job alert digest),
outreach_sent (the user's own outgoing message), other.
Automated acknowledgements are never counted as recruiter replies.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

NOREPLY = re.compile(r"(no-?reply|do-?not-?reply|notifications?@|noreply|jobs-noreply|alerts?@|mailer|system@|careers@|talent@|recruiting-?team@|hello@)", re.I)
ATS_DOMAINS = re.compile(r"@(?:[\w.-]+\.)?(greenhouse\.io|greenhouse-mail\.io|lever\.co|hire\.lever\.co|ashbyhq\.com|workable\.com|smartrecruiters\.com|myworkday\.com|icims\.com|jobvite\.com|bamboohr\.com|teamtailor\.com|recruitee\.com|personio\.de|personio\.com|join\.com|successfactors\.com|taleo\.net)", re.I)
ALERT_SENDER = re.compile(r"(jobalerts-noreply@linkedin\.com|jobs-listings@linkedin\.com|@indeed\.com|alert@|jobalert|job-alert|noreply@glassdoor|@jobs\.|@monster\.)", re.I)
RECEIPT = re.compile(r"(thank you for (your )?(application|applying|interest)|we (have )?received your application|application (has been )?(received|submitted)|your application (to|for|was)|confirmation of your application|successfully applied|application confirmation)", re.I)
REJECTION = re.compile(r"(unfortunately|we (will not|won't|are unable to|have decided not to) (be )?(moving|move|proceed)|not (been )?selected|decided to (move forward|proceed|pursue) with other|other candidates|not the right fit|will not be progressing|position has been filled|we('ve| have) decided to go|regret to inform|no longer under consideration)", re.I)
INTERVIEW = re.compile(r"(interview|schedule (a|an|your) (call|chat|conversation|meeting)|invite you to (a|an) (call|conversation|meeting)|next (step|stage|round)|technical (assessment|screen|challenge)|phone screen|calendly\.com|book a time|availability for a call|would love to (chat|talk|speak))", re.I)
OFFER = re.compile(r"(offer letter|pleased to offer|job offer|extend (you )?an offer|offer of employment|employment contract|we would like to offer)", re.I)
RECRUITER = re.compile(r"(recruiter|talent (acquisition|partner)|sourcing|hiring manager|your (background|profile|experience) (caught|stood)|came across your profile|opportunity (at|with)|role (at|with)|we('re| are) hiring|open (role|position))", re.I)
AUTOMATED_HEADERS = ("list-unsubscribe", "auto-submitted", "precedence")


@dataclass
class Classification:
    kind: str
    confidence: str  # high | medium | low
    automated: bool
    needs_body: bool = False
    reason: str = ""


def classify(sender: str, subject: str, text: str, headers: dict | None = None, outgoing: bool = False) -> Classification:
    headers = headers or {}
    s = f"{subject}\n{text}"
    automated = bool(NOREPLY.search(sender) or ATS_DOMAINS.search(sender) or any(h in headers for h in AUTOMATED_HEADERS))
    if outgoing:
        return Classification("outreach_sent", "high", False, reason="sent by you")
    if ALERT_SENDER.search(sender) or re.search(r"(job alert|jobs? (you may|that match|for you)|new jobs?|recommended jobs?)", subject, re.I):
        return Classification("alert", "high", True, reason="job alert sender or subject")
    if OFFER.search(s):
        return Classification("offer", "medium" if automated else "high", automated, needs_body=True, reason="offer wording")
    if INTERVIEW.search(s) and not RECEIPT.search(subject):
        conf = "medium" if (automated or not text) else "high"
        return Classification("interview", conf, automated, needs_body=not text, reason="interview wording")
    if REJECTION.search(s):
        return Classification("rejection", "high" if REJECTION.search(text) else "medium", automated, needs_body=not text, reason="rejection wording")
    if RECEIPT.search(s) or (automated and re.search(r"application", subject, re.I)):
        return Classification("receipt", "high", True, reason="application receipt wording or ATS sender")
    if not automated and RECRUITER.search(s):
        return Classification("recruiter", "medium", False, needs_body=not text, reason="human sender with role wording")
    if not automated and re.search(r"(application|applied|role|position|job|vacancy)", s, re.I):
        return Classification("recruiter", "low", False, needs_body=not text, reason="human sender mentioning a job")
    return Classification("other", "low", automated, reason="no job-search pattern")
