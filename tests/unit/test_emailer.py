"""Unit tests for src/job_radar/emailer.py.

Written before the module exists. Pins down the interface contract from this
task:

- send() never raises; True means "safe to call deduplicate.mark_sent",
  False means "don't" — and that boundary is enforced *inside* emailer, not
  by trusting the caller (dry_run always returns False; SMTP exceptions are
  always caught).
- emailer never touches seen_jobs.json / mark_sent — it only renders/sends.
- Templates support two shapes per job: AI-ranked (fit_score + evidence +
  risks) and keyword-fallback (fit_score only). Degradation must show a
  visible banner, not a TODO.
- Renderer never invents facts: null application_url never falls back to a
  GitHub link, is_closed only reflects the source's own flag, location/
  date_posted_raw are rendered verbatim (CLAUDE.md 铁律1 extension).
- SMTP is always mocked via an injectable factory — nothing here opens a
  real socket.
"""

from __future__ import annotations

from datetime import datetime, timezone
from email.message import EmailMessage

import pytest

from job_radar.deduplicate import SeenJobEntry
from job_radar.emailer import (
    EmailStats,
    RankingDetail,
    RenderedEmail,
    SendOutcome,
    SmtpConfig,
    render,
    send,
)

NOW = datetime(2026, 7, 28, 9, 30, tzinfo=timezone.utc)


def make_entry(
    *,
    canonical_id: str = "job-1",
    company: str = "Acme",
    role: str = "SWE Intern",
    location: str = "Remote",
    date_posted_raw: str = "Jul 26",
    fit_score: int | None = 80,
    application_url: str | None = "https://acme.example/apply",
    is_closed: bool = False,
    sent_at: str | None = None,
) -> SeenJobEntry:
    return SeenJobEntry(
        canonical_id=canonical_id,
        canonical_id_tier="url" if application_url else "hash_fallback",
        source_repo="vanshb03/Summer2027-Internships",
        company=company,
        role=role,
        location=location,
        date_posted_raw=date_posted_raw,
        fallback_key=None,
        merged_ids=[],
        merged_row_count=1,
        fit_score=fit_score,
        first_seen_at="2026-07-28T01:30:00+00:00",
        last_seen_at="2026-07-28T01:30:00+00:00",
        sent_at=sent_at,
        application_url=application_url,
        is_closed=is_closed,
    )


class FakeSmtpClient:
    """Records what would have been sent; supports the `with factory(cfg) as
    client:` context-manager protocol send() actually uses."""

    def __init__(self, should_raise: Exception | None = None):
        self.should_raise = should_raise
        self.sent_messages: list[EmailMessage] = []
        self.entered = False
        self.exited = False

    def __call__(self, _config: SmtpConfig) -> FakeSmtpClient:
        return self

    def __enter__(self) -> FakeSmtpClient:
        self.entered = True
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.exited = True

    def send_message(self, msg: EmailMessage) -> None:
        if self.should_raise is not None:
            raise self.should_raise
        self.sent_messages.append(msg)


def make_smtp_config(**overrides) -> SmtpConfig:
    defaults = dict(
        host="smtp.gmail.com",
        port=587,
        user="me@gmail.com",
        app_password="super-secret-app-password",
        mail_to="me@gmail.com",
    )
    defaults.update(overrides)
    return SmtpConfig(**defaults)


# ---------------------------------------------------------------------------
# render(): fact constraints (CLAUDE.md 铁律1 extension)
# ---------------------------------------------------------------------------


def test_render_shows_no_application_link_text_for_null_url_never_a_github_fallback():
    entry = make_entry(application_url=None)
    stats = EmailStats(new_count=1, backlog_count=0)

    rendered = render([entry], stats=stats, now=NOW)

    assert "无申请链接" in rendered.html
    assert "无申请链接" in rendered.text
    assert "github.com" not in rendered.html.lower()
    assert "<a href" not in rendered.html


