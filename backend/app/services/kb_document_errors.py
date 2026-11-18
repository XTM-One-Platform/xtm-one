# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""What a knowledge base document's processing failure is allowed to say.

``KnowledgeDocument.error_message`` is served to every reader of the knowledge
base (three-tier visibility, so read-only group members and every user of a
company-managed KB) and read back to agents. It therefore only ever holds a
fixed sentence the platform authored: a failure is classified where it happens
and the raw exception text goes to the server log, correlated by document id.

Rows written before this rule may still hold raw exception text. They are
cleaned when they are read: :func:`public_document_error` recognises the
platform's own sentences and answers the generic internal-error sentence for
anything else, so no serialiser can leak a legacy row. The activity search
matches the same served text (``knowledge_bases._activity_search_filter``,
built from ``PLATFORM_SENTENCES`` and ``VALUED_SENTENCES_FOR_SQL``), so it
cannot be used to probe a legacy row either.

Stdlib only: imported by the document processor, the KB helpers, the KB API
and the agent-facing KB tool.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from enum import StrEnum


class DocumentErrorCode(StrEnum):
    NO_CONTENT = "no_content"
    KNOWLEDGE_BASE_NOT_FOUND = "knowledge_base_not_found"
    NO_EXTRACTABLE_TEXT = "no_extractable_text"
    EMBEDDING_NOT_CONFIGURED = "embedding_not_configured"
    EMBEDDING_NOT_AVAILABLE = "embedding_not_available"
    EMBEDDING_PROVIDER_ERROR = "embedding_provider_error"
    DIMENSION_MISMATCH = "dimension_mismatch"
    TIMEOUT = "timeout"
    WORKER_TIMEOUT = "worker_timeout"
    INTERNAL_ERROR = "internal_error"


NO_CONTENT_MESSAGE = "No content to process"
KNOWLEDGE_BASE_NOT_FOUND_MESSAGE = "Knowledge base not found"
NO_CHUNKS_MESSAGE = (
    "Document contains no extractable text; no chunks were produced during preprocessing"
)
NO_VALID_CHUNKS_MESSAGE = "Document contains no extractable text after filtering short chunks"
NO_VECTORS_MESSAGE = "Embedding provider returned no vectors; document could not be embedded"
EMBEDDING_NOT_CONFIGURED_MESSAGE = (
    "No embedding model is configured. Set one in Settings > AI and models > Models, then "
    "re-embed this document."
)
# The embedding provider excludes the person the document is embedded for
# (``provider_access``): final, no automatic retry.
EMBEDDING_NOT_AVAILABLE_MESSAGE = (
    "The embedding model is not available to the person this document was added "
    "for: the LLM provider configured for embeddings is not used for them. An "
    "administrator can configure another embedding provider in Settings > AI "
    "and models > Models, then re-embed this document."
)
# No sentence promises an automatic retry: ``schedule_embedding_retry`` books
# none when auto-retry is off or the document's attempts are spent, and the
# schedule is served on its own (``next_auto_retry_at``).
EMBEDDING_PROVIDER_ERROR_MESSAGE = (
    "The embedding provider could not embed this document. Check the embedding "
    "model settings if the error persists."
)
TIMEOUT_MESSAGE = "Embedding this document timed out. Re-embed it if the error persists."
INTERNAL_ERROR_MESSAGE = (
    "Internal error while processing this document. The server logs have the details."
)

_EXACT_MESSAGES: dict[str, DocumentErrorCode] = {
    NO_CONTENT_MESSAGE: DocumentErrorCode.NO_CONTENT,
    KNOWLEDGE_BASE_NOT_FOUND_MESSAGE: DocumentErrorCode.KNOWLEDGE_BASE_NOT_FOUND,
    NO_CHUNKS_MESSAGE: DocumentErrorCode.NO_EXTRACTABLE_TEXT,
    NO_VALID_CHUNKS_MESSAGE: DocumentErrorCode.NO_EXTRACTABLE_TEXT,
    NO_VECTORS_MESSAGE: DocumentErrorCode.EMBEDDING_PROVIDER_ERROR,
    EMBEDDING_NOT_CONFIGURED_MESSAGE: DocumentErrorCode.EMBEDDING_NOT_CONFIGURED,
    EMBEDDING_NOT_AVAILABLE_MESSAGE: DocumentErrorCode.EMBEDDING_NOT_AVAILABLE,
    EMBEDDING_PROVIDER_ERROR_MESSAGE: DocumentErrorCode.EMBEDDING_PROVIDER_ERROR,
    TIMEOUT_MESSAGE: DocumentErrorCode.TIMEOUT,
    INTERNAL_ERROR_MESSAGE: DocumentErrorCode.INTERNAL_ERROR,
}

