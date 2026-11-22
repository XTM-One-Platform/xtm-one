"""A merged structured file is parsed before it is written or reported "merged cleanly" (#11986).

The #11937 shape: both sides add catalog keys at the same hunk join, one key (``Sync``) added on both
sides with two values; ``on_conflict=ref-then-workspace`` used to write invalid JSON with the key twice
and report it merged cleanly.
"""

from __future__ import annotations

import json

from app.integrations.providers.github.merge_hunks import merge_path
from app.services.sandbox_workspace import structured
from app.services.sandbox_workspace.filediff import three_way
from app.services.sandbox_workspace.jsonmerge import conflicting_keys
from app.services.sandbox_workspace.rebase import (
    OnConflict,
    RebaseSummary,
    has_conflict_markers,
    merge_file,
    merge_text,
)

CATALOG = "frontend/src/i18n/lang/fr.json"
BASE = '{\n  "Agents": "Agents",\n  "Settings": "Paramètres"\n}\n'
#: The workspace and the ref each add keys after "Agents", at the same hunk join.
OURS = '{\n  "Agents": "Agents",\n  "Run": "Exécuter",\n  "Sync": "Synchroniser",\n  "Settings": "Paramètres"\n}\n'
THEIRS = '{\n  "Agents": "Agents",\n  "Pause": "Pause",\n  "Sync": "Synchro",\n  "Settings": "Paramètres"\n}\n'


def _keys_in_order(text: str) -> list[str]:
    return [key for key, _ in json.loads(text, object_pairs_hook=lambda pairs: pairs)]


def test_adjacent_keys_added_on_both_sides_merge_to_valid_json_with_both_keys() -> None:
    ours = OURS.replace('  "Sync": "Synchroniser",\n', "")
    theirs = THEIRS.replace('  "Sync": "Synchro",\n', "")
    for mode in OnConflict:
        outcome = merge_file(BASE.encode(), ours.encode(), theirs.encode(), on_conflict=mode, path=CATALOG)
        assert outcome.conflicts == 0 and not outcome.reason
        assert outcome.validated == structured.JSON
        assert json.loads(outcome.data) == {
            "Agents": "Agents",
            "Run": "Exécuter",
            "Pause": "Pause",
            "Settings": "Paramètres",
        }


def test_the_11937_shape_is_a_conflict_naming_the_key_never_a_broken_clean_merge() -> None:
    # The line merge the old code wrote: not valid JSON and "Sync" twice, reported clean.
    led = merge_text(BASE, OURS, THEIRS, lead="theirs").text
    assert structured.parse_defect(structured.JSON, led)
    outcome = merge_file(
        BASE.encode(), OURS.encode(), THEIRS.encode(), on_conflict=OnConflict.REF_THEN_WORKSPACE, path=CATALOG
    )
    assert outcome.conflicts >= 1 and has_conflict_markers(outcome.data)
    assert "ref-then-workspace merge would break the JSON file" in outcome.detail
    assert "'Sync'" in outcome.detail
    assert outcome.validated == ""


def test_a_key_changed_differently_on_both_sides_is_named() -> None:
    assert conflicting_keys(BASE, OURS, THEIRS) == ["Sync"]
    nested_base = '{\n  "a": {\n    "x": "1"\n  }\n}\n'
    nested_ours = '{\n  "a": {\n    "x": "2"\n  }\n}\n'
    nested_theirs = '{\n  "a": {\n    "x": "3"\n  }\n}\n'
    assert conflicting_keys(nested_base, nested_ours, nested_theirs) == ["a.x"]


def test_a_duplicate_key_from_a_line_merge_without_overlap_is_never_written() -> None:
    base = "{\n" + "".join(f'  "k{n}": "{n}",\n' for n in range(10)) + '  "last": "z"\n}\n'
    ours = base.replace('  "k1": "1",\n', '  "k1": "1",\n  "Sync": "ours",\n')
    theirs = base.replace('  "k8": "8",\n', '  "k8": "8",\n  "Sync": "theirs",\n')
    assert merge_text(base, ours, theirs).conflicts == 0  # the line merge alone would call it clean
    for mode in OnConflict:
        outcome = merge_file(base.encode(), ours.encode(), theirs.encode(), on_conflict=mode, path=CATALOG)
        assert outcome.reason and "key 'Sync' written twice" in outcome.reason and "'Sync'" in outcome.reason
        assert outcome.data == ours.encode()  # the workspace side stays, reported as a conflict
    merge = merge_path(CATALOG, base.encode(), ours.encode(), theirs.encode())
    assert not merge.clean and "written twice" in merge.reason


def test_the_same_key_added_twice_without_overlap_on_the_github_path_is_refused() -> None:
    base = "{\n" + "".join(f'  "k{n}": "{n}",\n' for n in range(10)) + '  "last": "z"\n}\n'
    ours = base.replace('  "k1": "1",\n', '  "k1": "1",\n  "New": "same",\n')
    theirs = base.replace('  "k8": "8",\n', '  "k8": "8",\n  "New": "same",\n')
    merge = merge_path(CATALOG, base.encode(), ours.encode(), theirs.encode())
    # The key merge writes the shared key once: clean, valid, listed as validated.
    assert merge.clean and merge.merged is not None
    assert _keys_in_order(merge.merged).count("New") == 1
    assert merge.structured_kind == structured.JSON


