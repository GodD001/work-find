"""Fetcher for S1 — vanshb03/Summer2027-Internships.

Column layout, the "↳" inheritance marker, and the legend emoji are specific
to this source and intentionally not shared with other fetchers or with
normalize.py (CLAUDE.md: adapters don't share column index constants).
"""

from __future__ import annotations

import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from job_radar.models import Job
from job_radar.normalize import compute_canonical_id, normalize_url, row_hash

SOURCE_ID = "s1"


@dataclass(frozen=True)
class SourceConfig:
    repo: str
    branch: str
    path: str


# Mirrors config/sources.yaml's s1 entry. S1's real default branch is `dev`,
# not main/master: raw.githubusercontent.com silently falls back to the
# default branch for an unknown branch name, which is not proof `main`
# exists (CLAUDE.md — verified via the GitHub API, not by curling raw URLs).
DEFAULT_CONFIG = SourceConfig(
    repo="vanshb03/Summer2027-Internships",
    branch="dev",
    path="README.md",
)

_EXPECTED_HEADER = ("Company", "Role", "Location", "Application/Link", "Date Posted")
_SEPARATOR_CELL_RE = re.compile(r"^:?-+:?$")
_HREF_RE = re.compile(r'<a\s+href="([^"]+)"')

_INHERIT_MARKER = "↳"
_NO_SPONSORSHIP_EMOJI = "🛂"
_US_CITIZENSHIP_EMOJI = "🇺🇸"
_CLOSED_EMOJI = "🔒"

# Which columns each legend emoji is allowed to appear in (CLAUDE.md 常见陷阱10):
# 🛂/🇺🇸 only in Role, 🔒 only in Application/Link. Finding one anywhere else
# means the upstream table convention changed — raise, don't guess.
_ROLE_ONLY_EMOJI = (_NO_SPONSORSHIP_EMOJI, _US_CITIZENSHIP_EMOJI)
_LINK_ONLY_EMOJI = (_CLOSED_EMOJI,)


def fetch(
    config: SourceConfig = DEFAULT_CONFIG,
    *,
    timeout: float = 20.0,
    max_retries: int = 3,
) -> str:
    url = f"https://raw.githubusercontent.com/{config.repo}/{config.branch}/{config.path}"
    last_error: Exception | None = None
    for attempt in range(max_retries):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                return response.read().decode("utf-8")
        except urllib.error.URLError as exc:
            last_error = exc
            if attempt < max_retries - 1:
                time.sleep(2**attempt)
    raise RuntimeError(f"failed to fetch {url} after {max_retries} attempts") from last_error


def _split_row(line: str) -> list[str] | None:
    stripped = line.strip()
    if len(stripped) < 2 or not (stripped.startswith("|") and stripped.endswith("|")):
        return None
    inner = stripped[1:-1]
    return [cell.strip() for cell in inner.split("|")]


def _find_header(lines: list[str]) -> int:
    for i, line in enumerate(lines):
        cells = _split_row(line)
        if cells is not None and tuple(cells) == _EXPECTED_HEADER:
            return i
    raise ValueError(
        f"S1 parser: could not find table header matching {_EXPECTED_HEADER!r} — "
        "upstream table structure may have changed"
    )


def _check_separator_row(line: str, header_line_number: int) -> None:
    cells = _split_row(line)
    if cells is None or len(cells) != len(_EXPECTED_HEADER):
        raise ValueError(
            "S1 parser: expected a Markdown table separator row after header "
            f"(line {header_line_number}), got: {line!r}"
        )
    for cell in cells:
        if not _SEPARATOR_CELL_RE.match(cell):
            raise ValueError(
                f"S1 parser: malformed separator cell {cell!r} after header "
                f"(line {header_line_number})"
            )


