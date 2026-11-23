"""The split of a suite between CI jobs (``tests/sharding.py``)."""

from __future__ import annotations

import pytest

from tests.sharding import (
    DURATIONS_PATH,
    EVERY_SHARD,
    UNTIMED_SECONDS,
    dealt_files,
    load_durations,
    parse_shard,
    parse_split,
    shard_files,
    shard_node_ids,
)

NODE_IDS = [f"tests/test_{module}.py::test_{case}" for module in "abcdefg" for case in range(13)]


@pytest.mark.parametrize("total", [1, 2, 3, 5, 8])
def test_every_test_runs_in_exactly_one_job(total):
    shares = [shard_node_ids(NODE_IDS, index, total) for index in range(1, total + 1)]

    assert sum(len(share) for share in shares) == len(NODE_IDS)
    assert frozenset().union(*shares) == frozenset(NODE_IDS)


def test_the_share_does_not_depend_on_the_collection_order():
    reordered = [NODE_IDS[::-1], NODE_IDS[40:] + NODE_IDS[:40]]

    for order in reordered:
        assert shard_node_ids(order, 2, 3) == shard_node_ids(NODE_IDS, 2, 3)


def test_the_jobs_run_as_many_tests_give_or_take_one():
    sizes = {len(shard_node_ids(NODE_IDS, index, 4)) for index in range(1, 5)}

    assert max(sizes) - min(sizes) <= 1


def test_a_slow_file_is_spread_over_every_job():
    slow_file = [node_id for node_id in NODE_IDS if node_id.startswith("tests/test_c.py")]

    for index in range(1, 4):
        assert set(slow_file) & shard_node_ids(NODE_IDS, index, 3)


@pytest.mark.parametrize("value", ["", "3", "0/3", "4/3", "a/b", "1/0", "-1/3"])
def test_a_malformed_shard_is_refused(value):
    with pytest.raises(ValueError, match="PYTEST_SHARD"):
        parse_shard(value)


def test_a_shard_reads_index_then_total():
    assert parse_shard("2/3") == (2, 3)
    assert parse_shard(" 1 / 1 ") == (1, 1)


FILES = [f"tests/test_{name}.py" for name in "abcdefghijklmnopqrstuvwxyz"]


@pytest.mark.parametrize("total", [1, 2, 3, 6])
def test_by_file_every_file_is_read_by_exactly_one_job(total):
    durations = {name: float(position % 7) for position, name in enumerate(FILES)}
    shares = [shard_files(FILES, durations, index, total) for index in range(1, total + 1)]

    assert sum(len(share) for share in shares) == len(FILES)
    assert frozenset().union(*shares) == frozenset(FILES)


def test_by_file_the_share_does_not_depend_on_the_order_of_the_files():
    durations = {"tests/test_c.py": 40.0, "tests/test_k.py": 12.0}

    assert shard_files(FILES[::-1], durations, 2, 3) == shard_files(FILES, durations, 2, 3)


def test_by_file_the_slow_files_go_to_different_jobs_and_the_loads_stay_close():
    durations = {name: 1.0 for name in FILES} | {
        "tests/test_a.py": 30.0,
        "tests/test_b.py": 29.0,
        "tests/test_c.py": 28.0,
    }
    shares = [shard_files(FILES, durations, index, 3) for index in range(1, 4)]
    loads = [sum(durations[name] for name in share) for share in shares]

    assert [len(share & {"tests/test_a.py", "tests/test_b.py", "tests/test_c.py"}) for share in shares] == [1, 1, 1]
    assert max(loads) - min(loads) <= 1.0


def test_by_file_an_untimed_file_still_has_a_job():
    share = shard_files(["tests/test_new.py"], {}, 1, 1)

    assert share == {"tests/test_new.py"}
    assert UNTIMED_SECONDS > 0


def test_by_file_the_files_every_job_reads_are_in_every_share():
    files = FILES + sorted(EVERY_SHARD)

    for index in range(1, 7):
        assert shard_files(files, {}, index, 6) >= EVERY_SHARD


def test_the_durations_table_names_test_files_with_positive_seconds():
    durations = load_durations(DURATIONS_PATH)

    assert durations
    assert all(name.startswith("tests/") and name.endswith(".py") for name in durations)
    assert all(seconds > 0 for seconds in durations.values())


def test_by_file_the_six_unit_jobs_read_every_test_file_of_the_suite():
    root = DURATIONS_PATH.parent.parent
    files = sorted(path.relative_to(root).as_posix() for path in (root / "tests").rglob("test_*.py"))
    durations = load_durations(DURATIONS_PATH)
    shares = [shard_files(files, durations, index, 6) - EVERY_SHARD for index in range(1, 7)]

    assert frozenset().union(*shares) | (EVERY_SHARD & set(files)) == frozenset(files)
    assert sum(len(share) for share in shares) == len(set(files) - EVERY_SHARD)


@pytest.mark.parametrize("value", [None, "", "tests", " files "])
def test_a_split_reads_tests_or_files(value):
    assert parse_split(value) == ("files" if value and value.strip() == "files" else "tests")


def test_an_unknown_split_is_refused():
    with pytest.raises(ValueError, match="PYTEST_SHARD_SPLIT"):
        parse_split("modules")


def test_the_table_a_job_deals_by_is_the_one_its_run_handed_out(tmp_path):
    from tests.sharding import DURATIONS_PATH, durations_path

    assert durations_path(None) == DURATIONS_PATH
    table = tmp_path / "unit.json"
    table.write_text("{}", encoding="utf-8")
    assert durations_path(str(table)) == table
    # A job that fell back alone would deal the files unlike its siblings: one would run twice or never.
    with pytest.raises(ValueError, match="PYTEST_SHARD_DURATIONS"):
        durations_path(str(tmp_path / "missing.json"))


def test_a_marked_job_deals_only_the_files_that_spell_its_marker(tmp_path):
    # The Browser tool job runs the chromium files only: the others must not weigh in its deal.
    (tmp_path / "tests" / "sub").mkdir(parents=True)
    (tmp_path / "tests" / "test_browser.py").write_text("pytestmark = pytest.mark.chromium\n", encoding="utf-8")
    (tmp_path / "tests" / "sub" / "test_page.py").write_text(
        "@pytest.mark.chromium\ndef test_x(): ...\n", encoding="utf-8"
    )
    (tmp_path / "tests" / "test_unit.py").write_text("def test_x(): ...\n", encoding="utf-8")

    assert list(dealt_files(tmp_path, None, "chromium")) == ["tests/sub/test_page.py", "tests/test_browser.py"]
    assert list(dealt_files(tmp_path, {"tests/test_browser.py", "tests/test_unit.py"}, "chromium")) == [
        "tests/test_browser.py"
    ]
    assert list(dealt_files(tmp_path, None, None)) == [
        "tests/sub/test_page.py",
        "tests/test_browser.py",
        "tests/test_unit.py",
    ]