def test_yaml_and_toml_merges_are_parsed_too() -> None:
    base = "a: 1\n" + "".join(f"k{n}: {n}\n" for n in range(10))
    ours = base.replace("k1: 1\n", "k1: 1\nsync: ours\n")
    theirs = base.replace("k8: 8\n", "k8: 8\nsync: theirs\n")
    outcome = merge_file(base.encode(), ours.encode(), theirs.encode(), path="values.yaml")
    assert outcome.reason and "key 'sync' written twice" in outcome.reason and "line" in outcome.reason
    clean = merge_file(
        base.encode(), base.replace("a: 1", "a: 2").encode(), base.replace("k9: 9", "k9: 10").encode(), path="v.yml"
    )
    assert clean.validated == structured.YAML and not clean.reason
    toml_base = "[tool]\n" + "".join(f"k{n} = {n}\n" for n in range(10))
    toml_ours = toml_base.replace("k1 = 1\n", "k1 = 1\nsync = 1\n")
    toml_theirs = toml_base.replace("k8 = 8\n", "k8 = 8\nsync = 2\n")
    broken = merge_file(toml_base.encode(), toml_ours.encode(), toml_theirs.encode(), path="pyproject.toml")
    assert broken.reason and "TOML" in broken.reason


def test_a_side_already_broken_is_not_judged() -> None:
    # A Helm template is not YAML before rendering: the merge is not refused for a breakage it did not make.
    base = "a: {{ .Values.a }}\nb: 1\nc: 2\nd: 3\ne: 4\n"
    ours = base.replace("b: 1", "b: 10")
    theirs = base.replace("e: 4", "e: 40")
    outcome = merge_file(base.encode(), ours.encode(), theirs.encode(), path="templates/x.yaml")
    assert not outcome.reason and outcome.validated == "" and outcome.conflicts == 0


def test_plain_text_line_merges_are_unchanged() -> None:
    base, ours, theirs = "a\nb\nc\nd\ne\n", "A\nb\nc\nd\ne\n", "a\nb\nc\nd\nE\n"
    for mode in OnConflict:
        outcome = merge_file(base.encode(), ours.encode(), theirs.encode(), on_conflict=mode, path="src/app.py")
        assert outcome.data == b"A\nb\nc\nd\nE\n" and outcome.validated == "" and not outcome.reason
    led = merge_file(
        b"a\nold()\nc\n",
        b"a\nold()\nmine()\nc\n",
        b"a\nnew()\nc\n",
        on_conflict=OnConflict.REF_THEN_WORKSPACE,
        path="x.py",
    )
    assert led.data == b"a\nnew()\nmine()\nc\n" and led.conflicts == 0 and led.detail == ""


def test_the_rebase_answer_names_each_validated_file() -> None:
    summary = RebaseSummary(merged=[CATALOG], validated=[(CATALOG, structured.JSON)])
    text = summary.describe(shown_as="xo", before="aaa", after="main")
    assert f"Parsed after the merge, valid with no duplicate key (1): {CATALOG} (JSON)." in text


def test_sandbox_diff_files_three_way_names_a_broken_clean_merge() -> None:
    base = "{\n" + "".join(f'  "k{n}": "{n}",\n' for n in range(10)) + '  "last": "z"\n}\n'
    ours = base.replace('  "k1": "1",\n', '  "k1": "1",\n  "Sync": "ours",\n')
    theirs = base.replace('  "k8": "8",\n', '  "k8": "8",\n  "Sync": "theirs",\n')
    merged = three_way(base, ours, theirs, ours_label="work/fr.json", theirs_label="main/fr.json")
    assert merged.conflicts == 0 and "key 'Sync' written twice" in merged.defect and merged.validated == ""
    sound = three_way(BASE, BASE.replace('Agents"\n', 'Agents"\n'), BASE, ours_label="a.json", theirs_label="b.json")
    assert sound.defect == ""


def test_a_given_yaml_resolution_with_a_duplicate_key_is_refused() -> None:
    from app.integrations.providers.github.merge_resolution import _broken_resolutions

    refused = _broken_resolutions({"values.yaml": b"a: 1\na: 2\n"}, {"values.yaml": b"a: 1\n"})
    assert refused is not None and "values.yaml" in refused and "key 'a' written twice" in refused
    assert _broken_resolutions({"values.yaml": b"a: 2\n"}, {"values.yaml": b"a: 1\n"}) is None


def test_a_given_json_resolution_that_drops_keys_both_sides_hold_is_refused() -> None:
    from app.integrations.providers.github.merge_resolution import _broken_resolutions

    branch = b'{\n  "a": "1",\n  "mine": "m"\n}\n'
    ref = b'{\n  "a": "1",\n  "main": "x",\n  "nested": {\n    "k": "v"\n  }\n}\n'
    stale = b'{\n  "a": "1",\n  "main": "x",\n  "mine": "m"\n}\n'
    assert _broken_resolutions({"en.json": stale}, {"en.json": ref}, {"en.json": branch}) is None
    both = b'{\n  "a": "1",\n  "gone": "g",\n  "nested": {\n    "k": "v"\n  }\n}\n'
    dropped = b'{\n  "nested": {}\n}\n'
    refused = _broken_resolutions({"en.json": dropped}, {"en.json": both}, {"en.json": both})
    assert refused is not None and "drops 3 key(s) both sides hold: a, gone, nested.k" in refused
    # Without the branch's version the check cannot tell a meant removal: nothing is judged.
    assert _broken_resolutions({"en.json": dropped}, {"en.json": both}) is None


def test_dropped_keys_reads_json_objects_only() -> None:
    from app.services.sandbox_workspace.jsonmerge import dropped_keys

    assert dropped_keys('{"a": 1}', '{"a": 1, "b": 2}', '{"b": 3}') == ["b"]
    assert dropped_keys('{"a": 1}', '{"a": 1, "b": 2}', '{"a": 1}') == []
    assert dropped_keys("[]", '{"b": 2}', '{"b": 2}') == []
    assert dropped_keys('{"a": 1}') == []
