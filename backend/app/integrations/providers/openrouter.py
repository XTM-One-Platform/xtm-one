# SPDX-License-Identifier: Apache-2.0

"""OpenRouter integration - the account, catalog and API keys behind the OpenRouter LLM provider.

OpenRouter (https://openrouter.ai) serves hundreds of models behind one
OpenAI-compatible API. Inference itself goes through the platform LLM provider
(``app/llm/openrouter_provider.py``); this module is the rest of the API, which
the integration's own pages use: what the key has spent, the credits left, the
daily activity per model, the model catalog with each model's providers, and
the API keys of the account.

Two credentials, both stored on the integration:

* the **API key** (``api_key_enc``) - an inference key. It reads its own usage
  (``GET /key``) and the catalog, and it is the key the platform LLM provider
  is populated with (``services/openrouter_llm_provider.py``);
* the **management key** (``extra_config.management_key``, optional) - the
  account's admin credential. OpenRouter refuses it for inference and refuses
  an inference key on credits, activity and key management.

Every call goes to the one fixed host, through an ``egress_client``: when the
integration is routed through a relay group, the caller sets its route
(``egress_route``) and the request leaves from the relay. A value a caller
chose (a model id, a key hash, a date) is validated before it reaches a URL,
and a body is read in bounded raw chunks. An upstream failure is raised as :class:`OpenRouterError`,
whose ``public_message`` is a fixed sentence: OpenRouter's own wording and
status only reach the log.

API reference: https://openrouter.ai/docs/api/reference/overview
OpenAPI spec: https://openrouter.ai/openapi.json
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from app.core.egress import egress_client
from app.integrations.http import PLATFORM_USER_AGENT

logger = logging.getLogger(__name__)

PROVIDER = "openrouter"
BASE_URL = "https://openrouter.ai/api/v1"
#: The extra-config key the management key is stored under. Its name makes the
#: integration API redact it (``_is_sensitive_setting_name``).
MANAGEMENT_KEY_FIELD = "management_key"

_TIMEOUT = httpx.Timeout(20.0, connect=10.0)
#: The full text catalog is ~1.5 MB (464 models, 2026-10); anything ten times
#: that is not a catalog.
_MAX_BODY_BYTES = 16 * 1024 * 1024
#: ``GET /keys`` pages by 100 (no ``limit`` parameter); an account with more
#: keys than this is listed up to it, and the response says so.
_KEYS_PAGE_SIZE = 100
_MAX_KEY_PAGES = 20
_MAX_DESCRIPTION_CHARS = 600

#: ``author/slug`` with an optional ``:variant`` (``:free``, ``:nitro``) - the
#: shape of every OpenRouter model id - and an optional leading ``~``, which
#: marks OpenRouter's aliases of an author's latest model
#: (``~google/gemini-pro-latest``). Anything else never reaches a URL.
MODEL_ID_RE = re.compile(r"^~?[a-z0-9][a-z0-9._-]{0,99}/[A-Za-z0-9][A-Za-z0-9._~-]{0,199}(:[a-z0-9-]{1,30})?$")
#: A key ``hash``: the SHA-256 hex of the key, as the keys API returns it.
KEY_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

LIMIT_RESETS = ("daily", "weekly", "monthly")


class OpenRouterError(Exception):
    """A call OpenRouter refused or could not answer.

    ``kind`` decides the HTTP status the console answers with; ``public_message``
    is what the caller is shown. ``detail`` is for the log only: it may carry
    OpenRouter's own wording, which is never sent back (it would turn the
    endpoint into an oracle on the upstream account).
    """

    def __init__(self, kind: str, public_message: str, *, detail: str = "") -> None:
        super().__init__(public_message)
        self.kind = kind
        self.public_message = public_message
        self.detail = detail


_PUBLIC_MESSAGES = {
    "auth": "OpenRouter refused the integration's credentials. Replace them in the Configuration tab.",
    "management_key_required": (
        "OpenRouter only allows this with a management key. Add one in the Configuration tab "
        "(OpenRouter > Settings > Management keys)."
    ),
    "not_found": "OpenRouter has no such resource.",
    "rate_limited": "OpenRouter is rate limiting these requests. Try again in a minute.",
    "invalid_request": "OpenRouter refused the request.",
    "unavailable": "OpenRouter could not be reached. Try again later.",
    "invalid_response": "OpenRouter sent an answer this page cannot read.",
}


def _error(kind: str, detail: str = "") -> OpenRouterError:
    return OpenRouterError(kind, _PUBLIC_MESSAGES[kind], detail=detail)


def key_hash(api_key: str) -> str:
    """The ``hash`` the keys API gives the key ``api_key`` (SHA-256 hex).

    Lets the console tell which listed key is the one this integration, or the
    platform LLM provider, runs on - without ever sending a key anywhere.
    """
    return hashlib.sha256(api_key.strip().encode()).hexdigest()


def is_valid_model_id(model_id: str) -> bool:
    return bool(MODEL_ID_RE.fullmatch(model_id or "")) and ".." not in model_id


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


def _upstream_message(body: bytes) -> str:
    """OpenRouter's error sentence, for the log only, quoted and truncated."""
    try:
        parsed = json.loads(body.decode("utf-8", errors="replace"))
        message = parsed.get("error", {}).get("message") if isinstance(parsed, dict) else None
    except (ValueError, AttributeError):
        message = None
    text = message if isinstance(message, str) else body[:200].decode("utf-8", errors="replace")
    return repr(text[:200])


