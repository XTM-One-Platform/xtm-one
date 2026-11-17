"""Per-platform tool approval.

A registered Suite platform contributes tools XTM One cannot classify: they
are not on the shipped whitelist and never can be, because we do not know what
they do. So they gate, and on upgrade that begins everywhere at once.

Whether that is right depends on two things that are deliberately kept apart.
*Can this platform display an approval prompt* is a *fact* only it knows.
*What to do while it cannot* is a *policy* only an operator can set. An earlier
design answered both from a table of "product X version Y supports approvals",
which wrote other teams' release schedules into ours and lied the moment
anyone backported.
"""

from __future__ import annotations

import uuid

import pytest

from app.tools.approval import (
    NUDGE_DECLARED,
    NUDGE_OBSERVED,
    POLICY_REQUIRE,
    POLICY_SKIP,
    SET_BY_ADMIN,
    SET_BY_DEFAULT,
    SET_BY_DEPLOYMENT,
    platform_capability_nudge,
    platform_gates_tools,
)


def _gates(supports, policy=POLICY_REQUIRE, set_by=SET_BY_DEFAULT) -> bool:
    return platform_gates_tools(
        supports_approval_prompts=supports,
        approval_policy=policy,
        approval_policy_set_by=set_by,
    )


# --- the safe direction is the default ----------------------------------


def test_an_undeclared_platform_gates():
    """Most of the fleet. Silence must not be read as consent."""
    assert _gates(None) is True


def test_never_declared_is_not_the_same_as_declared_false():
    """NULL means "has not said"; False means "has said it cannot ask".

    Both fall through to policy, but conflating them would lose the ability to
    tell an old build from a deliberate statement — and the UI says different
    things about each.
    """
    assert _gates(None, POLICY_SKIP, SET_BY_DEPLOYMENT) is False
    assert _gates(False, POLICY_SKIP, SET_BY_DEPLOYMENT) is False
    assert _gates(None) is True
    assert _gates(False) is True


# --- the fact layer, when there is one ----------------------------------


def test_declaring_support_restores_gating_over_a_seeded_default():
    """The self-correcting property, and the reason this beats an env var.

    A deployment set to skip because the platform could not ask should start
    gating again the moment it can — on the next heartbeat re-registration,
    with nobody touching a setting and nothing to remember to undo.
    """
    assert _gates(True, POLICY_SKIP, SET_BY_DEPLOYMENT) is True


# --- an explicit human choice outranks everything ------------------------


def test_an_administrators_choice_survives_a_capability_declaration():
    """A product upgrade must never silently re-arm a control someone
    deliberately switched off. The UI nudges instead."""
    assert _gates(True, POLICY_SKIP, SET_BY_ADMIN) is False


def test_an_administrator_can_also_gate_a_platform_that_cannot_ask():
    """The choice runs both ways: an operator may prefer agents that stop and
    explain themselves over agents that act unreviewed."""
    assert _gates(False, POLICY_REQUIRE, SET_BY_ADMIN) is True


# --- the nudge that resolves the one conflict ---------------------------


def test_nudge_fires_only_when_a_capable_platform_is_still_skipping():
    assert (
        platform_capability_nudge(
            supports_approval_prompts=True,
            approval_policy=POLICY_SKIP,
            approval_policy_set_by=SET_BY_ADMIN,
        )
        == NUDGE_DECLARED
    )


@pytest.mark.parametrize(
    ("supports", "policy", "set_by"),
    [
        # Already gating — nothing to offer.
        (True, POLICY_REQUIRE, SET_BY_ADMIN),
        # Skipping, but the seeded default is about to be overridden by the
        # fact layer anyway, so it is already gating.
        (True, POLICY_SKIP, SET_BY_DEPLOYMENT),
        # Cannot ask: skipping is the correct state, not something to nag about.
        (False, POLICY_SKIP, SET_BY_ADMIN),
        (None, POLICY_SKIP, SET_BY_DEPLOYMENT),
    ],
)
def test_nudge_stays_quiet_otherwise(supports, policy, set_by):
    """A nudge that fires when nothing is wrong trains people to ignore it."""
    assert (
        platform_capability_nudge(
            supports_approval_prompts=supports,
            approval_policy=policy,
            approval_policy_set_by=set_by,
        )
        is None
    )


# --- a client's declaration is the other source of evidence --------------


def _nudge(supports, policy=POLICY_SKIP, set_by=SET_BY_ADMIN, *, seen=False):
    return platform_capability_nudge(
        supports_approval_prompts=supports,
        approval_policy=policy,
        approval_policy_set_by=set_by,
        approval_support_seen=seen,
    )


