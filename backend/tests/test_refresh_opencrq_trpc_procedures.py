"""refresh_opencrq_trpc_procedures pins a manifest already fetched without the gh CLI.

An agent sandbox has no ``gh``: ``--manifest PATH --commit SHA`` reads the copy
fetched through any authorised client and writes the same pin as the gh path.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import refresh_opencrq_trpc_procedures as refresh

_MANIFEST = {
    "formatVersion": refresh.MANIFEST_FORMAT,
    "procedures": [
        {"path": "riskScenarios.list", "type": "query", "input": {"type": "object"}},
        {"path": "riskScenarios.create", "type": "mutation"},
    ],
}


@pytest.fixture
def pinned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    target = tmp_path / "opencrq_trpc_procedures.json"
    monkeypatch.setattr(refresh, "PINNED", target)
    monkeypatch.setattr(refresh, "_gh_api", lambda *a, **k: pytest.fail("gh must not be called"))
    return target


def test_a_fetched_manifest_is_pinned_without_gh(tmp_path: Path, pinned: Path) -> None:
    source = tmp_path / "trpc-procedures.json"
    source.write_text(json.dumps(_MANIFEST), encoding="utf-8")

    assert refresh.main(["--manifest", str(source), "--commit", "c" * 40, "--check"]) == 1
    assert not pinned.exists()

    assert refresh.main(["--manifest", str(source), "--commit", "c" * 40]) == 0
    data = json.loads(pinned.read_text(encoding="utf-8"))
    assert data["pinnedFrom"]["commit"] == "c" * 40
    assert data["procedures"] == [
        {"path": "riskScenarios.list", "type": "query"},
        {"path": "riskScenarios.create", "type": "mutation"},
    ]
    assert refresh.main(["--manifest", str(source), "--commit", "c" * 40, "--check"]) == 0


def test_manifest_without_commit_is_refused(tmp_path: Path, pinned: Path) -> None:
    source = tmp_path / "trpc-procedures.json"
    source.write_text(json.dumps(_MANIFEST), encoding="utf-8")
    with pytest.raises(SystemExit):
        refresh.main(["--manifest", str(source)])
