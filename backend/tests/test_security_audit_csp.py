"""The content security policy in the security audit.

``GET /api/v1/security-audit`` says whether the policy is off,
report-only or enforced, what browsers reported against it over the last 7
days on every replica (the daily Redis counters of ``services/csp_reports.py``)
- the one question report-only mode exists to answer - and who may frame the
platform, which only an enforced policy restricts. Every finding
carries a literal id and names the control it reports on, and each
control is in the state its findings describe.
"""

from __future__ import annotations

import ast
import inspect
import logging
import time
from datetime import date

import pytest

import app.core.csp as csp
import app.services.csp_reports as csp_reports
import app.services.registered_platform_origins as registered_platform_origins
from app.api.v1 import security_audit
from app.core import security_controls
from app.core.registration_token import PUBLISHED_DEFAULT_REGISTRATION_TOKEN
from app.services.csp_reports import RecentViolations, count_violations
from tests.test_csp_reports import _FakeRedis, _violation

MODE_CONTROL = "web.content_security_policy"
FRAME_CONTROL = "web.csp_frame_ancestors"
QUIET_WEEK = RecentViolations(days=7, known=True)
CHOSEN_TOKEN = "an-operator-chosen-token"
NAMED = ["'self'", "https://portal.example.com", "https://*.intranet.example.com"]


async def _audit(
    monkeypatch,
    *,
    mode: str = "report-only",
    ancestors: tuple[str, ...] | list[str] = (),
    token: str = "",
    recent: RecentViolations = QUIET_WEEK,
    reads: list[int] | None = None,
) -> list[security_audit.AuditFinding]:
    monkeypatch.setattr(security_audit.settings, "content_security_policy_mode", mode)
    monkeypatch.setattr(security_audit.settings, "content_security_policy_frame_ancestors", list(ancestors))
    monkeypatch.setattr(security_audit.settings, "platform_registration_token", token)

    async def _recent(days: int = csp_reports.RECENT_VIOLATION_DAYS) -> RecentViolations:
        if reads is not None:
            reads.append(days)
        return recent

    monkeypatch.setattr(security_audit, "recent_violations", _recent)
    findings: list[security_audit.AuditFinding] = []
    await security_audit._check_content_security_policy(findings)
    return findings


def _shape(finding: security_audit.AuditFinding) -> tuple[str, str, str, str]:
    return finding.id, finding.control, finding.category, finding.severity


def _mode(findings: list[security_audit.AuditFinding]) -> security_audit.AuditFinding:
    [finding] = [f for f in findings if f.control == MODE_CONTROL]
    return finding


def _framing(findings: list[security_audit.AuditFinding]) -> security_audit.AuditFinding:
    [finding] = [f for f in findings if f.control == FRAME_CONTROL]
    return finding


# -- the mode ----------------------------------------------------------------


async def test_off_is_a_warning_that_reads_no_counter(monkeypatch):
    reads: list[int] = []
    # No frame-ancestors finding: the policy that would carry the directive is not sent at all.
    findings = await _audit(monkeypatch, mode="off", ancestors=["*"], reads=reads)
    assert [_shape(f) for f in findings] == [("web.csp_off", MODE_CONTROL, "web", "warning")]
    [finding] = findings
    assert "sends no Content-Security-Policy" in finding.description
    assert (
        "Framing is not restricted either: with no frame-ancestors directive, and no X-Frame-Options (which the "
        "platform never sends), any site may show its pages in an iframe."
    ) in finding.description
    assert "CONTENT_SECURITY_POLICY_MODE=report-only" in finding.recommendation
    assert "for a week" in finding.recommendation
    assert reads == []


async def test_report_only_names_the_violations_of_the_last_seven_days(monkeypatch):
    reads: list[int] = []
    recent = RecentViolations(
        days=7, known=True, by_directive={"img-src": 30, "connect-src": 10, "script-src-elem": 2}, extensions=5
    )
    finding = _mode(await _audit(monkeypatch, recent=recent, reads=reads))
    assert _shape(finding) == ("web.csp_report_only", MODE_CONTROL, "web", "info")
    assert reads == [7]
    assert (
        "In the last 7 days, across every replica, browsers reported 42 violations: img-src 30, "
        "connect-src 10, script-src-elem 2 (and 5 caused by browser extensions, which are not the platform's). "
        "Enforcing the policy would block each of them."
    ) in finding.description
    assert "CONTENT_SECURITY_POLICY_MODE=enforce" in finding.recommendation
    assert "only violations left are understood" in finding.recommendation