def test_an_observed_client_nudges_a_platform_that_never_declared():
    """The blind spot this closes.

    Policy *skip*, capability never declared, and a client that would happily
    have shown a prompt: the tools run unattended, and before this the operator
    was never told the reason for their choice had expired — the nudge fired
    only on a declared TRUE, which a silent platform never produces.
    """
    assert _nudge(None, seen=False) is None
    assert _nudge(None, seen=True) == NUDGE_OBSERVED


def test_an_observation_never_makes_the_tools_gate():
    """It is evidence for a question, not an answer to it. One capable client
    does not speak for an unattended caller of the same platform."""
    assert _gates(None, POLICY_SKIP, SET_BY_ADMIN) is False


def test_the_platforms_own_word_outranks_one_clients():
    """Both in hand, the declaration is the stronger claim and names the
    nudge — the UI says "this platform can now ask", not "a client can"."""
    assert _nudge(True, seen=True) == NUDGE_DECLARED


def test_an_observation_stays_quiet_while_the_tools_already_gate():
    """Same rule as the declared nudge: nothing to offer when the safer
    posture is already in force."""
    assert _nudge(None, POLICY_REQUIRE, SET_BY_ADMIN, seen=True) is None
    assert _nudge(None, POLICY_REQUIRE, SET_BY_DEFAULT, seen=True) is None


def test_a_platform_that_said_it_cannot_ask_still_nudges_on_a_client():
    """Not a contradiction to resolve silently. The platform spoke for its own
    assistant; some other caller of it demonstrably answers prompts, and which
    of the two an operator cares about is the operator's call."""
    assert _nudge(False, seen=True) == NUDGE_OBSERVED


def test_the_alias_pair_resolves_both_ways():
    """OpenAEV is reachable under either name — the header says one, an older
    registration may say the other — so recording and gating must agree."""
    from app.tools.approval import platform_identifiers

    assert platform_identifiers("openaev") == {"openaev", "openbas"}
    assert platform_identifiers("openbas") == {"openaev", "openbas"}
    assert platform_identifiers("opencti") == {"opencti"}


def test_the_chat_path_records_what_a_client_declares():
    """Evidence nobody collects is evidence nobody has. The declaration only
    ever arrives on a chat message, so that is where it has to be caught."""
    import inspect

    from app.api.v1 import platform

    source = inspect.getsource(platform.send_platform_chat_message)
    assert "_note_client_approval_support" in source
    assert "body.supports_tool_approval" in source


# --- the deployment default is a seed, not an override -------------------


def test_env_var_is_read_only_when_a_row_is_created():
    """The distinction the whole design rests on.

    A runtime override would be invisible: the settings page would show
    approval enabled while nothing gated. Seeding writes a value an operator
    can then see and change, so the page never lies about what is enforced.
    """
    import inspect

    from app.api.v1 import platform

    source = inspect.getsource(platform.register_platform)
    create_at = source.index("db.add(reg)")
    assert "_seed_platform_policy()" in source[:create_at], (
        "the deployment default must be applied where the row is created"
    )
    assert "_seed_platform_policy()" not in source[create_at:]


def test_re_registration_never_touches_the_policy():
    """The nastiest failure this avoids.

    Platforms re-register on a heartbeat. Re-seeding there would revert an
    administrator's choice minutes after they made it, with the settings page
    still showing what they picked until the next refresh.
    """
    import inspect

    from app.api.v1 import platform

    source = inspect.getsource(platform.register_platform)
    update_branch = source[source.index("reg.platform_version = body.platform_version") :]
    update_branch = update_branch[: update_branch.index("db.add(reg)")]

    assert "reg.supports_approval_prompts" in update_branch, (
        "the capability must refresh, or gating never self-corrects"
    )
    assert "approval_policy" not in update_branch, (
        "a heartbeat must not rewrite the operator's policy"
    )


def test_an_unrecognised_deployment_value_gates():
    """A typo in a deployment variable must not quietly disable a control."""
    from app.api.v1.platform import _seed_platform_policy
    from app.core.config import settings

    original = settings.tool_approvals_platform_default
    try:
        settings.tool_approvals_platform_default = "sikp"
        assert _seed_platform_policy()["approval_policy"] == POLICY_REQUIRE
        settings.tool_approvals_platform_default = ""
        assert _seed_platform_policy()["approval_policy"] == POLICY_REQUIRE
    finally:
        settings.tool_approvals_platform_default = original


