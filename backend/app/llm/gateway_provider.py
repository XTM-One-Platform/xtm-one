# SPDX-License-Identifier: Apache-2.0

"""Filigran AI Gateway provider.

Speaks the gateway's OpenAI-compatible API (``/v1/chat/completions``,
``/v1/models``) but authenticates with the XTM Suite JWT scheme instead of a
static API key: every outgoing HTTP request carries a freshly signed EdDSA
JWT (:func:`app.core.xtm_auth.issue_xtm_jwt`) whose ``aud`` is the gateway's
base URL and whose ``sub``/``email`` identify the acting user (resolved from
the LLM caller context — see :mod:`app.llm.identity`).

Credential format (stored in the usual ``api_key_enc`` slot, like Ollama's
URL-only credential):

- ``<gateway_base_url>``
- ``<gateway_base_url>||<target_provider>`` — optionally pins the gateway's
  upstream provider via the ``X-Target-Provider`` header.
- ``<gateway_base_url>||<target_provider>||<token>`` — optional static token;
  the target may be empty (``<url>||||<token>``).

Without a token no API key is needed: trust is established by the gateway
operator registering this platform's ``BASE_URL`` as a trusted issuer, and by
this platform's public keys being served at ``{BASE_URL}/xtm/auth/jwks``. With
a token, it is sent as the bearer instead of the XTM JWT.

The provider instance is deliberately user-agnostic (identity is read per
request from the ContextVar), so the router's shared provider cache and the
failover wrapper keep working unchanged. Do not store per-user state here.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from collections.abc import Generator
from dataclasses import replace
from typing import Any, NamedTuple
from urllib.parse import quote

import httpx
import openai

from app.core.utils import format_exception_message
from app.core.xtm_auth import _normalize_url, issue_xtm_jwt
from app.llm.anthropic_provider import build_cache_control
from app.llm.base import ChatMessage
from app.llm.decisions import DECISIONS_OPENAI
from app.llm.elevenlabs_provider import PHONEME_MODELS, VOICES_TTL_SECONDS, account_voice, own_voices_first
from app.llm.identity import LLMCallerIdentity, resolve_llm_caller
from app.llm.model_capabilities import is_claude_model, is_gemini_model
from app.llm.music import (
    MusicGenerationError,
    MusicProviderBusyError,
    MusicRequest,
    MusicResult,
    clamp_seconds,
    status_error,
)
from app.llm.openai_compat import (
    Endpoint,
    OpenAICompatibleProvider,
    declared_model_types,
    endpoint_of,
    gateway_upstream_refusal,
    marked_not_retryable,
)
from app.llm.speech import (
    PronunciationSupport,
    SpeechRequest,
    SpeechResult,
    Voice,
    _speaks_openai_voices,
    pick_voice,
)
from app.llm.system_prompt_split import split_system_prompt

logger = logging.getLogger(__name__)

# A model the gateway does not list is looked up again after this.
_OWNER_MISS_TTL_SECONDS = 30.0

# An owner lookup on the chat path never waits out the SDK's retries or read timeout.
_OWNER_LISTING_TIMEOUT = httpx.Timeout(5.0)
# A voices read answers within the 20 s the speech settings wait for it.
_VOICES_TIMEOUT = httpx.Timeout(15.0)
# The listing's default models are read again after this.
_DEFAULTS_TTL_SECONDS = 300.0
# The types whose default model the listing marks (``"default_for": [...]``, XTM-One-Platform/ai-gateway#391).
_DEFAULT_TYPES = ("image", "speech", "chat", "embedding")
# Upstreams the gateway sends a PDF ``file`` part to natively; each is also a
# ``file_resolver`` key. Bedrock is left out: its key admits Office files too.
_NATIVE_PDF_UPSTREAMS = frozenset({"anthropic", "gemini", "openai"})

# One composition is one request. The read outlasts the gateway's own 10 minute
# upstream wait, so its answer (a refusal included) arrives before the client
# gives up; connecting stays fast, the only network failure that is retried.
_MUSIC_TIMEOUT = httpx.Timeout(connect=10, read=630, write=30, pool=10)


class GatewayCredential(NamedTuple):
    base_url: str
    target_provider: str | None
    token: str | None

    def __repr__(self) -> str:
        # The default repr prints the token when the tuple itself is logged or formatted.
        token = "***" if self.token else None
        return (
            f"GatewayCredential(base_url={self.base_url!r}, "
            f"target_provider={self.target_provider!r}, token={token!r})"
        )


class GatewayModel(NamedTuple):
    """One entry of the gateway's ``/v1/models``: its upstream and what it serves."""

    id: str
    owner: str
    types: tuple[str, ...]


