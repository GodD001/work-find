"""filters.py — 抓取解析之后、去重之前的过滤层（CLAUDE.md 常见陷阱5 附近）.

第一个职责：丢弃 `is_closed` 为 true 的记录。这和陷阱5「身份风险默认只
展示不淘汰」是两码事——身份风险是"现在不合适、条件变了可能合适"（比如
sponsorship 要求可能随公司政策变化），`is_closed` 是岗位已经死了，永远
不会重新可投，展示出来没有任何行动价值。海投模式下 cap=80（D-2）是稀缺
资源，不该浪费在一个点进去就是 404 的岗位上。

副作用：被过滤掉的记录不进 `merge_duplicates`/`reconcile`，也就不写入
`seen_jobs.json`——过滤发生在 reconcile 之前，reconcile 压根不知道这条
记录存在过。所以如果上游哪天把 🔒 撤掉、重新开放同一个岗位，下次抓取会
把它当成一条全新记录推送——这是设计意图，不是 bug：`is_closed` 在这个
系统里不是"记住我见过这个"的状态，只是"这一行要不要进入下游流程"的
一次性判断。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from job_radar.models import Job


@dataclass(frozen=True)
class FilterResult:
    kept: list[Job]
    filtered_counts: dict[str, int] = field(default_factory=dict)  # source_repo -> 被过滤条数


def filter_closed(jobs: list[Job]) -> FilterResult:
    kept: list[Job] = []
    filtered_counts: dict[str, int] = {}
    for job in jobs:
        if job.is_closed:
            filtered_counts[job.source_repo] = filtered_counts.get(job.source_repo, 0) + 1
        else:
            kept.append(job)
    return FilterResult(kept=kept, filtered_counts=filtered_counts)


def build_filtered_closed_events(filtered_counts: dict[str, int], *, now_iso: str) -> list[str]:
    """一次运行、一个 source_repo 一条汇总事件——只记数量，不逐条记
    （被过滤的记录本身已经不再需要被追溯，跟 D-3 dedup_merged 那种"合并
    是不可逆丢弃、必须能回查"的场景不同：is_closed 是来源自己明确标注的
    事实，不存在误判需要事后核对的问题）。"""
    return [
        json.dumps(
            {
                "event": "filtered_closed",
                "source_repo": source_repo,
                "count": count,
                "timestamp": now_iso,
            },
            ensure_ascii=False,
        )
        for source_repo, count in filtered_counts.items()
    ]