def test_the_deployment_value_is_recorded_as_such():
    """Provenance is what lets the UI say whether a setting is protecting the
    operator or merely inherited from an install nobody remembers."""
    from app.api.v1.platform import _seed_platform_policy
    from app.core.config import settings

    original = settings.tool_approvals_platform_default
    try:
        settings.tool_approvals_platform_default = "skip"
        seeded = _seed_platform_policy()
        assert seeded == {
            "approval_policy": POLICY_SKIP,
            "approval_policy_set_by": SET_BY_DEPLOYMENT,
        }
    finally:
        settings.tool_approvals_platform_default = original


# --- the decision has to reach the gate ---------------------------------


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeSession:
    """Answers the three queries ``resolve_exempt_sources`` makes.

    Dispatches on the table named in the statement rather than on call order,
    so it keeps telling the truth if the reads are ever reordered.
    """

    def __init__(self, *, registrations=(), toggled_off=(), bots_off=()):
        self._registrations = list(registrations)
        # ``(provider, id)``: a connection's own switch is keyed by the
        # connection, so one owner turning theirs off cannot exempt
        # everybody else's connection of the same provider.
        self._toggled_off = [
            p if isinstance(p, tuple) else (p, f"{p}-id") for p in toggled_off
        ]
        self._bots_off = [(b,) for b in bots_off]

    async def execute(self, stmt):
        sql = str(stmt)
        if "platform_registrations" in sql:
            return _FakeResult(self._registrations)
        # Answered separately: falling through to the integration rows
        # turned every ungated provider into a ``channel_bot:<provider>``
        # source that no bot had ever asked for.
        if "channel_bots" in sql:
            return _FakeResult(self._bots_off)
        return _FakeResult(self._toggled_off)


async def _exempt(**kwargs) -> frozenset[str]:
    from app.services.hitl_whitelist import resolve_exempt_sources

    return await resolve_exempt_sources(_FakeSession(**kwargs))


@pytest.mark.asyncio
async def test_a_platform_set_to_skip_reaches_the_gate():
    """Without this the setting is decorative: the gate would read only the
    per-integration toggle and a platform set to skip would still stop every
    agent that uses it."""
    exempt = await _exempt(
        registrations=[("openaev", False, POLICY_SKIP, SET_BY_ADMIN)]
    )
    assert "integration:openaev" in exempt


@pytest.mark.asyncio
async def test_the_legacy_identifier_covers_the_canonical_key():
    """Registration under ``openbas`` creates the integration as ``openaev``,
    so a decision made under the old name has to travel to the new one."""
    exempt = await _exempt(
        registrations=[("openbas", False, POLICY_SKIP, SET_BY_ADMIN)]
    )
    assert {"integration:openbas", "integration:openaev"} <= exempt


@pytest.mark.asyncio
async def test_the_older_toggle_cannot_override_a_platform_set_to_require():
    """The bug this closes. ``requires_approval`` predates the per-platform
    control and registration creates an integration row for the platform, so
    unioning the two made exemption an OR — one flip of the older flag and a
    platform an operator deliberately gated ran unattended, while the settings
    card still said its tools ask before running."""
    exempt = await _exempt(
        registrations=[("openaev", True, POLICY_REQUIRE, SET_BY_ADMIN)],
        toggled_off=["openaev"],
    )
    assert "integration:openaev" not in exempt


@pytest.mark.asyncio
async def test_the_legacy_identifier_is_governed_under_both_keys():
    """Same precedence, reached through the alias: an ``openbas`` registration
    set to require must not be undone by the toggle on ``openaev``."""
    exempt = await _exempt(
        registrations=[("openbas", True, POLICY_REQUIRE, SET_BY_ADMIN)],
        toggled_off=["openaev", "openbas"],
    )
    assert not exempt


@pytest.mark.asyncio
async def test_an_unregistered_integration_keeps_its_own_toggle():
    """Precedence is scoped to platforms. A plain Slack or Jira connection has
    no platform policy to defer to, so its toggle stays the only answer."""
    exempt = await _exempt(
        registrations=[("openaev", True, POLICY_REQUIRE, SET_BY_ADMIN)],
        toggled_off=["slack"],
    )
    assert exempt == frozenset({"integration:slack:slack-id"})


@pytest.mark.asyncio
async def test_an_unreadable_toggle_gates_everything():
    """Fail closed: not knowing a source was exempted must never read as
    knowing it was."""
    from app.services.hitl_whitelist import resolve_exempt_sources

    class _Broken:
        async def execute(self, _stmt):
            raise RuntimeError("connection reset")

    assert await resolve_exempt_sources(_Broken()) == frozenset()