def parse_gateway_credential(raw: str) -> GatewayCredential:
    """Split ``<url>[||<target>[||<token>]]``; empty segments mean "unset"."""
    parts = [p.strip() for p in raw.split("||")]
    base = parts[0].rstrip("/")
    if base.endswith("/v1"):
        base = base[: -len("/v1")].rstrip("/")
    target = parts[1] if len(parts) > 1 and parts[1] else None
    # The token is last, so a `||` inside it stays part of it.
    token = "||".join(parts[2:]) or None
    return GatewayCredential(base, target, token)


def redact_token(text: str, token: str | None) -> str:
    """Hide a static token a gateway may echo in an error body, as is or JSON- or URL-encoded.

    The encoded forms are replaced whatever the token's length. The raw form of a short
    token only as a whole word, or it would garble the text.
    """
    if not token:
        return text
    encoded = {json.dumps(token)[1:-1], quote(token, safe=""), quote(token)} - {token}
    for form in sorted(encoded, key=len, reverse=True):
        text = text.replace(form, "***")
    if len(token) < 8:
        return re.sub(rf"(?<!\w){re.escape(token)}(?!\w)", "***", text)
    return text.replace(token, "***")


def _redact_error_bodies(token: str):
    """httpx response hook: no error body reaches the SDK, and so a message, with the token."""

    async def hook(response: httpx.Response) -> None:
        if response.status_code < 400:
            return
        await response.aread()
        # Not `.text`: it caches the decoded original.
        body = response.content.decode(response.encoding or "utf-8", errors="replace")
        response._content = redact_token(body, token).encode("utf-8")
        response.encoding = "utf-8"

    return hook


async def _obey_retryable_false(response: httpx.Response) -> None:
    """httpx response hook: the SDK re-sends a 5xx twice unless told ``x-should-retry: false``."""
    if response.status_code < 400:
        return
    await response.aread()
    try:
        body = json.loads(response.content)
    except (ValueError, RecursionError):
        return
    if marked_not_retryable(body):
        response.headers["x-should-retry"] = "false"


def _gateway_http_client(
    auth: httpx.Auth, token: str | None, **kwargs
) -> httpx.AsyncClient:
    """The provider's client: the auth hook, ``retryable: false`` obeyed, and the token redacted from error bodies."""
    hooks = [_redact_error_bodies(token)] if token else []
    return httpx.AsyncClient(
        auth=auth,
        event_hooks={"response": [*hooks, _obey_retryable_false]},
        timeout=httpx.Timeout(connect=10, read=600, write=30, pool=10),
        **kwargs,
    )


class _GatewayAuth(httpx.Auth):
    """Common to both auth modes: the bearer, the attribution and target headers."""

    def __init__(self, target_provider: str | None = None):
        self.target_provider = target_provider

    def _bearer(self) -> tuple[str, LLMCallerIdentity | None]:
        raise NotImplementedError

    def auth_flow(
        self, request: httpx.Request
    ) -> Generator[httpx.Request, httpx.Response]:
        bearer, ident = self._bearer()
        # Overwrites the OpenAI SDK's placeholder Authorization header.
        request.headers["Authorization"] = f"Bearer {bearer}"
        if ident:
            request.headers["X-User-Id"] = str(ident.user_id)
            request.headers["X-Source"] = ident.source
        if self.target_provider:
            request.headers["X-Target-Provider"] = self.target_provider
        yield request


class XtmJwtAuth(_GatewayAuth):
    """httpx auth hook that mints a fresh XTM EdDSA JWT per outgoing request.

    Ed25519 signing is in-memory and microsecond-cheap, so per-request
    minting needs no cache and sidesteps the 10-minute token TTL entirely
    (the gateway validates at request start; streams may outlive ``exp``).
    """

    def __init__(self, audience_url: str, target_provider: str | None = None):
        super().__init__(target_provider)
        self.audience_url = audience_url

    def _bearer(self) -> tuple[str, LLMCallerIdentity]:
        ident = resolve_llm_caller()
        return issue_xtm_jwt(ident.user_id, ident.email, self.audience_url), ident


class StaticTokenAuth(_GatewayAuth):
    """httpx auth hook that sends the operator's static token instead of a JWT.

    The ``X-User-Id`` / ``X-Source`` attribution headers still go out, so the
    gateway receives platform user ids even though it did not issue the token.
    """

    def __init__(self, token: str, target_provider: str | None = None):
        super().__init__(target_provider)
        self.token = token

    def _bearer(self) -> tuple[str, LLMCallerIdentity | None]:
        # Nothing to sign, so a missing caller only drops the attribution headers.
        try:
            return self.token, resolve_llm_caller()
        except RuntimeError:
            return self.token, None


