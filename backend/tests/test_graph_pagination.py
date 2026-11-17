import logging

import pytest

from app.api.v1.graph_pagination import (
    _extract_error_detail,
    _is_top_not_allowed_error,
    paginate_graph_collection,
)


class MockGraphResponse:
    def __init__(self, status_code: int, payload: dict, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self) -> dict:
        return self._payload


class MockGraphClient:
    def __init__(self, responses: dict[str, MockGraphResponse]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict]] = []

    async def get(self, url: str, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses[url]


@pytest.mark.asyncio
async def test_paginate_graph_collection_supports_extra_first_page_params():
    client = MockGraphClient(
        {
            "https://graph.microsoft.com/v1.0/me/joinedTeams": MockGraphResponse(
                200, {"value": []}
            )
        }
    )

    await paginate_graph_collection(
        client,
        initial_url="https://graph.microsoft.com/v1.0/me/joinedTeams",
        headers={"Authorization": "Bearer token"},
        select_fields="id,displayName",
        top=100,
        extra_params={"$filter": "resourceProvisioningOptions/Any(x:x eq 'Team')"},
        logger=logging.getLogger("test"),
        error_prefix="Failed",
        truncate_prefix="Truncated",
    )

    params = client.calls[0][1]["params"]
    assert params["$top"] == 100
    assert params["$select"] == "id,displayName"
    assert params["$filter"] == "resourceProvisioningOptions/Any(x:x eq 'Team')"


@pytest.mark.asyncio
async def test_paginate_graph_collection_returns_empty_and_logs_on_error(caplog):
    client = MockGraphClient(
        {
            "https://graph.microsoft.com/v1.0/me/joinedTeams": MockGraphResponse(
                403, {}, "forbidden"
            )
        }
    )

    with caplog.at_level(logging.WARNING):
        items = await paginate_graph_collection(
            client,
            initial_url="https://graph.microsoft.com/v1.0/me/joinedTeams",
            headers={"Authorization": "Bearer token"},
            select_fields="id,displayName",
            top=100,
            logger=logging.getLogger("test"),
            error_prefix="Failed to list joined Teams",
            truncate_prefix="Teams listing",
        )

    assert items == []
    assert "Failed to list joined Teams (HTTP 403)" in caplog.text


# ── Additional contract coverage layered on top of the bot's two tests ───
#
# A sequenced fake client is needed for the multi-page assertions below
# (``MockGraphClient`` returns the same response for the same URL every
# time, which doesn't model Graph's per-call ``@odata.nextLink`` cursor
# semantics).  Keep it local so the bot's simpler dict-keyed fake stays
# the natural choice for single-page tests.


class _SequencedGraphClient:
    """Returns canned responses in FIFO order; records every ``get`` call.

    Models Graph's per-call cursor semantics — each request gets the
    *next* response in the queue, regardless of URL — so the helper's
    "first page carries our params, subsequent pages reuse the opaque
    ``@odata.nextLink`` with ``params=None``" contract can be asserted
    at the call boundary.
    """

    def __init__(self, responses: list[MockGraphResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    async def get(self, url: str, *, headers=None, params=None):
        self.calls.append({"url": url, "headers": headers, "params": params})
        return self._responses.pop(0)


@pytest.mark.asyncio
async def test_paginate_graph_collection_pins_first_page_only_param_flow():
    """The bot's existing extra-params test only inspects page 1.  Pin
    the *contract* the readability refactor was about: page 1 carries
    the hand-built params, and EVERY subsequent page carries
    ``params=None`` so we don't stomp the server-encoded
    ``@odata.nextLink`` cursor.  A future change that re-passes
    ``first_page_params`` on later requests fails this test loudly.
    """
    client = _SequencedGraphClient(
        responses=[
            MockGraphResponse(
                200,
                {
                    "value": [{"id": "p1"}],
                    "@odata.nextLink": "https://graph.microsoft.com/v1.0/teams?next=cursor-1",
                },
            ),
            MockGraphResponse(
                200,
                {
                    "value": [{"id": "p2"}],
                    "@odata.nextLink": "https://graph.microsoft.com/v1.0/teams?next=cursor-2",
                },
            ),
            MockGraphResponse(200, {"value": [{"id": "p3"}]}),
        ]
    )

    items = await paginate_graph_collection(
        client,  # type: ignore[arg-type]
        initial_url="https://graph.microsoft.com/v1.0/teams",
        headers={"Authorization": "Bearer x"},
        select_fields="id",
        top=50,
        logger=logging.getLogger(__name__),
        error_prefix="list teams",
        truncate_prefix="teams listing",
        extra_params={"$filter": "displayName eq 'Engineering'"},
    )

    assert [it["id"] for it in items] == ["p1", "p2", "p3"]

    # Page 1: $select + $top + the caller's $filter.
    assert client.calls[0]["params"] == {
        "$select": "id",
        "$top": 50,
        "$filter": "displayName eq 'Engineering'",
    }
    # Pages 2 + 3: nextLink already encodes the cursor + original
    # query parameters server-side; passing ``params=`` here would
    # either be ignored or silently override the cursor.
    assert client.calls[1]["params"] is None
    assert client.calls[2]["params"] is None


@pytest.mark.asyncio
async def test_paginate_graph_collection_no_extra_params_keeps_select_top_only():
    """Pin the no-regression invariant for the existing two callers
    (`integration_elements.py`, `channel_bots.py`) which don't pass
    ``extra_params``: the first-page params dict must still contain
    *exactly* ``$select`` + ``$top``, nothing else.
    """
    client = _SequencedGraphClient(
        responses=[MockGraphResponse(200, {"value": [{"id": "only"}]})]
    )

    await paginate_graph_collection(
        client,  # type: ignore[arg-type]
        initial_url="https://graph.microsoft.com/v1.0/teams",
        headers={"Authorization": "Bearer x"},
        select_fields="id",
        top=10,
        logger=logging.getLogger(__name__),
        error_prefix="list teams",
        truncate_prefix="teams listing",
    )

    assert set(client.calls[0]["params"].keys()) == {"$select", "$top"}


@pytest.mark.asyncio
async def test_paginate_graph_collection_handles_unparseable_error_body(caplog):
    """When the error body isn't valid JSON the helper's
    ``try/except`` falls back to ``resp.text[:300]`` — covers the
    reverse-proxy / gateway HTML-body case.  Without this branch
    being exercised, a future refactor that drops the ``except`` would
    silently start raising ``ValueError`` out of the helper and turn
    a degraded picker into a 5xx for the whole settings page.
    """

    class _BadJsonResponse:
        status_code = 502
        text = "<html><body>Bad Gateway</body></html>" + "x" * 1000

        def json(self):
            raise ValueError("not json")

    client = _SequencedGraphClient(responses=[_BadJsonResponse()])  # type: ignore[arg-type]

    with caplog.at_level(logging.WARNING):
        items = await paginate_graph_collection(
            client,  # type: ignore[arg-type]
            initial_url="https://graph.microsoft.com/v1.0/teams",
            headers={"Authorization": "Bearer x"},
            select_fields="id",
            top=10,
            logger=logging.getLogger("test"),
            error_prefix="list teams",
            truncate_prefix="teams listing",
        )

    assert items == []
    assert len(client.calls) == 1
    msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("HTTP 502" in m for m in msgs), msgs
    assert any("Bad Gateway" in m for m in msgs), msgs


@pytest.mark.asyncio
async def test_paginate_graph_collection_truncates_at_max_pages(caplog):
    """``max_pages`` must cap the loop and emit a structured warning
    so operators know the listing was truncated (they may need to
    raise the cap or add ``extra_params={"$filter": ...}`` to narrow
    results).  Pin both behaviours so a future refactor can't silently
    lift the cap or drop the warning.
    """
    client = _SequencedGraphClient(
        responses=[
            MockGraphResponse(
                200,
                {
                    "value": [{"id": "p1"}],
                    "@odata.nextLink": "https://graph.microsoft.com/v1.0/teams?p=2",
                },
            ),
            MockGraphResponse(
                200,
                {
                    "value": [{"id": "p2"}],
                    "@odata.nextLink": "https://graph.microsoft.com/v1.0/teams?p=3",
                },
            ),
            MockGraphResponse(200, {"value": [{"id": "p3"}]}),
        ]
    )

    with caplog.at_level(logging.WARNING):
        items = await paginate_graph_collection(
            client,  # type: ignore[arg-type]
            initial_url="https://graph.microsoft.com/v1.0/teams",
            headers={"Authorization": "Bearer x"},
            select_fields="id",
            top=10,
            max_pages=2,
            logger=logging.getLogger("test"),
            error_prefix="list teams",
            truncate_prefix="teams listing",
        )

    assert [it["id"] for it in items] == ["p1", "p2"]
    assert len(client.calls) == 2
    msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("teams listing truncated after 2 pages" in m for m in msgs), msgs


# ── $top-not-allowed self-healing ────────────────────────


@pytest.mark.parametrize(
    "detail,expected",
    [
        ("Query option 'Top' is not allowed.", True),
        ("query option 'top' is not allowed", True),  # case-insensitive
        (
            "The query specified in the URI is not valid. "
            "Query option 'Top' is not allowed.",
            True,
        ),
        ("Insufficient privileges to complete the operation.", False),
        ("", False),
        (None, False),  # defensive: None detail must not raise
    ],
)
def test_is_top_not_allowed_error_detects_graph_message(detail, expected):
    assert _is_top_not_allowed_error(detail) is expected


def test_extract_error_detail_always_returns_str():
    """``error.message`` can be an explicit null or a non-string value.
    ``_extract_error_detail`` MUST always return a ``str`` so downstream
    logging and ``_is_top_not_allowed_error`` never operate on a
    non-string.
    """
    # Explicit null message → fall back to the truncated raw body.
    null_msg = MockGraphResponse(
        400, {"error": {"message": None}}, text="raw body"
    )
    assert _extract_error_detail(null_msg) == "raw body"
    # Absent message key → same fallback.
    no_msg = MockGraphResponse(400, {"error": {}}, text="raw body")
    assert _extract_error_detail(no_msg) == "raw body"
    # Non-string message → coerced to str (never returned bare).
    int_msg = MockGraphResponse(400, {"error": {"message": 123}}, text="raw")
    detail = _extract_error_detail(int_msg)
    assert isinstance(detail, str) and detail == "123"
    # Normal string message → returned unchanged and still routed by the
    # $top matcher.
    str_msg = MockGraphResponse(
        400, {"error": {"message": "Query option 'Top' is not allowed."}}
    )
    extracted = _extract_error_detail(str_msg)
    assert extracted == "Query option 'Top' is not allowed."
    assert _is_top_not_allowed_error(extracted) is True


@pytest.mark.asyncio
async def test_paginate_graph_collection_retries_without_top_on_400(caplog):
    """The /teams/{id}/channels endpoint rejects $top with HTTP 400.
    The helper MUST transparently retry the first page
    without $top and return the items — instead of logging a warning
    and returning an empty list (the silent breakage).
    """
    client = _SequencedGraphClient(
        responses=[
            MockGraphResponse(
                400,
                {
                    "error": {
                        "message": (
                            "The query specified in the URI is not valid. "
                            "Query option 'Top' is not allowed."
                        )
                    }
                },
                text="bad request",
            ),
            MockGraphResponse(200, {"value": [{"id": "general"}, {"id": "ops"}]}),
        ]
    )

    with caplog.at_level(logging.INFO):
        items = await paginate_graph_collection(
            client,  # type: ignore[arg-type]
            initial_url="https://graph.microsoft.com/v1.0/teams/t1/channels",
            headers={"Authorization": "Bearer x"},
            select_fields="id,displayName",
            top=200,
            logger=logging.getLogger("test"),
            error_prefix="Failed to list channels",
            truncate_prefix="channel listing",
        )

    # Listing succeeded via the retry.
    assert [it["id"] for it in items] == ["general", "ops"]
    # Exactly two requests: the rejected first attempt (with $top) and
    # the successful retry (without $top).
    assert len(client.calls) == 2
    assert client.calls[0]["params"]["$top"] == 200
    assert "$top" not in client.calls[1]["params"]
    # The retry decision is observable.
    assert any(
        "retrying without it" in r.getMessage()
        for r in caplog.records
        if r.levelno == logging.INFO
    )


@pytest.mark.asyncio
async def test_paginate_graph_collection_does_not_retry_other_400(caplog):
    """A 400 that is NOT the $top-not-allowed error must keep the
    existing behaviour: log a warning and return [] with no retry — we
    only want to self-heal the specific, well-understood $top rejection.
    """
    client = _SequencedGraphClient(
        responses=[
            MockGraphResponse(
                400,
                {"error": {"message": "Invalid filter clause."}},
                text="bad request",
            ),
        ]
    )

    with caplog.at_level(logging.WARNING):
        items = await paginate_graph_collection(
            client,  # type: ignore[arg-type]
            initial_url="https://graph.microsoft.com/v1.0/teams/t1/channels",
            headers={"Authorization": "Bearer x"},
            select_fields="id,displayName",
            top=200,
            logger=logging.getLogger("test"),
            error_prefix="Failed to list channels",
            truncate_prefix="channel listing",
        )

    assert items == []
    assert len(client.calls) == 1  # no retry
    msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("HTTP 400" in m and "Invalid filter clause" in m for m in msgs), msgs
