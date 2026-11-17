"""Freeze the clock PyJWT checks ``exp`` / ``nbf`` / ``iat`` against.

A boundary one second either side of the leeway cannot be tested against the
real clock: PyJWT floors the timestamps it encodes, so a token minted near a
second boundary would land on the other side. PyJWT reads the time through
``datetime.now`` in ``jwt.api_jwt`` and nowhere else, so replacing that one
name puts every time check at an exact instant.
"""

from __future__ import annotations

from datetime import UTC, datetime

import jwt.api_jwt


def freeze_pyjwt_clock(monkeypatch, instant: int) -> None:
    """Make PyJWT's ``now`` the POSIX second *instant* for the rest of the test."""
    frozen = datetime.fromtimestamp(instant, UTC)

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen if tz is None else frozen.astimezone(tz)

    monkeypatch.setattr(jwt.api_jwt, "datetime", _FrozenDatetime)
