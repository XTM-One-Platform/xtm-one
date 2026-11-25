"""Deep Investigation stays behind ``DEEP_INVESTIGATION``.

Unlike ``FLOW_BUILDER`` the flag is an enforcement boundary, not presentation:
off, the ``/api/v1/investigations`` routes answer 403, the chat mode is an
unknown one, the ``deep_investigation`` tool is never built, and the seeded
Deep Investigation Agent - with every reference another built-in makes to it -
is not seeded. An agent that already carries the tool id keeps it in its
configuration: the flag decides what runs, not what is stored.

The frontend has no unit-test harness for these screens, so its half is
pinned by source assertions, as in ``test_flow_builder_feature_flag.py``. The
chat routing is exercised through the real socket in
``test_deep_investigation_turn_route.py``.

See ``dev-docs/deep-investigation-feature-flag.md``.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from app.agents import seed as seed_module
from app.agents.builtin_agents import BUILTIN_AGENTS, FEATURE_GATED_BUILTIN_AGENTS
from app.core.feature_flags import KNOWN_FEATURE_FLAGS, is_feature_enabled

BACKEND = Path(__file__).resolve().parent.parent
FRONTEND = BACKEND.parent / "frontend"

FLAG = "DEEP_INVESTIGATION"
AGENT_SLUG = "deep-investigation-agent"
AGENT_NAME = "Deep Investigation Agent"

requires_flag_off = pytest.mark.skipif(
    is_feature_enabled(FLAG),
    reason=f"ENABLED_FEATURES has {FLAG} on; these assert the flag-off shape",
)


def _squash(text: str) -> str:
    return " ".join(text.split())


def _has(source: str, snippet: str) -> bool:
    return _squash(snippet) in _squash(source)


def _read_backend(*parts: str) -> str:
    return BACKEND.joinpath(*parts).read_text(encoding="utf-8")


def _read_frontend(*parts: str) -> str:
    return FRONTEND.joinpath(*parts).read_text(encoding="utf-8")


def _agent(agents: list[dict], slug: str) -> dict | None:
    return next((a for a in agents if a["slug"] == slug), None)


def _referrers() -> list[dict]:
    return [a for a in BUILTIN_AGENTS if AGENT_SLUG in (a.get("handover_agent_slugs") or [])]


def test_the_flag_is_in_both_registries():
    assert FLAG in KNOWN_FEATURE_FLAGS
    assert f'"{FLAG}",' in _read_frontend("src", "lib", "utils.ts")
    assert FEATURE_GATED_BUILTIN_AGENTS[AGENT_SLUG] == FLAG


@requires_flag_off
async def test_the_investigations_api_answers_403_and_is_unpublished():
    """The flag's own 403 answers before authentication, and no investigation
    operation is in the published schema."""
    from app.main import application

    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        for path in (
            "/api/v1/investigations",
            "/api/v1/investigations/source-packs?subject=APT28",
            "/api/v1/investigations/00000000-0000-0000-0000-000000000001",
        ):
            response = await client.get(path)
            assert response.status_code == 403, path
            assert FLAG in response.json()["detail"], path

    leaked = [p for p in application.openapi()["paths"] if p.startswith("/api/v1/investigations")]
    assert not leaked, f"investigation endpoints still published: {leaked}"


@requires_flag_off
async def test_the_catalog_withholds_the_tool_but_the_registry_keeps_it():
    """Skill-package validation reads the registry's keys, so the id must stay
    valid there; the endpoint an integrator reads must not offer it."""
    from app.api.v1.settings import get_builtin_tool_catalog
    from app.services.builtin_tool_catalog import build_builtin_tool_catalog

    assert "deep_investigation" in build_builtin_tool_catalog(include_disabled=True)
    assert "deep_investigation" not in await get_builtin_tool_catalog(_user=None)


def test_the_tool_is_only_built_with_the_flag_on():
    gate = 'if "deep_investigation" in agent_builtin_tool_ids and is_feature_enabled("DEEP_INVESTIGATION"):'
    for module in ("agent_chat.py", "assignment_engine.py"):
        assert _has(_read_backend("app", "services", module), gate), module
    assert _has(
        _read_backend("app", "api", "v1", "chat.py"),
        '_turn_deep_investigation = data.get("mode") == "deep_investigation" and is_feature_enabled(',
    )


@requires_flag_off
def test_the_seed_withholds_the_agent_and_every_reference_to_it():
    seeded = seed_module._seeded_builtin_agents()

    assert _agent(seeded, AGENT_SLUG) is None
    referrers = _referrers()
    assert referrers, "a built-in hands over to the agent; the test would prove nothing"
    for original in referrers:
        agent = _agent(seeded, original["slug"])
        assert agent is not None
        assert AGENT_SLUG not in agent["handover_agent_slugs"]
        assert AGENT_SLUG not in (agent.get("handover_type_slugs") or {})
        assert AGENT_SLUG not in (agent.get("handover_condition_slugs") or {})
        assert AGENT_NAME not in agent["persona"]
        assert "reproduce the citations exactly" not in agent["persona"]
        # Every other handover and specialist is kept.
        assert agent["handover_agent_slugs"] == [s for s in original["handover_agent_slugs"] if s != AGENT_SLUG]
        assert "**OpenCTI Assistant**" in agent["persona"]

    # The declaration itself is untouched: the flag decides what is seeded.
    assert _agent(BUILTIN_AGENTS, AGENT_SLUG) is not None
    assert all(AGENT_SLUG in a["handover_agent_slugs"] for a in _referrers())


def test_with_the_flag_on_the_seed_is_the_declaration(monkeypatch):
    monkeypatch.setattr(seed_module, "is_feature_enabled", lambda _flag: True)
    # Every built-in fleet seeded (none withheld by the fleet switch, #2105).
    monkeypatch.setattr("app.agents.builtin_fleets.UNSEEDED_FLEET_AGENT_SLUGS", frozenset())
    assert seed_module._seeded_builtin_agents() is BUILTIN_AGENTS


def test_a_withheld_specialist_is_removed_with_its_continuation_lines():
    persona = (
        "You route requests.\n"
        "- **A Assistant** - for A.\n"
        "- **Gone Agent** - CONSULT it.\n"
        "  - reproduce its citations.\n"
        "\tand keep them.\n"
        "- **B Assistant** - for B:\n"
        "  - an example\n"
        "\n"
        "Closing words.\n"
    )
    assert seed_module._without_specialists(persona, ["Gone Agent"]) == (
        "You route requests.\n"
        "- **A Assistant** - for A.\n"
        "- **B Assistant** - for B:\n"
        "  - an example\n"
        "\n"
        "Closing words.\n"
    )


def test_the_intent_bindings_skip_a_withheld_agent():
    source = _read_backend("app", "agents", "seed.py")
    assert _has(source, "withheld_slugs = _withheld_builtin_agent_slugs()")
    assert _has(source, "agents = [a for a in _seeded_builtin_agents() if seed_matches_platform(a, platform_mode)]")


def test_the_frontend_offers_nothing_with_the_flag_off():
    # The card's gate is served by the backend registry.
    from app.services.builtin_tool_groups import BUILTIN_TOOL_GROUPS

    entry = next(g for g in BUILTIN_TOOL_GROUPS if g.id == "deep_investigation")
    assert entry.gate == "deep_investigation"
    assert _has(_read_frontend("src", "types", "index.ts"), '"flow_builder" | "deep_investigation"')

    assert _has(
        _read_frontend("src", "hooks", "useBuiltinTools.ts"),
        "deep_investigation: deepInvestigationEnabled,",
    )
    chat = _read_frontend("src", "pages", "ChatPanelPage.tsx")
    assert _has(chat, 'const deepInvestigationEnabled = useFeatureFlag("DEEP_INVESTIGATION");')
    assert _has(
        chat,
        "deepInvestigationEnabled ? () => setDeepInvestigation((on) => !on) : undefined",
    )
    assert _has(
        _read_frontend("src", "components", "chat-panel", "InlineFileContent.tsx"),
        "deepInvestigationEnabled ? refs.investigations : []",
    )
    config_tab = _read_frontend("src", "pages", "mcp-server-detail", "ConfigTab.tsx")
    assert _has(
        config_tab,
        "deepInvestigationEnabled ? (srv.investigation_tool_name ?? null) : null",
    )
    assert _has(config_tab, "{canManage && deepInvestigationEnabled && (")
