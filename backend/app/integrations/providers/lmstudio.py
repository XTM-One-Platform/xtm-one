# SPDX-License-Identifier: Apache-2.0

"""LM Studio integration - an LM Studio server reached through an XTM One Relay.

LM Studio (https://lmstudio.ai) runs models on a workstation or a GPU server
inside the customer's network. The platform never connects to it: the
integration is routed through a relay group, and every call leaves from the
relay installed next to the server (``core/egress.py``). A call made with no
route is refused here (``not_routed``), whoever the caller is, so a base URL
naming a private address is only ever reached from the customer's network,
where the relay checks its own allow-list.

What this module calls is LM Studio's native REST API v1 (LM Studio 0.4 and
later, https://lmstudio.ai/docs/developer/rest):

* ``GET /api/v1/models`` - the models on disk, each with its loaded instances;
* ``POST /api/v1/models/load`` and ``/unload`` - load a model into memory with
  its load options, unload one instance;
* ``POST /api/v1/models/download`` and ``GET /api/v1/models/download/status/{job}``
  - fetch a model from the LM Studio catalog or Hugging Face, and follow it.

Inference goes through the platform LLM provider fed by the integration
(``app/llm/lmstudio_provider.py``, OpenAI-compatible ``/v1``), on the same
route. The API token is optional: LM Studio asks for one only when "Require
Authentication" is on (``Authorization: Bearer``); it is an extra-config
field (``api_token``), so an integration without one is connected as soon as
its server URL is set (``auth_type`` ``none``).

Every value LM Studio returns is typed on the way in, a body is read in bounded
raw chunks, and a failure is raised as :class:`LMStudioError`, whose
``public_message`` is a fixed sentence: LM Studio's own wording and the relay's
detail only reach the log.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from app.core.egress import current_route, egress_client
from app.integrations.http import PLATFORM_USER_AGENT

logger = logging.getLogger(__name__)

PROVIDER = "lmstudio"
#: The extra-config key the server URL is stored under.
BASE_URL_FIELD = "base_url"
#: The extra-config key of the optional API token. Its name makes the
#: integration API redact it and the forms mask it (``token``).
TOKEN_FIELD = "api_token"  # noqa: S105 - a field name, not a secret

#: A listing or a download call answers in a second; loading a large model
#: takes minutes (the relay carries a request for 15 minutes at most).
_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
_LOAD_TIMEOUT = httpx.Timeout(connect=10.0, read=600.0, write=30.0, pool=10.0)
#: A listing of a few hundred models is well under 1 MB.
_MAX_BODY_BYTES = 8 * 1024 * 1024
_MAX_MODELS = 2000

#: A model key or an instance id, as LM Studio names them
#: (``openai/gpt-oss-20b``, ``qwen3-4b-2507``, ``text-embedding-nomic-embed-text-v1.5``,
#: an instance ``openai/gpt-oss-20b:2``). Sent in JSON bodies only.
MODEL_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@:/+-]{0,299}$")
#: A download job id (``job_493c7c9ded``): the one value that reaches a URL path.
JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$")
QUANTIZATION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,39}$")
#: Hugging Face links a download may name (LM Studio accepts "exact Hugging
#: Face links" besides its catalog ids).
_HUGGING_FACE_HOSTS = frozenset({"huggingface.co", "www.huggingface.co", "hf.co"})

MODEL_TYPES = ("llm", "embedding", "decision")
DOWNLOAD_STATUSES = ("downloading", "paused", "completed", "failed", "already_downloaded")
REASONING_OPTIONS = ("off", "on", "low", "medium", "high")


class LMStudioError(Exception):
    """A call LM Studio, or the relay in front of it, refused or could not answer.

    ``kind`` decides the HTTP status the console answers with; ``public_message``
    is what the caller is shown. ``detail`` is for the log only.
    """

    def __init__(self, kind: str, public_message: str, *, detail: str = "") -> None:
        super().__init__(public_message)
        self.kind = kind
        self.public_message = public_message
        self.detail = detail


PUBLIC_MESSAGES = {
    "not_routed": (
        "Route this integration through a relay group first: LM Studio is reached from the "
        "relay installed in its network, never from the platform."
    ),
    "no_base_url": "Set the LM Studio server URL in the Configuration tab.",
    "invalid_base_url": (
        "The LM Studio server URL must be an http or https URL with a host and no credentials "
        "(for example http://gpu-host:1234)."
    ),
    "auth": (
        "LM Studio refused the API token. Set a valid token in the Configuration tab, or turn "
        "Require Authentication off in LM Studio's server settings."
    ),
    "not_found": "LM Studio has no such model, instance or download.",
    "unsupported": (
        "This LM Studio server does not serve the REST API v1 (/api/v1). Update LM Studio to 0.4 or later."
    ),
    "invalid_request": "LM Studio refused the request.",
    "unavailable": (
        "LM Studio could not be reached through the relay. Check that its server is started and "
        "that the relay may reach the server URL."
    ),
    "invalid_response": "LM Studio sent an answer this page cannot read.",
}

#: A relay failure, by the relay's code: the platform's own fixed sentences
#: (``relay_transport.RELAY_ERROR_MESSAGES``), never the relay's detail.
_RELAY_HINT = " Check the relay group on the Relays page."


def _error(kind: str, detail: str = "") -> LMStudioError:
    return LMStudioError(kind, PUBLIC_MESSAGES[kind], detail=detail)


def _relay_failure(exc: Exception, action: str) -> LMStudioError:
    from app.services.relay_transport import RELAY_ERROR_MESSAGES

    code = str(getattr(exc, "code", "") or "")
    sentence = RELAY_ERROR_MESSAGES.get(code, RELAY_ERROR_MESSAGES["protocol_error"])
    return LMStudioError(f"relay_{code or 'error'}", f"{sentence}.{_RELAY_HINT}", detail=f"{action}: {exc}")


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Connection:
    """Where the server is and the token it may ask for."""

    base_url: str
    token: str | None = None

    def headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "User-Agent": PLATFORM_USER_AGENT,
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers


#: Path suffixes an admin may paste with the server URL: the OpenAI base
#: (``/v1``) or a native API root. The server's root is what is stored.
_API_SUFFIXES = ("/api/v1", "/api/v0", "/v1")


def normalize_base_url(raw: Any) -> str:
    """The server's root URL (``scheme://host[:port][/prefix]``), or raise.

    Refuses anything but http(s) with a host, credentials in the URL, a query
    and a fragment. A reverse-proxy prefix is kept; an API suffix is removed.
    """
    text = raw.strip() if isinstance(raw, str) else ""
    if not text:
        raise _error("no_base_url")
    try:
        parts = urlsplit(text)
        host = parts.hostname
        port = parts.port
    except ValueError as exc:
        raise _error("invalid_base_url", "unparsable URL") from exc
    if (
        parts.scheme not in ("http", "https")
        or not host
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or any(ch.isspace() for ch in text)
    ):
        raise _error("invalid_base_url", "scheme, host, userinfo, query or fragment")
    path = parts.path.rstrip("/")
    for suffix in _API_SUFFIXES:
        if path.endswith(suffix):
            path = path[: -len(suffix)]
            break
    if ".." in path.split("/"):
        raise _error("invalid_base_url", "dot segment")
    netloc = parts.netloc
    if port is None and ":" in netloc.rsplit("]", 1)[-1]:
        # ``http://host:`` - an empty port.
        raise _error("invalid_base_url", "empty port")
    return urlunsplit((parts.scheme, netloc, path, "", ""))


def make_connection(base_url: Any, token: Any) -> Connection:
    token_text = token.strip() if isinstance(token, str) else ""
    if token_text.lower() in ("none", "dummy"):
        token_text = ""
    if any(ch in token_text for ch in "\r\n"):
        raise _error("auth", "token with a line break")
    return Connection(base_url=normalize_base_url(base_url), token=token_text or None)


async def integration_connection(integration: Any) -> Connection:
    """The connection an LM Studio integration row describes: its server URL and optional token."""
    from app.services.vault import reveal_json_field

    try:
        extra = await reveal_json_field(integration, "extra_config_enc") if integration.extra_config_enc else None
    except Exception:
        logger.warning("LM Studio integration %s: extra config unreadable", integration.id)
        extra = None
    if not isinstance(extra, dict):
        extra = {}
    return make_connection(extra.get(BASE_URL_FIELD), extra.get(TOKEN_FIELD))


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


def _upstream_message(body: bytes) -> str:
    """LM Studio's error sentence, for the log only, quoted and truncated."""
    try:
        parsed = json.loads(body.decode("utf-8", errors="replace"))
        error = parsed.get("error") if isinstance(parsed, dict) else None
        message = error.get("message") if isinstance(error, dict) else error
    except (ValueError, AttributeError):
        message = None
    text = message if isinstance(message, str) else body[:200].decode("utf-8", errors="replace")
    return repr(text[:200])


