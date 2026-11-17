"""Dockerfile.worker's Python stages are the platform image's, instruction for instruction.

The E2E jobs build both images, and the worker build is cheap only because its
layers resolve against the blobs the platform build just pulled: the two files
must stay identical from their first Python base image (the backend-deps stage
that exports uv.lock, then the runtime stage) through ``COPY backend/ .``. A base
tag bumped in one file, or an apt package added to one only, passes every other
check and silently puts the worker back on a full rebuild (~5 minutes) in all
eight E2E jobs, where it takes ~2 seconds now.

Delete this test with Dockerfile.worker once the worker is a target stage of
the root Dockerfile.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# The Python runtime base image, whether spelled `python:3.14-slim` or with its
# registry (`docker.io/library/python:3.14-slim`), pinned by digest or not.
PYTHON_BASE = re.compile(r"FROM\s+(?:[\w.-]+/)*python:")
PLATFORM_DOCKERFILE = ROOT / "Dockerfile"
WORKER_DOCKERFILE = ROOT / "Dockerfile.worker"


def _python_stages(dockerfile: Path) -> list[str]:
    """The instructions from the first Python base image through ``COPY backend/ .``, comments dropped."""
    lines = [
        line.rstrip()
        for line in dockerfile.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    bases = [i for i, line in enumerate(lines) if PYTHON_BASE.match(line)]
    assert bases, f"{dockerfile.name}: no Python base image (`FROM [registry/]python:`)"
    start = bases[0]
    assert "COPY backend/ ." in lines[start:], f"{dockerfile.name}: no `COPY backend/ .` after the runtime base image"
    return lines[start : lines.index("COPY backend/ .", start) + 1]


def test_worker_runtime_stage_is_the_platforms() -> None:
    assert _python_stages(WORKER_DOCKERFILE) == _python_stages(PLATFORM_DOCKERFILE), (
        "Dockerfile.worker and Dockerfile differ between their first Python base image and `COPY backend/ .`: "
        "the E2E worker build no longer reuses the platform's layers and takes ~5 minutes in every job. "
        "Make the same change in both files."
    )
