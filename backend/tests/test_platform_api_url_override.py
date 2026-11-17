"""The ``{PROVIDER}_API_URL`` overrides and the deprecated OPENGRC_API_URL.

An operator can point XTM One at a platform's internal API URL with
``OPENCTI_API_URL`` / ``OPENAEV_API_URL`` / ``OPENCRQ_API_URL``. The OpenCRQ
rename renamed its variable; the old ``OPENGRC_API_URL`` keeps working
until ``LEGACY_API_URL_REMOVAL_DATE``, the new name wins, and the old one is
warned about once per process (registration is also the heartbeat).
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1 import helpers as helpers_api
from app.api.v1 import platform
from app.core.config import settings
from app.core.security import decrypt_value
from app.enterprise.licensing import clear_license_cache
from app.models.integration import Integration

_ALL_VARS = ("OPENCTI_API_URL", "OPENAEV_API_URL", "OPENCRQ_API_URL", "OPENGRC_API_URL")
_TOKEN = "test-registration-token"
# The cache key ``resolve_xtm_license`` resolves through.
_XTM_CACHE_KEY = "xtm"
_REPO_ROOT = Path(__file__).resolve().parents[2]
# What an operator reads the removal date in.
_REMOVAL_ANNOUNCEMENTS = (
    ".env.sample",
    "docs/docs/deployment/upgrade.md",
    "docs/docs/deployment/xtm-suite-integration.md",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in _ALL_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(platform, "_warned_legacy_api_url_vars", set())


def _deprecation_warnings(caplog) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING and "deprecated" in r.getMessage()
    ]


def test_no_override_returns_the_current_name():
    assert platform._resolve_api_url_override("opencrq") == ("", "OPENCRQ_API_URL")


@pytest.mark.parametrize("provider", ["opencti", "openaev"])
def test_other_platforms_read_their_own_variable(monkeypatch, provider):
    var = f"{provider.upper()}_API_URL"
    monkeypatch.setenv(var, " http://internal:8080 ")
    monkeypatch.setenv("OPENGRC_API_URL", "http://legacy:4000")
    assert platform._resolve_api_url_override(provider) == ("http://internal:8080", var)


def test_the_new_name_wins_when_both_are_set(monkeypatch, caplog):
    monkeypatch.setenv("OPENCRQ_API_URL", "http://opencrq:4000")
    monkeypatch.setenv("OPENGRC_API_URL", "http://opengrc:4000")
    with caplog.at_level(logging.WARNING):
        result = platform._resolve_api_url_override("opencrq")
    assert result == ("http://opencrq:4000", "OPENCRQ_API_URL")
    assert _deprecation_warnings(caplog) == []


def test_the_old_name_alone_is_honoured_and_warned_about_once(monkeypatch, caplog):
    monkeypatch.setenv("OPENGRC_API_URL", "http://opengrc:4000")
    with caplog.at_level(logging.WARNING):
        first = platform._resolve_api_url_override("opencrq")
        # Registration is also the heartbeat: the next beats stay quiet.
        second = platform._resolve_api_url_override("opencrq")
    assert first == second == ("http://opengrc:4000", "OPENGRC_API_URL")
    warnings = _deprecation_warnings(caplog)
    assert len(warnings) == 1
    assert "OPENGRC_API_URL" in warnings[0]
    assert "OPENCRQ_API_URL" in warnings[0]
    assert platform.LEGACY_API_URL_REMOVAL_DATE in warnings[0]


def test_a_blank_new_name_falls_through_to_the_old_one(monkeypatch):
    monkeypatch.setenv("OPENCRQ_API_URL", "   ")
    monkeypatch.setenv("OPENGRC_API_URL", "http://opengrc:4000")
    assert platform._resolve_api_url_override("opencrq") == (
        "http://opengrc:4000",
        "OPENGRC_API_URL",
    )


class _Result:
    def __init__(self, value: object) -> None:
        self._value = value

    def scalar_one_or_none(self) -> object:
        return self._value


class _Session:
    """Just enough ``AsyncSession`` for ``register_platform``: nothing is
    registered yet, and every row the route adds is kept."""

    def __init__(self) -> None:
        self.added: list[object] = []

    async def execute(self, _statement: object) -> _Result:
        return _Result(None)

    def add(self, obj: object) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        return None

    async def refresh(self, _obj: object) -> None:
        return None


@pytest.fixture
def register(monkeypatch):
    """``POST /api/v1/platform/register`` through the real route. Returns the
    ``base_url`` stored on the platform's integration: the URL every agent
    call to that platform goes to."""

    async def _no_intent_sync(*_args, **_kwargs) -> None:
        return None

    async def _no_owner(_db) -> None:
        return None

    async def _load_ps(_db) -> SimpleNamespace:
        return SimpleNamespace(
            enterprise_license=None,
            created_at=datetime.now(UTC) - timedelta(days=30),
        )

    monkeypatch.setattr(settings, "platform_registration_token", _TOKEN)
    monkeypatch.setattr(settings, "enterprise_license", "")
    monkeypatch.setattr(platform, "_load_ps", _load_ps)
    monkeypatch.setattr(platform, "_sync_platform_intents", _no_intent_sync)
    monkeypatch.setattr(helpers_api, "resolve_company_managed_owner_id", _no_owner)
    clear_license_cache(_XTM_CACHE_KEY)

    def _call(identifier: str) -> str:
        session = _Session()
        app = FastAPI()
        app.include_router(platform.router, prefix="/api/v1/platform")
        app.dependency_overrides[platform.get_db] = lambda: session
        response = TestClient(app).post(
            "/api/v1/platform/register",
            json={
                "platform_identifier": identifier,
                "platform_url": "https://platform.example",
            },
            headers={"Authorization": f"Bearer {_TOKEN}"},
        )
        assert response.status_code == 200, response.text
        (integration,) = [row for row in session.added if isinstance(row, Integration)]
        return json.loads(decrypt_value(integration.extra_config_enc))["base_url"]

    yield _call
    clear_license_cache(_XTM_CACHE_KEY)


@pytest.mark.parametrize("identifier", ["opencrq", "opengrc"])
def test_registration_calls_the_platform_on_the_old_name_and_warns_once(
    register, monkeypatch, caplog, identifier
):
    """Through the route: an OpenCRQ platform, or one still registering as
    OpenGRC, is called on the URL set under the old name, and the heartbeat
    that follows the registration stays quiet."""
    monkeypatch.setenv("OPENGRC_API_URL", "http://opengrc-internal:4000")
    with caplog.at_level(logging.WARNING):
        first = register(identifier)
        second = register(identifier)
    assert first == second == "http://opengrc-internal:4000"
    assert len(_deprecation_warnings(caplog)) == 1


@pytest.mark.parametrize("document", _REMOVAL_ANNOUNCEMENTS)
def test_every_announcement_names_the_removal_date_of_the_code(document):
    """Operators plan against the date they read. Moving
    ``LEGACY_API_URL_REMOVAL_DATE`` (to postpone the removal the tripwire
    below demands, say) must move every announcement with it."""
    text = (_REPO_ROOT / document).read_text(encoding="utf-8")
    assert "OPENGRC_API_URL" in text
    assert platform.LEGACY_API_URL_REMOVAL_DATE in text, (
        f"{document} announces another removal date for OPENGRC_API_URL than "
        f"LEGACY_API_URL_REMOVAL_DATE ({platform.LEGACY_API_URL_REMOVAL_DATE})"
    )


def test_the_legacy_alias_is_removed_after_its_date():
    """Tripwire: the shim is temporary. Once the date passes, delete the
    legacy loop in ``_resolve_api_url_override`` and its tests here, and the
    notes in .env.sample, docs/docs/deployment/upgrade.md and
    xtm-suite-integration.md."""
    removal = date.fromisoformat(platform.LEGACY_API_URL_REMOVAL_DATE)
    assert date.today() <= removal, (
        "OPENGRC_API_URL was due for removal on "
        f"{platform.LEGACY_API_URL_REMOVAL_DATE}: remove the legacy fallback"
    )
