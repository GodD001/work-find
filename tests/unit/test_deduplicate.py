"""Unit tests for src/job_radar/deduplicate.py.

Written before the module exists — these tests pin down the four "must land"
design decisions from CLAUDE.md D-2/D-3:

1. canonical_id 三级优先级已经在 normalize.py 落地 (compute_canonical_id);
   here we only test what deduplicate.py adds on top of it.
2. D-3 second-layer merge: three constraints (tier-3-only, location/date guard,
   no re-scan of the full set) — see test_layer2_* below.
3. sent_at three states + two independent predicates (dedup key existence vs
   sent_at is None) — see test_select_for_send_* and test_reconcile_*.
4. Three-tier sort key (first_seen_at ASC -> fit_score DESC -> canonical_id ASC).

Plus the loser_id -> winner_id traceability mechanism this task asked us to
design ourselves (SeenJobEntry.merged_ids), tested under
test_reconcile_cross_run_alias_*.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from job_radar.deduplicate import (
    BOOTSTRAP_SENT_AT,
    SeenJobEntry,
    apply_fit_scores,
    bootstrap,
    load_seen_jobs,
    mark_sent,
    merge_duplicates,
    pending_count,
    reconcile,
    save_seen_jobs_atomic,
    select_for_send,
)
from job_radar.fetchers import vansh
from job_radar.models import Job
from job_radar.normalize import compute_canonical_id, fallback_key, row_hash

T1 = "2026-07-28T01:30:00+00:00"
T2 = "2026-07-29T01:30:00+00:00"
T3 = "2026-07-30T01:30:00+00:00"

SOURCE_REPO = "vanshb03/Summer2027-Internships"


def make_job(
    *,
    source_repo: str = SOURCE_REPO,
    company: str = "Acme",
    role: str = "SWE Intern",
    location: str = "Remote",
    date_posted_raw: str = "Jul 1",
    application_url: str | None = None,
    application_url_raw: str | None = None,
    source_job_id: str | None = None,
    is_closed: bool = False,
    row_hash_salt: str = "",
) -> Job:
    """Build a Job with a canonical_id derived the same way normalize.py does.

    row_hash_salt lets a test force two records with identical
    company/role/location/date_posted_raw (same fallback_key) to nonetheless
    get different raw row hashes / canonical_ids — the exact situation D-3
    layer 2 exists for (upstream row text differs, semantic fields don't).
    """
    raw_row_hash = row_hash([company, role, location, date_posted_raw, row_hash_salt])
    canonical = compute_canonical_id(
        source_repo=source_repo,
        row_hash_value=raw_row_hash,
        application_url=application_url,
        source_job_id=source_job_id,
    )
    return Job(
        source_repo=source_repo,
        company=company,
        role=role,
        location=location,
        date_posted_raw=date_posted_raw,
        application_url=application_url,
        application_url_raw=application_url_raw,
        source_job_id=source_job_id,
        is_closed=is_closed,
        offers_sponsorship=None,
        requires_us_citizenship=None,
        raw_row_hash=raw_row_hash,
        canonical_id=canonical.value,
        canonical_id_tier=canonical.tier,
    )


def make_seen_entry(
    job: Job,
    *,
    fit_score: int | None,
    first_seen_at: str,
    sent_at: str | None,
    last_seen_at: str | None = None,
    merged_ids: list[str] | None = None,
    merged_row_count: int = 1,
) -> SeenJobEntry:
    return SeenJobEntry(
        canonical_id=job.canonical_id,
        canonical_id_tier=job.canonical_id_tier,
        source_repo=job.source_repo,
        company=job.company,
        role=job.role,
        location=job.location,
        date_posted_raw=job.date_posted_raw,
        fallback_key=fallback_key(job.company, job.role)
        if job.canonical_id_tier == "hash_fallback"
        else None,
        merged_ids=merged_ids or [],
        merged_row_count=merged_row_count,
        fit_score=fit_score,
        first_seen_at=first_seen_at,
        last_seen_at=last_seen_at or first_seen_at,
        sent_at=sent_at,
    )


class FakeEmailer:
    def __init__(self, should_succeed: bool = True):
        self.should_succeed = should_succeed
        self.calls: list[list[SeenJobEntry]] = []

    def send(self, batch: list[SeenJobEntry]) -> bool:
        self.calls.append(list(batch))
        return self.should_succeed


# ---------------------------------------------------------------------------
# merge_duplicates: layer 1 (identical raw row hash)
# ---------------------------------------------------------------------------


def test_layer1_collapses_identical_hash_fallback_rows_and_logs_event():
    a = make_job(company="Kudu Dynamics", role="SWE Intern", is_closed=True)
    b = make_job(company="Kudu Dynamics", role="SWE Intern", is_closed=True)
    assert a.canonical_id == b.canonical_id  # identical inputs -> identical hash

    result = merge_duplicates([a, b], now=T1)

    assert [w.canonical_id for w in result.winners] == [a.canonical_id]
    assert result.merge_counts[a.canonical_id] == 2
    assert len(result.events) == 1
    event = result.events[0]
    assert event.reason == "row_hash_duplicate"
    assert event.winner_id == event.loser_id == a.canonical_id
    assert event.merged_count == 2
    assert event.matched_fields == ("raw_row_hash",)


def test_layer1_does_not_touch_distinct_url_tier_rows():
    a = make_job(application_url="https://a.example/job", application_url_raw="https://a.example/job")
    b = make_job(application_url="https://b.example/job", application_url_raw="https://b.example/job")

    result = merge_duplicates([a, b], now=T1)

    assert {w.canonical_id for w in result.winners} == {a.canonical_id, b.canonical_id}
    assert result.events == []


# ---------------------------------------------------------------------------
# merge_duplicates: layer 2 (fallback_key + location + date guard)
# ---------------------------------------------------------------------------


def test_layer2_merges_hash_fallback_rows_with_matching_fallback_key_and_location_date():
    anchor = make_job(
        company="Kudu Dynamics",
        role="Software Engineer Intern",
        location="Chantilly, VA",
        date_posted_raw="May 22",
        is_closed=True,
        row_hash_salt="anchor",
    )
    inherited = make_job(
        company="Kudu Dynamics",
        role="Software Engineer Intern",
        location="Chantilly, VA",
        date_posted_raw="May 22",
        is_closed=True,
        row_hash_salt="inherited",
    )
    assert anchor.canonical_id != inherited.canonical_id  # different salt -> different hash

    result = merge_duplicates([anchor, inherited], now=T1)

    assert len(result.winners) == 1
    winner_id = min(anchor.canonical_id, inherited.canonical_id)
    assert result.winners[0].canonical_id == winner_id
    assert result.merge_counts[winner_id] == 2
    assert len(result.events) == 1
    assert result.events[0].reason == "fallback_key"
    assert result.events[0].winner_id == winner_id
    assert result.events[0].matched_fields == ("company", "role", "location", "date_posted_raw")


def test_layer2_winner_is_lexicographically_smallest_regardless_of_row_order():
    anchor = make_job(
        company="Kudu Dynamics", role="SWE Intern", location="VA", date_posted_raw="May 22",
        row_hash_salt="anchor",
    )
    inherited = make_job(
        company="Kudu Dynamics", role="SWE Intern", location="VA", date_posted_raw="May 22",
        row_hash_salt="inherited",
    )
    expected_winner = min(anchor.canonical_id, inherited.canonical_id)

    forward = merge_duplicates([anchor, inherited], now=T1)
    backward = merge_duplicates([inherited, anchor], now=T1)

    assert forward.winners[0].canonical_id == expected_winner
    assert backward.winners[0].canonical_id == expected_winner


def test_layer2_does_not_merge_when_location_differs():
    a = make_job(
        company="Acme", role="SWE Intern", location="Remote", date_posted_raw="Jul 1",
        row_hash_salt="a",
    )
    b = make_job(
        company="Acme", role="SWE Intern", location="NYC", date_posted_raw="Jul 1",
        row_hash_salt="b",
    )

    result = merge_duplicates([a, b], now=T1)

    assert {w.canonical_id for w in result.winners} == {a.canonical_id, b.canonical_id}
    assert result.events == []


def test_layer2_does_not_merge_when_date_posted_raw_differs():
    a = make_job(
        company="Acme", role="SWE Intern", location="Remote", date_posted_raw="Jul 1",
        row_hash_salt="a",
    )
    b = make_job(
        company="Acme", role="SWE Intern", location="Remote", date_posted_raw="Jul 2",
        row_hash_salt="b",
    )

    result = merge_duplicates([a, b], now=T1)

    assert {w.canonical_id for w in result.winners} == {a.canonical_id, b.canonical_id}
    assert result.events == []


def test_layer2_does_not_apply_to_url_or_job_id_tier_records():
    """CLAUDE.md D-3 约束 a：第二层只在两条都属于第 3 类时生效。"""
    url_job = make_job(
        company="Acme", role="SWE Intern", location="Remote", date_posted_raw="Jul 1",
        application_url="https://a.example/job", application_url_raw="https://a.example/job",
    )
    job_id_job = make_job(
        company="Acme", role="SWE Intern", location="Remote", date_posted_raw="Jul 1",
        source_job_id="REQ-123",
    )
    hash_job = make_job(
        company="Acme", role="SWE Intern", location="Remote", date_posted_raw="Jul 1",
        row_hash_salt="hash-tier",
    )

    result = merge_duplicates([url_job, job_id_job, hash_job], now=T1)

    assert {w.canonical_id for w in result.winners} == {
        url_job.canonical_id,
        job_id_job.canonical_id,
        hash_job.canonical_id,
    }
    assert result.events == []


def test_layer2_is_single_pass_not_transitive_rescan():
    """约束 c：第二层只作用于第一层产出，不回头重新分组。

    Three hash-fallback rows: (x, y) share fallback_key/location/date and get
    merged. A third row z shares none of that with x or y — it must stay
    separate, and merging x+y must not somehow pull z in.
    """
    x = make_job(
        company="Acme", role="SWE Intern", location="Remote", date_posted_raw="Jul 1",
        row_hash_salt="x",
    )
    y = make_job(
        company="Acme", role="SWE Intern", location="Remote", date_posted_raw="Jul 1",
        row_hash_salt="y",
    )
    z = make_job(
        company="Other Co", role="Data Intern", location="NYC", date_posted_raw="Jul 5",
        row_hash_salt="z",
    )

    result = merge_duplicates([x, y, z], now=T1)

    winner_ids = {w.canonical_id for w in result.winners}
    assert z.canonical_id in winner_ids
    assert len(result.winners) == 2


# ---------------------------------------------------------------------------
# duplicate_identical_rows.md fixture: the real 3-row -> 1 collapse
# ---------------------------------------------------------------------------


def test_duplicate_identical_rows_fixture_collapses_to_one_with_merged_count_3(load_fixture):
    raw = load_fixture("s1/duplicate_identical_rows.md")
    jobs = vansh.parse(raw)
    assert len(jobs) == 3

    result = merge_duplicates(jobs, now=T1)

    assert len(result.winners) == 1
    winner = result.winners[0]
    assert winner.company == "Kudu Dynamics"
    expected_winner_id = min(j.canonical_id for j in jobs)
    assert winner.canonical_id == expected_winner_id
    assert result.merge_counts[winner.canonical_id] == 3

    reasons = [e.reason for e in result.events]
    assert reasons == ["row_hash_duplicate", "fallback_key"]
    assert result.events[-1].merged_count == 3
    assert result.events[-1].winner_id == expected_winner_id


# ---------------------------------------------------------------------------
# reconcile: dedup predicate (canonical_id / alias / fallback_key existence)
# ---------------------------------------------------------------------------


def test_reconcile_adds_genuinely_new_record_with_sent_at_none_and_fit_score_none():
    """reconcile cannot know fit_score up front — whether a record is new is
    only known after it runs, so scoring is a separate step (apply_fit_scores)
    the caller drives using result.new_ids, keeping AI ranking scoped to only
    genuinely new records (CLAUDE.md 分批 budget)."""
    job = make_job(company="Acme", role="SWE Intern")
    result = reconcile({}, [job], now=T1)

    assert result.new_ids == [job.canonical_id]
    entry = result.store[job.canonical_id]
    assert entry.sent_at is None
    assert entry.fit_score is None
    assert entry.first_seen_at == T1
    assert entry.last_seen_at == T1


def test_apply_fit_scores_writes_scores_for_new_ids_only():
    job = make_job(company="Acme", role="SWE Intern")
    result = reconcile({}, [job], now=T1)

    scored = apply_fit_scores(result.store, {job.canonical_id: 42})

    assert scored[job.canonical_id].fit_score == 42
    # original store (and result.store, since apply_fit_scores doesn't mutate) untouched
    assert result.store[job.canonical_id].fit_score is None


def test_reconcile_known_canonical_id_is_not_new_and_first_seen_at_is_immutable():
    job = make_job(company="Acme", role="SWE Intern")
    existing = make_seen_entry(job, fit_score=10, first_seen_at=T1, sent_at=None)
    store = {job.canonical_id: existing}

    result = reconcile(store, [job], now=T2)

    assert result.new_ids == []
    entry = result.store[job.canonical_id]
    assert entry.first_seen_at == T1  # unchanged
    assert entry.last_seen_at == T2  # updated
    assert entry.fit_score == 10  # frozen, not overwritten
    assert entry.sent_at is None  # untouched by reconcile


def test_reconcile_cross_run_fallback_key_hit_reuses_old_id_and_logs_event():
    """CLAUDE.md D-3 跨 run 稳定性：canonical_id 查不到时查 fallback_key 索引。"""
    old_job = make_job(
        company="Kudu Dynamics", role="SWE Intern", location="VA", date_posted_raw="May 22",
        row_hash_salt="old-text",
    )
    existing = make_seen_entry(old_job, fit_score=50, first_seen_at=T1, sent_at=T1)
    store = {old_job.canonical_id: existing}

    # Same job, upstream tweaked something else in the row (emoji, whitespace...)
    # -> different row hash -> different canonical_id, but same company/role/
    # location/date_posted_raw.
    new_job = make_job(
        company="Kudu Dynamics", role="SWE Intern", location="VA", date_posted_raw="May 22",
        row_hash_salt="new-text",
    )
    assert new_job.canonical_id != old_job.canonical_id

    result = reconcile(store, [new_job], now=T2)

    assert result.new_ids == []  # not treated as new -> not a resend candidate
    assert old_job.canonical_id in result.store
    assert new_job.canonical_id not in result.store  # no second entry created
    updated = result.store[old_job.canonical_id]
    assert new_job.canonical_id in updated.merged_ids
    assert updated.merged_row_count == 2
    assert updated.sent_at == T1  # untouched — still counted as already-sent

    assert len(result.events) == 1
    assert result.events[0].reason == "fallback_key_cross_run"
    assert result.events[0].winner_id == old_job.canonical_id
    assert result.events[0].loser_id == new_job.canonical_id


def test_reconcile_alias_reappearance_does_not_double_count_or_re_log():
    """The loser_id -> winner_id traceability mechanism this task asked us to design:
    once a loser canonical_id has been folded into a winner, it must be
    recognized directly (O(1) alias lookup) on every future reappearance —
    without incrementing merged_row_count or emitting another audit event
    each time, since it's the *same* historical row being observed again,
    not a newly-discovered duplicate.
    """
    winner_job = make_job(
        company="Acme", role="SWE Intern", location="Remote", date_posted_raw="Jul 1",
        row_hash_salt="winner",
    )
    loser_job = make_job(
        company="Acme", role="SWE Intern", location="Remote", date_posted_raw="Jul 1",
        row_hash_salt="loser",
    )
    store = {
        winner_job.canonical_id: make_seen_entry(
            winner_job, fit_score=50, first_seen_at=T1, sent_at=T1
        )
    }

    # Run N+1: loser row appears for the first time since the winner was recorded.
    r1 = reconcile(store, [loser_job], now=T2)
    assert len(r1.events) == 1
    assert r1.store[winner_job.canonical_id].merged_row_count == 2

    # Run N+2: the exact same loser row reappears unchanged.
    r2 = reconcile(r1.store, [loser_job], now=T3)
    assert r2.events == []  # no new merge — this is a known alias, not a new duplicate
    assert r2.store[winner_job.canonical_id].merged_row_count == 2  # unchanged
    assert r2.store[winner_job.canonical_id].last_seen_at == T3
    assert r2.new_ids == []


def test_reconcile_fallback_key_does_not_cross_source_repos():
    a = make_job(
        source_repo="vanshb03/Summer2027-Internships",
        company="Acme", role="SWE Intern", location="Remote", date_posted_raw="Jul 1",
        row_hash_salt="a",
    )
    b = make_job(
        source_repo="speedyapply/2027-SWE-College-Jobs",
        company="Acme", role="SWE Intern", location="Remote", date_posted_raw="Jul 1",
        row_hash_salt="b",
    )
    store = {a.canonical_id: make_seen_entry(a, fit_score=10, first_seen_at=T1, sent_at=T1)}

    result = reconcile(store, [b], now=T2)

    assert result.new_ids == [b.canonical_id]
    assert b.canonical_id in result.store
    assert a.canonical_id in result.store
    assert result.events == []


def test_reconcile_cross_run_hit_counts_full_batch_local_row_count_not_flat_one():
    """Regression: a same-run merge_duplicates winner that itself already
    absorbed a same-batch loser must contribute its *entire* batch-local row
    count when it turns out to also match a historical winner via
    fallback_key — not a flat +1. Getting this wrong silently undercounts
    merged_row_count and makes the audit trail in run_manifest.jsonl lie
    about how many raw rows actually fed into a canonical_id.
    """
    historical = make_job(
        company="Kudu Dynamics", role="SWE Intern", location="VA", date_posted_raw="May 22",
        row_hash_salt="historical-text",
    )
    store = {
        historical.canonical_id: make_seen_entry(
            historical, fit_score=50, first_seen_at=T1, sent_at=T1, merged_row_count=1
        )
    }

    # This run's fetch independently contains two *different* raw-text rows
    # that share fallback_key/location/date with each other (and with
    # `historical`) but not with `historical`'s own exact text — merge_duplicates
    # collapses them into one batch-local winner representing 2 rows.
    same_run_a = make_job(
        company="Kudu Dynamics", role="SWE Intern", location="VA", date_posted_raw="May 22",
        row_hash_salt="today-a",
    )
    same_run_b = make_job(
        company="Kudu Dynamics", role="SWE Intern", location="VA", date_posted_raw="May 22",
        row_hash_salt="today-b",
    )
    merge_result = merge_duplicates([same_run_a, same_run_b], now=T2)
    assert len(merge_result.winners) == 1
    batch_winner = merge_result.winners[0]
    assert merge_result.merge_counts[batch_winner.canonical_id] == 2
    assert batch_winner.canonical_id != historical.canonical_id

    result = reconcile(
        store,
        merge_result.winners,
        now=T2,
        merge_counts=merge_result.merge_counts,
        merged_ids=merge_result.merged_ids,
    )

    updated = result.store[historical.canonical_id]
    # 1 (historical baseline) + 2 (this batch's two rows) = 3, not 1 + 1.
    assert updated.merged_row_count == 3
    # Both today's rows must be traceable as aliases of the historical winner.
    assert same_run_a.canonical_id in updated.merged_ids
    assert same_run_b.canonical_id in updated.merged_ids
    assert result.new_ids == []
    assert len(result.events) == 1
    assert result.events[0].merged_count == 3


# ---------------------------------------------------------------------------
# bootstrap
# ---------------------------------------------------------------------------


def test_bootstrap_writes_all_records_with_sent_at_bootstrap_and_no_fit_score():
    jobs = [
        make_job(company="Acme", role="SWE Intern"),
        make_job(company="Globex", role="Data Intern", row_hash_salt="g"),
    ]

    store, events = bootstrap(jobs, now=T1)

    assert len(store) == 2
    for entry in store.values():
        assert entry.sent_at == BOOTSTRAP_SENT_AT
        assert entry.fit_score is None
        assert entry.first_seen_at == T1
        assert entry.last_seen_at == T1


def test_bootstrap_applies_dedup_merging_before_writing():
    raw_dupe_a = make_job(company="Acme", role="SWE Intern")
    raw_dupe_b = make_job(company="Acme", role="SWE Intern")  # identical -> same hash
    assert raw_dupe_a.canonical_id == raw_dupe_b.canonical_id

    store, _events = bootstrap([raw_dupe_a, raw_dupe_b], now=T1)

    assert len(store) == 1


def test_bootstrap_entries_never_enter_select_for_send_candidates():
    jobs = [make_job(company="Acme", role="SWE Intern")]
    store, _events = bootstrap(jobs, now=T1)

    ids = select_for_send(store, cap=80)

    assert ids == []
    assert pending_count(store) == 0


# ---------------------------------------------------------------------------
# select_for_send / mark_sent: sent_at three states, independent predicates,
# sort key, cap. These are the pure building blocks daily.py will sequence
# as: ids = select_for_send(...); success = emailer.send(...); if success:
# store = mark_sent(...) — see the module docstring for why the emailer call
# itself lives in the test, not inside deduplicate.py.
# ---------------------------------------------------------------------------


def test_select_for_send_then_mark_sent_stamps_sent_at():
    job = make_job(company="Acme", role="SWE Intern")
    store = {job.canonical_id: make_seen_entry(job, fit_score=50, first_seen_at=T1, sent_at=None)}

    ids = select_for_send(store, cap=80)
    assert ids == [job.canonical_id]

    emailer = FakeEmailer(should_succeed=True)
    batch = [store[cid] for cid in ids]
    success = emailer.send(batch)
    assert success
    updated = mark_sent(store, ids, now=T2)

    assert updated[job.canonical_id].sent_at == T2
    assert pending_count(updated) == 0
    assert len(emailer.calls) == 1 and len(emailer.calls[0]) == 1


def test_select_for_send_email_failure_keeps_sent_at_null():
    """必须有的用例：邮件发送失败后 sent_at 保持 null. mark_sent is the only
    place sent_at is written, and the caller must not invoke it unless
    emailer.send() returned True — a failed send here simply never calls it.
    """
    job = make_job(company="Acme", role="SWE Intern")
    store = {job.canonical_id: make_seen_entry(job, fit_score=50, first_seen_at=T1, sent_at=None)}

    ids = select_for_send(store, cap=80)
    emailer = FakeEmailer(should_succeed=False)
    batch = [store[cid] for cid in ids]
    success = emailer.send(batch)
    assert not success

    # Orchestrator (future daily.py) must not call mark_sent here — store is
    # simply never touched. Simulate that by not calling it and asserting
    # the untouched store still has sent_at=None.
    assert store[job.canonical_id].sent_at is None
    assert pending_count(store) == 1


def test_select_for_send_dedup_and_resend_predicates_are_independent():
    """去重判据 (canonical_id 是否存在) 与选发判据 (sent_at is None) 必须是两个
    独立表达式：一条已经存在（去重判据=真）但从未发送（sent_at=None）的记录，
    必须仍然参选，即使它不是"新增"。
    """
    already_seen_but_unsent = make_job(company="Old Co", role="Backlog Intern")
    store = {
        already_seen_but_unsent.canonical_id: make_seen_entry(
            already_seen_but_unsent, fit_score=30, first_seen_at=T1, sent_at=None
        )
    }

    ids = select_for_send(store, cap=80)
    updated = mark_sent(store, ids, now=T2)

    assert ids == [already_seen_but_unsent.canonical_id]
    assert updated[already_seen_but_unsent.canonical_id].sent_at == T2


def test_select_for_send_overflow_never_writes_sent_at_for_excluded_records():
    jobs = [make_job(company="Acme", role=f"Intern {i}", row_hash_salt=str(i)) for i in range(5)]
    store = {
        j.canonical_id: make_seen_entry(j, fit_score=50, first_seen_at=T1, sent_at=None)
        for j in jobs
    }

    ids = select_for_send(store, cap=3)
    updated = mark_sent(store, ids, now=T2)

    assert len(ids) == 3
    sent_count = sum(1 for e in updated.values() if e.sent_at is not None)
    unsent_count = sum(1 for e in updated.values() if e.sent_at is None)
    assert sent_count == 3
    assert unsent_count == 2


def test_select_for_send_sort_key_first_seen_at_beats_fit_score():
    """必须有的用例：backlog 里 fit_score 低的项排在本次新增 fit_score 高的项前面。"""
    old_low_score = make_job(company="Old Co", role="Backlog Intern")
    new_high_score = make_job(company="New Co", role="Hot Intern", row_hash_salt="new")

    store = {
        old_low_score.canonical_id: make_seen_entry(
            old_low_score, fit_score=10, first_seen_at=T1, sent_at=None
        )
    }
    # simulate a fresh reconcile() + ranker call landing the new high-score
    # record in the same store, with a later first_seen_at
    reconcile_result = reconcile(store, [new_high_score], now=T2)
    store = apply_fit_scores(reconcile_result.store, {new_high_score.canonical_id: 99})
    assert store[new_high_score.canonical_id].first_seen_at == T2

    ids = select_for_send(store, cap=1)

    assert ids == [old_low_score.canonical_id], (
        "first_seen_at 没能优先于 fit_score：低分积压被高分新记录顶出了邮件"
    )

    updated = mark_sent(store, ids, now=T3)
    assert updated[new_high_score.canonical_id].sent_at is None
    assert updated[old_low_score.canonical_id].sent_at == T3


def test_select_for_send_tiebreak_uses_canonical_id_ascending():
    same_time_jobs = [
        make_job(company="Acme", role=f"Intern {i}", row_hash_salt=str(i)) for i in range(3)
    ]
    store = {
        j.canonical_id: make_seen_entry(j, fit_score=50, first_seen_at=T1, sent_at=None)
        for j in same_time_jobs
    }

    ids = select_for_send(store, cap=2)

    expected_ids = sorted(j.canonical_id for j in same_time_jobs)[:2]
    assert ids == expected_ids


def test_select_for_send_no_candidates_returns_empty():
    job = make_job(company="Acme", role="SWE Intern")
    store = {job.canonical_id: make_seen_entry(job, fit_score=50, first_seen_at=T1, sent_at=T1)}

    ids = select_for_send(store, cap=80)

    assert ids == []


def test_select_for_send_raises_if_a_candidate_is_unscored():
    job = make_job(company="Acme", role="SWE Intern")
    store = {job.canonical_id: make_seen_entry(job, fit_score=None, first_seen_at=T1, sent_at=None)}

    with pytest.raises(ValueError):
        select_for_send(store, cap=80)


# ---------------------------------------------------------------------------
# atomic write
# ---------------------------------------------------------------------------


def test_save_and_load_seen_jobs_round_trip(tmp_path):
    job = make_job(company="Acme", role="SWE Intern")
    entry = make_seen_entry(job, fit_score=50, first_seen_at=T1, sent_at=T1)
    store = {job.canonical_id: entry}

    path = tmp_path / "seen_jobs.json"
    save_seen_jobs_atomic(path, store)
    loaded = load_seen_jobs(path)

    assert loaded == store


def test_save_seen_jobs_leaves_no_temp_files_behind(tmp_path):
    job = make_job(company="Acme", role="SWE Intern")
    store = {job.canonical_id: make_seen_entry(job, fit_score=50, first_seen_at=T1, sent_at=T1)}
    path = tmp_path / "seen_jobs.json"

    save_seen_jobs_atomic(path, store)

    leftover = [p for p in tmp_path.iterdir() if p.name != "seen_jobs.json"]
    assert leftover == []


def test_save_seen_jobs_atomic_replaces_not_appends(tmp_path):
    job_a = make_job(company="Acme", role="SWE Intern")
    job_b = make_job(company="Globex", role="Data Intern", row_hash_salt="b")
    path = tmp_path / "seen_jobs.json"

    entry_a = make_seen_entry(job_a, fit_score=1, first_seen_at=T1, sent_at=T1)
    save_seen_jobs_atomic(path, {job_a.canonical_id: entry_a})
    entry_b = make_seen_entry(job_b, fit_score=2, first_seen_at=T1, sent_at=T1)
    save_seen_jobs_atomic(path, {job_b.canonical_id: entry_b})

    loaded = load_seen_jobs(path)
    assert set(loaded.keys()) == {job_b.canonical_id}


def test_load_seen_jobs_missing_file_returns_empty_store(tmp_path):
    assert load_seen_jobs(tmp_path / "does_not_exist.json") == {}


def test_seen_job_entry_is_frozen():
    job = make_job(company="Acme", role="SWE Intern")
    entry = make_seen_entry(job, fit_score=50, first_seen_at=T1, sent_at=None)
    with pytest.raises(Exception):
        entry.sent_at = T2  # type: ignore[misc]
    # the sanctioned way to change a field
    resent = replace(entry, sent_at=T2)
    assert resent.sent_at == T2
    assert entry.sent_at is None
