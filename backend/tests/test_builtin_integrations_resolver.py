"""Regression tests for the XTM One built-in integrations resolver.

These tests pin the behaviour of the built-in integration seeding:

- Filigran AI is always seeded with hardcoded default URLs,
- Env vars FILIGRAN_AI_BASE_URL_STIX / _TTP override the defaults,
- Google Search is only seeded when the API key env var is set,
- whitespace-only env vars must not seed Google Search,
- Brave Search wins over Google when both keys are set (one built-in web
  search), Google stays the fallback,
- the integration-names log helper must never iterate the credential dicts,
- the startup-status log must only fire when seeding is enabled (i.e. it
  lives in the ``log_builtin_integration_status`` helper and is invoked
  from the seed pathway, not at import time).
"""

from __future__ import annotations

import importlib
import logging
from typing import Any

import pytest


@pytest.fixture
def reload_builtin_agents(monkeypatch):
    """Re-import ``app.agents.builtin_agents`` with patched env settings.

    The module evaluates ``_resolve_builtin_integrations()`` once at import
    time, so the integration list reflects the values of
    ``settings.builtin_*`` at first import. To exercise different env
    configurations we patch the singleton ``settings`` instance and force
    a reload of the module.
    """

    def _reload(
        *,
        google_key: str = "",
        brave_key: str = "",
        filigran_ai_stix_url: str = "",
        filigran_ai_ttp_url: str = "",
    ):
        from app.core import config as config_module

        monkeypatch.setattr(
            config_module.settings, "builtin_google_search_api_key", google_key
        )
        monkeypatch.setattr(
            config_module.settings, "builtin_brave_search_api_key", brave_key
        )
        monkeypatch.setattr(
            config_module.settings, "filigran_ai_base_url_stix", filigran_ai_stix_url
        )
        monkeypatch.setattr(
            config_module.settings, "filigran_ai_base_url_ttp", filigran_ai_ttp_url
        )

        builtin_agents = importlib.import_module("app.agents.builtin_agents")
        return importlib.reload(builtin_agents)

    return _reload


def test_resolver_always_seeds_filigran_ai(reload_builtin_agents):
    """Filigran AI is always seeded with hardcoded default URLs."""
    mod = reload_builtin_agents()

    metadata, credentials = mod._resolve_builtin_integrations()
    # Filigran AI should always be present
    filigran_entries = [m for m in metadata if m["provider"] == "filigran_ai"]
    assert len(filigran_entries) == 1
    assert "filigran_ai" in credentials
    assert credentials["filigran_ai"]["extra_config"]["base_url_stix"] == mod.FILIGRAN_AI_DEFAULT_STIX_URL
    assert credentials["filigran_ai"]["extra_config"]["base_url_ttp"] == mod.FILIGRAN_AI_DEFAULT_TTP_URL


def test_resolver_no_google_when_unconfigured(reload_builtin_agents):
    """Google Search env var empty → Google not seeded, but Filigran AI still is."""
    mod = reload_builtin_agents()

    metadata, credentials = mod._resolve_builtin_integrations()
    google_entries = [m for m in metadata if m["provider"] == "google_search"]
    assert google_entries == []
    assert "google_search" not in credentials
    # Filigran AI is always there
    filigran_entries = [m for m in metadata if m["provider"] == "filigran_ai"]
    assert len(filigran_entries) == 1


def test_resolver_seeds_google_when_configured(reload_builtin_agents):
    mod = reload_builtin_agents(google_key="search-id||my-api-key")

    metadata, credentials = mod._resolve_builtin_integrations()
    google_entries = [m for m in metadata if m["provider"] == "google_search"]
    assert len(google_entries) == 1
    # CRITICAL: secrets must NOT live in the iterated metadata dict.
    assert "api_key" not in google_entries[0]
    assert credentials["google_search"]["api_key"] == "search-id||my-api-key"


def test_resolver_strips_whitespace_only_google_key(reload_builtin_agents):
    """Whitespace-only value must not seed (and must not stash credentials)."""
    mod = reload_builtin_agents(google_key="   \t\n  ")

    metadata, credentials = mod._resolve_builtin_integrations()
    google_entries = [m for m in metadata if m["provider"] == "google_search"]
    assert google_entries == []
    assert "google_search" not in credentials


def test_resolver_strips_surrounding_whitespace_in_google_key(reload_builtin_agents):
    """Real value with leading/trailing whitespace is still accepted but
    the stored api_key is the stripped form (no invalid embedded ws)."""
    mod = reload_builtin_agents(google_key="\n  search-id||valid-key  \n")

    metadata, credentials = mod._resolve_builtin_integrations()
    google_entries = [m for m in metadata if m["provider"] == "google_search"]
    assert len(google_entries) == 1
    assert credentials["google_search"]["api_key"] == "search-id||valid-key"


def _providers(metadata: list[dict[str, Any]]) -> list[str]:
    return [m["provider"] for m in metadata]


def test_resolver_seeds_brave_alone_when_only_brave_is_set(reload_builtin_agents: Any) -> None:
    mod = reload_builtin_agents(brave_key="  BSA-key ")

    metadata, credentials = mod._resolve_builtin_integrations()
    assert "brave_search" in _providers(metadata)
    assert "google_search" not in _providers(metadata)
    assert credentials["brave_search"] == {"api_key": "BSA-key"}
    brave = next(m for m in metadata if m["provider"] == "brave_search")
    assert "api_key" not in brave
    assert "opencti-assistant" in brave["bind_to_agent_slugs"]