def _check_legend_columns(cells: list[str], row_number: int) -> None:
    for label, cell in zip(_EXPECTED_HEADER, cells, strict=True):
        if label == "Role":
            forbidden = _LINK_ONLY_EMOJI
        elif label == "Application/Link":
            forbidden = _ROLE_ONLY_EMOJI
        else:
            forbidden = _ROLE_ONLY_EMOJI + _LINK_ONLY_EMOJI
        for emoji in forbidden:
            if emoji in cell:
                raise ValueError(
                    f"S1 parser: legend emoji {emoji!r} found in unexpected column "
                    f"{label!r} at row {row_number} — this is a parsing contract, not "
                    f"something to silently accept (CLAUDE.md 常见陷阱10): {cell!r}"
                )


def parse(raw: str) -> list[Job]:
    lines = raw.splitlines()
    header_index = _find_header(lines)
    _check_separator_row(lines[header_index + 1], header_index + 1)

    jobs: list[Job] = []
    last_explicit_company: str | None = None

    row_index = header_index + 2
    while row_index < len(lines):
        cells = _split_row(lines[row_index])
        if cells is None:
            break
        if len(cells) != len(_EXPECTED_HEADER):
            raise ValueError(
                f"S1 parser: expected {len(_EXPECTED_HEADER)} columns, got "
                f"{len(cells)} at row {row_index + 1}: {lines[row_index]!r}"
            )

        _check_legend_columns(cells, row_index + 1)
        company_cell, role_cell, location_cell, link_cell, date_cell = cells

        if company_cell == _INHERIT_MARKER:
            if last_explicit_company is None:
                raise ValueError(
                    f"S1 parser: '{_INHERIT_MARKER}' at row {row_index + 1} has no "
                    "anchor company to inherit from"
                )
            company = last_explicit_company
        else:
            if not company_cell:
                raise ValueError(f"S1 parser: empty Company cell at row {row_index + 1}")
            company = company_cell
            last_explicit_company = company_cell

        if not role_cell:
            raise ValueError(f"S1 parser: empty Role cell at row {row_index + 1}")
        if not location_cell:
            raise ValueError(f"S1 parser: empty Location cell at row {row_index + 1}")
        if not date_cell:
            raise ValueError(f"S1 parser: empty Date Posted cell at row {row_index + 1}")

        offers_sponsorship = False if _NO_SPONSORSHIP_EMOJI in role_cell else None
        requires_us_citizenship = True if _US_CITIZENSHIP_EMOJI in role_cell else None

        href_match = _HREF_RE.search(link_cell)
        is_closed = _CLOSED_EMOJI in link_cell
        if href_match is None and not is_closed:
            raise ValueError(
                "S1 parser: unrecognized Application/Link cell format at row "
                f"{row_index + 1}: {link_cell!r}"
            )

        application_url_raw = href_match.group(1) if href_match else None
        application_url = normalize_url(application_url_raw) if application_url_raw else None

        raw_hash = row_hash(cells)
        canonical = compute_canonical_id(
            source_repo=DEFAULT_CONFIG.repo,
            row_hash_value=raw_hash,
            application_url=application_url,
            source_job_id=None,
        )

        jobs.append(
            Job(
                source_repo=DEFAULT_CONFIG.repo,
                company=company,
                role=role_cell,
                location=location_cell,
                date_posted_raw=date_cell,
                application_url=application_url,
                application_url_raw=application_url_raw,
                source_job_id=None,
                is_closed=is_closed,
                offers_sponsorship=offers_sponsorship,
                requires_us_citizenship=requires_us_citizenship,
                raw_row_hash=raw_hash,
                canonical_id=canonical.value,
                canonical_id_tier=canonical.tier,
            )
        )
        row_index += 1

    return jobs


def validate(records: list[Job]) -> list[Job]:
    for record in records:
        if record.source_repo != DEFAULT_CONFIG.repo:
            raise ValueError(
                f"S1 validate: record source_repo {record.source_repo!r} does not "
                f"match expected {DEFAULT_CONFIG.repo!r}"
            )
    return records
