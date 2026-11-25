# SPDX-License-Identifier: Apache-2.0

"""Why Microsoft Entra ID refused a token, kept for the tool that needed it.

The delegated (refresh-token) and app-only (client-credentials) token paths of
the Microsoft security integrations (Defender XDR, Sentinel, Purview) used to
return ``None`` on any failure, so every tool answered one fixed
"re-authorize" text. Entra's answer says which of three different fixes
applies (#2386):

* ``consent``: the app has no admin consent on that resource (AADSTS65001,
  AADSTS65004, ``invalid_scope``...). Re-authorizing does not help: an
  administrator must add the resource's permissions and grant admin consent.
* ``grant``: the refresh token is expired, revoked or missing
  (``invalid_grant``). Re-authorizing the integration is the fix.
* ``client``: the app credentials are wrong (``invalid_client``, a 401).
  The tenant ID, client ID and client secret are to be checked.

This module classifies the token endpoint's answer and keeps the last failure
per (holder, audience) in process for a few minutes, so the tool that asked
for the token can name the right fix. It holds no secret: only the error code,
the AADSTS code and the HTTP status are kept, never a token or a description
that could echo request material.
"""

from __future__ import annotations

import base64
import json
import re
import time
from dataclasses import dataclass
from typing import Any

#: AADSTS codes that mean the app (or the user) has no consent on the resource.
_CONSENT_CODES = frozenset({"65001", "65004", "65005", "90094", "90095", "500011", "650056", "650057"})
#: AADSTS codes that mean the refresh token or the grant itself is gone.
_GRANT_CODES = frozenset({"50173", "700082", "700084", "70043", "70008", "50076", "50079", "530003", "54005"})
#: AADSTS codes that mean the app credentials are wrong.
_CLIENT_CODES = frozenset({"7000215", "7000222", "700016", "90002", "900023", "50034"})

_AADSTS = re.compile(r"AADSTS(\d{5,7})")

#: How long a recorded failure is kept (a tool call reads it right after).
_TTL_SECONDS = 600
_MAX_ENTRIES = 1024


@dataclass(frozen=True)
class EntraTokenFailure:
    """The classified reason a token request for one audience failed."""

    kind: str  # consent | grant | client | other
    error: str = ""  # the OAuth error field (invalid_grant, invalid_client...)
    aadsts: str = ""  # the AADSTS code, digits only
    status: int | None = None  # the HTTP status of the token endpoint

    def code(self) -> str:
        """A short, secret-free label: ``AADSTS65001 (invalid_grant, HTTP 400)``."""
        parts = [p for p in (self.error, f"HTTP {self.status}" if self.status else "") if p]
        head = f"AADSTS{self.aadsts}" if self.aadsts else ""
        if head and parts:
            return f"{head} ({', '.join(parts)})"
        return head or ", ".join(parts)


def classify(status: int | None, body: Any) -> EntraTokenFailure:
    """Classify a token endpoint answer (*body* a dict or the raw text)."""
    data: dict[str, Any] = {}
    if isinstance(body, dict):
        data = body
    elif isinstance(body, str | bytes):
        try:
            parsed = json.loads(body)
            data = parsed if isinstance(parsed, dict) else {}
        except (ValueError, TypeError):
            data = {"error_description": body if isinstance(body, str) else ""}
    error = str(data.get("error") or "").strip()[:40]
    description = str(data.get("error_description") or "")
    codes = data.get("error_codes")
    aadsts = ""
    if isinstance(codes, list) and codes:
        aadsts = str(codes[0])
    if not aadsts:
        match = _AADSTS.search(description)
        aadsts = match.group(1) if match else ""
    aadsts = aadsts if aadsts.isdigit() else ""
    if aadsts in _CONSENT_CODES or error in {"invalid_scope", "consent_required"}:
        kind = "consent"
    elif aadsts in _CLIENT_CODES or error in {"invalid_client", "unauthorized_client"} or status == 401:
        kind = "client"
    elif aadsts in _GRANT_CODES or error in {"invalid_grant", "interaction_required"}:
        kind = "grant"
    else:
        kind = "other"
    return EntraTokenFailure(kind=kind, error=error, aadsts=aadsts, status=status)


_FAILURES: dict[str, tuple[EntraTokenFailure, float]] = {}


def delegated_key(holder: str, scope: str) -> str:
    """The failure key of a delegated (refresh-token) holder and audience."""
    return f"delegated:{holder}:{scope}"


def app_key(cache_key: str) -> str:
    """The failure key of an app-only token, from its secret-free cache key."""
    return f"app:{cache_key}"


def remember(key: str, failure: EntraTokenFailure | None) -> None:
    """Keep *failure* for *key* (a holder and audience), or drop it on ``None``."""
    if failure is None:
        _FAILURES.pop(key, None)
        return
    now = time.monotonic()
    if len(_FAILURES) >= _MAX_ENTRIES:
        for stale in [k for k, (_, at) in _FAILURES.items() if now - at > _TTL_SECONDS] or list(_FAILURES)[:1]:
            _FAILURES.pop(stale, None)
    _FAILURES[key] = (failure, now)


def recall(key: str) -> EntraTokenFailure | None:
    """The failure kept for *key*, if recent."""
    entry = _FAILURES.get(key)
    if entry is None:
        return None
    failure, at = entry
    if time.monotonic() - at > _TTL_SECONDS:
        _FAILURES.pop(key, None)
        return None
    return failure


def token_permissions(access_token: str) -> set[str] | None:
    """The permissions a Microsoft access token carries (``scp`` and ``roles``).

    The JWT payload is decoded without verification, for diagnosis only; the
    token is never logged or returned. ``None`` when it cannot be read (an
    opaque or encrypted token).
    """
    parts = (access_token or "").split(".")
    if len(parts) < 2:
        return None
    segment = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(segment.encode("ascii")))
    except (ValueError, TypeError, UnicodeError):
        return None
    if not isinstance(claims, dict):
        return None
    granted: set[str] = set()
    scp = claims.get("scp")
    if isinstance(scp, str):
        granted.update(s for s in scp.split() if s)
    roles = claims.get("roles")
    if isinstance(roles, list):
        granted.update(str(r) for r in roles if r)
    return {g.lower() for g in granted}
