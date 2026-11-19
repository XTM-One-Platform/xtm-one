"""``scripts/refresh_shard_durations.py`` sums a nightly run's JUnit reports into the shard weights."""

from __future__ import annotations

from scripts.refresh_shard_durations import MIN_SECONDS, file_seconds


def test_seconds_are_summed_per_file_across_the_shards_reports(tmp_path):
    one = tmp_path / "unit-1.xml"
    one.write_text(
        '<testsuites><testsuite><testcase file="tests/test_a.py" name="x" time="1.6"/>'
        '<testcase file="tests/test_b.py" name="y" time="0.4"/></testsuite></testsuites>',
        encoding="utf-8",
    )
    two = tmp_path / "unit-2.xml"
    two.write_text(
        '<testsuites><testsuite><testcase file="tests/test_a.py" name="z" time="1.5"/></testsuite></testsuites>',
        encoding="utf-8",
    )
    durations = file_seconds([one, two])
    assert durations == {"tests/test_a.py": 3}
    # A file under the floor weighs UNTIMED_SECONDS in tests/sharding.py instead.
    assert MIN_SECONDS > 0.4