def test_render_shows_application_link_when_present():
    entry = make_entry(application_url="https://acme.example/apply")
    stats = EmailStats(new_count=1, backlog_count=0)

    rendered = render([entry], stats=stats, now=NOW)

    assert 'href="https://acme.example/apply"' in rendered.html
    assert "https://acme.example/apply" in rendered.text


def test_render_is_closed_badge_only_from_source_flag():
    closed = make_entry(canonical_id="closed-1", is_closed=True, application_url=None)
    open_job = make_entry(canonical_id="open-1", is_closed=False, application_url="https://x.example")
    stats = EmailStats(new_count=2, backlog_count=0)

    rendered = render([closed, open_job], stats=stats, now=NOW)

    assert "已关闭" in rendered.html
    # exactly one of the two rows may claim closed status
    assert rendered.html.count("已关闭") == 1


def test_render_location_with_embedded_html_renders_unescaped_in_html_and_verbatim_in_text():
    """S1 location can literally contain <details>/</br> markup (CLAUDE.md
    常见陷阱9) — must render as real HTML in the html body (not escaped to
    &lt;details&gt;), and appear byte-for-byte in the text body."""
    raw_location = "<details><summary>3 locations</summary>NYC</br>SF</br>Austin</details>"
    entry = make_entry(location=raw_location)
    stats = EmailStats(new_count=1, backlog_count=0)

    rendered = render([entry], stats=stats, now=NOW)

    assert "<details><summary>3 locations</summary>" in rendered.html
    assert "&lt;details&gt;" not in rendered.html
    assert raw_location in rendered.text


def test_render_date_posted_raw_is_verbatim_no_year_no_relative_conversion():
    entry = make_entry(date_posted_raw="Jul 26")
    stats = EmailStats(new_count=1, backlog_count=0)

    rendered = render([entry], stats=stats, now=NOW)

    assert "Jul 26" in rendered.html
    assert "Jul 26" in rendered.text
    assert "Jul 26, 2026" not in rendered.html  # no year silently appended
    assert "Jul 26 2026" not in rendered.html
    assert "天前" not in rendered.html
    assert "天前" not in rendered.text


def test_render_company_is_escaped_in_html_but_not_location():
    """Unlike location, company/role are arbitrary scraped text with no
    guarantee of being safe HTML — they must be escaped, not trusted."""
    entry = make_entry(company="<b>Evil</b> Corp")
    stats = EmailStats(new_count=1, backlog_count=0)

    rendered = render([entry], stats=stats, now=NOW)

    assert "<b>Evil</b>" not in rendered.html
    assert "&lt;b&gt;Evil&lt;/b&gt;" in rendered.html
    assert "<b>Evil</b> Corp" in rendered.text  # text has no escaping concept


# ---------------------------------------------------------------------------
# render(): dual input shape (AI-ranked vs keyword-fallback) + banner
# ---------------------------------------------------------------------------


def test_render_ai_ranked_job_shows_evidence_and_risks():
    entry = make_entry(canonical_id="ai-1", fit_score=91)
    detail = RankingDetail(
        matching_evidence=["3 years Python", "internship posted this week"],
        explicit_risks=["requires relocation"],
    )
    stats = EmailStats(new_count=1, backlog_count=0)

    rendered = render([entry], stats=stats, now=NOW, ranking_details={"ai-1": detail})

    assert "3 years Python" in rendered.html
    assert "requires relocation" in rendered.html
    assert "91" in rendered.html


def test_render_keyword_fallback_job_shows_only_score_no_evidence_fields():
    entry = make_entry(canonical_id="kw-1", fit_score=40)
    stats = EmailStats(new_count=1, backlog_count=0)

    rendered = render([entry], stats=stats, now=NOW, ranking_details=None)

    assert "40" in rendered.html
    assert "匹配依据" not in rendered.html
    assert "风险提示" not in rendered.html


