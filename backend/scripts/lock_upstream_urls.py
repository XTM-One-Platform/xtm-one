# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""Write the public PyPI URLs back into a uv.lock resolved through the sandbox registry mirror.

The ``backend-lock-update`` check runs ``uv lock`` on toolchain-python, whose only
route to PyPI is the read-only registry mirror (``registry_mirror.command_env``
sets ``UV_DEFAULT_INDEX`` to ``<mirror>/pypi/simple/``). uv records the index it
resolved against, so the lock would name a cluster-only host that no GitHub runner
or laptop resolves (#2360). The mirror serves PyPI byte for byte (its nginx
``sub_filter`` only rewrites ``files.pythonhosted.org`` to ``/pypi/files/``), so the
hashes are PyPI's: this maps the two mirror prefixes back to ``https://pypi.org/simple``
and ``https://files.pythonhosted.org/`` and keeps the lock ``revision`` the file had
before the run when it was higher. Without ``UV_DEFAULT_INDEX`` (a laptop) nothing changes.

Usage: ``python3 scripts/lock_upstream_urls.py uv.lock [revision-before]``.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

PYPI_SIMPLE = "https://pypi.org/simple"
PYPI_FILES = "https://files.pythonhosted.org/"
_REVISION = re.compile(r"^revision = (\d+)$", re.MULTILINE)


def mirror_base(default_index: str) -> str | None:
    """The mirror root of a ``<root>/pypi/simple/`` index; ``None`` for any other index (pypi.org included)."""
    index = default_index.strip().rstrip("/")
    if not index.endswith("/pypi/simple"):
        return None
    return index.removesuffix("/pypi/simple")


def upstream(text: str, base: str | None, revision_before: int | None = None) -> str:
    """*text* with the mirror prefixes of *base* mapped to PyPI, and the revision kept at least *revision_before*."""
    if base:
        text = text.replace(f"{base}/pypi/simple/", PYPI_SIMPLE).replace(f"{base}/pypi/simple", PYPI_SIMPLE)
        text = text.replace(f"{base}/pypi/files/", PYPI_FILES)
    match = _REVISION.search(text)
    if revision_before is not None and match and int(match.group(1)) < revision_before:
        text = text[: match.start(1)] + str(revision_before) + text[match.end(1) :]
    return text


def main(argv: list[str]) -> int:
    path = Path(argv[1])
    revision_before = int(argv[2]) if len(argv) > 2 and argv[2].isdigit() else None
    base = mirror_base(os.environ.get("UV_DEFAULT_INDEX", ""))
    before = path.read_text(encoding="utf-8")
    after = upstream(before, base, revision_before)
    if after != before:
        path.write_text(after, encoding="utf-8")
    leaked = base is not None and base in after
    print(f"{path}: mirror {'unset' if base is None else base}, upstream URLs written, mirror left: {leaked}")
    return 1 if leaked else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
