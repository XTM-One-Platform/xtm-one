"""Which test files a CI job reads: ``PYTEST_MARKED_FILES=<marker>``.

The Bootstrap and Browser tool jobs run one marker's tests (``-m integration``,
``-m chromium``), yet collecting the whole suite to keep them took two to four
minutes of each job. With the variable set, a ``test_*.py`` file whose source
never spells ``mark.<marker>`` is not collected at all.

The scan reads text, so a marker applied some other way would leave its tests
out silently. ``tests/conftest.py`` notes every collected test that carries a
scanned marker its file never spells (``UNSCANNED``), after any conftest has
added its markers and before ``-m`` deselects anything, and
``tests/test_marked_files.py`` fails on any: a unit job collects every file,
so each push checks the whole suite.

The scan errs on reading too much: a ``mark.<marker>`` in a comment or a
string keeps the file, which then only costs its collection.
"""

from __future__ import annotations

import re
from functools import cache
from pathlib import Path

import pytest

SCANNED_MARKERS = ("integration", "chromium")

# The marker ``PYTEST_MARKED_FILES`` names, or None to collect every file.
MARKED_FILES = pytest.StashKey[str | None]()

UNSCANNED = pytest.StashKey[list[str]]()


def parse_marker(value: str) -> str:
    """The marker ``PYTEST_MARKED_FILES`` names; one this module does not scan is refused."""
    marker = value.strip()
    if marker not in SCANNED_MARKERS:
        raise ValueError(f"PYTEST_MARKED_FILES must be one of {', '.join(SCANNED_MARKERS)}, got {value!r}")
    return marker


def spells_marker(source: str, marker: str) -> bool:
    """Whether ``source`` names ``mark.<marker>`` (``@pytest.mark.x``, ``pytestmark = pytest.mark.x``, ...)."""
    return re.search(rf"mark\.{re.escape(marker)}\b", source) is not None


@cache
def file_spells_marker(path: Path, marker: str) -> bool:
    return spells_marker(path.read_text(encoding="utf-8"), marker)


def skips_file(path: Path, marker: str) -> bool:
    """Whether a job running ``marker``'s tests leaves ``path`` out of its collection."""
    return path.suffix == ".py" and path.name.startswith("test_") and not file_spells_marker(path, marker)


def unscanned_markers(item: pytest.Item) -> list[str]:
    """The scanned markers ``item`` carries although its file never spells them."""
    return [
        marker
        for marker in SCANNED_MARKERS
        if item.get_closest_marker(marker) is not None and skips_file(item.path, marker)
    ]
