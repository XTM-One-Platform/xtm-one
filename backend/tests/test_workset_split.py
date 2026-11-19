"""``workset_split``: one tool result cut into workset items, the content exact.

The patterns come from the model and run on third-party text, so every split
runs on the bounded regex engine within one time budget, and every cap refuses
rather than cuts: a half corpus that looks whole is what ``to_workset`` exists
to prevent.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime

import pytest

from app.services import workset_split
from app.services.workset_split import SplitError, SplitSpec, parse_date, split_result

_CASES = """Page 1 of 662 (19832 cases)

## Case 101: Biolabs in Ukraine
Date: 10/6/2026
Link: https://example.test/cases/101
The outlet claims that "laboratories" exist.

## Case 102: Elections rigged
Date: 3/12/2026
Link: https://example.test/cases/102
The post asserts that the vote was rigged.
"""


def _spec(**fields) -> SplitSpec:
    return SplitSpec.from_argument(fields)


class _FrozenClock:
    """A ``time`` stand-in whose monotonic clock never moves."""

    @staticmethod
    def monotonic() -> float:
        return 1000.0


@pytest.fixture(autouse=True)
def _frozen_budget_clock(request, monkeypatch):
    """Only ``TestBounds`` measures the time budget, on the real clock.

    Every other test splits a few hundred characters with trivial patterns: a
    stalled xdist worker (GC, CPU contention) must not spend their budget, so
    the module clock is frozen and ``_Budget.remaining()`` answers the whole
    budget. The budget itself is never loosened.
    """
    if request.cls is not None and request.cls.__name__ == "TestBounds":
        return
    monkeypatch.setattr(workset_split, "time", _FrozenClock)


class TestTextSplit:
    def test_items_keep_their_exact_text_and_fields(self):
        result = split_result(
            _CASES,
            _spec(
                item_start=r"^## Case \d+:",
                title_pattern=r"^## (Case \d+: .+)$",
                ref_pattern=r"^## Case (\d+):",
                date_pattern=r"^Date: (\S+)",
                date_order="MDY",
            ),
        )
        assert [item.title for item in result.items] == ["Case 101: Biolabs in Ukraine", "Case 102: Elections rigged"]
        assert [item.external_ref for item in result.items] == ["101", "102"]
        assert [item.occurred_at for item in result.items] == [
            datetime(2026, 10, 6, tzinfo=UTC),
            datetime(2026, 3, 12, tzinfo=UTC),
        ]
        assert [item.source_url for item in result.items] == [
            "https://example.test/cases/101",
            "https://example.test/cases/102",
        ]
        first = _CASES.index("## Case 101")
        second = _CASES.index("## Case 102")
        assert result.items[0].content == _CASES[first:second].strip()
        assert result.items[1].content == _CASES[second:].strip()
        assert result.items[0].ref_is_digest is False

    def test_the_preamble_is_counted_and_kept_for_the_receipt(self):
        result = split_result(_CASES, _spec(item_start=r"^## Case"))
        assert result.preamble == "Page 1 of 662 (19832 cases)"
        assert result.preamble_chars == len("Page 1 of 662 (19832 cases)")

    def test_an_item_start_that_matches_nothing_stores_the_result_whole(self) -> None:
        # The tool ran: storing nothing would waste the call (partner reports
        # on text results). One item, the exact text, and the receipt says why.
        result = split_result(_CASES, _spec(item_start=r"^### Article", ref_pattern=r"^## Case (\d+):"))
        assert [item.content for item in result.items] == [_CASES.strip()]
        assert result.items[0].external_ref == "101"
        assert result.preamble_chars == 0
        assert "item_start matched no line" in result.fallback
        assert "stored whole as one item" in result.fallback

    def test_a_split_that_applies_has_no_fallback(self) -> None:
        assert split_result(_CASES, _spec(item_start=r"^## Case ")).fallback == ""

    def test_without_a_reference_the_item_gets_a_digest(self):
        result = split_result(_CASES, _spec(item_start=r"^## Case"))
        assert [item.ref_is_digest for item in result.items] == [True, True]
        assert all(item.external_ref.startswith("sha256:") for item in result.items)
        assert result.items[0].external_ref != result.items[1].external_ref

    def test_a_duplicate_reference_is_skipped_and_said(self):
        text = "## Case 7: a\nbody\n## Case 7: a again\nbody\n"
        result = split_result(text, _spec(item_start=r"^## Case", ref_pattern=r"^## Case (\d+)"))
        assert [item.external_ref for item in result.items] == ["7"]
        assert result.skipped == ["'Case 7: a again' (its reference 7 appears twice in this result)"]

    def test_an_unreadable_date_leaves_the_item_undated_and_named(self):
        text = "## A\nDate: sometime\n## B\nDate: 2026-01-02\n"
        result = split_result(text, _spec(item_start=r"^## ", date_pattern=r"^Date: (.+)$"))
        assert [item.occurred_at for item in result.items] == [None, datetime(2026, 1, 2, tzinfo=UTC)]
        assert result.undated == ["A"]

    def test_without_a_spec_the_whole_result_is_one_item(self):
        result = split_result("  one report\nhttps://example.test/r  ", SplitSpec.from_argument(True))
        assert len(result.items) == 1
        assert result.items[0].content == "one report\nhttps://example.test/r"
        assert result.items[0].title == "one report"
        assert result.items[0].source_url == "https://example.test/r"


class TestJsonSplit:
    _DATA = {
        "data": {
            "items": [
                {"id": "c-1", "title": "First", "date": "2026-02-01", "url": "https://example.test/1", "body": "x"},
                {"id": "c-2", "title": "Second", "date": "01/03/2026", "url": "https://example.test/2", "body": "y"},
            ],
            "total": 2,
        }
    }

    def test_each_record_is_one_item_with_its_fields(self):
        result = split_result(
            json.dumps(self._DATA),
            _spec(
                json_path="data.items",
                ref_field="id",
                title_field="title",
                date_field="date",
                url_field="url",
                date_order="DMY",
            ),
        )
        assert [(i.external_ref, i.title, i.source_url) for i in result.items] == [
            ("c-1", "First", "https://example.test/1"),
            ("c-2", "Second", "https://example.test/2"),
        ]
        assert [i.occurred_at for i in result.items] == [
            datetime(2026, 2, 1, tzinfo=UTC),
            datetime(2026, 3, 1, tzinfo=UTC),
        ]
        assert json.loads(result.items[0].content) == self._DATA["data"]["items"][0]

    def test_a_path_that_holds_no_list_is_refused(self):
        with pytest.raises(SplitError, match=r"no JSON list at 'data\.total'"):
            split_result(json.dumps(self._DATA), _spec(json_path="data.total"))

    def test_a_json_path_on_text_stores_the_text_whole(self) -> None:
        result = split_result("Report: plain text\nLine two", _spec(json_path="data.items", ref_field="id"))
        assert [(item.title, item.content) for item in result.items] == [
            ("Report: plain text", "Report: plain text\nLine two")
        ]
        # No JSON record, so no ref_field: the reference is the text's digest.
        assert result.items[0].ref_is_digest is True
        assert "the result is not JSON" in result.fallback
        assert "item_start" in result.fallback

    def test_a_json_path_on_json_without_a_list_there_is_still_refused(self) -> None:
        with pytest.raises(SplitError, match="holds no JSON list"):
            split_result(json.dumps({"data": {"items": 3}}), _spec(json_path="data.items"))

    def test_the_values_next_to_the_list_go_to_the_receipt(self):
        """The source's total sits next to its list: dropping it would hide the coverage."""
        data = {"meta": "page 1", "data": {"items": [{"id": 1}], "total": 19832, "pages": 662}}
        result = split_result(json.dumps(data), _spec(json_path="data.items", ref_field="id"))
        assert result.preamble == 'meta: "page 1", total: 19832, pages: 662'
        assert result.preamble_chars == len(result.preamble)

    def test_without_a_json_split_a_json_result_is_one_item(self):
        """`{}` keeps the whole result as one item, JSON or not."""
        for raw in (json.dumps(self._DATA), json.dumps([1, 2, 3])):
            result = split_result(raw, SplitSpec.from_argument({}))
            assert [item.content for item in result.items] == [raw]

    def test_an_empty_json_path_is_the_top_level_list(self):
        result = split_result(json.dumps([{"id": "a"}, {"id": "b"}]), _spec(json_path=""))
        assert [item.title for item in result.items] == ["Item 1", "Item 2"]

    def test_a_record_without_a_title_is_named_by_its_reference(self):
        result = split_result(json.dumps([{"id": "c-9", "body": "x"}]), _spec(ref_field="id"))
        assert result.items[0].title == "Item c-9"

    def test_a_json_split_and_item_start_together_are_refused(self):
        with pytest.raises(SplitError, match="not both"):
            split_result("[]", _spec(json_path="", item_start="^## "))

    def test_a_record_field_is_never_read_from_its_text(self):
        """No URL field given: a JSON record's link is not guessed from its text."""
        result = split_result(json.dumps([{"id": 1, "body": "see https://example.test/x"}]), _spec(ref_field="id"))
        assert result.items[0].source_url is None


