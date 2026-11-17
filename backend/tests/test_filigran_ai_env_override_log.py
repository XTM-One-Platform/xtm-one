"""Regression test for the Filigran AI env-override log.

The ``FILIGRAN_AI_BASE_URL_STIX`` / ``FILIGRAN_AI_BASE_URL_TTP``
environment variables silently override the per-integration
``extra_config`` values inside ``_get_token``.  Before this fix the
operator who configured the integration UI saw no indication their
value was being ignored — they'd just observe HTTP requests going to
the env-configured host.

The fix logs at INFO level when (and only when) the override is
actually in effect AND the integration row carries a different value.
Specifically:

* No log when no env override is set.
* No log when env and integration agree (silent silent agreement).
* No log when only the env is set (no override-of-existing event).
* INFO log per-URL when env != integration value AND both non-empty.

This test exercises ``_get_token`` end-to-end against a fake DB +
fake decrypt + monkeypatched settings, asserting the log behaviour
for each of those branches.
"""

from __future__ import annotations

import logging
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

import app.services.integration_tools as integration_tools

_OWNER_ID = uuid.uuid4()


@pytest.fixture
def _stub_decrypt(monkeypatch):
    """Replace ``decrypt_json`` so the test controls the
    ``extra_config`` dict that ``_get_token`` will see.
    """

    extra_state: dict[str, Any] = {}

    def _fake_decrypt(_blob):
        return dict(extra_state)

    monkeypatch.setattr(integration_tools, "decrypt_json", _fake_decrypt)

    async def _fake_reveal_extra(_integ):
        return dict(extra_state)

    # The tools read extra_config through the vault resolver (#6080).
    monkeypatch.setattr(integration_tools, "_reveal_extra", _fake_reveal_extra)
    return extra_state


@pytest.fixture
def _stub_settings(monkeypatch):
    """Yield a SimpleNamespace masquerading as the settings object so
    each test can dial the two env vars.  We have to patch the
    ``settings`` symbol re-imported inside ``_get_token`` rather than
    the top-level module attribute (the function does ``from
    app.core.config import settings as _settings``).
    """
    fake_settings = SimpleNamespace(
        filigran_ai_base_url_stix="",
        filigran_ai_base_url_ttp="",
    )

    # ``_get_token`` does a local import (``from app.core.config import
    # settings``) so we must patch the symbol on the source module
    # rather than any re-export.  ``raising`` is left at its default
    # (True) so a future rename of the ``settings`` attribute fails
    # this fixture loudly instead of silently no-oping and turning
    # every test that consumes it into a green misfire.
    monkeypatch.setattr("app.core.config.settings", fake_settings)
    return fake_settings


class _FakeIntegration:
    def __init__(
        self,
        *,
        provider: str,
        extra_config_enc: bytes,
        name: str = "filigran-ai-prod",
        access_token_enc: bytes = b"encrypted",
        token_expires_at: Any = None,
    ):
        self.id = uuid.uuid4()
        self.name = name
        self.provider = provider
        self.extra_config_enc = extra_config_enc
        self.access_token_enc = access_token_enc
        self.token_expires_at = token_expires_at
        # Owned by the acting user of the test call: an OAuth token only
        # resolves for its owner.
        self.user_id = _OWNER_ID
        self.scope_kind = None
        self.scope_id = None
        self.binding_metadata = None
        # ``_get_token`` reads several diagnostic fields off the
        # Integration row to enrich structured logging — surface them
        # here so the test fixture doesn't trip an AttributeError
        # before reaching the override-logging block.
        self.auth_type = "oauth"
        self.is_company_managed = False
        self.refresh_token_enc = None
        self.token_meta = None
        self.installation_id = None


class _FakeDB:
    """Minimal AsyncSession stub that returns a pre-seeded integration
    row on ``execute(...).scalar_one_or_none()``.
    """

    def __init__(self, integ):
        self._integ = integ

    async def execute(self, _statement):
        return SimpleNamespace(scalar_one_or_none=lambda: self._integ)

    async def commit(self):
        return None

    async def rollback(self):
        return None


@pytest.fixture
def _call_get_token(monkeypatch, _stub_decrypt, _stub_settings):
    """Invoke ``_get_token`` with a fake DB+integration so the test
    only has to dial the override scenario.
    """

    # Bypass token decryption — return a fixed string so the function
    # gets to the post-decode override-logging block.
    monkeypatch.setattr(
        integration_tools,
        "decrypt_value",
        lambda _blob: "fake-access-token",
    )
    # Skip the refresh logic entirely. ``_try_sm_jwt_refresh`` was deleted
    # alongside the SaaS Manager provider — we only need to stub
    # the generic ``_try_inline_refresh`` path that ``_get_token`` now
    # walks for every OAuth provider.
    async def _no_refresh(*_args, **_kwargs):
        return None

    monkeypatch.setattr(integration_tools, "_try_inline_refresh", _no_refresh)

    async def _runner(
        *,
        env_stix: str,
        env_ttp: str,
        extra_stix: str,
        extra_ttp: str,
    ) -> str | None:
        _stub_settings.filigran_ai_base_url_stix = env_stix
        _stub_settings.filigran_ai_base_url_ttp = env_ttp
        _stub_decrypt.clear()
        _stub_decrypt["base_url_stix"] = extra_stix
        _stub_decrypt["base_url_ttp"] = extra_ttp

        integ = _FakeIntegration(
            provider="filigran_ai",
            extra_config_enc=b"some-encrypted-blob",
        )
        db = _FakeDB(integ)
        return await integration_tools._get_token(
            db,  # type: ignore[arg-type]
            str(integ.id),
            provider="filigran_ai",
            user_id=str(_OWNER_ID),
        )

    return _runner


