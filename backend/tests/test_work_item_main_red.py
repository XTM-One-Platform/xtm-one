"""One carrier per red on main (``payload.main_red``): the pure key and text.

The store path (any key, any owner, the lock, ``get_work_item``'s
``same_red``) runs against a real PostgreSQL in
``test_work_item_main_red_postgres.py``.
"""

from __future__ import annotations

import pytest

from app.services.work_item_main_red import (
    main_red_key_of,
    normalize_main_red,
    red_key,
    same_red_text,
)


def _red(**fields) -> dict:
    return {"main_red": {"repo": "XTM-One-Platform/xtm-one", "check": "Frontend unit tests", **fields}}


def test_two_writers_naming_one_red_differently_get_one_key():
    a = normalize_main_red(_red(test="src/office/addin.test.ts"))
    b = normalize_main_red(
        {
            "main_red": {
                "repo": " xtm-one-platform/XTM-ONE ",
                "check": "frontend  unit tests",
                "test": "SRC/office/addin.test.ts",
            }
        }
    )
    assert main_red_key_of(a) == main_red_key_of(b)
    assert main_red_key_of(a) == "xtm-one-platform/xtm-one|frontend unit tests|test:src/office/addin.test.ts"


def test_a_full_and_a_short_sha_of_one_commit_give_one_key():
    full = normalize_main_red(_red(sha="9EE057F3FC80aa11bb22cc33dd44ee55ff667788"))
    short = normalize_main_red(_red(sha="9ee057f"))
    assert main_red_key_of(full) == main_red_key_of(short)


@pytest.mark.parametrize(
    "other",
    [
        {"test": "src/office/other.test.ts"},
        {"check": "Backend unit tests", "test": "src/office/addin.test.ts"},
        {"repo": "OpenCTI-Platform/opencti", "test": "src/office/addin.test.ts"},
    ],
)
def test_another_red_is_another_key(other):
    base = main_red_key_of(normalize_main_red(_red(test="src/office/addin.test.ts")))
    assert main_red_key_of(normalize_main_red(_red(**{"test": "src/office/addin.test.ts", **other}))) != base


def test_the_test_wins_over_the_sha():
    with_both = normalize_main_red(_red(test="t.py", sha="abcdef1"))
    assert main_red_key_of(with_both) == red_key("XTM-One-Platform/xtm-one", "Frontend unit tests", test="t.py")


def test_a_payload_without_main_red_is_untouched():
    assert normalize_main_red({"notify": {"channel": "x"}}) == {"notify": {"channel": "x"}}
    assert normalize_main_red(None) is None
    assert main_red_key_of({"main_red": "not an object"}) is None


@pytest.mark.parametrize(
    "red",
    [
        "a string",
        {"check": "c", "test": "t"},
        {"repo": "no-slash", "check": "c", "test": "t"},
        {"repo": "o/n", "test": "t"},
        {"repo": "o/n", "check": "c"},
        {"repo": "o/n", "check": "c", "sha": "not-a-sha"},
    ],
)
def test_a_red_that_cannot_name_one_red_is_refused(red):
    with pytest.raises(ValueError, match="main_red"):
        normalize_main_red({"main_red": red})


def test_same_red_text_tells_a_second_run_to_stand_down():
    held = [
        {
            "id": "a",
            "correlation_key": "dev:x",
            "status": "in_progress",
            "claimed_by_run": {"id": "run-1", "alive": True},
        }
    ]
    text = same_red_text(held, ("run-2",))
    assert text and "HELD BY ANOTHER RUN" in text and "run-1" in text and "Stand down" in text
    # The holder itself only sees the other carriers named.
    own = same_red_text(held, ("RUN-1",))
    assert own and "HELD BY ANOTHER RUN" not in own and "`a`" in own
    # An ended holder is no stand-down.
    ended = [{**held[0], "claimed_by_run": {"id": "run-1", "alive": False}}]
    assert "HELD" not in (same_red_text(ended, ("run-2",)) or "")
    assert same_red_text([], ()) is None
