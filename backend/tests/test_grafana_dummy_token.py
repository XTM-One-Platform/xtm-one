"""Regression test for Grafana dummy-token warning.

Verifies that ``build_dummy_token("grafana")`` produces the compound URL
token required by the Grafana provider and that introspecting tools with
that token neither emits a warning nor returns an empty list.

Mirrors the pattern used in ``test_sekoia.py::test_catalog_dummy_token_yields_all_tools``
to prevent future regressions on any code path that calls provider
``build_tools()`` for catalog / pipeline-tool introspection.
"""

from __future__ import annotations

import logging

from app.api.v1.integration_catalog import build_dummy_token
from app.integrations.providers.grafana import build_tools
from app.tools.base import CallableToolExecutor


def test_grafana_in_url_dummy_providers():
    """grafana must be registered in _URL_DUMMY_PROVIDERS so that
    build_dummy_token returns a compound 'base_url||api_key' token
    instead of the plain '__dummy__' that would trigger the warning."""
    from app.api.v1.integration_catalog import _URL_DUMMY_PROVIDERS

    assert "grafana" in _URL_DUMMY_PROVIDERS, (
        "grafana is missing from _URL_DUMMY_PROVIDERS — "
        "catalog introspection will log a warning and return no tools"
    )


def test_build_dummy_token_grafana_is_compound():
    """build_dummy_token('grafana') must return a compound 'base_url||api_key'
    string so that the Grafana provider can parse it without emitting a warning."""
    token = build_dummy_token("grafana")
    assert "||" in token, (
        f"Expected a compound 'base_url||api_key' token for grafana, got {token!r}"
    )
    base_url, api_key = token.split("||", 1)
    assert base_url, "base_url component of Grafana dummy token must not be empty"
    assert api_key, "api_key component of Grafana dummy token must not be empty"


def test_catalog_dummy_token_yields_all_grafana_tools(caplog):
    """Catalog introspection via build_dummy_token must return all Grafana tools
    without logging a warning.

    Regression test for the bug where ``list_pipeline_tools`` (and any similar
    code path) called ``mod.build_tools('__probe__', ...)`` instead of
    ``mod.build_tools(build_dummy_token(provider), ...)``, causing the
    Grafana provider to log a warning and return an empty list on every fetch.
    """
    with caplog.at_level(logging.WARNING, logger="app.integrations.providers.grafana"):
        tools = build_tools(
            token=build_dummy_token("grafana"), executor=CallableToolExecutor()
        )

    assert len(tools) >= 29, (
        f"Expected at least 29 Grafana tools from dummy-token introspection, "
        f"got {len(tools)}"
    )
    tool_names = {t.name for t in tools}
    assert "get_grafana_org" in tool_names
    assert "list_grafana_dashboards" in tool_names
    assert "query_loki_logs" in tool_names
    assert "list_grafana_alerts" in tool_names

    grafana_warnings = [
        r for r in caplog.records
        if r.levelno >= logging.WARNING and "Grafana token missing" in r.message
    ]
    assert not grafana_warnings, (
        "build_tools with build_dummy_token('grafana') must not log a warning; "
        f"got: {[r.message for r in grafana_warnings]}"
    )


def test_plain_probe_token_triggers_warning(caplog):
    """Passing the old '__probe__' sentinel (without a URL) must trigger the
    Grafana warning and return no tools — confirming the bug that was fixed."""
    with caplog.at_level(logging.WARNING, logger="app.integrations.providers.grafana"):
        tools = build_tools(token="__probe__", executor=CallableToolExecutor())

    assert tools == [], "Plain '__probe__' token should return no tools for Grafana"
    grafana_warnings = [
        r for r in caplog.records
        if r.levelno >= logging.WARNING and "Grafana token missing" in r.message
    ]
    assert grafana_warnings, (
        "build_tools with '__probe__' token should log the missing-token warning"
    )
