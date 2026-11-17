"""Every test that reads the parsed backend source runs on one xdist worker (#10437).

``tests/app_sources.py`` keeps about 1.5 GB of syntax trees per process; the
conftest groups its readers so the CI runner holds one copy, not two.
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


def test_the_conftest_puts_the_readers_in_the_group(request):
    item = next(i for i in request.session.items if i.nodeid == request.node.nodeid)
    marker = item.get_closest_marker("xdist_group")
    assert marker is not None and (marker.args[0] if marker.args else marker.kwargs["name"]) == app_sources.XDIST_GROUP