def test_render_ai_unavailable_shows_prominent_banner():
    entry = make_entry()
    stats = EmailStats(new_count=1, backlog_count=0, ai_ranking_unavailable=True)

    rendered = render([entry], stats=stats, now=NOW)

    assert "AI 排序不可用" in rendered.html
    assert "AI 排序不可用" in rendered.text
    assert "AI 排序不可用" in rendered.subject


def test_render_no_banner_when_ai_ranking_available():
    entry = make_entry()
    stats = EmailStats(new_count=1, backlog_count=0, ai_ranking_unavailable=False)

    rendered = render([entry], stats=stats, now=NOW)

    assert "AI 排序不可用" not in rendered.html
    assert "AI 排序不可用" not in rendered.text


def test_render_mixed_batch_only_scored_jobs_show_evidence():
    ai_job = make_entry(canonical_id="ai-1")
    fallback_job = make_entry(canonical_id="kw-1")
    stats = EmailStats(new_count=2, backlog_count=0)
    detail = RankingDetail(matching_evidence=["strong match"], explicit_risks=[])

    rendered = render(
        [ai_job, fallback_job], stats=stats, now=NOW, ranking_details={"ai-1": detail}
    )

    assert rendered.html.count("匹配依据") == 1


# ---------------------------------------------------------------------------
# render(): header counts + missing sources
# ---------------------------------------------------------------------------


def test_render_header_shows_new_sent_backlog_counts():
    entries = [make_entry(canonical_id="a"), make_entry(canonical_id="b")]
    stats = EmailStats(new_count=2, backlog_count=5)

    rendered = render(entries, stats=stats, now=NOW)

    assert "本次新增 2" in rendered.html
    # sent_count is derived from len(entries), not a separate stats field
    assert "本次推送 2" in rendered.html
    assert "队列积压 5" in rendered.html
    assert "本次新增 2" in rendered.text
    assert "本次推送 2" in rendered.text
    assert "队列积压 5" in rendered.text


def test_render_missing_sources_listed():
    entry = make_entry()
    stats = EmailStats(new_count=1, backlog_count=0, missing_sources=["s2", "s4"])

    rendered = render([entry], stats=stats, now=NOW)

    assert "s2" in rendered.html
    assert "s4" in rendered.html
    assert "s2" in rendered.text


def test_render_no_missing_sources_section_when_empty():
    entry = make_entry()
    stats = EmailStats(new_count=1, backlog_count=0, missing_sources=[])

    rendered = render([entry], stats=stats, now=NOW)

    assert "抓取失败" not in rendered.html


# ---------------------------------------------------------------------------
# SmtpConfig
# ---------------------------------------------------------------------------


def test_smtp_config_from_env_reads_all_five_vars():
    env = {
        "SMTP_HOST": "smtp.gmail.com",
        "SMTP_PORT": "587",
        "SMTP_USER": "me@gmail.com",
        "SMTP_APP_PASSWORD": "secret",
        "MAIL_TO": "dest@example.com",
    }
    config = SmtpConfig.from_env(env)

    assert config.host == "smtp.gmail.com"
    assert config.port == 587
    assert config.user == "me@gmail.com"
    assert config.app_password == "secret"
    assert config.mail_to == "dest@example.com"


def test_smtp_config_from_env_raises_on_missing_var_naming_it():
    env = {
        "SMTP_HOST": "smtp.gmail.com",
        "SMTP_PORT": "587",
        "SMTP_USER": "me@gmail.com",
        # SMTP_APP_PASSWORD missing
        "MAIL_TO": "dest@example.com",
    }
    with pytest.raises(ValueError, match="SMTP_APP_PASSWORD"):
        SmtpConfig.from_env(env)


def test_smtp_config_repr_never_includes_app_password():
    config = make_smtp_config(app_password="super-secret-app-password")

    assert "super-secret-app-password" not in repr(config)
    assert "super-secret-app-password" not in str(config)


# ---------------------------------------------------------------------------
# send(): success / failure / no-raise
# ---------------------------------------------------------------------------


