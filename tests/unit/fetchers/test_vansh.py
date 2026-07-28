"""Unit tests for the S1 (vanshb03/Summer2027-Internships) fetcher.

Written before src/job_radar/fetchers/vansh.py — these tests define the
contract: fetch() does a timed/retried HTTP GET, parse() turns raw README
markdown into Job records without ever guessing at structure it doesn't
recognize, and validate() is a final sanity gate.
"""

from __future__ import annotations

import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from job_radar.fetchers import vansh
from job_radar.models import Job

# ---------------------------------------------------------------------------
# parse(): full real snapshot
# ---------------------------------------------------------------------------


def test_parse_raw_snapshot_yields_one_job_per_data_row(load_fixture):
    raw = load_fixture("s1/raw_snapshot.md")
    jobs = vansh.parse(raw)
    assert len(jobs) == 144


def test_parse_raw_snapshot_closed_rows_have_no_url(load_fixture):
    raw = load_fixture("s1/raw_snapshot.md")
    jobs = vansh.parse(raw)
    closed = [j for j in jobs if j.is_closed]
    assert len(closed) == 32
    assert all(j.application_url is None for j in closed)
    assert all(j.application_url_raw is None for j in closed)


def test_parse_raw_snapshot_open_rows_have_url_tier_canonical_id(load_fixture):
    raw = load_fixture("s1/raw_snapshot.md")
    jobs = vansh.parse(raw)
    open_jobs = [j for j in jobs if not j.is_closed]
    assert len(open_jobs) == 144 - 32
    assert all(j.application_url is not None for j in open_jobs)
    assert all(j.canonical_id_tier == "url" for j in open_jobs)


def test_parse_raw_snapshot_all_jobs_source_repo_and_source_job_id(load_fixture):
    raw = load_fixture("s1/raw_snapshot.md")
    jobs = vansh.parse(raw)
    assert all(j.source_repo == "vanshb03/Summer2027-Internships" for j in jobs)
    assert all(j.source_job_id is None for j in jobs)


def test_parse_is_idempotent(load_fixture):
    raw = load_fixture("s1/raw_snapshot.md")
    jobs_a = vansh.parse(raw)
    jobs_b = vansh.parse(raw)
    assert [j.canonical_id for j in jobs_a] == [j.canonical_id for j in jobs_b]


# ---------------------------------------------------------------------------
# parse(): specific fields on known real rows from raw_snapshot.md
# ---------------------------------------------------------------------------


def test_intel_row_has_normalized_url_and_no_legend_flags(load_fixture):
    raw = load_fixture("s1/raw_snapshot.md")
    jobs = vansh.parse(raw)
    intel = next(j for j in jobs if j.company == "Intel Corporation")
    assert intel.role == "AI Software Engineering PhD Intern"
    assert intel.location == "Hillsboro, OR"
    assert intel.date_posted_raw == "Jul 27"
    assert intel.offers_sponsorship is None
    assert intel.requires_us_citizenship is None
    assert intel.is_closed is False
    assert intel.application_url_raw == (
        "https://intel.wd1.myworkdayjobs.com/external/job/US-Oregon-Hillsboro/"
        "AI-Applied-intern_JR0285916?utm_source=github-vansh-ouckah"
    )
    # utm_source stripped, nothing else in the query string to keep
    assert intel.application_url == (
        "https://intel.wd1.myworkdayjobs.com/external/job/US-Oregon-Hillsboro/"
        "AI-Applied-intern_JR0285916"
    )
    assert intel.canonical_id_tier == "url"


def test_url_normalization_keeps_non_tracking_query_params(load_fixture):
    raw = load_fixture("s1/raw_snapshot.md")
    jobs = vansh.parse(raw)
    anduril = next(j for j in jobs if j.company == "Anduril")
    assert "gh_jid=5148079007" in anduril.application_url
    assert "utm_source" not in anduril.application_url


def test_hudson_river_trading_gh_src_and_utm_both_stripped(load_fixture):
    raw = load_fixture("s1/raw_snapshot.md")
    jobs = vansh.parse(raw)
    hrt = next(j for j in jobs if j.company == "Hudson River Trading")
    assert hrt.application_url_raw.endswith(
        "internship-summer-2027/?gh_src=&utm_source=github-vansh-ouckah"
    )
    assert "gh_src" not in hrt.application_url
    assert "utm_source" not in hrt.application_url


# ---------------------------------------------------------------------------
# legend_role_col.md: 🛂 / 🇺🇸 parsed per-row, never inherited
# ---------------------------------------------------------------------------


