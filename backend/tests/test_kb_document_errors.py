"""A document's processing failure is served as a fixed sentence, never raw text.

The processor classifies a failure where it happens and stores the platform's
sentence; the raw exception text only reaches the server log. Rows written
before the fix are cleaned when they are read.
"""

from __future__ import annotations

import logging
import re
import string
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import anthropic
import asyncpg
import botocore.exceptions as botocore_errors
import google.api_core.exceptions as google_api_errors
import httpcore
import httpx
import openai
import pytest
from google.genai import errors as genai_errors
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import DBAPIError, ProgrammingError

from app.api.v1 import knowledge_bases as kb_mod
from app.llm.router import AllProvidersFailedError, EmbeddingConfigurationError
from app.models.knowledge_base import KnowledgeDocument
from app.services import document_processor
from app.services.kb_document_errors import (
    EMBEDDING_NOT_CONFIGURED_MESSAGE,
    EMBEDDING_PROVIDER_ERROR_MESSAGE,
    INTERNAL_ERROR_MESSAGE,
    KNOWLEDGE_BASE_NOT_FOUND_MESSAGE,
    NO_CHUNKS_MESSAGE,
    NO_CONTENT_MESSAGE,
    NO_VALID_CHUNKS_MESSAGE,
    NO_VECTORS_MESSAGE,
    PLATFORM_SENTENCES,
    TIMEOUT_MESSAGE,
    VALUED_SENTENCES_FOR_SQL,
    DocumentErrorCode,
    classify_document_exception,
    classify_embedding_failures,
    dimension_mismatch_message,
    embedding_failed_details,
    public_activity_details,
    public_document_error,
    worker_timeout_message,
)
from tests.test_document_processor import _build_doc_and_kb, _FakeSession
from tests.test_knowledge_bases_list import _ExplodingDB, _fake_kb


@pytest.fixture(autouse=True)
def _embedding_config_not_moved(monkeypatch):
    """The platform embedding config is the one each document job embedded with."""
    monkeypatch.setattr("app.services.document_processor.embedding_config_moved", AsyncMock(return_value=False))


# Stands for any text a driver or SDK puts in an exception: it must never be
# stored on the row or served.
_RAW = "internal-detail-marker"
_REQUEST = httpx.Request("POST", "https://embeddings.example/v1")


def _response(status: int) -> httpx.Response:
    return httpx.Response(status, request=_REQUEST)


def _genai_error(cls: type, status: int, reason: str) -> Exception:
    return cls(status, {"error": {"code": status, "message": _RAW, "status": reason}})


def _bedrock_error(code: str, http_status: int | None = None) -> Exception:
    response: dict = {"Error": {"Code": code, "Message": _RAW}}
    if http_status is not None:
        response["ResponseMetadata"] = {"HTTPStatusCode": http_status}
    return botocore_errors.ClientError(response, "InvokeModel")


def _defined_in(module: str, name: str = "SDKError") -> Exception:
    # For an SDK the classifier recognises that is not a dependency here.
    cls = type(name, (Exception,), {})
    cls.__module__ = module
    return cls(_RAW)


# -- classification by structure ------------------------------------------------


def test_classifier_reads_the_exception_type_not_its_message():
    assert classify_document_exception(TimeoutError(_RAW))[0] == DocumentErrorCode.TIMEOUT
    provider = classify_document_exception(httpx.ConnectError(_RAW))
    assert provider == (
        DocumentErrorCode.EMBEDDING_PROVIDER_ERROR,
        EMBEDDING_PROVIDER_ERROR_MESSAGE,
    )
    driver = classify_document_exception(ProgrammingError(_RAW, {}, Exception(_RAW)))
    assert driver == (DocumentErrorCode.INTERNAL_ERROR, INTERNAL_ERROR_MESSAGE)
    assert classify_document_exception(ValueError(_RAW))[1] == INTERNAL_ERROR_MESSAGE


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ReadTimeout(_RAW, request=_REQUEST),
        httpx.ConnectTimeout(_RAW, request=_REQUEST),
        httpx.WriteTimeout(_RAW, request=_REQUEST),
        httpx.PoolTimeout(_RAW, request=_REQUEST),
        httpcore.ReadTimeout(_RAW),
        openai.APITimeoutError(request=_REQUEST),
        anthropic.APITimeoutError(request=_REQUEST),
        google_api_errors.DeadlineExceeded(_RAW),
        botocore_errors.ReadTimeoutError(endpoint_url="https://bedrock.example"),
        botocore_errors.ConnectTimeoutError(endpoint_url="https://bedrock.example"),
        aiohttp.ServerTimeoutError(_RAW),
        TimeoutError(),
        _defined_in("litellm.exceptions", "Timeout"),
    ],
    ids=lambda exc: type(exc).__name__,
)
def test_sdk_and_http_client_timeouts_are_timeouts(exc):
    assert classify_document_exception(exc) == (DocumentErrorCode.TIMEOUT, TIMEOUT_MESSAGE)


