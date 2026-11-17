"""The Certified qualification of the integration catalog (#12004).

The catalog card's corner ribbon reads ``is_certified`` from the catalog the
backend serves, which reads ``CERTIFIED_PROVIDERS``: the one list. These tests
pin that set, so certifying or un-certifying a provider is a reviewed change
of this file, and check every key still names a catalog entry.
"""

from __future__ import annotations

from app.api.v1.integration_catalog import (
    CERTIFIED_PROVIDERS,
    PROVIDER_CATALOG,
    is_certified_provider,
)
from app.api.v1.schemas.integration import ProviderCatalogItem

EXPECTED_CERTIFIED = frozenset(
    {
        # Filigran products and services.
        "opencti",
        "openaev",
        "opencrq",
        "xtm_hub",
        # The integrations the internal agent fleets run on every day.
        "ashby",
        "concord",
        "docusign",
        "drata",
        "figma",
        "github",
        "google",
        "google_search",
        "grafana",
        "hibob",
        "hubspot",
        "jira",
        "letsignit",
        "linkedin",
        "metabase",
        "microsoft",
        "notion",
        "openapi",
        "perk",
        "planhat",
        "slack",
        "whatsapp",
        "defender",
        "entra",
        "hackerone",
        "intune",
        "purview",
    }
)


def test_the_certified_set_is_pinned():
    assert CERTIFIED_PROVIDERS == EXPECTED_CERTIFIED
    assert len(CERTIFIED_PROVIDERS) == 31


def test_every_certified_key_is_a_catalog_entry():
    missing = sorted(CERTIFIED_PROVIDERS - PROVIDER_CATALOG.keys())
    assert missing == [], f"certified keys absent from PROVIDER_CATALOG: {missing}"


def test_every_other_catalog_entry_stays_preview():
    preview = PROVIDER_CATALOG.keys() - CERTIFIED_PROVIDERS
    assert preview, "the catalog keeps Preview providers"
    for key in preview:
        assert not is_certified_provider(key), key
    # Out of scope by decision: the inbound partner channel and the model
    # providers keep their current Preview ribbon.
    for key in ("oauth_a2a", "anthropic", "openai", "gitlab", "zendesk", "n8n"):
        assert key in PROVIDER_CATALOG
        assert not is_certified_provider(key), key


def test_a_custom_provider_is_never_certified():
    assert not is_certified_provider("custom:acme")


def test_the_catalog_item_defaults_to_not_certified():
    item = ProviderCatalogItem(provider="slack", **PROVIDER_CATALOG["slack"])
    assert item.is_certified is False
    assert "is_certified" in ProviderCatalogItem.model_fields
