"""The key-by-key merge of JSON object files (the i18n catalogs) in rebase and the server line merge."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.integrations.providers.github.merge_hunks import merge_path
from app.services.sandbox_workspace.jsonmerge import merge_json_objects
from app.services.sandbox_workspace.rebase import merge_file, merge_text


def _cat(**pairs: object) -> str:
    return json.dumps(pairs, indent=2, ensure_ascii=False) + "\n"


BASE = _cat(Hello="Bonjour", Bye="Au revoir")
OURS = _cat(Hello="Bonjour", Bye="Au revoir", Save="Enregistrer")
THEIRS = _cat(Hello="Bonjour", Bye="Au revoir", Open="Ouvrir")


def test_keys_appended_on_both_sides_conflict_by_line_and_merge_by_key():
    assert merge_text(BASE, OURS, THEIRS).conflicts == 1
    merged = merge_json_objects(BASE, OURS, THEIRS)
    assert merged == _cat(Hello="Bonjour", Bye="Au revoir", Save="Enregistrer", Open="Ouvrir")


def test_a_key_added_by_the_ref_goes_after_its_predecessor_there():
    theirs = _cat(Hello="Bonjour", Open="Ouvrir", Bye="Au revoir")
    ours = _cat(Hello="Salut", Bye="Au revoir", Save="Enregistrer")
    merged = merge_json_objects(BASE, ours, theirs)
    assert list(json.loads(merged)) == ["Hello", "Open", "Bye", "Save"]
    assert json.loads(merged)["Hello"] == "Salut"


def test_one_side_changes_and_the_other_removes_another_key():
    ours = _cat(Hello="Salut", Bye="Au revoir")
    theirs = _cat(Hello="Bonjour")
    assert merge_json_objects(BASE, ours, theirs) == _cat(Hello="Salut")


def test_the_same_change_on_both_sides_is_taken_once():
    side = _cat(Hello="Salut", Bye="Au revoir", Save="Enregistrer")
    assert merge_json_objects(BASE, side, side) == side


@pytest.mark.parametrize(
    ("ours", "theirs"),
    [
        (_cat(Hello="Salut", Bye="Au revoir"), _cat(Hello="Coucou", Bye="Au revoir")),
        (_cat(Hello="Salut", Bye="Au revoir"), _cat(Bye="Au revoir")),
        (_cat(Hello="Bonjour", Bye="Au revoir", New="A"), _cat(Hello="Bonjour", Bye="Au revoir", New="B")),
    ],
    ids=["changed-differently", "removed-against-changed", "added-differently"],
)
def test_a_real_conflict_keeps_the_line_merge(ours: str, theirs: str):
    assert merge_json_objects(BASE, ours, theirs) is None


def test_nested_objects_merge_recursively():
    base = _cat(menu={"a": "A"})
    ours = _cat(menu={"a": "A", "b": "B"})
    theirs = _cat(menu={"a": "A", "c": "C"})
    assert json.loads(merge_json_objects(base, ours, theirs)) == {"menu": {"a": "A", "b": "B", "c": "C"}}


@pytest.mark.parametrize(
    "ours",
    [
        '{\n  "Hello": "Bonjour",\n  "Bye": "Au revoir",\n  "Save":   "Enregistrer"\n}\n',
        '{\n  "Hello": "Bonjour",\n  "Hello": "Salut",\n  "Bye": "Au revoir"\n}\n',
        "[1, 2]\n",
        "not json\n",
    ],
    ids=["layout-not-reproduced", "duplicate-key", "not-an-object", "not-json"],
)
def test_an_uncertain_file_keeps_the_line_merge(ours: str):
    assert merge_json_objects(BASE, ours, THEIRS) is None


def test_the_layout_is_kept_four_spaces_ascii_no_final_newline():
    def dump(**pairs: object) -> str:
        return json.dumps(pairs, indent=4, ensure_ascii=True)

    merged = merge_json_objects(dump(a="é"), dump(a="é", b="1"), dump(a="é", c="2"))
    assert merged == dump(a="é", b="1", c="2")


def test_added_on_both_sides_merges_from_an_empty_object():
    assert merge_json_objects("", _cat(a="1"), _cat(b="2")) == _cat(a="1", b="2")


def test_rebase_merge_file_writes_the_key_merge_without_markers():
    outcome = merge_file(BASE.encode(), OURS.encode(), THEIRS.encode())
    assert outcome.conflicts == 0 and not outcome.reason
    assert b"<<<<<<<" not in outcome.data
    assert json.loads(outcome.data) == {"Hello": "Bonjour", "Bye": "Au revoir", "Save": "Enregistrer", "Open": "Ouvrir"}


def test_rebase_merge_file_still_marks_a_real_conflict():
    outcome = merge_file(BASE.encode(), _cat(Hello="Salut", Bye="x").encode(), _cat(Hello="Coucou", Bye="x").encode())
    assert outcome.conflicts == 1 and b"<<<<<<< workspace" in outcome.data


def test_server_line_merge_resolves_a_catalog_without_hunks():
    merge = merge_path("frontend/src/i18n/lang/fr.json", BASE.encode(), OURS.encode(), THEIRS.encode())
    assert merge.clean
    assert json.loads(merge.merged or "") == json.loads(merge_json_objects(BASE, OURS, THEIRS) or "")


_CATALOG = Path(__file__).resolve().parents[2] / "frontend" / "src" / "i18n" / "lang" / "en.json"


@pytest.mark.skipif(not _CATALOG.is_file(), reason="the frontend catalogs are not in this checkout")
def test_the_real_english_catalog_is_written_back_byte_for_byte():
    text = _CATALOG.read_text(encoding="utf-8")
    data = json.loads(text)
    keys = list(data)
    ours = dict(data, **{"zz ours key": "Ours"})
    theirs = dict(data, **{"zz theirs key": "Theirs"})
    merged = merge_json_objects(
        text,
        json.dumps(ours, indent=2, ensure_ascii=False) + "\n",
        json.dumps(theirs, indent=2, ensure_ascii=False) + "\n",
    )
    assert merged is not None, "en.json is not in the json.dumps(indent=2, ensure_ascii=False) layout"
    merged_keys = list(json.loads(merged))
    assert len(merged_keys) == len(keys) + 2
    assert merged_keys[-3:] == [keys[-1], "zz ours key", "zz theirs key"]
    assert merged_keys[: len(keys)] == keys
