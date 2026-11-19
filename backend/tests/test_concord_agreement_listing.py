"""Concord ``list_concord_agreements``: status, search and paging parameters.

The fake API follows ``ListAgreements`` of the Concord OpenAPI: ``statuses``
is a repeated query parameter, ``page`` is zero-based and the answer is an
``AgreementListDto`` (``items``, ``total``, ``page``). Nothing reaches Concord.
"""

from __future__ import annotations

import httpx
import pytest

from app.integrations.providers import concord
from app.services.integration_tools import _SafeIntegrationExecutor
from tests.integration_file_fakes import call, fake_api

_LIST = "/api/rest/1/user/me/organizations/12/agreements"

#: One ordering of agreements per stage, as the API would page it.
_LIBRARY = {
    "CURRENT_CONTRACT": [f"Current {i:02d}" for i in range(3)],
    "COMPLETED_CONTRACT": [f"Completed {i:02d}" for i in range(23)],
    "TRASHED": ["Old trash"],
}


def _executor():
    executor = _SafeIntegrationExecutor()
    concord.build_tools("12||concord-key", executor)
    return executor


def _api(request: httpx.Request) -> httpx.Response:
    assert request.url.path == _LIST
    query = request.url.params
    stages = query.get_list("statuses")
    if not stages:
        return httpx.Response(400, json={"message": "statuses is required"})
    search = query.get("search", "").lower()
    rows = [
        {"uuid": f"{stage}-{n}", "title": title, "status": stage}
        for stage in stages
        for n, title in enumerate(_LIBRARY.get(stage, []))
        if search in title.lower()
    ]
    page, size = int(query["page"]), int(query["numberOfItemsByPage"])
    window = rows[page * size : (page + 1) * size]
    return httpx.Response(
        200,
        json={
            "items": window,
            "numberOfItems": len(window),
            "numberOfItemsByPage": size,
            "page": page,
            "total": len(rows),
            "totalInvitations": 0,
        },
    )


def _titles(content: str) -> list[str]:
    return [line.split("**")[1] for line in content.splitlines() if line.startswith("**")]


async def test_statuses_go_out_as_a_repeated_parameter_and_the_page_zero_based():
    with fake_api(_api) as sent:
        result = await call(
            _executor(),
            "list_concord_agreements",
            organization_id=12,
            statuses="CURRENT_CONTRACT, completed_contract",
            page=1,
            page_size=5,
        )
    assert not result.is_error, result.content
    (request,) = sent
    assert request.url.params.get_list("statuses") == ["CURRENT_CONTRACT", "COMPLETED_CONTRACT"]
    assert request.url.params["page"] == "0"
    assert request.url.params["numberOfItemsByPage"] == "5"
    titles = _titles(result.content)
    assert titles[:3] == ["Current 00", "Current 01", "Current 02"]
    assert titles[3:] == ["Completed 00", "Completed 01"]
    assert "Page 1 of 6 (26 agreements match, 5 per page)" in result.content


async def test_no_status_lists_every_stage_but_the_trash():
    with fake_api(_api) as sent:
        result = await call(_executor(), "list_concord_agreements", organization_id=12, page_size=50)
    stages = sent[0].url.params.get_list("statuses")
    assert "TRASHED" not in stages
    assert {"CURRENT_CONTRACT", "COMPLETED_CONTRACT", "DRAFT", "TEMPLATE"} <= set(stages)
    assert len(_titles(result.content)) == 26
    assert "Old trash" not in result.content


@pytest.mark.parametrize("size", [5, 10, 20, 50])
async def test_page_one_is_the_same_prefix_at_every_page_size(size):
    with fake_api(_api):
        result = await call(
            _executor(),
            "list_concord_agreements",
            organization_id=12,
            statuses="COMPLETED_CONTRACT",
            page_size=size,
        )
    assert _titles(result.content) == _LIBRARY["COMPLETED_CONTRACT"][:size]


async def test_pages_walk_the_whole_status_once():
    seen: list[str] = []
    with fake_api(_api):
        for page in range(1, 4):
            result = await call(
                _executor(),
                "list_concord_agreements",
                organization_id=12,
                statuses="COMPLETED_CONTRACT",
                page=page,
                page_size=10,
            )
            seen += _titles(result.content)
    assert seen == _LIBRARY["COMPLETED_CONTRACT"]


async def test_a_title_seen_in_a_listing_is_found_by_search():
    with fake_api(_api) as sent:
        result = await call(
            _executor(),
            "list_concord_agreements",
            organization_id=12,
            statuses="COMPLETED_CONTRACT",
            search="Completed 07",
        )
    assert sent[0].url.params["search"] == "Completed 07"
    assert sent[0].url.params["page"] == "0"
    assert _titles(result.content) == ["Completed 07"]


async def test_an_unknown_status_is_refused_with_the_valid_ones_before_any_call():
    with fake_api(_api) as sent:
        result = await call(_executor(), "list_concord_agreements", organization_id=12, statuses="INBOX")
    assert sent == []
    assert "unknown Concord agreement status INBOX" in result.content
    assert "COMPLETED_CONTRACT_RENEWABLE" in result.content


async def test_a_page_past_the_end_says_how_many_pages_exist():
    with fake_api(_api):
        result = await call(
            _executor(),
            "list_concord_agreements",
            organization_id=12,
            statuses="CURRENT_CONTRACT",
            page=4,
            page_size=5,
        )
    assert result.content == "No agreements on page 4: 3 match, 1 page(s) of 5."


async def test_an_unexpected_answer_is_an_error_never_an_empty_list():
    def odd(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"result": "success"})

    with fake_api(odd):
        result = await call(_executor(), "list_concord_agreements", organization_id=12)
    assert "No agreements found" not in result.content
    assert "without an items list" in result.content


async def test_an_api_error_is_surfaced():
    def refused(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"message": "typeMismatch"})

    with fake_api(refused):
        result = await call(_executor(), "list_concord_agreements", organization_id=12)
    assert "No agreements found" not in result.content
    assert "Concord" in result.content
