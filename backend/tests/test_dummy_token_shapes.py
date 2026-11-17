"""Every tool builder answers the dummy credential ``build_dummy_token`` gives it.

Tool introspection - the integration catalog, the per-integration tools
endpoint, the derived HITL whitelist, the channel-bot catalog, tool approvals -
calls each provider's builder with ``build_dummy_token(provider)`` purely to
read its ``ToolDefinition`` metadata. A compound-credential builder handed a
shape it does not parse returns no tools without raising, so the provider
silently disappeared from all of them: the catalog listed zero tools for
Intel 471, Recorded Future, TheHive and Tenable, their read tools kept asking
for approval, and the Teams and WhatsApp bot tool names were missing from the
names the MCP whitelist derivation must never exempt.
"""

from __future__ import annotations

import pytest

from app.api.v1.integration_catalog import build_dummy_token, build_provider_tools
from app.services.integration_tools import CHANNEL_BOT_TOOLS, PROVIDER_TOOLS
from app.tools.base import CallableToolExecutor

#: A builder whose tools come from data the dummy cannot carry: the OpenAPI
#: integration builds its tools from the spec each integration imported.
_NO_STATIC_TOOLS = {"openapi"}


def _tool_names(builder, provider: str) -> list[str]:
    return [
        d.name
        for d in builder(build_dummy_token(provider), CallableToolExecutor(), refresh_fn=lambda: None)
    ]


@pytest.mark.parametrize("provider", sorted(set(PROVIDER_TOOLS) - _NO_STATIC_TOOLS))
def test_every_integration_builder_answers_its_dummy_token(provider):
    assert _tool_names(PROVIDER_TOOLS[provider], provider), provider


@pytest.mark.parametrize("provider", sorted(CHANNEL_BOT_TOOLS))
def test_every_channel_bot_builder_answers_its_dummy_token(provider):
    assert _tool_names(CHANNEL_BOT_TOOLS[provider], provider), provider


@pytest.mark.parametrize(
    ("provider", "tool"),
    [
        ("intel471", "search_intel471_reports"),
        ("recorded_future", "lookup_rf_ip"),
        ("thehive", "search_thehive_cases"),
        ("tenable", "list_tenable_vulnerabilities"),
    ],
)
def test_the_integration_catalog_lists_the_compound_token_providers(provider, tool):
    assert tool in [t.name for t in build_provider_tools()[provider]]


def test_the_mcp_derivation_excludes_every_channel_bot_tool_name():
    from app.services import hitl_whitelist_derived

    hitl_whitelist_derived._CHANNEL_BOT_NAMES_CACHE = None
    try:
        names = hitl_whitelist_derived._channel_bot_tool_names()
    finally:
        hitl_whitelist_derived._CHANNEL_BOT_NAMES_CACHE = None
    for provider, builder in CHANNEL_BOT_TOOLS.items():
        tools = set(_tool_names(builder, provider))
        assert tools, provider
        assert tools <= names, provider