@pytest.mark.parametrize(
    "exc",
    [
        # A provider ANSWER naming a timeout, read from its status fields.
        _genai_error(genai_errors.ServerError, 504, "DEADLINE_EXCEEDED"),
        openai.InternalServerError(_RAW, response=_response(504), body=None),
        openai.APIStatusError(_RAW, response=_response(408), body=None),
        httpx.HTTPStatusError(_RAW, request=_REQUEST, response=_response(504)),
        aiohttp.ClientResponseError(None, (), status=504),
        _bedrock_error("ModelTimeoutException"),
        _bedrock_error("ValidationException", http_status=408),
    ],
    ids=["genai-504", "openai-504", "openai-408", "httpx-504", "aiohttp-504", "bedrock-code", "bedrock-408"],
)
def test_server_reported_timeouts_are_timeouts(exc):
    assert classify_document_exception(exc) == (DocumentErrorCode.TIMEOUT, TIMEOUT_MESSAGE)


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectError(_RAW, request=_REQUEST),
        httpx.HTTPStatusError(_RAW, request=_REQUEST, response=_response(503)),
        httpcore.ConnectError(_RAW),
        openai.APIConnectionError(request=_REQUEST),
        openai.RateLimitError(_RAW, response=_response(429), body=None),
        openai.AuthenticationError(_RAW, response=_response(401), body=None),
        anthropic.APIConnectionError(request=_REQUEST),
        anthropic.InternalServerError(_RAW, response=_response(500), body=None),
        # Gemini: the SDK's own errors, and its async transport (aiohttp).
        _genai_error(genai_errors.ServerError, 503, "UNAVAILABLE"),
        _genai_error(genai_errors.ClientError, 400, "INVALID_ARGUMENT"),
        aiohttp.ClientConnectionError(_RAW),
        aiohttp.ClientResponseError(None, (), status=503),
        google_api_errors.ServiceUnavailable(_RAW),
        botocore_errors.EndpointConnectionError(endpoint_url="https://bedrock.example"),
        _bedrock_error("ThrottlingException", http_status=429),
        AllProvidersFailedError([("openai", RuntimeError(_RAW))]),
        _defined_in("mistralai.models"),
        _defined_in("aiobotocore.session"),
        _defined_in("litellm.exceptions", "APIError"),
    ],
    ids=lambda exc: f"{type(exc).__module__}.{type(exc).__name__}",
)
def test_every_provider_module_the_classifier_claims_is_a_provider_error(exc):
    assert classify_document_exception(exc) == (
        DocumentErrorCode.EMBEDDING_PROVIDER_ERROR,
        EMBEDDING_PROVIDER_ERROR_MESSAGE,
    )


@pytest.mark.parametrize(
    "exc",
    [
        asyncpg.exceptions.UndefinedColumnError(_RAW),
        asyncpg.exceptions.QueryCanceledError(_RAW),
        DBAPIError(_RAW, {}, asyncpg.exceptions.UndefinedColumnError(_RAW)),
        RuntimeError(_RAW),
        ConnectionRefusedError(_RAW),
    ],
    ids=lambda exc: type(exc).__name__,
)
def test_anything_else_is_an_internal_error(exc):
    assert classify_document_exception(exc) == (
        DocumentErrorCode.INTERNAL_ERROR,
        INTERNAL_ERROR_MESSAGE,
    )