async def _read_capped(response: httpx.Response) -> bytes:
    """The raw body, at most ``_MAX_BODY_BYTES``; one byte more ends the transfer."""
    encoding = (response.headers.get("content-encoding") or "identity").strip().lower()
    if encoding not in ("", "identity"):
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
    connection: Connection,
    method: str,
    path: str,
    *,
    action: str,
    json_body: dict[str, Any] | None = None,
    timeout: httpx.Timeout = _TIMEOUT,
    missing: str = "not_found",
) -> Any:
    """Call ``base_url + path`` through the current relay route; the parsed JSON body.

    ``path`` is always built by this module from validated parts. ``missing``
    is the kind a 404 is reported as (``unsupported`` for the listing, which
    only an LM Studio without the v1 API answers with a 404).
    """
    if not path.startswith("/api/v1/") or "//" in path or ".." in path:
        raise ValueError(f"unexpected LM Studio path {path!r}")
    if current_route() is None:
        # Fail closed: the server is in the customer's network, never reached
        # from the platform's own.
        raise _error("not_routed", action)
    url = f"{connection.base_url}{path}"
    try:
        async with (
            egress_client(timeout=timeout, follow_redirects=False) as client,
            client.stream(method, url, headers=connection.headers(), json=json_body) as response,
        ):
            body = await _read_capped(response)
            status = response.status_code
            content_type = (response.headers.get("content-type") or "").lower()
    except LMStudioError:
        raise
    except httpx.HTTPError as exc:
        if getattr(exc, "is_relay_error", False):
            raise _relay_failure(exc, action) from exc
        raise _error("unavailable", f"{action}: {type(exc).__name__}") from exc

    if status >= 400:
        upstream = _upstream_message(body)
        logger.warning("LM Studio %s answered %s: %s", action, status, upstream)
        if status in (401, 403):
            raise _error("auth", f"{action}: {status} {upstream}")
        if status == 404:
            raise _error(missing, f"{action}: 404 {upstream}")
        if status in (400, 409, 422):
            raise _error("invalid_request", f"{action}: {status} {upstream}")
        raise _error("unavailable", f"{action}: {status} {upstream}")
    if 300 <= status < 400:
        # Redirects are not followed: a server answering one is not LM Studio's API.
        raise _error("invalid_response", f"{action}: redirect {status}")
    if "json" not in content_type:
        raise _error("invalid_response", f"{action}: content-type {content_type[:60]!r}")
    try:
        return json.loads(body)
    except ValueError as exc:
        raise _error("invalid_response", f"{action}: not JSON") from exc


