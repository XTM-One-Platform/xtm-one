"""``sandbox_json_keys``: the pure key changes (``jsonkeys.py``) and the tool over a workspace."""

from __future__ import annotations

import json

import pytest

from app.services.sandbox_workspace import jsonkeys

EN = '{\n  "Save": "Save",\n  "Cancel": "Cancel"\n}\n'
FR = '{\n  "Save": "Enregistrer",\n  "Cancel": "Annuler"\n}\n'


def _by_path(changes: list[jsonkeys.FileChange]) -> dict[str, jsonkeys.FileChange]:
    return {c.path: c for c in changes}


def test_set_adds_a_new_key_last_and_keeps_an_existing_key_in_place():
    out = _by_path(
        jsonkeys.change_keys(
            {"en.json": EN, "fr.json": FR},
            set_keys={"en.json": {"Delete": "Delete", "Save": "Save it"}, "fr.json": {"Delete": "Supprimer"}},
        )
    )
    assert out["en.json"].text == '{\n  "Save": "Save it",\n  "Cancel": "Cancel",\n  "Delete": "Delete"\n}\n'
    assert out["en.json"].added == ["Delete"] and out["en.json"].changed == ["Save"]
    assert list(json.loads(out["fr.json"].text)) == ["Save", "Cancel", "Delete"]


def test_star_sets_the_key_in_every_file():
    out = jsonkeys.change_keys({"en.json": EN, "fr.json": FR}, set_keys={"*": {"Beta": "Beta"}})
    assert all(json.loads(c.text)["Beta"] == "Beta" for c in out)


def test_rename_keeps_the_place_and_the_value():
    out = _by_path(jsonkeys.change_keys({"en.json": EN, "fr.json": FR}, rename={"Save": "Save changes"}))
    assert out["fr.json"].text == '{\n  "Save changes": "Enregistrer",\n  "Cancel": "Annuler"\n}\n'
    assert out["fr.json"].renamed == ["Save -> Save changes"]


def test_rename_of_a_missing_key_or_onto_an_existing_one_is_refused():
    with pytest.raises(jsonkeys.JsonKeysError, match="no key"):
        jsonkeys.change_keys({"en.json": EN}, rename={"Nope": "Yes"})
    with pytest.raises(jsonkeys.JsonKeysError, match="already has"):
        jsonkeys.change_keys({"en.json": EN}, rename={"Save": "Cancel"})


def test_remove_reports_an_absent_key_and_drops_a_present_one():
    (change,) = jsonkeys.change_keys({"en.json": EN}, remove=["Cancel", "Gone"])
    assert change.removed == ["Cancel"] and change.absent == ["Gone"]
    assert change.text == '{\n  "Save": "Save"\n}\n'
    assert "absent 1" in change.line()


def test_get_reads_without_writing():
    (change,) = jsonkeys.change_keys({"en.json": EN}, get=["Save", "Gone"])
    assert not change.modified and change.text == EN
    assert jsonkeys.describe_values(change) == ['  "Save": "Save"', '  "Gone": (absent)']


def test_the_layout_is_kept_byte_for_byte():
    ascii_tab = '{\n\t"caf\\u00e9": "x"\n}'
    (change,) = jsonkeys.change_keys({"a.json": ascii_tab}, set_keys={"a.json": {"thé": "y"}})
    assert change.text == '{\n\t"caf\\u00e9": "x",\n\t"th\\u00e9": "y"\n}'


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ('{"a": 1, "a": 2}', "twice"),
        ("[1, 2]", "not a JSON object"),
        ('{\n  "a": 1,\n    "b": 2\n}\n', "layout"),
    ],
)
def test_a_file_that_cannot_be_written_back_is_refused(text, message):
    with pytest.raises(jsonkeys.JsonKeysError, match=message):
        jsonkeys.change_keys({"a.json": text}, set_keys={"*": {"c": 3}})


def test_one_refused_file_refuses_the_whole_call():
    with pytest.raises(jsonkeys.JsonKeysError):
        jsonkeys.change_keys({"en.json": EN, "bad.json": "[]"}, set_keys={"*": {"k": "v"}})


def test_place_puts_a_new_key_after_or_before_a_named_key():
    (change,) = jsonkeys.change_keys(
        {"en.json": EN},
        set_keys={"*": {"Save all": "Save all", "Open": "Open"}},
        place={"Save all": {"after": "Save"}, "Open": {"before": "Save"}},
    )
    assert list(json.loads(change.text)) == ["Open", "Save", "Save all", "Cancel"]
    assert change.text.startswith('{\n  "Open": "Open",\n  "Save": "Save",\n  "Save all": "Save all",\n')
    assert change.added == ["Save all", "Open"]


def test_place_of_an_existing_key_leaves_it_in_place():
    (change,) = jsonkeys.change_keys(
        {"en.json": EN}, set_keys={"*": {"Save": "Keep"}}, place={"Save": {"after": "Cancel"}}
    )
    assert list(json.loads(change.text)) == ["Save", "Cancel"] and change.changed == ["Save"]