def test_the_classifier_never_raises_on_a_provider_exception_it_cannot_read():
    class _UnreadableError(Exception):
        @property
        def status_code(self):
            raise RuntimeError(_RAW)

    _UnreadableError.__module__ = "openai._exceptions"

    assert classify_document_exception(_UnreadableError(_RAW)) == (
        DocumentErrorCode.EMBEDDING_PROVIDER_ERROR,
        EMBEDDING_PROVIDER_ERROR_MESSAGE,
    )


def test_missing_embedding_configuration_gets_its_own_sentence():
    config_error = type("EmbeddingConfigurationError", (ValueError,), {})
    config_error.__module__ = "app.llm.router"

    for exc in (config_error(_RAW), EmbeddingConfigurationError(_RAW)):
        assert classify_document_exception(exc) == (
            DocumentErrorCode.EMBEDDING_NOT_CONFIGURED,
            EMBEDDING_NOT_CONFIGURED_MESSAGE,
        )


_TIMEOUT = httpx.ReadTimeout(_RAW, request=_REQUEST)
_PROVIDER = httpx.ConnectError(_RAW, request=_REQUEST)


@pytest.mark.parametrize(
    ("failures", "returned_empty", "expected"),
    [
        ([_TIMEOUT, _TIMEOUT], False, (DocumentErrorCode.TIMEOUT, TIMEOUT_MESSAGE)),
        # A timeout is named only when every call timed out.
        ([_TIMEOUT], True, (DocumentErrorCode.EMBEDDING_PROVIDER_ERROR, NO_VECTORS_MESSAGE)),
        ([_TIMEOUT, RuntimeError(_RAW)], False, (DocumentErrorCode.EMBEDDING_PROVIDER_ERROR, NO_VECTORS_MESSAGE)),
        ([_TIMEOUT, _PROVIDER], False, (DocumentErrorCode.EMBEDDING_PROVIDER_ERROR, EMBEDDING_PROVIDER_ERROR_MESSAGE)),
        ([_PROVIDER, _TIMEOUT], True, (DocumentErrorCode.EMBEDDING_PROVIDER_ERROR, EMBEDDING_PROVIDER_ERROR_MESSAGE)),
        (
            [_PROVIDER, EmbeddingConfigurationError(_RAW)],
            False,
            (DocumentErrorCode.EMBEDDING_NOT_CONFIGURED, EMBEDDING_NOT_CONFIGURED_MESSAGE),
        ),
        # Nothing the provider can be told apart by: the provider returned no vectors.
        ([RuntimeError(_RAW)], False, (DocumentErrorCode.EMBEDDING_PROVIDER_ERROR, NO_VECTORS_MESSAGE)),
        ([], True, (DocumentErrorCode.EMBEDDING_PROVIDER_ERROR, NO_VECTORS_MESSAGE)),
    ],
)
def test_a_document_whose_every_chunk_failed_names_the_most_actionable_cause(
    failures, returned_empty, expected
):
    assert classify_embedding_failures(failures, returned_empty=returned_empty) == expected
    assert classify_embedding_failures(reversed(failures), returned_empty=returned_empty) == expected


def test_no_sentence_promises_an_automatic_retry():
    # ``schedule_embedding_retry`` books none when auto-retry is off or the
    # document's attempts are spent.
    for sentence in PLATFORM_SENTENCES:
        assert "automatically" not in sentence.lower(), sentence


# -- read side: platform sentences pass, anything else is cleaned -------------


@pytest.mark.parametrize(
    "sentence",
    [
        NO_CONTENT_MESSAGE,
        KNOWLEDGE_BASE_NOT_FOUND_MESSAGE,
        NO_CHUNKS_MESSAGE,
        NO_VALID_CHUNKS_MESSAGE,
        NO_VECTORS_MESSAGE,
        EMBEDDING_NOT_CONFIGURED_MESSAGE,
        EMBEDDING_PROVIDER_ERROR_MESSAGE,
        TIMEOUT_MESSAGE,
        INTERNAL_ERROR_MESSAGE,
        dimension_mismatch_message("embed-model", "768", 1536),
        worker_timeout_message(600),
    ],
)
def test_platform_sentences_pass_through(sentence):
    public = public_document_error(sentence)
    assert public is not None
    assert public[1] == sentence


