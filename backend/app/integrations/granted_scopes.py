# SPDX-License-Identifier: Apache-2.0

"""The OAuth scopes a stored token was actually granted.

An authorization server may grant fewer scopes than the client requested: a
person can leave a granular consent box unticked (Google), an admin policy can
strip one (Microsoft). RFC 6749 section 5.1 has the token endpoint say so in
its ``scope`` field. Without it recorded, a reconnect that did not obtain a new
scope looks exactly like one that did, and the first sign is a 403 from the
provider.

This module reads that field, stores it on the token holder (an integration
row or a person's own credential, column ``granted_scopes``) and compares it
with the scopes the integration requests. It holds no provider logic: a
provider that echoes no ``scope`` leaves the granted set unknown, and nothing
is then reported missing.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

#: Identity scopes a provider honours without echoing them in the same form
#: (Google answers ``openid`` with ``https://www.googleapis.com/auth/userinfo.email``
#: for ``email``; Microsoft drops ``offline_access``): never reported missing.
_IDENTITY_SCOPES = frozenset({"openid", "email", "profile", "offline_access"})


def split_scopes(value: Any) -> list[str]:
    """The scopes of a ``scope`` value: a space- or comma-separated string, or a list.

    Order kept, duplicates and blanks dropped; anything else gives ``[]``.
    """
    if isinstance(value, str):
        raw: Iterable[Any] = value.replace(",", " ").split()
    elif isinstance(value, list | tuple):
        raw = value
    else:
        return []
    seen: dict[str, None] = {}
    for item in raw:
        if isinstance(item, str) and item.strip():
            seen.setdefault(item.strip(), None)
    return list(seen)


def granted_scope_string(tokens: dict[str, Any]) -> str | None:
    """The scopes a token-endpoint answer says it granted, space-separated.

    ``None`` when the answer carries no ``scope`` field (the grant is then
    unknown, never assumed complete).
    """
    if not isinstance(tokens, dict) or "scope" not in tokens:
        return None
    return " ".join(split_scopes(tokens.get("scope")))


def record_granted_scopes(target: Any, tokens: dict[str, Any], *, keep_when_absent: bool) -> None:
    """Store on *target* the scopes *tokens* granted.

    At a code exchange (``keep_when_absent=False``) an answer without ``scope``
    clears what an earlier token was granted: it no longer describes the
    stored token. At a refresh (``keep_when_absent=True``) it keeps it: a
    refresh answer often omits the field, and the grant does not change.
    """
    granted = granted_scope_string(tokens)
    if granted is None and keep_when_absent:
        return
    target.granted_scopes = granted


def _scope_key(scope: str) -> str:
    """Compare scopes on their last segment, case-insensitively: Microsoft
    grants ``Mail.Read`` for a request of ``https://graph.microsoft.com/Mail.Read``."""
    return scope.rstrip("/").rsplit("/", 1)[-1].lower()


def missing_tool_scopes(tool_scopes: Any, granted: Any) -> list[str]:
    """The tool permissions the token lacks, as ``Scope (tool, tool)`` lines.

    *tool_scopes* is a catalog entry's ``tool_scopes`` (#2386): a list of
    ``{"any_of": [scope, ...], "tools": [tool, ...]}``, the permissions some
    tools need beyond the scopes asked at sign-in (an admin grants them on the
    app; Microsoft then puts them in the token). An entry is satisfied by any
    one of its scopes. ``[]`` when the grant is unknown.
    """
    if granted is None or not isinstance(tool_scopes, list):
        return []
    granted_keys = {_scope_key(s) for s in split_scopes(granted)}
    lacking: list[str] = []
    for entry in tool_scopes:
        if not isinstance(entry, dict):
            continue
        alternatives = split_scopes(entry.get("any_of"))
        tools = [t for t in entry.get("tools") or [] if isinstance(t, str)]
        if not alternatives or any(_scope_key(s) in granted_keys for s in alternatives):
            continue
        lacking.append(f"{alternatives[0]} ({', '.join(tools)})" if tools else alternatives[0])
    return lacking


def missing_scopes(requested: Any, granted: Any, optional: Any = None) -> list[str] | None:
    """The requested scopes the token was not granted, in request order.

    ``None`` when the grant is unknown (nothing recorded); identity scopes
    (``openid``, ``email``, ``profile``, ``offline_access``) are never missing,
    nor are the *optional* ones: the scopes a provider entry declares it may
    drop silently (``optional_scopes`` of the catalog, #11033), which no
    consent can obtain where the account lacks the product behind them. Left
    reported, such a scope would hold the integration in "Re-authorization
    required" forever.
    """
    if granted is None:
        return None
    granted_keys = {_scope_key(s) for s in split_scopes(granted)}
    optional_keys = {_scope_key(s) for s in split_scopes(optional)}
    return [
        scope
        for scope in split_scopes(requested)
        if scope.lower() not in _IDENTITY_SCOPES
        and _scope_key(scope) not in granted_keys
        and _scope_key(scope) not in optional_keys
    ]