async def test_one_violation_is_singular(monkeypatch):
    recent = RecentViolations(days=7, known=True, by_directive={"font-src": 1})
    finding = _mode(await _audit(monkeypatch, recent=recent))
    assert "browsers reported 1 violation: font-src 1." in finding.description


async def test_report_only_without_a_violation_says_enforcing_would_have_blocked_nothing(monkeypatch):
    finding = _mode(await _audit(monkeypatch, recent=QUIET_WEEK))
    assert _shape(finding) == ("web.csp_report_only", MODE_CONTROL, "web", "info")
    assert (
        "In the last 7 days, across every replica, browsers reported no violation. Enforcing the policy "
        "would have blocked nothing the platform's pages loaded in that period."
    ) in finding.description


async def test_browser_extension_noise_alone_is_no_violation_of_the_platform(monkeypatch):
    finding = _mode(await _audit(monkeypatch, recent=RecentViolations(days=7, known=True, extensions=12)))
    assert (
        "browsers reported no violation (only 12 caused by browser extensions, which are not the platform's). "
        "Enforcing the policy would have blocked nothing"
    ) in finding.description


async def test_report_only_counts_that_cannot_be_read_are_unknown_never_zero(monkeypatch):
    finding = _mode(await _audit(monkeypatch, recent=RecentViolations(days=7, known=False)))
    assert _shape(finding) == ("web.csp_report_only", MODE_CONTROL, "web", "info")
    assert (
        "The violations of the last 7 days could not be read (Redis did not answer), so what enforcing the "
        "policy would block is unknown."
    ) in finding.description
    assert "no violation" not in finding.description
    assert "blocked nothing" not in finding.description
    assert "CONTENT_SECURITY_POLICY_MODE=enforce" in finding.recommendation


async def test_enforce_is_a_pass_that_still_names_the_violations(monkeypatch):
    recent = RecentViolations(days=7, known=True, by_directive={"img-src": 3}, extensions=2)
    finding = _mode(await _audit(monkeypatch, mode="enforce", recent=recent))
    assert _shape(finding) == ("web.csp_enforced", MODE_CONTROL, "web", "pass")
    assert (
        "browsers reported 3 violations: img-src 3 (and 2 caused by browser extensions, which are not the "
        "platform's). Each is a load the browser blocked, which users may have hit"
    ) in finding.description
    assert "CSP violation first seen" in finding.recommendation


async def test_enforce_without_a_violation_is_a_plain_pass(monkeypatch):
    finding = _mode(await _audit(monkeypatch, mode="enforce", recent=QUIET_WEEK))
    assert _shape(finding) == ("web.csp_enforced", MODE_CONTROL, "web", "pass")
    assert finding.description.endswith("In the last 7 days, across every replica, browsers reported no violation.")
    assert finding.recommendation is None


async def test_enforce_counts_that_cannot_be_read_are_said_so(monkeypatch):
    finding = _mode(await _audit(monkeypatch, mode="enforce", recent=RecentViolations(days=7, known=False)))
    assert finding.description.endswith("The violations of the last 7 days could not be read (Redis did not answer).")
    assert finding.recommendation is None


# -- who may frame the platform ----------------------------------------------


@pytest.mark.parametrize("mode", ["report-only", "enforce"])
@pytest.mark.parametrize(
    ("ancestors", "named"),
    [
        (["*"], "*"),
        (["https:"], "https:"),
        (["https://*"], "https://*"),
        (["https://portal.example.com", "https:", "https://*:8443"], "https:, https://*:8443"),
    ],
)
async def test_frame_ancestors_that_admit_every_site_are_a_warning_naming_them(monkeypatch, mode, ancestors, named):
    findings = await _audit(monkeypatch, mode=mode, ancestors=ancestors)
    finding = _framing(findings)
    assert _shape(finding) == ("web.csp_frame_ancestors_any", FRAME_CONTROL, "web", "warning")
    assert f"CONTENT_SECURITY_POLICY_FRAME_ANCESTORS allows {named}:" in finding.description
    assert "portal.example.com" not in finding.description
    # Report-only: every site can frame today, and these entries would keep admitting every site once enforced.
    assert ("once the policy is enforced" in finding.description) is (mode == "report-only")
    assert ("Until then the policy is report-only and refuses no site at all." in finding.description) is (
        mode == "report-only"
    )
    assert "CONTENT_SECURITY_POLICY_FRAME_ANCESTORS" in finding.recommendation