#: The fixed sentences (the two valued ones aside): a stored reason equal to
#: one of them is served, and searched, exactly as written.
PLATFORM_SENTENCES: frozenset[str] = frozenset(_EXACT_MESSAGES)

#: Fixed sentences of failures another embedding provider can fix; content
#: failures are not. A dimension mismatch is a valued sentence.
PROVIDER_FAILURE_SENTENCES: frozenset[str] = frozenset(
    sentence
    for sentence, code in _EXACT_MESSAGES.items()
    if code
    in {
        DocumentErrorCode.EMBEDDING_NOT_CONFIGURED,
        DocumentErrorCode.EMBEDDING_NOT_AVAILABLE,
        DocumentErrorCode.EMBEDDING_PROVIDER_ERROR,
        DocumentErrorCode.TIMEOUT,
    }
)

# The two sentences that carry a value. Matched on their fixed opening so the
# older wording of each (written before this module existed) is recognised
# too; the served sentence is REBUILT from the parsed values, never the stored
# text, so whatever follows the opening of a legacy row is never served.
DIMENSION_MISMATCH_OPENING = "Embedding model '"
WORKER_TIMEOUT_OPENING = "Worker did not finish embedding within "
_MODEL_NAME_MAX_CHARS = 200
_MODEL_NAME_UNSAFE_RE = re.compile(r"['\x00-\x1f]")
_DIMENSION_MISMATCH_RE = re.compile(
    rf"^{re.escape(DIMENSION_MISMATCH_OPENING)}"
    rf"(?P<model>[^'\x00-\x1f]{{0,{_MODEL_NAME_MAX_CHARS}}})' produced "
    r"(?P<produced>[0-9]+(?:, [0-9]+)*)-dimension vector\(s\) but the "
    r"platform is configured for (?P<expected>[0-9]{1,9}) dimensions\."
)
_WORKER_TIMEOUT_RE = re.compile(
    rf"^{re.escape(WORKER_TIMEOUT_OPENING)}(?P<seconds>[0-9]{{1,9}})s\b"
)
_DIMENSION_MISMATCH_TEMPLATE = (
    DIMENSION_MISMATCH_OPENING + "{model}' produced {produced}-dimension vector(s) but the "
    "platform is configured for {expected} dimensions. Re-embedding cannot reconcile this - "
    "update Settings > AI and models > Models > Vector Dimensions to match the model, or pick a model that "
    "outputs {expected} dimensions."
)
_WORKER_TIMEOUT_TEMPLATE = (
    WORKER_TIMEOUT_OPENING + "{seconds}s - re-enqueued by the stuck-document sweeper."
)

#: The two valued sentences for a SQL search that must match them as served:
#: ``(PostgreSQL pattern, template, integer fields)``. Each pattern captures
#: the template's fields in their order of first appearance - the values the
#: patterns above parse (PostgreSQL's ``\y`` is ``\b``, and a text value never
#: holds NUL) - and an integer field is served as its ``int`` rendering.
VALUED_SENTENCES_FOR_SQL: tuple[tuple[str, str, frozenset[str]], ...] = (
    (
        rf"^Embedding model '([^'\x01-\x1f]{{0,{_MODEL_NAME_MAX_CHARS}}})' produced "
        r"([0-9]+(?:, [0-9]+)*)-dimension vector\(s\) but the platform is configured for "
        r"([0-9]{1,9}) dimensions\.",
        _DIMENSION_MISMATCH_TEMPLATE,
        frozenset({"expected"}),
    ),
    (
        r"^Worker did not finish embedding within ([0-9]{1,9})s\y",
        _WORKER_TIMEOUT_TEMPLATE,
        frozenset({"seconds"}),
    ),
)


def dimension_mismatch_message(model: str | None, produced: str, expected: int) -> str:
    # The model name is cleaned the way the read side parses it back, so the
    # sentence written here is always the one served.
    model_name = _MODEL_NAME_UNSAFE_RE.sub("", str(model))[:_MODEL_NAME_MAX_CHARS]
    return _DIMENSION_MISMATCH_TEMPLATE.format(
        model=model_name, produced=produced, expected=expected
    )


