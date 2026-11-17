"""The web chat's agent picker and the sessions started from it - ``uv run pytest``.

``GET /api/v1/web/chat/agents`` offers, in order: the General Assistant unless
an agent stands in for it, the agents open to chat by name, the chat-flows
(behind ``FLOW_BUILDER``), then the assignment-only agents;
``POST /api/v1/web/chat/sessions`` starts a session from any entry. An agent
carries its badges (company-managed, standing in for the General Assistant),
and the badges and headings are in the reader's language.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.v1 import chat as chat_module
from app.api.v1 import chat_web_stream
from app.services import general_assistant_link


def _agent(
    name: str,
    *,
    slug: str | None = None,
    disable_chat: bool = False,
    tools: tuple[str, ...] = (),
    company: bool = False,
):
    return SimpleNamespace(
        id=uuid.uuid4(),
        name=name,
        slug=slug or name.lower().replace(" ", "-"),
        icon="Bot",
        description=None,
        disable_chat=disable_chat,
        builtin_tool_ids=list(tools),
        builtin_allowed_tools=None,
        is_company_managed=company,
    )


def _flow(name: str, agent_id: str):
    return SimpleNamespace(flow_id=str(uuid.uuid4()), flow_name=name, description=None, agent_id=agent_id)


class _Db:
    """Answers every query with *rows*: the agents, or the one agent a slug names;
    a scalar read with the General Assistant's platform tools."""

    def __init__(self, rows, ga_tools: list[str] | None = None):
        self.rows = rows
        self.ga_tools = ga_tools

    async def execute(self, _stmt):
        rows, ga_tools = self.rows, self.ga_tools

        class _R:
            def scalars(self):
                return SimpleNamespace(all=lambda: list(rows))

            def scalar_one_or_none(self):
                return rows[0] if rows else None

            def scalar(self):
                return ga_tools

        return _R()


@pytest.fixture
def picker(monkeypatch):
    from app.api.v1 import helpers
    from app.services import channel_notices

    state = SimpleNamespace(stand_in=None, flows=[], platform_language=None)

    async def _groups(_db, _uid):
        return []

    async def _stand_in(_db):
        return state.stand_in

    async def _flows(_db, _user):
        return state.flows

    monkeypatch.setattr(helpers, "get_user_group_ids", _groups)
    monkeypatch.setattr(general_assistant_link, "general_assistant_stand_in", _stand_in)
    monkeypatch.setattr(chat_web_stream, "_flow_chat_entries", _flows)
    monkeypatch.setattr(channel_notices, "platform_language", lambda: state.platform_language)
    return state


async def test_the_picker_lists_the_general_assistant_agents_flows_then_assignment_agents(picker):
    analyst, scoping = _agent("Analyst"), _agent("Scoping only", disable_chat=True)
    picker.flows = [_flow("Triage flow", str(analyst.id))]

    entries = await chat_web_stream.list_web_chat_agents(
        db=_Db([analyst, scoping]), user=SimpleNamespace(id=uuid.uuid4())
    )

    assert [(e.name, e.group) for e in entries] == [
        ("General Assistant", None),
        ("Analyst", None),
        ("Triage flow", chat_web_stream.FLOWS_GROUP),
        ("Scoping only", chat_web_stream.NOT_FOR_CHAT_GROUP),
    ]
    # The panel flags the assignment agent in the picker and warns in the composer.
    assert [e.disable_chat for e in entries] == [False, False, False, True]
    assert entries[0].id == entries[0].slug == chat_web_stream.GENERAL_ASSISTANT_ENTRY
    assert entries[2].id == entries[2].slug == f"flow:{picker.flows[0].flow_id}"


async def test_an_agent_standing_in_for_the_general_assistant_replaces_its_entry(picker):
    stand_in = _agent("XTM One Assistant")
    picker.stand_in = stand_in

    entries = await chat_web_stream.list_web_chat_agents(db=_Db([stand_in]), user=SimpleNamespace(id=uuid.uuid4()))

    assert [e.name for e in entries] == ["XTM One Assistant"]