def test_older_wording_of_the_valued_sentences_is_still_recognised():
    older_dimension = (
        "Embedding model 'embed-model' produced 768-dimension vector(s) but the "
        "platform is configured for 1536 dimensions. Re-embedding cannot "
        "reconcile this \u2014 update Settings \u2192 AI Models."
    )
    older_worker = (
        "Worker did not finish embedding within 600s \u2014 re-enqueued by the "
        "stuck-document sweeper."
    )
    assert public_document_error(older_dimension) == (
        DocumentErrorCode.DIMENSION_MISMATCH,
        dimension_mismatch_message("embed-model", "768", 1536),
    )
    assert public_document_error(older_worker) == (
        DocumentErrorCode.WORKER_TIMEOUT,
        worker_timeout_message(600),
    )


def test_a_recognised_opening_never_carries_the_rest_of_a_legacy_row():
    for stored in (
        dimension_mismatch_message("embed-model", "768", 1536) + f" {_RAW}",
        f"Worker did not finish embedding within 600s {_RAW}",
    ):
        public = public_document_error(stored)
        assert public is not None
        assert _RAW not in public[1]
        assert _RAW not in (public_activity_details("embedding_failed", f"Failed to embed 'a': {stored}") or "")


@pytest.mark.parametrize(
    "stored",
    [
        dimension_mismatch_message("embed-model", "768, 1024", 1536),
        dimension_mismatch_message("", "768", 1536),
        "Embedding model 'm' produced 768-dimension vector(s) but the platform is "
        f"configured for 01536 dimensions. {_RAW}",
        worker_timeout_message(600),
        f"Worker did not finish embedding within 0600s \u2014 {_RAW}",
        "Worker did not finish embedding within 600sx",
        f"Embedding model 'm' produced lots {_RAW}",
        _RAW,
    ],
)
def test_the_sql_patterns_rebuild_the_valued_sentences_as_the_reader_does(stored):
    # The activity search rebuilds a valued sentence in SQL from these
    # patterns and templates (``knowledge_bases._served_valued_sentence``);
    # read the same way here, they must give exactly what is served.
    rebuilt = None
    for pattern, template, integer_fields in VALUED_SENTENCES_FOR_SQL:
        match = re.match(pattern.replace(r"\y", r"\b"), stored)
        if match is None:
            continue
        fields = list(dict.fromkeys(f for _, f, _, _ in string.Formatter().parse(template) if f))
        values = {
            name: str(int(value)) if name in integer_fields else value
            for name, value in zip(fields, match.groups(), strict=True)
        }
        rebuilt = template.format(**values)
        break

    code, served = public_document_error(stored)
    if code in (DocumentErrorCode.DIMENSION_MISMATCH, DocumentErrorCode.WORKER_TIMEOUT):
        assert rebuilt == served
    else:
        assert rebuilt is None


@pytest.mark.parametrize("model", ["", "a'b", "m\x00n", "x" * 300, None])
def test_the_written_dimension_sentence_is_always_the_one_served(model):
    # A model-optional provider runs with no model name, and a configured one
    # may hold a quote or be long: the writer cleans it the way the reader
    # parses it, so the sentence is never mistaken for raw text.
    written = dimension_mismatch_message(model, "768, 1024", 1536)
    assert public_document_error(written) == (DocumentErrorCode.DIMENSION_MISMATCH, written)


def test_legacy_raw_text_is_served_as_the_generic_sentence():
    assert public_document_error(_RAW) == (
        DocumentErrorCode.INTERNAL_ERROR,
        INTERNAL_ERROR_MESSAGE,
    )
    assert public_document_error(None) is None
    assert public_document_error("   ") is None


@pytest.mark.parametrize("filename", ["notes': draft.md", "x': ", "a'': b", "file'", "x':"])
def test_a_filename_holding_the_separator_keeps_its_sentence(filename):
    # The reader splits at the first separator, so the writer spaces out the
    # ones a filename holds: the entry is served as written, sentence intact.
    details = embedding_failed_details(filename, TIMEOUT_MESSAGE)
    assert public_activity_details("embedding_failed", details) == details
    assert details.endswith(f"': {TIMEOUT_MESSAGE}")