def test_no_sponsorship_emoji_sets_offers_sponsorship_false(load_fixture):
    raw = load_fixture("s1/legend_role_col.md")
    jobs = vansh.parse(raw)
    appian = next(j for j in jobs if j.company == "Appian")
    assert appian.offers_sponsorship is False
    assert appian.requires_us_citizenship is None
    assert "🛂" in appian.role  # raw role text preserved, emoji included verbatim


def test_us_citizenship_emoji_sets_requires_us_citizenship_true(load_fixture):
    raw = load_fixture("s1/legend_role_col.md")
    jobs = vansh.parse(raw)
    five_rings = next(j for j in jobs if j.company == "Five Rings")
    assert five_rings.requires_us_citizenship is True
    assert five_rings.offers_sponsorship is None


def test_legend_flags_are_independent_within_an_inherit_chain(load_fixture):
    raw = load_fixture("s1/legend_role_col.md")
    jobs = vansh.parse(raw)
    akuna_rows = [j for j in jobs if j.company == "Akuna Capital"]
    assert len(akuna_rows) == 3
    anchor, csharp, python_role = akuna_rows
    assert anchor.requires_us_citizenship is True
    assert csharp.requires_us_citizenship is None  # no emoji on this specific row
    assert python_role.requires_us_citizenship is True


def test_closed_row_can_also_carry_no_sponsorship_flag(load_fixture):
    raw = load_fixture("s1/legend_role_col.md")
    jobs = vansh.parse(raw)
    abc = next(j for j in jobs if j.company == "ABC Fitness")
    assert abc.offers_sponsorship is False
    assert abc.is_closed is True
    assert abc.application_url is None


@pytest.mark.parametrize(
    ("company_cell", "role_cell", "location_cell", "link_cell", "bad_label"),
    [
        ("Acme 🛂", "SWE Intern", "NY", '<a href="https://x.test/a">apply</a>', "Company"),
        ("Acme", "SWE Intern", "NY 🇺🇸", '<a href="https://x.test/a">apply</a>', "Location"),
        ("Acme", "SWE Intern 🔒", "NY", '<a href="https://x.test/a">apply</a>', "Role"),
        ("Acme", "SWE Intern", "NY", "🛂", "Application/Link"),
        ("Acme", "SWE Intern", "NY", "🇺🇸", "Application/Link"),
    ],
)
def test_legend_emoji_in_wrong_column_raises(
    company_cell, role_cell, location_cell, link_cell, bad_label
):
    raw = (
        "| Company | Role | Location | Application/Link | Date Posted |\n"
        "| ------- | ---- | -------- | ---------------- | ----------- |\n"
        f"| {company_cell} | {role_cell} | {location_cell} | {link_cell} | Jul 24 |\n"
    )
    with pytest.raises(ValueError, match=bad_label):
        vansh.parse(raw)


def test_closed_emoji_in_date_column_raises():
    raw = (
        "| Company | Role | Location | Application/Link | Date Posted |\n"
        "| ------- | ---- | -------- | ---------------- | ----------- |\n"
        '| Acme | SWE Intern | NY | <a href="https://x.test/a">apply</a> | Jul 24 🔒 |\n'
    )
    with pytest.raises(ValueError, match="Date Posted"):
        vansh.parse(raw)


# ---------------------------------------------------------------------------
# ↳ inheritance
# ---------------------------------------------------------------------------


def test_inherit_chain_all_fourteen_rows_inherit_same_anchor(load_fixture):
    raw = load_fixture("s1/inherit_chain.md")
    jobs = vansh.parse(raw)
    assert len(jobs) == 15
    assert all(j.company == "Jane Street" for j in jobs)


def test_inherit_broken_resets_anchor_on_explicit_company(load_fixture):
    raw = load_fixture("s1/inherit_broken.md")
    jobs = vansh.parse(raw)
    assert len(jobs) == 12

    akuna_rows = [j for j in jobs if j.company == "Akuna Capital"]
    assert len(akuna_rows) == 8

    jtg_rows = [j for j in jobs if j.company == "Jump Trading Group"]
    assert len(jtg_rows) == 1

    jt_rows = [j for j in jobs if j.company == "Jump Trading"]
    assert len(jt_rows) == 3

    # None of Jump Trading's ↳ rows leaked the previous anchor.
    assert not any(j.company in ("Akuna Capital", "Jump Trading Group") for j in jt_rows[1:])


