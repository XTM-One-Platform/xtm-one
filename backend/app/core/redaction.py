# SPDX-License-Identifier: Apache-2.0

"""Shared credential redaction.

Third-party text — an exception message, a tool's own error string, a page's
body — reaches us verbatim and routinely echoes the request that produced it:
``401 Unauthorized: {"Authorization": "Bearer sk-live-..."}`` is the canonical
shape. Anywhere that text is persisted or rendered, it needs a pass first.

These patterns started life private to ``conversation_memory``, which is where
the problem was first met. They are here because it is not the only place: the
investigation queriers persist an MCP server's own error string onto a step
that the block renders, which is the same exposure by a different route. One
copy, so a pattern added for one caller protects the others.

Length-truncating the text is not a substitute and never was: a bearer token
is comfortably inside 300 characters.

The second half is exact rather than heuristic: the values of the secret
variables a run or a chat turn resolved. They are substituted into
what the model reads, so they come back in what is stored (run messages, chat
messages, tool calls), traced and logged, and in model output that echoes
them. :func:`known_secrets_scope` opens a per-run / per-turn registry in a
``ContextVar`` (tasks spawned later share it), ``load_variables`` fills it,
and every sink passes what it writes through :func:`redact_known_secrets`: the
engine's write hooks (``core/database.py``), the trace sanitizers
(``services/langfuse_tracing.py``), the log pipeline
(:func:`redact_log_event`, ``core/logging.py``) and the user surfaces
(:class:`KnownSecretStream`, :func:`redact_known_secrets_for_display`). A
stored value becomes a marker (:func:`secret_marker`) that
:func:`restore_known_secrets` turns back into the live value wherever the
platform rebuilds the model's context from what it stored.
"""

from __future__ import annotations

import contextlib
import contextvars
import functools
import hashlib
import hmac
import inspect
import json
import re
import uuid
from collections.abc import Callable, Iterator
from typing import Any
from urllib.parse import quote, quote_plus

REDACT_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # JWT (xxx.yyy.zzz with ≥8 base64url chars per segment).
    (
        re.compile(r"\b[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
        "<redacted-jwt>",
    ),
    # Bearer / Basic Authorization-header content.
    (
        re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._\-+=/]{8,}\b"),
        "<redacted-auth-header>",
    ),
    # Platform API keys: fcp- (user), mcp- (MCP endpoint), sk-/sk_live/sk_test
    # (OpenAI-style), ghp_/gho_ (GitHub PAT/OAuth), xoxb-/xoxp- (Slack bot/user
    # tokens). The trailing length floor keeps short identifier-shaped strings
    # (e.g. ``api-v2``) from being flagged.
    (
        re.compile(
            r"\b(?:fcp|mcp|sk|sk_live|sk_test|ghp|gho|ghu|ghs|ghr|xoxb|xoxp)"
            r"[-_][A-Za-z0-9_\-]{16,}\b"
        ),
        "<redacted-apikey>",
    ),
    # Catch-all for standalone long base64-ish / hex blobs (hashes,
    # session ids, OAuth refresh tokens, ...). 40 chars is comfortably
    # above any English word in a real directive AND we additionally
    # require the run to contain at least one of each: uppercase,
    # lowercase, and digit. That mixed-class requirement is what
    # distinguishes a real high-entropy token from a long fixture
    # string ("xxxx...x") or a long sentence-cased phrase that happens
    # to lack whitespace (rare but possible in URLs).
    (
        re.compile(
            r"\b"
            r"(?=[A-Za-z0-9+/=_\-]*[A-Z])"  # ≥1 uppercase
            r"(?=[A-Za-z0-9+/=_\-]*[a-z])"  # ≥1 lowercase
            r"(?=[A-Za-z0-9+/=_\-]*[0-9])"  # ≥1 digit
            r"[A-Za-z0-9+/=_\-]{40,}"
            r"\b",
        ),
        "<redacted-token>",
    ),
]


