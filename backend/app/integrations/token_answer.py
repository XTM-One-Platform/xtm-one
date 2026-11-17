# SPDX-License-Identifier: Apache-2.0

"""The error of a token-endpoint answer that carries no usable token.

A leaf module: ``services/token_refresh.py`` (which re-exports it) and
``integrations/service_token_exchange.py`` both raise it, and the latter is
imported by the former.
"""

from __future__ import annotations


class MalformedTokenAnswerError(ValueError):
    """A 2xx token-endpoint answer that is not a refresh.

    ``refused``: the answer carries an OAuth ``error``, a refusal reported
    inside a success, which backs off like an unexplained 401 / 403.
    ``reason``: the classifier's verdict when that refusal can never succeed
    on retry (GitHub ``bad_refresh_token``, Slack ``invalid_refresh_token``,
    ...); it implies ``refused``.
    """

    def __init__(self, message: str, *, refused: bool = False, reason: str | None = None) -> None:
        super().__init__(message)
        self.reason = reason
        self.refused = refused or reason is not None