async def _read_capped(response: httpx.Response) -> bytes:
    """The raw body, at most ``_MAX_BODY_BYTES``; one byte more ends the transfer."""
    encoding = (response.headers.get("content-encoding") or "identity").strip().lower()
    if encoding not in ("", "identity"):
        # Asked for identity: a compressed answer would be inflated without a
        # bound by anything reading it decoded.
        raise _error("invalid_response", f"content-encoding {encoding[:40]!r}")
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_raw():
        total += len(chunk)
        if total > _MAX_BODY_BYTES:
            raise _error("invalid_response", "body over the size cap")
        chunks.append(chunk)
    return b"".join(chunks)


async def request(
    method: str,
    path: str,
    *,
    key: str | None,
    action: str,
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
    management: bool = False,
    client: httpx.AsyncClient | None = None,
) -> Any:
    """Call ``BASE_URL + path`` and return the parsed JSON body.

    ``path`` is always built by this module from validated parts. ``key`` is
    the credential to send (``None`` for the public catalog routes);
    ``management`` says the route needs a management key, so that OpenRouter's
    403 is reported as the missing management key rather than as a bad key.
    """
    if not path.startswith("/") or "//" in path or ".." in path:
        raise ValueError(f"unexpected OpenRouter path {path!r}")
    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "identity",
        "User-Agent": PLATFORM_USER_AGENT,
    }
    if key:
        headers["Authorization"] = f"Bearer {key.strip()}"

    async def _send(c: httpx.AsyncClient) -> Any:
        try:
            async with c.stream(
                method, f"{BASE_URL}{path}", headers=headers, params=params, json=json_body
            ) as response:
                body = await _read_capped(response)
                status = response.status_code
                content_type = (response.headers.get("content-type") or "").lower()
        except OpenRouterError:
            raise
        except httpx.HTTPError as exc:
            raise _error("unavailable", f"{action}: {type(exc).__name__}") from exc

        if status >= 400:
            upstream = _upstream_message(body)
            logger.warning("OpenRouter %s answered %s: %s", action, status, upstream)
            if status == 401:
                raise _error("auth", f"{action}: 401 {upstream}")
            if status == 403:
                raise _error(
                    "management_key_required" if management else "auth",
                    f"{action}: 403 {upstream}",
                )
            if status == 404:
                raise _error("not_found", f"{action}: 404 {upstream}")
            if status == 429:
                raise _error("rate_limited", f"{action}: 429")
            if status in (400, 409, 422):
                raise _error("invalid_request", f"{action}: {status} {upstream}")
            raise _error("unavailable", f"{action}: {status} {upstream}")
        if "json" not in content_type:
            raise _error("invalid_response", f"{action}: content-type {content_type[:60]!r}")
        try:
            return json.loads(body)
        except ValueError as exc:
            raise _error("invalid_response", f"{action}: not JSON") from exc

    if client is not None:
        return await _send(client)
    async with egress_client(timeout=_TIMEOUT, follow_redirects=False) as owned:
        return await _send(owned)