def test_inherit_first_row_with_no_anchor_raises(load_fixture):
    raw = load_fixture("s1/inherit_first_row.md")
    with pytest.raises(ValueError, match="↳"):
        vansh.parse(raw)


# ---------------------------------------------------------------------------
# location: stored raw, untouched
# ---------------------------------------------------------------------------


def test_br_separated_locations_kept_verbatim(load_fixture):
    raw = load_fixture("s1/multi_location_br.md")
    jobs = vansh.parse(raw)
    virtu_financial = next(j for j in jobs if j.company == "Virtu Financial")
    virtu = next(j for j in jobs if j.company == "Virtu")
    deepgram = next(j for j in jobs if j.company == "Deepgram")
    assert virtu_financial.location == "Austin, TX</br>New York"
    assert virtu.location == "Austin, TX</br>Chicago, IL</br>New York, NY"
    assert deepgram.location == "Remote</br>US"


def test_details_summary_location_kept_verbatim(load_fixture):
    raw = load_fixture("s1/multi_location_details.md")
    jobs = vansh.parse(raw)
    assert len(jobs) == 1
    google = jobs[0]
    assert google.location.startswith("<details><summary>**30 locations**</summary>Mountain View, CA")
    assert google.location.endswith("Sunnyvale, CA</details>")
    assert "</br>" in google.location


# ---------------------------------------------------------------------------
# closed rows / no application link
# ---------------------------------------------------------------------------


def test_closed_no_link_fixture_all_closed_with_null_url(load_fixture):
    raw = load_fixture("s1/closed_no_link.md")
    jobs = vansh.parse(raw)
    assert len(jobs) == 6
    assert all(j.is_closed for j in jobs)
    assert all(j.application_url is None for j in jobs)
    assert all(j.application_url_raw is None for j in jobs)
    kudu_rows = [j for j in jobs if j.company == "Kudu Dynamics"]
    assert len(kudu_rows) == 3


# ---------------------------------------------------------------------------
# D-3 tier-3 hash fallback: identical rows get identical canonical_id
# ---------------------------------------------------------------------------


def test_duplicate_identical_rows_produce_three_jobs_not_merged_by_parser(load_fixture):
    # Merging (D-3 two-layer dedup) is deduplicate.py's job, not parse()'s.
    # parse() must faithfully emit one Job per input row.
    raw = load_fixture("s1/duplicate_identical_rows.md")
    jobs = vansh.parse(raw)
    assert len(jobs) == 3
    assert all(j.company == "Kudu Dynamics" for j in jobs)
    assert all(j.is_closed for j in jobs)
    assert all(j.canonical_id_tier == "hash_fallback" for j in jobs)

    anchor, dup_a, dup_b = jobs
    # The two byte-identical ↳ rows hash to the same canonical_id.
    assert dup_a.canonical_id == dup_b.canonical_id
    assert dup_a.raw_row_hash == dup_b.raw_row_hash
    # The anchor's raw row text differs ("Kudu Dynamics" vs "↳"), so its hash
    # (and therefore canonical_id) legitimately differs at the parse layer.
    assert anchor.canonical_id != dup_a.canonical_id


# ---------------------------------------------------------------------------
# structural violations: parser raises rather than guesses (CLAUDE.md 铁律3)
# ---------------------------------------------------------------------------


def test_missing_header_raises():
    raw = "no table here at all\njust some text\n"
    with pytest.raises(ValueError, match="header"):
        vansh.parse(raw)


def test_reordered_header_columns_raises():
    raw = (
        "| Role | Company | Location | Application/Link | Date Posted |\n"
        "| ---- | ------- | -------- | ---------------- | ----------- |\n"
        '| SWE Intern | Acme | NY | <a href="https://x.test/a">apply</a> | Jul 24 |\n'
    )
    with pytest.raises(ValueError, match="header"):
        vansh.parse(raw)


def test_malformed_separator_row_raises():
    raw = (
        "| Company | Role | Location | Application/Link | Date Posted |\n"
        "| this is not a separator row at all |\n"
        '| Acme | SWE Intern | NY | <a href="https://x.test/a">apply</a> | Jul 24 |\n'
    )
    with pytest.raises(ValueError, match="separator"):
        vansh.parse(raw)


def test_separator_row_with_malformed_cell_raises():
    raw = (
        "| Company | Role | Location | Application/Link | Date Posted |\n"
        "| ------- | ---- | not-dashes | ---------------- | ----------- |\n"
        '| Acme | SWE Intern | NY | <a href="https://x.test/a">apply</a> | Jul 24 |\n'
    )
    with pytest.raises(ValueError, match="malformed separator cell"):
        vansh.parse(raw)


