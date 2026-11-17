"""A large parameter value never becomes a large test id (``conftest.pytest_make_parametrize_id``)."""

from __future__ import annotations

import hashlib

import pytest

from tests.conftest import PARAM_ID_MAX_CHARS, pytest_make_parametrize_id


def test_a_5_mb_bytes_value_is_named_by_its_length_and_digest():
    value = b"\x89PNG\r\n\x1a\n" + b"\x00" * (5 * 1024 * 1024 + 1)

    name = pytest_make_parametrize_id(None, value, "data")

    assert name == f"data-bytes{len(value)}-{hashlib.sha256(value).hexdigest()[:12]}"
    assert len(name) < 64


def test_a_long_str_value_is_named_by_its_length_and_digest():
    value = "a@" * 20_000

    name = pytest_make_parametrize_id(None, value, "text")

    assert name.startswith(f"text-str{len(value)}-")
    assert len(name) < 64


@pytest.mark.parametrize("value", ["x" * PARAM_ID_MAX_CHARS, b"x" * PARAM_ID_MAX_CHARS, 3, None, ("a", "b")])
def test_a_short_or_other_value_keeps_pytest_s_own_id(value):
    assert pytest_make_parametrize_id(None, value, "v") is None


def test_two_long_values_of_the_same_length_get_two_ids():
    first = pytest_make_parametrize_id(None, "a" * 1000, "v")
    second = pytest_make_parametrize_id(None, "b" * 1000, "v")

    assert first != second