def _data(payload: Any, action: str) -> Any:
    """``payload["data"]``, the envelope every OpenRouter answer uses."""
    if not isinstance(payload, dict) or "data" not in payload:
        raise _error("invalid_response", f"{action}: no data envelope")
    return payload["data"]


# ---------------------------------------------------------------------------
# Value hygiene - every field below is OpenRouter's, so it is typed on the way in
# ---------------------------------------------------------------------------


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _str(value: Any, limit: int = 200) -> str | None:
    if not isinstance(value, str):
        return None
    return value[:limit]


def _bool(value: Any) -> bool:
    return value is True


def _str_list(value: Any, limit: int = 40) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item[:60] for item in value[:limit] if isinstance(item, str)]


def _price_per_million(value: Any) -> float | None:
    """A per-token USD price string as USD per million tokens.

    ``None`` when absent or variable: dynamic routers (``openrouter/auto``)
    price with the ``"-1"`` sentinel, which is not a price.
    """
    if not isinstance(value, str):
        return None
    try:
        price = Decimal(value)
    except InvalidOperation:
        return None
    if not price.is_finite() or price < 0:
        return None
    return float(price * 1_000_000)


def _unit_price(value: Any) -> float | None:
    """A per-unit USD price string (per request, image, search)."""
    if not isinstance(value, str):
        return None
    try:
        price = Decimal(value)
    except InvalidOperation:
        return None
    if not price.is_finite() or price < 0:
        return None
    return float(price)


def _is_variable_price(pricing: dict) -> bool:
    return any(pricing.get(field) == "-1" for field in ("prompt", "completion"))


# ---------------------------------------------------------------------------
# Account
# ---------------------------------------------------------------------------


def normalize_key_info(data: Any) -> dict[str, Any]:
    """``GET /key``: the calling key's limit and spend (USD)."""
    if not isinstance(data, dict):
        raise _error("invalid_response", "key: not an object")
    free = data.get("free_model_daily_requests")
    free = free if isinstance(free, dict) else {}
    return {
        "label": _str(data.get("label"), 120),
        "limit": _num(data.get("limit")),
        "limit_remaining": _num(data.get("limit_remaining")),
        "limit_reset": data.get("limit_reset") if data.get("limit_reset") in LIMIT_RESETS else None,
        "include_byok_in_limit": _bool(data.get("include_byok_in_limit")),
        "usage": _num(data.get("usage")) or 0.0,
        "usage_daily": _num(data.get("usage_daily")) or 0.0,
        "usage_weekly": _num(data.get("usage_weekly")) or 0.0,
        "usage_monthly": _num(data.get("usage_monthly")) or 0.0,
        "byok_usage": _num(data.get("byok_usage")) or 0.0,
        "byok_usage_daily": _num(data.get("byok_usage_daily")) or 0.0,
        "byok_usage_weekly": _num(data.get("byok_usage_weekly")) or 0.0,
        "byok_usage_monthly": _num(data.get("byok_usage_monthly")) or 0.0,
        "is_free_tier": _bool(data.get("is_free_tier")),
        "is_management_key": _bool(data.get("is_management_key")) or _bool(data.get("is_provisioning_key")),
        "expires_at": _str(data.get("expires_at"), 40),
        "free_model_daily_requests": {
            "limit": _int(free.get("limit")),
            "remaining": _int(free.get("remaining")),
            "used": _int(free.get("used")),
        }
        if free
        else None,
        "allowed_data_regions": _str_list(data.get("allowed_data_regions"), 10),
    }


async def get_key_info(api_key: str) -> dict[str, Any]:
    payload = await request("GET", "/key", key=api_key, action="read the API key")
    return normalize_key_info(_data(payload, "key"))


async def get_credits(management_key: str) -> dict[str, Any]:
    """``GET /credits`` (management key): credits bought and used, all keys together."""
    payload = await request(
        "GET", "/credits", key=management_key, action="read credits", management=True
    )
    data = _data(payload, "credits")
    if not isinstance(data, dict):
        raise _error("invalid_response", "credits: not an object")
    total = _num(data.get("total_credits")) or 0.0
    used = _num(data.get("total_usage")) or 0.0
    return {"total_credits": total, "total_usage": used, "remaining": total - used}


