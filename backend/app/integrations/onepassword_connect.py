# SPDX-License-Identifier: Apache-2.0

"""1Password Connect: the vaults, items and fields a Connect token sees.

The one client of the Connect API (``{base}/v1``), shared by the 1Password
integration's agent tools (``providers/onepassword.py``) and the 1Password
secret vault provider (``services/vault/onepassword.py``): the URL and header
rules, reading vaults and items, resolving one by id or name, and finding a
field on an item. Every read answers ``(data, failure)``: a
:class:`ConnectFailure` carries the text an agent tool returns as is and the
HTTP status a vault provider turns into a reason. Item writes, files, the
activity log, the Events API and the SCIM bridge are the integration's alone.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

from app.integrations.http import safe_request

PROVIDER = "1Password"

#: ``safe_request``'s shape: ``(response, error)``.
Requester = Callable[..., Awaitable[tuple[httpx.Response | None, str | None]]]

# A 2xx response that is not JSON usually means the URL points at a login
# page, an SSO redirect target, or a misconfigured reverse proxy rather than
# the 1Password API itself - surface that instead of crashing the tool.
NON_JSON_ERROR = (
    "Error: 1Password returned a response that is not valid JSON. Check that "
    "the configured URL points at the API server itself (Connect server, "
    "events.1password.com, or the SCIM bridge) and not at a login page or "
    "misconfigured proxy."
)

_CONNECT_ID_ALPHABET = frozenset("abcdefghijklmnopqrstuvwxyz0123456789")


def onepassword_api_base(base_url: str) -> str:
    """Normalise a 1Password server base URL (strip whitespace + trailing '/').

    Applies to the Connect server URL (the REST API lives at ``{base}/v1``)
    and the Events Reporting host. Returns ``""`` when unset.
    """
    return (base_url or "").strip().rstrip("/")


def build_onepassword_headers(token: str | None) -> dict[str, str]:
    """Build 1Password auth headers (``Authorization: Bearer <token>``).

    Shared by all three surfaces (Connect, Events, SCIM) - each uses its own
    bearer token. A blank/whitespace-only token omits the header entirely so
    the request falls through to a clean 401 instead of sending an illegal
    empty header value.
    """
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    key = (token or "").strip()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return headers


def unexpected_shape_error(expected: str) -> str:
    """Build the error for a valid-JSON body of the wrong shape."""
    return (
        f"Error: 1Password returned an unexpected response shape (expected "
        f"a JSON {expected}). Verify the configured URL points at the "
        "1Password API and is not behind a rewriting proxy."
    )


def parse_json(resp: httpx.Response) -> tuple[Any, str | None]:
    """Parse a response body once; returns ``(data, error)``."""
    try:
        return resp.json(), None
    except ValueError:
        return None, NON_JSON_ERROR


def json_dict(resp: httpx.Response) -> tuple[dict, str | None]:
    """Parse a response body as a JSON object; ``({}, error)`` otherwise."""
    data, err = parse_json(resp)
    if err:
        return {}, err
    if not isinstance(data, dict):
        return {}, unexpected_shape_error("object")
    return data, None


def json_list(resp: httpx.Response) -> tuple[list, str | None]:
    """Parse a response body as a JSON array; ``([], error)`` otherwise."""
    data, err = parse_json(resp)
    if err:
        return [], err
    if not isinstance(data, list):
        return [], unexpected_shape_error("array")
    return data, None


def sid(value: Any) -> str:
    """Coerce a tool argument to a trimmed string.

    Tool arguments arrive from the LLM without runtime type coercion, so an
    id declared as ``string`` in the schema may still show up as ``42``.
    """
    if value is None:
        return ""
    return str(value).strip()


def pid(value: Any) -> str:
    """Percent-encode a user-supplied id for use as a single URL path segment.

    1Password ids (26-char lowercase base32) never need a literal ``/`` -
    encoding everything keeps a crafted id such as ``../activity`` from
    escaping its path position and reaching an unintended endpoint.
    """
    return quote(sid(value), safe="")


def is_connect_id(value: str) -> bool:
    """Return True when *value* looks like a Connect UUID (26-char base32)."""
    v = sid(value)
    return len(v) == 26 and all(c in _CONNECT_ID_ALPHABET for c in v)


def field_secret_kind(field: dict) -> str:
    """Classify a field: 'otp', 'concealed', or 'plain'."""
    ftype = sid(field.get("type")).upper()
    purpose = sid(field.get("purpose")).upper()
    if ftype == "OTP":
        return "otp"
    if ftype == "CONCEALED" or purpose == "PASSWORD":
        return "concealed"
    return "plain"


def field_label(field: dict) -> str:
    """What a field is shown as: its label, else its id."""
    return sid(field.get("label")) or sid(field.get("id"))


def item_fields(item: dict) -> list[dict]:
    return [field for field in item.get("fields") or [] if isinstance(field, dict)]


def find_field(item: dict, ref: str) -> tuple[dict | None, str | None]:
    """Find a field on a full item by id or label (case-insensitive)."""
    ref_s = sid(ref)
    if not ref_s:
        return None, "Error: a field (label or id) is required."
    fields = item_fields(item)
    by_id = [f for f in fields if sid(f.get("id")) == ref_s]
    if len(by_id) == 1:
        return by_id[0], None
    lowered = ref_s.lower()
    by_label = [f for f in fields if sid(f.get("label")).lower() == lowered]
    if len(by_label) == 1:
        return by_label[0], None
    if len(by_label) > 1:
        ids = ", ".join(f"'{f.get('label')}' (id={f.get('id')})" for f in by_label)
        return None, (
            f"Error: field label '{ref_s}' is ambiguous on this item - "
            f"matches: {ids}. Use the field id."
        )
    available = ", ".join(f"'{field_label(f)}'" for f in fields[:20])
    return None, (
        f"Error: no field matching '{ref_s}' on this item. Available "
        f"fields: {available or 'none'}."
    )


@dataclass(frozen=True)
class ConnectFailure:
    """Why a Connect read did not answer: the text an agent tool returns, and what a vault provider maps.

    ``kind`` is ``http`` (the server refused: ``status`` says how), ``network``
    (no answer), ``not_json`` / ``shape`` (an answer that is not the Connect
    API), ``not_found`` or ``ambiguous`` (a name that matches no entry, or
    several).
    """

    message: str
    kind: str
    status: int | None = None


class ConnectAPI:
    """The read side of one Connect server, on an HTTP client the caller owns.

    *request* is the transport (``safe_request`` by default): the integration
    passes its own module's, which its tests replace.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        token: str,
        *,
        request: Requester | None = None,
        max_total_seconds: float | None = None,
    ) -> None:
        self.client = client
        self.base = onepassword_api_base(base_url)
        self.api = f"{self.base}/v1" if self.base else ""
        self.headers = build_onepassword_headers(token)
        self._request = request or safe_request
        self._max_total_seconds = max_total_seconds

    async def _get(self, path: str, action: str, *, params: dict[str, str] | None = None) -> tuple[Any, ConnectFailure | None]:
        kwargs: dict[str, Any] = {"headers": self.headers, "expected_statuses": set()}
        if params:
            kwargs["params"] = params
        if self._max_total_seconds is not None:
            kwargs["max_total_seconds"] = self._max_total_seconds
        resp, err = await self._request(self.client, "get", f"{self.api}{path}", PROVIDER, action, **kwargs)
        if err:
            status = getattr(resp, "status_code", None) if resp is not None else None
            return None, ConnectFailure(err, "http" if status is not None else "network", status)
        if resp is None:
            return None, ConnectFailure(f"Error: 1Password did not answer ({action}).", "network")
        data, jerr = parse_json(resp)
        if jerr:
            return None, ConnectFailure(jerr, "not_json")
        return data, None

    async def _get_list(self, path: str, action: str, *, params: dict[str, str] | None = None) -> tuple[list[dict], ConnectFailure | None]:
        data, failure = await self._get(path, action, params=params)
        if failure:
            return [], failure
        if not isinstance(data, list):
            return [], ConnectFailure(unexpected_shape_error("array"), "shape")
        return [entry for entry in data if isinstance(entry, dict)], None

    async def _get_dict(self, path: str, action: str) -> tuple[dict, ConnectFailure | None]:
        data, failure = await self._get(path, action)
        if failure:
            return {}, failure
        if not isinstance(data, dict):
            return {}, ConnectFailure(unexpected_shape_error("object"), "shape")
        return data, None

    async def list_vaults(self) -> tuple[list[dict], ConnectFailure | None]:
        """The vaults the token was granted."""
        return await self._get_list("/vaults", "list vaults")

    async def get_vault(self, vault_id: str) -> tuple[dict, ConnectFailure | None]:
        return await self._get_dict(f"/vaults/{pid(vault_id)}", "get vault")

    async def list_items(self, vault_id: str) -> tuple[list[dict], ConnectFailure | None]:
        """The item summaries of one vault (no field values)."""
        return await self._get_list(f"/vaults/{pid(vault_id)}/items", "list vault items")

    async def get_item(self, vault_id: str, item_id: str) -> tuple[dict, ConnectFailure | None]:
        """One item with its fields and their values."""
        return await self._get_dict(f"/vaults/{pid(vault_id)}/items/{pid(item_id)}", "get item")

    async def resolve_vault(self, ref: str) -> tuple[dict | None, ConnectFailure | None]:
        """Resolve a vault by id or (exact, then unique substring) name."""
        ref = sid(ref)
        if not ref:
            return None, ConnectFailure("Error: a vault (name or id) is required.", "not_found")
        if is_connect_id(ref):
            vault, failure = await self.get_vault(ref)
            return (None, failure) if failure else (vault, None)
        vaults, failure = await self.list_vaults()
        if failure:
            return None, failure
        lowered = ref.lower()
        exact = [v for v in vaults if sid(v.get("name")).lower() == lowered]
        if len(exact) == 1:
            return exact[0], None
        partial = [v for v in vaults if lowered in sid(v.get("name")).lower()]
        if len(exact) > 1 or (not exact and len(partial) > 1):
            candidates = exact or partial
            names = ", ".join(f"'{v.get('name')}' (id={v.get('id')})" for v in candidates[:10])
            return None, ConnectFailure(
                f"Error: vault reference '{ref}' is ambiguous - matches: {names}. Use the vault id.",
                "ambiguous",
            )
        if not exact and len(partial) == 1:
            return partial[0], None
        return None, ConnectFailure(
            f"Error: no vault matching '{ref}' found. Use list_onepassword_vaults to see the "
            "vaults in the Connect token's scope.",
            "not_found",
        )

    async def resolve_item(self, vault_id: str, ref: str) -> tuple[dict | None, ConnectFailure | None]:
        """Resolve an item summary by id or (exact, then unique substring) title."""
        ref = sid(ref)
        if not ref:
            return None, ConnectFailure("Error: an item (title or id) is required.", "not_found")
        if is_connect_id(ref):
            return {"id": ref}, None
        # The Connect API only supports a SCIM 'eq' filter on title - try the
        # exact match server-side first, then fall back to a client-side
        # case-insensitive / substring match.
        matches, failure = await self._get_list(
            f"/vaults/{pid(vault_id)}/items", "find item by title", params={"filter": f'title eq "{ref}"'}
        )
        if failure:
            return None, failure
        if len(matches) == 1:
            return matches[0], None
        if len(matches) > 1:
            names = ", ".join(f"'{i.get('title')}' (id={i.get('id')})" for i in matches[:10])
            return None, ConnectFailure(
                f"Error: item title '{ref}' is ambiguous - matches: {names}. Use the item id.", "ambiguous"
            )
        items, failure = await self.list_items(vault_id)
        if failure:
            return None, failure
        lowered = ref.lower()
        exact = [i for i in items if sid(i.get("title")).lower() == lowered]
        if len(exact) == 1:
            return exact[0], None
        partial = [i for i in items if lowered in sid(i.get("title")).lower()]
        if len(exact) > 1 or (not exact and len(partial) > 1):
            candidates = exact or partial
            names = ", ".join(f"'{i.get('title')}' (id={i.get('id')})" for i in candidates[:10])
            return None, ConnectFailure(
                f"Error: item reference '{ref}' is ambiguous - matches: {names}. Use the item id.", "ambiguous"
            )
        if not exact and len(partial) == 1:
            return partial[0], None
        return None, ConnectFailure(
            f"Error: no item matching '{ref}' found in the vault. Use search_onepassword_items to locate it.",
            "not_found",
        )

    async def resolve_vault_and_item(self, vault_ref: str, item_ref: str) -> tuple[dict, dict, ConnectFailure | None]:
        """Resolve (vault, full item) in one go; ``({}, {}, failure)`` when either is not found."""
        vault, failure = await self.resolve_vault(vault_ref)
        if failure or vault is None:
            return {}, {}, failure or ConnectFailure("Error: vault not found.", "not_found")
        summary, failure = await self.resolve_item(sid(vault.get("id")), item_ref)
        if failure or summary is None:
            return {}, {}, failure or ConnectFailure("Error: item not found.", "not_found")
        item, failure = await self.get_item(sid(vault.get("id")), sid(summary.get("id")))
        if failure:
            return {}, {}, failure
        return vault, item, None
