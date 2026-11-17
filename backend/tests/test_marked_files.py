"""The test files a one-marker CI job reads (``tests/marked_files.py``)."""

from __future__ import annotations

import pytest

from tests.marked_files import UNSCANNED, parse_marker, skips_file, spells_marker


def test_every_marked_test_lives_in_a_file_that_spells_its_marker(pytestconfig):
    """The Bootstrap and Browser tool jobs read only the files that spell their marker.

    ``tests/conftest.py`` notes, for every test this run collected, a scanned
    marker its file never spells. A unit job collects every file, so this
    checks the whole suite on each push.
    """
    assert UNSCANNED in pytestconfig.stash, "tests/conftest.py no longer notes the unscanned markers"
    assert pytestconfig.stash[UNSCANNED] == [], (
        "These tests carry a marker their file never spells as `mark.<marker>`, so the CI job running "
        "that marker would not collect them; apply it with pytest.mark.<marker> in the file:\n  "
        + "\n  ".join(pytestconfig.stash[UNSCANNED])
    )


@pytest.mark.parametrize(
    "source",
    [
        "@pytest.mark.integration\ndef test_x(): ...",
        "pytestmark = pytest.mark.integration",
        "pytestmark = [pytest.mark.integration, pytest.mark.asyncio]",
        "from pytest import mark\n@mark.integration\ndef test_x(): ...",
    ],
    ids=["decorator", "module-mark", "module-mark-list", "bare-mark"],
)
def test_the_ways_a_file_applies_a_marker_are_read(source):
    assert spells_marker(source, "integration")


@pytest.mark.parametrize(
    "source",
    ["def test_integration_flow(): ...", "pytest.mark.integrations", "# an integration test"],
    ids=["test-name", "longer-marker", "prose"],
)
def test_the_word_alone_is_not_a_marker(source):
    assert not spells_marker(source, "integration")


def test_a_commented_out_marker_still_keeps_the_file():
    """The scan reads text: a file read for nothing only costs its collection, a file skipped loses its tests."""
    assert spells_marker("# @pytest.mark.integration\ndef test_x(): ...", "integration")


def test_only_test_modules_that_never_spell_the_marker_are_left_out(tmp_path):
    marked = tmp_path / "test_marked.py"
    marked.write_text("pytestmark = pytest.mark.chromium\n", encoding="utf-8")
    unmarked = tmp_path / "test_unmarked.py"
    unmarked.write_text("def test_x(): ...\n", encoding="utf-8")
    helper = tmp_path / "helpers.py"
    helper.write_text("def helper(): ...\n", encoding="utf-8")

    assert not skips_file(marked, "chromium")
    assert skips_file(unmarked, "chromium")
    assert not skips_file(helper, "chromium")
    assert not skips_file(tmp_path, "chromium")


def test_an_unknown_marker_is_refused():
    assert parse_marker(" integration ") == "integration"
    with pytest.raises(ValueError, match="PYTEST_MARKED_FILES"):
        parse_marker("integraton")