# ---------------------------------------------------------------------------
# Value hygiene - every field below is LM Studio's, so it is typed on the way in
# ---------------------------------------------------------------------------


def _str(value: Any, limit: int = 200) -> str | None:
    if not isinstance(value, str):
        return None
    return value[:limit]


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def is_valid_model_key(value: Any) -> bool:
    return (
        isinstance(value, str)
        and bool(MODEL_KEY_RE.fullmatch(value))
        and ".." not in value
        and "://" not in value
    )


def is_valid_job_id(value: Any) -> bool:
    return isinstance(value, str) and bool(JOB_ID_RE.fullmatch(value))


def is_valid_download_source(value: Any) -> bool:
    """A catalog id (``owner/model``) or a Hugging Face link."""
    if not isinstance(value, str) or len(value) > 500:
        return False
    if value.startswith("https://"):
        try:
            parts = urlsplit(value)
            host = (parts.hostname or "").lower()
        except ValueError:
            return False
        return (
            host in _HUGGING_FACE_HOSTS
            and parts.username is None
            and parts.port is None
            and not parts.fragment
            and not any(ch.isspace() for ch in value)
        )
    return is_valid_model_key(value)


def _reasoning(value: Any) -> list[str]:
    if not isinstance(value, dict):
        return []
    options = value.get("allowed_options")
    if not isinstance(options, list):
        return []
    return [o for o in options if isinstance(o, str) and o in REASONING_OPTIONS]