class FiligranGatewayProvider(OpenAICompatibleProvider):
    """Filigran AI Gateway — OpenAI-compatible, XTM-JWT-authenticated."""

    provider_name = "filigran_gateway"
    default_model = "default"
    # The gateway serves decision models on OpenAI's Decisions route
    # (``/v1/decisions``) and takes each to its upstream.
    decisions_api = DECISIONS_OPENAI
    decisions_only_when_listed = True
    lists_decision_models = True
    # The gateway returns Claude's thinking blocks as ``thinking_blocks``, and
    # Gemini's thought signatures on its tool calls.
    replays_thinking_blocks = True
    # The gateway's 10 MiB body cap is below some providers' (Anthropic: 32 MB);
    # a provider's own 413 arrives as a 502. Per-provider caps: ai-gateway#360.
    body_cap_error_code = "request_too_large"

    def __init__(self, api_key: str, **kwargs):
        base, target, token = parse_gateway_credential(api_key)
        self._token = token
        self._target = target.lower() if target else None

        self.gateway_url = base
        self.base_url = f"{base}/v1"
        # The JWT `aud` must equal the gateway's own configured public base
        # URL — normalize the same way the issuer side normalizes `iss`.
        audience = _normalize_url(base)

        # Client-level auth is applied by AsyncClient.send() to every request
        # the OpenAI SDK builds — chat, streaming, and models.list() alike.
        kwargs.setdefault(
            "http_client",
            _gateway_http_client(
                StaticTokenAuth(token, target) if token else XtmJwtAuth(audience, target),
                token,
            ),
        )
        # Placeholder — replaced per request by the auth flow, never sent.
        super().__init__(api_key="xtm-jwt", **kwargs)
        # model id → upstream owner, so embed/image calls can pin the target
        # provider (their ids don't always name the provider, e.g.
        # "text-embedding-3-small"). Populated lazily from /v1/models.
        self._owner_cache: dict[str, str | None] = {}
        self._owner_misses: dict[str, float] = {}
        # ``self.client`` and its copy without SDK retries (:meth:`_chat_client`).
        self._retryless_chat: tuple[openai.AsyncOpenAI, openai.AsyncOpenAI] | None = None
        # (model, dimension) pairs the upstream refused: embedded at native size from then on.
        self._refused_dimensions: set[tuple[str, int]] = set()
        # Image models the gateway declares it edits with (``"image_edit": true``, XTM-One-Platform/ai-gateway#358),
        # to whether it lists them taking a mask (``image_mask``, XTM-One-Platform/ai-gateway#369); None when unsaid.
        self._image_edit_masks: dict[str, bool | None] = {}
        # Per type, the model the listing marks default, and until when that listing holds.
        self._listed_defaults: dict[str, str] = {}
        self._listed_defaults_until = 0.0
        # Per model: the ElevenLabs account's voices (``GET /v1/audio/voices``) and when they were read,
        # and a failed read's message until it is tried again.
        self._account_voices: dict[str, tuple[float, list[Voice]]] = {}
        self._account_voice_misses: dict[str, tuple[float, str]] = {}

    def refusal_endpoint(self) -> Endpoint | None:
        # The client's api_key is a placeholder: the row's token and target route it.
        return endpoint_of(self.client, f"{self._token or ''}||{self._target or ''}")

    def _chat_client(self) -> openai.AsyncOpenAI:
        # ``retry_llm_call`` already retries chat 5 times: the SDK's 2 under it sent 18 requests
        # per call into a throttled upstream. Embeddings, media and listings keep the SDK retry.
        if self._retryless_chat is None or self._retryless_chat[0] is not self.client:
            self._retryless_chat = (self.client, self.client.with_options(max_retries=0))
        return self._retryless_chat[1]

    def _reasoning_effort(self, model: str, kwargs: dict) -> str | None:
        # ``disable_thinking`` has no OpenAI-format field: Claude 4.7+ thinks by
        # default within ``max_tokens``, so small JSON calls come back cut. The
        # gateway maps ``none`` to each Claude model's thinking-off form.
        if kwargs.get("disable_thinking") and is_claude_model(model):
            return "none"
        return super()._reasoning_effort(model, kwargs)

    async def _shape_request(self, params: dict, kwargs: dict) -> None:
        if await self._upstream_of(params["model"]) == "anthropic":
            mark_claude_cache_breakpoints(params, build_cache_control(kwargs.get("prompt_cache")))

    async def _upstream_of(self, model: str) -> str | None:
        """Anthropic or Gemini by model name, else the pinned target, else the gateway's listed owner (an alias such as ``default``)."""
        if is_claude_model(model):
            return "anthropic"
        if is_gemini_model(model):
            return "gemini"
        if self._target:
            return self._target
        try:
            owner = await self._owner_of(
                model, self.client.with_options(max_retries=0, timeout=_OWNER_LISTING_TIMEOUT)
            )
        except RuntimeError:
            # A failed listing must not fail the chat call, nor be retried on every one.
            self._owner_misses[model] = time.monotonic() + _OWNER_MISS_TTL_SECONDS
            return None
        return (owner or "").lower() or None

    async def _file_provider_key(self, model: str) -> str:
        # The gateway turns a ``file`` PDF part into each of these upstreams' own
        # document form; the key also brings that upstream's page limit.
        upstream = await self._upstream_of(model)
        return upstream if upstream in _NATIVE_PDF_UPSTREAMS else await super()._file_provider_key(model)

    async def _build_messages(self, messages: list[ChatMessage], model: str) -> tuple[list[dict], int]:
        oai_messages, pdf_tokens = await super()._build_messages(messages, model)
        if await self._upstream_of(model) != "gemini":
            return oai_messages, pdf_tokens
        # Gemini answers a call by tool name. Without it the gateway uses the call
        # id, which restarts at ``call_0`` every response: read the turn's own calls.
        names: dict = {}
        out: list[dict] = []
        for message in oai_messages:
            if message.get("role") == "assistant":
                names = {c.get("id"): (c.get("function") or {}).get("name") for c in message.get("tool_calls") or []}
            elif message.get("role") == "tool" and (name := names.get(message.get("tool_call_id"))):
                message = {**message, "name": name}
            out.append(message)
        return out, pdf_tokens

    async def _models_with_meta(self, client: openai.AsyncOpenAI | None = None) -> list[GatewayModel]:
        """Every gateway model with its upstream owner (``owned_by``) and types (:func:`_types_of`)."""
        try:
            response = await (client or self.client).models.list()
        except Exception as e:
            # `from None`: the chained cause would carry the raw, unredacted text.
            raise RuntimeError(
                f"Failed to list models from Filigran AI Gateway at {self.gateway_url}: "
                f"{redact_token(str(e), self._token)}"
            ) from None
        out: list[GatewayModel] = []
        defaults: dict[str, str] = {}
        edit_masks: dict[str, bool | None] = {}
        for m in response.data:
            owner = getattr(m, "owned_by", None) or "gateway"
            self._owner_cache[m.id] = owner
            # Absent on an older gateway: no edits.
            if getattr(m, "image_edit", None) is True:
                mask = getattr(m, "image_mask", None)
                edit_masks[m.id] = mask if isinstance(mask, bool) else None
            # Absent on an older gateway: no default. Only for a type the entry declares, never its id's guess.
            default_for = getattr(m, "default_for", None)
            if isinstance(default_for, list):
                declared = declared_model_types(getattr(m, "types", None), getattr(m, "type", None))
                for t in _DEFAULT_TYPES:
                    if t in default_for and t in declared:
                        defaults.setdefault(t, m.id)
            out.append(GatewayModel(m.id, owner, _types_of(m)))
        self._listed_defaults = defaults
        self._image_edit_masks = edit_masks
        self._listed_defaults_until = time.monotonic() + _DEFAULTS_TTL_SECONDS
        return out

    async def marked_default_model(self, kind: str) -> str | None:
        """The model the gateway marks default for *kind* (``image``, ``speech``, ``chat``, ``embedding``).

        Read from a listing at most :data:`_DEFAULTS_TTL_SECONDS` old. While one is read, or after one
        failed, the last defaults read answer: one listing at a time, retried after the miss TTL.
        """
        if self._listed_defaults_until <= time.monotonic():
            self._listed_defaults_until = time.monotonic() + _OWNER_MISS_TTL_SECONDS
            try:
                await self._models_with_meta(self.client.with_options(max_retries=0, timeout=_OWNER_LISTING_TIMEOUT))
            except RuntimeError as exc:
                logger.warning("Gateway default models not read: %s", format_exception_message(exc))
        return self._listed_defaults.get(kind)

    async def list_models(self) -> list[str]:
        """Chat models only (the reasoning picker + chat routing)."""
        return sorted(m.id for m in await self._models_with_meta() if "chat" in m.types)

    async def list_models_and_offered(self) -> tuple[list[str], list[str]]:
        """Chat models, and every id the gateway offers, from one ``/v1/models`` call."""
        meta = await self._models_with_meta()
        chat = sorted(m.id for m in meta if "chat" in m.types)
        return chat, sorted(m.id for m in meta)

    async def list_models_grouped(self) -> dict[str, list[str]] | None:
        """Chat models grouped by their upstream provider, for the picker."""
        groups: dict[str, list[str]] = {}
        for m in await self._models_with_meta():
            if "chat" in m.types:
                groups.setdefault(m.owner, []).append(m.id)
        for ids in groups.values():
            ids.sort()
        return groups

    async def list_embedding_models(self) -> list[str]:
        return sorted(m.id for m in await self._models_with_meta() if "embedding" in m.types)

    async def list_image_models(self) -> list[str]:
        return sorted(m.id for m in await self._models_with_meta() if "image" in m.types)

    async def list_video_models(self) -> list[str]:
        """The models typed ``video``, plus those the platform classes as video by id."""
        from app.llm.router import is_video_model

        return sorted(m.id for m in await self._models_with_meta() if "video" in m.types or is_video_model(m.id))

    async def _video_headers(self, model: str) -> dict[str, str]:
        return await self._target_headers(model)

    async def list_decision_models(self) -> list[str]:
        """The models whose types include ``decision``: its operator declares them, never their id."""
        return sorted(m.id for m in await self._models_with_meta() if "decision" in m.types)

    async def _decision_headers(self, model: str) -> dict[str, str]:
        # A decision id rarely names its upstream, so the listing's owner is
        # often "gateway" and nothing is sent; a credential that pins a target
        # sets it on every request regardless (``_GatewayAuth``). A listing that
        # fails leaves the decision to the gateway rather than failing it.
        try:
            return await self._target_headers(model)
        except Exception as exc:
            logger.debug("Gateway owner of %s not read: %s", model, format_exception_message(exc))
            return {}

    async def list_speech_models(self) -> list[str]:
        """The models typed ``speech``, plus those the platform classes as speech by id."""
        from app.llm.router import is_speech_model

        return sorted(m.id for m in await self._models_with_meta() if "speech" in m.types or is_speech_model(m.id))

    async def _speech_headers(self, model: str) -> dict[str, str]:
        # As its voices read: an ``eleven_`` id the listing omits has no owner to pin.
        if self._elevenlabs_speaks(model):
            return {"X-Target-Provider": "elevenlabs"}
        return await self._target_headers(model)

    def _elevenlabs_speaks(self, model: str | None) -> bool:
        """Whether ElevenLabs speaks *model*: its ``eleven_`` id, the pinned target, or the owner listed
        (read by :meth:`load_speech_capabilities`)."""
        if not model or _speaks_openai_voices(model):
            return False
        if self._target:
            # Every request goes there, whatever the model is called.
            return self._target == "elevenlabs"
        upstream = (self._owner_cache.get(model) or "").lower()
        return model.lower().rsplit("/", 1)[-1].startswith("eleven_") or upstream == "elevenlabs"

    async def load_speech_capabilities(self, model: str | None) -> None:
        """Read *model*'s upstream before the sync checks: an alias is known by its listed owner only."""
        if model and not _speaks_openai_voices(model):
            await self._upstream_of(model)

    async def list_voices(self, model: str | None = None) -> list[Voice]:
        """A Gemini model speaks Gemini's prebuilt voices, passed through by name; an ElevenLabs one the account's.

        Gemini's library and project voices need a gateway route that does not exist. ElevenLabs' come from
        ``GET /v1/audio/voices``, own first, reused per model for :data:`VOICES_TTL_SECONDS` as direct.
        """
        upstream = await self._upstream_of(model) if model and not _speaks_openai_voices(model) else None
        if upstream == "gemini":
            from app.llm.gemini_provider import GeminiProvider

            return GeminiProvider.prebuilt_voices()
        if not model or not self._elevenlabs_speaks(model):
            return await super().list_voices(model)
        now = time.monotonic()
        read_at, voices = self._account_voices.get(model, (float("-inf"), []))
        if now - read_at < VOICES_TTL_SECONDS:
            return list(voices)
        until, failure = self._account_voice_misses.get(model, (0.0, ""))
        if until > now:
            raise RuntimeError(failure)
        try:
            # Sent once and briefly, as the owner listing: every scene of a video may ask.
            response = await self.client.with_options(max_retries=0, timeout=_VOICES_TIMEOUT).get(
                "/audio/voices",
                cast_to=httpx.Response,
                options={"params": {"model": model}, "headers": {"X-Target-Provider": "elevenlabs"}},
            )
            data = response.json().get("data")
        except Exception as e:
            failure = f"ElevenLabs voices not read from the gateway: {redact_token(format_exception_message(e), self._token)}"
            self._account_voice_misses[model] = (time.monotonic() + _OWNER_MISS_TTL_SECONDS, failure)
            raise RuntimeError(failure) from None
        voices = own_voices_first([v for item in data or [] if (v := account_voice(item))])
        self._account_voices[model] = (time.monotonic(), voices)
        return list(voices)

    async def synthesize_speech(self, request: SpeechRequest) -> SpeechResult:
        await self.load_speech_capabilities(request.model)
        if not self._elevenlabs_speaks(request.model):
            return await super().synthesize_speech(request)
        # The gateway refuses ElevenLabs speech without a voice id: a name, or a voice the account
        # lacks, is resolved as direct. A voice asked is sent as is when the voices cannot be read.
        try:
            voices = await self.list_voices(request.model)
        except RuntimeError:
            if not request.voice:
                raise
            voices = []
        notes: list[str] = []
        voice = pick_voice(voices, request.voice, None, notes)
        result = await super().synthesize_speech(replace(request, voice=voice))
        result.notes[:0] = notes
        return result

    def speech_takes_style(self, model: str) -> bool | None:
        """ElevenLabs takes no written delivery instruction: the gateway drops it."""
        return False if self._elevenlabs_speaks(model) else super().speech_takes_style(model)

    def speech_pronunciation(self, model: str) -> PronunciationSupport:
        """Inline IPA phonemes reach the ElevenLabs models that honour them; the gateway keeps no dictionary."""
        if self._elevenlabs_speaks(model) and model.lower().rsplit("/", 1)[-1] in PHONEME_MODELS:
            return PronunciationSupport(ssml="inline", phoneme=True)
        return super().speech_pronunciation(model)

    async def list_music_models(self) -> list[str]:
        """The models whose types include ``music``: its operator declares them, never their id."""
        return sorted(m.id for m in await self._models_with_meta() if "music" in m.types)

    async def generate_music(self, request: MusicRequest) -> MusicResult:
        """Compose through ``POST /v1/audio/music``.

        Sent once: the SDK's own retries are off, and a call that timed out after
        it was sent is never asked again, since a second request is a second
        billed composition. Only what the gateway says is retryable is retried
        (:func:`_music_failure`), by the job's back-off.
        """
        if request.continue_from is not None:
            raise MusicGenerationError(f"{request.model} on the gateway cannot continue a clip.")
        body: dict[str, Any] = {
            "model": request.model,
            "prompt": request.prompt.strip(),
            "duration_seconds": clamp_seconds(request.duration_seconds),
            "instrumental": bool(request.instrumental),
        }
        if request.negative_prompt:
            body["negative_prompt"] = request.negative_prompt.strip()
        # The gateway echoes it and keys its call log by it: a failed composition can be traced.
        options: dict[str, Any] = {
            "timeout": _MUSIC_TIMEOUT,
            "max_retries": 0,
            "headers": {"X-Request-Id": str(uuid.uuid4())},
        }
        try:
            headers = await self._target_headers(request.model)
        except Exception as exc:
            # Nothing was sent: asking again costs nothing.
            raise MusicProviderBusyError(
                f"The gateway model list could not be read: {format_exception_message(exc)}"
            ) from None
        options["headers"].update(headers)
        try:
            response = await self.client.post(
                "/audio/music", cast_to=httpx.Response, body=body, options=options
            )
        except openai.APIStatusError as exc:
            raise _music_failure(exc.status_code, exc.response) from None
        except (openai.APITimeoutError, openai.APIConnectionError) as exc:
            # Only a request that never left (no connection, no pool slot) is safe to send again.
            if isinstance(exc.__cause__, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)):
                raise status_error(None, type(exc.__cause__).__name__, vendor="The gateway") from None
            raise MusicGenerationError(
                "The gateway did not answer the music request in time; it may still be billed, so it is not sent again."
            ) from None
        if not response.content:
            raise MusicGenerationError("The gateway returned no music.")
        mime = (response.headers.get("content-type") or "").split(";", 1)[0].strip()
        return MusicResult(response.content, mime if mime.startswith("audio/") else "audio/mpeg")

    async def _owner_of(self, model: str, client: openai.AsyncOpenAI | None = None) -> str | None:
        """The upstream provider a model belongs to (from the gateway's
        ``owned_by``), so non-chat calls can pin ``X-Target-Provider`` — embedding
        / image ids don't reliably name their provider."""
        if model not in self._owner_cache and self._owner_misses.get(model, 0.0) <= time.monotonic():
            for m in await self._models_with_meta(client):
                self._owner_cache[m.id] = m.owner
            if model not in self._owner_cache:
                # Not listed: asked again once the operator may have added it.
                self._owner_misses[model] = time.monotonic() + _OWNER_MISS_TTL_SECONDS
        return self._owner_cache.get(model)

    async def max_tools_for(self, model: str | None) -> int:
        """The cap of the upstream serving *model*: the gateway sets none of its own.

        A credential that pins a target sends every request there, whatever the listing says.
        """
        owner = self._target
        m = model or self.default_model
        if owner is None:
            try:
                owner = await self._owner_of(m, self.client.with_options(max_retries=0, timeout=_OWNER_LISTING_TIMEOUT))
            except Exception as exc:
                # Not retried before the TTL, as a model the listing leaves out.
                self._owner_misses[m] = time.monotonic() + _OWNER_MISS_TTL_SECONDS
                logger.warning(
                    "Gateway owner of %s not read, tool cap left at %d: %s",
                    m, self.max_tools, format_exception_message(exc),
                )
                return self.max_tools
        # The platform's own provider for that upstream; one it has none for gets the default.
        # Lowercased: ``PROVIDER_MAP`` keys are, a listed ``owned_by`` may not be.
        from app.llm.router import PROVIDER_MAP

        return getattr(PROVIDER_MAP.get((owner or "").lower()), "max_tools", self.max_tools)

    async def _target_headers(self, model: str) -> dict[str, str]:
        owner = await self._owner_of(model)
        return {"X-Target-Provider": owner} if owner and owner != "gateway" else {}

    async def embed(self, text: str, model: str | None = None) -> list[float]:
        """Embed via the gateway's /v1/embeddings (routed to the model's upstream
        provider, authenticated with the per-request XTM JWT)."""
        m = model or self.default_model
        headers = await self._target_headers(m)
        # Only the text-embedding-3 family takes ``dimensions`` (as in ``OpenAIProvider``);
        # another model answers its native size, which the processor checks.
        dims = self.embedding_dimensions if m.startswith("text-embedding-3") else None
        shorten = {"dimensions": dims} if dims and (m, dims) not in self._refused_dimensions else {}
        try:
            response = await self.client.embeddings.create(input=text, model=m, extra_headers=headers, **shorten)
        except openai.APIStatusError as exc:
            # A size above the model's native one is refused: ask again at the native size.
            if not shorten or not _may_refuse_dimensions(exc):
                raise
            response = await self.client.embeddings.create(input=text, model=m, extra_headers=headers)
            # Only a native vector smaller than ``dims`` proves the size was refused
            # (text-embedding-3 refuses only above native): the message may not say why.
            if len(response.data[0].embedding) < dims:
                self._refused_dimensions.add((m, dims))
        return list(response.data[0].embedding)

    async def generate_image(
        self, prompt: str, model: str | None = None, size: str = "1024x1024"
    ) -> bytes:
        """Generate an image via the gateway's /v1/images/generations."""
        import base64

        m = model or self.default_model
        response = await self.client.images.generate(
            model=m, prompt=prompt, n=1, size=size, extra_headers=await self._target_headers(m)
        )
        return base64.b64decode(response.data[0].b64_json)

    async def load_image_capabilities(self, model: str | None) -> None:
        # The edit flag comes with the listing; a failed one offers no pictures until listed.
        try:
            await self._owner_of(
                model or self.default_model, self.client.with_options(max_retries=0, timeout=_OWNER_LISTING_TIMEOUT)
            )
        except RuntimeError as exc:
            self._owner_misses[model or self.default_model] = time.monotonic() + _OWNER_MISS_TTL_SECONDS
            logger.warning("Gateway image capabilities not read: %s", format_exception_message(exc))

    def image_reference_limit(self, model: str | None) -> int:
        """As OpenAI direct, for a model the gateway declares it edits with (:meth:`load_image_capabilities`)."""
        from app.llm.openai_provider import OpenAIProvider

        return OpenAIProvider._GPT_IMAGE_REFERENCES if (model or self.default_model) in self._image_edit_masks else 0

    def image_takes_mask(self, model: str | None) -> bool:
        m = model or self.default_model
        if (listed := self._image_edit_masks.get(m, False)) is not None:
            return listed
        # A gateway before 1.261120.0 lists no ``image_mask`` (drop this once none is supported):
        # Gemini takes none, so an alias is Gemini by its pinned target or listed owner, as in _upstream_of.
        upstream = (self._target or self._owner_cache.get(m) or "").lower()
        return not is_gemini_model(m) and upstream != "gemini"

    async def edit_image(
        self,
        prompt: str,
        images: list[tuple[bytes, str]],
        *,
        mask: tuple[bytes, str] | None = None,
        model: str | None = None,
        size: str = "1024x1024",
    ) -> bytes:
        """An image from the gateway's /v1/images/edits, which forwards OpenAI's multipart request unchanged."""
        import base64

        from app.llm.openai_provider import OpenAIProvider

        m = model or self.default_model
        response = await self.client.images.edit(
            **OpenAIProvider.image_edit_params(prompt, images, mask, m, size), extra_headers=await self._target_headers(m)
        )
        return base64.b64decode(response.data[0].b64_json)