def test_activity_details_keep_the_filename_and_clean_the_reason():
    assert public_activity_details(
        "embedding_failed", f"Failed to embed 'report.pdf': {_RAW}"
    ) == embedding_failed_details("report.pdf", INTERNAL_ERROR_MESSAGE)
    fresh = embedding_failed_details("report.pdf", NO_VECTORS_MESSAGE)
    assert public_activity_details("embedding_failed", fresh) == fresh
    unshaped = public_activity_details("embedding_failed", _RAW)
    assert unshaped is not None
    assert _RAW not in unshaped
    assert public_activity_details("document_added", "Uploaded 'a.txt'") == "Uploaded 'a.txt'"


# -- serialisers ---------------------------------------------------------------


def _legacy_doc() -> KnowledgeDocument:
    now = datetime.now(UTC)
    return KnowledgeDocument(
        id=uuid.uuid4(),
        knowledge_base_id=uuid.uuid4(),
        filename="legacy.txt",
        source_type="upload",
        status="error",
        chunk_count=0,
        content="x",
        error_message=_RAW,
        created_at=now,
        updated_at=now,
    )


def test_document_responses_never_serve_legacy_raw_text():
    doc = _legacy_doc()
    for resp in (kb_mod._doc_to_response(doc), kb_mod._doc_to_detail_response(doc)):
        assert resp.error_message == INTERNAL_ERROR_MESSAGE
        assert resp.error_code == "internal_error"
        assert _RAW not in resp.model_dump_json()


@pytest.mark.asyncio
async def test_the_driver_error_of_the_issue_never_reaches_a_response():
    # The raw driver text, as an older row holds it.
    driver_error = DBAPIError(
        "SELECT platform_settings.id, platform_settings.guard_exempt_tools FROM platform_settings",
        {},
        asyncpg.exceptions.UndefinedColumnError(
            "column platform_settings.guard_exempt_tools does not exist"
        ),
    )
    stored = str(driver_error)
    assert "guard_exempt_tools" in stored
    assert classify_document_exception(driver_error)[0] == DocumentErrorCode.INTERNAL_ERROR

    doc = _legacy_doc()
    doc.error_message = stored
    kb_response = await kb_mod._kb_to_response(
        _fake_kb(),
        _ExplodingDB(),
        can_manage=False,
        is_package_origin=False,
        status_counts={"error": 1},
        latest_error=stored,
        next_retry=None,
        retryable_error_count=0,
    )
    for payload in (
        kb_mod._doc_to_response(doc).model_dump_json(),
        kb_mod._doc_to_detail_response(doc).model_dump_json(),
        kb_response.model_dump_json(),
    ):
        assert "guard_exempt_tools" not in payload
        assert "SELECT" not in payload
        assert "internal_error" in payload


class _CapturingActivitySession:
    def __init__(self) -> None:
        self.statements: list = []

    async def execute(self, statement):
        self.statements.append(statement)
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=list), scalar_one=lambda: 0)


@pytest.mark.parametrize("manages", [True, False])
@pytest.mark.asyncio
async def test_the_activity_search_and_its_count_never_match_the_stored_details(
    monkeypatch, manages
):
    # A legacy ``embedding_failed`` entry holds raw text that is never served:
    # matching the column itself would let the count answer whether a hidden
    # term is in it - for a caller who manages the knowledge base and for a
    # reader alike. The real-database behaviour is pinned in
    # ``test_kb_activity_search_postgres.py``.
    kb = _fake_kb()
    monkeypatch.setattr(kb_mod, "get_user_knowledge_base", AsyncMock(return_value=kb))
    monkeypatch.setattr(kb_mod, "get_user_group_ids", AsyncMock(return_value=[]))
    session = _CapturingActivitySession()
    user = SimpleNamespace(id=kb.user_id if manages else uuid.uuid4(), role="user", is_admin=False)
    kb_id = str(kb.id)

    await kb_mod.list_activity_logs(
        kb_id, limit=50, offset=0, search="guard_exempt_tools", user=user, db=session
    )
    await kb_mod.count_activity_logs(kb_id, search="guard_exempt_tools", user=user, db=session)

    assert len(session.statements) == 2
    for statement in session.statements:
        sql = str(statement.compile(dialect=postgresql.dialect()))
        assert "kb_activity_logs.details ILIKE" not in sql
        assert "strpos(" in sql


