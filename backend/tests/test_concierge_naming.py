"""The Concierge's name (#8261): validated as hostile text, defaulted by platform mode, never formatted."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import settings
from app.services.concierge.cycles import CycleStart
from app.services.concierge.naming import DEFAULT_NAMES, concierge_name, default_concierge_name
from app.services.concierge.settings import MAX_NAME_LENGTH, ConciergeSettings


@pytest.mark.parametrize(
    ("mode", "expected"),
    [("copilot", "Alfred"), ("xtm_one", "Concierge"), ("dev", "Concierge"), ("ai_disabled", "Concierge")],
)
def test_the_default_name_follows_the_platform_mode(monkeypatch, mode: str, expected: str) -> None:
    monkeypatch.setattr(settings, "platform_mode", mode)
    assert default_concierge_name() == expected
    assert concierge_name(ConciergeSettings()) == expected
    assert concierge_name(ConciergeSettings(name="Jeeves")) == "Jeeves"


def test_every_default_name_is_known() -> None:
    assert frozenset({"Alfred", "Concierge"}) == DEFAULT_NAMES


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
    assert ConciergeSettings(name=value).name == stored


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
        ConciergeSettings(name=value)


def test_a_refusal_carries_the_name_as_written() -> None:
    refused = CycleStart(refusal="cycle_open")
    assert refused.message("{0}{name}%s") == "{0}{name}%s is already working on this target."
    assert CycleStart(refusal="no_such_code").message("Jeeves") == "Jeeves could not start this work."
    assert CycleStart(refusal="not_found").message("Jeeves") == "Target not found."
    assert CycleStart().message("Jeeves") is None