def _instance(entry: Any) -> dict | None:
    if not isinstance(entry, dict):
        return None
    instance_id = _str(entry.get("id"), 300)
    if not instance_id or not is_valid_model_key(instance_id):
        return None
    config = entry.get("config") if isinstance(entry.get("config"), dict) else {}
    return {
        "id": instance_id,
        "context_length": _int(config.get("context_length")),
        "flash_attention": _bool(config.get("flash_attention")),
        "parallel": _int(config.get("parallel")),
    }


def _model(entry: Any) -> dict | None:
    if not isinstance(entry, dict):
        return None
    key = _str(entry.get("key"), 300)
    if not key or not is_valid_model_key(key):
        return None
    model_type = entry.get("type")
    quantization = entry.get("quantization") if isinstance(entry.get("quantization"), dict) else {}
    capabilities = entry.get("capabilities") if isinstance(entry.get("capabilities"), dict) else {}
    instances_raw = entry.get("loaded_instances")
    instances = [
        inst for inst in (_instance(i) for i in (instances_raw if isinstance(instances_raw, list) else [])[:50])
        if inst is not None
    ]
    return {
        "key": key,
        "type": model_type if model_type in MODEL_TYPES else "llm",
        "display_name": _str(entry.get("display_name")) or key,
        "publisher": _str(entry.get("publisher")),
        "architecture": _str(entry.get("architecture"), 100),
        "format": _str(entry.get("format"), 20),
        "quantization": _str(quantization.get("name"), 40),
        "bits_per_weight": _num(quantization.get("bits_per_weight")),
        "size_bytes": _int(entry.get("size_bytes")),
        "params": _str(entry.get("params_string"), 40),
        "max_context_length": _int(entry.get("max_context_length")),
        "vision": bool(_bool(capabilities.get("vision"))),
        "tool_use": bool(_bool(capabilities.get("trained_for_tool_use"))),
        "reasoning_options": _reasoning(capabilities.get("reasoning") or entry.get("reasoning")),
        "description": _str(entry.get("description"), 600),
        "loaded_instances": instances,
    }