def redact_secrets(text: str) -> str:
    """Best-effort redaction of credential-shaped tokens from *text*.

    Best-effort is the honest word: these are shape heuristics, not a
    guarantee, and a secret that looks like an English word survives them.
    They are worth having anyway wherever third-party text is about to be
    persisted or shown — the common leak is an error message echoing the
    request that produced it, and that shape is exactly what these catch.
    The secret variable values the current run or turn resolved go first,
    exactly (:func:`redact_known_secrets`).
    """
    text = redact_known_secrets(text)
    for pattern, replacement in REDACT_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


# ---------------------------------------------------------------------------
# Known secret values: the secret variables a run or a turn resolved
# ---------------------------------------------------------------------------

#: A value shorter than this is never redacted: replacing every "1234" or
#: "admin" in the text a run stores would mangle unrelated words and numbers.
#: Such a value stays in what is stored like any other text.
MIN_KNOWN_SECRET_LENGTH = 8

#: What a surface shown to someone who cannot read the variable carries where
#: model output echoed its value (a channel message, the public widget). Not a
#: marker: nothing restores it.
SECRET_DISPLAY_TEXT = "[secret]"  # noqa: S105 - the placeholder shown instead of one, not a secret

_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_TAG_CHARS = 16
#: ``[[SECRET_VARIABLE:<variable id>:<tag>]]``, the stored form of a secret value.
SECRET_MARKER_RE = re.compile(rf"\[\[SECRET_VARIABLE:({_UUID}):([0-9a-f]{{{_TAG_CHARS}}})\]\]")


def _marker_key() -> bytes:
    from app.core.config import settings

    return hashlib.sha256(b"secret-variable-marker\x00" + settings.secret_key.encode()).digest()


def _marker_tag(binding: str, variable_id: str) -> str:
    message = f"{binding}\x1f{variable_id}".encode()
    return hmac.new(_marker_key(), message, hashlib.sha256).hexdigest()[:_TAG_CHARS]


def _fingerprint(variable_id: str, value: str) -> str:
    key = hashlib.sha256(b"secret-variable-fingerprint\x00" + _marker_key()).digest()
    digest = hmac.new(key, f"{variable_id}\x1f{value}".encode(), hashlib.sha256).hexdigest()
    return f"{variable_id}:{digest[:32]}"


def normalize_binding(binding: object) -> str:
    """The id a scope is bound to, one spelling per id: a UUID in its canonical form (a path's ``ABC-...`` included)."""
    text = str(binding or "").strip()
    try:
        return str(uuid.UUID(text))
    except ValueError:
        return text


def _surface_forms(value: str) -> set[str]:
    """*value* as it can appear in text: as is, escaped inside a JSON string (ASCII-escaped or not), and
    percent-encoded in a URL (an HTTP error echoes the request's, query included)."""
    forms = {
        value,
        json.dumps(value)[1:-1],
        json.dumps(value, ensure_ascii=False)[1:-1],
        quote(value),
        quote(value, safe=""),
        quote_plus(value),
    }
    return {form for form in forms if len(form) >= MIN_KNOWN_SECRET_LENGTH}


