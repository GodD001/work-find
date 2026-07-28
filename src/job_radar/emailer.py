"""Rendering + SMTP delivery for the daily digest email.

Boundaries (CLAUDE.md 目录结构 + this task's interface contract):
  - Never writes seen_jobs.json, never calls deduplicate.mark_sent — the
    caller (future daily.py) decides whether to persist state, based
    entirely on this module's SendOutcome return from send().
  - send() never raises. Every failure — SMTP error, network error, bad
    server response — is caught and turned into SendOutcome.FAILED.
  - --dry-run is enforced HERE, not by trusting callers to check a flag
    before calling mark_sent: dry_run is checked *inside* send(), before any
    SMTP contact, so the guarantee doesn't depend on daily.py correctly
    routing between two different entry points. It renders the email,
    writes the preview file, never touches SMTP, and always returns
    SendOutcome.DRY_RUN — a value that is neither SENT nor FAILED.
  - An empty candidate list is a no-op: no SMTP contact, no preview file,
    returns SendOutcome.SENT immediately (CLAUDE.md D-2: never send an
    all-zero email; mark_sent on an empty id list is a harmless no-op, so
    this degenerate case doesn't need its own outcome). In the real
    pipeline the caller is expected to skip calling send() at all when the
    pool is empty — this guard is defense-in-depth only.

Why SendOutcome instead of bool: an earlier version returned True/False,
but that makes False carry two unrelated meanings — "dry run, nothing was
supposed to happen" and "a real send actually failed" — which a caller
(daily.py, step 6) cannot tell apart to decide its exit code. SendOutcome
has three members (SENT/DRY_RUN/FAILED) and deliberately has no truthiness:
`bool(outcome)` raises TypeError rather than returning something misleading.
Plain enum members are truthy in Python regardless of value, so without
that override a leftover `if send(...):` from the bool era would keep
silently "working" — always entering the branch, including for DRY_RUN and
FAILED — which is worse than the ambiguity being fixed. The only sanctioned
check is `outcome is SendOutcome.SENT`.

EmailStats.backlog_count is the caller-computed post-send backlog (CLAUDE.md
D-2: `K = backlog_before + N - M`), not derived from new_count/len(entries)
here — this module has no visibility into backlog_before, only
deduplicate.select_for_send + mark_sent do.
"""

from __future__ import annotations

import smtplib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from email.message import EmailMessage
from enum import Enum
from pathlib import Path
from typing import Protocol

from jinja2 import Environment, FileSystemLoader

from job_radar.deduplicate import SeenJobEntry

_TEMPLATE_DIR = Path(__file__).resolve().parents[2] / "templates"
_DEFAULT_PREVIEW_PATH = Path("/tmp/preview.html")

_REQUIRED_ENV_VARS = ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_APP_PASSWORD", "MAIL_TO")


class SendOutcome(Enum):
    """See module docstring for why this isn't a bool."""

    SENT = "sent"
    DRY_RUN = "dry_run"
    FAILED = "failed"

    def __bool__(self) -> bool:
        raise TypeError(
            "SendOutcome has no truthiness — compare explicitly, e.g. "
            "`outcome is SendOutcome.SENT`. Call deduplicate.mark_sent() iff "
            "that comparison is true; DRY_RUN and FAILED must never persist "
            "state (CLAUDE.md 铁律2)."
        )


@dataclass(frozen=True)
class RankingDetail:
    """Per-job AI ranking explanation. Only present when the AI ranker
    actually scored this job this run — absent (not a key in the
    ranking_details dict passed to render()/send()) means keyword-fallback
    scoring, and the template shows fit_score alone with no evidence/risk
    lines. This is what makes the "two input shapes" requirement a template
    branch on presence, not two different templates."""

    matching_evidence: list[str]
    explicit_risks: list[str]


@dataclass(frozen=True)
class EmailStats:
    new_count: int
    backlog_count: int
    missing_sources: list[str] = field(default_factory=list)
    ai_ranking_unavailable: bool = False


@dataclass(frozen=True)
class SmtpConfig:
    host: str
    port: int
    user: str
    mail_to: str
    app_password: str = field(repr=False)

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> SmtpConfig:
        import os

        source = env if env is not None else os.environ
        missing = [name for name in _REQUIRED_ENV_VARS if not source.get(name)]
        if missing:
            raise ValueError(f"missing required SMTP env vars: {', '.join(missing)}")
        return cls(
            host=source["SMTP_HOST"],
            port=int(source["SMTP_PORT"]),
            user=source["SMTP_USER"],
            app_password=source["SMTP_APP_PASSWORD"],
            mail_to=source["MAIL_TO"],
        )


