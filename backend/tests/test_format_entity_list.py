"""A listing header names its noun in correct English and never reads as the whole.

``format_entity_list`` heads every integration's list and search answers: an
``entity`` is counted as ``entities`` (not ``entitys``), and a page of a larger
result says how many it shows, so an agent that asked for two of 30,145
matches does not take the two for the whole set.
"""

from __future__ import annotations

import pytest

from app.integrations.http import format_entity_list, plural_noun


@pytest.mark.parametrize(
    ("noun", "expected"),
    [
        ("entity", "entities"),
        ("KEV entry", "KEV entries"),
        ("vulnerability", "vulnerabilities"),
        ("key", "keys"),
        ("survey", "surveys"),
        ("address", "addresses"),
        ("review status", "review statuses"),
        ("vector search index", "vector search indexes"),
        ("IoC match", "IoC matches"),
        ("correlation search", "correlation searches"),
        ("domain object", "domain objects"),
        ("CVE", "CVEs"),
        ("", ""),
    ],
)
def test_plural_noun(noun: str, expected: str) -> None:
    assert plural_noun(noun) == expected


def _fmt(item: dict) -> str:
    return f"- {item['name']}"


def test_a_page_of_a_larger_result_says_how_many_it_shows() -> None:
    text = format_entity_list([{"name": "a"}, {"name": "b"}], _fmt, noun="entity", total=30145)
    assert text.splitlines()[0] == "Found 30145 entities, showing 2:"


@pytest.mark.parametrize(
    ("items", "total", "header"),
    [
        ([{"name": "a"}], None, "Found 1 entity:"),
        ([{"name": "a"}, {"name": "b"}], None, "Found 2 entities:"),
        ([{"name": "a"}, {"name": "b"}], 2, "Found 2 entities:"),
    ],
)
def test_a_whole_result_names_only_its_count(items: list[dict], total: int | None, header: str) -> None:
    assert format_entity_list(items, _fmt, noun="entity", total=total).splitlines()[0] == header


def test_the_empty_answer_uses_the_plural() -> None:
    assert format_entity_list([], _fmt, noun="entry") == "No entries found."


def test_a_caller_spells_a_plural_that_is_not_on_the_last_word() -> None:
    items = [{"name": "a"}, {"name": "b"}]
    text = format_entity_list(items, _fmt, noun="group with IOCs", plural="groups with IOCs")
    assert text.splitlines()[0] == "Found 2 groups with IOCs:"
    assert format_entity_list(items[:1], _fmt, noun="group with IOCs", plural="groups with IOCs").startswith(
        "Found 1 group with IOCs:"
    )
