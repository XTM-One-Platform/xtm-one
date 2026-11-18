"""Platform Health's name (#8261, #101): validated as hostile text, one default in every mode, never formatted."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import settings
from app.services.health.cycles import CycleStart
from app.services.health.naming import DEFAULT_NAMES, concierge_name, default_concierge_name
from app.services.health.settings import MAX_NAME_LENGTH, HealthSettings


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("copilot", "Platform Health"),
        ("xtm_one", "Platform Health"),
        ("dev", "Platform Health"),
        ("ai_disabled", "Platform Health"),
    ],
)
def test_the_default_name_is_platform_health_in_every_mode(monkeypatch, mode: str, expected: str) -> None:
    monkeypatch.setattr(settings, "platform_mode", mode)
    assert default_concierge_name() == expected
    assert concierge_name(HealthSettings()) == expected
    assert concierge_name(HealthSettings(name="Jeeves")) == "Jeeves"


def test_every_default_name_is_known() -> None:
    # The former defaults stay known, so a row seeded under one is renamed at the next seed.
    assert frozenset({"Platform Health", "Alfred", "Concierge"}) == DEFAULT_NAMES


@pytest.mark.parametrize(
    ("value", "stored"),
    [
        (None, None),
        ("", None),
        ("   ", None),
        ("Jeeves", "Jeeves"),
        ("  Jeeves  ", "Jeeves"),
        # A line break at either end is trimmed with the spaces.
        ("\nJeeves\r\n", "Jeeves"),
        ("Ålfrèd 執事", "Ålfrèd 執事"),
        ("{name} <b>&amp;", "{name} <b>&amp;"),
        ("x" * MAX_NAME_LENGTH, "x" * MAX_NAME_LENGTH),
    ],
)
def test_a_valid_name_is_stored_trimmed(value, stored) -> None:
    assert HealthSettings(name=value).name == stored


@pytest.mark.parametrize(
    "value",
    [
        "x" * (MAX_NAME_LENGTH + 1),
        "x" * 5000,
        "Al\nfred",
        "Al\rfred",
        "Al\tfred",
        "Al\x00fred",
        "Al\x1bfred",
        "Al\x7ffred",
        "Al\u0085fred",
        "Al fred",
        "Al fred",
        "Al‮fred",
        "Al​fred",
        "Al﻿fred",
        12,
        ["Alfred"],
    ],
)
def test_a_hostile_name_is_refused(value) -> None:
    with pytest.raises(ValidationError):
        HealthSettings(name=value)


def test_a_refusal_carries_the_name_as_written() -> None:
    refused = CycleStart(refusal="cycle_open")
    assert refused.message("{0}{name}%s") == "{0}{name}%s is already working on this target."
    assert CycleStart(refusal="no_such_code").message("Jeeves") == "Jeeves could not start this work."
    assert CycleStart(refusal="not_found").message("Jeeves") == "Target not found."
    assert CycleStart().message("Jeeves") is None