def test_send_success_calls_smtp_and_returns_sent():
    entry = make_entry()
    stats = EmailStats(new_count=1, backlog_count=0)
    client = FakeSmtpClient()

    result = send(
        [entry],
        stats=stats,
        now=NOW,
        smtp_config=make_smtp_config(),
        smtp_client_factory=client,
    )

    assert result is SendOutcome.SENT
    assert len(client.sent_messages) == 1
    assert client.entered and client.exited


def test_send_smtp_exception_returns_failed_never_raises():
    entry = make_entry()
    stats = EmailStats(new_count=1, backlog_count=0)
    client = FakeSmtpClient(should_raise=OSError("connection refused"))

    result = send(
        [entry],
        stats=stats,
        now=NOW,
        smtp_config=make_smtp_config(),
        smtp_client_factory=client,
    )

    assert result is SendOutcome.FAILED


def test_send_factory_construction_exception_returns_failed():
    def failing_factory(_config: SmtpConfig):
        raise ConnectionRefusedError("no route to host")

    entry = make_entry()
    stats = EmailStats(new_count=1, backlog_count=0)

    result = send(
        [entry], stats=stats, now=NOW, smtp_config=make_smtp_config(), smtp_client_factory=failing_factory
    )

    assert result is SendOutcome.FAILED


def test_send_app_password_never_appears_in_sent_message():
    entry = make_entry()
    stats = EmailStats(new_count=1, backlog_count=0)
    client = FakeSmtpClient()
    config = make_smtp_config(app_password="super-secret-app-password")

    send([entry], stats=stats, now=NOW, smtp_config=config, smtp_client_factory=client)

    sent = client.sent_messages[0]
    assert "super-secret-app-password" not in sent.as_string()


# ---------------------------------------------------------------------------
# send(): dry-run guarantee enforced inside emailer, not by caller discipline
# ---------------------------------------------------------------------------


def test_send_dry_run_writes_preview_and_never_calls_smtp(tmp_path):
    entry = make_entry(company="PreviewCo")
    stats = EmailStats(new_count=1, backlog_count=0)
    client = FakeSmtpClient()
    preview_path = tmp_path / "preview.html"

    result = send(
        [entry],
        stats=stats,
        now=NOW,
        smtp_config=make_smtp_config(),
        smtp_client_factory=client,
        dry_run=True,
        preview_path=preview_path,
    )

    assert result is SendOutcome.DRY_RUN
    assert client.sent_messages == []
    assert not client.entered
    assert preview_path.exists()
    assert "PreviewCo" in preview_path.read_text(encoding="utf-8")


def test_send_dry_run_outcome_is_distinguishable_from_sent_and_failed(tmp_path):
    """The whole point of the three-state return: DRY_RUN must never be
    confusable with SENT (so a caller can't accidentally mark_sent on a
    dry run) or with FAILED (so daily.py's exit code doesn't treat a
    successful preview as a delivery failure)."""
    entry = make_entry()
    stats = EmailStats(new_count=1, backlog_count=0)

    result = send(
        [entry],
        stats=stats,
        now=NOW,
        smtp_config=make_smtp_config(),
        smtp_client_factory=FakeSmtpClient(),
        dry_run=True,
        preview_path=tmp_path / "preview.html",
    )

    assert result is SendOutcome.DRY_RUN
    assert result is not SendOutcome.SENT
    assert result is not SendOutcome.FAILED


def test_send_outcome_has_no_truthiness():
    """Guards against the classic `if send(...):` idiom silently surviving
    the migration from bool: plain enum members are truthy in Python
    regardless of value, so without this override a leftover boolean check
    would keep "working" — always entering the branch, including for
    DRY_RUN and FAILED — which is worse than the ambiguity being fixed.
    bool() must raise immediately instead of returning a misleading value.
    """
    for outcome in SendOutcome:
        with pytest.raises(TypeError):
            bool(outcome)
        with pytest.raises(TypeError):
            _ = True if outcome else False


