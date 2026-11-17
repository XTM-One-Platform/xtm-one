"""Global search registry and term parsing (#11201), no database."""

from __future__ import annotations

import typing
from datetime import UTC, datetime

from app.api.v1.search import SearchType, search
from app.services import global_search


def test_every_source_is_a_route_type_and_back() -> None:
    assert set(typing.get_args(SearchType)) == set(global_search.SEARCH_TYPES)
    assert len(global_search.SEARCH_TYPES) == len(global_search.SOURCES)


def test_words_are_distinct_lower_case_and_bounded() -> None:
    assert global_search.search_words("  Zephyr  zephyr TRIAGE ") == ["zephyr", "triage"]
    assert global_search.search_words("") == []
    many = " ".join(f"w{n}" for n in range(20))
    assert len(global_search.search_words(many)) == global_search.MAX_WORDS


def test_like_wildcards_are_escaped() -> None:
    assert global_search.escape_like("50%_a\\b") == "50\\%\\_a\\\\b"


def test_the_route_offers_every_sort_the_service_knows() -> None:
    sort_by = typing.get_type_hints(search)["sort_by"]
    assert set(typing.get_args(sort_by)) == set(global_search.SORTS)


def _hit(id_: str, name: str, rank: int, day: int | None) -> dict[str, object]:
    changed = datetime(2026, 11, day, tzinfo=UTC) if day else None
    return {"type": "agent", "id": id_, "name": name, "rank": rank, "updated_at": changed}


def _merged(sort: str, descending: bool) -> list[str]:
    items = [_hit("a", "beta", 2, 3), _hit("b", "Alpha", 1, None), _hit("c", "gamma", 0, 1), _hit("d", "alpha", 2, 5)]
    return [str(item["id"]) for item in global_search._merge(items, sort, descending)]


def test_the_merged_page_follows_the_chosen_sort() -> None:
    assert _merged("rank", False) == ["c", "b", "d", "a"]
    assert _merged("name", False) == ["b", "d", "a", "c"]
    assert _merged("name", True) == ["c", "a", "b", "d"]
    assert _merged("updated", True) == ["d", "a", "c", "b"]
    assert _merged("updated", False) == ["c", "a", "d", "b"]
