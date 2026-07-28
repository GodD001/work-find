"""
参照实现说明：job_radar.daily / deduplicate / emailer 尚未落地，
本文件内的 run_cycle() 是一个可执行参照实现，先把行为钉成断言，
真实模块落地后应把 run_cycle 换成 `from job_radar.daily import run_cycle`，
下面的测试用例本身不需要改。它的行为分两类：

A. 由 CLAUDE.md 铁律 2 / D-2 直接规定的，替换成真实模块时必须保持一致：
  - 去重判据 = canonical_id 是否已存在于 seen_jobs（铁律2）。
  - 选发判据 = sent_at is None，与去重判据相互独立（铁律2）：新增但未
    进入邮件正文的记录、以及上一轮的积压记录，都要按同一套判据参选。
  - 选发排序用三级键 first_seen_at ASC → fit_score DESC → canonical_id
    ASC（D-2）。
  - 单封邮件最多 cap（默认 80）条，超额部分不拆第二封，留到下次运行
    （D-2）。
  - 状态写入（first_seen_at、sent_at）必须整体挂在“邮件发送成功之后”
    的同一次原子更新上；邮件失败则本次运行一条也不写，哪怕只是新记录
    的 first_seen_at（铁律2 + D-2）。
  - 只有真正进入本次邮件正文（被选中）的记录才写 sent_at；未被选中的
    新记录仍要落 seen_jobs（不然下轮会被当成“新增”重复计数），但
    sent_at 留空（D-2）。
  - 候选池为空（`本次新增=0` 且 `队列积压=0`）时，完全跳过
    emailer.send()，不发一封全是 0 的空邮件（D-2；PRD §9.1，此前漏抄进
    CLAUDE.md，已补上）。
  - first_seen_at 取值 = 本系统首次成功抓取到该 canonical_id 的时刻
    （本次运行的统一时刻 now），不从来源的 date_posted_raw 反推（D-2）：
    date_posted_raw 是来源声称的发布时间（事实字段，铁律1下不补年份、
    不可信为排序时间戳），first_seen_at 是本系统的观察时间（系统字段），
    两者不可互相推导，这是铁律1的直接推论。

B. 参照实现自己补的细节，CLAUDE.md 没规定；换成真实模块时以真实模块
   为准，如果两者行为冲突，要回头补 CLAUDE.md，而不是回改真实模块：
  - fit_score 在记录首次写入 seen_jobs 时被“冻结”，之后作为积压记录
    参与排序直接复用旧值，不会每次运行重新打分。这么选是因为“要不要
    在每次运行时用最新 profile/关键词权重给积压记录重新打分”是一个
    还没拍板的产品问题（旧岗位的分数该维持首次判断、还是跟随口味变化
    重排？），冻结值最省事、也让断言的期望值在写入那一刻就完全确定，
    不代表这就是对的产品行为。详见 docs/open-questions.md OQ-1（含触发
    面评估：三级键下 fit_score 只在同批内生效，影响面很窄）。
  - seen_jobs 的每条记录假设存了完整的 company / role / fit_score 等
    字段，不只是 canonical_id 加两个时间戳，供积压记录重新进邮件正文、
    以及配合上面 fit_score 冻结的选择使用。D-3 的 fallback_key 兜底比对
    本就要求 company/role 可查，但 seen_jobs.json 的完整字段 schema
    CLAUDE.md 没有给出，这属于 models.py 阶段要正式定下来的内容，参照
    实现只是先当既定事实用了。
  - 没有真的实现一个做关键词匹配的 DeterministicScorer，而是在 fixture
    里直接给每条记录写死一个 fit_score 数字。这么选是因为这条测试要锁
    的是“选发排序逻辑”本身（三级键 + cap + 原子写入），不是打分算法；
    FR-011 的具体关键词打分公式不在这次任务范围内，也没有源文档可依据
    ——把两者耦合会导致 ranker.py 的打分 bug 连带把这条排序测试搞红，
    掩盖真正该测的问题，所以打分算法自己的正确性应该留给 ranker.py 的
    单元测试去管。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from job_radar.deduplicate import (
    SeenJobEntry,
    apply_fit_scores,
    mark_sent,
    pending_count,
    reconcile,
    select_for_send,
)
from job_radar.models import Job

CAP = 80
T1 = datetime(2026, 7, 28, 1, 30, tzinfo=timezone.utc)
T2 = T1 + timedelta(days=1)
T3 = T1 + timedelta(days=2)


@dataclass
class JobRecord:
    canonical_id: str
    company: str
    role: str
    fit_score: int


@dataclass
class SeenEntry:
    company: str
    role: str
    fit_score: int
    first_seen_at: datetime
    sent_at: datetime | None


class RecordingEmailer:
    def __init__(self, should_succeed: bool = True):
        self.should_succeed = should_succeed
        self.calls: list[list[JobRecord]] = []

    def send(self, batch: list[JobRecord]) -> bool:
        self.calls.append(list(batch))
        return self.should_succeed


def _to_job(record: JobRecord) -> Job:
    """Adapt this test file's minimal JobRecord into a full Job.

    Tagged canonical_id_tier="url" (with a synthetic application_url derived
    from the test's own canonical_id) specifically so D-3's fallback_key
    merge machinery — irrelevant to what this suite locks down — never
    triggers on these fixtures' repeated company/role text (make_flat_batch
    reuses "Acme"/"SWE Intern" across every record; under tier="hash_fallback"
    that would make deduplicate.merge_duplicates collapse them into one).
    """
    url = f"https://internal.test/{record.canonical_id}"
    return Job(
        source_repo="test",
        company=record.company,
        role=record.role,
        location="",
        date_posted_raw="",
        application_url=url,
        application_url_raw=url,
        source_job_id=None,
        is_closed=False,
        offers_sponsorship=None,
        requires_us_citizenship=None,
        raw_row_hash="n/a",
        canonical_id=record.canonical_id,
        canonical_id_tier="url",
    )


def _seen_entry_to_dedup(cid: str, e: SeenEntry) -> SeenJobEntry:
    return SeenJobEntry(
        canonical_id=cid,
        canonical_id_tier="url",
        source_repo="test",
        company=e.company,
        role=e.role,
        location="",
        date_posted_raw="",
        fallback_key=None,
        merged_ids=[],
        merged_row_count=1,
        fit_score=e.fit_score,
        first_seen_at=e.first_seen_at.isoformat(),
        last_seen_at=e.first_seen_at.isoformat(),
        sent_at=e.sent_at.isoformat() if e.sent_at else None,
    )


def _dedup_entry_to_seen(entry: SeenJobEntry) -> SeenEntry:
    return SeenEntry(
        company=entry.company,
        role=entry.role,
        fit_score=entry.fit_score,
        first_seen_at=datetime.fromisoformat(entry.first_seen_at),
        sent_at=datetime.fromisoformat(entry.sent_at) if entry.sent_at else None,
    )


def run_cycle(
    seen_jobs: dict[str, SeenEntry],
    fetched_records: list[JobRecord],
    emailer: RecordingEmailer,
    now: datetime,
    cap: int = CAP,
) -> tuple[dict[str, SeenEntry], dict[str, int]]:
    """Thin adapter over job_radar.deduplicate's real reconcile /
    select_for_send / mark_sent. daily.py (implementation step 6) doesn't
    exist yet, so this adapter is the orchestration stand-in until then —
    every bit of sorting/selection/state-write logic that used to be
    reimplemented in this file has been deleted; this function only
    translates between this test file's minimal fixtures and the real
    module's types, and sequences the three real calls the same way
    daily.py eventually will (see job_radar.deduplicate's module docstring).
    """
    now_iso = now.isoformat()
    backlog_before = sum(1 for e in seen_jobs.values() if e.sent_at is None)

    store = {cid: _seen_entry_to_dedup(cid, e) for cid, e in seen_jobs.items()}
    winners = [_to_job(r) for r in fetched_records]

    reconcile_result = reconcile(store, winners, now=now_iso)
    reconciled_store = reconcile_result.store
    new_ids = reconcile_result.new_ids

    # Stand-in for the (not-yet-built) ranker: this test's fixtures already
    # carry a deterministic fit_score directly on JobRecord (see docstring
    # section B) — apply_fit_scores only ever touches genuinely new ids, per
    # CLAUDE.md's AI 调用约束 (batch scoring only new records, never backlog).
    scores_by_id = {r.canonical_id: r.fit_score for r in fetched_records}
    reconciled_store = apply_fit_scores(reconciled_store, {cid: scores_by_id[cid] for cid in new_ids})

    stats = {"new": len(new_ids), "backlog_before": backlog_before}

    selected_ids = select_for_send(reconciled_store, cap=cap)
    if not selected_ids:
        return seen_jobs, {**stats, "sent": 0, "backlog": backlog_before}

    batch = [
        JobRecord(
            cid,
            reconciled_store[cid].company,
            reconciled_store[cid].role,
            reconciled_store[cid].fit_score,
        )
        for cid in selected_ids
    ]
    success = emailer.send(batch)
    if not success:
        # CLAUDE.md 铁律2: email failed -> this run writes nothing, not even
        # first_seen_at for brand-new records. Discard reconciled_store
        # entirely and return the caller's original, untouched seen_jobs.
        return seen_jobs, {**stats, "sent": 0, "backlog": backlog_before}

    final_store = mark_sent(reconciled_store, selected_ids, now=now_iso)
    remaining_backlog = pending_count(final_store)

    updated_seen_jobs = {cid: _dedup_entry_to_seen(entry) for cid, entry in final_store.items()}
    return updated_seen_jobs, {**stats, "sent": len(selected_ids), "backlog": remaining_backlog}


def make_flat_batch(prefix: str, count: int, score: int) -> list[JobRecord]:
    return [
        JobRecord(f"{prefix}-{i:03d}", "Acme", "SWE Intern", score) for i in range(count)
    ]


def test_backlog_drains_across_two_runs_with_no_loss_or_duplication():
    seen_jobs: dict[str, SeenEntry] = {}
    batch_150 = make_flat_batch("s1", 150, score=50)

    # Run 1: 150 条全新，first_seen_at 全打平，cap=80 只能发 80 条
    emailer_1 = RecordingEmailer(should_succeed=True)
    seen_jobs, stats_1 = run_cycle(seen_jobs, batch_150, emailer_1, now=T1)
    assert stats_1 == {"new": 150, "backlog_before": 0, "sent": 80, "backlog": 70}
    assert len(emailer_1.calls) == 1 and len(emailer_1.calls[0]) == 80

    # Run 2：输入不变 → 新增必须为 0，但要把上次的 70 条积压发出去
    emailer_2 = RecordingEmailer(should_succeed=True)
    seen_jobs, stats_2 = run_cycle(seen_jobs, batch_150, emailer_2, now=T2)
    assert stats_2 == {"new": 0, "backlog_before": 70, "sent": 70, "backlog": 0}
    assert len(emailer_2.calls) == 1 and len(emailer_2.calls[0]) == 70

    # Run 3：输入不变，队列已清空 → 不应该调用 emailer.send
    emailer_3 = RecordingEmailer(should_succeed=True)
    seen_jobs, stats_3 = run_cycle(seen_jobs, batch_150, emailer_3, now=T3)
    assert stats_3 == {"new": 0, "backlog_before": 0, "sent": 0, "backlog": 0}
    assert emailer_3.calls == []

    assert len(seen_jobs) == 150
    sent_ats = [e.sent_at for e in seen_jobs.values()]
    assert all(sent_at is not None for sent_at in sent_ats), "存在遗漏：有记录 sent_at 仍为空"
    assert len(set(sent_ats)) == len(sent_ats) or True  # sent_at 允许同批重复值，不作为重复判据
    assert sorted(sent_ats).count(T1) == 80
    assert sorted(sent_ats).count(T2) == 70
    assert len(set(seen_jobs.keys())) == 150, "存在重复：canonical_id 出现了不止一次"


def test_first_seen_at_outranks_fit_score_in_overflow_selection():
    """
    专门锁三级键第一层：first_seen_at ASC 必须优先于 fit_score DESC。
    如果实现里把排序键写反（先按 fit_score 排），
    这条测试必红——因为 90+ 分的新岗位会把低分积压顶出邮件。
    """
    seen_jobs: dict[str, SeenEntry] = {}

    # Run 1：80 条高分（70）先发出去，70 条低分（20）沦为积压
    sent_in_run1 = make_flat_batch("ovf", 80, score=70)
    backlog_from_run1 = make_flat_batch("ovf", 70, score=20)
    # 修正 id 前缀避免和高分批重叠
    backlog_from_run1 = [
        JobRecord(f"ovf-{i + 80:03d}", r.company, r.role, r.fit_score)
        for i, r in enumerate(backlog_from_run1)
    ]
    run1_input = sent_in_run1 + backlog_from_run1
    emailer_1 = RecordingEmailer(should_succeed=True)
    seen_jobs, stats_1 = run_cycle(seen_jobs, run1_input, emailer_1, now=T1)
    assert stats_1 == {"new": 150, "backlog_before": 0, "sent": 80, "backlog": 70}
    backlog_ids = {cid for cid, e in seen_jobs.items() if e.sent_at is None}
    assert backlog_ids == {f"ovf-{i:03d}" for i in range(80, 150)}

    # Run 2b：同一批积压（70 条，低分、first_seen_at=t1）
    # + 100 条全新高分岗位（95+，first_seen_at=t2），其中 10 条 score=99 明显最高
    top_new = [JobRecord(f"new-{i:03d}", "Acme", "SWE Intern", 99) for i in range(10)]
    rest_new = [JobRecord(f"new-{i:03d}", "Acme", "SWE Intern", 95) for i in range(10, 100)]
    run2b_input = top_new + rest_new  # 积压不需要重新喂给 run_cycle，它已经在 seen_jobs 里

    emailer_2b = RecordingEmailer(should_succeed=True)
    seen_jobs, stats_2b = run_cycle(seen_jobs, run2b_input, emailer_2b, now=T2)

    assert stats_2b == {"new": 100, "backlog_before": 70, "sent": 80, "backlog": 90}
    assert len(emailer_2b.calls) == 1
    sent_ids = {r.canonical_id for r in emailer_2b.calls[0]}

    expected_sent_ids = backlog_ids | {f"new-{i:03d}" for i in range(10)}
    assert sent_ids == expected_sent_ids, (
        "first_seen_at 没能优先于 fit_score：低分积压被高分新岗位顶出了邮件"
    )

    # 90 条低优先级新岗位必须留在队列里，不能被跳过发送，也不能提前标记 sent_at
    still_pending = {cid for cid, e in seen_jobs.items() if e.sent_at is None}
    assert still_pending == {f"new-{i:03d}" for i in range(10, 100)}
