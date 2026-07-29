"""Unit tests for src/job_radar/filters.py.

Written before the module exists. Pins down the one responsibility this
task gives filters.py: drop is_closed jobs after fetch/parse, before dedup
(CLAUDE.md 常见陷阱5 附近的新增说明).

  - filter_closed() partitions into kept (open) jobs + a per-source_repo
    count of how many were dropped — never a flat total, since the
    run_manifest event this feeds is one line per source_repo.
  - build_filtered_closed_events() turns that count dict into the exact
    {"event": "filtered_closed", "source_repo": ..., "count": N,
    "timestamp": ...} shape, one line per source_repo, none at all when
    nothing was filtered.
"""

from __future__ import annotations

import json

from job_radar.filters import FilterResult, build_filtered_closed_events, filter_closed
from job_radar.models import Job
from job_radar.normalize import compute_canonical_id, row_hash

SOURCE_A = "vanshb03/Summer2027-Internships"
SOURCE_B = "speedyapply/2027-SWE-College-Jobs"


def make_job(
    *,
    source_repo: str = SOURCE_A,
    company: str = "Acme",
    role: str = "SWE Intern",
    is_closed: bool = False,
    salt: str = "",
) -> Job:
    raw_hash = row_hash([company, role, "Remote", "Jul 1", salt])
    canonical = compute_canonical_id(source_repo=source_repo, row_hash_value=raw_hash)
    return Job(
        source_repo=source_repo,
        company=company,
        role=role,
        location="Remote",
        date_posted_raw="Jul 1",
        application_url=None,
        application_url_raw=None,
        source_job_id=None,
        is_closed=is_closed,
        offers_sponsorship=None,
        requires_us_citizenship=None,
        raw_row_hash=raw_hash,
        canonical_id=canonical.value,
        canonical_id_tier=canonical.tier,
    )


# ---------------------------------------------------------------------------
# filter_closed
# ---------------------------------------------------------------------------


def test_filter_closed_drops_closed_jobs():
    open_job = make_job(company="Open Co", salt="a")
    closed_job = make_job(company="Closed Co", is_closed=True, salt="b")
    result = filter_closed([open_job, closed_job])
    assert result.kept == [open_job]


def test_filter_closed_keeps_all_open_jobs_in_order():
    j1 = make_job(company="A", salt="a")
    j2 = make_job(company="B", salt="b")
    j3 = make_job(company="C", salt="c")
    result = filter_closed([j1, j2, j3])
    assert result.kept == [j1, j2, j3]


def test_filter_closed_empty_list():
    result = filter_closed([])
    assert result.kept == []
    assert result.filtered_counts == {}


def test_filter_closed_all_closed_yields_empty_kept():
    jobs = [make_job(is_closed=True, salt=str(i)) for i in range(3)]
    result = filter_closed(jobs)
    assert result.kept == []
    assert result.filtered_counts == {SOURCE_A: 3}


def test_filter_closed_no_closed_jobs_yields_empty_counts():
    jobs = [make_job(salt=str(i)) for i in range(3)]
    result = filter_closed(jobs)
    assert result.filtered_counts == {}


def test_filter_closed_counts_grouped_by_source_repo_independently():
    jobs = [
        make_job(source_repo=SOURCE_A, is_closed=True, salt="a1"),
        make_job(source_repo=SOURCE_A, is_closed=True, salt="a2"),
        make_job(source_repo=SOURCE_A, salt="a3"),  # open, not counted
        make_job(source_repo=SOURCE_B, is_closed=True, salt="b1"),
    ]
    result = filter_closed(jobs)
    assert result.filtered_counts == {SOURCE_A: 2, SOURCE_B: 1}
    assert len(result.kept) == 1
    assert result.kept[0].source_repo == SOURCE_A
    assert result.kept[0].is_closed is False


def test_filter_result_is_a_dataclass_with_kept_and_filtered_counts():
    result = filter_closed([])
    assert isinstance(result, FilterResult)
    assert hasattr(result, "kept")
    assert hasattr(result, "filtered_counts")


# ---------------------------------------------------------------------------
# build_filtered_closed_events
# ---------------------------------------------------------------------------


def test_build_filtered_closed_events_empty_counts_yields_no_lines():
    assert build_filtered_closed_events({}, now_iso="2026-07-30T01:30:00+00:00") == []


def test_build_filtered_closed_events_one_line_per_source_repo():
    lines = build_filtered_closed_events(
        {SOURCE_A: 5, SOURCE_B: 2}, now_iso="2026-07-30T01:30:00+00:00"
    )
    assert len(lines) == 2
    parsed = [json.loads(line) for line in lines]
    by_source = {p["source_repo"]: p for p in parsed}
    assert by_source[SOURCE_A] == {
        "event": "filtered_closed",
        "source_repo": SOURCE_A,
        "count": 5,
        "timestamp": "2026-07-30T01:30:00+00:00",
    }
    assert by_source[SOURCE_B]["count"] == 2


def test_build_filtered_closed_events_only_summarizes_never_per_record():
    # 只记数量，不逐条记：一个 source_repo 无论过滤了多少条，只产出一行。
    lines = build_filtered_closed_events({SOURCE_A: 42}, now_iso="2026-07-30T01:30:00+00:00")
    assert len(lines) == 1
    assert json.loads(lines[0])["count"] == 42
