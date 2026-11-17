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

"""One carrier per red on main: the ``main_red`` key of a work item's payload.

A red on a repository's default branch used to be filed by several desks
under several correlation keys (a red-check event, a sweep, a friction), so
two runs diagnosed it and pushed two fixes. An item whose payload names the
red - ``{"main_red": {"repo", "check", "test"}}``, or ``"sha"`` (the red
commit) when no test is known - carries a key derived from those fields
(:func:`red_key`), whatever its correlation key:

* ``create_work_item`` answers the unfinished item already carrying that key
  instead of filing a second one (``"dedupe_by": "main_red"``), with or
  without ``dedupe``;
* ``get_work_item`` names the other unfinished items on the same red and the
  live run holding one, so a run that starts on a red already held stands
  down at its first read.

The fields are normalised (case, spacing, a full or short SHA) so two
writers naming the same red the same way land on one key; a red on another
repository, check or test is another key.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

#: The payload key naming the red on main an item carries.
MAIN_RED_KEY = "main_red"
#: The derived key stored under ``payload.main_red`` (what the lookup reads).
DERIVED_FIELD = "key"

_SPACES = re.compile(r"\s+")
_SHA = re.compile(r"[0-9a-f]{7,40}")


def _clean(value: Any) -> str:
    return _SPACES.sub(" ", str(value or "")).strip().casefold()


def normalize_main_red(payload: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """*payload* with its ``main_red`` checked and its derived key added.

    No ``main_red``: *payload* unchanged. One without ``repo`` and ``check``,
    or with neither ``test`` nor a hexadecimal ``sha``, is refused
    (``ValueError``): a key that cannot name one red would merge unrelated
    reds.
    """
    if not payload or MAIN_RED_KEY not in payload:
        return dict(payload) if payload is not None else None
    raw = payload[MAIN_RED_KEY]
    if not isinstance(raw, Mapping):
        raise ValueError("payload main_red must be an object {repo, check, test or sha}")
    repo, check, test = _clean(raw.get("repo")), _clean(raw.get("check")), _clean(raw.get("test"))
    sha = _clean(raw.get("sha"))
    if not repo or "/" not in repo:
        raise ValueError("payload main_red.repo must be the repository as owner/name")
    if not check:
        raise ValueError("payload main_red.check must name the failing check")
    if sha and not _SHA.fullmatch(sha):
        raise ValueError("payload main_red.sha must be a commit SHA (7 to 40 hexadecimal characters)")
    if not test and not sha:
        raise ValueError("payload main_red needs the failing test, else the red commit sha")
    red = {**raw, "repo": repo, "check": check}
    if test:
        red["test"] = test
    if sha:
        red["sha"] = sha
    red[DERIVED_FIELD] = red_key(repo, check, test=test, sha=sha)
    return {**payload, MAIN_RED_KEY: red}


def red_key(repo: str, check: str, *, test: str = "", sha: str = "") -> str:
    """The carrier key of one red: repository, check, then the test, else the SHA.

    A SHA is cut to its first 7 characters so a full and a short SHA of the
    same commit give one key.
    """
    subject = f"test:{_clean(test)}" if _clean(test) else f"sha:{_clean(sha)[:7]}"
    return f"{_clean(repo)}|{_clean(check)}|{subject}"


def main_red_key_of(payload: Mapping[str, Any] | None) -> str | None:
    """The derived key an item's payload carries, else ``None``."""
    red = (payload or {}).get(MAIN_RED_KEY)
    if not isinstance(red, Mapping):
        return None
    key = red.get(DERIVED_FIELD)
    return key if isinstance(key, str) and key else None


def same_red_text(items: list[dict[str, Any]], own_run_ids: tuple[object, ...] = ()) -> str | None:
    """The line ``get_work_item`` adds when other items carry the same red.

    *items* are the other unfinished carriers (``same_red`` of the item): a
    live run that is not the reader's makes it a stand-down line, else the
    line only names them.
    """
    if not items:
        return None
    own = {str(rid).lower() for rid in own_run_ids if rid}
    for other in items:
        run = other.get("claimed_by_run") or {}
        if run.get("id") and run.get("alive") and str(run["id"]).lower() not in own:
            return (
                f"  - SAME RED ON MAIN HELD BY ANOTHER RUN: item `{other['id']}` "
                f"({other.get('correlation_key') or 'no key'}, {other.get('status')}) is held by live "
                f"run `{run['id']}`. Stand down: write one note on that item, not a second fix."
            )
    names = ", ".join(f"`{other['id']}` ({other.get('status')})" for other in items)
    return f"  - same red on main also carried by: {names}"