async def test_an_agent_carries_its_badges_company_managed_and_standing_in(picker):
    stand_in, company, plain = (
        _agent("XTM One Assistant", company=True),
        _agent("Company bot", company=True),
        _agent("Mine"),
    )
    picker.stand_in = stand_in

    entries = await chat_web_stream.list_web_chat_agents(
        db=_Db([company, plain, stand_in]), user=SimpleNamespace(id=uuid.uuid4())
    )

    assert {e.name: e.badges for e in entries} == {
        "Company bot": ["Company"],
        "Mine": [],
        "XTM One Assistant": ["Assistant", "Company"],
    }


@pytest.mark.parametrize(
    ("user_language", "platform_language", "accept_language", "expected"),
    [
        # The user's own preference first, whatever the browser says.
        ("fr-fr", "es", "ja-JP,ja;q=0.9", ("Entreprise", "Flows", "Non recommandé pour le chat")),
        # Then the platform's default.
        ("auto", "es", "ja-JP,ja;q=0.9", ("Empresa", "Flujos", "No recomendado para chat")),
        # Then the browser's first language the catalog holds.
        ("auto", None, "de-DE,ja-JP;q=0.8,fr;q=0.5", ("会社", "フロー", "チャットには非推奨")),
        # Else English.
        ("auto", None, "de-DE", ("Company", "Flows", "Not recommended for chat")),
        (None, None, None, ("Company", "Flows", "Not recommended for chat")),
    ],
)
async def test_badges_and_headings_are_in_the_readers_language(
    picker, user_language, platform_language, accept_language, expected
):
    company, scoping = _agent("Company bot", company=True), _agent("Scoping only", disable_chat=True)
    picker.flows = [_flow("Triage flow", str(company.id))]
    picker.platform_language = platform_language

    entries = await chat_web_stream.list_web_chat_agents(
        db=_Db([company, scoping]),
        user=SimpleNamespace(id=uuid.uuid4(), language=user_language),
        accept_language=accept_language,
    )

    by_name = {e.name: e for e in entries}
    assert (by_name["Company bot"].badges[0], by_name["Triage flow"].group, by_name["Scoping only"].group) == expected


def _user(ga_tools: list[str] | None = None):
    return SimpleNamespace(id=uuid.uuid4(), ga_builtin_tool_ids=ga_tools)


async def test_a_studio_rail_offers_only_the_chat_agents_carrying_its_tool(picker):
    writer = _agent("Deck writer", tools=("generate_presentation",))
    editor = _agent("Video editor", tools=("video_studio",))
    hidden = _agent("Scoping deck", disable_chat=True, tools=("generate_presentation",))
    picker.flows = [_flow("Triage flow", str(writer.id))]

    decks = await chat_web_stream.list_web_chat_agents(
        db=_Db([writer, editor, hidden], ga_tools=[]), user=_user(), studio="presentation"
    )
    videos = await chat_web_stream.list_web_chat_agents(
        db=_Db([writer, editor, hidden], ga_tools=[]), user=_user(), studio="video"
    )

    assert [e.name for e in decks] == ["Deck writer"]
    assert [e.name for e in videos] == ["Video editor"]


async def test_the_fleet_rail_offers_the_agents_whose_platform_tool_keeps_update_fleet(picker):
    """The fleet page's Modify with AI: the platform group with its ``update_fleet`` function (#10162)."""
    whole = _agent("Fleet keeper", tools=("platform",))
    narrowed = _agent("Fleet editor", tools=("platform",))
    narrowed.builtin_allowed_tools = {"platform": ["list_fleets", "update_fleet"]}
    reader = _agent("Fleet reader", tools=("platform",))
    reader.builtin_allowed_tools = {"platform": ["list_fleets"]}

    fleets = await chat_web_stream.list_web_chat_agents(
        db=_Db([whole, narrowed, reader], ga_tools=["platform"]), user=_user(), studio="fleet"
    )

    assert [e.name for e in fleets] == ["General Assistant", "Fleet keeper", "Fleet editor"]