def _may_refuse_dimensions(exc: openai.APIStatusError) -> bool:
    """The gateway's 502 for an upstream 400 that refuses ``dimensions``. Without the
    provider's message (an older gateway), any upstream 400 may be that refusal."""
    upstream = gateway_upstream_refusal(exc)
    if upstream is None or upstream[0] != 400:
        return False
    # OpenAI: "Invalid value for 'dimensions'"; not one provider's exact wording.
    return upstream[1] is None or "dimensions" in upstream[1].lower()


def mark_claude_cache_breakpoints(params: dict, cache_control: dict | None) -> None:
    """The Anthropic provider's prompt-cache breakpoints, in OpenAI format: last tool, stable system prefix, conversation.

    The conversation tail is the top-level ``cache_control`` (Anthropic's automatic
    caching), not a marked part: the tail is usually a tool result, which the gateway
    reads only as a string.
    """
    if cache_control is None:
        return
    if tools := params.get("tools"):
        tools[-1] = {**tools[-1], "cache_control": cache_control}
    messages = params["messages"]
    # The last of the leading system messages: a later one is a per-turn nudge.
    lead = 0
    while lead < len(messages) and messages[lead].get("role") == "system":
        lead += 1
    if lead:
        system = messages[lead - 1]
        content = system.get("content")
        if isinstance(content, str) and content:
            # As the Anthropic provider: the breakpoint ends the stable prefix, the clock line on rides unmarked.
            stable, volatile = split_system_prompt(content)
            if volatile.strip():
                parts = [{"type": "text", "text": stable, "cache_control": cache_control}, {"type": "text", "text": volatile}]
            else:
                parts = [{"type": "text", "text": content, "cache_control": cache_control}]
        elif isinstance(content, list) and content and content[-1].get("type") == "text" and content[-1].get("text"):
            parts = [*content[:-1], {**content[-1], "cache_control": cache_control}]
        else:
            parts = None
        if parts:
            params["messages"] = [*messages[: lead - 1], {**system, "content": parts}, *messages[lead:]]
    params["extra_body"] = {**(params.get("extra_body") or {}), "cache_control": cache_control}