# ---------------------------------------------------------------------------
# send(): empty candidate pool is a hard no-op
# ---------------------------------------------------------------------------


def test_send_empty_entries_never_calls_smtp_returns_sent():
    """Empty is a degenerate case, not a real send — but it maps to SENT
    (not a fourth outcome) because calling mark_sent on an empty id list is
    a harmless no-op, and this path shouldn't happen in the real pipeline
    anyway (D-2: the caller is expected to skip calling send() at all when
    the candidate pool is empty). This guard is defense-in-depth only."""
    stats = EmailStats(new_count=0, backlog_count=0)
    client = FakeSmtpClient()

    result = send([], stats=stats, now=NOW, smtp_config=make_smtp_config(), smtp_client_factory=client)

    assert result is SendOutcome.SENT
    assert client.sent_messages == []
    assert not client.entered


def test_send_empty_entries_dry_run_never_writes_preview(tmp_path):
    stats = EmailStats(new_count=0, backlog_count=0)
    preview_path = tmp_path / "preview.html"

    result = send(
        [],
        stats=stats,
        now=NOW,
        smtp_config=make_smtp_config(),
        smtp_client_factory=FakeSmtpClient(),
        dry_run=True,
        preview_path=preview_path,
    )

    # the empty guard fires before the dry_run branch is even consulted
    assert result is SendOutcome.SENT
    assert not preview_path.exists()


# ---------------------------------------------------------------------------
# send(): never splits into multiple emails
# ---------------------------------------------------------------------------


def test_send_many_entries_produces_exactly_one_message():
    entries = [make_entry(canonical_id=f"job-{i}", company=f"Co{i}") for i in range(50)]
    stats = EmailStats(new_count=50, backlog_count=0)
    client = FakeSmtpClient()

    result = send(
        entries, stats=stats, now=NOW, smtp_config=make_smtp_config(), smtp_client_factory=client
    )

    assert result is SendOutcome.SENT
    assert len(client.sent_messages) == 1
    body = client.sent_messages[0].get_body(preferencelist=("html",)).get_content()
    for i in range(50):
        assert f"Co{i}" in body


# ---------------------------------------------------------------------------
# send(): multipart/alternative structure (html + text)
# ---------------------------------------------------------------------------


def test_send_message_is_multipart_alternative_with_text_and_html():
    entry = make_entry(company="StructCo")
    stats = EmailStats(new_count=1, backlog_count=0)
    client = FakeSmtpClient()

    send([entry], stats=stats, now=NOW, smtp_config=make_smtp_config(), smtp_client_factory=client)

    msg = client.sent_messages[0]
    assert msg.is_multipart()
    assert msg.get_content_type() == "multipart/alternative"

    text_part = msg.get_body(preferencelist=("plain",))
    html_part = msg.get_body(preferencelist=("html",))
    assert text_part is not None
    assert html_part is not None
    assert "StructCo" in text_part.get_content()
    assert "StructCo" in html_part.get_content()


def test_send_sets_subject_from_and_to_headers():
    entry = make_entry()
    stats = EmailStats(new_count=1, backlog_count=0)
    client = FakeSmtpClient()
    config = make_smtp_config(user="sender@gmail.com", mail_to="dest@example.com")

    send([entry], stats=stats, now=NOW, smtp_config=config, smtp_client_factory=client)

    msg = client.sent_messages[0]
    assert msg["From"] == "sender@gmail.com"
    assert msg["To"] == "dest@example.com"
    assert msg["Subject"] is not None
    assert "2026-07-28" in str(msg["Subject"])


# ---------------------------------------------------------------------------
# RenderedEmail is a plain data holder — sanity check
# ---------------------------------------------------------------------------


def test_render_returns_rendered_email_with_all_three_fields():
    entry = make_entry()
    stats = EmailStats(new_count=1, backlog_count=0)

    rendered = render([entry], stats=stats, now=NOW)

    assert isinstance(rendered, RenderedEmail)
    assert rendered.subject
    assert rendered.html
    assert rendered.text