def _job(payload: Any, action: str) -> dict:
    if not isinstance(payload, dict):
        raise _error("invalid_response", f"{action}: not an object")
    status = payload.get("status")
    if status not in DOWNLOAD_STATUSES:
        raise _error("invalid_response", f"{action}: status {str(status)[:40]!r}")
    job_id = payload.get("job_id")
    return {
        "job_id": job_id if is_valid_job_id(job_id) else None,
        "status": status,
        "total_size_bytes": _int(payload.get("total_size_bytes")),
        "downloaded_bytes": _int(payload.get("downloaded_bytes")),
        "bytes_per_second": _num(payload.get("bytes_per_second")),
        "started_at": _str(payload.get("started_at"), 40),
        "estimated_completion": _str(payload.get("estimated_completion"), 40),
        "completed_at": _str(payload.get("completed_at"), 40),
    }


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


async def list_models(connection: Connection) -> list[dict]:
    """The models on the server, chat and embedding alike, each with its loaded instances."""
    payload = await request(
        connection, "GET", "/api/v1/models", action="list models", missing="unsupported"
    )
    return parse_models(payload)


def parse_models(payload: Any) -> list[dict]:
    """The models of a ``GET /api/v1/models`` answer, typed; chat models first."""
    entries = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        raise _error("invalid_response", "list models: no models array")
    models = [m for m in (_model(e) for e in entries[:_MAX_MODELS]) if m is not None]
    models.sort(key=lambda m: (m["type"] != "llm", m["display_name"].lower()))
    return models


async def load_model(
    connection: Connection,
    model: str,
    *,
    context_length: int | None = None,
    flash_attention: bool | None = None,
    eval_batch_size: int | None = None,
    offload_kv_cache_to_gpu: bool | None = None,
) -> dict:
    """Load ``model`` into memory; the new instance."""
    if not is_valid_model_key(model):
        raise _error("invalid_request", "load: invalid model key")
    body: dict[str, Any] = {"model": model}
    for name, value in (
        ("context_length", context_length),
        ("flash_attention", flash_attention),
        ("eval_batch_size", eval_batch_size),
        ("offload_kv_cache_to_gpu", offload_kv_cache_to_gpu),
    ):
        if value is not None:
            body[name] = value
    payload = await request(
        connection, "POST", "/api/v1/models/load", action="load model", json_body=body, timeout=_LOAD_TIMEOUT
    )
    if not isinstance(payload, dict):
        raise _error("invalid_response", "load model: not an object")
    instance_id = payload.get("instance_id")
    if not is_valid_model_key(instance_id):
        raise _error("invalid_response", "load model: no instance id")
    model_type = payload.get("type")
    return {
        "instance_id": instance_id,
        "type": model_type if model_type in MODEL_TYPES else "llm",
        "load_time_seconds": _num(payload.get("load_time_seconds")),
    }


async def unload_model(connection: Connection, instance_id: str) -> str:
    """Unload one loaded instance; its id."""
    if not is_valid_model_key(instance_id):
        raise _error("invalid_request", "unload: invalid instance id")
    payload = await request(
        connection,
        "POST",
        "/api/v1/models/unload",
        action="unload model",
        json_body={"instance_id": instance_id},
    )
    returned = payload.get("instance_id") if isinstance(payload, dict) else None
    return returned if is_valid_model_key(returned) else instance_id


async def download_model(connection: Connection, model: str, *, quantization: str | None = None) -> dict:
    """Start downloading ``model`` (a catalog id or a Hugging Face link); its job."""
    if not is_valid_download_source(model):
        raise _error("invalid_request", "download: invalid source")
    body: dict[str, Any] = {"model": model}
    if quantization is not None:
        if not QUANTIZATION_RE.fullmatch(quantization):
            raise _error("invalid_request", "download: invalid quantization")
        body["quantization"] = quantization
    payload = await request(
        connection, "POST", "/api/v1/models/download", action="download model", json_body=body
    )
    return _job(payload, "download model")


async def download_status(connection: Connection, job_id: str) -> dict:
    """Where download ``job_id`` stands."""
    if not is_valid_job_id(job_id):
        raise _error("not_found", "download status: invalid job id")
    payload = await request(
        connection,
        "GET",
        f"/api/v1/models/download/status/{job_id}",
        action="download status",
    )
    return _job(payload, "download status")