async def test_a_studio_rail_offers_the_general_assistant_when_its_tools_carry_the_studio_tool(picker):
    writer = _agent("Deck writer", tools=("generate_presentation",))

    platform = await chat_web_stream.list_web_chat_agents(
        db=_Db([writer], ga_tools=["generate_presentation"]), user=_user(), studio="presentation"
    )
    # The user's own General Assistant tools override the platform's, both ways.
    overridden_off = await chat_web_stream.list_web_chat_agents(
        db=_Db([writer], ga_tools=["generate_presentation"]), user=_user([]), studio="presentation"
    )
    overridden_on = await chat_web_stream.list_web_chat_agents(
        db=_Db([writer], ga_tools=[]), user=_user(["video_studio"]), studio="video"
    )

    assert [e.id for e in platform] == [chat_web_stream.GENERAL_ASSISTANT_ENTRY, str(writer.id)]
    assert [e.name for e in overridden_off] == ["Deck writer"]
    assert [e.id for e in overridden_on] == [chat_web_stream.GENERAL_ASSISTANT_ENTRY]


async def test_a_studio_rail_never_offers_the_general_assistant_an_agent_stands_in_for(picker):
    stand_in = _agent("XTM One Assistant", tools=("generate_presentation",))
    picker.stand_in = stand_in

    entries = await chat_web_stream.list_web_chat_agents(
        db=_Db([stand_in], ga_tools=["generate_presentation"]), user=_user(), studio="presentation"
    )

    assert [e.name for e in entries] == ["XTM One Assistant"]


@pytest.fixture
def created(monkeypatch):
    calls: list[chat_module.ConversationCreate] = []

    async def _create(*, body, user, db, _chat_gate):
        calls.append(body)
        return SimpleNamespace(id=str(uuid.uuid4()), agent_id=None, workspace_id=body.workspace_id, channel="web")

    async def _chat_available(_db):
        return None

    monkeypatch.setattr(chat_module, "create_conversation", _create)
    monkeypatch.setattr(chat_web_stream, "require_chat_enabled", _chat_available)
    return calls


async def _open(db, slug: str):
    body = chat_web_stream.WebChatSessionRequest(agent_slug=slug)
    return await chat_web_stream.open_web_chat_session(body, db=db, user=SimpleNamespace(id=uuid.uuid4()))


async def test_a_session_from_the_general_assistant_entry_has_no_agent(picker, created):
    await _open(_Db([]), chat_web_stream.GENERAL_ASSISTANT_ENTRY)

    assert (created[0].agent_id, created[0].title) == (None, None)


async def test_a_session_from_a_flow_binds_its_agent_under_the_flow_name(picker, created):
    flow = _flow("Triage flow", str(uuid.uuid4()))
    picker.flows = [flow]

    await _open(_Db([]), f"flow:{flow.flow_id}")

    assert (created[0].agent_id, created[0].title) == (flow.agent_id, "Triage flow")


async def test_a_flow_the_caller_cannot_open_is_404(picker, created):
    with pytest.raises(HTTPException) as exc:
        await _open(_Db([]), f"flow:{uuid.uuid4()}")
    assert exc.value.status_code == 404
    assert created == []


async def test_a_session_from_an_agent_binds_it_assignment_only_ones_included(picker, created):
    scoping = _agent("Scoping only", disable_chat=True)

    await _open(_Db([scoping]), scoping.slug)

    assert created[0].agent_id == str(scoping.id)


async def test_a_studio_rail_session_is_created_bound_to_its_subject(picker, created):
    deck, video = str(uuid.uuid4()), str(uuid.uuid4())
    body = chat_web_stream.WebChatSessionRequest(agent_slug=chat_web_stream.GENERAL_ASSISTANT_ENTRY)

    await chat_web_stream.open_web_chat_session(body, db=_Db([]), user=_user(), presentation_id=deck)
    await chat_web_stream.open_web_chat_session(body, db=_Db([]), user=_user(), video_project_id=video)

    assert (created[0].presentation_id, created[0].video_project_id) == (deck, None)
    assert (created[1].presentation_id, created[1].video_project_id) == (None, video)