def _music_failure(code: int, response: httpx.Response) -> RuntimeError:
    """The error for a refused composition, reading the gateway's ``error`` envelope.

    Each request is billed, so only what is known to be safe to send again is
    busy (the job's back-off asks again): a 429, a 503 ``gateway_overloaded``
    (refused before any upstream call) and a ``502 provider_error`` the gateway
    marks ``retryable: true``. Any other 5xx, a proxy's 504 or a page that is not
    the envelope included, may have composed and billed: permanent.

    A busy refusal carries the envelope's ``retry_after_seconds`` (else the
    ``Retry-After`` header): the job waits that long. A ``429
    quota_exceeded`` is a ceiling that holds for hours, never cleared by a
    back-off: permanent, saying when it resets when the gateway said.
    """
    try:
        error = response.json().get("error")
    except (ValueError, AttributeError):
        error = None
    error = error if isinstance(error, dict) else {}
    reason = str(error.get("message") or "")
    kind = error.get("code") or error.get("type")
    wait = _retry_after_seconds(error, response)
    detail = f": {reason.strip()[:300]}" if reason.strip() else ""
    if code == 429 and kind == "quota_exceeded":
        resets = f" It resets in about {_wait_words(wait)}." if wait else ""
        return MusicGenerationError(
            f"The gateway's music quota is exhausted{detail.rstrip('.')}.{resets} Compose again once it resets."
        )
    if (
        code == 429
        or (code == 503 and kind == "gateway_overloaded")
        or (code == 502 and kind == "provider_error" and error.get("retryable") is True)
    ):
        return status_error(code, reason, vendor="The music provider" if code == 502 else "The gateway", retry_after=wait)
    if code == 502 and kind == "provider_error" and error.get("retryable") is False:
        return MusicGenerationError(f"The music provider refused the request{detail}")
    if code >= 500:
        return MusicGenerationError(
            f"The gateway failed while composing (HTTP {code}){detail}; it may have been billed, so it is not sent again."
        )
    return status_error(code, reason, vendor="The gateway")


