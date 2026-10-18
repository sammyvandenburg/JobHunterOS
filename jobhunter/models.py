"""Typed schemas shared by the CLI, services, dashboard, and model boundary.

These are the only shapes the model is allowed to produce; everything it
returns is validated against them before it touches the database.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

Confidence = Literal["explicit", "asserted", "inferred"]
FactCategory = Literal[
    "role",
    "skill",
    "certification",
    "course",
    "project",
    "education",
    "achievement",
    "transferable",
    "other",
]
ExperienceKind = Literal["professional", "personal", "guided", "educational", "team", "unknown"]


class EvidenceRef(BaseModel):
    """Where a fact comes from. Every substantive fact needs at least one."""

    type: Literal["document", "repository", "link", "user"]
    ref: str = Field(..., description="document:<id>#<section/page>, repo URL, link URL, or 'user:<date>'")
    quote: str | None = Field(default=None, description="Short supporting excerpt, if any")


class Fact(BaseModel):
    id: str
    category: FactCategory
    text: str
    confidence: Confidence = "explicit"
    experience_kind: ExperienceKind = "unknown"
    start: str | None = None  # YYYY-MM
    end: str | None = None  # YYYY-MM or 'present'
    employer: str | None = None
    status: Literal["confirmed", "needs_clarification", "rejected"] = "confirmed"
    evidence: list[EvidenceRef] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)

    @field_validator("text")
    @classmethod
    def _not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("fact text must not be empty")
        return v.strip()


class ProfileTargets(BaseModel):
    direction: list[str] = Field(default_factory=list, description="e.g. ['platform', 'devops', 'sre']")
    level: Literal["entry", "junior", "mid", "senior", "staff", "unknown"] = "unknown"
    stretch_ok: bool = True
    notes: str | None = None


class ProfileDraft(BaseModel):
    """What the rule-based builder or Claude submits for a profile version."""

    summary_text: str
    facts: list[Fact]
    targets: ProfileTargets = Field(default_factory=ProfileTargets)
    questions: list[ProfileQuestion] = Field(default_factory=list)
    professional_years_relevant: float | None = None


class ProfileQuestion(BaseModel):
    key: str
    question: str
    reason: str | None = None


class Preference(BaseModel):
    value: object | None = None
    strength: Literal["must", "nice", "unset"] = "unset"


class Preferences(BaseModel):
    current_location: str | None = None
    work_countries: list[str] = Field(default_factory=list)  # ISO alpha-2 codes
    work_countries_note: str | None = None
    needs_sponsorship: Literal["yes", "no", "not_sure", "unset"] = "unset"
    relocation: Literal["yes", "no", "not_sure", "unset"] = "unset"
    timezone: str | None = None
    salary_min: float | None = None
    salary_currency: str | None = None
    salary_period: Literal["year", "month", "hour"] | None = None
    salary_strength: Literal["must", "nice", "unset"] = "unset"
    remote: Literal["required", "preferred", "no_preference", "unset"] = "unset"
    regions: list[str] = Field(default_factory=lambda: ["na", "eu"])
    # "More options"
    extras: dict[str, Preference] = Field(default_factory=dict)


class RawPosting(BaseModel):
    """What a source adapter returns before normalization."""

    source_id: str
    external_id: str
    title: str
    employer_name: str | None
    url: str  # discovery URL (where we found it)
    apply_url: str | None = None
    location_text: str | None = None
    country_codes: list[str] = Field(default_factory=list)
    remote_type: Literal["remote", "hybrid", "onsite", "unknown"] = "unknown"
    remote_restriction: str | None = None
    employment_type: str | None = None
    description_html: str | None = None
    description_text: str | None = None
    salary_text: str | None = None
    salary_min: float | None = None
    salary_max: float | None = None
    salary_currency: str | None = None
    salary_period: str | None = None
    posted_at: str | None = None
    posted_at_provenance: str | None = None
    source_updated_at: str | None = None
    requisition_id: str | None = None
    employer_confidential: bool = False
    is_employer_hosted: bool = False
    raw: dict = Field(default_factory=dict)


class FetchResult(BaseModel):
    source_id: str
    status: Literal["ok", "partial", "failed", "skipped"]
    postings: list[RawPosting] = Field(default_factory=list)
    error: str | None = None
    http_status: int | None = None
    notes: str | None = None
    complete: bool = True  # False when pagination stopped early or the feed was truncated


class MatchReason(BaseModel):
    text: str
    fact_ids: list[str] = Field(default_factory=list)


class MatchExplanation(BaseModel):
    """Structured output contract for explanation generation (rules or Claude)."""

    job_id: int
    group_hint: Literal["good", "worth_a_shot", "backup", "excluded"] | None = None
    summary: str = Field(..., max_length=400)
    reasons: list[MatchReason] = Field(..., min_length=1, max_length=3)
    gaps: list[str] = Field(default_factory=list, max_length=4)
    next_step: str = Field(..., max_length=200)


class ExplanationBatch(BaseModel):
    explanations: list[MatchExplanation]


class DraftAnswer(BaseModel):
    question: str
    answer: str
    fact_ids: list[str] = Field(default_factory=list)
    needs_user: bool = False
    note: str | None = None


class ApplicationDraft(BaseModel):
    answers: list[DraftAnswer] = Field(default_factory=list)
    intro: str | None = None
    intro_fact_ids: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)


class OutreachDraft(BaseModel):
    subject: str
    body: str
    fact_ids: list[str] = Field(default_factory=list)


ProfileDraft.model_rebuild()