# Framing is restricted only by an enforced policy: report-only blocks
# no frame and the platform sends no X-Frame-Options, so no mode but enforce
# may call framing restricted, and no setting of the variable is left unsaid.
@pytest.mark.parametrize(
    ("mode", "ancestors", "expected"),
    [
        ("off", [], None),
        ("off", ["'self'"], None),
        ("off", NAMED, None),
        ("off", ["*"], None),
        ("report-only", [], ("web.csp_frame_ancestors_not_enforced", "info")),
        ("report-only", ["'self'"], ("web.csp_frame_ancestors_not_enforced", "info")),
        ("report-only", NAMED, ("web.csp_frame_ancestors_not_enforced", "info")),
        ("report-only", ["*"], ("web.csp_frame_ancestors_any", "warning")),
        ("enforce", [], ("web.csp_frame_ancestors_restricted", "pass")),
        ("enforce", ["'self'"], ("web.csp_frame_ancestors_restricted", "pass")),
        ("enforce", NAMED, ("web.csp_frame_ancestors_restricted", "pass")),
        ("enforce", ["*"], ("web.csp_frame_ancestors_any", "warning")),
    ],
)
@pytest.mark.parametrize("token", ["", CHOSEN_TOKEN])
async def test_each_mode_and_frame_ancestors_setting_has_its_framing_finding(
    monkeypatch, mode, ancestors, expected, token
):
    findings = await _audit(monkeypatch, mode=mode, ancestors=ancestors, token=token)
    framing = [(f.id, f.severity) for f in findings if f.control == FRAME_CONTROL]
    assert framing == ([expected] if expected else [])


async def test_report_only_says_any_site_can_frame_until_enforced_and_who_may_then(monkeypatch):
    finding = _framing(await _audit(monkeypatch, mode="report-only", ancestors=NAMED))
    assert finding.title == "Any site can frame the platform until the policy is enforced"
    assert finding.description == (
        "The policy is report-only and the platform sends no X-Frame-Options, so any site can show the platform's "
        "pages in an iframe: browsers block no framing until the policy is enforced. Once it is, the web "
        "application may be framed only by the platform itself and https://portal.example.com, "
        "https://*.intranet.example.com (CONTENT_SECURITY_POLICY_FRAME_ANCESTORS); the MCP OAuth consent page "
        "only by the platform itself. The platforms registered with it may not frame it while "
        "PLATFORM_REGISTRATION_TOKEN is unset. Until then, browsers report each page another site frames as a "
        'frame-ancestors violation, counted in the per-directive totals of the "Content security policy is '
        'report-only" finding.'
    )
    assert "CONTENT_SECURITY_POLICY_FRAME_ANCESTORS" in finding.recommendation


async def test_enforce_says_framing_is_restricted_and_to_whom(monkeypatch):
    finding = _framing(await _audit(monkeypatch, mode="enforce", ancestors=NAMED))
    assert finding.title == "Framing is restricted"
    assert finding.description == (
        "The policy is enforced and browsers refuse to show the platform in the iframe of a site it does not "
        "allow: the web application may be framed only by the platform itself and https://portal.example.com, "
        "https://*.intranet.example.com (CONTENT_SECURITY_POLICY_FRAME_ANCESTORS); the MCP OAuth consent page "
        "only by the platform itself. The platforms registered with it may not frame it while "
        "PLATFORM_REGISTRATION_TOKEN is unset. The API reference, the Teams tab and the Office add-in stay "
        "report-only in every mode and restrict no framing."
    )
    assert finding.recommendation is None


