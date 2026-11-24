"""Integration platform fixes of harness batch x12-17 (#1799).

* the Microsoft package imports ``teams_scope`` lazily (no _ModuleLock deadlock);
* ``webhook_secret_state`` says whether a receiver secret is set and its source;
* a ``missing_integration`` finding a member's bindings contradict is dropped.
"""

from __future__ import annotations

import ast
import pathlib
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.services.fleet_doc_findings import drop_bound_integrations, member_key
from app.services.fleet_doc_generation import integrations_by_member

BACKEND = pathlib.Path(__file__).resolve().parents[1]


def test_microsoft_package_never_imports_teams_scope_at_module_level():
    tree = ast.parse((BACKEND / "app/integrations/providers/microsoft/__init__.py").read_text())
    top = [n for n in tree.body if isinstance(n, ast.ImportFrom)]
    assert not any(n.module == "teams_scope" for n in top)
    assert not any(n.module and n.module.endswith("microsoft.teams_scope") for n in top)


def test_teams_scope_imports_alone_then_the_package():
    import app.integrations.providers.microsoft as microsoft
    from app.integrations.providers.microsoft import teams_scope

    assert teams_scope.TEAMS_CHANNEL_SCOPE_KEY == "microsoft:teams_channel"
    assert hasattr(microsoft, "MicrosoftContext")


def _integ(auth_type: str, extra: object):
    return SimpleNamespace(auth_type=auth_type, extra_config_enc=b"x" if extra is not None else None, _extra=extra)


@pytest.mark.parametrize(
    ("auth_type", "extra", "expected"),
    [
        ("platform_managed", {"webhook_secret": "s3cret-value-long"}, "set (auto-generated)"),
        ("api_key", {"webhook_secret": "s3cret-value-long"}, "set (entered by an administrator)"),
        ("api_key", {}, "not set"),
        ("api_key", None, "not set"),
    ],
)
async def test_webhook_secret_state(auth_type, extra, expected):
    from app.services import trigger_provisioning

    async def reveal(integration, _field):
        return integration._extra

    with (
        patch("app.services.vault.reveal_json_field", reveal),
        patch("app.core.security.configured_secret", lambda v: v or None),
    ):
        state = await trigger_provisioning.webhook_secret_state(_integ(auth_type, extra))
    assert state == expected
    assert "s3cret" not in state


async def test_webhook_secret_state_unreadable():
    from app.services import trigger_provisioning

    async def reveal(_integration, _field):
        raise ValueError("bad")

    with patch("app.services.vault.reveal_json_field", reveal):
        assert await trigger_provisioning.webhook_secret_state(_integ("api_key", {})) == "unreadable"


def _finding(member: str, subject: str, line: str, kind: str = "missing_integration") -> dict:
    return {"kind": kind, "member": member, "subject": subject, "finding": line, "suggested_fix": ""}


def test_bound_integration_finding_is_dropped():
    context = {"tools": [{"member": "Developer", "integrations": ["github", "google_search"]}]}
    bound = integrations_by_member(context)
    assert bound == {member_key("Developer"): ["github", "google_search"]}
    findings = [
        _finding("Developer", "GitHub", "Developer needs a GitHub integration."),
        _finding("Developer", "", "Developer would need Google Search to read the web."),
        _finding("Developer", "Jira", "Developer has no Jira integration."),
        _finding("Assistant", "GitHub", "Assistant needs GitHub."),
        _finding("Developer", "GitHub", "Uses GitHub without a gate.", kind="no_gate_on_sensitive_action"),
    ]
    kept = drop_bound_integrations(findings, bound)
    assert [(f["member"], f["subject"], f["kind"]) for f in kept] == [
        ("Developer", "Jira", "missing_integration"),
        ("Assistant", "GitHub", "missing_integration"),
        ("Developer", "GitHub", "no_gate_on_sensitive_action"),
    ]


def test_provider_name_inside_a_longer_word_is_not_a_match():
    bound = {member_key("Desk"): ["git"]}
    kept = drop_bound_integrations([_finding("Desk", "GitHub", "Desk needs GitHub.")], bound)
    assert len(kept) == 1
