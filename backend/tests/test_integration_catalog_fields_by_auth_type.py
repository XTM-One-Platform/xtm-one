"""The catalog serves the credential fields of each authentication mode (#2317).

A provider with several auth types (GitHub: OAuth app or a personal access token)
used to serve only the fields of its default mode, so the creation form kept
asking for a Client ID and Secret after "API key" was picked. ``fields_by_auth_type``
names the fields of every mode; ``fields`` stays those of the default one.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from app.api.v1.integration_catalog import (
    PROVIDER_CATALOG,
    credential_fields_by_auth_type,
    supported_auth_types,
)
from app.api.v1.schemas.integration import ProviderCatalogItem


def _entry(provider: str) -> dict[str, Any]:
    return cast(dict[str, Any], PROVIDER_CATALOG[provider])


def test_github_api_key_asks_for_the_token_and_oauth_for_the_client() -> None:
    fields = credential_fields_by_auth_type(_entry("github"))
    assert fields == {"oauth": ["client_id", "client_secret"], "api_key": ["api_key"]}
    # The default mode's fields are served as before.
    assert _entry("github")["fields"] == ["client_id", "client_secret"]


def test_a_single_mode_provider_serves_none() -> None:
    assert credential_fields_by_auth_type({"auth_type": "api_key", "fields": ["api_key"]}) is None
    assert credential_fields_by_auth_type({"auth_type": "oauth", "supported_auth_types": ["oauth"]}) is None
    assert credential_fields_by_auth_type(None) is None


def test_modes_without_credentials_ask_for_none() -> None:
    assert credential_fields_by_auth_type(_entry("opencrq")) == {"api_key": ["api_key"], "platform_managed": []}
    assert credential_fields_by_auth_type(_entry("xtm_hub")) == {"none": [], "api_key": ["api_key"]}


def test_a_self_registering_oauth_client_asks_for_nothing() -> None:
    assert credential_fields_by_auth_type(_entry("wordsmith")) == {"api_key": ["api_key"], "oauth": []}


@pytest.mark.parametrize(
    "provider",
    [key for key in PROVIDER_CATALOG if len(supported_auth_types(_entry(key))) > 1],
)
def test_every_multi_mode_provider_has_fields_for_each_mode(provider: str) -> None:
    entry = _entry(provider)
    fields = credential_fields_by_auth_type(entry)
    assert fields is not None
    assert list(fields) == supported_auth_types(entry)
    for mode in ("api_key", "bot_token"):
        if mode in fields:
            assert fields[mode] == ["api_key"], provider
    if "oauth" in fields and not entry.get("oauth_registration"):
        assert fields["oauth"] == ["client_id", "client_secret"], provider


def test_the_response_model_keeps_the_per_mode_fields() -> None:
    entry = _entry("github")
    payload: dict[str, Any] = {**entry, "fields_by_auth_type": credential_fields_by_auth_type(entry)}
    item = ProviderCatalogItem(provider="github", **payload)
    assert item.model_dump()["fields_by_auth_type"]["api_key"] == ["api_key"]
