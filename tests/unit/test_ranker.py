"""Unit tests for src/job_radar/ranker.py — FR-011 关键词降级路径.

Pins down:
  - Profile 的 keywords/weights 全部来自传入的 YAML 文件，不硬编码。
  - score_job: base_score + 命中关键词权重之和，夹到 [min_score, max_score]。
  - 大小写不敏感的子串匹配，匹配范围是 role + company。
  - score_new_jobs 只给传入的这批 Job 打分，返回 canonical_id -> fit_score。
"""

from __future__ import annotations

from pathlib import Path

from job_radar.models import Job
from job_radar.normalize import compute_canonical_id, row_hash
from job_radar.ranker import Keyword, Profile, load_profile, score_job, score_new_jobs

SOURCE_REPO = "vanshb03/Summer2027-Internships"


def make_job(*, company: str = "Acme", role: str = "SWE Intern", salt: str = "") -> Job:
    raw_hash = row_hash([company, role, "Remote", "Jul 1", salt])
    canonical = compute_canonical_id(source_repo=SOURCE_REPO, row_hash_value=raw_hash)
    return Job(
        source_repo=SOURCE_REPO,
        company=company,
        role=role,
        location="Remote",
        date_posted_raw="Jul 1",
        application_url=None,
        application_url_raw=None,
        source_job_id=None,
        is_closed=False,
        offers_sponsorship=None,
        requires_us_citizenship=None,
        raw_row_hash=raw_hash,
        canonical_id=canonical.value,
        canonical_id_tier=canonical.tier,
    )


def make_profile(
    *, base_score: int = 50, min_score: int = 0, max_score: int = 100, keywords=None
) -> Profile:
    return Profile(
        base_score=base_score,
        min_score=min_score,
        max_score=max_score,
        keywords=keywords or [],
    )


# ---------------------------------------------------------------------------
# load_profile: reads config, doesn't hardcode
# ---------------------------------------------------------------------------


def test_load_profile_reads_keywords_and_weights_from_yaml(tmp_path: Path):
    path = tmp_path / "profile.yaml"
    path.write_text(
        """
scoring:
  base_score: 40
  min_score: 10
  max_score: 90
  keywords:
    - term: "machine learning"
      weight: 25
    - term: "new grad"
      weight: -20
""",
        encoding="utf-8",
    )
    profile = load_profile(path)
    assert profile.base_score == 40
    assert profile.min_score == 10
    assert profile.max_score == 90
    assert profile.keywords == [
        Keyword(term="machine learning", weight=25),
        Keyword(term="new grad", weight=-20),
    ]


def test_load_profile_defaults_min_max_when_absent(tmp_path: Path):
    path = tmp_path / "profile.yaml"
    path.write_text("scoring:\n  base_score: 50\n", encoding="utf-8")
    profile = load_profile(path)
    assert profile.min_score == 0
    assert profile.max_score == 100
    assert profile.keywords == []


# ---------------------------------------------------------------------------
# score_job
# ---------------------------------------------------------------------------


def test_score_job_returns_base_score_when_no_keyword_matches():
    job = make_job(role="Backend Engineer Intern", company="Acme")
    profile = make_profile(base_score=50, keywords=[Keyword(term="machine learning", weight=30)])
    assert score_job(job, profile) == 50


def test_score_job_adds_weight_for_matched_keyword_in_role():
    job = make_job(role="Machine Learning Intern", company="Acme")
    profile = make_profile(base_score=50, keywords=[Keyword(term="machine learning", weight=30)])
    assert score_job(job, profile) == 80


def test_score_job_matches_company_field_too():
    job = make_job(role="SWE Intern", company="MachineLearningCo")
    profile = make_profile(base_score=50, keywords=[Keyword(term="machinelearning", weight=10)])
    assert score_job(job, profile) == 60


def test_score_job_is_case_insensitive():
    job = make_job(role="MACHINE LEARNING INTERN")
    profile = make_profile(base_score=50, keywords=[Keyword(term="machine learning", weight=10)])
    assert score_job(job, profile) == 60


def test_score_job_sums_multiple_matched_keywords():
    job = make_job(role="Machine Learning Backend Intern, New Grad")
    profile = make_profile(
        base_score=50,
        keywords=[
            Keyword(term="machine learning", weight=20),
            Keyword(term="backend", weight=15),
            Keyword(term="new grad", weight=-15),
        ],
    )
    assert score_job(job, profile) == 70


def test_score_job_clips_to_max_score():
    job = make_job(role="Machine Learning Backend Intern")
    profile = make_profile(
        base_score=90,
        max_score=100,
        keywords=[Keyword(term="machine learning", weight=20), Keyword(term="backend", weight=20)],
    )
    assert score_job(job, profile) == 100


def test_score_job_clips_to_min_score():
    job = make_job(role="New Grad Intern")
    profile = make_profile(
        base_score=10, min_score=0, keywords=[Keyword(term="new grad", weight=-50)]
    )
    assert score_job(job, profile) == 0


def test_score_job_does_not_match_location_or_date():
    job = make_job(role="SWE Intern", company="Acme")
    # location/date_posted_raw are fixed to "Remote"/"Jul 1" by make_job; a
    # keyword targeting those must never match, since ranker only reads
    # role+company (CLAUDE.md 常见陷阱9: those fields stay untouched raw text).
    profile = make_profile(base_score=50, keywords=[Keyword(term="remote", weight=30)])
    assert score_job(job, profile) == 50


# ---------------------------------------------------------------------------
# score_new_jobs
# ---------------------------------------------------------------------------


def test_score_new_jobs_returns_dict_keyed_by_canonical_id():
    job1 = make_job(role="Machine Learning Intern", salt="a")
    job2 = make_job(role="Backend Intern", salt="b")
    profile = make_profile(base_score=50, keywords=[Keyword(term="machine learning", weight=30)])
    scores = score_new_jobs([job1, job2], profile)
    assert scores == {job1.canonical_id: 80, job2.canonical_id: 50}


def test_score_new_jobs_empty_list_returns_empty_dict():
    profile = make_profile()
    assert score_new_jobs([], profile) == {}
