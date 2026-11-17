"""BYOLLM: the administrator owns the LLM configuration, not the environment.

``BYOLLM_ACTIVATED=true`` is the deployment saying "bring your own LLM". It
resolves into two predicates on ``Settings`` that the rest of the codebase
reads instead of the raw flags, and it empties the env-managed lock set:

1. ``provider_models_admin_curated`` — catalogs of env-seeded providers are
   admin-curated (``LLM_PROVIDER_MODELS_EDITABLE`` is the other way to ask).
2. ``provider_management_env_only`` — provider management stays open, even on
   a deployment that still sets ``MODELS_ENV_PROVIDING_ONLY``.
3. ``_env_managed_ga_fields()`` returns nothing, so every Settings → AI and models → Models
   control is editable and ``update_general_assistant_config`` accepts it.

The seed-once half of the contract (``DEFAULT_*`` applied only while the
stored value is empty) runs against a real database in
``test_byollm_seed_once_postgres.py``.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterator
from types import ModuleType, SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.v1.settings import (
    _env_managed_ga_fields,
    _serialize_platform_settings,
)
from app.core.config import Settings
from app.core.config import settings as app_settings
from app.llm.env_models import models_env_locked
from app.services.llm_provider_management import (
    ensure_llm_provider_management_allowed,
)

# Every DEFAULT_* attribute that feeds the locked-field computation, so a value
# from the developer's own .env cannot make an assertion flaky.
_DEFAULT_ATTRS = {
    "default_model": "",
    "default_general_assistant_model": "",
    "default_reasoning_effort": "",
    "default_routing_model": "",
    "default_fallback_model": "",
    "default_embedding_provider": "",
    "default_embedding_model": "",
    "default_embedding_dimensions": None,
    "default_image_provider": "",
    "default_image_model": "",
}

_ALL_DEFAULTS_SET = {
    "default_model": "gpt-4.1",
    "default_general_assistant_model": "claude-sonnet-5",
    "default_reasoning_effort": "high",
    "default_routing_model": "gpt-4.1-mini",
    "default_fallback_model": "claude-haiku-4-5",
    "default_embedding_provider": "openai",
    "default_embedding_model": "text-embedding-3-large",
    "default_embedding_dimensions": 3072,
    "default_image_provider": "openai",
    "default_image_model": "gpt-image-1",
}


def _reset(monkeypatch, **overrides):
    for attr, value in {**_DEFAULT_ATTRS, **overrides}.items():
        monkeypatch.setattr(app_settings, attr, value)


# ---------------------------------------------------------------------------
# 1. The flag and the two resolved predicates
# ---------------------------------------------------------------------------


def test_byollm_is_off_by_default():
    """Opt-in: an existing deployment keeps env-pinned defaults until it says
    otherwise, so upgrading never silently hands the LLM configuration over."""
    assert Settings.model_fields["byollm_activated"].default is False


@pytest.mark.parametrize(
    ("editable", "byollm", "curated"),
    [
        (False, False, False),
        (True, False, True),
        (False, True, True),
        (True, True, True),
    ],
)
def test_provider_models_admin_curated_resolves_both_flags(editable, byollm, curated):
    """Either way of opting in curates the catalog; neither cancels the other."""
    assert (
        Settings(
            llm_provider_models_editable=editable, byollm_activated=byollm
        ).provider_models_admin_curated
        is curated
    )


@pytest.mark.parametrize(
    ("env_only", "byollm", "blocked"),
    [
        (False, False, False),
        (True, False, True),
        # The contradictory pair: BYOLLM is the narrower, more recent opt-in
        # and wins, so provider management stays open.
        (True, True, False),
        (False, True, False),
    ],
)
def test_provider_management_env_only_lets_byollm_win(env_only, byollm, blocked):
    assert (
        Settings(
            models_env_providing_only=env_only, byollm_activated=byollm
        ).provider_management_env_only
        is blocked
    )


# ---------------------------------------------------------------------------
# 2. The provider-management gate
# ---------------------------------------------------------------------------


def test_provider_management_is_blocked_by_env_only_mode(monkeypatch):
    monkeypatch.setattr(app_settings, "byollm_activated", False)
    monkeypatch.setattr(app_settings, "models_env_providing_only", True)

    with pytest.raises(HTTPException) as exc:
        ensure_llm_provider_management_allowed()

    assert exc.value.status_code == 403
    assert "MODELS_ENV_PROVIDING_ONLY=true" in str(exc.value.detail)


def test_byollm_reopens_provider_management_over_env_only_mode(monkeypatch):
    """The whole point of the feature: an admin can add their own provider and
    key from the UI even where the operator left ``MODELS_ENV_PROVIDING_ONLY``
    behind. One gate guards add / edit / rename / delete, so this covers all
    four verbs."""
    monkeypatch.setattr(app_settings, "byollm_activated", True)
    monkeypatch.setattr(app_settings, "models_env_providing_only", True)

    ensure_llm_provider_management_allowed()  # must not raise


# ---------------------------------------------------------------------------
# 3. Catalog ownership (the Env badge / the PUT 403 on available_models)
# ---------------------------------------------------------------------------


def _env_row(provider="anthropic"):
    return SimpleNamespace(provider=provider, is_from_env=True)


def _byollm_settings(**overrides):
    base = {
        "llm_provider_models_editable": False,
        "byollm_activated": True,
        "builtin_embedding_server_base_url": "",
        "builtin_embedding_server_model": "",
    }
    base.update(overrides)
    base["provider_models_admin_curated"] = (
        base["llm_provider_models_editable"] or base["byollm_activated"]
    )
    return SimpleNamespace(**base)


def test_byollm_unlocks_the_catalog_of_an_env_seeded_provider():
    assert models_env_locked(_env_row(), _byollm_settings()) is False


def test_byollm_leaves_the_builtin_tei_row_locked():
    """The built-in embedding row's own seed re-pins ``available_models`` to
    the single served model on every boot, so an editor there would be a
    phantom edit — the one exception, unchanged by BYOLLM."""
    tei = _byollm_settings(
        builtin_embedding_server_base_url="http://tei.test",
        builtin_embedding_server_model="bge-m3",
    )
    assert models_env_locked(_env_row("tei"), tei) is True


# ---------------------------------------------------------------------------
# 4. Nothing is env-managed under BYOLLM
# ---------------------------------------------------------------------------


def test_byollm_reports_no_env_managed_field_even_with_every_default_set(monkeypatch):
    """Every ``DEFAULT_*`` is a seed-once baseline under BYOLLM, so no admin
    edit reverts on restart — the only reason these fields were read-only.
    Embedding and image defaults included: the UI path that changes the
    embedding model also runs the knowledge-base re-embedding the env-seed
    path never did."""
    _reset(monkeypatch, **_ALL_DEFAULTS_SET)
    monkeypatch.setattr(app_settings, "byollm_activated", True)

    assert _env_managed_ga_fields() == set()


def test_without_byollm_the_same_defaults_lock_every_group(monkeypatch):
    """The contrast that makes the test above mean something: with BYOLLM off
    the very same environment locks all five groups."""
    _reset(monkeypatch, **_ALL_DEFAULTS_SET)
    monkeypatch.setattr(app_settings, "byollm_activated", False)

    assert _env_managed_ga_fields() == {
        "llm_provider",
        "llm_provider_id",
        "llm_model",
        "agent_default_provider",
        "agent_default_provider_id",
        "agent_default_model",
        "default_reasoning_effort",
        "routing_model",
        "routing_provider_id",
        "fallback_model",
        "fallback_provider_id",
        "embedding_provider",
        "embedding_provider_id",
        "embedding_model",
        "embedding_dimensions",
        "image_provider",
        "image_provider_id",
        "image_model",
    }


# ---------------------------------------------------------------------------
# 5. What /platform tells the UI
# ---------------------------------------------------------------------------


def _platform_settings_row():
    return SimpleNamespace(
        id=uuid.uuid4(),
        platform_name="X",
        platform_logo=None,
        platform_logo_url=None,
        platform_description="",
        platform_login_message=None,
        platform_theme_color=None,
        platform_favicon=None,
        platform_accent_color=None,
        platform_background_color=None,
        platform_og_image=None,
        enterprise_license=None,
        ai_disabled=None,
    )


def test_platform_payload_serves_the_resolved_provider_mode():
    """``models_env_providing_only`` on the wire is the answer the API will
    actually give, not the raw variable: serving ``true`` here while the API
    accepts the mutation would hide the Add Provider button the backend now
    allows."""
    payload = _serialize_platform_settings(
        _platform_settings_row(),
        SimpleNamespace(
            enterprise_license="",
            models_env_providing_only=True,
            byollm_activated=True,
            provider_management_env_only=False,
        ),
    )

    assert payload["models_env_providing_only"] is False
    assert payload["byollm_activated"] is True


def test_platform_payload_still_reports_env_only_mode_without_byollm():
    payload = _serialize_platform_settings(
        _platform_settings_row(),
        SimpleNamespace(
            enterprise_license="",
            models_env_providing_only=True,
            byollm_activated=False,
            provider_management_env_only=True,
        ),
    )

    assert payload["models_env_providing_only"] is True
    assert payload["byollm_activated"] is False


# ---------------------------------------------------------------------------
# 6. The startup log
# ---------------------------------------------------------------------------


@pytest.fixture
def app_main_log(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> Iterator[ModuleType]:
    """``app.main`` with caplog's handler attached straight to its logger.

    The first import of ``app.main`` runs ``setup_logging()``, which clears the
    root handlers and with them the one caplog installed, so a test that
    relies on propagation sees no record when it is the first on its worker to
    import the module. Attaching the handler to the named logger (and not
    propagating, so a record is never captured twice) makes the capture
    independent of import order and of the global logging configuration.
    """
    from app import main as app_main

    monkeypatch.setattr(app_main.logger, "propagate", False)
    app_main.logger.addHandler(caplog.handler)
    try:
        yield app_main
    finally:
        app_main.logger.removeHandler(caplog.handler)


def test_startup_warns_when_both_ownership_flags_are_set(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, app_main_log: ModuleType
) -> None:
    """The ignored flag must never be silent: an operator who leaves
    ``MODELS_ENV_PROVIDING_ONLY`` behind learns it stopped applying."""
    app_main = app_main_log

    monkeypatch.setattr(app_main.settings, "byollm_activated", True)
    monkeypatch.setattr(app_main.settings, "models_env_providing_only", True)

    with caplog.at_level(logging.WARNING, logger=app_main.logger.name):
        app_main._log_llm_ownership_policy()

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "MODELS_ENV_PROVIDING_ONLY=true is ignored" in warnings[0].getMessage()


def test_startup_says_nothing_when_byollm_is_off(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, app_main_log: ModuleType
) -> None:
    app_main = app_main_log

    monkeypatch.setattr(app_main.settings, "byollm_activated", False)
    monkeypatch.setattr(app_main.settings, "models_env_providing_only", True)

    with caplog.at_level(logging.INFO, logger=app_main.logger.name):
        app_main._log_llm_ownership_policy()

    assert caplog.records == []