def normalize_activity_row(row: Any) -> dict[str, Any] | None:
    if not isinstance(row, dict):
        return None
    date = row.get("date")
    if not isinstance(date, str):
        return None
    return {
        # OpenRouter has sent both "2026-09-30" and a full timestamp here.
        "date": date[:10],
        "model": _str(row.get("model"), 200) or "",
        "provider_name": _str(row.get("provider_name"), 120) or "",
        "usage": _num(row.get("usage")) or 0.0,
        "byok_usage_inference": _num(row.get("byok_usage_inference")) or 0.0,
        "requests": _int(row.get("requests")) or 0,
        "prompt_tokens": _int(row.get("prompt_tokens")) or 0,
        "completion_tokens": _int(row.get("completion_tokens")) or 0,
        "reasoning_tokens": _int(row.get("reasoning_tokens")) or 0,
        "cached_tokens": _int(row.get("cached_tokens")) or 0,
    }


async def get_activity(
    management_key: str, *, date: str | None = None, api_key_hash: str | None = None
) -> list[dict[str, Any]]:
    """``GET /activity`` (management key): spend per day, model and provider.

    The last 30 completed UTC days; ``date`` narrows it to one of them and
    ``api_key_hash`` to one key.
    """
    params: dict[str, str] = {}
    if date is not None:
        if not _DATE_RE.fullmatch(date):
            raise ValueError("date must be YYYY-MM-DD")
        params["date"] = date
    if api_key_hash is not None:
        if not KEY_HASH_RE.fullmatch(api_key_hash):
            raise ValueError("api_key_hash must be a key hash")
        params["api_key_hash"] = api_key_hash
    payload = await request(
        "GET", "/activity", key=management_key, action="read activity",
        params=params or None, management=True,
    )
    rows = _data(payload, "activity")
    if not isinstance(rows, list):
        raise _error("invalid_response", "activity: not a list")
    return [r for r in (normalize_activity_row(row) for row in rows) if r is not None]


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


def normalize_model(model: Any) -> dict[str, Any] | None:
    """One catalog entry, typed, with prices per million tokens."""
    if not isinstance(model, dict):
        return None
    model_id = model.get("id")
    if not isinstance(model_id, str) or not is_valid_model_id(model_id):
        return None
    pricing = model.get("pricing") if isinstance(model.get("pricing"), dict) else {}
    architecture = model.get("architecture") if isinstance(model.get("architecture"), dict) else {}
    top = model.get("top_provider") if isinstance(model.get("top_provider"), dict) else {}
    supported = _str_list(model.get("supported_parameters"), 60)
    prompt = _price_per_million(pricing.get("prompt"))
    completion = _price_per_million(pricing.get("completion"))
    created = _int(model.get("created"))
    return {
        "id": model_id,
        "name": _str(model.get("name"), 200) or model_id,
        # An alias (``~google/...``) is filed under its author.
        "author": model_id.split("/", 1)[0].removeprefix("~"),
        "created": datetime.fromtimestamp(created, UTC).isoformat() if created else None,
        "description": _str(model.get("description"), _MAX_DESCRIPTION_CHARS),
        "context_length": _int(model.get("context_length")) or _int(top.get("context_length")),
        "max_completion_tokens": _int(top.get("max_completion_tokens")),
        "is_moderated": _bool(top.get("is_moderated")),
        "input_modalities": _str_list(architecture.get("input_modalities"), 10),
        "output_modalities": _str_list(architecture.get("output_modalities"), 10),
        "pricing": {
            "prompt": prompt,
            "completion": completion,
            "input_cache_read": _price_per_million(pricing.get("input_cache_read")),
            "input_cache_write": _price_per_million(pricing.get("input_cache_write")),
            "internal_reasoning": _price_per_million(pricing.get("internal_reasoning")),
            "request": _unit_price(pricing.get("request")),
            "image": _unit_price(pricing.get("image")),
            "web_search": _unit_price(pricing.get("web_search")),
        },
        "variable_pricing": _is_variable_price(pricing),
        "is_free": prompt == 0 and completion == 0,
        "supports_tools": "tools" in supported,
        "supports_reasoning": "reasoning" in supported or "include_reasoning" in supported,
        "supports_structured_outputs": "structured_outputs" in supported or "response_format" in supported,
        "supported_parameters": supported,
        "knowledge_cutoff": _str(model.get("knowledge_cutoff"), 20),
        "expiration_date": _str(model.get("expiration_date"), 20),
    }