def test_resolver_prefers_brave_over_google(reload_builtin_agents: Any) -> None:
    """Both keys set: one built-in web search, Brave; Google is not seeded."""
    mod = reload_builtin_agents(brave_key="BSA-key", google_key="cx||AIza")

    metadata, credentials = mod._resolve_builtin_integrations()
    assert "brave_search" in _providers(metadata)
    assert "google_search" not in _providers(metadata)
    assert "google_search" not in credentials


def test_resolver_falls_back_to_google_without_brave(reload_builtin_agents: Any) -> None:
    mod = reload_builtin_agents(brave_key="   ", google_key="cx||AIza")

    metadata, credentials = mod._resolve_builtin_integrations()
    assert "google_search" in _providers(metadata)
    assert "brave_search" not in _providers(metadata)
    assert "brave_search" not in credentials


def test_log_status_warns_on_whitespace_brave_key(
    reload_builtin_agents: Any, caplog: pytest.LogCaptureFixture
) -> None:
    mod = reload_builtin_agents(brave_key="  ")

    caplog.set_level(logging.WARNING, logger="app.agents.builtin_agents")
    mod.log_builtin_integration_status()
    assert "BUILTIN_BRAVE_SEARCH_API_KEY" in caplog.text


def test_builtin_integrations_metadata_contains_no_credentials(
    reload_builtin_agents,
):
    """The exported ``BUILTIN_INTEGRATIONS`` list (the one seed.py
    iterates over) must never contain ``api_key`` / ``extra_config``
    fields. Locking this invariant guards the CodeQL taint surface."""
    mod = reload_builtin_agents(
        google_key="search-id||SECRET-API-KEY-DO-NOT-LEAK",
    )

    for data in mod.BUILTIN_INTEGRATIONS:
        assert "api_key" not in data, f"api_key leaked into metadata for {data}"
        assert "extra_config" not in data, f"extra_config leaked into metadata for {data}"


def test_get_builtin_integration_credentials_returns_empty_for_unknown(
    reload_builtin_agents,
):
    mod = reload_builtin_agents()
    assert mod.get_builtin_integration_credentials("nonexistent") == {}


def test_log_status_uses_static_display_names_not_credential_dicts(
    reload_builtin_agents, caplog
):
    """The startup status log must only emit hardcoded display names from
    the static map, never values reachable from the credentials map.
    Guards the CodeQL clear-text-logging finding from regressing."""
    mod = reload_builtin_agents(
        google_key="search-id||SECRET-API-KEY-DO-NOT-LOG",
    )

    caplog.set_level(logging.INFO, logger="app.agents.builtin_agents")
    mod.log_builtin_integration_status()

    log_text = caplog.text
    assert "Google Search" in log_text
    assert "Filigran AI" in log_text
    # CodeQL safety guard: none of these credential-derived strings can
    # appear in the captured log output.
    assert "SECRET-API-KEY-DO-NOT-LOG" not in log_text


def test_log_status_emits_whitespace_google_key_warning(
    reload_builtin_agents, caplog
):
    mod = reload_builtin_agents(google_key="   \t   ")

    caplog.set_level(logging.WARNING, logger="app.agents.builtin_agents")
    mod.log_builtin_integration_status()

    warning_records = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("BUILTIN_GOOGLE_SEARCH_API_KEY" in w for w in warning_records)


def test_log_status_is_not_emitted_at_module_import(
    reload_builtin_agents, caplog
):
    """``log_builtin_integration_status`` must not be auto-invoked at
    import time; only the seed pathway calls it. Reloading the module
    must not produce any log records from this module."""
    caplog.set_level(logging.DEBUG, logger="app.agents.builtin_agents")
    reload_builtin_agents()
    module_logs = [
        rec for rec in caplog.records if rec.name == "app.agents.builtin_agents"
    ]
    assert module_logs == []


def test_filigran_ai_env_var_overrides_defaults(reload_builtin_agents):
    """Env vars FILIGRAN_AI_BASE_URL_STIX / _TTP override hardcoded defaults."""
    custom_stix = "https://custom-stix.example.com"
    custom_ttp = "https://custom-ttp.example.com"
    mod = reload_builtin_agents(
        filigran_ai_stix_url=custom_stix,
        filigran_ai_ttp_url=custom_ttp,
    )

    _, credentials = mod._resolve_builtin_integrations()
    assert credentials["filigran_ai"]["extra_config"]["base_url_stix"] == custom_stix
    assert credentials["filigran_ai"]["extra_config"]["base_url_ttp"] == custom_ttp


def test_filigran_ai_partial_env_var_override(reload_builtin_agents):
    """Only one env var set — the other falls back to the hardcoded default."""
    custom_stix = "https://custom-stix.example.com"
    mod = reload_builtin_agents(filigran_ai_stix_url=custom_stix)

    _, credentials = mod._resolve_builtin_integrations()
    assert credentials["filigran_ai"]["extra_config"]["base_url_stix"] == custom_stix
    assert credentials["filigran_ai"]["extra_config"]["base_url_ttp"] == mod.FILIGRAN_AI_DEFAULT_TTP_URL

