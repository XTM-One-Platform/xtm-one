"""The tools list has to say what pauses, and who can change it (FR-045).

Four groups, because the group decides who can act: a builtin is on the shipped
list, a custom tool is this deployment's own, a platform read was classified
rather than chosen, and everything else — MCP and integration tools — can never
be on the shipped list at all. That last group is the one an operator has to
rule on, which is why it is reported separately rather than mixed in.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.api.v1 import hitl_whitelist as api
from app.services.hitl_whitelist import ACTION_ADD, ACTION_REMOVE
from app.tools.approval import REACH_ANY, REACH_PRODUCT

USER = SimpleNamespace(id=uuid.uuid4(), role="user", email="r@example.com")


class _DB:
    """Answers the custom-tool query, and keeps it so its scope can be read."""

    def __init__(self, custom=("my_script",)):
        self._custom = list(custom)
        self.statements: list = []

    async def execute(self, stmt):
        self.statements.append(stmt)
        rows = [SimpleNamespace(name=name) for name in self._custom]
        return SimpleNamespace(
            scalars=lambda: SimpleNamespace(all=lambda: rows)
        )

    async def scalar(self, _stmt):
        # ``tool_approvals_enabled``: False means the platform switch is off.
        return not getattr(self, "platform_off", False)


@pytest.fixture
def _catalogue(monkeypatch):
    import app.services.builtin_tool_catalog as builtin_tool_catalog

    monkeypatch.setattr(
        builtin_tool_catalog,
        "build_builtin_tool_catalog",
        lambda: {"agent_builder": [{"name": "list_agents"}, {"name": "update_agent"}]},
    )

    async def _reads(_db):
        return frozenset({"get_opencti_entity"})

    async def _exempt(_db, *, reach=None):
        # ``update_agent`` gates; the rest are exempt for three different reasons.
        # Records a reach as ``resolve_whitelist`` does for every name it exempts.
        if reach is not None:
            reach.setdefault("list_agents", set()).add(REACH_PRODUCT)
            reach.setdefault("get_opencti_entity", set()).add(REACH_PRODUCT)
            reach.setdefault("my_script", set()).add(REACH_ANY)
        return frozenset({"list_agents", "get_opencti_entity", "my_script"})

    async def _overrides(_db):
        return {"my_script": ACTION_ADD}

    async def _default(_db):
        # What the default exempts, before any override. ``update_agent`` is
        # absent: it is a write, gated because nothing ever exempted it.
        return frozenset({"list_agents", "get_opencti_entity"})

    async def _from_runs(_db, _user):
        return {"list_agents", "mcp_search_wiki", "who_knows"}

    async def _groups(_db, _user_id):
        return []

    async def _attribution(_db, _user):
        return (
            {"mcp_search_wiki": "Wiki server"},
            {"notion_create_page": "notion"},
            {"send_discord_bot_message": "discord_bot"},
            {"send_discord_bot_message": ["channel_bot:discord_bot:bot-1"]},
            {"notion_create_page": ["integration:notion:integ-1"]},
        )

    async def _no_switches(_db):
        return frozenset()

    monkeypatch.setattr(api, "_tool_names_from_visible_runs", _from_runs)
    monkeypatch.setattr(api, "_attribution", _attribution)
    monkeypatch.setattr(api, "resolve_exempt_sources", _no_switches)
    monkeypatch.setattr(api, "get_user_group_ids", _groups)
    monkeypatch.setattr(api, "platform_read_default", _reads)
    monkeypatch.setattr(api, "resolve_whitelist", _exempt)
    monkeypatch.setattr(api, "load_overrides", _overrides)
    monkeypatch.setattr(api, "default_membership", _default)


async def _catalog(db=None):
    result = await api.tool_gate_catalog(user=USER, db=db or _DB())
    return {t["tool_key"]: t for t in result["tools"]}, result


@pytest.mark.asyncio
async def test_every_source_layer_is_named(_catalogue):
    tools, _ = await _catalog()

    assert tools["list_agents"]["source"] == "builtin"
    assert tools["my_script"]["source"] == "custom"
    assert tools["get_opencti_entity"]["source"] == "platform"
    # Harvested from run history and then attributed, so the group FR-045
    # exists for can be read by type rather than as one undifferentiated pile.
    assert tools["mcp_search_wiki"]["source"] == "mcp"
    assert tools["mcp_search_wiki"]["provider"] == "Wiki server"
    assert tools["notion_create_page"]["source"] == "integration"
    assert tools["notion_create_page"]["provider"] == "notion"
    # Channel bots are their own model, contribute real tools, and have no
    # per-connection switch — so this list is the only place they are ruled on.
    assert tools["send_discord_bot_message"]["source"] == "channel"
    assert tools["send_discord_bot_message"]["provider"] == "discord_bot"
    # A tool that has run here and matches nothing stays visible, unattributed —
    # hiding it would hide exactly the tool nobody has ruled on.
    assert tools["who_knows"]["source"] == "other"


@pytest.mark.asyncio
async def test_a_tool_off_the_whitelist_reads_as_gating(_catalogue):
    tools, _ = await _catalog()

    assert tools["update_agent"]["gates"] is True
    assert tools["update_agent"]["exempt_source"] is None
    assert tools["list_agents"]["gates"] is False


@pytest.mark.asyncio
async def test_exemption_names_the_layer_it_came_from(_catalogue):
    """"Why is this exempt?" has three different answers, and an audit asks
    about exactly one of them."""
    tools, _ = await _catalog()

    assert tools["my_script"]["exempt_source"] == "admin_added"
    assert tools["get_opencti_entity"]["exempt_source"] == "platform_read"
    assert tools["get_opencti_entity"]["provider"] == "opencti"


@pytest.mark.asyncio
async def test_unreadable_registrations_are_reported_not_hidden(_catalogue, monkeypatch):
    """Unknown must not render as "this deployment has no platform tools"."""

    async def _boom(_db):
        raise api.PlatformReadsUnavailableError()

    monkeypatch.setattr(api, "platform_read_default", _boom)

    tools, result = await _catalog()

    assert result["platform_reads_available"] is False
    assert "get_opencti_entity" not in tools


@pytest.mark.asyncio
async def test_custom_tools_are_scoped_to_what_the_caller_may_see(_catalogue):
    """A flat listing named other people's private tools to everyone.

    The names alone are the disclosure — a user saw tools that are not shared
    with them and do not appear anywhere else on the page. Scoped exactly as
    ``/custom-tools`` scopes the list this sits beside.
    """
    db = _DB()
    await api.tool_gate_catalog(user=USER, db=db)

    sql = str(db.statements[0].compile(compile_kwargs={"literal_binds": True}))
    assert "custom_tools.user_id" in sql, "the custom tool query has no owner predicate"
    assert str(USER.id) in sql


@pytest.mark.asyncio
async def test_a_sanitised_alias_is_not_listed_as_a_second_tool(_catalogue, monkeypatch):
    """One tool, one row — under the name the gate actually matches.

    Run history records the spelling the model saw, which is sanitised for the
    providers: a custom tool "My tool" appears there as "My_tool". Listing both
    showed one tool twice, the second time under a heading saying nothing
    claimed it — and whitelisting that spelling would have done nothing, since
    ``evaluate_gate`` keys off the canonical name.
    """

    async def _from_runs(_db, _user):
        return {"My_tool", "list_agents"}

    monkeypatch.setattr(api, "_tool_names_from_visible_runs", _from_runs)

    tools, _ = await _catalog(_DB(custom=("My tool",)))

    assert tools["My tool"]["source"] == "custom"
    assert "My_tool" not in tools


@pytest.mark.asyncio
async def test_a_genuine_snake_case_tool_is_kept(_catalogue, monkeypatch):
    """The alias rule must not delete a real tool that merely looks like one."""

    async def _from_runs(_db, _user):
        return {"my_tool"}

    monkeypatch.setattr(api, "_tool_names_from_visible_runs", _from_runs)

    # "my tool" is registered AND "my_tool" has run: both are real names here.
    tools, _ = await _catalog(_DB(custom=("my tool", "my_tool")))

    assert tools["my tool"]["source"] == "custom"
    assert tools["my_tool"]["source"] == "custom"


@pytest.mark.asyncio
async def test_a_connected_provider_lists_every_tool_not_only_what_has_run(_catalogue):
    """The write half is the half that gates, and it was missing.

    Names were seeded from run history alone, so a connected OpenCTI showed its
    18 classified reads and none of its 45 gating writes until somebody had
    already called one — the exact rows an operator opens this screen to rule
    on. The attribution already knew them; it just was not listed.
    """
    tools, _ = await _catalog()

    # ``notion_create_page`` is attributed but never appears in run history.
    assert "notion_create_page" in tools
    assert tools["notion_create_page"]["source"] == "integration"
    assert tools["notion_create_page"]["gates"] is True


@pytest.mark.asyncio
async def test_a_connection_switch_shows_as_not_gating(_catalogue, monkeypatch):
    """The two screens have to agree.

    A bot's switch exempts by *source*; the whitelist answers by name. Reading
    only the whitelist meant an administrator flipped the switch, watched the
    tools list carry on saying the tool pauses, and had no way to tell which
    screen was lying — while the gate itself honoured the switch perfectly.
    """

    async def _bot_off(_db):
        return frozenset({"channel_bot:discord_bot:bot-1"})

    monkeypatch.setattr(api, "resolve_exempt_sources", _bot_off)

    tools, _ = await _catalog()

    assert tools["send_discord_bot_message"]["gates"] is False
    assert tools["send_discord_bot_message"]["exempt_source"] == "connection_switch"


@pytest.mark.asyncio
async def test_a_name_two_bots_provide_needs_both_switched_off(_catalogue, monkeypatch):
    """Each definition carries its own bot's id at runtime, so a mixed answer
    means some calls still pause — and the honest summary of that is that it
    pauses."""

    async def _attribution(_db, _user):
        return ({}, {}, {"send_discord_bot_message": "discord_bot"},
                {"send_discord_bot_message": [
                    "channel_bot:discord_bot:bot-1",
                    "channel_bot:discord_bot:bot-2",
                ]}, {})

    async def _one_off(_db):
        return frozenset({"channel_bot:discord_bot:bot-1"})

    monkeypatch.setattr(api, "_attribution", _attribution)
    monkeypatch.setattr(api, "resolve_exempt_sources", _one_off)

    tools, _ = await _catalog()

    assert tools["send_discord_bot_message"]["gates"] is True


@pytest.mark.asyncio
async def test_the_platform_switch_off_makes_everything_read_as_not_gating(_catalogue):
    """While it is off nothing pauses, so a row saying otherwise is wrong.

    Read once here rather than in each consumer: the badges on the cards, the
    tables, the tool pages and the gate section would each have had to remember
    the switch, and one forgetting is a screen that lies.
    """
    db = _DB()
    db.platform_off = True

    result = await api.tool_gate_catalog(user=USER, db=db)
    tools = {t["tool_key"]: t for t in result["tools"]}

    assert all(not t["gates"] for t in tools.values())
    assert tools["update_agent"]["exempt_source"] == "platform_switch"


@pytest.mark.asyncio
async def test_an_integration_switch_shows_as_not_gating(_catalogue, monkeypatch):
    """The same agreement the bot shape has, for the shape that changed.

    A connection's own switch emits ``integration:<provider>:<id>``; this page
    was still looking for the bare provider form, so an operator turned their
    Notion connection off, watched the row carry on saying it pauses, and had no
    way to tell which screen was lying — while the gate honoured the switch.
    """

    async def _one_off(_db):
        return frozenset({"integration:notion:integ-1"})

    monkeypatch.setattr(api, "resolve_exempt_sources", _one_off)

    tools, _ = await _catalog()

    assert tools["notion_create_page"]["gates"] is False
    assert tools["notion_create_page"]["exempt_source"] == "connection_switch"


@pytest.mark.asyncio
async def test_a_name_two_connections_provide_needs_both_switched_off(
    _catalogue, monkeypatch
):
    """One of three Notion connections exempted means calls through the other
    two still pause."""

    async def _attribution(_db, _user):
        return ({}, {"shared_tool": "notion"}, {}, {},
                {"shared_tool": ["integration:notion:a", "integration:notion:b"]})

    async def _one_off(_db):
        return frozenset({"integration:notion:a"})

    monkeypatch.setattr(api, "_attribution", _attribution)
    monkeypatch.setattr(api, "resolve_exempt_sources", _one_off)

    tools, _ = await _catalog()

    assert tools["shared_tool"]["gates"] is True


@pytest.mark.asyncio
async def test_a_re_gated_default_says_so_rather_than_looking_untouched(
    _catalogue, monkeypatch
):
    """"We ship this exempt — why does it prompt?" is answerable from the row.

    A removal override and a tool nobody ever exempted both read as ``gates``,
    and the control differs: one restores a shipped default, the other makes a
    new exemption.
    """

    async def _overrides(_db):
        return {"my_script": ACTION_ADD, "list_agents": ACTION_REMOVE}

    async def _exempt(_db, **_kw):
        return frozenset({"get_opencti_entity", "my_script"})

    monkeypatch.setattr(api, "load_overrides", _overrides)
    monkeypatch.setattr(api, "resolve_whitelist", _exempt)

    tools, _ = await _catalog()

    assert tools["list_agents"]["gates"] is True
    assert tools["list_agents"]["regated"] is True
    # Gated, but nobody re-gated it — it was never exempt to begin with.
    assert tools["update_agent"]["gates"] is True
    assert tools["update_agent"]["regated"] is False


@pytest.mark.asyncio
async def test_an_addition_nothing_provides_is_still_listed(_catalogue, monkeypatch):
    """A whitelisted name no connected thing offers still gets a row.

    Whitelisting a tool ahead of connecting its integration is supported, and a
    typo is the other way to get here. Neither is in ``known_tool_names``, so
    without this the exemption exists and appears on no screen — which is the
    one state this catalogue is for.
    """

    async def _overrides(_db):
        return {"my_script": ACTION_ADD, "toto": ACTION_ADD}

    async def _exempt(_db, **_kw):
        return frozenset({"list_agents", "get_opencti_entity", "my_script", "toto"})

    monkeypatch.setattr(api, "load_overrides", _overrides)
    monkeypatch.setattr(api, "resolve_whitelist", _exempt)

    tools, _ = await _catalog()

    assert tools["toto"]["gates"] is False
    assert tools["toto"]["exempt_source"] == "admin_added"
    assert tools["toto"]["source"] == "other"


@pytest.mark.asyncio
async def test_a_hand_gated_read_says_it_is_hand_gated(_catalogue, monkeypatch):
    """"It says Read, so why does it pause?" has to be answerable on the row.

    Nothing an operator controls explains this one: not the whitelist, not the
    switches. Only the product decided it, so only the product can say so.
    """

    # Sourced as an integration tool, which is what production produces: both
    # derivations subtract ``HAND_GATED`` before returning, so the name never
    # reaches ``platform_reads`` and the row exists only because an OpenCTI
    # integration is connected. Patching it into the platform reads instead
    # tested a state the product cannot reach.
    async def _attribution(_db, _user):
        return (
            {},
            {"download_opencti_file": "opencti", "notion_create_page": "notion"},
            {},
            {},
            {"download_opencti_file": ["integration:opencti:integ-9"]},
        )

    async def _exempt(_db, **_kw):
        # The hand-gated read is deliberately absent from the effective list.
        return frozenset({"list_agents", "get_opencti_entity", "my_script"})

    monkeypatch.setattr(api, "_attribution", _attribution)
    monkeypatch.setattr(api, "resolve_whitelist", _exempt)

    tools, _ = await _catalog()

    assert tools["download_opencti_file"]["source"] == "integration"
    assert tools["download_opencti_file"]["gates"] is True
    assert tools["download_opencti_file"]["hand_gated"] is True
    assert tools["get_opencti_entity"]["hand_gated"] is False


@pytest.mark.asyncio
async def test_a_removal_whose_tool_left_the_default_is_not_re_gated(
    _catalogue, monkeypatch
):
    """A stale ``remove`` override is not a re-gated default.

    The override outlives the membership that justified it — a platform
    disconnected, a tool reclassified as a write, a release that drops it. The
    row must not then offer to "restore the shipped default", because
    ``add_to_whitelist`` answers that by pinning an ``add`` and exempting the
    tool for good.
    """

    async def _overrides(_db):
        return {"my_script": ACTION_ADD, "who_knows": ACTION_REMOVE}

    async def _default(_db):
        # ``who_knows`` is not in it, so nothing exempts it any more.
        return frozenset({"list_agents", "get_opencti_entity"})

    monkeypatch.setattr(api, "load_overrides", _overrides)
    monkeypatch.setattr(api, "default_membership", _default)

    tools, _ = await _catalog()

    assert tools["who_knows"]["gates"] is True
    assert tools["who_knows"]["regated"] is False


@pytest.mark.asyncio
async def test_flow_builder_tools_are_withheld_while_the_flag_is_off(
    _catalogue, monkeypatch
):
    """The one screen every user reads must not announce the feature.

    ``get_builtin_tool_catalog`` withholds ``flow_builder`` behind the flag;
    this list built its builtin names straight from the builder, so thirteen
    sub-tools named an unannounced feature here.
    """
    import app.services.builtin_tool_catalog as builtin_tool_catalog

    monkeypatch.setattr(
        builtin_tool_catalog,
        "build_builtin_tool_catalog",
        lambda: {
            "agent_builder": [{"name": "list_agents"}],
            "flow_builder": [{"name": "create_flow"}],
        },
    )
    # Patched where it is imported from: the call site imports it inside the
    # function, so the module attribute is what resolves.
    import app.core.feature_flags as flags

    monkeypatch.setattr(flags, "is_feature_enabled", lambda _flag: False)

    tools, _ = await _catalog()

    assert "list_agents" in tools
    assert "create_flow" not in tools
