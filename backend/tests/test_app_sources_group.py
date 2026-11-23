"""The tests that read the parsed backend source run on ``app_sources.XDIST_GROUPS`` xdist workers (#10437).

``tests/app_sources.py`` keeps about 1.5 GB of syntax trees per process; the
conftest deals its readers into that many groups, so the CI runner holds one
copy per group, not one per worker.
"""

from __future__ import annotations

from pathlib import Path

from tests import app_sources

TESTS = Path(__file__).resolve().parent


def test_a_file_importing_the_module_reads_the_sources():
    assert app_sources.reads_sources(TESTS / "test_execution_count_writes.py")


def test_a_file_reading_it_through_a_helper_reads_the_sources(tmp_path):
    (tmp_path / "scan_helper.py").write_text("from tests import app_sources\n", encoding="utf-8")
    test_file = tmp_path / "test_through_helper.py"
    test_file.write_text("from tests.scan_helper import scan\n", encoding="utf-8")
    assert app_sources.reads_sources(test_file)


def test_a_file_that_never_names_it_does_not(tmp_path):
    test_file = tmp_path / "test_plain.py"
    test_file.write_text("def test_one():\n    assert True\n", encoding="utf-8")
    assert not app_sources.reads_sources(test_file)


def test_a_missing_file_does_not(tmp_path):
    assert not app_sources.reads_sources(tmp_path / "test_gone.py")


def test_the_conftest_puts_the_readers_in_a_group(request):
    item = next(i for i in request.session.items if i.nodeid == request.node.nodeid)
    marker = item.get_closest_marker("xdist_group")
    assert marker is not None
    name = marker.args[0] if marker.args else marker.kwargs["name"]
    assert name in {f"{app_sources.XDIST_GROUP}-{index}" for index in range(1, app_sources.XDIST_GROUPS + 1)}


def test_the_readers_are_dealt_longest_first_into_every_group():
    durations = {"tests/test_a.py": 60, "tests/test_b.py": 40, "tests/test_c.py": 30, "tests/test_d.py": 10}
    groups = app_sources.xdist_groups(durations, durations)
    # 60 | 40 + 30 ... then 10 to the lighter one: 70 against 70.
    assert groups == {
        "tests/test_a.py": "app_sources-1",
        "tests/test_b.py": "app_sources-2",
        "tests/test_c.py": "app_sources-2",
        "tests/test_d.py": "app_sources-1",
    }
    assert len(set(groups.values())) == app_sources.XDIST_GROUPS


def test_every_worker_names_the_same_group_whatever_its_collection_order():
    files = [f"tests/test_{i}.py" for i in range(9)]
    durations = {name: i % 4 for i, name in enumerate(files)}
    assert app_sources.xdist_groups(files, durations) == app_sources.xdist_groups(reversed(files), durations)