async def list_models(api_key: str | None, *, kind: str = "chat") -> list[dict[str, Any]]:
    """The catalog: text models (``kind="chat"``) or embedding models.

    Sent with the integration's key: an organization that filters its catalog
    gets the models its keys may use, not the public list.
    """
    path = "/embeddings/models" if kind == "embeddings" else "/models"
    payload = await request("GET", path, key=api_key, action="list models")
    rows = _data(payload, "models")
    if not isinstance(rows, list):
        raise _error("invalid_response", "models: not a list")
    models = [m for m in (normalize_model(row) for row in rows) if m is not None]
    models.sort(key=lambda m: m["id"])
    return models


#: The catalog changes a few times a day; the console reads it on every visit.
_CATALOG_TTL_SECONDS = 300.0
_CATALOG_MAX_ENTRIES = 32
_catalog_cache: dict[tuple[str, str], tuple[float, list[dict[str, Any]]]] = {}
_catalog_locks: dict[tuple[str, str], asyncio.Lock] = {}


async def cached_models(api_key: str | None, *, kind: str = "chat") -> list[dict[str, Any]]:
    """:func:`list_models`, kept for five minutes per key and kind.

    Per key, since an organization may filter the catalog its keys see.
    Concurrent misses for the same key wait on one fetch.
    """
    cache_key = (key_hash(api_key) if api_key else "", kind)
    hit = _catalog_cache.get(cache_key)
    if hit is not None and time.monotonic() - hit[0] < _CATALOG_TTL_SECONDS:
        return hit[1]
    lock = _catalog_locks.setdefault(cache_key, asyncio.Lock())
    async with lock:
        hit = _catalog_cache.get(cache_key)
        if hit is not None and time.monotonic() - hit[0] < _CATALOG_TTL_SECONDS:
            return hit[1]
        models = await list_models(api_key, kind=kind)
        if cache_key not in _catalog_cache and len(_catalog_cache) >= _CATALOG_MAX_ENTRIES:
            oldest = min(_catalog_cache, key=lambda k: _catalog_cache[k][0])
            _catalog_cache.pop(oldest, None)
            _catalog_locks.pop(oldest, None)
        _catalog_cache[cache_key] = (time.monotonic(), models)
        return models


def normalize_endpoint(endpoint: Any) -> dict[str, Any] | None:
    if not isinstance(endpoint, dict):
        return None
    pricing = endpoint.get("pricing") if isinstance(endpoint.get("pricing"), dict) else {}
    latency = endpoint.get("latency_last_30m") if isinstance(endpoint.get("latency_last_30m"), dict) else {}
    throughput = (
        endpoint.get("throughput_last_30m") if isinstance(endpoint.get("throughput_last_30m"), dict) else {}
    )
    return {
        "provider_name": _str(endpoint.get("provider_name"), 120) or "",
        "tag": _str(endpoint.get("tag"), 120),
        "quantization": _str(endpoint.get("quantization"), 20),
        "context_length": _int(endpoint.get("context_length")),
        "max_completion_tokens": _int(endpoint.get("max_completion_tokens")),
        "status": _int(endpoint.get("status")),
        "uptime_last_30m": _num(endpoint.get("uptime_last_30m")),
        "uptime_last_1d": _num(endpoint.get("uptime_last_1d")),
        "latency_p50_ms": _num(latency.get("p50")),
        "throughput_p50": _num(throughput.get("p50")),
        "supports_implicit_caching": _bool(endpoint.get("supports_implicit_caching")),
        "supports_tools": "tools" in _str_list(endpoint.get("supported_parameters"), 60),
        "pricing": {
            "prompt": _price_per_million(pricing.get("prompt")),
            "completion": _price_per_million(pricing.get("completion")),
            "input_cache_read": _price_per_million(pricing.get("input_cache_read")),
        },
    }