def test_place_is_refused_on_a_missing_anchor_or_a_bad_shape():
    with pytest.raises(jsonkeys.JsonKeysError, match="to place"):
        jsonkeys.change_keys({"en.json": EN}, set_keys={"*": {"k": "v"}}, place={"k": {"after": "Nope"}})
    with pytest.raises(jsonkeys.JsonKeysError, match="set does not give"):
        jsonkeys.change_keys({"en.json": EN}, set_keys={"*": {"k": "v"}}, place={"x": {"after": "Save"}})
    with pytest.raises(jsonkeys.JsonKeysError, match="must be"):
        jsonkeys.change_keys({"en.json": EN}, set_keys={"*": {"k": "v"}}, place={"k": {"near": "Save"}})
    with pytest.raises(jsonkeys.JsonKeysError, match="another key"):
        jsonkeys.change_keys({"en.json": EN}, set_keys={"*": {"k": "v"}}, place={"k": {"after": "k"}})


def test_one_call_adds_beside_renames_with_new_values_and_drops_per_locale():
    """The catalog edit of xtm-one #9217: what used to need code_interpreter is one call."""
    en = '{\n  "Activate License": "Activate License",\n  "Old A": "A",\n  "Old B": "B",\n  "Gone": "x"\n}\n'
    fr = '{\n  "Activate License": "Activer la licence",\n  "Old A": "A fr",\n  "Old B": "B fr",\n  "Gone": "x"\n}\n'
    out = _by_path(
        jsonkeys.change_keys(
            {"en.json": en, "fr.json": fr},
            rename={"Old A": "New A", "Old B": "New B"},
            set_keys={
                "en.json": {"Activate license": "Activate license", "New A": "A2", "New B": "B2"},
                "fr.json": {"Activate license": "Activer la licence", "New A": "A2 fr", "New B": "B2 fr"},
            },
            place={"Activate license": {"after": "Activate License"}},
            remove=["Gone"],
        )
    )
    assert out["fr.json"].text == (
        '{\n  "Activate License": "Activer la licence",\n  "Activate license": "Activer la licence",\n'
        '  "New A": "A2 fr",\n  "New B": "B2 fr"\n}\n'
    )
    assert list(json.loads(out["en.json"].text).values()) == ["Activate License", "Activate license", "A2", "B2"]
    assert out["en.json"].renamed == ["Old A -> New A", "Old B -> New B"]
    assert out["en.json"].changed == ["New A", "New B"]


def test_bad_requests_are_refused():
    with pytest.raises(jsonkeys.JsonKeysError, match="nothing to do"):
        jsonkeys.change_keys({"en.json": EN})
    with pytest.raises(jsonkeys.JsonKeysError, match="not in paths"):
        jsonkeys.change_keys({"en.json": EN}, set_keys={"fr.json": {"k": "v"}})
    with pytest.raises(jsonkeys.JsonKeysError, match="same new name"):
        jsonkeys.change_keys({"en.json": EN}, rename={"Save": "X", "Cancel": "X"})
    with pytest.raises(jsonkeys.JsonKeysError, match="list of keys"):
        jsonkeys.change_keys({"en.json": EN}, remove="Save")
    with pytest.raises(jsonkeys.JsonKeysError, match="french_spacing"):
        jsonkeys.change_keys({"en.json": EN}, set_keys={"*": {"k": "v"}}, french_spacing="auto")


def test_the_key_cap_is_per_file_not_per_call() -> None:
    texts = {f"{lang}.json": EN for lang in ("en", "fr", "es", "ja", "de", "it", "ko", "zh", "ru")}
    sets = {path: {f"k{i}": f"{path} {i}" for i in range(83)} for path in texts}
    out = _by_path(jsonkeys.change_keys(texts, set_keys=sets))
    assert len(out) == 9
    assert json.loads(out["ja.json"].text)["k82"] == "ja.json 82"
    heavy = {"fr.json": {f"k{i}": "v" for i in range(400)}, "*": {f"s{i}": "v" for i in range(101)}}
    with pytest.raises(jsonkeys.JsonKeysError, match=r"501 keys named for fr\.json, at most 500 per file"):
        jsonkeys.change_keys({"en.json": EN, "fr.json": EN}, set_keys=heavy)


PLURAL = "{count, plural, one {# action : {names}} other {# actions : {names}}}"


def test_a_plain_space_before_a_mark_in_a_french_value_is_reported_by_key():
    out = _by_path(
        jsonkeys.change_keys(
            {"en.json": EN, "fr.json": FR},
            set_keys={"*": {"Sure?": "Vraiment ?"}, "fr.json": {"Count": PLURAL, "Plain": "Fine"}},
        )
    )
    fr = out["fr.json"]
    assert fr.spacing == [("Sure?", "?"), ("Count", ":")]
    assert not fr.spacing_fixed
    assert json.loads(fr.text)["Count"] == PLURAL
    line = fr.spacing_line()
    assert line.startswith("  WARNING French spacing") and '"Count" before :' in line and "french_spacing" in line
    assert out["en.json"].spacing == [] and out["en.json"].spacing_line() == ""


