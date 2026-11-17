"""Shared surface for the session doubles that drive ``app.a2a.hooks``.

The hooks queue each exposure-counter increment on ``session.info``, tagged
with the transaction it belongs to, and count it when that transaction
commits. A double that stands in for a ``Session`` therefore needs
``info`` and the two transaction accessors; it has no transaction, so the
increment is simply queued and never emitted.

That is also why no double can say anything about the counter: when an
increment is counted or dropped depends on which session events SQLAlchemy
fires, and in what order. The counting is proven on real sessions only, in
``test_a2a_exposure_metrics_commit.py`` (SQLite) and
``test_a2a_hooks_postgres.py`` (PostgreSQL).
"""

from __future__ import annotations


class TxnSurface:
    def get_nested_transaction(self):
        return None

    def get_transaction(self):
        return None

    @property
    def info(self) -> dict:
        return self.__dict__.setdefault("_info", {})

    @info.setter
    def info(self, value: dict) -> None:
        self.__dict__["_info"] = value