@pytest.mark.asyncio
async def test_the_older_toggle_is_refused_for_a_governed_provider():
    """Silently ignoring the write would leave the API answering later reads
    with a preference nothing enforces — the same lie, one layer up. The alias
    is shared with the gate, so refusal and enforcement cover the same set."""
    import inspect

    from app.api.v1 import integrations as integrations_api
    from app.services.hitl_whitelist import platform_approval_by_provider

    session = _FakeSession(
        registrations=[("openbas", True, POLICY_REQUIRE, SET_BY_ADMIN)]
    )
    governed, _exempt = await platform_approval_by_provider(session)
    assert "openaev" in governed
    assert "slack" not in governed

    assert "platform_approval_by_provider" in inspect.getsource(
        integrations_api.update_integration
    ), "the toggle must be refused where it is written, not just ignored"


# --- and it has to reach the operator, truthfully ------------------------


def _integration(provider: str, *, requires_approval: bool):
    from datetime import UTC, datetime

    from app.models.integration import Integration

    # Column defaults are applied on flush, so an unsaved row needs the
    # non-nullable booleans spelled out.
    now = datetime.now(UTC)
    return Integration(
        id=uuid.uuid4(),
        provider=provider,
        auth_type="api_key",
        name=provider,
        status="connected",
        requires_approval=requires_approval,
        is_company_managed=False,
        is_builtin=False,
        oauth_share_acknowledged=False,
        created_at=now,
        updated_at=now,
    )


# Main made the caller's rights required keywords; neither affects the approval fields.
_PERMS = {"can_manage": True, "can_authorize": True}


def test_the_api_reports_the_stored_toggle_for_an_ordinary_integration():
    """It reported a hard-coded ``True`` before this: the column was written by
    the update endpoint and then never read back, so the one client able to
    show the setting would have shown the wrong one."""
    from app.api.v1.integrations import _to_response

    assert _to_response(_integration("slack", requires_approval=False), **_PERMS).requires_approval is False
    assert _to_response(_integration("slack", requires_approval=True), **_PERMS).requires_approval is True


def test_the_api_reports_the_platform_policy_where_that_is_what_gates():
    """For a governed provider the stored column is inert, so echoing it would
    describe a setting nothing enforces. ``requires_approval`` means one thing
    everywhere: do these tools ask before running."""
    from app.api.v1.integrations import _to_response

    governed, exempt = frozenset({"openaev"}), frozenset({"openaev"})
    row = _integration("openaev", requires_approval=True)

    response = _to_response(row, **_PERMS, platform_approval=(governed, exempt))
    assert response.requires_approval is False, "the platform is set to skip"
    assert response.approval_managed_by_platform is True


def test_an_ungoverned_integration_is_not_marked_as_platform_managed():
    """The flag is what sends a reader to Settings instead of the local
    control, so a false positive would strand them on a page with no toggle."""
    from app.api.v1.integrations import _to_response

    response = _to_response(
        _integration("slack", requires_approval=False),
        **_PERMS,
        platform_approval=(frozenset({"openaev"}), frozenset({"openaev"})),
    )
    assert response.approval_managed_by_platform is False
    assert response.requires_approval is False


def test_both_entry_points_resolve_exemptions_the_same_way():
    """A run and a conversation must not reach opposite verdicts about the same
    tool. The assignment path used to re-query the integration toggle inline and
    never read the platform policy, so a platform set to skip gated in runs and
    ran in chat."""
    import inspect

    from app.services import assignment_engine, hitl_whitelist

    assert "resolve_exempt_sources" in inspect.getsource(
        hitl_whitelist.build_approval_gate
    )
    assert "resolve_exempt_sources" in inspect.getsource(assignment_engine)
    assert "Integration.requires_approval" not in inspect.getsource(
        assignment_engine
    ), "the inline copy is what drifted; there must be exactly one resolver"


@pytest.mark.asyncio
async def test_one_owner_s_toggle_does_not_exempt_another_owner_s_connection():
    """The control says per integration; it behaved per provider.

    Several Notion connections can exist, owned by different people. A bare
    ``integration:notion`` in the exempt set matched every one of their tools,
    so one operator turning their own connection off stopped everybody else's
    Notion calls pausing — silently, and against what the switch claims.
    """
    exempt = await _exempt(toggled_off=[("notion", "mine")])

    assert "integration:notion:mine" in exempt
    assert "integration:notion" not in exempt, (
        "the bare provider form is a platform policy, not one connection's switch"
    )


@pytest.mark.asyncio
async def test_a_platform_policy_stays_provider_wide():
    """The other shape, and it is deliberate: a registered platform's policy
    governs every connection of that provider."""
    from app.tools.approval import POLICY_SKIP, SET_BY_ADMIN

    exempt = await _exempt(
        registrations=[("openaev", False, POLICY_SKIP, SET_BY_ADMIN)],
    )

    assert "integration:openaev" in exempt