def test_fix_writes_the_no_break_space_each_mark_takes_in_french_catalogs_only():
    out = _by_path(
        jsonkeys.change_keys(
            {"locales/en.json": EN, "locales/fr.json": FR, "locales/es.json": EN, "locales/ja.json": EN},
            set_keys={"*": {"Ask": "Supprimer ? Oui ; non ! Note : ok", "Count": PLURAL}},
            french_spacing="fix",
        )
    )
    fr = json.loads(out["locales/fr.json"].text)
    assert fr["Ask"] == "Supprimer\u202f? Oui\u202f; non\u202f! Note\u00a0: ok"
    assert fr["Count"] == PLURAL.replace(" :", "\u00a0:")
    assert out["locales/fr.json"].spacing_fixed and "fixed" in out["locales/fr.json"].spacing_line()
    for other in ("locales/en.json", "locales/es.json", "locales/ja.json"):
        assert json.loads(out[other].text)["Ask"] == "Supprimer ? Oui ; non ! Note : ok"
        assert out[other].spacing == []


def test_a_mark_that_does_not_end_a_word_is_left_alone():
    values = {
        "help": "Rechercher une page, ou taper > pour les commandes, ? pour l'aide",
        "ipv6": "venant de cette machine (127.0.0.1, ::1)",
        "uri": "une URI otpauth:// ; les codes",
        "done": "Supprimer l'entrée\u202f?",
        "nested": {"a": ["Oui ?"]},
    }
    out = jsonkeys.change_keys({"fr.json": FR}, set_keys={"fr.json": values}, french_spacing="fix")[0]
    assert out.spacing == [("nested", "?")]
    written = json.loads(out.text)
    assert written["help"] == values["help"] and written["uri"] == values["uri"] and written["ipv6"] == values["ipv6"]
    assert written["nested"] == {"a": ["Oui\u202f?"]}


CONFIG = '{\n  "server": {\n    "port": 80,\n    "hosts": [\n      "a"\n    ]\n  },\n  "/raw": 1\n}\n'


def test_a_pointer_sets_a_nested_value_in_place_and_keeps_the_layout():
    """#11483: a nested config value, once a code_interpreter json.dump, is one set."""
    (change,) = jsonkeys.change_keys(
        {"config.json": CONFIG},
        set_keys={"*": {"/server/port": 8080, "/server/hosts/-": "b", "/server/tls": True}},
        place={"/server/tls": {"after": "port"}},
    )
    assert change.text == (
        '{\n  "server": {\n    "port": 8080,\n    "tls": true,\n    "hosts": [\n      "a",\n      "b"\n    ]\n  },\n'
        '  "/raw": 1\n}\n'
    )
    assert change.changed == ["/server/port"] and change.added == ["/server/hosts/-", "/server/tls"]


def test_a_pointer_removes_and_reads_nested_values():
    (change,) = jsonkeys.change_keys(
        {"config.json": CONFIG}, remove=["/server/hosts/0", "/server/nope", "/missing/x"], get=["/server/port", "/raw"]
    )
    assert change.removed == ["/server/hosts/0"] and change.absent == ["/server/nope", "/missing/x"]
    assert json.loads(change.text)["server"]["hosts"] == []
    assert change.values == {"/server/port": 80, "/raw": 1}


def test_a_top_level_key_that_starts_with_a_slash_keeps_its_literal_meaning():
    (change,) = jsonkeys.change_keys({"config.json": CONFIG}, set_keys={"*": {"/raw": 2}})
    assert json.loads(change.text)["/raw"] == 2 and change.changed == ["/raw"]


def test_a_pointer_never_creates_a_parent_nor_an_index_past_the_end():
    with pytest.raises(jsonkeys.JsonKeysError, match="does not exist"):
        jsonkeys.change_keys({"c.json": CONFIG}, set_keys={"*": {"/nope/x": 1}})
    with pytest.raises(jsonkeys.JsonKeysError, match="not an item"):
        jsonkeys.change_keys({"c.json": CONFIG}, set_keys={"*": {"/server/hosts/5": "x"}})
    with pytest.raises(jsonkeys.JsonKeysError, match="not an object or an array"):
        jsonkeys.change_keys({"c.json": CONFIG}, set_keys={"*": {"/server/port/x": 1}})


@pytest.mark.parametrize(
    ("path", "french"),
    [
        ("fr.json", True),
        ("src/i18n/lang/fr-FR.json", True),
        ("fr_CA.json", True),
        ("FR.json", True),
        ("en.json", False),
        ("fra.json", False),
        ("chef.json", False),
        ("fr.yaml", False),
    ],
)
def test_french_catalog_names(path, french):
    assert jsonkeys.is_french_catalog(path) is french
