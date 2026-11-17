# SPDX-License-Identifier: Apache-2.0

"""Where the transactions of one ``Session`` sit relative to each other.

Work that must follow a transaction's commit and never outlive its rollback (an
exposure counter; the cancellation of a deleted document's queued jobs)
waits on ``Session.info`` tagged with the innermost transaction that can
end on its own: the SAVEPOINT in progress, else the root. The ``after_commit``
listener takes it on the durable commit, or hands it to the enclosing
transaction when a SAVEPOINT is only released; ``after_transaction_end`` drops
what a transaction that ended without committing still holds. These answer the
two questions those listeners ask.
"""

from __future__ import annotations

from sqlalchemy.orm import Session, SessionTransaction


def innermost_transaction(session: Session) -> SessionTransaction | None:
    """The SAVEPOINT in progress, else the root transaction (``None`` before one begins)."""
    return session.get_nested_transaction() or session.get_transaction()


def transaction_within(txn: SessionTransaction | None, ancestor: SessionTransaction) -> bool:
    """True when *txn* is *ancestor* or a transaction begun inside it."""
    while txn is not None:
        if txn is ancestor:
            return True
        txn = txn.parent
    return False


def enclosing_transaction(txn: SessionTransaction | None) -> SessionTransaction | None:
    """The nearest transaction from *txn* up that can end on its own."""
    while txn is not None and not (txn.nested or txn.parent is None):
        txn = txn.parent
    return txn
