"""No default URL points at a Filigran deployment or a private address.

An installation that sets nothing must not trust, link to or send OAuth
redirects to our own instance or a developer's machine: every default stays on
localhost, and a missing ``BASE_URL`` is said at startup.
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

import pytest

from app.core.config import Settings

URL_DEFAULTS = ("base_url", "frontend_url", "cors_origins")


def _default_urls() -> list[tuple[str, str]]:
    urls = []
    for name in URL_DEFAULTS:
        default = Settings.model_fields[name].default
        urls += [(name, url) for url in (default if isinstance(default, list) else [default])]
    return urls


@pytest.mark.parametrize(("setting", "url"), _default_urls(), ids=lambda value: str(value))
def test_no_default_url_names_an_internal_host(setting: str, url: str) -> None:
    host = urlsplit(url).hostname or ""
    assert "filigran" not in host, f"{setting} defaults to a Filigran host: {url}"
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        assert host == "localhost", f"{setting} defaults to a host other than localhost: {url}"
    else:
        assert address.is_loopback, f"{setting} defaults to a non-loopback address: {url}"


@pytest.mark.parametrize("overrides", [{}, {"base_url": ""}, {"base_url": "  "}], ids=["unset", "empty", "blank"])
def test_an_unset_or_empty_base_url_is_said_at_startup(monkeypatch, caplog, overrides) -> None:
    monkeypatch.delenv("BASE_URL", raising=False)
    monkeypatch.delenv("DEBUG", raising=False)
    with caplog.at_level("WARNING", logger="app.core.config"):
        Settings(_env_file=None, **overrides)
    assert any("BASE_URL is not set" in record.getMessage() for record in caplog.records)


@pytest.mark.parametrize("overrides", [{"base_url": "https://xtm.example.com"}, {"debug": True}], ids=["set", "debug"])
def test_a_set_base_url_or_debug_mode_is_not_warned_about(monkeypatch, caplog, overrides) -> None:
    monkeypatch.delenv("BASE_URL", raising=False)
    monkeypatch.delenv("DEBUG", raising=False)
    with caplog.at_level("WARNING", logger="app.core.config"):
        Settings(_env_file=None, **overrides)
    assert not any("BASE_URL is not set" in record.getMessage() for record in caplog.records)
