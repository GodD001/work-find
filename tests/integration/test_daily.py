"""Integration tests for src/job_radar/daily.py — 步骤6a 编排.

Locks the six-step order and its failure-mode contract as given for this
task (CLAUDE.md 铁律2/D-2 extended by the 6a instructions):

    抓取 -> 去重 -> 打分 -> 选发 -> 发信 -> 原子写状态 -> commit

  - 全部 source 失败 -> 不写任何 seen 状态，直接退出（非零退出码）。
  - 单个 source 失败 -> 继续，EmailStats.missing_sources 标明缺失来源。
  - mark_sent 当且仅当 SendOutcome.SENT。
  - SendOutcome.FAILED -> 非零退出码，一条状态都不写。
  - SendOutcome.DRY_RUN -> 退出码 0，不写 data/。
  - 候选池为空（新增=0 且积压=0）-> 跳过 send()，但原子写 + commit 仍然发生
    （reconcile 的 last_seen_at 更新独立于是否发信）。

All network (fetch) and SMTP (send) boundaries are faked; git operations run
for real against a throwaway repo under tmp_path, never the actual job-radar
checkout.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from job_radar.daily import run_daily
from job_radar.deduplicate import load_seen_jobs
from job_radar.emailer import SendOutcome, SmtpConfig
from job_radar.models import Job
from job_radar.normalize import compute_canonical_id, row_hash

SOURCE_REPO = "test-source"


def make_job(cid: str, *, company: str = "Acme", role: str = "SWE Intern") -> Job:
    url = f"https://internal.test/{cid}"
    raw_hash = row_hash([company, role, "Remote", "Jul 1", cid])
    return Job(
        source_repo=SOURCE_REPO,
        company=company,
        role=role,
        location="Remote",
        date_posted_raw="Jul 1",
        application_url=url,
        application_url_raw=url,
        source_job_id=None,
        is_closed=False,
        offers_sponsorship=None,
        requires_us_citizenship=None,
        raw_row_hash=raw_hash,
        canonical_id=compute_canonical_id(
            source_repo=SOURCE_REPO, row_hash_value=raw_hash, application_url=url
        ).value,
        canonical_id_tier="url",
    )


class FakeSource:
    def __init__(self, jobs: list[Job] | None = None, fail: Exception | None = None):
        self._jobs = jobs or []
        self._fail = fail

    def fetch(self) -> str:
        if self._fail is not None:
            raise self._fail
        return "raw"

    def parse(self, raw: str) -> list[Job]:
        return self._jobs

    def validate(self, records: list[Job]) -> list[Job]:
        return records


class FakeSmtpClient:
    def __init__(self, should_succeed: bool, sink: list):
        self.should_succeed = should_succeed
        self.sink = sink

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def send_message(self, msg):
        if not self.should_succeed:
            raise RuntimeError("simulated SMTP failure")
        self.sink.append(msg)


FAKE_SMTP_CONFIG = SmtpConfig(
    host="localhost", port=25, user="bot@example.com", mail_to="me@example.com", app_password="pw"
)


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test Bot"], cwd=repo, check=True)
    (repo / "README.md").write_text("init\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    return repo


@pytest.fixture
def profile_path(tmp_path: Path) -> Path:
    path = tmp_path / "profile.yaml"
    path.write_text(
        'scoring:\n  base_score: 50\n  keywords:\n    - term: "swe"\n      weight: 10\n',
        encoding="utf-8",
    )
    return path


def _commit_count(repo: Path) -> int:
    out = subprocess.run(
        ["git", "rev-list", "--count", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    )
    return int(out.stdout.strip())


def _run(
    git_repo: Path,
    profile_path: Path,
    *,
    sources,
    dry_run: bool = False,
    smtp_should_succeed: bool = True,
    now,
    sent_sink: list | None = None,
    cap: int = 80,
):
    sink = sent_sink if sent_sink is not None else []
    return run_daily(
        now=now,
        dry_run=dry_run,
        sources=sources,
        seen_jobs_path=git_repo / "data" / "seen_jobs.json",
        run_manifest_path=git_repo / "data" / "run_manifest.jsonl",
        history_dir=git_repo / "data" / "history",
        repo_root=git_repo,
        profile_path=profile_path,
        cap=cap,
        smtp_config=FAKE_SMTP_CONFIG,
        smtp_client_factory=lambda cfg: FakeSmtpClient(smtp_should_succeed, sink),
        preview_path=git_repo.parent / "preview.html",
    ), sink


# ---------------------------------------------------------------------------
# Happy path: fresh new jobs, real send succeeds
# ---------------------------------------------------------------------------


def test_fresh_run_sends_writes_state_and_commits(git_repo, profile_path):
    now = datetime(2026, 7, 29, 9, 30, tzinfo=UTC)
    jobs = [make_job(f"job-{i}") for i in range(3)]
    sources = {"s1": FakeSource(jobs=jobs)}

    before_commits = _commit_count(git_repo)
    result, sink = _run(git_repo, profile_path, sources=sources, now=now)

    assert result.exit_code == 0
    assert result.outcome is SendOutcome.SENT
    assert result.new_count == 3
    assert result.sent_count == 3
    assert result.backlog_count == 0
    assert len(sink) == 1  # one email actually "sent"

    store = load_seen_jobs(git_repo / "data" / "seen_jobs.json")
    assert len(store) == 3
    assert all(e.sent_at is not None for e in store.values())
    assert all(e.fit_score is not None for e in store.values())

    assert _commit_count(git_repo) == before_commits + 1
    log = subprocess.run(
        ["git", "log", "-1", "--pretty=%s"],
        cwd=git_repo,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "新增3" in log.stdout
    assert "推送3" in log.stdout


def test_fresh_run_scores_come_from_keyword_profile(git_repo, profile_path):
    now = datetime(2026, 7, 29, tzinfo=UTC)
    jobs = [make_job("job-swe", role="SWE Intern"), make_job("job-pm", role="PM Intern")]
    sources = {"s1": FakeSource(jobs=jobs)}

    _run(git_repo, profile_path, sources=sources, now=now)

    store = load_seen_jobs(git_repo / "data" / "seen_jobs.json")
    # profile_path fixture: base_score=50, "swe" keyword weight=10 (case-insensitive substring)
    assert store[jobs[0].canonical_id].fit_score == 60
    assert store[jobs[1].canonical_id].fit_score == 50


# ---------------------------------------------------------------------------
# All sources fail
# ---------------------------------------------------------------------------


def test_all_sources_fail_writes_nothing_and_exits_nonzero(git_repo, profile_path):
    now = datetime(2026, 7, 29, tzinfo=UTC)
    sources = {"s1": FakeSource(fail=RuntimeError("network down"))}

    before_commits = _commit_count(git_repo)
    result, sink = _run(git_repo, profile_path, sources=sources, now=now)

    assert result.exit_code == 1
    assert result.missing_sources == ["s1"]
    assert sink == []
    assert not (git_repo / "data" / "seen_jobs.json").exists()
    assert not (git_repo / "data" / "run_manifest.jsonl").exists()
    assert _commit_count(git_repo) == before_commits


# ---------------------------------------------------------------------------
# Partial source failure
# ---------------------------------------------------------------------------


def test_single_source_failure_continues_and_notes_missing_source(git_repo, profile_path):
    now = datetime(2026, 7, 29, tzinfo=UTC)
    ok_jobs = [make_job("job-ok")]
    sources = {
        "s1": FakeSource(jobs=ok_jobs),
        "s_broken": FakeSource(fail=RuntimeError("HTTP 500")),
    }

    result, sink = _run(git_repo, profile_path, sources=sources, now=now)

    assert result.exit_code == 0
    assert result.missing_sources == ["s_broken"]
    assert result.new_count == 1
    assert result.sent_count == 1

    store = load_seen_jobs(git_repo / "data" / "seen_jobs.json")
    assert len(store) == 1

    manifest_path = git_repo / "data" / "run_manifest.jsonl"
    lines = [json.loads(line) for line in manifest_path.read_text(encoding="utf-8").splitlines()]
    failure_events = [e for e in lines if e.get("event") == "source_fetch_failed"]
    assert len(failure_events) == 1
    assert failure_events[0]["source_id"] == "s_broken"


# ---------------------------------------------------------------------------
# SendOutcome.FAILED
# ---------------------------------------------------------------------------


def test_send_failure_writes_nothing_and_exits_nonzero(git_repo, profile_path):
    now = datetime(2026, 7, 29, tzinfo=UTC)
    jobs = [make_job("job-1")]
    sources = {"s1": FakeSource(jobs=jobs)}

    before_commits = _commit_count(git_repo)
    result, sink = _run(git_repo, profile_path, sources=sources, now=now, smtp_should_succeed=False)

    assert result.exit_code == 1
    assert result.outcome is SendOutcome.FAILED
    assert sink == []
    assert not (git_repo / "data" / "seen_jobs.json").exists()
    assert _commit_count(git_repo) == before_commits


def test_send_failure_does_not_touch_existing_seen_jobs(git_repo, profile_path):
    """A pre-existing seen_jobs.json (from an earlier successful run) must be
    byte-for-byte untouched when this run's send() fails — CLAUDE.md 铁律2:
    a failed send must never write even a last_seen_at bump. Run 2 adds a
    genuinely new job so the candidate pool is non-empty and send() actually
    gets called (and fails) — otherwise this would vacuously pass by never
    exercising the failure path at all."""
    now1 = datetime(2026, 7, 28, tzinfo=UTC)
    job1 = make_job("job-1")
    _run(git_repo, profile_path, sources={"s1": FakeSource(jobs=[job1])}, now=now1)

    seen_path = git_repo / "data" / "seen_jobs.json"
    before_bytes = seen_path.read_bytes()
    before_commits = _commit_count(git_repo)

    now2 = datetime(2026, 7, 29, tzinfo=UTC)
    job2 = make_job("job-2")
    result, sink = _run(
        git_repo,
        profile_path,
        sources={"s1": FakeSource(jobs=[job1, job2])},
        now=now2,
        smtp_should_succeed=False,
    )

    assert result.new_count == 1
    assert result.exit_code == 1
    assert seen_path.read_bytes() == before_bytes
    assert _commit_count(git_repo) == before_commits


# ---------------------------------------------------------------------------
# DRY_RUN
# ---------------------------------------------------------------------------


def test_dry_run_never_touches_data_dir(git_repo, profile_path):
    now = datetime(2026, 7, 29, tzinfo=UTC)
    jobs = [make_job("job-1")]
    sources = {"s1": FakeSource(jobs=jobs)}

    before_commits = _commit_count(git_repo)
    result, sink = _run(git_repo, profile_path, sources=sources, now=now, dry_run=True)

    assert result.exit_code == 0
    assert result.outcome is SendOutcome.DRY_RUN
    assert sink == []
    assert not (git_repo / "data").exists()
    assert _commit_count(git_repo) == before_commits
    assert (git_repo.parent / "preview.html").exists()


def test_dry_run_with_empty_candidate_pool_still_writes_nothing(git_repo, profile_path):
    """Regression guard: an empty selected_ids list must not bypass dry_run's
    "never write data/" guarantee just because the empty-pool skip-send path
    happens to look like a normal successful (SENT) run."""
    now = datetime(2026, 7, 29, tzinfo=UTC)
    sources = {"s1": FakeSource(jobs=[])}

    result, sink = _run(git_repo, profile_path, sources=sources, now=now, dry_run=True)

    assert result.exit_code == 0
    assert result.outcome is SendOutcome.DRY_RUN
    assert not (git_repo / "data").exists()


# ---------------------------------------------------------------------------
# Empty candidate pool (real run): skip send(), still write state + commit
# ---------------------------------------------------------------------------


def test_empty_pool_skips_send_but_still_commits_last_seen_at_bump(git_repo, profile_path):
    now1 = datetime(2026, 7, 28, tzinfo=UTC)
    jobs = [make_job("job-1")]

    # Run 1: send everything, nothing left pending.
    r1, sink1 = _run(git_repo, profile_path, sources={"s1": FakeSource(jobs=jobs)}, now=now1)
    assert r1.exit_code == 0
    assert r1.sent_count == 1
    assert len(sink1) == 1

    before_commits = _commit_count(git_repo)

    # Run 2: same jobs re-fetched (already known, not new) -> reconcile just
    # bumps last_seen_at; nothing new, nothing pending -> candidate pool empty.
    now2 = datetime(2026, 7, 29, tzinfo=UTC)
    r2, sink2 = _run(git_repo, profile_path, sources={"s1": FakeSource(jobs=jobs)}, now=now2)

    assert r2.exit_code == 0
    assert r2.new_count == 0
    assert r2.sent_count == 0
    assert r2.backlog_count == 0
    assert sink2 == []  # send() never called

    store = load_seen_jobs(git_repo / "data" / "seen_jobs.json")
    assert store[jobs[0].canonical_id].last_seen_at == now2.isoformat()

    # last_seen_at changed -> real diff -> a commit still happens even though
    # nothing was emailed (the two conditions are independent, D-2).
    assert _commit_count(git_repo) == before_commits + 1


# ---------------------------------------------------------------------------
# Overflow across runs (cap) still exercised end-to-end through daily.py
# ---------------------------------------------------------------------------


def test_overflow_backlog_carries_to_next_run(git_repo, profile_path):
    now1 = datetime(2026, 7, 28, tzinfo=UTC)
    jobs = [make_job(f"job-{i}") for i in range(5)]
    r1, sink1 = _run(git_repo, profile_path, sources={"s1": FakeSource(jobs=jobs)}, now=now1, cap=3)

    assert r1.new_count == 5
    assert r1.sent_count == 3
    assert r1.backlog_count == 2
    assert len(sink1[0].get_content_type()) > 0  # sanity: a real EmailMessage was built

    now2 = now1 + timedelta(days=1)
    r2, sink2 = _run(git_repo, profile_path, sources={"s1": FakeSource(jobs=jobs)}, now=now2, cap=3)

    assert r2.new_count == 0
    assert r2.sent_count == 2
    assert r2.backlog_count == 0

    store = load_seen_jobs(git_repo / "data" / "seen_jobs.json")
    assert all(e.sent_at is not None for e in store.values())