@pytest.mark.parametrize("mode", ["report-only", "enforce"])
@pytest.mark.parametrize("ancestors", [[], ["'self'"]])
async def test_with_no_embedder_named_only_the_platform_itself_may_frame(monkeypatch, mode, ancestors):
    finding = _framing(await _audit(monkeypatch, mode=mode, ancestors=ancestors))
    assert (
        "the web application and the MCP OAuth consent page may be framed only by the platform itself. The "
        "platforms registered with it may not frame it while PLATFORM_REGISTRATION_TOKEN is unset."
    ) in finding.description
    assert "CONTENT_SECURITY_POLICY_FRAME_ANCESTORS)" not in finding.description


@pytest.mark.parametrize("mode", ["report-only", "enforce"])
@pytest.mark.parametrize(
    ("token", "framers"),
    [
        (
            "",
            "the platform itself and https://portal.example.com (CONTENT_SECURITY_POLICY_FRAME_ANCESTORS); the MCP "
            "OAuth consent page only by the platform itself. The platforms registered with it may not frame it while "
            "PLATFORM_REGISTRATION_TOKEN is unset.",
        ),
        (
            PUBLISHED_DEFAULT_REGISTRATION_TOKEN,
            "the platform itself and https://portal.example.com (CONTENT_SECURITY_POLICY_FRAME_ANCESTORS); the MCP "
            "OAuth consent page only by the platform itself. The platforms registered with it may not frame it while "
            "PLATFORM_REGISTRATION_TOKEN is the published default, with which anyone can register.",
        ),
        (
            CHOSEN_TOKEN,
            "the platform itself, https://portal.example.com (CONTENT_SECURITY_POLICY_FRAME_ANCESTORS) and the "
            "platforms registered with it (OpenCTI, OpenAEV, OpenCRQ); the MCP OAuth consent page only by the "
            "platform itself.",
        ),
    ],
)
async def test_registered_platforms_are_named_only_behind_a_chosen_registration_token(monkeypatch, mode, token, framers):
    finding = _framing(await _audit(monkeypatch, mode=mode, ancestors=["https://portal.example.com"], token=token))
    assert f"the web application may be framed only by {framers}" in finding.description


async def test_with_a_chosen_token_and_no_embedder_the_platform_and_its_registered_platforms_may_frame(monkeypatch):
    finding = _framing(await _audit(monkeypatch, mode="enforce", token=CHOSEN_TOKEN))
    assert (
        "the web application may be framed only by the platform itself and the platforms registered with it "
        "(OpenCTI, OpenAEV, OpenCRQ); the MCP OAuth consent page only by the platform itself. The API reference"
    ) in finding.description


@pytest.mark.parametrize("mode", ["report-only", "enforce"])
@pytest.mark.parametrize("token", ["", PUBLISHED_DEFAULT_REGISTRATION_TOKEN, CHOSEN_TOKEN])
async def test_the_audit_names_the_framers_the_policy_header_carries(monkeypatch, mode, token):
    """What the finding says may frame is what the middleware writes into frame-ancestors."""
    monkeypatch.setattr(registered_platform_origins, "_origins", ("https://opencti.example.com",))
    monkeypatch.setattr(registered_platform_origins, "_refreshed_at", time.monotonic())
    finding = _framing(await _audit(monkeypatch, mode=mode, ancestors=["https://portal.example.com"], token=token))
    directive = csp.frame_ancestor_sources(None, embedders=registered_platform_origins.registered_platform_origins)
    assert directive[:2] == ("'self'", "https://portal.example.com")
    assert ("https://opencti.example.com" in directive) is (
        "and the platforms registered with it (OpenCTI, OpenAEV, OpenCRQ)" in finding.description
    )


def test_the_audit_and_the_middleware_share_one_registered_platform_gate():
    assert security_audit.registered_platforms_may_frame is registered_platform_origins.registered_platforms_may_frame


def test_the_audit_and_the_startup_warning_share_one_predicate():
    assert security_audit.permissive_frame_ancestor_sources is csp.permissive_frame_ancestor_sources


@pytest.mark.parametrize(
    "ancestors",
    [
        ["*"],
        ["https:"],
        ["chrome-extension:"],
        ["https://*:8443"],
        ["'self'"],
        ["https://portal.example.com"],
        ["https://*.example.com"],
        ["portal.example.com:8443"],
    ],
)
async def test_the_audit_warns_exactly_when_the_startup_does(monkeypatch, caplog, ancestors):
    findings = await _audit(monkeypatch, ancestors=ancestors)
    with caplog.at_level(logging.WARNING, logger="app.core.csp"):
        csp.log_content_security_policy_configuration()
    at_startup = any(r.name == "app.core.csp" and r.levelno >= logging.WARNING for r in caplog.records)
    assert any(f.id == "web.csp_frame_ancestors_any" for f in findings) is at_startup


