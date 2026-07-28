"""Shared normalization logic used across all fetchers.

Only source-agnostic building blocks live here: URL normalization, the
canonical_id three-tier priority, row hashing, and the fallback_key used by
the (not-yet-implemented) D-3 second-layer dedup merge. No column indices,
no source-specific string constants (e.g. no "↳", no legend emoji) — those
belong in each fetchers/<source>.py module (CLAUDE.md constraint).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from hashlib import sha256
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from job_radar.models import CanonicalIdTier

_TRACKING_EXACT_PARAMS = {"gh_src", "source", "ref"}


def normalize_url(url: str) -> str:
    """Strip known tracking query params (utm_* and a short exact-match list).

    Only these known-safe-to-drop params are removed (CLAUDE.md 常见陷阱2).
    Every other query param is preserved as-is, in its original order, since
    it may be load-bearing for locating the actual job.
    """
    parts = urlsplit(url)
    kept_params = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not key.startswith("utm_") and key not in _TRACKING_EXACT_PARAMS
    ]
    new_query = urlencode(kept_params)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, new_query, parts.fragment))


def row_hash(cells: Sequence[str]) -> str:
    """SHA-256 hex digest of a raw source row's cell values.

    Used as the base of canonical_id tier 3 (hash fallback), and as the
    "identical row" signal for D-3 dedup layer 1.
    """
    joined = "\x1f".join(cells)
    return sha256(joined.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CanonicalId:
    value: str
    tier: CanonicalIdTier


def compute_canonical_id(
    *,
    source_repo: str,
    row_hash_value: str,
    application_url: str | None = None,
    source_job_id: str | None = None,
) -> CanonicalId:
    """CLAUDE.md D-3 canonical_id priority:

    1. normalized application_url (caller must pass an already-normalized URL)
    2. source-given explicit job ID
    3. fallback: source_repo + raw row hash
    """
    if application_url:
        return CanonicalId(sha256(application_url.encode("utf-8")).hexdigest(), "url")
    if source_job_id:
        digest = sha256(f"{source_repo}:{source_job_id}".encode()).hexdigest()
        return CanonicalId(digest, "job_id")
    digest = sha256(f"{source_repo}:{row_hash_value}".encode()).hexdigest()
    return CanonicalId(digest, "hash_fallback")


def fallback_key(company: str, role: str) -> tuple[str, str]:
    """D-3 layer-2 dedup key: normalize(company), normalize(role).

    Only meaningful for records that already used canonical_id tier
    "hash_fallback" — that comparison is the dedup module's job, not this
    function's; this just produces the normalized key.
    """
    return (_normalize_text(company), _normalize_text(role))


def _normalize_text(value: str) -> str:
    return " ".join(value.split()).lower()