@dataclass(frozen=True)
class RenderedEmail:
    subject: str
    html: str
    text: str


class _SmtpConnection(Protocol):
    def __enter__(self) -> _SmtpConnection: ...
    def __exit__(self, *exc_info: object) -> object: ...
    def send_message(self, msg: EmailMessage) -> object: ...


def _default_smtp_client_factory(config: SmtpConfig) -> smtplib.SMTP:
    client = smtplib.SMTP(config.host, config.port, timeout=20)
    client.starttls()
    # smtplib.SMTPAuthenticationError carries the *server's* rejection text,
    # never echoes back the password used to authenticate — nothing here
    # constructs an error message containing config.app_password.
    client.login(config.user, config.app_password)
    return client


def _html_env() -> Environment:
    return Environment(
        loader=FileSystemLoader(_TEMPLATE_DIR),
        autoescape=True,
        trim_blocks=True,
        lstrip_blocks=True,
    )


def _text_env() -> Environment:
    return Environment(
        loader=FileSystemLoader(_TEMPLATE_DIR),
        autoescape=False,
        trim_blocks=True,
        lstrip_blocks=True,
    )


def _job_context(entry: SeenJobEntry, ranking_details: dict[str, RankingDetail]) -> dict:
    return {
        "canonical_id": entry.canonical_id,
        "company": entry.company,
        "role": entry.role,
        "location": entry.location,
        "date_posted_raw": entry.date_posted_raw,
        "application_url": entry.application_url,
        "is_closed": entry.is_closed,
        "fit_score": entry.fit_score,
        "ranking": ranking_details.get(entry.canonical_id),
    }


def _build_subject(*, now: datetime, stats: EmailStats, sent_count: int) -> str:
    prefix = "[AI 排序不可用] " if stats.ai_ranking_unavailable else ""
    return (
        f"{prefix}AI 职位雷达 {now:%Y-%m-%d} · "
        f"新增{stats.new_count} 推送{sent_count} 积压{stats.backlog_count}"
    )


def render(
    entries: list[SeenJobEntry],
    *,
    stats: EmailStats,
    now: datetime,
    ranking_details: dict[str, RankingDetail] | None = None,
) -> RenderedEmail:
    ranking_details = ranking_details or {}
    jobs = [_job_context(e, ranking_details) for e in entries]
    sent_count = len(entries)
    subject = _build_subject(now=now, stats=stats, sent_count=sent_count)

    context = {
        "jobs": jobs,
        "stats": stats,
        "sent_count": sent_count,
        "now": now,
        "subject": subject,
    }
    html = _html_env().get_template("daily.html.j2").render(**context)
    text = _text_env().get_template("daily.txt.j2").render(**context)
    return RenderedEmail(subject=subject, html=html, text=text)


def _build_message(rendered: RenderedEmail, smtp_config: SmtpConfig) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = rendered.subject
    msg["From"] = smtp_config.user
    msg["To"] = smtp_config.mail_to
    msg.set_content(rendered.text)
    msg.add_alternative(rendered.html, subtype="html")
    return msg


def send(
    entries: list[SeenJobEntry],
    *,
    stats: EmailStats,
    now: datetime,
    smtp_config: SmtpConfig,
    ranking_details: dict[str, RankingDetail] | None = None,
    dry_run: bool = False,
    preview_path: Path = _DEFAULT_PREVIEW_PATH,
    smtp_client_factory: Callable[[SmtpConfig], _SmtpConnection] | None = None,
) -> SendOutcome:
    if not entries:
        return SendOutcome.SENT

    rendered = render(entries, stats=stats, now=now, ranking_details=ranking_details)

    if dry_run:
        preview_path.parent.mkdir(parents=True, exist_ok=True)
        preview_path.write_text(rendered.html, encoding="utf-8")
        return SendOutcome.DRY_RUN

    msg = _build_message(rendered, smtp_config)
    factory = smtp_client_factory or _default_smtp_client_factory
    try:
        with factory(smtp_config) as client:
            client.send_message(msg)
    except Exception:
        # Deliberately broad: send() must never raise (CLAUDE.md 铁律2 relies
        # on this — the caller's "call mark_sent iff SENT" logic only holds
        # if every possible failure surfaces as FAILED, not an exception).
        # Nothing here logs or re-wraps the exception, so smtp_config.
        # app_password (never part of any exception smtplib raises anyway)
        # has no path into a log line or re-raised message.
        return SendOutcome.FAILED
    return SendOutcome.SENT
