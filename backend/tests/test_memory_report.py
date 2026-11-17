"""The per-file memory ledger of ``tests/memory_report.py``."""

from __future__ import annotations

from pathlib import Path

from tests import memory_report

MB = 1024 * 1024


def test_unset_or_empty_means_no_report():
    assert memory_report.report_dir({}) is None
    assert memory_report.report_dir({memory_report.MEMORY_REPORT_ENV: "  "}) is None
    assert memory_report.report_dir({memory_report.MEMORY_REPORT_ENV: "reports/memory"}) == Path("reports/memory")


def test_growth_is_summed_per_file_and_ranked():
    ledger = memory_report.MemoryLedger()
    ledger.record("tests/test_a.py::test_one", 100 * MB, 400 * MB)
    ledger.record("tests/test_b.py::test_one[1]", 400 * MB, 450 * MB)
    ledger.record("tests/test_a.py::test_two", 450 * MB, 420 * MB)
    ledger.record("tests/test_c.py::test_one", 420 * MB, 1020 * MB)

    assert ledger.ranked() == [
        ("tests/test_c.py", 600 * MB, 1),
        ("tests/test_a.py", 270 * MB, 2),
        ("tests/test_b.py", 50 * MB, 1),
    ]
    assert ledger.first == 100 * MB
    assert ledger.last == 1020 * MB


def test_an_unreadable_reading_is_left_out():
    ledger = memory_report.MemoryLedger()
    ledger.record("tests/test_a.py::test_one", None, 10 * MB)
    assert ledger.ranked() == []
    assert ledger.first is None


def test_the_report_names_the_worker_and_the_top_files(tmp_path):
    ledger = memory_report.MemoryLedger()
    for index in range(30):
        ledger.record(f"tests/test_{index:02d}.py::test", 0, index * MB)

    path = memory_report.write_report(tmp_path / "out", "gw1", ledger, top=3)

    text = path.read_text(encoding="utf-8").splitlines()
    assert path.name == "memory-gw1.txt"
    assert text[0] == "Memory added per test file on gw1: 0 MB at the first test, 29 MB at the last"
    assert [line.split()[-1] for line in text[1:]] == ["tests/test_29.py", "tests/test_28.py", "tests/test_27.py"]


def test_the_resident_size_reads_statm(tmp_path):
    statm = tmp_path / "statm"
    statm.write_text("1000 256 10 1 0 100 0\n", encoding="ascii")
    assert memory_report.resident_bytes(statm) == 256 * memory_report._PAGE_SIZE
    assert memory_report.resident_bytes(tmp_path / "absent") is None
