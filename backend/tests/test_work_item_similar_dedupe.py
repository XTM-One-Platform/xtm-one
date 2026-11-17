"""``dedupe_by="similar"``: one friction filed under two slugs lands once (#8919).

The matcher (:func:`work_item.near_duplicate`) is pure and tested here; the
store path (owner, window, unfinished, lock) runs against a real PostgreSQL in
``test_work_item_dependencies_postgres.py``.
"""

from __future__ import annotations

import pytest

from app.services import work_item as wi_service
from app.services.work_item import key_namespace, near_duplicate, normalize_dedupe_by, recent_twin


def test_two_slugs_for_one_trigger_filter_are_one_item():
    assert near_duplicate(
        "fleet:main-red-trigger-fork-filter",
        "Main-red trigger: skip fork PR runs",
        "fleet:main-red-trigger-fork-pr-filter",
        "Main-red trigger filter: skip workflow runs of fork PRs",
    )


def test_a_reworded_slug_with_the_same_title_matches():
    assert near_duplicate(
        "dev:sandbox-python-dev-create-504",
        "Sandbox python-dev pool create answers 504",
        "dev:sandbox-pool-create-504",
        "Sandbox python-dev pool create answers 504",
    )


@pytest.mark.parametrize(
    ("key_a", "title_a", "key_b", "title_b"),
    [
        # Another namespace is another desk's work.
        ("dev:main-red-trigger-fork-filter", "t", "fleet:main-red-trigger-fork-filter", "t"),
        # A slice item of a program is its child, never its duplicate.
        ("dev:work-item-lifecycle-races", "races", "dev:work-item-lifecycle-races-8904", "races"),
        # Another PR number is another PR.
        ("dev:ux-verdict-8814-comment", "UX verdict on #8814", "dev:ux-verdict-8850-comment", "UX verdict on #8850"),
        # Unrelated slugs and titles.
        ("dev:sandbox-pool-create-504", "Pool create 504", "dev:chat-reactivity-retest", "Chat reactivity retest"),
        # No namespace: nothing to compare against.
        ("plain-key-one", "Same title", "plain-key-two", "Same title"),
    ],
)
def test_distinct_work_does_not_match(key_a, title_a, key_b, title_b):
    assert not near_duplicate(key_a, title_a, key_b, title_b)


def test_two_runs_filing_one_symptom_under_two_slugs_are_twins():
    # Two runs on sibling items filed one gap in the same minute (9 Nov 2026).
    key_a, title_a = (
        "dev:finalizer-ui-pr-no-merged-or-closed-ref",
        "Inbox finalizer / PR-linked item: no blocked_on_refs merged_or_closed on an xtm-one UI PR awaiting its UX round",
    )
    key_b, title_b = (
        "dev:finalizer-blocked-on-refs-ux-wait",
        "the inbox finalizer parks a UI PR item on merged_or_closed while its UX round is owed",
    )
    assert not near_duplicate(key_a, title_a, key_b, title_b)
    assert recent_twin(key_a, title_a, key_b, title_b)
    assert recent_twin(key_b, title_b, key_a, title_a)


@pytest.mark.parametrize(
    ("key_a", "title_a", "key_b", "title_b"),
    [
        # A few shared words are a shared area, not one symptom.
        ("dev:ux-gate-capture-dark", "UX gate capture in dark mode", "dev:ux-gate-capture-light", "UX gate light"),
        ("dev:pr-states-merge-unknown", "PR states answer merge unknown", "dev:pr-pools-refill", "Refill PR pools"),
        # Another number is another PR, whatever the words.
        (
            "dev:ux-verdict-8814-comment",
            "Post the UX verdict comment on xtm-one PR #8814 round",
            "dev:ux-verdict-8850-comment",
            "Post the UX verdict comment on xtm-one PR #8850 round",
        ),
        # A slice item is the child of its program.
        (
            "dev:finalizer-ui-pr",
            "Inbox finalizer parks a UI PR item on merged_or_closed during its UX round",
            "dev:finalizer-ui-pr-2",
            "Inbox finalizer parks a UI PR item on merged_or_closed during its UX round",
        ),
        ("", "same", "dev:x", "same"),
    ],
)
def test_distinct_work_is_no_twin(key_a, title_a, key_b, title_b):
    assert not recent_twin(key_a, title_a, key_b, title_b)


def test_a_twin_may_carry_another_namespace():
    title = "Inbox finalizer parks a UI PR item on merged_or_closed while its UX round is owed"
    assert recent_twin("dev:finalizer-ux-wait", title, "fleet:finalizer-ux-round", title)


def test_the_same_key_matches_and_an_empty_one_does_not():
    assert near_duplicate("dev:x", "a", "dev:x", "b")
    assert not near_duplicate("", "a", "", "a")


def test_key_namespace():
    assert key_namespace("fleet:main-red") == "fleet"
    assert key_namespace(" DEV:x") == "dev"
    assert key_namespace("no-namespace") == ""
    assert key_namespace(None) == ""


def test_similar_is_a_dedupe_mode():
    assert normalize_dedupe_by("similar") == "similar"
    assert "similar" in wi_service.DEDUPE_BY_MODES
    with pytest.raises(ValueError):
        normalize_dedupe_by("fuzzy")