# -- the finding contract ---------------------------------------------------


def test_a_finding_carries_its_id_and_its_control():
    finding = security_audit.AuditFinding(
        id="web.csp_off", control=MODE_CONTROL, category="web", severity="warning", title="t", description="d"
    )
    assert finding.model_dump()["id"] == "web.csp_off"
    assert finding.model_dump()["control"] == MODE_CONTROL


def _control_statuses(monkeypatch, *, mode: str, ancestors=()) -> dict[str, security_controls.ControlStatus]:
    monkeypatch.setattr(security_audit.settings, "content_security_policy_mode", mode)
    monkeypatch.setattr(security_audit.settings, "content_security_policy_frame_ancestors", list(ancestors))
    return {status.id: status for status in security_controls.control_statuses(None, None)}


@pytest.mark.parametrize(("mode", "state"), [("off", "off"), ("report-only", "monitoring"), ("enforce", "enforced")])
def test_the_policy_control_monitors_in_report_only_mode_and_is_enforced_only_by_enforce(monkeypatch, mode, state):
    status = _control_statuses(monkeypatch, mode=mode)[MODE_CONTROL]
    assert (status.state, status.value, status.default) == (state, mode, "report-only")
    assert (status.setting, status.hardened, status.category) == ("CONTENT_SECURITY_POLICY_MODE", "enforce", "web")
    # Report-only by default: enforcing is a choice to make.
    assert status.opt_in is True


@pytest.mark.parametrize(
    ("mode", "ancestors", "state"),
    [
        ("off", [], "off"),
        ("off", NAMED, "off"),
        ("report-only", [], "monitoring"),
        ("report-only", NAMED, "monitoring"),
        ("report-only", ["*"], "off"),
        ("report-only", ["https:"], "off"),
        ("enforce", [], "enforced"),
        ("enforce", NAMED, "enforced"),
        ("enforce", ["https://*"], "off"),
    ],
)
def test_the_framing_control_is_enforced_only_by_an_enforced_policy_that_names_its_sites(
    monkeypatch, mode, ancestors, state
):
    status = _control_statuses(monkeypatch, mode=mode, ancestors=ancestors)[FRAME_CONTROL]
    assert status.state == state
    assert (status.setting, status.category, status.default) == (
        "CONTENT_SECURITY_POLICY_FRAME_ANCESTORS",
        "web",
        "empty",
    )


_STATE_OF_THE_FINDING = {
    "web.csp_off": "off",
    "web.csp_report_only": "monitoring",
    "web.csp_enforced": "enforced",
    "web.csp_frame_ancestors_any": "off",
    "web.csp_frame_ancestors_not_enforced": "monitoring",
    "web.csp_frame_ancestors_restricted": "enforced",
}


@pytest.mark.parametrize("mode", ["off", "report-only", "enforce"])
@pytest.mark.parametrize("ancestors", [[], ["'self'"], NAMED, ["*"], ["https:"], ["https://*:8443"]])
async def test_each_control_is_in_the_state_its_findings_report(monkeypatch, mode, ancestors):
    """A control and the findings naming it never disagree: framing reads as
    enforced only when the finding calls it restricted."""
    findings = await _audit(monkeypatch, mode=mode, ancestors=ancestors)
    statuses = _control_statuses(monkeypatch, mode=mode, ancestors=ancestors)
    assert statuses[MODE_CONTROL].state == _STATE_OF_THE_FINDING[_mode(findings).id]
    framing = [f.id for f in findings if f.control == FRAME_CONTROL]
    # Off: no framing finding, and framing is not restricted.
    assert statuses[FRAME_CONTROL].state == (_STATE_OF_THE_FINDING[framing[0]] if framing else "off")


