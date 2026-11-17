"""One frozen instant for the clocks the code under test reads.

A test that pins a period (``MONTH = _current_period_key("monthly")``) or a
``NOW`` and then calls code reading the wall clock again goes red the moment
the run crosses 00:00 UTC: the test holds ``2026-10`` while the code reads
``2026-11``, and every night the daily period splits the same way. The fix is
not a retry nor a looser assertion: both sides read one instant.

``FROZEN_NOW`` is taken once, when the suite first imports this module. The
autouse fixture of ``conftest.py`` makes ``app.services.quota`` read it for
every test; a module that pins its own period or ``NOW`` derives it from
``FROZEN_NOW`` and freezes the clock of the modules it calls with
``freeze_datetime``.
"""

from __future__ import annotations

from datetime import UTC, datetime, tzinfo
from types import ModuleType

import pytest

#: The instant every frozen clock of the test session answers.
FROZEN_NOW = datetime.now(UTC)


class _AnyDatetime(type):
    """``isinstance(value, datetime)`` stays true of every datetime in a frozen module."""

    def __instancecheck__(cls, instance: object) -> bool:
        return isinstance(instance, _REAL_DATETIME)

    def __subclasscheck__(cls, subclass: type) -> bool:
        return issubclass(subclass, _REAL_DATETIME)


_REAL_DATETIME = datetime


class FrozenDatetime(datetime, metaclass=_AnyDatetime):
    """``datetime`` whose ``now()`` answers ``FROZEN_NOW``, in the zone asked."""

    @classmethod
    def now(cls, tz: tzinfo | None = None) -> datetime:  # type: ignore[override]
        if tz is None:
            return FROZEN_NOW.astimezone().replace(tzinfo=None)
        return FROZEN_NOW.astimezone(tz)


def freeze_datetime(monkeypatch: pytest.MonkeyPatch, *modules: ModuleType) -> None:
    """Make each module's ``datetime.now()`` answer ``FROZEN_NOW`` for the test."""
    for module in modules:
        monkeypatch.setattr(module, "datetime", FrozenDatetime)
