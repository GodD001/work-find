"""每日运行编排 (CLAUDE.md 目录结构 步骤6a / 铁律2).

顺序不可调换：

    抓取 -> 去重 -> 打分 -> 选发 -> 发信 -> 原子写状态 -> commit

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

这一步只做本地编排到可运行；把它接进 GitHub Actions workflow
（daily.yml、workflow_dispatch --dry-run、push）留给步骤 6b。这里的 commit
只在本地仓库落一个提交，不 push——push 是有更广播达范围的操作，交给 CI
去做更合适。

打分目前只有 FR-011 关键词降级路径（ranker.py）接进来了——AI 排序
（分批调用 Claude、批失败重试）是 ranker.py 后续要补的部分，这一步之前，
每次运行的 EmailStats.ai_ranking_unavailable 恒为 True，邮件横幅照
CLAUDE.md 要求如实显示"AI 排序不可用"，这不是 bug，是当前实现阶段的
真实状态。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from job_radar.deduplicate import (
    MergeEvent,
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
from job_radar.emailer import EmailStats, SendOutcome, SmtpConfig
from job_radar.emailer import send as emailer_send
from job_radar.fetchers import vansh
from job_radar.models import Job
from job_radar.ranker import DEFAULT_PROFILE_PATH, load_profile, score_new_jobs

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DATA_DIR = _REPO_ROOT / "data"
_SEEN_JOBS_PATH = _DATA_DIR / "seen_jobs.json"
_RUN_MANIFEST_PATH = _DATA_DIR / "run_manifest.jsonl"
_HISTORY_DIR = _DATA_DIR / "history"
_DEFAULT_PREVIEW_PATH = Path("/tmp/preview.html")
_DEFAULT_CAP = 80


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
        try:
            raw = adapter.fetch()
            records = adapter.validate(adapter.parse(raw))
        except Exception as exc:  # noqa: BLE001 - deliberately broad, see docstring
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
    available = sources if sources is not None else _SOURCES
    if only_source is not None:
        if only_source not in available:
            raise ValueError(f"unknown source: {only_source!r} (known: {sorted(available)})")
        selected_sources = {only_source: available[only_source]}
    else:
        selected_sources = dict(available)

    fetched, missing_sources, source_failure_lines = _fetch_all(selected_sources, now_iso=now_iso)

    if missing_sources and len(missing_sources) == len(selected_sources):
        # 全部 source 失败：铁律3 + 6a 指令要求当次不写任何 seen 状态，直接退出。
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

    profile = load_profile(profile_path)
    jobs_by_id = {job.canonical_id: job for job in merge_result.winners}
    scores = score_new_jobs([jobs_by_id[cid] for cid in new_ids], profile)
    store = apply_fit_scores(store, scores)

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
    else:
        entries_to_send = [store[cid] for cid in selected_ids]
        backlog_count = pending_count(store) - len(selected_ids)
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

    if outcome is SendOutcome.FAILED:
        # 铁律2: 邮件失败 -> 这次运行一条状态都不写，哪怕只是新记录的 first_seen_at。
        return CycleResult(
            exit_code=1,
            outcome=outcome,
            new_count=len(new_ids),
            sent_count=0,
            backlog_count=backlog_before,
            missing_sources=missing_sources,
        )

    if outcome is SendOutcome.DRY_RUN:
        return CycleResult(
            exit_code=0,
            outcome=outcome,
            new_count=len(new_ids),
            sent_count=0,
            backlog_count=backlog_before,
            missing_sources=missing_sources,
        )

    # outcome is SendOutcome.SENT (真正发出，或候选池为空的"无需发送"情形)
    if selected_ids:
        store = mark_sent(store, selected_ids, now=now_iso)

    save_seen_jobs_atomic(seen_jobs_path, store)
    append_run_manifest(run_manifest_path, manifest_events)
    _append_manifest_lines(run_manifest_path, source_failure_lines)
    _append_job_history(history_dir, store, selected_ids, now)
    commit_message = (
        f"chore(data): daily run {now:%Y-%m-%d} — "
        f"新增{len(new_ids)} 推送{sent_count} 积压{pending_count(store)}"
    )
    _git_commit(repo_root, [seen_jobs_path, run_manifest_path, history_dir], commit_message)

    return CycleResult(
        exit_code=0,
        outcome=outcome,
        new_count=len(new_ids),
        sent_count=sent_count,
        backlog_count=pending_count(store),
        missing_sources=missing_sources,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m job_radar.daily")
    parser.add_argument(
        "--dry-run", action="store_true", help="不发邮件、不写 data/、渲染到 /tmp/preview.html"
    )
    parser.add_argument("--source", choices=sorted(_SOURCES), default=None, help="只跑一个 source")
    args = parser.parse_args(argv)

    result = run_daily(now=datetime.now(UTC), dry_run=args.dry_run, only_source=args.source)
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
