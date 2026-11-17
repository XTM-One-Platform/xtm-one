"""Flows check who may read an agent's traces before wiring trace inspection.

A flow's Tool node extends the agent node it feeds, so it skips the
chat-session bound. Trace inspection reads the agent's own traces, every
user's conversations with it, so a flow is held to the rule the chats follow
(``traces.may_read_agent_traces``) twice: when a writer adds the wiring, and
when a run wires the tools, for a flow saved before the check existed.
Every case is a non-admin (rule 19).
"""

import uuid
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.asyncio


def _agent(*, company_managed=True, owner=None, carries=(), **shares):
    return SimpleNamespace(
        id=uuid.uuid4(),
        builtin_tool_ids=list(carries),
        is_company_managed=company_managed,
        user_id=owner,
        shared_user_ids=shares.get("shared_user_ids"),
        shared_user_permissions=shares.get("shared_user_permissions"),
        shared_group_ids=shares.get("shared_group_ids"),
        shared_group_permissions=shares.get("shared_group_permissions"),
        trace_supervision_target_agent_ids=None,
    )


def _user():
    return SimpleNamespace(id=uuid.uuid4(), role="user", is_admin=False)


def _graph(agent_id, tool_ids=("builtin:inspect_my_traces",)):
    return {
        "nodes": [
            {"id": "agent-1", "kind": "agent", "config": {"agent_id": str(agent_id)}},
            {"id": "tools-1", "kind": "capability.tool", "config": {"tool_ids": list(tool_ids)}},
        ],
        "edges": [{"id": "e-1", "source": "tools-1", "target": "agent-1", "target_handle": "tools"}],
    }


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _DB:
    """Answers the agent load with the given agents."""

    def __init__(self, *agents):
        self._agents = list(agents)

    async def execute(self, _stmt):
        return _Rows(self._agents)


@pytest.fixture
def seen_everything(monkeypatch):
    """Every id the graph names is one the writer can see: only the trace rule can refuse."""

    async def _none(*_a, **_k):
        return []

    monkeypatch.setattr("app.services.flow_references._unseen_source_refs", _none)


@pytest.fixture
def groups(monkeypatch):
    member_of: list[str] = []

    async def _groups(_db, _user_id):
        return list(member_of)

    monkeypatch.setattr("app.api.v1.helpers.get_user_group_ids", _groups)
    return member_of


async def _save(user, agent, *, graph=None, previous=None):
    from app.services.flow_references import check_source_references

    return await check_source_references(
        _DB(agent),
        user,
        graph=graph or _graph(agent.id),
        previous_graph=previous,
        flow_owner_id=None,
    )


class TestASaveIsRefusedTraceInspectionOnAnAgentItsWriterMayNotRead:
    async def test_a_company_managed_agent(self, seen_everything, groups):
        issues = await _save(_user(), _agent())
        assert [issue["code"] for issue in issues] == ["trace_tools.not_readable"]
        assert issues[0]["severity"] == "error"
        assert issues[0]["node_id"] == "agent-1"

    @pytest.mark.parametrize("tier", ["config", "read_only"])
    async def test_a_grantee_without_chat_access(self, seen_everything, groups, tier):
        user = _user()
        direct = _agent(
            company_managed=False,
            owner=uuid.uuid4(),
            shared_user_ids=[str(user.id)],
            shared_user_permissions={str(user.id): tier},
        )
        assert [i["code"] for i in await _save(user, direct)] == ["trace_tools.not_readable"]
        group_id = str(uuid.uuid4())
        groups.append(group_id)
        through_group = _agent(
            company_managed=False,
            owner=uuid.uuid4(),
            shared_group_ids=[group_id],
            shared_group_permissions={group_id: tier},
        )
        assert [i["code"] for i in await _save(user, through_group)] == ["trace_tools.not_readable"]

    async def test_the_supervision_suite_too(self, seen_everything, groups):
        agent = _agent()
        issues = await _save(_user(), agent, graph=_graph(agent.id, ["builtin:inspect_agent_traces"]))
        assert [issue["code"] for issue in issues] == ["trace_tools.not_readable"]


class TestASaveKeepsWhatItMayOrDidNotAdd:
    async def test_a_config_with_chats_grantee_directly_or_through_a_group(self, seen_everything, groups):
        user = _user()
        direct = _agent(
            company_managed=False,
            owner=uuid.uuid4(),
            shared_user_ids=[str(user.id)],
            shared_user_permissions={str(user.id): "config_with_chats"},
        )
        assert await _save(user, direct) == []
        group_id = str(uuid.uuid4())
        groups.append(group_id)
        through_group = _agent(
            company_managed=False,
            owner=uuid.uuid4(),
            shared_group_ids=[group_id],
            shared_group_permissions={group_id: "config_with_chats"},
        )
        assert await _save(user, through_group) == []

    async def test_the_owner_of_a_personal_agent(self, seen_everything, groups):
        user = _user()
        assert await _save(user, _agent(company_managed=False, owner=user.id)) == []

    async def test_a_wiring_the_flow_already_held(self, seen_everything, groups):
        agent = _agent()
        assert await _save(_user(), agent, previous=_graph(agent.id)) == []

    async def test_a_suite_the_agent_carries_itself(self, seen_everything, groups):
        agent = _agent(carries=["inspect_my_traces"])
        assert await _save(_user(), agent) == []

    async def test_other_built_ins(self, seen_everything, groups):
        agent = _agent()
        assert await _save(_user(), agent, graph=_graph(agent.id, ["builtin:web_fetch", "builtin:agent_builder"])) == []


class TestARunWiresTraceInspectionOnlyForSomeoneWhoMayReadIt:
    """A flow saved before the check: ``run_agent_chat`` gets the flow's union and no session bound."""

    async def _suites(self, user, agent, turn):
        from app.services.agent_chat import _turn_trace_tool_ids

        return await _turn_trace_tool_ids(object(), user, agent, turn, user_group_ids=[])

    async def test_a_suite_the_flow_adds(self, groups):
        agent = _agent()
        assert await self._suites(_user(), agent, ["web_fetch", "inspect_my_traces", "inspect_agent_traces"]) == set()

    async def test_the_suites_a_flow_wired_builder_implies(self, groups):
        assert await self._suites(_user(), _agent(), ["agent_builder"]) == set()

    async def test_a_config_with_chats_grantee_gets_them(self, groups):
        user = _user()
        agent = _agent(
            company_managed=False,
            owner=uuid.uuid4(),
            shared_user_ids=[str(user.id)],
            shared_user_permissions={str(user.id): "config_with_chats"},
        )
        assert await self._suites(user, agent, ["inspect_my_traces"]) == {"inspect_my_traces"}
