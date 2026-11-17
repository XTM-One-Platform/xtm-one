"""Tests for the platform feature-flags mechanism (app/core/feature_flags.py).

This pins the design:
- ``ENABLED_FEATURES`` unset/empty -> no flags enabled (fail-closed default).
- An unknown flag id in ``ENABLED_FEATURES`` is logged and ignored at
  startup (so switching branches with a stale ``.env`` never bricks boot)
  while staying fail-closed — it can never enable anything.
- ``"*"`` enables every known flag at once.
- ``require_feature()`` 403s when the flag is disabled.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.core.config import Settings
from app.core.feature_flags import (
    FEATURE_FLAG_ALL,
    FLAG_CONDITIONS,
    KNOWN_FEATURE_FLAGS,
    is_feature_enabled,
    require_feature,
)


def test_empty_enabled_features_means_no_flags_enabled():
    settings = Settings(enabled_features="")
    assert settings.enabled_feature_flags == frozenset()


def test_unset_enabled_features_defaults_to_no_flags_enabled(monkeypatch):
    # Isolate from BOTH configuration sources: `_env_file=None` for the
    # developer's `.env`, `delenv` for an exported variable (pydantic-settings
    # reads the environment whatever `_env_file` says). Without both, this
    # asserts "nobody working on this repo has a feature flag configured
    # locally" — the contract is that the *default*, with nothing configured
    # anywhere, is fail-closed.
    monkeypatch.delenv("ENABLED_FEATURES", raising=False)
    settings = Settings(_env_file=None)
    assert settings.enabled_feature_flags == frozenset()


def test_wildcard_is_parsed_as_a_single_token():
    settings = Settings(enabled_features=FEATURE_FLAG_ALL)
    assert settings.enabled_feature_flags == frozenset({FEATURE_FLAG_ALL})


def test_unknown_flag_id_is_ignored_and_does_not_crash(caplog):
    # A flag id absent from the known registry (typo, or a flag that only
    # exists on another branch) must NOT crash startup — switching branches
    # with a stale .env should never brick boot. It is logged and ignored,
    # and stays fail-closed (can never enable anything).
    import logging

    assert "TOTALLY_MADE_UP_FLAG" not in KNOWN_FEATURE_FLAGS
    with caplog.at_level(logging.WARNING):
        settings = Settings(enabled_features="TOTALLY_MADE_UP_FLAG")
    assert any("unknown flag id" in r.message for r in caplog.records)
    # Fail-closed: the unknown id is stripped from the stored value, so no
    # reader (raw string or parsed set) can ever observe it as enabled.
    assert settings.enabled_features == ""
    assert settings.enabled_feature_flags == frozenset()


def test_unknown_flag_ids_are_stripped_but_known_ids_survive(caplog):
    # Mixing a known flag with a stale/unknown one must keep the known flag
    # enabled while silently-but-loudly dropping the unknown id.
    import logging

    known_flag = sorted(KNOWN_FEATURE_FLAGS)[0]
    with caplog.at_level(logging.WARNING):
        settings = Settings(
            enabled_features=f"{known_flag},TOTALLY_MADE_UP_FLAG"
        )
    assert any("unknown flag id" in r.message for r in caplog.records)
    assert settings.enabled_features == known_flag
    assert settings.enabled_feature_flags == frozenset({known_flag})


def test_whitespace_and_empty_tokens_are_ignored():
    settings = Settings(enabled_features=" * , , ")
    assert settings.enabled_feature_flags == frozenset({FEATURE_FLAG_ALL})


# A real flag id from the registry — is_feature_enabled() resolves through
# settings.enabled_feature_flags, which drops unknown ids (fail-closed), so
# the tests below must use a known id rather than a made-up one, and one with
# no deployment condition (``FLAG_CONDITIONS``) so the platform mode the test
# process runs in does not decide the answer.
KNOWN_FLAG = sorted(KNOWN_FEATURE_FLAGS - FLAG_CONDITIONS.keys())[0]


def test_is_feature_enabled_false_by_default(monkeypatch):
    monkeypatch.setattr("app.core.config.settings.enabled_features", "")
    assert is_feature_enabled(KNOWN_FLAG) is False


def test_is_feature_enabled_true_when_flag_present(monkeypatch):
    monkeypatch.setattr(
        "app.core.config.settings.enabled_features", KNOWN_FLAG
    )
    assert is_feature_enabled(KNOWN_FLAG) is True
    assert is_feature_enabled("OTHER_FLAG") is False


def test_is_feature_enabled_ignores_unknown_id_in_raw_value(monkeypatch):
    # Even if an unknown id sneaks into the raw string (bypassing the
    # Settings validator, e.g. via direct attribute assignment), the
    # known-registry filter in enabled_feature_flags keeps it disabled.
    monkeypatch.setattr(
        "app.core.config.settings.enabled_features", "TOTALLY_MADE_UP_FLAG"
    )
    assert is_feature_enabled("TOTALLY_MADE_UP_FLAG") is False


def test_is_feature_enabled_true_for_any_flag_with_wildcard(monkeypatch):
    monkeypatch.setattr(
        "app.core.config.settings.enabled_features", FEATURE_FLAG_ALL
    )
    assert is_feature_enabled(KNOWN_FLAG) is True


def test_wildcard_does_not_enable_unknown_flag_ids(monkeypatch):
    # "*" expands to the known registry only (consistent with
    # resolve_enabled_feature_flags()) — an unknown id stays disabled
    # even with the wildcard set.
    monkeypatch.setattr(
        "app.core.config.settings.enabled_features", FEATURE_FLAG_ALL
    )
    assert is_feature_enabled("TOTALLY_MADE_UP_FLAG") is False


def test_require_feature_dependency_raises_403_when_disabled(monkeypatch):
    monkeypatch.setattr("app.core.config.settings.enabled_features", "")
    dependency = require_feature(KNOWN_FLAG)
    with pytest.raises(HTTPException) as exc_info:
        dependency()
    assert exc_info.value.status_code == 403


def test_require_feature_dependency_passes_when_enabled(monkeypatch):
    monkeypatch.setattr(
        "app.core.config.settings.enabled_features", KNOWN_FLAG
    )
    dependency = require_feature(KNOWN_FLAG)
    dependency()  # should not raise