# ---------------------------------------------------------------------------
# Connectivity test
# ---------------------------------------------------------------------------


async def check_connectivity(ctx: Any) -> Any:
    """``POST /integrations/{id}/test``: the model listing, through the integration's relay."""
    from app.integrations.connectivity import ProbeResult

    integration = ctx.integration
    if integration is None:
        return ProbeResult.failed(f"{ctx.name}: nothing to test without the integration.")
    if getattr(integration, "relay_group_id", None) is None:
        return ProbeResult.failed(PUBLIC_MESSAGES["not_routed"])
    try:
        connection = await integration_connection(integration)
        models = await list_models(connection)
    except LMStudioError as exc:
        logger.info("LM Studio connectivity test: %s (%s)", exc.kind, exc.detail[:300])
        return ProbeResult.failed(f"{ctx.name}: {exc.public_message}")
    loaded = sum(1 for m in models if m["loaded_instances"])
    count = f"{len(models)} model{'' if len(models) == 1 else 's'}"
    return ProbeResult.ok(f"{ctx.name} is connected through the relay: {count} on the server, {loaded} loaded.")


# ---------------------------------------------------------------------------
# Agent tools
# ---------------------------------------------------------------------------

#: The tool that reads the server's models, for an agent or a dashboard's integration source.
LIST_MODELS_TOOL = "list_lmstudio_models"


def build_tools(token: str, executor: Any, **_kwargs: Any) -> list[Any]:
    """What an agent reads on the LM Studio server: its models, as JSON (``base_url||api_token``).

    Read only: loading, unloading and downloading stay in the integration's console, behind its managers. The calls
    leave through the integration's relay route, like the console's (``integration_tools._route_through_relay``).
    """
    from app.integrations.http import parse_compound_token
    from app.tools.base import ToolDefinition

    base_raw, api_token = parse_compound_token(token, 2, greedy_last=True)
    if not (base_raw or "").strip():
        return []

    async def list_lmstudio_models(type: str | None = None, loaded_only: bool = False) -> str:
        try:
            models = await list_models(make_connection(base_raw, api_token))
        except LMStudioError as exc:
            logger.info("LM Studio tool: %s (%s)", exc.kind, exc.detail[:300])
            return f"Error: {exc.public_message}"
        rows = [
            {
                "model": m["display_name"],
                "key": m["key"],
                "type": m["type"],
                "publisher": m["publisher"],
                "architecture": m["architecture"],
                "params": m["params"],
                "quantization": m["quantization"],
                "size_gb": round(m["size_bytes"] / 1e9, 2) if m["size_bytes"] else None,
                "max_context_length": m["max_context_length"],
                "loaded_instances": len(m["loaded_instances"]),
                "vision": m["vision"],
                "tool_use": m["tool_use"],
            }
            for m in models
            if (not type or m["type"] == type) and (not loaded_only or m["loaded_instances"])
        ]
        return json.dumps(
            {"models": rows, "count": len(rows), "loaded": sum(1 for r in rows if r["loaded_instances"])},
            ensure_ascii=False,
        )

    executor.register(LIST_MODELS_TOOL, list_lmstudio_models)
    return [
        ToolDefinition(
            name=LIST_MODELS_TOOL,
            description=(
                "List the models on the LM Studio server, as JSON: each model's name, key, type (llm, embedding), "
                "publisher, architecture, parameters, quantization, size in GB, maximum context and how many "
                "instances are loaded; with the count of models and of loaded ones."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": list(MODEL_TYPES), "description": "Only the models of this type."},
                    "loaded_only": {"type": "boolean", "description": "Only the models loaded in memory."},
                },
            },
            read_only=True,
        )
    ]


def _register() -> None:
    from app.integrations.connectivity import register_connectivity

    register_connectivity(PROVIDER, check_connectivity)


_register()