def worker_timeout_message(seconds: int) -> str:
    return _WORKER_TIMEOUT_TEMPLATE.format(seconds=seconds)


# Exceptions raised by an embedding provider's SDK or transport, recognised by
# the module that defines the exception class (anywhere in its MRO), never by
# its message.
_PROVIDER_MODULE_PREFIXES = (
    "app.llm",
    "openai",
    "anthropic",
    "httpx",
    "httpcore",
    # google-genai runs its async client (Gemini embeddings) on aiohttp
    # whenever aiohttp is installed, so those transport errors are aiohttp's.
    "aiohttp",
    "google",
    "mistralai",
    "botocore",
    "aiobotocore",
    "litellm",
)

# A provider ANSWER that reports a timeout, read from the exception's parsed
# fields: an HTTP 408 / 504 (Gemini's ``ServerError`` 504 DEADLINE_EXCEEDED, an
# OpenAI-compatible gateway timeout) or a Bedrock error code naming one
# (``ModelTimeoutException``).
_TIMEOUT_HTTP_STATUSES = frozenset({408, 504})


def _defined_under(exc: BaseException, prefixes: tuple[str, ...]) -> bool:
    for cls in type(exc).__mro__:
        module = getattr(cls, "__module__", "") or ""
        if any(module == p or module.startswith(p + ".") for p in prefixes):
            return True
    return False


def _as_status(value: object) -> int | None:
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else None


def _http_status(exc: BaseException) -> int | None:
    # ``status_code``: openai / anthropic; ``status`` then ``code``: aiohttp,
    # google-genai (whose ``status`` is the textual one) and google-api-core.
    for attr in ("status_code", "status", "code"):
        if (status := _as_status(getattr(exc, attr, None))) is not None:
            return status
    response = getattr(exc, "response", None)
    if (status := _as_status(getattr(response, "status_code", None))) is not None:
        return status  # httpx.HTTPStatusError
    if isinstance(response, dict):  # botocore ClientError
        metadata = response.get("ResponseMetadata")
        if isinstance(metadata, dict):
            return _as_status(metadata.get("HTTPStatusCode"))
    return None


def _botocore_error_code(exc: BaseException) -> str | None:
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        error = response.get("Error")
        if isinstance(error, dict) and isinstance(error.get("Code"), str):
            return error["Code"]
    return None


def _reports_timeout(exc: BaseException) -> bool:
    # SDK and HTTP-client timeouts (``httpx.TimeoutException``,
    # ``openai.APITimeoutError``...) are not builtin ``TimeoutError``s: a
    # ``Timeout`` class in their MRO says it, else the status they carry.
    if any("timeout" in cls.__name__.lower() for cls in type(exc).__mro__):
        return True
    if _http_status(exc) in _TIMEOUT_HTTP_STATUSES:
        return True
    code = _botocore_error_code(exc)
    return code is not None and "timeout" in code.lower()


def classify_document_exception(exc: BaseException) -> tuple[DocumentErrorCode, str]:
    """``(code, sentence)`` for an exception raised while processing a document.

    Decided by the exception's type and, for a provider exception, the status
    fields it carries: its message is never stored or served. Never raises -
    it runs inside the handlers that record the failure.
    """
    if type(exc).__name__ == "EmbeddingConfigurationError" and _defined_under(
        exc, ("app.llm",)
    ):
        return DocumentErrorCode.EMBEDDING_NOT_CONFIGURED, EMBEDDING_NOT_CONFIGURED_MESSAGE
    if isinstance(exc, TimeoutError):
        return DocumentErrorCode.TIMEOUT, TIMEOUT_MESSAGE
    if _defined_under(exc, _PROVIDER_MODULE_PREFIXES):
        try:
            timed_out = _reports_timeout(exc)
        except Exception:
            timed_out = False
        if timed_out:
            return DocumentErrorCode.TIMEOUT, TIMEOUT_MESSAGE
        return DocumentErrorCode.EMBEDDING_PROVIDER_ERROR, EMBEDDING_PROVIDER_ERROR_MESSAGE
    return DocumentErrorCode.INTERNAL_ERROR, INTERNAL_ERROR_MESSAGE