class KnownSecrets:
    """The secret values one run or turn resolved, and how a stored copy names each.

    A stored value becomes ``[[SECRET_VARIABLE:<variable id>:<tag>]]``. The
    tag is keyed by ``SECRET_KEY`` and bound to *binding* (the run, the
    conversation): a marker typed into a message, a webhook payload or a
    fetched page, or copied from another run, is restored by nothing.
    """

    def __init__(self, binding: object = "", inherited: KnownSecrets | None = None) -> None:
        # A nested scope with no id of its own (a consulted agent, a sub-agent of
        # the turn) works for the same conversation or run: it reads its markers.
        self.binding = normalize_binding(binding) or (inherited.binding if inherited is not None else "")
        self._by_id: dict[str, str] = {}
        self._by_form: dict[str, str] = {}
        self._pattern: re.Pattern[str] | None = None
        self._longest = 0
        self._parent = inherited
        if inherited is not None:
            # A nested scope (a consulted agent, a sub-agent) stores none of the
            # outer secrets either, and restores only the markers of its own binding.
            for variable_id, value in inherited._by_id.items():
                self._add(variable_id, value)

    def remember(self, variable_id: str, value: str | None) -> None:
        if not value or len(value) < MIN_KNOWN_SECRET_LENGTH:
            return
        self._add(str(variable_id).lower(), value)
        # The outer turn stores what the nested one answered, after it closed.
        if self._parent is not None:
            self._parent.remember(variable_id, value)

    def _add(self, variable_id: str, value: str) -> None:
        self._by_id[variable_id] = value
        for form in _surface_forms(value):
            self._by_form.setdefault(form, variable_id)
        self._pattern = None

    def __bool__(self) -> bool:
        return bool(self._by_form)

    def marker(self, variable_id: str) -> str:
        return f"[[SECRET_VARIABLE:{variable_id}:{_marker_tag(self.binding, variable_id)}]]"

    def _compiled(self) -> re.Pattern[str]:
        if self._pattern is None:
            # Longest first: where two values overlap, the longer is replaced whole.
            # A marker already in the text is matched first and kept as it is.
            forms = sorted(self._by_form, key=len, reverse=True)
            self._longest = len(forms[0]) if forms else 0
            alternatives = "|".join(re.escape(form) for form in forms)
            self._pattern = re.compile(rf"(?P<marker>{SECRET_MARKER_RE.pattern})|(?P<secret>{alternatives})")
        return self._pattern

    def partial_suffix(self, text: str) -> int:
        """The length of the longest end of *text* that more text could complete into a known value."""
        self._compiled()
        for size in range(min(len(text), self._longest - 1), 0, -1):
            tail = text[-size:]
            if any(form.startswith(tail) for form in self._by_form):
                return size
        return 0

    def redact_text(self, text: str, replacement: str | None = None) -> str:
        if not self._by_form:
            return text

        def _replace(match: re.Match[str]) -> str:
            if match.group("marker") is not None:
                return match.group(0)
            if replacement is not None:
                return replacement
            return self.marker(self._by_form[match.group("secret")])

        return self._compiled().sub(_replace, text)

    def _restorable(self, match: re.Match[str]) -> str | None:
        variable_id, tag = match.group(1), match.group(2)
        value = self._by_id.get(variable_id)
        if value is None or not hmac.compare_digest(tag, _marker_tag(self.binding, variable_id)):
            return None
        return value

    def restore_text(self, text: str) -> str:
        return SECRET_MARKER_RE.sub(lambda match: self._restorable(match) or match.group(0), text)

    def restored_fingerprints(self, text: str) -> set[str]:
        """A keyed digest of each value :meth:`restore_text` puts into *text*: it names the value, never holds it."""
        return {
            _fingerprint(match.group(1), value)
            for match in SECRET_MARKER_RE.finditer(text)
            if (value := self._restorable(match)) is not None
        }

    def knows_fingerprint(self, fingerprint: str) -> bool:
        """Whether this scope holds the very value *fingerprint* names, so it can still redact it."""
        variable_id = fingerprint.partition(":")[0]
        value = self._by_id.get(variable_id)
        return value is not None and hmac.compare_digest(
            _fingerprint(variable_id, value).encode(), fingerprint.encode("utf-8", "replace")
        )


_known_secrets: contextvars.ContextVar[KnownSecrets | None] = contextvars.ContextVar("known_secrets", default=None)


def current_known_secrets() -> KnownSecrets | None:
    """The registry of the run or turn in progress, ``None`` outside one."""
    return _known_secrets.get()


@contextlib.contextmanager
def known_secrets_scope(binding: object = "", *, inherit: bool = True) -> Iterator[KnownSecrets]:
    """Open the registry of a run or a turn: *binding* is its id (run, conversation).

    Opened at the top of each entry point, before anything is spawned: a task
    created inside copies the context, so it shares this registry and sees the
    values ``load_variables`` remembers later. A scope opened inside another
    one (a sub-agent, a handover run inline) keeps redacting the outer values.
    ``inherit=False`` opens one that shares nothing with the scope around it:
    another identity's values, loaded for one runner command, stay in it.
    """
    scope = KnownSecrets(binding, inherited=_known_secrets.get() if inherit else None)
    token = _known_secrets.set(scope)
    try:
        yield scope
    finally:
        _known_secrets.reset(token)


