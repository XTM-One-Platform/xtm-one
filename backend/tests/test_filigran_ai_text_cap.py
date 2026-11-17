"""Regression tests for the ``extract_ttp`` text-input cap and its
defensive guards.

The cap exists because an LLM that hallucinates a multi-megabyte
``text`` argument would otherwise be uploaded as a multipart form
field with a 120s timeout, holding worker memory and bandwidth.  The
tests below pin:

* The cap value is enforced server-side (not just announced in the
  schema).
* Oversized input is truncated AND emits a WARNING — silent
  truncation would be a worse failure mode than the original bug.
* The cap is also published in the tool's ``input_schema``
  (``maxLength``) so a well-behaved LLM stays under it without needing
  the truncation to fire.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import pytest

import app.integrations.providers.filigran_ai as filigran_ai
from app.tools.base import CallableToolExecutor, ToolCall


class _FakeHttpxResponse:
    def __init__(self, *, status_code: int = 200, text: str = "{}"):
        self.status_code = status_code
        self.text = text

    @property
    def is_success(self) -> bool:
        return 200 <= self.status_code < 300


class _CapturingHttpxClient:
    def __init__(self):
        self.posts: list[dict[str, Any]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _tb):
        return False

    async def post(self, url: str, **kwargs):
        self.posts.append({"url": url, **kwargs})
        return _FakeHttpxResponse(status_code=200, text='{"ttps": []}')


def _patched_async_client(captured: list[_CapturingHttpxClient]):
    """Return a stub for ``httpx.AsyncClient`` that records every POST."""

    def _factory(*_args, **_kwargs):
        client = _CapturingHttpxClient()
        captured.append(client)
        return client

    return _factory


def _extract_multipart_field(files_payload, field_name: str) -> str | None:
    """Extract the value of a multipart form field from an httpx
    ``files=`` payload.

    ``extract_ttp`` sends all data — including the plain ``text`` form
    field — through the ``files=`` channel using the
    ``(filename, content, content_type)`` tuple convention with
    ``filename=None`` to mark form fields (vs. file uploads).  This
    helper isolates the ``field_name`` lookup so the assertions
    elsewhere stay readable.
    """
    if not files_payload:
        return None
    for name, value_tuple in files_payload:
        if name != field_name:
            continue
        # ``value_tuple`` is ``(filename, content, content_type)`` for
        # form fields (filename=None) and file uploads alike.
        if len(value_tuple) >= 2:
            return value_tuple[1]
    return None


@pytest.fixture
def _stubbed_extract_ttp(monkeypatch):
    """Wire up the minimum stubs needed to call ``extract_ttp``
    end-to-end: a licence PEM resolution and a base_url_ttp config.
    """

    async def _resolve_pem_stub():
        return SimpleNamespace(header="X-XTMOne-Certificate", pem="dummy-pem")

    monkeypatch.setattr(filigran_ai, "_resolve_pem", _resolve_pem_stub)

    captured: list[_CapturingHttpxClient] = []
    monkeypatch.setattr(filigran_ai.httpx, "AsyncClient", _patched_async_client(captured))

    executor = CallableToolExecutor()
    # ``build_tools`` registers the three Filigran AI tools against the
    # ``cfg`` dict built from the (here fake) integration token.
    filigran_ai.build_tools(
        token="https://stix.example.com||https://ttp.example.com",
        executor=executor,
    )
    return executor, captured


@pytest.mark.asyncio
async def test_extract_ttp_truncates_oversized_text(
    _stubbed_extract_ttp, caplog: pytest.LogCaptureFixture
):
    """An LLM-supplied ``text`` argument larger than the cap must be
    truncated to exactly the cap AND a WARNING must be emitted so
    operators see the loss-of-information event in logs.

    Otherwise an LLM could (a) hold the worker's 120s HTTP timeout
    window with a megabyte-scale payload, and (b) saturate outbound
    bandwidth.
    """
    executor, captured_clients = _stubbed_extract_ttp

    oversized = "x" * (filigran_ai._TEXT_INPUT_MAX_CHARS + 10_000)

    with caplog.at_level(logging.WARNING, logger=filigran_ai.logger.name):
        result = await executor.execute_tool(
            ToolCall(
                id="call-oversized",
                name="extract_ttp",
                arguments={"text": oversized},
            )
        )

    assert not result.is_error, (
        "the tool must return cleanly with the (truncated) result, NOT "
        "raise; truncation is a soft-cap"
    )

    # Verify the HTTP call was issued with truncated text.  ``text``
    # rides through ``files=`` (not ``data=``) so the upstream sees a
    # multipart body even in the text-only path — see the comment in
    # ``extract_ttp`` for the FastAPI/Form/File rationale.
    assert len(captured_clients) == 1
    posts = captured_clients[0].posts
    assert len(posts) == 1, f"expected exactly one POST; observed {posts!r}"
    sent_text = _extract_multipart_field(posts[0].get("files"), "text")
    assert sent_text is not None, (
        f"expected a multipart ``text`` field in the POST; observed "
        f"files={posts[0].get('files')!r}"
    )
    # The oversized input has no whitespace ("x" * N) so the
    # whitespace-aware truncation falls back to the hard cut and the
    # truncated length equals the cap exactly.
    assert len(sent_text) == filigran_ai._TEXT_INPUT_MAX_CHARS, (
        f"oversized whitespace-free text must be truncated to exactly "
        f"the cap ({filigran_ai._TEXT_INPUT_MAX_CHARS}); observed "
        f"length {len(sent_text)}"
    )

    # Verify the WARNING was emitted with both the original and capped sizes.
    truncation_warnings = [
        r
        for r in caplog.records
        if r.levelname == "WARNING" and "truncated" in r.getMessage()
    ]
    assert truncation_warnings, (
        "extract_ttp must emit a WARNING when truncating oversized text "
        "input; otherwise an LLM hallucination of a multi-MB text "
        "would silently lose data.  Observed records: "
        f"{[r.getMessage() for r in caplog.records]}"
    )
    assert any(
        str(filigran_ai._TEXT_INPUT_MAX_CHARS) in r.getMessage()
        for r in truncation_warnings
    ), (
        f"the truncation WARNING should include the cap value "
        f"({filigran_ai._TEXT_INPUT_MAX_CHARS}) so operators can "
        f"correlate it with the configured limit"
    )


@pytest.mark.asyncio
async def test_extract_ttp_below_cap_passes_through_unchanged(
    _stubbed_extract_ttp, caplog: pytest.LogCaptureFixture
):
    """A normal-sized input (well under the cap) must be sent
    unchanged AND must NOT trigger the truncation WARNING.  Silent
    over-aggressive truncation would hurt analyst productivity.
    """
    executor, captured_clients = _stubbed_extract_ttp

    normal_text = "APT-29 used PowerShell to exfiltrate data via DNS."

    with caplog.at_level(logging.WARNING, logger=filigran_ai.logger.name):
        result = await executor.execute_tool(
            ToolCall(
                id="call-normal",
                name="extract_ttp",
                arguments={"text": normal_text},
            )
        )

    assert not result.is_error
    posts = captured_clients[0].posts
    sent_text = _extract_multipart_field(posts[0].get("files"), "text")
    assert sent_text == normal_text, (
        "short text must round-trip unchanged through the cap guard"
    )

    truncation_warnings = [
        r
        for r in caplog.records
        if "truncated" in r.getMessage().lower()
    ]
    assert not truncation_warnings, (
        f"no truncation WARNING should fire for input below the cap; "
        f"observed {[r.getMessage() for r in truncation_warnings]}"
    )


@pytest.mark.asyncio
async def test_extract_ttp_strips_whitespace_exactly_once(_stubbed_extract_ttp):
    """``text.strip()`` is computed once and reused.  This locks the
    contract that the form-data ``text`` field is the trimmed value
    even when the input had leading/trailing whitespace.

    A regression that drops the strip would send the un-trimmed text
    upstream and a regression that double-strips (the pre-fix
    behaviour) would be slower for large inputs but observationally
    identical — so we focus on the contract: the value reaching the
    API is trimmed, ONCE.
    """
    executor, captured_clients = _stubbed_extract_ttp

    text_with_padding = "   APT actor description with padding   \n"

    result = await executor.execute_tool(
        ToolCall(
            id="call-padded",
            name="extract_ttp",
            arguments={"text": text_with_padding},
        )
    )

    assert not result.is_error
    sent = _extract_multipart_field(
        captured_clients[0].posts[0].get("files"), "text"
    )
    assert sent == text_with_padding.strip(), (
        f"text must be stripped before upload; got {sent!r}"
    )
    # And the original padding must not have leaked through.
    assert not sent.startswith(" ")
    assert not sent.endswith("\n")


@pytest.mark.asyncio
async def test_extract_ttp_input_schema_announces_cap():
    """The ``input_schema`` must advertise the cap as ``maxLength``
    so a well-behaved LLM reading the spec stays under it without
    needing the server-side truncation to fire.

    Without this, the only signal the LLM has about the cap is the
    description text — which it may not respect for very-long
    threat reports.
    """
    executor = CallableToolExecutor()
    tools = filigran_ai.build_tools(
        token="https://stix.example.com||https://ttp.example.com",
        executor=executor,
    )

    extract_ttp_tool = next(t for t in tools if t.name == "extract_ttp")
    text_schema = extract_ttp_tool.input_schema["properties"]["text"]

    assert text_schema.get("maxLength") == filigran_ai._TEXT_INPUT_MAX_CHARS, (
        f"extract_ttp 'text' input_schema must declare maxLength="
        f"{filigran_ai._TEXT_INPUT_MAX_CHARS}; got {text_schema!r}"
    )
    # The description should also mention the cap so an LLM reading
    # the tool spec at a different layer (e.g. through a chat
    # framework that ignores maxLength) still sees the constraint.
    assert (
        str(filigran_ai._TEXT_INPUT_MAX_CHARS) in text_schema.get("description", "")
        or "capped" in text_schema.get("description", "").lower()
    ), (
        f"extract_ttp 'text' input_schema description should mention "
        f"the cap for LLMs that don't honour maxLength; got "
        f"{text_schema.get('description')!r}"
    )


@pytest.mark.asyncio
async def test_extract_stix_and_entities_relations_remain_file_only():
    """Documenting the intentional asymmetry: ``extract_stix`` and
    ``extract_entities_relations`` do NOT accept a ``text`` argument.

    This was raised in the PR review as a question — the answer is
    'by design, those endpoints' upstream API expects a multipart
    file body with no text form field'.  Locking the schema here
    prevents accidental drift if a future contributor copy-pastes
    the ``extract_ttp`` signature into the siblings without
    confirming the upstream API.

    A future change that genuinely needs text support for the STIX
    siblings should update this test in the same commit as the
    schema/handler change, documenting the upstream coordination.
    """
    executor = CallableToolExecutor()
    tools = filigran_ai.build_tools(
        token="https://stix.example.com||https://ttp.example.com",
        executor=executor,
    )

    for tool_name in ("extract_stix", "extract_entities_relations"):
        tool = next(t for t in tools if t.name == tool_name)
        props = tool.input_schema.get("properties", {})
        assert "text" not in props, (
            f"{tool_name} must NOT accept a 'text' argument — it is a "
            f"file-only tool by design (upstream API expects multipart "
            f"with no text field).  If this changed, the upstream "
            f"coordination should be documented in the same commit as "
            f"the schema update.  Observed input_schema properties: "
            f"{sorted(props)}"
        )


@pytest.mark.asyncio
async def test_extract_ttp_text_only_path_sends_multipart_not_urlencoded(
    _stubbed_extract_ttp,
):
    """When only ``text`` is provided (no files), the upstream POST
    must still be issued as ``multipart/form-data`` — not
    ``application/x-www-form-urlencoded``.

    A FastAPI route declared with ``Form(text)`` + ``File(files)``
    expects multipart on every call; previously the text-only path
    used ``data={"text": ...}, files=None`` which httpx encodes as
    urlencoded, and the upstream would reject the request with a
    400/422 even though it's documented as a supported invocation
    mode.  The fix routes ``text`` through ``files=`` with the
    ``(filename=None, value, content_type)`` form-field convention,
    forcing multipart regardless of attachment presence.

    This test locks the multipart contract: the POST must pass
    ``files=`` (non-empty), must NOT pass a non-empty ``data=``
    kwarg, and the ``text`` field must appear inside the multipart
    body.
    """
    executor, captured_clients = _stubbed_extract_ttp

    result = await executor.execute_tool(
        ToolCall(
            id="call-text-only",
            name="extract_ttp",
            arguments={"text": "APT-29 used PowerShell to exfiltrate."},
        )
    )

    assert not result.is_error
    assert len(captured_clients) == 1
    posts = captured_clients[0].posts
    assert len(posts) == 1, f"expected exactly one POST; observed {posts!r}"

    kwargs = posts[0]
    files_kwarg = kwargs.get("files")
    data_kwarg = kwargs.get("data")

    # Multipart precondition: ``files=`` non-empty.
    assert files_kwarg, (
        f"text-only path must pass a non-empty ``files=`` so httpx "
        f"emits a multipart body; observed files={files_kwarg!r}"
    )
    # Negative precondition: ``data=`` must NOT be the carrier — if
    # ``data`` were non-empty and ``files`` empty, httpx would fall
    # back to urlencoded encoding and the upstream multipart route
    # would 400/422.
    assert not data_kwarg, (
        f"text-only path must not rely on ``data=`` (would trigger "
        f"urlencoded encoding); observed data={data_kwarg!r}"
    )

    # The ``text`` form field must actually appear in the multipart
    # body with the analyst-supplied value.
    sent_text = _extract_multipart_field(files_kwarg, "text")
    assert sent_text == "APT-29 used PowerShell to exfiltrate.", (
        f"text field must round-trip through multipart unchanged; "
        f"observed {sent_text!r}"
    )


@pytest.mark.asyncio
async def test_extract_ttp_truncates_at_last_whitespace_when_available(
    _stubbed_extract_ttp,
):
    """When the oversized input contains whitespace near the cap, the
    truncation should land on the last whitespace boundary (not slice
    mid-word).

    Slicing mid-word would feed the upstream tokenizer a partial
    word at the end of the prompt, which a future contributor might
    misread as a corrupted token.  For analyst-written prose (which
    is what this argument carries) whitespace is reliably present,
    so the boundary-aware truncation produces a cleaner cut.

    The hard-cut fallback for whitespace-free input is exercised by
    ``test_extract_ttp_truncates_oversized_text`` above.
    """
    executor, captured_clients = _stubbed_extract_ttp

    cap = filigran_ai._TEXT_INPUT_MAX_CHARS
    # Build "word word word ..." padded just past the cap.  Each
    # "word " is 5 chars, so cap+5 chars guarantees one boundary
    # in the cap window.
    prefix = "word " * (cap // 5)
    oversized = prefix + "trailing"
    assert len(oversized) > cap

    result = await executor.execute_tool(
        ToolCall(
            id="call-ws-trunc",
            name="extract_ttp",
            arguments={"text": oversized},
        )
    )

    assert not result.is_error
    sent_text = _extract_multipart_field(
        captured_clients[0].posts[0].get("files"), "text"
    )
    assert sent_text is not None
    # Truncated content stays under or at the cap...
    assert len(sent_text) <= cap, (
        f"truncated text must not exceed the cap ({cap}); observed "
        f"length {len(sent_text)}"
    )
    # ... and ends on a whitespace boundary (no partial 'word').
    assert not sent_text.endswith("wor"), (
        f"truncation should land on a whitespace boundary, not "
        f"slice mid-word; observed tail {sent_text[-20:]!r}"
    )
    assert not sent_text.endswith("wo"), (
        f"truncation should land on a whitespace boundary; observed "
        f"tail {sent_text[-20:]!r}"
    )
