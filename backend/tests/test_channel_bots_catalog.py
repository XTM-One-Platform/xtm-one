"""Regression tests for the channel-bot catalog endpoint.

These tests live separately from the channel-router and bot CRUD suites
because they cover the catalog resolver specifically — the spot that
substitutes ``{base_url}`` / ``{APP_NAME}`` into setup-guide steps and
the Teams ``manifest_example`` JSON template.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.api.v1 import channel_bots as channel_bots_api


class _FakeResult:
    def __init__(self, scalar: object | None = None):
        self._scalar = scalar

    def scalar_one_or_none(self):
        return self._scalar

    def scalar_one(self):
        return self._scalar


class _FakeSession:
    def __init__(self, ps: object | None = None):
        self._ps = ps

    async def execute(self, _statement):
        return _FakeResult(self._ps)


@pytest.mark.asyncio
async def test_catalog_imports_get_platform_settings_from_helpers_module():
    """Regression: an earlier rev imported ``get_platform_settings`` from
    a module that doesn't exist (``app.services.platform_settings``).
    Calling the endpoint would have raised ``ModuleNotFoundError`` before
    returning any rows.  The helper lives at
    ``app.api.v1.platform_settings_helpers`` — this assertion guards
    against a regression of the wrong import path."""
    from app.api.v1 import platform_settings_helpers

    assert hasattr(platform_settings_helpers, "get_platform_settings")
    # Importing the route module itself must not blow up either.
    assert hasattr(channel_bots_api, "list_channel_bot_catalog")


@pytest.mark.asyncio
async def test_catalog_substitutes_app_name_into_manifest_example(monkeypatch):
    """``{APP_NAME}`` in the Teams ``manifest_example`` template is
    replaced with the resolved platform name (admin override preferred
    over the mode default).  Setup-guide ``steps`` for the Teams
    provider don't currently use ``{APP_NAME}`` placeholders directly,
    so the assertion targets the manifest template where the
    substitution lives in practice."""
    monkeypatch.setattr(
        channel_bots_api,
        "get_cached_branding",
        lambda: SimpleNamespace(name="Acme Brand"),
    )

    items = await channel_bots_api.list_channel_bot_catalog(
        _user=SimpleNamespace(id=uuid4()), db=_FakeSession()
    )

    teams = next(item for item in items if item.provider == "teams_bot")
    assert teams.setup_guide is not None
    manifest_example = teams.setup_guide.manifest_example
    assert manifest_example is not None
    parsed = json.loads(manifest_example)
    # The manifest template contains ``"My {APP_NAME} Bot"`` /
    # ``"powered by {APP_NAME}"`` — both must now reflect the
    # admin-configured brand instead of the raw placeholder.
    assert "Acme Brand" in parsed["name"]["short"]
    assert "Acme Brand" in parsed["description"]["full"]
    assert "{APP_NAME}" not in manifest_example


@pytest.mark.asyncio
async def test_teams_manifest_example_is_one_teams_accepts_without_any_deployment_value(
    monkeypatch,
):
    """The template a self-hoster starts from: schema 1.29 with
    ``supportsChannelFeatures: tier1`` and never ``supportedChannelTypes``
    (Teams rejects a package that declares both), files on, the three RSC
    permissions the setup guide lists, and only placeholders for the app
    id and domain."""
    monkeypatch.setattr(
        channel_bots_api,
        "get_cached_branding",
        lambda: SimpleNamespace(name="Acme Brand"),
    )

    items = await channel_bots_api.list_channel_bot_catalog(
        _user=SimpleNamespace(id=uuid4()), db=_FakeSession()
    )

    teams = next(item for item in items if item.provider == "teams_bot")
    manifest_example = teams.setup_guide.manifest_example
    parsed = json.loads(manifest_example)
    assert parsed["manifestVersion"] == "1.29"
    assert "/v1.29/" in parsed["$schema"]
    assert parsed["supportsChannelFeatures"] == "tier1"
    assert "supportedChannelTypes" not in parsed
    assert parsed["bots"][0]["supportsFiles"] is True
    assert parsed["id"] == parsed["bots"][0]["botId"] == "<YOUR_APP_ID>"
    rsc = {
        p["name"] for p in parsed["authorization"]["permissions"]["resourceSpecific"]
    }
    assert rsc == {
        "ChannelMessage.Read.Group",
        "ChatMessage.Read.Chat",
        "ChannelMember.Read.Group",
    }
    listed = {p.scope.removesuffix(" (RSC)") for p in teams.setup_guide.permissions}
    assert rsc <= listed
    assert "filigran" not in manifest_example.lower()


@pytest.mark.asyncio
async def test_catalog_json_escapes_app_name_in_manifest_example(monkeypatch):
    """An admin-customised platform name containing ``"``, ``\\`` or
    control characters must not break the Teams ``manifest_example``
    JSON template.  The substitution uses ``json.dumps`` to escape the
    fragment before splicing it into the existing JSON string literal."""
    monkeypatch.setattr(
        channel_bots_api,
        "get_cached_branding",
        lambda: SimpleNamespace(name='Acme "Edge" \\ Co'),
    )

    items = await channel_bots_api.list_channel_bot_catalog(
        _user=SimpleNamespace(id=uuid4()), db=_FakeSession()
    )

    teams = next(item for item in items if item.provider == "teams_bot")
    assert teams.setup_guide is not None
    manifest_example = teams.setup_guide.manifest_example
    assert manifest_example is not None

    # Must still parse as JSON — would fail with the pre-fix
    # ``str.replace`` substitution because the raw ``"`` and ``\``
    # characters would corrupt the surrounding JSON literal.
    parsed = json.loads(manifest_example)
    # And the parsed name field should round-trip the original value
    # (not the escaped form).
    assert "Acme \"Edge\" \\ Co" in parsed["name"]["short"]


@pytest.mark.asyncio
async def test_catalog_falls_back_to_branding_name_when_platform_name_unset(
    monkeypatch,
):
    """When the admin hasn't customised the brand, the catalog uses the
    branding name resolved via ``get_cached_branding`` (DB > env >
    mode-default)."""
    monkeypatch.setattr(
        channel_bots_api,
        "get_cached_branding",
        lambda: SimpleNamespace(name="Fallback Brand"),
    )

    expected_fallback = "Fallback Brand"

    items = await channel_bots_api.list_channel_bot_catalog(
        _user=SimpleNamespace(id=uuid4()), db=_FakeSession()
    )

    teams = next(item for item in items if item.provider == "teams_bot")
    assert teams.setup_guide is not None
    manifest_example = teams.setup_guide.manifest_example
    assert manifest_example is not None
    parsed = json.loads(manifest_example)
    assert expected_fallback in parsed["name"]["short"]
    assert "{APP_NAME}" not in manifest_example


@pytest.mark.asyncio
async def test_catalog_passes_the_open_source_credit_through(monkeypatch):
    """A channel guide's ``open_source`` credit reaches the shared dialog, not dropped."""
    teams = dict(channel_bots_api.CHANNEL_BOT_CATALOG["teams_bot"])
    teams["setup_guide"] = {
        **teams["setup_guide"],
        "open_source": {"name": "Upstream", "url": "https://example.org/upstream"},
    }
    monkeypatch.setattr(channel_bots_api, "CHANNEL_BOT_CATALOG", {"teams_bot": teams})

    items = await channel_bots_api.list_channel_bot_catalog(
        _user=SimpleNamespace(id=uuid4()), db=_FakeSession()
    )

    credit = items[0].setup_guide.open_source
    assert credit is not None
    assert (credit.name, credit.url) == ("Upstream", "https://example.org/upstream")
