"""D-3 identity resolution, seen_jobs.json state, bootstrap, and D-2 selection.

Three independent concerns live here, kept deliberately separate because
CLAUDE.md 铁律2 requires the two predicates never be merged into one
condition:

- `merge_duplicates` / `reconcile`: is this canonical_id (or a known alias of
  it) already in seen_jobs? This is the *dedup* predicate.
- `select_for_send`: is `sent_at is None`? This is the independent *resend*
  predicate — it doesn't care whether a record is brand new this run or
  backlog from three runs ago.

Loser -> winner traceability (this module's own design — see CLAUDE.md D-3
"补充：merged_ids 可追溯映射" for the write-up): every `SeenJobEntry` for a
hash_fallback-tier winner carries `merged_ids`, the full history of loser
canonical_ids ever folded into it (both same-run layer-2 merges and cross-run
fallback_key hits). A loser canonical_id is a deterministic hash of
source_repo + raw row text, so if that exact row reappears unchanged next run
it recomputes to the *same* id — `merged_ids` lets `reconcile` recognize it by
direct alias lookup instead of re-deriving the merge from scratch every time,
and without incrementing the merge count or re-logging on every reappearance
(see test_reconcile_alias_reappearance_does_not_double_count_or_re_log).

Module boundaries (CLAUDE.md 目录结构: fetchers/ -> deduplicate.py ->
ranker.py -> emailer.py -> daily.py orchestrates): this module never scores
and never sends mail.
  - `reconcile` does not accept fit_score — it can't: whether a record is
    new is only known *after* reconcile runs, so a caller can't pre-score
    everything (that would mean re-scoring hundreds of already-sent jobs with
    the AI ranker every day, blowing the batching budget in CLAUDE.md's "AI
    调用约束"). New entries are created with `fit_score=None`; the caller
    reads `new_ids` off the result, runs the ranker on just those, and writes
    scores back with `apply_fit_scores`.
  - `select_for_send` picks candidate ids and stops — it does not call an
    emailer. Sending mail is emailer.py's job and sequencing "select -> send
    -> mark_sent only if send succeeded" is daily.py's job (CLAUDE.md 铁律2).
    This module only guarantees `mark_sent` is the *sole* place `sent_at`
    ever gets written, so that invariant can't be bypassed by construction.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path

from job_radar.models import CanonicalIdTier, Job
from job_radar.normalize import fallback_key as compute_fallback_key

BOOTSTRAP_SENT_AT = "bootstrap"

_LAYER1_MATCHED_FIELDS = ("raw_row_hash",)
_LAYER2_MATCHED_FIELDS = ("company", "role", "location", "date_posted_raw")


@dataclass(frozen=True)
class MergeEvent:
    """One `run_manifest.jsonl` `dedup_merged` record (CLAUDE.md D-3)."""

    canonical_id: str  # == winner_id; kept as its own field for the CLAUDE.md example schema
    winner_id: str
    loser_id: str
    source_repo: str
    merged_count: int
    reason: str  # "row_hash_duplicate" | "fallback_key" | "fallback_key_cross_run"
    matched_fields: tuple[str, ...]
    timestamp: str
    event: str = "dedup_merged"
    # SHA-256 of the folded-away row's raw cells (normalize.row_hash). For
    # "row_hash_duplicate" this *is* winner_id/loser_id's own hash, restated
    # here so the log line carries a concrete, greppable content digest
    # instead of only an opaque id repeating itself.
    raw_row_hash: str | None = None

    def to_json_line(self) -> str:
        return json.dumps(
            {
                "event": self.event,
                "canonical_id": self.canonical_id,
                "winner_id": self.winner_id,
                "loser_id": self.loser_id,
                "source_repo": self.source_repo,
                "merged_count": self.merged_count,
                "reason": self.reason,
                "matched_fields": list(self.matched_fields),
                "raw_row_hash": self.raw_row_hash,
                "timestamp": self.timestamp,
            },
            ensure_ascii=False,
        )


@dataclass(frozen=True)
class SeenJobEntry:
    """One row of seen_jobs.json.

    `fallback_key`/`location`/`date_posted_raw` are stored (not just
    `company`/`role`) per CLAUDE.md D-3 "跨 run 稳定性" so a later run can
    rebuild the fallback_key index without re-deriving normalize() results —
    freezing them at write time also means a future change to normalize's
    text-cleaning rules can't retroactively shift an existing index entry.
    """

    canonical_id: str
    canonical_id_tier: CanonicalIdTier
    source_repo: str
    company: str
    role: str
    location: str
    date_posted_raw: str
    fallback_key: tuple[str, str] | None
    merged_ids: list[str]
    merged_row_count: int
    fit_score: int | None
    first_seen_at: str
    last_seen_at: str
    sent_at: str | None

    def to_dict(self) -> dict:
        return {
            "canonical_id_tier": self.canonical_id_tier,
            "source_repo": self.source_repo,
            "company": self.company,
            "role": self.role,
            "location": self.location,
            "date_posted_raw": self.date_posted_raw,
            "fallback_key": list(self.fallback_key) if self.fallback_key is not None else None,
            "merged_ids": list(self.merged_ids),
            "merged_row_count": self.merged_row_count,
            "fit_score": self.fit_score,
            "first_seen_at": self.first_seen_at,
            "last_seen_at": self.last_seen_at,
            "sent_at": self.sent_at,
        }

    @classmethod
    def from_dict(cls, canonical_id: str, data: dict) -> SeenJobEntry:
        fk = data["fallback_key"]
        return cls(
            canonical_id=canonical_id,
            canonical_id_tier=data["canonical_id_tier"],
            source_repo=data["source_repo"],
            company=data["company"],
            role=data["role"],
            location=data["location"],
            date_posted_raw=data["date_posted_raw"],
            fallback_key=tuple(fk) if fk is not None else None,
            merged_ids=list(data["merged_ids"]),
            merged_row_count=data["merged_row_count"],
            fit_score=data["fit_score"],
            first_seen_at=data["first_seen_at"],
            last_seen_at=data["last_seen_at"],
            sent_at=data["sent_at"],
        )


SeenStore = dict[str, SeenJobEntry]


# ---------------------------------------------------------------------------
# merge_duplicates — D-3 layer 1 + layer 2, within a single fetch batch
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MergeResult:
    winners: list[Job]
    events: list[MergeEvent]
    merge_counts: dict[str, int]  # winner canonical_id -> total raw rows merged (this batch)
    # winner canonical_id -> loser ids folded into it (layer 2 only)
    merged_ids: dict[str, list[str]] = field(default_factory=dict)


def _cluster_key(job: Job) -> tuple[str, tuple[str, str], str, str]:
    # source_repo is included even though CLAUDE.md's fallback_key definition
    # doesn't mention it — see CLAUDE.md D-3 "补充：merged_ids 可追溯映射" for
    # why this module requires source_repo to also match.
    return (
        job.source_repo,
        compute_fallback_key(job.company, job.role),
        job.location,
        job.date_posted_raw,
    )


def _entry_fk_key(entry: SeenJobEntry) -> tuple[str, tuple[str, str], str, str]:
    """Same shape as `_cluster_key`, but reading an already-computed
    `SeenJobEntry.fallback_key` instead of recomputing it from company/role —
    used to index/query `store` entries during cross-run reconciliation."""
    assert entry.fallback_key is not None
    return (entry.source_repo, entry.fallback_key, entry.location, entry.date_posted_raw)


def merge_duplicates(jobs: list[Job], *, now: str) -> MergeResult:
    events: list[MergeEvent] = []

    # Layer 1: collapse jobs sharing an identical canonical_id already (any
    # tier — for url/job_id tiers this is just plain duplicate-row dedup, not
    # audit-worthy; CLAUDE.md only requires logging for tier 3, see 铁律3/D-3).
    order: list[str] = []
    groups: dict[str, list[Job]] = {}
    for job in jobs:
        if job.canonical_id not in groups:
            groups[job.canonical_id] = []
            order.append(job.canonical_id)
        groups[job.canonical_id].append(job)

    layer1_repr: dict[str, Job] = {}
    layer1_counts: dict[str, int] = {}
    for cid in order:
        members = groups[cid]
        layer1_repr[cid] = members[0]
        layer1_counts[cid] = len(members)
        if len(members) > 1 and members[0].canonical_id_tier == "hash_fallback":
            events.append(
                MergeEvent(
                    canonical_id=cid,
                    winner_id=cid,
                    loser_id=cid,
                    source_repo=members[0].source_repo,
                    merged_count=len(members),
                    reason="row_hash_duplicate",
                    matched_fields=_LAYER1_MATCHED_FIELDS,
                    timestamp=now,
                    raw_row_hash=members[0].raw_row_hash,
                )
            )

    # Layer 2: among hash_fallback-tier layer-1 groups only, cluster by
    # (source_repo, fallback_key, location, date_posted_raw). Operates purely
    # on the layer-1 output set — never rescans jobs — per constraint c.
    tier3_ids = [cid for cid in order if layer1_repr[cid].canonical_id_tier == "hash_fallback"]
    clusters: dict[tuple, list[str]] = {}
    for cid in tier3_ids:
        clusters.setdefault(_cluster_key(layer1_repr[cid]), []).append(cid)

    redirect: dict[str, str] = {}
    final_counts: dict[str, int] = dict(layer1_counts)
    merged_ids_by_winner: dict[str, list[str]] = {}

    for ids in clusters.values():
        if len(ids) <= 1:
            continue
        winner = min(ids)
        losers = sorted(cid for cid in ids if cid != winner)
        cumulative = layer1_counts[winner]
        folded: list[str] = []
        for loser in losers:
            cumulative += layer1_counts[loser]
            redirect[loser] = winner
            folded.append(loser)
            final_counts[winner] = cumulative
            events.append(
                MergeEvent(
                    canonical_id=winner,
                    winner_id=winner,
                    loser_id=loser,
                    source_repo=layer1_repr[winner].source_repo,
                    merged_count=cumulative,
                    reason="fallback_key",
                    matched_fields=_LAYER2_MATCHED_FIELDS,
                    timestamp=now,
                    raw_row_hash=layer1_repr[loser].raw_row_hash,
                )
            )
        merged_ids_by_winner[winner] = folded

    winners: list[Job] = []
    merge_counts: dict[str, int] = {}
    for cid in order:
        if cid in redirect:
            continue
        winners.append(layer1_repr[cid])
        merge_counts[cid] = final_counts.get(cid, layer1_counts[cid])

    return MergeResult(
        winners=winners, events=events, merge_counts=merge_counts, merged_ids=merged_ids_by_winner
    )


# ---------------------------------------------------------------------------
# reconcile — cross-run identity resolution against seen_jobs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReconcileResult:
    store: SeenStore
    new_ids: list[str]
    events: list[MergeEvent]


def reconcile(
    store: SeenStore,
    winners: list[Job],
    *,
    now: str,
    merge_counts: dict[str, int] | None = None,
    merged_ids: dict[str, list[str]] | None = None,
) -> ReconcileResult:
    """Fold already-merged `winners` (the output of `merge_duplicates`) into
    `store`. `merge_counts`/`merged_ids` are `merge_duplicates`'s outputs,
    carried over so a winner that already absorbed same-batch losers gets its
    seen_jobs entry created with the right merged_ids/merged_row_count on
    first sight, instead of only being discoverable later.

    Dedup predicate (three independent lookups, in order — never merged into
    one expression, CLAUDE.md 铁律2):
      1. `job.canonical_id` already a key in `store` -> known.
      2. `job.canonical_id` already appears in some entry's `merged_ids` ->
         known (this is the loser_id alias mechanism).
      3. (hash_fallback tier only) `(source_repo, fallback_key, location,
         date_posted_raw)` matches an existing entry -> known; record the new
         alias and log a `fallback_key_cross_run` merge event.
      Otherwise: genuinely new — created with fit_score=None. Deliberately no
      fit_score parameter here: whether a record is new is only known *after*
      this function runs, so a caller cannot pre-score every winner without
      re-scoring the entire history through the AI ranker on every run
      (blowing CLAUDE.md's 25–40/batch budget). Call `apply_fit_scores` with
      `result.new_ids` once the ranker has scored just those.
    """
    merge_counts = merge_counts or {}
    merged_ids = merged_ids or {}

    new_store: SeenStore = dict(store)
    events: list[MergeEvent] = []
    new_ids: list[str] = []

    alias_index: dict[str, str] = {}
    for cid, entry in store.items():
        for loser in entry.merged_ids:
            alias_index[loser] = cid

    fk_index: dict[tuple, str] = {}
    for cid, entry in store.items():
        if entry.canonical_id_tier == "hash_fallback" and entry.fallback_key is not None:
            fk_index[_entry_fk_key(entry)] = cid

    for job in winners:
        if job.canonical_id in new_store:
            entry = new_store[job.canonical_id]
            new_store[job.canonical_id] = replace(entry, last_seen_at=now)
            continue

        alias_winner = alias_index.get(job.canonical_id)
        if alias_winner is not None:
            entry = new_store[alias_winner]
            new_store[alias_winner] = replace(entry, last_seen_at=now)
            continue

        if job.canonical_id_tier == "hash_fallback":
            key = (
                job.source_repo,
                compute_fallback_key(job.company, job.role),
                job.location,
                job.date_posted_raw,
            )
            fk_winner = fk_index.get(key)
            if fk_winner is not None:
                entry = new_store[fk_winner]
                # `job` is itself a merge_duplicates winner and may already
                # represent more than one raw row from *this* batch (e.g. it
                # absorbed same-run layer-1/layer-2 losers before ever
                # reaching here) — the row count it contributes to the
                # historical winner is that full batch-local count, not a
                # flat +1, and every one of its own batch-local losers must
                # become an alias of fk_winner too, not just job itself.
                contributed_rows = merge_counts.get(job.canonical_id, 1)
                newly_folded_ids = [job.canonical_id, *merged_ids.get(job.canonical_id, [])]
                new_count = entry.merged_row_count + contributed_rows
                new_store[fk_winner] = replace(
                    entry,
                    last_seen_at=now,
                    merged_ids=[*entry.merged_ids, *newly_folded_ids],
                    merged_row_count=new_count,
                )
                events.append(
                    MergeEvent(
                        canonical_id=fk_winner,
                        winner_id=fk_winner,
                        loser_id=job.canonical_id,
                        source_repo=job.source_repo,
                        merged_count=new_count,
                        reason="fallback_key_cross_run",
                        matched_fields=_LAYER2_MATCHED_FIELDS,
                        timestamp=now,
                        raw_row_hash=job.raw_row_hash,
                    )
                )
                # Every newly-folded id, once resolved, must itself become an
                # alias so a future reappearance of *that exact* row hits the
                # O(1) alias lookup above instead of the fallback_key path.
                for lid in newly_folded_ids:
                    alias_index[lid] = fk_winner
                continue

        entry = SeenJobEntry(
            canonical_id=job.canonical_id,
            canonical_id_tier=job.canonical_id_tier,
            source_repo=job.source_repo,
            company=job.company,
            role=job.role,
            location=job.location,
            date_posted_raw=job.date_posted_raw,
            fallback_key=compute_fallback_key(job.company, job.role)
            if job.canonical_id_tier == "hash_fallback"
            else None,
            merged_ids=list(merged_ids.get(job.canonical_id, [])),
            merged_row_count=merge_counts.get(job.canonical_id, 1),
            fit_score=None,
            first_seen_at=now,
            last_seen_at=now,
            sent_at=None,
        )
        new_store[job.canonical_id] = entry
        new_ids.append(job.canonical_id)
        if entry.canonical_id_tier == "hash_fallback" and entry.fallback_key is not None:
            fk_index[_entry_fk_key(entry)] = job.canonical_id

    return ReconcileResult(store=new_store, new_ids=new_ids, events=events)


def apply_fit_scores(store: SeenStore, scores: dict[str, int]) -> SeenStore:
    """Write ranker output back onto entries `reconcile` created with
    fit_score=None. Split out from `reconcile` itself so scoring only ever
    touches `new_ids` (CLAUDE.md AI 调用约束: 分批, never the whole backlog).
    """
    updated: SeenStore = dict(store)
    for cid, score in scores.items():
        updated[cid] = replace(updated[cid], fit_score=score)
    return updated


# ---------------------------------------------------------------------------
# bootstrap — D-2: full-history load, sent_at="bootstrap", never ranked/sent
# ---------------------------------------------------------------------------


def bootstrap(jobs: list[Job], *, now: str) -> tuple[SeenStore, list[MergeEvent]]:
    merge_result = merge_duplicates(jobs, now=now)
    store: SeenStore = {}
    for job in merge_result.winners:
        store[job.canonical_id] = SeenJobEntry(
            canonical_id=job.canonical_id,
            canonical_id_tier=job.canonical_id_tier,
            source_repo=job.source_repo,
            company=job.company,
            role=job.role,
            location=job.location,
            date_posted_raw=job.date_posted_raw,
            fallback_key=compute_fallback_key(job.company, job.role)
            if job.canonical_id_tier == "hash_fallback"
            else None,
            merged_ids=list(merge_result.merged_ids.get(job.canonical_id, [])),
            merged_row_count=merge_result.merge_counts.get(job.canonical_id, 1),
            fit_score=None,
            first_seen_at=now,
            last_seen_at=now,
            sent_at=BOOTSTRAP_SENT_AT,
        )
    return store, merge_result.events


# ---------------------------------------------------------------------------
# select_for_send / mark_sent — D-2: three-tier sort, cap, sent_at write
#
# Deliberately two pure functions, not one that also calls an emailer: this
# module owns identity/state, not delivery (CLAUDE.md 目录结构 puts sending
# in emailer.py, orchestration in daily.py). The caller is expected to run:
#
#   ids = select_for_send(store, cap=cap)
#   if ids:
#       success = emailer.send([store[cid] for cid in ids])
#       if success:
#           store = mark_sent(store, ids, now=now)
#   # on failure, store is untouched -> sent_at stays null (CLAUDE.md 铁律2)
#
# so "email failed -> never write sent_at" holds by construction: there is no
# code path that writes sent_at other than mark_sent, and nothing calls
# mark_sent except after a confirmed successful send.
# ---------------------------------------------------------------------------


def pending_count(store: SeenStore) -> int:
    return sum(1 for entry in store.values() if entry.sent_at is None)


def select_for_send(store: SeenStore, *, cap: int = 80) -> list[str]:
    """Resend predicate is `sent_at is None` — independent of, and never
    merged with, the dedup predicate in `reconcile` (CLAUDE.md 铁律2). An
    entry reaches here whether it was created moments ago by `reconcile` or
    has been sitting in the backlog for weeks; this function doesn't care
    which. Pure: takes no `now`, sends no mail, mutates nothing.
    """
    candidates: list[tuple[str, int, str]] = []
    for cid, entry in store.items():
        if entry.sent_at is not None:
            continue
        if entry.fit_score is None:
            raise ValueError(
                f"seen entry {cid!r} has sent_at=None but fit_score is None — "
                "it must be scored (apply_fit_scores) before it can be a send candidate"
            )
        candidates.append((entry.first_seen_at, -entry.fit_score, cid))

    # 三级键：first_seen_at ASC -> fit_score DESC -> canonical_id ASC
    candidates.sort()
    return [cid for *_rest, cid in candidates[:cap]]


def mark_sent(store: SeenStore, ids: list[str], *, now: str) -> SeenStore:
    """The only function in this codebase allowed to write `sent_at`. Callers
    must only invoke this after a confirmed-successful `emailer.send()`."""
    updated: SeenStore = dict(store)
    for cid in ids:
        updated[cid] = replace(updated[cid], sent_at=now)
    return updated


# ---------------------------------------------------------------------------
# seen_jobs.json / run_manifest.jsonl persistence
# ---------------------------------------------------------------------------


def load_seen_jobs(path: Path) -> SeenStore:
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {cid: SeenJobEntry.from_dict(cid, data) for cid, data in raw.items()}


def save_seen_jobs_atomic(path: Path, store: SeenStore) -> None:
    """Temp file + os.replace — never an in-place overwrite, so a crash or
    concurrent read mid-write can never observe a partially-written file
    (CLAUDE.md D-2 硬约束)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {cid: entry.to_dict() for cid, entry in store.items()}

    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".seen_jobs.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.remove(tmp_name)
        raise


def append_run_manifest(path: Path, events: list[MergeEvent]) -> None:
    if not events:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for event in events:
            f.write(event.to_json_line() + "\n")
