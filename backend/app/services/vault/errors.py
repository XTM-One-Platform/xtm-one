"""Why a vault reference could not be resolved.

Two layers on purpose. A provider raises :class:`VaultReadError` with a
reason code and an operator detail (an HTTP status, an AWS error code) that
goes to the server log only. The caller of a secret gets
:class:`SecretUnavailableError`, whose message is the same generic sentence
whatever happened: a user who triggered the read learns that the secret is
unavailable, never where it lives or why the vault refused.
"""

from __future__ import annotations

from typing import Literal

FailureReason = Literal[
    "provider_not_configured",
    "provider_disabled",
    "invalid_reference",
    "path_not_allowed",
    "not_found",
    "key_required",
    "access_denied",
    "unreachable",
    "invalid_response",
    "misconfigured",
    "variable_unavailable",
]

#: What each reason means, for the entity page and the Security Audit; no secret, no path.
REASON_DESCRIPTIONS: dict[str, str] = {
    "provider_not_configured": "the reference names a vault provider this platform does not declare",
    "provider_disabled": "the reference names a vault provider an administrator disabled",
    "invalid_reference": "the stored reference is not a valid vault reference",
    "path_not_allowed": "the reference names a path outside the paths its vault provider allows",
    "not_found": "the vault has no such secret, or no such key in it",
    "key_required": "the secret holds several entries and the reference names none (#key)",
    "access_denied": "the vault refused the platform's credentials for this secret",
    "unreachable": "the vault could not be reached or did not answer in time",
    "invalid_response": "the vault answered with something that is not a secret value",
    "misconfigured": "the vault provider's own configuration or credentials are missing or invalid",
    "variable_unavailable": (
        "the secret variable the credential references is deleted, not secret, empty "
        "or no longer visible to the owner"
    ),
}

#: The one message a caller of an unavailable secret is given (the detail is in the server log).
VAULT_UNAVAILABLE_DETAIL = (
    "A secret stored in a vault could not be read. An administrator can find the details in the server logs."
)


class VaultReadError(Exception):
    """A provider could not hand back a secret: a reason code and an operator detail, never a value."""

    def __init__(self, reason: FailureReason, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason: FailureReason = reason
        self.detail = detail


class SecretUnavailableError(RuntimeError):
    """A secret its feature needs could not be read from its vault; the message is generic."""

    def __init__(self, reason: FailureReason) -> None:
        super().__init__(VAULT_UNAVAILABLE_DETAIL)
        self.reason: FailureReason = reason