def classify_embedding_failures(
    failures: Iterable[BaseException], *, returned_empty: bool
) -> tuple[DocumentErrorCode, str]:
    """``(code, sentence)`` for a document none of whose chunks could be embedded.

    *failures* are the exceptions the embedding calls raised, each classified
    by :func:`classify_document_exception`; *returned_empty* says whether some
    calls returned no usable vector instead. The most actionable cause is
    named, whatever order the calls failed in: a missing configuration, then
    an error the provider answered with, then a timeout - only when every call
    timed out -, else "no vectors" (an empty vector, or an exception from the
    embedding call that is not recognised as the provider's).
    """
    codes = {classify_document_exception(exc)[0] for exc in failures}
    if DocumentErrorCode.EMBEDDING_NOT_CONFIGURED in codes:
        return DocumentErrorCode.EMBEDDING_NOT_CONFIGURED, EMBEDDING_NOT_CONFIGURED_MESSAGE
    if DocumentErrorCode.EMBEDDING_PROVIDER_ERROR in codes:
        return DocumentErrorCode.EMBEDDING_PROVIDER_ERROR, EMBEDDING_PROVIDER_ERROR_MESSAGE
    if codes == {DocumentErrorCode.TIMEOUT} and not returned_empty:
        return DocumentErrorCode.TIMEOUT, TIMEOUT_MESSAGE
    return DocumentErrorCode.EMBEDDING_PROVIDER_ERROR, NO_VECTORS_MESSAGE


def public_document_error(stored: str | None) -> tuple[DocumentErrorCode, str] | None:
    """``(code, sentence)`` safe to serve for a stored ``error_message``.

    A sentence the platform writes passes through with its code; anything else
    (raw exception text from an older row) becomes the generic
    internal-error sentence.
    """
    if not stored or not stored.strip():
        return None
    code = _EXACT_MESSAGES.get(stored)
    if code is not None:
        return code, stored
    if match := _DIMENSION_MISMATCH_RE.match(stored):
        return DocumentErrorCode.DIMENSION_MISMATCH, dimension_mismatch_message(
            match["model"], match["produced"], int(match["expected"])
        )
    if match := _WORKER_TIMEOUT_RE.match(stored):
        return DocumentErrorCode.WORKER_TIMEOUT, worker_timeout_message(int(match["seconds"]))
    return DocumentErrorCode.INTERNAL_ERROR, INTERNAL_ERROR_MESSAGE


def public_error_message(stored: str | None) -> str | None:
    public = public_document_error(stored)
    return public[1] if public else None


def public_error_code(stored: str | None) -> str | None:
    public = public_document_error(stored)
    return str(public[0]) if public else None


EMBEDDING_FAILED_EVENT = "embedding_failed"
EMBEDDING_FAILED_PREFIX = "Failed to embed '"
EMBEDDING_FAILED_SEPARATOR = "': "
#: How an ``embedding_failed`` entry that is not in the processor's shape is
#: served.
UNSHAPED_EMBEDDING_FAILED_DETAILS = "Failed to embed a document: " + INTERNAL_ERROR_MESSAGE


def embedding_failed_details(filename: str, sentence: str) -> str:
    # The reader splits at the FIRST separator: splitting at the last would
    # serve part of a legacy row's raw text (which may contain one) as the
    # filename. So a separator inside the filename is spaced out here.
    name = filename.replace(EMBEDDING_FAILED_SEPARATOR, "' : ")
    return f"{EMBEDDING_FAILED_PREFIX}{name}{EMBEDDING_FAILED_SEPARATOR}{sentence}"


def public_activity_details(event: str | None, details: str | None) -> str | None:
    """The ``details`` of a KB activity entry, safe to serve.

    Only ``embedding_failed`` entries embed a failure reason. Their reason is
    cleaned like a document's ``error_message``; an entry whose shape is not
    the one the processor writes is answered with the generic sentence.
    """
    if event != EMBEDDING_FAILED_EVENT or not details:
        return details
    if details.startswith(EMBEDDING_FAILED_PREFIX):
        rest = details[len(EMBEDDING_FAILED_PREFIX) :]
        sep = rest.find(EMBEDDING_FAILED_SEPARATOR)
        if sep != -1:
            public = public_document_error(rest[sep + len(EMBEDDING_FAILED_SEPARATOR) :])
            sentence = public[1] if public else INTERNAL_ERROR_MESSAGE
            return embedding_failed_details(rest[:sep], sentence)
    return UNSHAPED_EMBEDDING_FAILED_DETAILS
