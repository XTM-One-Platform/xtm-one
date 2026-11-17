"""The public widget renders answers as before unless strict rendering is opted in.

``PUBLIC_WIDGET_STRICT_RENDERING`` reaches the widget through the public config
(``strict_rendering``); the rendering itself is tested on ``widget.js`` in
``frontend/tests/embedded-markdown-xss.test.ts``. The default is reported by
the Security Audit only.
"""

from __future__ import annotations

import contextlib
import json
from types import SimpleNamespace

import pytest

from app.api.v1 import public_chat, security_audit


@pytest.fixture
def config_route(monkeypatch):
    @contextlib.asynccontextmanager
    async def _session():
        yield object()

    async def _resolve(_token, _db=None):
        return SimpleNamespace(
            name="Support", description="d", icon="bot", web_suggested_prompts=None
        )

    monkeypatch.setattr(public_chat, "async_session", _session)
    monkeypatch.setattr(public_chat, "_resolve_agent", _resolve)


async def _config() -> dict:
    response = await public_chat.get_public_config("token")
    return json.loads(response.body)


@pytest.mark.parametrize("strict", [False, True])
async def test_the_rendering_mode_reaches_the_widget_without_a_log_line(
    config_route, monkeypatch, caplog, strict
):
    # The default is a deliberate choice the Security Audit reports:
    # logging it on every start would be noise.
    monkeypatch.setattr(public_chat.settings, "public_widget_strict_rendering", strict)
    with caplog.at_level("DEBUG", logger=public_chat.logger.name):
        config = await _config()
    assert config["strict_rendering"] is strict
    assert config["name"] == "Support"
    assert caplog.records == []


class _CountDb:
    def __init__(self, count: int):
        self._count = count
        self.queries = 0

    async def execute(self, *_args, **_kwargs):
        self.queries += 1
        return SimpleNamespace(scalar=lambda: self._count)


async def test_the_security_audit_reports_the_default_while_a_widget_is_enabled(monkeypatch):
    monkeypatch.setattr(security_audit.settings, "public_widget_strict_rendering", False)
    findings: list[security_audit.AuditFinding] = []
    await security_audit._check_public_widget(_CountDb(2), findings)
    [finding] = findings
    assert (finding.id, finding.category, finding.severity) == ("web.public_widget_remote_images", "web", "warning")
    assert finding.control == "web.public_widget_strict_rendering"
    assert "2 agent(s)" in finding.title
    assert "PUBLIC_WIDGET_STRICT_RENDERING=true" in (finding.recommendation or "")
    # The admin deciding to opt in is told what else strict rendering drops.
    assert "styles" in (finding.recommendation or "")
    assert "http(s), mailto and tel" in (finding.recommendation or "")


@pytest.mark.parametrize("strict", [False, True])
async def test_the_security_audit_is_silent_without_a_widget(monkeypatch, strict):
    monkeypatch.setattr(security_audit.settings, "public_widget_strict_rendering", strict)
    findings: list[security_audit.AuditFinding] = []
    await security_audit._check_public_widget(_CountDb(0), findings)
    assert findings == []


async def test_the_security_audit_reports_the_opt_in_as_a_pass_while_a_widget_is_enabled(monkeypatch):
    # No silent pass: strict rendering on, with widgets embedded, says so.
    monkeypatch.setattr(security_audit.settings, "public_widget_strict_rendering", True)
    findings: list[security_audit.AuditFinding] = []
    await security_audit._check_public_widget(_CountDb(3), findings)
    [finding] = findings
    assert (finding.id, finding.category, finding.severity) == ("web.public_widget_strict", "web", "pass")
    assert "3 agent(s)" in finding.title
    assert finding.control == "web.public_widget_strict_rendering"