class TestDates:
    @pytest.mark.parametrize(
        ("raw", "order", "expected"),
        [
            ("2026-10-06", "MDY", datetime(2026, 10, 6, tzinfo=UTC)),
            ("2026-10-06T08:00:00Z", "DMY", datetime(2026, 10, 6, tzinfo=UTC)),
            ("10/6/2026", "MDY", datetime(2026, 10, 6, tzinfo=UTC)),
            ("10/6/2026", "DMY", datetime(2026, 6, 10, tzinfo=UTC)),
            ("2026/10/06", "YMD", datetime(2026, 10, 6, tzinfo=UTC)),
            ("6.10.2026", "DMY", datetime(2026, 10, 6, tzinfo=UTC)),
        ],
    )
    def test_a_date_is_read_in_the_order_the_source_writes_it(self, raw, order, expected):
        assert parse_date(raw, order) == expected

    @pytest.mark.parametrize("raw", ["", None, "yesterday", "13/13/2026", "10/6/26", "31/02/2026"])
    def test_an_ambiguous_or_impossible_date_is_none(self, raw):
        assert parse_date(raw, "DMY") is None

    def test_a_numeric_date_in_the_default_order_is_not_guessed(self):
        # YMD by default: a month-first source must say so.
        assert parse_date("10/6/2026", "YMD") is None


