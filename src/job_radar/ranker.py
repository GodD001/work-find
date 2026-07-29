"""FR-011 确定性关键词打分器 — AI 排序不可用时的降级路径.

CLAUDE.md AI 调用约束：「降级路径：确定性关键词打分，照常发邮件，邮件中
标注「AI 排序不可用」」。这是常驻路径，不是临时脚手架——关键词和权重全部
来自 config/profile.yaml，不硬编码在这里（CLAUDE.md 目录结构「约束」：
阈值、关键词一律走 config）。

输出形状故意和 AI 排序保持一致：只填 fit_score，不产生
matching_evidence / explicit_risks。emailer.py 靠某个 canonical_id 在
ranking_details 里有没有对应的 RankingDetail 来决定要不要展示评分依据
（见 emailer.py RankingDetail 的文档）——这个模块永远不产出 RankingDetail，
所以「AI 排序不可用」横幅是模板对 stats.ai_ranking_unavailable 的自然反应，
不需要这里额外做任何"降级开关"。

只给调用方传入的这一批打分，不读 seen_jobs、不重新给积压项打分：
docs/open-questions.md OQ-1 定的是"fit_score 首次写入后冻结"，这条规则不
区分打分器是 AI 还是关键词——daily.py 只会把 reconcile 产出的 new_ids 对应
的 Job 传进来。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from job_radar.models import Job

DEFAULT_PROFILE_PATH = Path(__file__).resolve().parents[2] / "config" / "profile.yaml"


@dataclass(frozen=True)
class Keyword:
    term: str
    weight: int


@dataclass(frozen=True)
class Profile:
    base_score: int
    min_score: int
    max_score: int
    keywords: list[Keyword] = field(default_factory=list)


def load_profile(path: Path = DEFAULT_PROFILE_PATH) -> Profile:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    scoring = raw["scoring"]
    keywords = [Keyword(term=kw["term"], weight=kw["weight"]) for kw in scoring.get("keywords", [])]
    return Profile(
        base_score=scoring["base_score"],
        min_score=scoring.get("min_score", 0),
        max_score=scoring.get("max_score", 100),
        keywords=keywords,
    )


def _matched_keywords(job: Job, profile: Profile) -> list[Keyword]:
    """role + company 原文子串匹配，大小写不敏感。不读 location /
    date_posted_raw——那两个字段是 CLAUDE.md 常见陷阱9 里明确要求原样保留、
    不做任何解析的字段，不适合拿来做子串匹配这种"理解"它们内容的操作。
    """
    haystack = f"{job.role} {job.company}".lower()
    return [kw for kw in profile.keywords if kw.term.lower() in haystack]


def _clip(raw_score: int, profile: Profile) -> int:
    return max(profile.min_score, min(profile.max_score, raw_score))


def score_job(job: Job, profile: Profile) -> int:
    raw_score = profile.base_score + sum(kw.weight for kw in _matched_keywords(job, profile))
    return _clip(raw_score, profile)


def score_new_jobs(jobs: list[Job], profile: Profile) -> dict[str, int]:
    return {job.canonical_id: score_job(job, profile) for job in jobs}


@dataclass(frozen=True)
class ScoreExplanation:
    """--explain-scores 用：一个岗位的最终分数 + 具体命中了哪些关键词、
    各自贡献了多少权重——用来看出 profile.yaml 里哪些关键词从没命中过
    （权重白设）、哪些命中太广（比如 "systems" 可能匹配一半岗位）。"""

    canonical_id: str
    company: str
    role: str
    score: int
    matched_keywords: list[Keyword]


def explain_job(job: Job, profile: Profile) -> ScoreExplanation:
    matched = _matched_keywords(job, profile)
    raw_score = profile.base_score + sum(kw.weight for kw in matched)
    return ScoreExplanation(
        canonical_id=job.canonical_id,
        company=job.company,
        role=job.role,
        score=_clip(raw_score, profile),
        matched_keywords=matched,
    )