@pytest.mark.asyncio
async def test_logs_info_when_env_override_diverges_from_integration_stix(
    _call_get_token, caplog: pytest.LogCaptureFixture
):
    with caplog.at_level(logging.INFO, logger=integration_tools.logger.name):
        await _call_get_token(
            env_stix="https://env.stix.example.com",
            env_ttp="",
            extra_stix="https://integration.stix.example.com",
            extra_ttp="https://integration.ttp.example.com",
        )

    override_records = [
        r
        for r in caplog.records
        if r.levelname == "INFO"
        and "filigran_ai" in r.getMessage().lower()
        and "overridden" in r.getMessage().lower()
        and "stix" in r.getMessage().lower()
    ]
    assert override_records, (
        "an INFO log is required when FILIGRAN_AI_BASE_URL_STIX env "
        "differs from the integration's extra_config value — without "
        "it the operator who configured the integration UI sees no "
        "indication their value was ignored.  Observed: "
        f"{[r.getMessage() for r in caplog.records]}"
    )


@pytest.mark.asyncio
async def test_no_log_when_env_and_integration_agree(
    _call_get_token, caplog: pytest.LogCaptureFixture
):
    """Silent agreement — when both sides carry the same URL,
    no operator action is needed and a log line would be noise.
    """
    same_url = "https://same.example.com"
    with caplog.at_level(logging.INFO, logger=integration_tools.logger.name):
        await _call_get_token(
            env_stix=same_url,
            env_ttp=same_url,
            extra_stix=same_url,
            extra_ttp=same_url,
        )

    override_records = [
        r
        for r in caplog.records
        if "overridden" in r.getMessage().lower()
    ]
    assert not override_records, (
        "no override log should fire when env and integration row "
        f"agree; observed {[r.getMessage() for r in override_records]}"
    )


@pytest.mark.asyncio
async def test_no_log_when_only_env_is_set(
    _call_get_token, caplog: pytest.LogCaptureFixture
):
    """If only the env is set and the integration row has no value
    for that URL, the env isn't "overriding" anything — it's the
    sole source.  No log should fire.
    """
    with caplog.at_level(logging.INFO, logger=integration_tools.logger.name):
        await _call_get_token(
            env_stix="https://env.stix.example.com",
            env_ttp="",
            extra_stix="",
            extra_ttp="https://integration.ttp.example.com",
        )

    override_records = [
        r
        for r in caplog.records
        if "overridden" in r.getMessage().lower()
    ]
    assert not override_records, (
        "no override log should fire when the integration row has no "
        f"value to override; observed {[r.getMessage() for r in override_records]}"
    )


@pytest.mark.asyncio
async def test_logs_info_for_both_urls_when_both_diverge(
    _call_get_token, caplog: pytest.LogCaptureFixture
):
    """Both URLs diverging should produce TWO INFO records (one per
    URL).  A single combined log would be harder for operators to
    grep and would couple unrelated override events.
    """
    with caplog.at_level(logging.INFO, logger=integration_tools.logger.name):
        await _call_get_token(
            env_stix="https://env.stix.example.com",
            env_ttp="https://env.ttp.example.com",
            extra_stix="https://integration.stix.example.com",
            extra_ttp="https://integration.ttp.example.com",
        )

    override_records = [
        r
        for r in caplog.records
        if "overridden" in r.getMessage().lower()
    ]
    assert len(override_records) == 2, (
        f"expected exactly 2 override records (one per URL); observed "
        f"{len(override_records)}: {[r.getMessage() for r in override_records]}"
    )
    # Each record should name its specific URL field so the operator
    # can find and correct the right env var.
    assert any("base_url_stix" in r.getMessage() for r in override_records)
    assert any("base_url_ttp" in r.getMessage() for r in override_records)


@pytest.mark.asyncio
async def test_no_log_when_only_trailing_slash_differs(
    _call_get_token, caplog: pytest.LogCaptureFixture
):
    """Cosmetic trailing-slash difference is NOT a real divergence.

    The effective ``base_url_*`` value is computed by ``rstrip("/")``,
    so ``https://host/`` and ``https://host`` resolve to the same
    target.  Without normalising trailing slashes on both sides of
    the override-detection comparison, an operator who configured
    the env var with a trailing slash (and the integration row
    without — or vice versa) would see an INFO log on every
    ``_get_token`` invocation, drowning out real override events
    in steady-state traffic.

    This test exercises both directions (env-has-slash / row-bare,
    and row-has-slash / env-bare) AND both URL fields, so a future
    refactor that re-introduces a raw-string compare on either side
    fails loudly.
    """
    scenarios = [
        # env trails, row bare
        dict(
            env_stix="https://same.example.com/",
            env_ttp="https://same.ttp.example.com/",
            extra_stix="https://same.example.com",
            extra_ttp="https://same.ttp.example.com",
        ),
        # row trails, env bare (the other direction)
        dict(
            env_stix="https://same.example.com",
            env_ttp="https://same.ttp.example.com",
            extra_stix="https://same.example.com/",
            extra_ttp="https://same.ttp.example.com/",
        ),
        # both trail (different lengths still normalise to the
        # same host)
        dict(
            env_stix="https://same.example.com//",
            env_ttp="https://same.ttp.example.com/",
            extra_stix="https://same.example.com",
            extra_ttp="https://same.ttp.example.com//",
        ),
    ]
    for scenario in scenarios:
        caplog.clear()
        with caplog.at_level(logging.INFO, logger=integration_tools.logger.name):
            await _call_get_token(**scenario)

        override_records = [
            r
            for r in caplog.records
            if "overridden" in r.getMessage().lower()
        ]
        assert not override_records, (
            "no override log should fire for cosmetic trailing-slash "
            f"differences; scenario={scenario}, observed="
            f"{[r.getMessage() for r in override_records]}"
        )