def known_secrets_scoped(binding_arg: str | None = None) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorate an entry point (a coroutine or an async generator) so it runs in a scope of its own.

    *binding_arg* names the parameter holding the run or conversation id
    (``run_id``), or an attribute of one (``conversation.id``); without one the
    scope is bound later (:func:`bind_known_secrets`).
    """
    name, *attributes = (binding_arg or "").split(".")

    def decorate(fn: Callable[..., Any]) -> Callable[..., Any]:
        signature = inspect.signature(fn)

        def _binding(args: tuple, kwargs: dict) -> object:
            if not name:
                return ""
            value = signature.bind_partial(*args, **kwargs).arguments.get(name)
            for attribute in attributes:
                value = getattr(value, attribute, None)
            return value or ""

        if inspect.isasyncgenfunction(fn):

            @functools.wraps(fn)
            async def scoped_stream(*args: Any, **kwargs: Any) -> Any:
                scope = KnownSecrets(_binding(args, kwargs), inherited=_known_secrets.get())
                # Each step runs in the consumer's context: set the scope around each one.
                stream = fn(*args, **kwargs)
                try:
                    while True:
                        token = _known_secrets.set(scope)
                        try:
                            item = await anext(stream)
                        except StopAsyncIteration:
                            return
                        finally:
                            _known_secrets.reset(token)
                        yield item
                finally:
                    token = _known_secrets.set(scope)
                    try:
                        await stream.aclose()
                    finally:
                        _known_secrets.reset(token)

            return scoped_stream

        @functools.wraps(fn)
        async def scoped(*args: Any, **kwargs: Any) -> Any:
            with known_secrets_scope(_binding(args, kwargs)):
                return await fn(*args, **kwargs)

        return scoped

    return decorate


def renew_known_secrets(binding: object = "") -> KnownSecrets:
    """Start the next turn of a long-lived connection (a chat socket) on a registry of its own.

    Called inside the scope the connection opened, which still resets the
    context when it closes. Nothing an earlier turn resolved carries over: a
    later turn restores the marker of a variable only once it resolved it
    again, so a revoked share stops restoring at the next turn.
    """
    current = _known_secrets.get()
    scope = KnownSecrets(binding, inherited=current._parent if current is not None else None)
    _known_secrets.set(scope)
    return scope


def bind_known_secrets(binding: object) -> None:
    """Give the open scope its id once it is known (a conversation created during the turn), before anything is stored."""
    scope = _known_secrets.get()
    if scope is not None and not scope.binding:
        scope.binding = normalize_binding(binding)


def remember_secret_value(variable_id: object, value: str | None) -> None:
    """Record a resolved secret variable value in the open scope.

    Outside one, a scope is opened in the current context, so the value is
    still redacted from what this task and the tasks it spawns afterwards
    store; the entry points open theirs explicitly so that every task of the
    run shares it.
    """
    scope = _known_secrets.get()
    if scope is None:
        scope = KnownSecrets()
        _known_secrets.set(scope)
    scope.remember(str(variable_id), value)


def _walk(data: Any, transform: Any) -> Any:
    """*data* with *transform* applied to every string, containers rebuilt only where something changed."""
    if isinstance(data, str):
        return transform(data)
    if isinstance(data, list | tuple):
        changed = False
        items = []
        for item in data:
            new = _walk(item, transform)
            changed = changed or new is not item
            items.append(new)
        if not changed:
            return data
        return items if isinstance(data, list) else tuple(items)
    if isinstance(data, dict):
        out: dict | None = None
        for key, item in data.items():
            new = _walk(item, transform)
            if new is not item and out is None:
                out = dict(data)
            if out is not None:
                out[key] = new
        return data if out is None else out
    return data


def redact_known_secrets(data: Any) -> Any:
    """*data* (a string, or JSON-shaped) with every known secret value replaced by its marker.

    The one rule every sink applies: the engine's write hooks, the trace
    sanitizers, the log filter. Outside a scope, or when the scope knows no
    value, *data* is returned as is (the same object).
    """
    scope = _known_secrets.get()
    if not scope:
        return data
    return _walk(data, lambda text: scope.redact_text(text) if text else text)


def redact_known_secrets_for_display(data: Any) -> Any:
    """:func:`redact_known_secrets` for a surface that cannot restore: :data:`SECRET_DISPLAY_TEXT` instead of a marker.

    Model output sent to a channel or an embedded chat: the reader is not the
    run's identity, and a marker pasted back into the conversation must not be
    restorable by them either. A stored marker becomes the display text with
    no scope open too (history served to such a surface).
    """
    scope = _known_secrets.get()

    def _display(text: str) -> str:
        if not text:
            return text
        if scope:
            text = scope.redact_text(text, SECRET_DISPLAY_TEXT)
        return SECRET_MARKER_RE.sub(SECRET_DISPLAY_TEXT, text) if "[[SECRET_VARIABLE:" in text else text

    return _walk(data, _display)


#: The top-level keys of a streamed frame or event that route it rather than say
#: something (a frame's ``type``, an event's ``status``, the ids it names): never
#: rewritten, even by a secret variable equal to one of their values (``cancelled``).
PROTOCOL_KEYS = frozenset(
    {
        "type", "status", "event", "kind", "role", "tool", "tool_name", "tool_call_id", "call_id", "id",
        "message_id", "turn_id", "run_id", "conversation_id", "agent_id", "relayed", "intermediate", "origin",
    }
)


def redact_frame(frame: Any, *, display: bool = False) -> Any:
    """:func:`redact_known_secrets` (or its display form) for a frame or an event: every value but its :data:`PROTOCOL_KEYS`."""
    redact = redact_known_secrets_for_display if display else redact_known_secrets
    if not isinstance(frame, dict):
        return redact(frame)
    out: dict | None = None
    for key, value in frame.items():
        new = value if key in PROTOCOL_KEYS else redact(value)
        if new is not value and out is None:
            out = dict(frame)
        if out is not None:
            out[key] = new
    return frame if out is None else out


def defuse_secret_markers(data: Any) -> Any:
    """*data* written by a person (a chat message, a channel post) with every marker as the display text.

    What a person writes is never restored, a marker copied from this very
    conversation included: restoring it would hand them the value the platform
    holds back. The model reads :data:`SECRET_DISPLAY_TEXT` in its place.
    """
    return _walk(data, lambda text: SECRET_MARKER_RE.sub(SECRET_DISPLAY_TEXT, text) if "[[SECRET_VARIABLE:" in text else text)


def restore_known_secrets(data: Any) -> Any:
    """*data* read back from storage with each marker of this scope turned back into its value.

    Where the platform rebuilds the model's context from what it stored (a
    resumed run, a reloaded conversation, an approved tool call): the model and
    the tools get the value they would have had. A marker of another run or
    conversation, a forged one, or one whose variable the scope did not
    resolve stays as it is.
    """
    scope = _known_secrets.get()
    if scope is None:
        return data
    return _walk(data, lambda text: scope.restore_text(text) if "[[SECRET_VARIABLE:" in text else text)


def restored_secret_fingerprints(data: Any) -> set[str]:
    """A keyed digest of each value :func:`restore_known_secrets` puts into *data* in the open scope, never the value.

    Kept where the restored copy goes out of the platform's hands (a runner
    command), so what comes back can be checked with :func:`known_secrets_cover`.
    """
    scope = _known_secrets.get()
    found: set[str] = set()

    def _collect(text: str) -> str:
        if scope is not None and "[[SECRET_VARIABLE:" in text:
            found.update(scope.restored_fingerprints(text))
        return text

    _walk(data, _collect)
    return found


def known_secrets_cover(fingerprints: Any) -> bool:
    """Whether the open scope still holds every value *fingerprints* name, so text echoing one is redacted whole.

    ``False`` once one of them was rotated, deleted, unshared or cannot be read
    any more: the caller must not store what may echo it.
    """
    scope = _known_secrets.get()
    return all(scope is not None and scope.knows_fingerprint(str(item)) for item in fingerprints or ())


class KnownSecretStream:
    """Redact a stream of text chunks (model output streamed to a user) without ever showing part of a secret.

    Text that could be the start of a known value is held back until the next
    chunk decides, so what is emitted is final: nothing shown is ever taken
    back. :meth:`flush` releases the rest when the stream ends.
    """

    def __init__(self, *, display: bool = False) -> None:
        self._display = display
        self._pending = ""

    def _redact(self, text: str, scope: KnownSecrets) -> str:
        if self._display:
            return scope.redact_text(text, SECRET_DISPLAY_TEXT)
        return scope.redact_text(text)

    def feed(self, chunk: str) -> str:
        """The part of the stream so far that is safe to show; the rest waits for the next chunk.

        Decided on the raw text: a value that is also the start of a longer one
        is held until the next chunk says which it is, and a whole value (or a
        marker) is never split by the cut, so what is shown is redacted whole.
        """
        scope = _known_secrets.get()
        text = self._pending + chunk
        if not scope:
            self._pending = ""
            return text
        cut = len(text) - scope.partial_suffix(text)
        for match in scope._compiled().finditer(text):
            if match.start() < cut < match.end():
                cut = match.start()
                break
        self._pending = text[cut:]
        return self._redact(text[:cut], scope)

    def flush(self) -> str:
        scope = _known_secrets.get()
        out, self._pending = self._pending, ""
        return self._redact(out, scope) if scope else out

    def discard(self) -> None:
        """Drop what is held: the answer it belonged to was taken back."""
        self._pending = ""


def redacted_stream_callbacks(
    on_token: Callable[[str], Any] | None, on_event: Callable[[dict], Any] | None
) -> tuple[Callable[[str], Any] | None, Callable[[dict], Any] | None, Callable[[], Any] | None]:
    """Wrap an agent loop's token and event callbacks so model output reaches the user redacted.

    The answer tokens and the reasoning deltas (``thinking_text`` events) each
    go through a :class:`KnownSecretStream`, so a value split across chunks is
    never shown in part. What is held is released at the next boundary: held
    reasoning before the answer starts and before any other event, held answer
    text before any other event (so the order on the wire is kept); held
    answer text is dropped on a ``stream_retract``. Other events are redacted
    whole. The third item releases what is still held when the stream ends;
    all three are the callbacks as given (and ``None``) outside a scope.
    """
    if _known_secrets.get() is None or (on_token is None and on_event is None):
        return on_token, on_event, None
    answer = KnownSecretStream()
    reasoning = KnownSecretStream()

    async def release_reasoning() -> None:
        held = reasoning.flush()
        if held and on_event is not None:
            await on_event({"status": "thinking_text", "content": held})

    async def release() -> None:
        await release_reasoning()
        held = answer.flush()
        if held and on_token is not None:
            await on_token(held)

    async def token(chunk: str) -> None:
        await release_reasoning()
        if visible := answer.feed(chunk):
            await on_token(visible)

    async def event(evt: dict) -> None:
        if isinstance(evt, dict) and evt.get("status") == "thinking_text" and isinstance(evt.get("content"), str):
            if visible := reasoning.feed(evt["content"]):
                await on_event(redact_frame({**evt, "content": visible}))
            return
        if isinstance(evt, dict) and evt.get("status") == "stream_retract":
            answer.discard()
            await release_reasoning()
        else:
            await release()
        await on_event(redact_frame(evt))

    return (token if on_token is not None else None), (event if on_event is not None else None), release


def redact_log_event(_logger: object, _method: str, event_dict: dict) -> dict:
    """A structlog processor: the known secret values of the current run or turn out of a log line.

    Runs after the exception is rendered and before the renderer
    (``core/logging.py``), so the message, the ``extra`` fields and the
    traceback all go through it, in the task that logs.
    """
    return redact_known_secrets(event_dict)