async def list_model_endpoints(api_key: str | None, model_id: str) -> list[dict[str, Any]]:
    """The providers serving ``model_id``, with their price, uptime and speed."""
    if not is_valid_model_id(model_id):
        raise ValueError("not an OpenRouter model id")
    # The endpoints route takes the model without its variant suffix.
    author, slug = model_id.split(":", 1)[0].split("/", 1)
    payload = await request(
        "GET", f"/models/{author}/{slug}/endpoints", key=api_key, action="list model endpoints"
    )
    data = _data(payload, "endpoints")
    endpoints = data.get("endpoints") if isinstance(data, dict) else None
    if not isinstance(endpoints, list):
        raise _error("invalid_response", "endpoints: not a list")
    return [e for e in (normalize_endpoint(row) for row in endpoints) if e is not None]


# ---------------------------------------------------------------------------
# API keys (management key)
# ---------------------------------------------------------------------------


def normalize_api_key(key: Any) -> dict[str, Any] | None:
    if not isinstance(key, dict):
        return None
    key_id = key.get("hash")
    if not isinstance(key_id, str) or not KEY_HASH_RE.fullmatch(key_id):
        return None
    return {
        "hash": key_id,
        "name": _str(key.get("name"), 200) or "",
        "label": _str(key.get("label"), 120) or "",
        "disabled": _bool(key.get("disabled")),
        "limit": _num(key.get("limit")),
        "limit_remaining": _num(key.get("limit_remaining")),
        "limit_reset": key.get("limit_reset") if key.get("limit_reset") in LIMIT_RESETS else None,
        "include_byok_in_limit": _bool(key.get("include_byok_in_limit")),
        "usage": _num(key.get("usage")) or 0.0,
        "usage_daily": _num(key.get("usage_daily")) or 0.0,
        "usage_weekly": _num(key.get("usage_weekly")) or 0.0,
        "usage_monthly": _num(key.get("usage_monthly")) or 0.0,
        "created_at": _str(key.get("created_at"), 40),
        "updated_at": _str(key.get("updated_at"), 40),
        "expires_at": _str(key.get("expires_at"), 40),
    }


@dataclass
class KeyListing:
    keys: list[dict[str, Any]]
    #: True when the account holds more keys than were listed.
    truncated: bool


async def list_api_keys(management_key: str, *, include_disabled: bool = True) -> KeyListing:
    keys: list[dict[str, Any]] = []
    async with egress_client(timeout=_TIMEOUT, follow_redirects=False) as client:
        for page in range(_MAX_KEY_PAGES):
            payload = await request(
                "GET", "/keys", key=management_key, action="list API keys", management=True,
                params={"include_disabled": "true" if include_disabled else "false",
                        "offset": page * _KEYS_PAGE_SIZE},
                client=client,
            )
            rows = _data(payload, "keys")
            if not isinstance(rows, list):
                raise _error("invalid_response", "keys: not a list")
            keys.extend(k for k in (normalize_api_key(row) for row in rows) if k is not None)
            if len(rows) < _KEYS_PAGE_SIZE:
                return KeyListing(keys=keys, truncated=False)
    return KeyListing(keys=keys, truncated=True)


async def create_api_key(management_key: str, body: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Create a key; returns it and its secret, which OpenRouter shows only once."""
    payload = await request(
        "POST", "/keys", key=management_key, action="create an API key",
        json_body=body, management=True,
    )
    created = normalize_api_key(_data(payload, "key"))
    secret = payload.get("key") if isinstance(payload, dict) else None
    if created is None or not isinstance(secret, str) or not secret:
        raise _error("invalid_response", "create key: no key in the answer")
    return created, secret


async def update_api_key(management_key: str, hash_: str, body: dict[str, Any]) -> dict[str, Any]:
    if not KEY_HASH_RE.fullmatch(hash_):
        raise ValueError("not a key hash")
    payload = await request(
        "PATCH", f"/keys/{hash_}", key=management_key, action="update an API key",
        json_body=body, management=True,
    )
    updated = normalize_api_key(_data(payload, "key"))
    if updated is None:
        raise _error("invalid_response", "update key: no key in the answer")
    return updated


async def delete_api_key(management_key: str, hash_: str) -> None:
    if not KEY_HASH_RE.fullmatch(hash_):
        raise ValueError("not a key hash")
    payload = await request(
        "DELETE", f"/keys/{hash_}", key=management_key, action="delete an API key", management=True
    )
    if not isinstance(payload, dict) or payload.get("deleted") is not True:
        raise _error("invalid_response", "delete key: not confirmed")
