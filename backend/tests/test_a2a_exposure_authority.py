"""Who may expose an agent, and whose boundary the exposure carries.

Authority is deliberately NOT a new permission: it is
``writable_agent_error()``, the same gate that already decides who may edit
an agent or attach an assignment to it. These tests pin that delegation,
because reimplementing the rule here is exactly the drift the design set out
to avoid — and A2A briefly WAS that drift: it delegated to bare
``can_manage``, making it the one attachment surface that admitted built-in
agents in XTM One mode while assignments and channel wiring refused them.

They also pin the rule the whole feature turns on - the boundary follows the
AGENT, never the actor who exposed it. Mirrors the repo's fake-session
convention; the backend suite has no real-DB harness.
"""

import uuid

import pytest

from app.a2a import exposure as expo


class _User:
    def __init__(self, role="user", uid=None):
        self.id = uid or uuid.uuid4()
        self.role = role
        self.email = "u@example.com"

    @property
    def is_admin(self):
        return self.role == "admin"


class _Agent:
    def __init__(
        self,
        owner_id=None,
        *,
        company_managed=False,
        builtin=False,
        shared_group_ids=None,
        shared_group_permissions=None,
    ):
        self.id = uuid.uuid4()
        self.user_id = owner_id or uuid.uuid4()
        self.is_company_managed = company_managed
        self.is_builtin = builtin
        self.shared_group_ids = shared_group_ids
        self.shared_group_permissions = shared_group_permissions
        self.name = "Test Agent"


@pytest.fixture
def groups(monkeypatch):
    """Control what groups a user belongs to."""
    state = {"ids": []}

    async def _get(_db, _uid):
        return list(state["ids"])

    monkeypatch.setattr(expo, "get_user_group_ids", _get)
    return state


class TestExposureAuthority:
    async def test_owner_may_expose(self, groups):
        owner = _User()
        agent = _Agent(owner_id=owner.id)
        assert await expo.user_can_expose(None, owner, agent) is True

    async def test_admin_may_expose_another_users_agent(self, groups):
        agent = _Agent()
        assert await expo.user_can_expose(None, _User(role="admin"), agent) is True

    async def test_unrelated_user_may_not_expose(self, groups):
        agent = _Agent()
        assert await expo.user_can_expose(None, _User(), agent) is False

    async def test_config_tier_group_member_may_expose(self, groups):
        """Accepted risk: this places the OWNER's boundary externally on
        someone else's initiative. Mitigated by notifying the owner, not by
        narrowing the authority."""
        gid = str(uuid.uuid4())
        groups["ids"] = [gid]
        agent = _Agent(
            shared_group_ids=[gid], shared_group_permissions={gid: "config"}
        )
        assert await expo.user_can_expose(None, _User(), agent) is True

    async def test_read_only_group_member_may_not_expose(self, groups):
        gid = str(uuid.uuid4())
        groups["ids"] = [gid]
        agent = _Agent(
            shared_group_ids=[gid], shared_group_permissions={gid: "read_only"}
        )
        assert await expo.user_can_expose(None, _User(), agent) is False

    async def test_builtin_agent_is_exposable_in_xtm_one_mode(
        self, groups, monkeypatch
    ):
        """XTM One's read-only rule for built-ins does not reach exposure.

        That rule exists because ``seed._sync_agent_fields`` overwrites a
        built-in's fields on every restart, so an edit would silently drift
        back. Exposure lives in ``integration_bindings``, which the seed pass
        never touches — the decision holds. And in XTM One the built-ins ARE
        the product's agents, so refusing them would leave partners able to
        reach only hand-built ones."""
        from app.core.config import settings

        monkeypatch.setattr(settings, "platform_mode", "xtm_one")
        agent = _Agent(company_managed=True, builtin=True)
        assert await expo.user_can_expose(None, _User(role="admin"), agent) is True

    async def test_builtin_agent_is_still_admin_only(self, groups, monkeypatch):
        """``allow_builtin`` drops the read-only gate and NOTHING else.

        A built-in is company-managed, so the ordinary company-managed rule
        still applies and a non-admin — including one holding a config-tier
        grant — is refused. If this ever passes for a plain user, the flag has
        been widened past what it was for."""
        from app.core.config import settings

        monkeypatch.setattr(settings, "platform_mode", "xtm_one")
        agent = _Agent(company_managed=True, builtin=True)
        assert await expo.user_can_expose(None, _User(), agent) is False

    async def test_builtin_agent_stays_exposable_outside_xtm_one_mode(
        self, groups, monkeypatch
    ):
        """Unchanged in copilot/dev mode, where the read-only rule never
        applied in the first place."""
        from app.core.config import settings

        monkeypatch.setattr(settings, "platform_mode", "copilot")
        agent = _Agent(company_managed=True, builtin=True)
        assert await expo.user_can_expose(None, _User(role="admin"), agent) is True

    async def test_builtin_read_only_rule_still_holds_for_ordinary_edits(
        self, groups, monkeypatch
    ):
        """The carve-out is opt-in per call site, not a change to the gate.

        Without ``allow_builtin`` the same agent is refused, so every other
        surface that asks ``writable_agent_error`` — the agent builder, the
        assignment endpoints, channel wiring — is unaffected by this."""
        from app.api.v1.helpers import writable_agent_error
        from app.core.config import settings

        monkeypatch.setattr(settings, "platform_mode", "xtm_one")
        agent = _Agent(company_managed=True, builtin=True)
        assert (
            writable_agent_error(agent, _User(role="admin"), [], action="edit")
            == "Cannot edit built-in agents."
        )

    async def test_company_managed_agent_is_admin_only(self, groups):
        owner = _User()
        agent = _Agent(owner_id=owner.id, company_managed=True)
        # Even its own owner cannot, because can_manage says admins only.
        assert await expo.user_can_expose(None, owner, agent) is False
        assert await expo.user_can_expose(None, _User(role="admin"), agent) is True
