"""Tests for `--force-email` (daily.py run_force_email): a one-off pipeline
check that borrows emailer.py's real send() path to prove SMTP creds /
Secrets injection / SendOutcome actually work — daily.py has never sent a
real email before (the one bootstrap run marked its whole backlog
sent_at="bootstrap", and cron has never yet hit a non-empty candidate pool).

Contract under test (agreed before writing this):
  - Reuses the real emailer.send()/render() — no parallel send code path.
    send() short-circuits to SendOutcome.SENT for an empty entries list
    without ever contacting SMTP, so proving the SMTP path actually runs
    requires passing send() a real (synthetic, clearly-labelled) entry.
  - Never writes seen_jobs.json, never appends run_manifest.jsonl, never
    commits, never calls mark_sent — it only borrows the send path.
  - The real current backlog is read (read-only) and reported in the email
    body/stats, not hardcoded to 0.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from job_radar.daily import run_force_email
from job_radar.deduplicate import SeenJobEntry, load_seen_jobs, save_seen_jobs_atomic
from job_radar.emailer import SendOutcome, SmtpConfig

NOW = datetime(2026, 7, 30, 9, 30, tzinfo=UTC)

FAKE_SMTP_CONFIG = SmtpConfig(
    host="localhost", port=25, user="bot@example.com", mail_to="me@example.com", app_password="pw"
)


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


def _commit_count(repo: Path) -> int:
    out = subprocess.run(
        ["git", "rev-list", "--count", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    )
    return int(out.stdout.strip())


def _seed_seen_jobs(path: Path, *, pending: int, sent: int) -> None:
    store = {}
    for i in range(pending):
        cid = f"pending-{i}"
        store[cid] = SeenJobEntry(
            canonical_id=cid,
            canonical_id_tier="hash_fallback",
            source_repo="seed-repo",
            company="Acme",
            role="SWE Intern",
            location="Remote",
            date_posted_raw="Jul 1",
            fallback_key=None,
            merged_ids=[],
            merged_row_count=1,
            fit_score=50,
            first_seen_at="2026-07-20T00:00:00+00:00",
            last_seen_at="2026-07-20T00:00:00+00:00",
            sent_at=None,
        )
    for i in range(sent):
        cid = f"sent-{i}"
        store[cid] = SeenJobEntry(
            canonical_id=cid,
            canonical_id_tier="hash_fallback",
            source_repo="seed-repo",
            company="Acme",
            role="SWE Intern",
            location="Remote",
            date_posted_raw="Jul 1",
            fallback_key=None,
            merged_ids=[],
            merged_row_count=1,
            fit_score=50,
            first_seen_at="2026-07-20T00:00:00+00:00",
            last_seen_at="2026-07-20T00:00:00+00:00",
            sent_at="2026-07-20T00:00:00+00:00",
        )
    save_seen_jobs_atomic(path, store)


def test_force_email_calls_real_smtp_path_exactly_once(git_repo):
    seen_path = git_repo / "data" / "seen_jobs.json"
    _seed_seen_jobs(seen_path, pending=3, sent=2)
    sink: list = []

    result = run_force_email(
        now=NOW,
        seen_jobs_path=seen_path,
        smtp_config=FAKE_SMTP_CONFIG,
        smtp_client_factory=lambda cfg: FakeSmtpClient(True, sink),
    )

    assert result.exit_code == 0
    assert result.outcome is SendOutcome.SENT
    assert len(sink) == 1  # send_message actually invoked — proves the real SMTP path ran,
    # not the empty-entries no-op short circuit in emailer.send().

    msg = sink[0]
    body = msg.get_body(preferencelist=("plain",)).get_content()
    assert "链路验证" in body
    assert "积压 3" in body  # real backlog (3 pending) surfaces in the stats line, not hardcoded 0


def test_force_email_leaves_state_file_byte_and_mtime_identical(git_repo):
    seen_path = git_repo / "data" / "seen_jobs.json"
    _seed_seen_jobs(seen_path, pending=1, sent=1)
    before_bytes = seen_path.read_bytes()
    before_mtime = seen_path.stat().st_mtime_ns
    sink: list = []

    run_force_email(
        now=NOW,
        seen_jobs_path=seen_path,
        smtp_config=FAKE_SMTP_CONFIG,
        smtp_client_factory=lambda cfg: FakeSmtpClient(True, sink),
    )

    assert seen_path.read_bytes() == before_bytes
    assert seen_path.stat().st_mtime_ns == before_mtime

    # sent_at values in particular must be untouched — this is the one thing
    # 铁律2 says can never happen by accident.
    store = load_seen_jobs(seen_path)
    assert store["pending-0"].sent_at is None
    assert store["sent-0"].sent_at == "2026-07-20T00:00:00+00:00"


def test_force_email_never_creates_a_git_commit(git_repo):
    seen_path = git_repo / "data" / "seen_jobs.json"
    _seed_seen_jobs(seen_path, pending=0, sent=1)
    before_commits = _commit_count(git_repo)
    sink: list = []

    run_force_email(
        now=NOW,
        seen_jobs_path=seen_path,
        smtp_config=FAKE_SMTP_CONFIG,
        smtp_client_factory=lambda cfg: FakeSmtpClient(True, sink),
    )

    assert _commit_count(git_repo) == before_commits


def test_force_email_works_when_seen_jobs_file_does_not_exist_yet(tmp_path):
    seen_path = tmp_path / "data" / "seen_jobs.json"
    sink: list = []

    result = run_force_email(
        now=NOW,
        seen_jobs_path=seen_path,
        smtp_config=FAKE_SMTP_CONFIG,
        smtp_client_factory=lambda cfg: FakeSmtpClient(True, sink),
    )

    assert result.exit_code == 0
    assert result.backlog_count == 0
    assert len(sink) == 1
    assert not seen_path.exists()


def test_force_email_reports_real_backlog_not_hardcoded_zero(tmp_path):
    seen_path = tmp_path / "data" / "seen_jobs.json"
    _seed_seen_jobs(seen_path, pending=7, sent=0)
    sink: list = []

    result = run_force_email(
        now=NOW,
        seen_jobs_path=seen_path,
        smtp_config=FAKE_SMTP_CONFIG,
        smtp_client_factory=lambda cfg: FakeSmtpClient(True, sink),
    )

    assert result.backlog_count == 7
    msg = sink[0]
    body = msg.get_body(preferencelist=("plain",)).get_content()
    assert "积压 7" in body


def test_force_email_dry_run_never_contacts_smtp_and_writes_preview(git_repo, tmp_path):
    seen_path = git_repo / "data" / "seen_jobs.json"
    _seed_seen_jobs(seen_path, pending=1, sent=0)
    before_bytes = seen_path.read_bytes()
    preview_path = tmp_path / "preview.html"
    sink: list = []

    result = run_force_email(
        now=NOW,
        dry_run=True,
        seen_jobs_path=seen_path,
        preview_path=preview_path,
        smtp_client_factory=lambda cfg: FakeSmtpClient(True, sink),
    )

    assert result.exit_code == 0
    assert result.outcome is SendOutcome.DRY_RUN
    assert sink == []  # SMTP never contacted
    assert seen_path.read_bytes() == before_bytes
    assert preview_path.exists()
    assert "链路验证" in preview_path.read_text(encoding="utf-8")


def test_force_email_smtp_failure_returns_nonzero_exit_and_still_no_state_write(git_repo):
    seen_path = git_repo / "data" / "seen_jobs.json"
    _seed_seen_jobs(seen_path, pending=1, sent=1)
    before_bytes = seen_path.read_bytes()
    before_commits = _commit_count(git_repo)
    sink: list = []

    result = run_force_email(
        now=NOW,
        seen_jobs_path=seen_path,
        smtp_config=FAKE_SMTP_CONFIG,
        smtp_client_factory=lambda cfg: FakeSmtpClient(False, sink),
    )

    assert result.exit_code == 1
    assert result.outcome is SendOutcome.FAILED
    assert sink == []
    assert seen_path.read_bytes() == before_bytes
    assert _commit_count(git_repo) == before_commits