def test_every_finding_of_the_check_is_literal_and_names_its_control():
    tree = ast.parse(inspect.getsource(security_audit._check_content_security_policy))
    calls = sorted(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "AuditFinding"
        ),
        key=lambda node: node.lineno,
    )
    literals = [
        tuple(
            next(
                (k.value.value for k in call.keywords if k.arg == name and isinstance(k.value, ast.Constant)),
                None,
            )
            for name in ("id", "control", "category")
        )
        for call in calls
    ]
    assert literals == [
        ("web.csp_off", MODE_CONTROL, "web"),
        ("web.csp_report_only", MODE_CONTROL, "web"),
        ("web.csp_enforced", MODE_CONTROL, "web"),
        ("web.csp_frame_ancestors_any", FRAME_CONTROL, "web"),
        ("web.csp_frame_ancestors_not_enforced", FRAME_CONTROL, "web"),
        ("web.csp_frame_ancestors_restricted", FRAME_CONTROL, "web"),
    ]


# -- the report ----------------------------------------------------------------


async def test_the_report_carries_the_policy_finding(monkeypatch):
    async def _no_finding(*args, **kwargs) -> None:
        return None

    def _no_finding_sync(*args, **kwargs) -> None:
        return None

    # Every other check needs a database, Redis or Docker; the proxy-trust check is synchronous.
    for name in dir(security_audit):
        if name.startswith("_check_") and name != "_check_content_security_policy":
            check = getattr(security_audit, name)
            monkeypatch.setattr(
                security_audit, name, _no_finding if inspect.iscoroutinefunction(check) else _no_finding_sync
            )
    monkeypatch.setattr(security_audit.settings, "content_security_policy_mode", "off")
    report = await security_audit.run_security_audit(admin=object(), db=object())  # type: ignore[arg-type]
    assert [(f.category, f.severity, f.title) for f in report.findings] == [
        ("web", "warning", "Content security policy is off")
    ]
    assert report.overall_score == "needs_attention"
    states = {control.id: control.state for control in report.controls}
    assert (states[MODE_CONTROL], states[FRAME_CONTROL]) == ("off", "off")


async def test_the_audit_reads_what_the_report_endpoint_counted(monkeypatch):
    """Through the counters themselves: what ``count_violations`` writes on any
    replica is what the finding names."""
    redis = _FakeRedis()

    async def _client():
        return redis

    monkeypatch.setattr(csp_reports, "redis_client", _client)
    monkeypatch.setattr(csp_reports, "_utc_today", lambda: date(2026, 9, 28))
    monkeypatch.setattr(security_audit.settings, "content_security_policy_mode", "report-only")
    monkeypatch.setattr(security_audit.settings, "content_security_policy_frame_ancestors", [])
    await count_violations([_violation(directive="img-src")] * 2 + [_violation(directive="connect-src")] * 5)
    await count_violations([_violation(directive="img-src"), _violation(extension=True)])
    findings: list[security_audit.AuditFinding] = []
    await security_audit._check_content_security_policy(findings)
    finding = _mode(findings)
    assert finding.id == "web.csp_report_only"
    assert (
        "browsers reported 8 violations: connect-src 5, img-src 3 (and 1 caused by browser extensions"
    ) in finding.description


async def test_a_site_framing_the_platform_in_report_only_mode_is_counted_where_the_finding_says(monkeypatch):
    """The framing finding points at the report-only finding's per-directive
    totals: a frame-ancestors violation the report endpoint counted is there."""
    redis = _FakeRedis()

    async def _client():
        return redis

    monkeypatch.setattr(csp_reports, "redis_client", _client)
    monkeypatch.setattr(csp_reports, "_utc_today", lambda: date(2026, 9, 28))
    monkeypatch.setattr(security_audit.settings, "content_security_policy_mode", "report-only")
    monkeypatch.setattr(security_audit.settings, "content_security_policy_frame_ancestors", [])
    monkeypatch.setattr(security_audit.settings, "platform_registration_token", "")
    await count_violations([_violation(directive="frame-ancestors")] * 3)
    findings: list[security_audit.AuditFinding] = []
    await security_audit._check_content_security_policy(findings)
    report_only, framing = _mode(findings), _framing(findings)
    assert "browsers reported 3 violations: frame-ancestors 3." in report_only.description
    assert framing.id == "web.csp_frame_ancestors_not_enforced"
    assert f'per-directive totals of the "{report_only.title}" finding' in framing.description
