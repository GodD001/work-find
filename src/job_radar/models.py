"""Shared data models for job_radar.

No column indices, no source-specific parsing logic here (see CLAUDE.md:
fetchers/ each own their column layout and don't share it).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

CanonicalIdTier = Literal["url", "job_id", "hash_fallback"]


class Job(BaseModel):
    """One job posting record as parsed from a single source row.

    Every field must trace back to something the source explicitly stated.
    Missing facts are `None` — never inferred (CLAUDE.md 铁律1).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_repo: str
    company: str
    role: str
    location: str
    date_posted_raw: str

    application_url: str | None
    """Normalized application URL (tracking params stripped). None if source gave no link."""

    application_url_raw: str | None
    """Application URL exactly as the source wrote it, before normalization."""

    source_job_id: str | None
    """Explicit job/req ID the source gave outside the URL, if any."""

    is_closed: bool

    offers_sponsorship: bool | None
    """None = not stated. False = source explicitly marked "does not sponsor". Never True:
    no source in this system positively asserts sponsorship, only its absence."""

    requires_us_citizenship: bool | None
    """None = not stated. True = source explicitly marked citizenship required."""

    raw_row_hash: str
    canonical_id: str
    canonical_id_tier: CanonicalIdTier

    @model_validator(mode="after")
    def _check_canonical_id_tier_consistency(self) -> Job:
        if self.canonical_id_tier == "url" and not self.application_url:
            raise ValueError("canonical_id_tier='url' requires application_url to be set")
        if self.canonical_id_tier == "job_id" and not self.source_job_id:
            raise ValueError("canonical_id_tier='job_id' requires source_job_id to be set")
        if self.canonical_id_tier == "hash_fallback" and (
            self.application_url or self.source_job_id
        ):
            raise ValueError(
                "canonical_id_tier='hash_fallback' but application_url or source_job_id "
                "is set — should have used a higher-priority tier"
            )
        if self.offers_sponsorship is True:
            raise ValueError(
                "offers_sponsorship must never be True: sources only ever mark the "
                "absence of sponsorship, not its presence"
            )
        if self.requires_us_citizenship is False:
            raise ValueError(
                "requires_us_citizenship must be None or True: sources only ever mark "
                "the requirement, not its absence"
            )
        return self
