"""CODE_INTERPRETER_OPEN_EGRESS: the code interpreter's open egress policy and modules."""

from __future__ import annotations

import pytest

from app.core.config import settings
from app.services import code_interpreter
from app.services._sandbox_selector import OPEN_EGRESS_DENY, egress_policy
from app.services._sandbox_utils import ALWAYS_BLOCKED_MODULES


def test_default_policy_denies_everything_but_the_allow_list() -> None:
    policy = egress_policy(frozenset({"api.example.com"}))

    assert policy.default_action == "deny"
    assert [(r.action, r.target) for r in policy.egress or []] == [("allow", "api.example.com")]


def test_default_policy_with_an_empty_allow_list_has_no_rule() -> None:
    policy = egress_policy(frozenset())

    assert policy.default_action == "deny"
    assert policy.egress is None


def test_open_policy_allows_public_egress_and_denies_metadata_and_internal_ranges() -> None:
    policy = egress_policy(frozenset({"api.example.com"}), open_egress=True)

    assert policy.default_action == "allow"
    rules = policy.egress or []
    assert {r.action for r in rules} == {"deny"}
    denied = {r.target for r in rules}
    for target in (
        "169.254.169.254",
        "metadata.google.internal",
        "*.svc",
        "*.cluster.local",
        "localhost",
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "100.64.0.0/10",
        "fc00::/7",
        "fe80::/10",
    ):
        assert target in denied
    assert denied == set(OPEN_EGRESS_DENY)


@pytest.mark.parametrize("open_egress", [False, True])
def test_opensandbox_modules_follow_the_switch(monkeypatch: pytest.MonkeyPatch, open_egress: bool) -> None:
    monkeypatch.setattr(settings, "code_interpreter_open_egress", open_egress)
    monkeypatch.setattr(code_interpreter, "_resolve_allowed_modules", lambda: frozenset({"sqlite3"}))

    allow = code_interpreter._opensandbox_allowed_modules()

    assert "sqlite3" in allow
    assert allow.issuperset(code_interpreter.OPEN_EGRESS_MODULES) is open_egress


def test_open_egress_modules_never_reopen_an_escape_primitive() -> None:
    assert not code_interpreter.OPEN_EGRESS_MODULES & ALWAYS_BLOCKED_MODULES


def test_open_egress_is_off_by_default() -> None:
    assert type(settings).model_fields["code_interpreter_open_egress"].default is False