def test_wrong_column_count_in_data_row_raises():
    raw = (
        "| Company | Role | Location | Application/Link | Date Posted |\n"
        "| ------- | ---- | -------- | ---------------- | ----------- |\n"
        "| Acme | SWE Intern | NY | Jul 24 |\n"
    )
    with pytest.raises(ValueError, match="columns"):
        vansh.parse(raw)


def test_unrecognized_link_cell_format_raises():
    raw = (
        "| Company | Role | Location | Application/Link | Date Posted |\n"
        "| ------- | ---- | -------- | ---------------- | ----------- |\n"
        "| Acme | SWE Intern | NY | N/A | Jul 24 |\n"
    )
    with pytest.raises(ValueError, match="Application/Link"):
        vansh.parse(raw)


@pytest.mark.parametrize(
    ("row", "field_name"),
    [
        ('|  | SWE Intern | NY | <a href="https://x.test/a">apply</a> | Jul 24 |', "Company"),
        ('| Acme |  | NY | <a href="https://x.test/a">apply</a> | Jul 24 |', "Role"),
        ('| Acme | SWE Intern |  | <a href="https://x.test/a">apply</a> | Jul 24 |', "Location"),
        ('| Acme | SWE Intern | NY | <a href="https://x.test/a">apply</a> |  |', "Date Posted"),
    ],
)
def test_empty_required_cell_raises(row, field_name):
    raw = (
        "| Company | Role | Location | Application/Link | Date Posted |\n"
        "| ------- | ---- | -------- | ---------------- | ----------- |\n"
        f"{row}\n"
    )
    with pytest.raises(ValueError, match=field_name):
        vansh.parse(raw)


def test_table_with_no_data_rows_returns_empty_list():
    raw = (
        "| Company | Role | Location | Application/Link | Date Posted |\n"
        "| ------- | ---- | -------- | ---------------- | ----------- |\n"
    )
    assert vansh.parse(raw) == []


# ---------------------------------------------------------------------------
# validate()
# ---------------------------------------------------------------------------


def test_validate_passes_through_well_formed_records(load_fixture):
    raw = load_fixture("s1/closed_no_link.md")
    jobs = vansh.parse(raw)
    assert vansh.validate(jobs) == jobs


def test_validate_raises_on_source_repo_mismatch():
    bad_job = Job(
        source_repo="someone-else/other-repo",
        company="Acme",
        role="SWE Intern",
        location="NY",
        date_posted_raw="Jul 24",
        application_url="https://x.test/a",
        application_url_raw="https://x.test/a",
        source_job_id=None,
        is_closed=False,
        offers_sponsorship=None,
        requires_us_citizenship=None,
        raw_row_hash="deadbeef",
        canonical_id="deadbeef",
        canonical_id_tier="url",
    )
    with pytest.raises(ValueError, match="source_repo"):
        vansh.validate([bad_job])


# ---------------------------------------------------------------------------
# fetch(): HTTP retry/timeout behavior (network itself is mocked)
# ---------------------------------------------------------------------------


def test_fetch_returns_decoded_body_on_first_success():
    mock_response = MagicMock()
    mock_response.read.return_value = "raw markdown content".encode("utf-8")
    mock_response.__enter__.return_value = mock_response

    with patch("job_radar.fetchers.vansh.urllib.request.urlopen", return_value=mock_response):
        result = vansh.fetch()

    assert result == "raw markdown content"


def test_fetch_retries_after_transient_error_then_succeeds():
    mock_response = MagicMock()
    mock_response.read.return_value = "ok".encode("utf-8")
    mock_response.__enter__.return_value = mock_response

    call_count = {"n": 0}

    def side_effect(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] < 3:
            raise urllib.error.URLError("temporary failure")
        return mock_response

    with (
        patch("job_radar.fetchers.vansh.urllib.request.urlopen", side_effect=side_effect),
        patch("job_radar.fetchers.vansh.time.sleep") as mock_sleep,
    ):
        result = vansh.fetch()

    assert result == "ok"
    assert call_count["n"] == 3
    assert mock_sleep.call_count == 2


def test_fetch_raises_after_exhausting_retries():
    with (
        patch(
            "job_radar.fetchers.vansh.urllib.request.urlopen",
            side_effect=urllib.error.URLError("down"),
        ),
        patch("job_radar.fetchers.vansh.time.sleep"),
    ):
        with pytest.raises(RuntimeError, match="failed to fetch"):
            vansh.fetch(max_retries=3)
