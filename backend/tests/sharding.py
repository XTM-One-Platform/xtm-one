"""Which tests one CI job runs: ``PYTEST_SHARD=<index>/<total>``.

CI splits each backend suite between several jobs, in one of two ways.

By test (the default): every job collects the whole suite and keeps every
``<total>``-th test of the sorted node ids, so a test runs in exactly one job
whatever order the collection came in, and a slow file's tests are spread over
all of them.

By file (``PYTEST_SHARD_SPLIT=files``, the unit job): every job imports only
its share of the test files. Collecting the whole unit suite holds about
1.6 GB per process, and each pytest-xdist worker is a process: on a 2-vCPU,
8 GB runner the two workers' collections plus the tests left the runner with
under 250 MB and nearly 3 GB of swap, and a runner short of memory loses its
agent ("The runner has received a shutdown signal", #10437). A sixth of the
files collects in about half the memory. The files are dealt out
longest-first by their measured duration (``shard_durations.json``) so the
jobs end together; a file the table does not name weighs ``UNTIMED_SECONDS``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Collection, Iterable, Iterator, Mapping
from pathlib import Path

#: Seconds of one worker each test file took on CI (files of 2 s and more): the
#: committed fallback of the table the nightly runs publish (scripts/shard_weights.py).
DURATIONS_PATH = Path(__file__).with_name("shard_durations.json")

#: The Browser tool job's table: its tests run one at a time, under a real Chromium.
CHROMIUM_DURATIONS_PATH = Path(__file__).with_name("shard_durations_chromium.json")

#: What a file the table does not name weighs: the median file takes under a second.
UNTIMED_SECONDS = 1.0

#: Read by every job of a file split: each checks the collection of its own files.
EVERY_SHARD = frozenset({"tests/test_marked_files.py"})

SPLITS = ("tests", "files")


def parse_shard(value: str) -> tuple[int, int]:
    """``"2/3"`` -> ``(2, 3)``; anything else is refused."""
    match = re.fullmatch(r"\s*(\d+)\s*/\s*(\d+)\s*", value)
    if match is None:
        raise ValueError(f"PYTEST_SHARD must read <index>/<total>, got {value!r}")
    index, total = int(match.group(1)), int(match.group(2))
    if not 1 <= index <= total:
        raise ValueError(f"PYTEST_SHARD index must be between 1 and {total}, got {value!r}")
    return index, total


def parse_split(value: str | None) -> str:
    """``PYTEST_SHARD_SPLIT``: ``tests`` (the default) or ``files``."""
    split = (value or "tests").strip()
    if split not in SPLITS:
        raise ValueError(f"PYTEST_SHARD_SPLIT must be one of {', '.join(SPLITS)}, got {value!r}")
    return split


def shard_node_ids(node_ids: Iterable[str], index: int, total: int) -> frozenset[str]:
    """The node ids job ``index`` of ``total`` keeps."""
    return frozenset(sorted(set(node_ids))[index - 1 :: total])


def durations_path(value: str | None) -> Path:
    """``PYTEST_SHARD_DURATIONS``: the table the run's select job handed every job (``scripts/shard_weights.py``).

    Unset, the committed unit table. Set, it must read: a job that fell back on
    another table than its siblings would deal the files differently, and a
    file would run twice or never.
    """
    if not value:
        return DURATIONS_PATH
    path = Path(value)
    if not path.is_file():
        raise ValueError(f"PYTEST_SHARD_DURATIONS names {value!r}, which is not a file")
    return path


def load_durations(path: Path = DURATIONS_PATH) -> dict[str, float]:
    return {name: float(seconds) for name, seconds in json.loads(path.read_text(encoding="utf-8")).items()}


def shard_files(files: Iterable[str], durations: Mapping[str, float], index: int, total: int) -> frozenset[str]:
    """The test files job ``index`` of ``total`` collects, ``files`` as paths from the backend root.

    Longest first, each file to the job with the least work so far (the lowest
    index on a tie): the same files give the same share whatever their order.
    """
    loads = [0.0] * total
    kept: set[str] = set()
    for name in sorted(set(files), key=lambda name: (-durations.get(name, UNTIMED_SECONDS), name)):
        job = loads.index(min(loads))
        loads[job] += durations.get(name, UNTIMED_SECONDS)
        if job == index - 1:
            kept.add(name)
    return frozenset(kept | (EVERY_SHARD & set(files)))


def dealt_files(root: Path, selected: Collection[str] | None, marker: str | None) -> Iterator[str]:
    """The test files a ``PYTEST_SHARD_SPLIT=files`` job deals out, as paths from ``root``.

    Only the files the job runs: the selected ones (``PYTEST_SELECTED_FILES``;
    every one when ``selected`` is None) and, under ``PYTEST_MARKED_FILES``, those
    that spell ``marker``. The files it leaves out would otherwise weigh in the
    deal, and the shards would get unequal shares of the files they do run.
    """
    from tests.marked_files import skips_file

    for path in sorted((root / "tests").rglob("test_*.py")):
        name = path.relative_to(root).as_posix()
        if (selected is None or name in selected) and (marker is None or not skips_file(path, marker)):
            yield name
