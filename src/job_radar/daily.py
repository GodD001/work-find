"""每日运行编排 (CLAUDE.md 目录结构 步骤6a / 铁律2).

顺序不可调换：

    抓取 -> 过滤(is_closed) -> 去重 -> 打分 -> 选发 -> 发信 -> 原子写状态 -> commit

- 过滤发生在去重之前（filters.py，CLAUDE.md 常见陷阱5 附近）：is_closed
  的记录被拿掉之后 reconcile 压根不知道它存在过，不写入 seen_jobs.json、
  不打分、不进邮件。每次运行往 run_manifest.jsonl 记一条按 source_repo
  汇总的 filtered_closed 事件（只记数量），--explain-scores 路径同样过滤，
  否则调 profile.yaml 权重时看到的数据和实际推送的不一致。
- 全部 source 失败 -> 当次不写任何 seen 状态，直接退出（非零退出码）。
- 单个 source 失败 -> 继续，邮件中通过 EmailStats.missing_sources 标明缺失来源。
- mark_sent 当且仅当 send() 返回 SendOutcome.SENT。
- SendOutcome.FAILED -> 非零退出码，一条状态都不写（seen_jobs.json /
  run_manifest.jsonl / job_history 都不动，铁律2：邮件失败=本次运行未发生）。
- SendOutcome.DRY_RUN -> 退出码 0，不碰 data/ 下任何文件。
- 候选池为空（本次新增=0 且队列积压=0，即 select_for_send 返回空列表）->
  完全跳过 send()，但 reconcile 产生的 last_seen_at / 新记录仍照常原子写入
  并 commit——"跳过发信"和"跳过写状态"是两件独立的事，不许合并（铁律2 的
  独立谓词精神同样适用于这里）。
- `run_bootstrap` / `--bootstrap`（D-2）：只在 `seen_jobs.json` 为空时允许
  跑，整份覆盖写、`sent_at` 全填 `"bootstrap"`、不打分不发信。已有内容时
  直接拒绝退出，不做任何写入——bootstrap 是不可逆的整份覆盖，不能假设它
  幂等。

这一步只做本地编排到可运行；把它接进 GitHub Actions workflow
（daily.yml、workflow_dispatch --dry-run/--bootstrap、push）留给步骤 6b。
这里的 commit 只在本地仓库落一个提交，不 push——push 是有更广播达范围的
操作，交给 CI 去做更合适。

打分目前只有 FR-011 关键词降级路径（ranker.py）接进来了——AI 排序
（分批调用 Claude、批失败重试）是 ranker.py 后续要补的部分，这一步之前，
每次运行的 EmailStats.ai_ranking_unavailable 恒为 True，邮件横幅照
CLAUDE.md 要求如实显示"AI 排序不可用"，这不是 bug，是当前实现阶段的
真实状态。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from job_radar.deduplicate import (
    MergeEvent,
    SeenJobEntry,
    SeenStore,
    append_run_manifest,
    apply_fit_scores,
    load_seen_jobs,
    mark_sent,
    merge_duplicates,
    pending_count,
    reconcile,
    save_seen_jobs_atomic,
    select_for_send,
)
from job_radar.deduplicate import (
    bootstrap as _bootstrap_store,
)
from job_radar.emailer import EmailStats, SendOutcome, SmtpConfig
from job_radar.emailer import send as emailer_send
from job_radar.fetchers import vansh
from job_radar.filters import build_filtered_closed_events, filter_closed
from job_radar.models import Job
from job_radar.ranker import (
    DEFAULT_PROFILE_PATH,
    ScoreExplanation,
    explain_job,
    load_profile,
    score_new_jobs,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DATA_DIR = _REPO_ROOT / "data"
_SEEN_JOBS_PATH = _DATA_DIR / "seen_jobs.json"
_RUN_MANIFEST_PATH = _DATA_DIR / "run_manifest.jsonl"
_HISTORY_DIR = _DATA_DIR / "history"
_DEFAULT_PREVIEW_PATH = Path("/tmp/preview.html")
_DEFAULT_CAP = 80

logger = logging.getLogger(__name__)


def _write_step_summary(title: str, lines: list[str]) -> None:
    """把关键统计追加一份到 GitHub Actions 的 $GITHUB_STEP_SUMMARY（Actions
    页面上直接渲染成 Markdown 的那块摘要），本地跑（这个环境变量不存在）时
    整个函数是 no-op。这是"再写一份"，不是替代 logger——logger 走的是这次
    运行的完整时间线，summary 只留最后结果，方便在 Actions 页面上一眼看到
    今天抓了/发了多少，不用点开日志逐行找。"""
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    with open(summary_path, "a", encoding="utf-8") as f:
        f.write(f"### {title}\n\n")
        for line in lines:
            f.write(f"- {line}\n")
        f.write("\n")


class SourceAdapter(Protocol):
    def fetch(self) -> str: ...
    def parse(self, raw: str) -> list[Job]: ...
    def validate(self, records: list[Job]) -> list[Job]: ...


_SOURCES: dict[str, SourceAdapter] = {"s1": vansh}


@dataclass(frozen=True)
class CycleResult:
    exit_code: int
    outcome: SendOutcome | None
    new_count: int
    sent_count: int
    backlog_count: int
    missing_sources: list[str] = field(default_factory=list)


def _select_sources(
    only_source: str | None, sources: dict[str, SourceAdapter] | None
) -> dict[str, SourceAdapter]:
    pool = sources if sources is not None else _SOURCES
    if only_source is None:
        return dict(pool)
    if only_source not in pool:
        raise ValueError(f"unknown source: {only_source!r} (known: {sorted(pool)})")
    return {only_source: pool[only_source]}


def _fetch_all(
    sources: dict[str, SourceAdapter], *, now_iso: str
) -> tuple[list[Job], list[str], list[str]]:
    """Returns (fetched jobs across all sources, missing source ids, raw
    manifest json lines for source failures). A source failure never raises
    out of here — 铁律3 says a single source failing must not stop the
    others; only an unrecognized *table structure within* a source's own
    parser is allowed to raise all the way up to here and get caught, same
    as any other fetch failure (network, HTTP error)."""
    fetched: list[Job] = []
    missing: list[str] = []
    manifest_lines: list[str] = []
    for source_id, adapter in sources.items():
        start = time.monotonic()
        try:
            raw = adapter.fetch()
            records = adapter.validate(adapter.parse(raw))
        except Exception as exc:  # noqa: BLE001 - deliberately broad, see docstring
            elapsed = time.monotonic() - start
            logger.warning("source=%s 抓取失败（%.1fs）：%s", source_id, elapsed, exc)
            missing.append(source_id)
            manifest_lines.append(
                json.dumps(
                    {
                        "event": "source_fetch_failed",
                        "source_id": source_id,
                        "error": str(exc),
                        "timestamp": now_iso,
                    },
                    ensure_ascii=False,
                )
            )
            continue
        elapsed = time.monotonic() - start
        logger.info("source=%s 抓到 %d 条（%.1fs）", source_id, len(records), elapsed)
        fetched.extend(records)
    return fetched, missing, manifest_lines


def _append_manifest_lines(path: Path, lines: list[str]) -> None:
    if not lines:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for line in lines:
            f.write(line + "\n")


def _append_job_history(
    history_dir: Path, store: SeenStore, sent_ids: list[str], now: datetime
) -> None:
    """CLAUDE.md 常见陷阱7: 按月轮转，一条发出的岗位写一行。只记录这次运行
    真正发出的岗位（selected_ids 里最终标了 sent_at 的那些），不是整个
    store——job_history 是"发送记录"，不是 seen_jobs 的镜像。"""
    if not sent_ids:
        return
    history_dir.mkdir(parents=True, exist_ok=True)
    path = history_dir / f"job_history_{now:%Y-%m}.jsonl"
    with path.open("a", encoding="utf-8") as f:
        for cid in sent_ids:
            entry = store[cid]
            payload = {"canonical_id": cid, **entry.to_dict()}
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _git_commit(repo_root: Path, paths: list[Path], message: str) -> None:
    rel_paths = [str(p.relative_to(repo_root)) for p in paths if p.exists()]
    if not rel_paths:
        return
    subprocess.run(["git", "add", *rel_paths], cwd=repo_root, check=True)
    staged = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=repo_root)
    if staged.returncode == 0:
        return  # 没有实际变化（比如 last_seen_at 重复写入同一个值），跳过空 commit
    subprocess.run(["git", "commit", "-m", message], cwd=repo_root, check=True)


def run_daily(
    *,
    now: datetime,
    dry_run: bool = False,
    only_source: str | None = None,
    sources: dict[str, SourceAdapter] | None = None,
    seen_jobs_path: Path = _SEEN_JOBS_PATH,
    run_manifest_path: Path = _RUN_MANIFEST_PATH,
    history_dir: Path = _HISTORY_DIR,
    repo_root: Path = _REPO_ROOT,
    profile_path: Path = DEFAULT_PROFILE_PATH,
    cap: int = _DEFAULT_CAP,
    smtp_config: SmtpConfig | None = None,
    smtp_client_factory=None,
    preview_path: Path = _DEFAULT_PREVIEW_PATH,
) -> CycleResult:
    now_iso = now.isoformat()
    selected_sources = _select_sources(only_source, sources)
    logger.info(
        "run_daily 开始：sources=%s dry_run=%s cap=%d", sorted(selected_sources), dry_run, cap
    )

    fetched, missing_sources, source_failure_lines = _fetch_all(selected_sources, now_iso=now_iso)

    if missing_sources and len(missing_sources) == len(selected_sources):
        # 全部 source 失败：铁律3 + 6a 指令要求当次不写任何 seen 状态，直接退出。
        logger.error("全部 source 失败（%s），本次运行不写任何状态", ", ".join(missing_sources))
        _write_step_summary(
            f"Daily {now:%Y-%m-%d} — 全部来源抓取失败",
            [f"失败来源：{', '.join(missing_sources)}", "本次运行未写入任何状态"],
        )
        return CycleResult(
            exit_code=1,
            outcome=None,
            new_count=0,
            sent_count=0,
            backlog_count=pending_count(load_seen_jobs(seen_jobs_path)),
            missing_sources=missing_sources,
        )

    store = load_seen_jobs(seen_jobs_path)
    backlog_before = pending_count(store)

    total_fetched = len(fetched)
    filter_result = filter_closed(fetched)
    fetched = filter_result.kept
    filtered_closed_lines = build_filtered_closed_events(
        filter_result.filtered_counts, now_iso=now_iso
    )
    filtered_total = sum(filter_result.filtered_counts.values())
    logger.info(
        "抓取合计 %d 条；过滤已关闭 %d 条；剩余 %d 条进入去重",
        total_fetched,
        filtered_total,
        len(fetched),
    )

    merge_result = merge_duplicates(fetched, now=now_iso)
    manifest_events: list[MergeEvent] = list(merge_result.events)

    reconcile_result = reconcile(
        store,
        merge_result.winners,
        now=now_iso,
        merge_counts=merge_result.merge_counts,
        merged_ids=merge_result.merged_ids,
    )
    store = reconcile_result.store
    manifest_events.extend(reconcile_result.events)
    new_ids = reconcile_result.new_ids
    logger.info("去重后 %d 条，其中新增 %d 条", len(merge_result.winners), len(new_ids))

    profile = load_profile(profile_path)
    jobs_by_id = {job.canonical_id: job for job in merge_result.winners}
    scores = score_new_jobs([jobs_by_id[cid] for cid in new_ids], profile)
    store = apply_fit_scores(store, scores)
    logger.info("打分完成：%d 条新增已打分", len(scores))

    selected_ids = select_for_send(store, cap=cap)

    if not selected_ids:
        # 候选池为空（本次新增=0 且队列积压=0）：完全跳过 send()，但下面仍要
        # 走原子写 + commit，因为 reconcile 可能已经更新了 last_seen_at /
        # 写入了新记录（D-2：去重判据和选发判据是两个独立表达式）。
        # dry_run 仍然必须整体生效——"不写 data/" 不能因为候选池恰好为空
        # 就被绕过，所以这里照样按 dry_run 出 DRY_RUN，而不是无条件 SENT。
        outcome: SendOutcome | None = SendOutcome.DRY_RUN if dry_run else SendOutcome.SENT
        sent_count = 0
        backlog_count = pending_count(store)
        logger.info("候选池为空（新增=0 且积压=0），跳过发信；仍照常写入 last_seen_at 并 commit")
    else:
        entries_to_send = [store[cid] for cid in selected_ids]
        backlog_count = pending_count(store) - len(selected_ids)
        logger.info("选发完成：本次选中 %d 条，积压 %d 条", len(selected_ids), backlog_count)
        stats = EmailStats(
            new_count=len(new_ids),
            backlog_count=backlog_count,
            missing_sources=missing_sources,
            ai_ranking_unavailable=True,  # 6a：只有关键词降级路径接进来了
        )
        effective_smtp_config = smtp_config
        if effective_smtp_config is None:
            effective_smtp_config = (
                SmtpConfig.from_env()
                if not dry_run
                else SmtpConfig(host="", port=0, user="", mail_to="", app_password="")
            )
        outcome = emailer_send(
            entries_to_send,
            stats=stats,
            now=now,
            smtp_config=effective_smtp_config,
            dry_run=dry_run,
            preview_path=preview_path,
            smtp_client_factory=smtp_client_factory,
        )
        sent_count = len(selected_ids) if outcome is SendOutcome.SENT else 0
        if outcome is SendOutcome.DRY_RUN:
            logger.info(
                "dry-run：%d 条候选已渲染，预览写入 %s（未发送邮件、未写 data/）",
                len(selected_ids),
                preview_path,
            )
        elif outcome is SendOutcome.FAILED:
            logger.error("邮件发送失败：%d 条候选未发出，本次运行不写任何状态", len(selected_ids))
        else:
            logger.info("邮件发送成功：%d 条", sent_count)

    # 下面统一收口：不管 FAILED / DRY_RUN / SENT 哪条路径，都在这一处算出
    # exit_code / summary_title / summary_lines / final_backlog，只有一个
    # _write_step_summary 调用点和一个 return。这是特意的——之前 FAILED /
    # DRY_RUN 各自提前 return、各自手写一份 summary 行，DRY_RUN 那份漏掉了
    # 推送/积压两个数字（早退分支绕开了"完成"分支才有的统一构造），根因是
    # 分叉的构造代码，不是漏了某一行；收口成一处之后，以后再加一个字段
    # （比如这次的"过滤已关闭"）只需要改一个地方，三条路径同步生效。
    missing_line = f"缺失来源：{', '.join(missing_sources) or '无'}"

    if outcome is SendOutcome.FAILED:
        # 铁律2: 邮件失败 -> 这次运行一条状态都不写，哪怕只是新记录的 first_seen_at。
        exit_code = 1
        final_backlog = backlog_before
        summary_title = f"Daily {now:%Y-%m-%d} — 邮件发送失败"
        summary_lines = [
            f"抓取合计 {total_fetched} 条，过滤已关闭 {filtered_total} 条",
            f"本次新增 {len(new_ids)} 条",
            f"本次待发 {len(selected_ids)} 条（发送失败，未发出）",
            f"队列积压 {backlog_count} 条（未变化——本次运行未写入任何状态，铁律2）",
            missing_line,
        ]
    elif outcome is SendOutcome.DRY_RUN:
        exit_code = 0
        final_backlog = backlog_before
        preview_line = (
            f"预览已写入 {preview_path}（未发送、未写 data/）"
            if selected_ids
            else "候选池为空，没有渲染预览"
        )
        summary_title = f"Daily {now:%Y-%m-%d} — dry-run"
        summary_lines = [
            f"抓取合计 {total_fetched} 条，过滤已关闭 {filtered_total} 条",
            f"本次新增 {len(new_ids)} 条",
            f"本次将推送 {len(selected_ids)} 条",
            f"队列将积压 {backlog_count} 条",
            preview_line,
            missing_line,
        ]
    else:
        # outcome is SendOutcome.SENT (真正发出，或候选池为空的"无需发送"情形)
        if selected_ids:
            store = mark_sent(store, selected_ids, now=now_iso)

        save_seen_jobs_atomic(seen_jobs_path, store)
        append_run_manifest(run_manifest_path, manifest_events)
        _append_manifest_lines(run_manifest_path, source_failure_lines + filtered_closed_lines)
        _append_job_history(history_dir, store, selected_ids, now)
        commit_message = (
            f"chore(data): daily run {now:%Y-%m-%d} — "
            f"新增{len(new_ids)} 推送{sent_count} 积压{pending_count(store)}"
        )
        _git_commit(repo_root, [seen_jobs_path, run_manifest_path, history_dir], commit_message)
        logger.info(
            "run_daily 完成：新增 %d 推送 %d 积压 %d",
            len(new_ids),
            sent_count,
            pending_count(store),
        )

        exit_code = 0
        final_backlog = pending_count(store)
        summary_title = f"Daily {now:%Y-%m-%d} — 完成"
        summary_lines = [
            f"抓取合计 {total_fetched} 条，过滤已关闭 {filtered_total} 条",
            f"本次新增 {len(new_ids)} 条",
            f"本次推送 {sent_count} 条",
            f"队列积压 {final_backlog} 条",
            missing_line,
        ]

    _write_step_summary(summary_title, summary_lines)

    return CycleResult(
        exit_code=exit_code,
        outcome=outcome,
        new_count=len(new_ids),
        sent_count=sent_count,
        backlog_count=final_backlog,
        missing_sources=missing_sources,
    )


def run_bootstrap(
    *,
    now: datetime,
    dry_run: bool = False,
    only_source: str | None = None,
    sources: dict[str, SourceAdapter] | None = None,
    seen_jobs_path: Path = _SEEN_JOBS_PATH,
    run_manifest_path: Path = _RUN_MANIFEST_PATH,
    repo_root: Path = _REPO_ROOT,
) -> CycleResult:
    """D-2 bootstrap：抓取全部存量，整份写入 seen_jobs.json，`sent_at` 全部
    填 `"bootstrap"`，不打分、不发邮件（CLAUDE.md D-2）。

    只能在首次部署、`seen_jobs.json` 为空/不存在时跑：这是整份覆盖写，不是
    合并写，第二次误跑会把已经真实发送过的 `sent_at` 状态连同去重历史全部
    冲掉——不能靠"反正 bootstrap 幂等"这种假设放行，所以这里显式拒绝而不
    是静默跳过或合并。
    """
    now_iso = now.isoformat()
    logger.info("run_bootstrap 开始：dry_run=%s only_source=%s", dry_run, only_source)
    existing = load_seen_jobs(seen_jobs_path)
    if existing:
        logger.error(
            "拒绝 bootstrap：%s 已有 %d 条记录，bootstrap 只能在首次部署、"
            "seen_jobs.json 为空时执行一次",
            seen_jobs_path,
            len(existing),
        )
        _write_step_summary(
            f"Bootstrap {now:%Y-%m-%d} — 拒绝执行",
            [f"`{seen_jobs_path}` 已有 {len(existing)} 条记录，未做任何改动"],
        )
        return CycleResult(exit_code=1, outcome=None, new_count=0, sent_count=0, backlog_count=0)

    selected_sources = _select_sources(only_source, sources)
    fetched, missing_sources, source_failure_lines = _fetch_all(selected_sources, now_iso=now_iso)

    if missing_sources and len(missing_sources) == len(selected_sources):
        # 全部 source 失败：跟 run_daily 一致，当次不写任何状态，直接退出。
        logger.error(
            "全部 source 失败（%s），本次 bootstrap 不写任何状态", ", ".join(missing_sources)
        )
        _write_step_summary(
            f"Bootstrap {now:%Y-%m-%d} — 全部来源抓取失败",
            [f"失败来源：{', '.join(missing_sources)}", "本次运行未写入任何状态"],
        )
        return CycleResult(
            exit_code=1,
            outcome=None,
            new_count=0,
            sent_count=0,
            backlog_count=0,
            missing_sources=missing_sources,
        )

    total_fetched = len(fetched)
    filter_result = filter_closed(fetched)
    fetched = filter_result.kept
    filtered_closed_lines = build_filtered_closed_events(
        filter_result.filtered_counts, now_iso=now_iso
    )
    filtered_total = sum(filter_result.filtered_counts.values())
    logger.info(
        "抓取合计 %d 条；过滤已关闭 %d 条；剩余 %d 条写入 bootstrap",
        total_fetched,
        filtered_total,
        len(fetched),
    )

    store, events = _bootstrap_store(fetched, now=now_iso)
    logger.info("bootstrap 去重完成：写入 %d 条（sent_at=bootstrap，不打分不发信）", len(store))

    # 跟 run_daily 同样的收口：dry-run / 真正写入两条路径共用同一个
    # _write_step_summary 调用，只有"是否真的落盘+commit"和标题/状态行不同，
    # 避免两份摘要各写各的、其中一份漏字段还没人发现。
    if dry_run:
        # --dry-run 契约：不碰 data/ 下任何文件，不 commit。
        logger.info("dry-run：不写 data/、不 commit")
        outcome: SendOutcome | None = SendOutcome.DRY_RUN
        summary_title = f"Bootstrap {now:%Y-%m-%d} — dry-run"
        state_line = f"存量共 {len(store)} 条（未写入，dry-run）"
    else:
        save_seen_jobs_atomic(seen_jobs_path, store)
        append_run_manifest(run_manifest_path, events)
        _append_manifest_lines(run_manifest_path, source_failure_lines + filtered_closed_lines)
        commit_message = f"chore(data): bootstrap {now:%Y-%m-%d} — 存量 {len(store)} 条"
        _git_commit(repo_root, [seen_jobs_path, run_manifest_path], commit_message)
        logger.info("run_bootstrap 完成：已写入并 commit（%s）", commit_message)
        outcome = None
        summary_title = f"Bootstrap {now:%Y-%m-%d} — 完成"
        state_line = f"存量共 {len(store)} 条，sent_at=bootstrap，不发邮件"

    _write_step_summary(
        summary_title,
        [
            f"抓取合计 {total_fetched} 条，过滤已关闭 {filtered_total} 条",
            state_line,
            f"缺失来源：{', '.join(missing_sources) or '无'}",
        ],
    )

    return CycleResult(
        exit_code=0,
        outcome=outcome,
        new_count=len(store),
        sent_count=0,
        backlog_count=0,
        missing_sources=missing_sources,
    )


_FORCE_EMAIL_CANONICAL_ID = "__force_email_check__"


def run_force_email(
    *,
    now: datetime,
    dry_run: bool = False,
    seen_jobs_path: Path = _SEEN_JOBS_PATH,
    smtp_config: SmtpConfig | None = None,
    smtp_client_factory=None,
    preview_path: Path = _DEFAULT_PREVIEW_PATH,
) -> CycleResult:
    """发信链路验证：不抓取、不去重、不打分、不写任何状态——只借用
    emailer.py 真实的 send() 路径发一封确认邮件。

    背景：daily.py 部署以来只成功跑过一次 bootstrap（待发池当次全部标
    `sent_at="bootstrap"`），日常 cron 还没有在待发池非空的情况下触发过
    send()——SMTP 凭据、GitHub Secrets 注入、SendOutcome 判定这条链路
    一次都没有被真实验证过。

    emailer.send() 对空 entries 直接短路返回 SENT、完全不联系 SMTP
    （见 emailer.py 模块注释：D-2 不发全零邮件），所以要真正验证这条链路
    必须传至少一条 entry；这里构造的是一个明确标注"非真实职位"的合成
    占位记录，只存在于这次调用的内存里，从不写入 seen_jobs.json /
    job_history.jsonl、不参与去重/排序——不与铁律1"不许编造职位事实"
    冲突，因为它从未被当成职位数据持久化或展示为真实抓取结果。

    `backlog_count` 读的是当前真实 seen_jobs.json 的积压数（只读一次，
    从不写回），让邮件正文如实反映"验证时刻真实的待发池状态"，而不是
    硬编码一个 0。
    """
    now_iso = now.isoformat()
    store = load_seen_jobs(seen_jobs_path)
    real_backlog = pending_count(store)
    logger.info("run_force_email 开始：dry_run=%s 当前真实积压=%d", dry_run, real_backlog)

    placeholder = SeenJobEntry(
        canonical_id=_FORCE_EMAIL_CANONICAL_ID,
        canonical_id_tier="hash_fallback",
        source_repo="__force_email_check__",
        company="[链路验证 — 非真实职位]",
        role="由 --force-email 生成，仅用于确认 SMTP 发信链路可用",
        location="—",
        date_posted_raw="—",
        fallback_key=None,
        merged_ids=[],
        merged_row_count=1,
        fit_score=None,
        first_seen_at=now_iso,
        last_seen_at=now_iso,
        sent_at=None,
        application_url=None,
        is_closed=False,
    )

    stats = EmailStats(
        new_count=0,
        backlog_count=real_backlog,
        missing_sources=[],
        force_email_check=True,
    )

    effective_smtp_config = smtp_config
    if effective_smtp_config is None:
        effective_smtp_config = (
            SmtpConfig.from_env()
            if not dry_run
            else SmtpConfig(host="", port=0, user="", mail_to="", app_password="")
        )

    outcome = emailer_send(
        [placeholder],
        stats=stats,
        now=now,
        smtp_config=effective_smtp_config,
        dry_run=dry_run,
        preview_path=preview_path,
        smtp_client_factory=smtp_client_factory,
    )

    if outcome is SendOutcome.FAILED:
        logger.error("--force-email：发信链路验证失败（SendOutcome.FAILED），未写入任何状态")
        exit_code = 1
        summary_title = f"Force-email {now:%Y-%m-%d} — 链路验证失败"
        summary_lines = [
            f"真实待发积压 {real_backlog} 条（本次未受影响）",
            "SMTP 发送失败，见运行日志",
        ]
    elif outcome is SendOutcome.DRY_RUN:
        logger.info("--force-email --dry-run：预览已写入 %s，未联系 SMTP", preview_path)
        exit_code = 0
        summary_title = f"Force-email {now:%Y-%m-%d} — dry-run"
        summary_lines = [
            f"真实待发积压 {real_backlog} 条",
            f"预览已写入 {preview_path}（未联系 SMTP）",
        ]
    else:
        logger.info("--force-email：链路验证邮件已发出")
        exit_code = 0
        summary_title = f"Force-email {now:%Y-%m-%d} — 完成"
        summary_lines = [
            f"真实待发积压 {real_backlog} 条（本次未受影响）",
            "验证邮件已发出，未写入任何状态",
        ]

    _write_step_summary(summary_title, summary_lines)

    return CycleResult(
        exit_code=exit_code,
        outcome=outcome,
        new_count=0,
        sent_count=1 if outcome is SendOutcome.SENT else 0,
        backlog_count=real_backlog,
    )


def _format_matched_keywords(explanation: ScoreExplanation) -> str:
    if not explanation.matched_keywords:
        return "—"
    return ", ".join(f"{kw.term}({kw.weight:+d})" for kw in explanation.matched_keywords)


def _print_score_table(explanations: list[ScoreExplanation], *, full: bool = False) -> None:
    if full:
        # --full: 不截断，列宽按最长的实际内容撑开，用来核对关键词到底命中
        # 在 role 的哪个位置（截断后看不到词在哪，只能去 fixture 里 grep）。
        company_width = max((len(e.company) for e in explanations), default=7)
        role_width = max((len(e.role) for e in explanations), default=4)
    else:
        company_width = max(7, min(24, max((len(e.company) for e in explanations), default=7)))
        role_width = max(4, min(40, max((len(e.role) for e in explanations), default=4)))

    def _truncate(text: str, width: int) -> str:
        if full or len(text) <= width:
            return text
        return text[: width - 1] + "…"

    header = f"{'Score':>5}  {'Company':<{company_width}}  {'Role':<{role_width}}  Matched keywords"
    print(header)
    print("-" * len(header))
    for e in explanations:
        company = _truncate(e.company, company_width)
        role = _truncate(e.role, role_width)
        print(
            f"{e.score:>5}  {company:<{company_width}}  {role:<{role_width}}  "
            f"{_format_matched_keywords(e)}"
        )


def explain_scores(
    *,
    only_source: str | None = None,
    sources: dict[str, SourceAdapter] | None = None,
    profile_path: Path = DEFAULT_PROFILE_PATH,
    full: bool = False,
) -> int:
    """--explain-scores: 纯打分调试工具，不接触 seen_jobs、不发信、不写
    data/、不 commit——跟 run_daily 完全独立的一条路径，改 profile.yaml
    之后跑一遍就能立刻看出：哪些关键词从没命中过（权重白设）、哪些命中太
    广（比如 "systems" 可能匹配一半岗位）、高分岗位是不是真的想要的。

    对本次抓到的全部岗位打分（不只是 new_ids），先跑一遍 merge_duplicates
    去掉同一快照内的重复行（不含跨 run 的 seen_jobs 比对——纯本地快照去重），
    避免像 CLAUDE.md 常见陷阱11 里 Kudu Dynamics 那种重复行把某个关键词的
    命中次数印象放大三倍。
    """
    now_iso = datetime.now(UTC).isoformat()
    selected_sources = _select_sources(only_source, sources)

    fetched, missing_sources, _ = _fetch_all(selected_sources, now_iso=now_iso)
    if missing_sources:
        print(f"警告：以下来源抓取失败，结果不完整：{', '.join(missing_sources)}", file=sys.stderr)
    if not fetched:
        print("没有抓到任何岗位。")
        return 1 if missing_sources else 0

    fetched = filter_closed(fetched).kept  # 跟 run_daily 一致：不看已关闭岗位的分数
    if not fetched:
        print("没有抓到任何岗位（全部已关闭）。")
        return 0

    winners = merge_duplicates(fetched, now=now_iso).winners
    profile = load_profile(profile_path)
    explanations = sorted(
        (explain_job(job, profile) for job in winners), key=lambda e: e.score, reverse=True
    )
    _print_score_table(explanations, full=full)
    return 0


def main(argv: list[str] | None = None) -> int:
    # 铁律4：格式里只有时间戳/级别/消息，不打印任何 env/config 快照——SMTP_*
    # 等密钥从来不经过这里（logger 调用点里没有一处引用过它们），日志天然
    # 不含密钥。basicConfig 默认输出到 stderr，跟 --explain-scores 的
    # print()（stdout）不冲突，两条通道分别可独立重定向/断言。
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(prog="python -m job_radar.daily")
    parser.add_argument(
        "--dry-run", action="store_true", help="不发邮件、不写 data/、渲染到 /tmp/preview.html"
    )
    parser.add_argument("--source", choices=sorted(_SOURCES), default=None, help="只跑一个 source")
    parser.add_argument(
        "--explain-scores",
        action="store_true",
        help="只打分不发信不写 data/，把每个岗位的分数和命中的关键词打到 stdout（调 profile 用）",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="配合 --explain-scores：不截断 company/role，方便核对关键词命中在原文哪里",
    )
    parser.add_argument(
        "--bootstrap",
        action="store_true",
        help="D-2 首次部署：抓取全部存量写入 seen_jobs.json（sent_at=bootstrap），不发邮件、"
        "不打分。只能在 seen_jobs.json 为空时跑一次",
    )
    parser.add_argument(
        "--force-email",
        action="store_true",
        help="发信链路验证：不看待发池，强制走一次真实 emailer.send() 发一封确认邮件。"
        "不写 data/、不 commit、不改任何 sent_at——只用来验证 SMTP 凭据/Secrets 注入是否正常。"
        "配合 --dry-run 只渲染预览、不联系 SMTP",
    )
    args = parser.parse_args(argv)

    if args.explain_scores:
        return explain_scores(only_source=args.source, full=args.full)

    if args.force_email:
        result = run_force_email(now=datetime.now(UTC), dry_run=args.dry_run)
        return result.exit_code

    if args.bootstrap:
        result = run_bootstrap(now=datetime.now(UTC), dry_run=args.dry_run, only_source=args.source)
        return result.exit_code

    result = run_daily(now=datetime.now(UTC), dry_run=args.dry_run, only_source=args.source)
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