class TestSpec:
    @pytest.mark.parametrize("raw", [True, {}, None, ""])
    def test_true_or_empty_is_one_item(self, raw):
        assert SplitSpec.from_argument(raw) == SplitSpec()

    def test_an_unknown_key_is_refused(self):
        with pytest.raises(SplitError, match="does not take item_end, regex"):
            SplitSpec.from_argument({"regex": "x", "item_end": "y"})

    def test_a_non_object_is_refused(self):
        with pytest.raises(SplitError, match="must be true or an object"):
            SplitSpec.from_argument(["^## "])

    def test_an_unknown_date_order_is_refused(self):
        with pytest.raises(SplitError, match="date_order must be one of YMD, DMY, MDY"):
            SplitSpec.from_argument({"date_order": "YDM"})

    def test_a_long_pattern_is_refused(self):
        with pytest.raises(SplitError, match="item_start is longer than 300"):
            SplitSpec.from_argument({"item_start": "a" * 301})

    def test_an_invalid_pattern_is_refused(self):
        with pytest.raises(SplitError, match="Invalid pattern"):
            split_result("text", _spec(item_start="(unclosed"))


class TestBounds:
    def test_a_catastrophic_pattern_stops_at_the_budget(self, monkeypatch):
        monkeypatch.setattr(workset_split, "SPLIT_REGEX_BUDGET_SECONDS", 0.2)
        started = time.monotonic()
        with pytest.raises(SplitError, match="time budget"):
            split_result("a" * 60 + "!", _spec(item_start=r"(a|aa)+$"))
        assert time.monotonic() - started < 2

    def test_the_budget_covers_the_field_patterns_too(self, monkeypatch):
        monkeypatch.setattr(workset_split, "SPLIT_REGEX_BUDGET_SECONDS", 0.2)
        with pytest.raises(SplitError, match="time budget"):
            split_result("## x\n" + "a" * 60 + "!", _spec(item_start=r"^## ", ref_pattern=r"((a+)\2)+b"))

    def test_a_result_over_the_cap_is_refused_not_cut(self, monkeypatch):
        monkeypatch.setattr(workset_split, "MAX_RESULT_CHARS", 10)
        with pytest.raises(SplitError, match="11 characters, over the 10"):
            split_result("x" * 11, SplitSpec())

    def test_too_many_items_are_refused_not_cut(self, monkeypatch):
        monkeypatch.setattr(workset_split, "MAX_ITEMS", 2)
        with pytest.raises(SplitError, match="3 items, over the 2"):
            split_result("## a\n## b\n## c\n", _spec(item_start=r"^## "))