def _retry_after_seconds(error: dict, response: httpx.Response) -> float | None:
    """The wait a refusal names: the envelope's ``retry_after_seconds``, else the ``Retry-After`` header (seconds)."""
    for raw in (error.get("retry_after_seconds"), response.headers.get("retry-after")):
        try:
            seconds = float(raw)
        except (TypeError, ValueError):
            continue
        if seconds > 0:
            return seconds
    return None


def _wait_words(seconds: float) -> str:
    """``45 s``, ``12 min``, ``6 h 5 min``."""
    if seconds < 60:
        return f"{round(seconds)} s"
    minutes = round(seconds / 60)
    hours, rest = divmod(minutes, 60)
    if not hours:
        return f"{minutes} min"
    return f"{hours} h {rest} min" if rest else f"{hours} h"


def _types_of(entry: Any) -> tuple[str, ...]:
    """What a ``/v1/models`` entry serves: what it declares, else the id's classification (older gateways)."""
    declared = declared_model_types(getattr(entry, "types", None), getattr(entry, "type", None))
    return declared or (_classify_model(entry.id),)


def _classify_model(model_id: str) -> str:
    """Client-side capability fallback, mirroring the gateway's classifier."""
    m = model_id.lower()
    if "embed" in m or m.startswith("voyage") or m.startswith("nomic"):
        return "embedding"
    if any(k in m for k in ("dall-e", "gpt-image", "imagen")) or m.startswith(("stable-diffusion", "flux")):
        return "image"
    from app.llm.router import is_music_model, is_speech_model, is_video_model

    if is_video_model(model_id):
        return "video"
    if is_speech_model(model_id):
        return "speech"
    if is_music_model(model_id):
        return "music"
    return "chat"