# -- write side ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_processor_stores_the_classified_sentence_not_the_exception(monkeypatch):
    doc, kb = _build_doc_and_kb()
    session = _FakeSession(doc=doc, kb=kb)
    log_mock = AsyncMock()
    monkeypatch.setattr(document_processor, "async_session", lambda: session)
    monkeypatch.setattr(
        document_processor,
        "chunk_text",
        lambda *_args, **_kwargs: ["This chunk has enough content to be embedded."],
    )
    monkeypatch.setattr(document_processor, "log_kb_activity", log_mock)
    monkeypatch.setattr(
        "app.llm.router.get_embedding_provider",
        AsyncMock(side_effect=httpx.ConnectError(_RAW)),
    )

    with pytest.raises(httpx.ConnectError):
        await document_processor._process_document_impl(str(doc.id))

    assert doc.status == "error"
    assert doc.error_message == EMBEDDING_PROVIDER_ERROR_MESSAGE
    details = log_mock.await_args.kwargs["details"]
    assert details == embedding_failed_details(doc.filename, EMBEDDING_PROVIDER_ERROR_MESSAGE)
    assert _RAW not in details


async def _process_with_embed(monkeypatch, embed_side_effect, chunks):
    doc, kb = _build_doc_and_kb()
    session = _FakeSession(doc=doc, kb=kb)
    log_mock = AsyncMock()
    provider = SimpleNamespace(embed=AsyncMock(side_effect=embed_side_effect), provider_name="test")
    monkeypatch.setattr(document_processor, "async_session", lambda: session)
    monkeypatch.setattr(document_processor, "chunk_text", lambda *_args, **_kwargs: chunks)
    monkeypatch.setattr(document_processor, "log_kb_activity", log_mock)
    monkeypatch.setattr(
        "app.llm.router.get_embedding_provider",
        AsyncMock(return_value=(provider, "embed-model", str(uuid.uuid4()))),
    )
    assert await document_processor._process_document_impl(str(doc.id)) == 0
    return doc, log_mock.await_args.kwargs["details"]


@pytest.mark.asyncio
async def test_a_document_whose_every_chunk_timed_out_records_the_timeout(monkeypatch, caplog):
    chunks = ["The first chunk has enough content.", "The second chunk has enough content."]

    with caplog.at_level(logging.WARNING, logger=document_processor.logger.name):
        doc, details = await _process_with_embed(
            monkeypatch, httpx.ReadTimeout(_RAW, request=_REQUEST), chunks
        )

    assert doc.status == "error"
    assert doc.error_message == TIMEOUT_MESSAGE
    assert details == embedding_failed_details(doc.filename, TIMEOUT_MESSAGE)
    # The raw text only reaches the log, next to the document id.
    assert any(str(doc.id) in r.getMessage() and _RAW in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_a_timeout_is_not_named_when_another_chunk_came_back_empty(monkeypatch):
    chunks = ["The first chunk has enough content.", "The second chunk has enough content."]

    doc, details = await _process_with_embed(
        monkeypatch, [httpx.ReadTimeout(_RAW, request=_REQUEST), []], chunks
    )

    assert doc.error_message == NO_VECTORS_MESSAGE
    assert details == embedding_failed_details(doc.filename, NO_VECTORS_MESSAGE)


@pytest.mark.asyncio
async def test_raw_text_reaches_the_log_with_the_document_id(monkeypatch, caplog):
    document_id = str(uuid.uuid4())
    failure = ValueError(_RAW)
    mark_error = AsyncMock()
    monkeypatch.setattr(
        document_processor, "_process_document_impl", AsyncMock(side_effect=failure)
    )
    monkeypatch.setattr(document_processor, "_mark_document_error", mark_error)

    with caplog.at_level(logging.ERROR, logger=document_processor.logger.name):
        assert await document_processor.process_document(document_id) == 0

    mark_error.assert_awaited_once_with(document_id, failure)
    assert any(
        document_id in record.getMessage() and _RAW in record.getMessage()
        for record in caplog.records
    )
