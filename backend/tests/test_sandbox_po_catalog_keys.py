"""``sandbox_json_keys`` on gettext ``.po`` catalogs (``pocatalog.py``, #1284)."""

from __future__ import annotations

import difflib

import pytest

from app.services.sandbox_workspace import jsonkeys, pocatalog

FR = (
    'msgid ""\n'
    'msgstr ""\n'
    '"Content-Type: text/plain; charset=utf-8\\n"\n'
    '"Language: fr\\n"\n'
    "\n"
    'msgid " of the estate tested"\n'
    'msgstr " du parc testé"\n'
    "\n"
    "#. A translator note kept with its entry.\n"
    "#: src/pages/Assets.tsx:12\n"
    'msgid "Assets"\n'
    'msgstr "Actifs"\n'
    "\n"
    'msgid ""\n'
    '"A long message "\n'
    '"on two lines"\n'
    'msgstr ""\n'
    '"Un long message "\n'
    '"sur deux lignes"\n'
    "\n"
    'msgctxt "verb"\n'
    'msgid "Open"\n'
    'msgstr "Ouvrir"\n'
    "\n"
    'msgid "{count} asset"\n'
    'msgid_plural "{count} assets"\n'
    'msgstr[0] "{count} actif"\n'
    'msgstr[1] "{count} actifs"\n'
    "\n"
    'msgid "Say \\"hi\\""\n'
    'msgstr "Dire \\"salut\\""\n'
    "\n"
    '#~ msgid "Old"\n'
    '#~ msgstr "Ancien"\n'
)
EN = 'msgid ""\nmsgstr ""\n"Language: en\\n"\n\nmsgid "Assets"\nmsgstr "Assets"\n'
FR_PATH = "front/src/locales/fr/messages.po"
EN_PATH = "front/src/locales/en/messages.po"


def _changed_lines(before: str, after: str) -> list[str]:
    return [
        line
        for line in difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="", n=0)
        if line[:1] in "+-" and not line.startswith(("+++", "---"))
    ]


def test_one_call_adds_a_message_after_an_anchor_per_locale_touching_only_that_entry():
    out = {
        c.path: c
        for c in jsonkeys.change_keys(
            {FR_PATH: FR, EN_PATH: EN},
            set_keys={FR_PATH: {"Asset owner": "Propriétaire de l’actif"}, EN_PATH: {"Asset owner": "Asset owner"}},
            place={"Asset owner": {"after": "Assets"}},
            get=["Asset owner"],
        )
    }
    fr = out[FR_PATH].text
    assert _changed_lines(FR, fr) == ["+", '+msgid "Asset owner"', '+msgstr "Propriétaire de l’actif"']
    assert fr.index('msgstr "Actifs"') < fr.index('msgid "Asset owner"') < fr.index('"A long message "')
    assert out[EN_PATH].text == EN + '\nmsgid "Asset owner"\nmsgstr "Asset owner"\n'
    assert out[FR_PATH].added == ["Asset owner"] and out[FR_PATH].values == {"Asset owner": "Propriétaire de l’actif"}


def test_a_message_set_last_and_unchanged_values_leave_the_file_alone():
    out = jsonkeys.change_keys({FR_PATH: FR}, set_keys={"*": {"Assets": "Actifs", "Zebra": "Zèbre"}})[0]
    assert out.changed == [] and out.added == ["Zebra"]
    assert out.text == FR + '\nmsgid "Zebra"\nmsgstr "Zèbre"\n'
    same = jsonkeys.change_keys({FR_PATH: FR}, set_keys={"*": {"Assets": "Actifs"}})[0]
    assert not same.modified and same.text == FR


def test_an_existing_msgstr_changes_in_place_with_escapes_and_its_comments_kept():
    out = jsonkeys.change_keys({FR_PATH: FR}, set_keys={FR_PATH: {"Assets": 'Les "actifs"'}})[0]
    assert out.changed == ["Assets"]
    assert _changed_lines(FR, out.text) == ['-msgstr "Actifs"', '+msgstr "Les \\"actifs\\""']
    assert "#. A translator note kept with its entry.\n#: src/pages/Assets.tsx:12\nmsgid" in out.text


def test_get_reads_wrapped_context_plural_and_escaped_entries():
    out = jsonkeys.change_keys(
        {FR_PATH: FR}, get=["A long message on two lines", "verb\x04Open", "{count} asset", 'Say "hi"', "Old"]
    )[0]
    assert out.text == FR and not out.modified
    assert out.values["A long message on two lines"] == "Un long message sur deux lignes"
    assert out.values["verb\x04Open"] == "Ouvrir"
    assert out.values["{count} asset"] == {"msgstr[0]": "{count} actif", "msgstr[1]": "{count} actifs"}
    assert out.values['Say "hi"'] == 'Dire "salut"'
    assert repr(out.values["Old"]) == "(absent)"


def test_rename_and_remove_touch_only_their_entries():
    out = jsonkeys.change_keys(
        {FR_PATH: FR}, rename={"Assets": "Your assets"}, remove=["A long message on two lines", "{count} asset"]
    )[0]
    assert out.renamed == ["Assets -> Your assets"]
    assert out.removed == ["A long message on two lines", "{count} asset"]
    assert '#: src/pages/Assets.tsx:12\nmsgid "Your assets"\nmsgstr "Actifs"\n\nmsgctxt "verb"' in out.text
    assert 'msgstr "Ouvrir"\n\nmsgid "Say' in out.text
    assert out.text.endswith('#~ msgid "Old"\n#~ msgstr "Ancien"\n')


def test_a_multi_line_value_is_written_wrapped():
    out = jsonkeys.change_keys({EN_PATH: EN}, set_keys={"*": {"Two\nlines": "Two\nlines"}})[0]
    assert out.text.endswith('\nmsgid ""\n"Two\\n"\n"lines"\nmsgstr ""\n"Two\\n"\n"lines"\n')
    again = jsonkeys.change_keys({EN_PATH: out.text}, get=["Two\nlines"])[0]
    assert again.values["Two\nlines"] == "Two\nlines"


def test_french_spacing_is_checked_on_a_fr_locale_directory():
    reported = jsonkeys.change_keys({FR_PATH: FR}, set_keys={"*": {"Delete?": "Supprimer ?"}})[0]
    assert reported.spacing == [("Delete?", "?")]
    fixed = jsonkeys.change_keys({FR_PATH: FR}, set_keys={"*": {"Delete?": "Supprimer ?"}}, french_spacing="fix")[0]
    assert 'msgstr "Supprimer\u202f?"' in fixed.text
    assert jsonkeys.is_french_catalog("locales/fr/messages.po") and jsonkeys.is_french_catalog("po/fr.po")
    assert not jsonkeys.is_french_catalog("locales/en/messages.po")


@pytest.mark.parametrize(
    ("call", "message"),
    [
        ({"set_keys": {"*": {"{count} asset": "x"}}}, "plural"),
        ({"set_keys": {"*": {"n": 3}}}, "must be a string"),
        ({"set_keys": {"*": {"z": "Z"}}, "place": {"z": {"after": "Nope"}}}, "to place"),
        ({"rename": {"Nope": "X"}}, "to rename"),
        ({"rename": {"Assets": "verb\x04Assets"}}, "msgctxt"),
    ],
)
def test_a_call_that_cannot_apply_is_refused_whole(call, message):
    with pytest.raises(jsonkeys.JsonKeysError, match=message):
        jsonkeys.change_keys({FR_PATH: FR}, **call)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ('msgid "a"\r\nmsgstr "A"\r\n', "CRLF"),
        ('msgid "a"\nmsgstr "A"\nnot po\n', "not a PO line"),
        ('msgid "a"\n', "needs a msgid and a msgstr"),
    ],
)
def test_a_file_that_is_not_a_catalog_is_refused(text, message):
    with pytest.raises(jsonkeys.JsonKeysError, match=message):
        jsonkeys.change_keys({"x.po": text}, set_keys={"*": {"z": "Z"}})


LINGUI_DUPLICATE = (
    'msgid "Assets"\nmsgstr "Actifs"\n'
    "\n"
    "#. js-lingui-explicit-id\n"
    'msgid "{from} to {to}"\nmsgstr "{from} à {to}"\n'
    "\n"
    'msgid "{from} to {to}"\nmsgstr "du {from} au {to}"\n'
)


def test_a_msgid_present_twice_leaves_the_other_messages_editable():
    out = jsonkeys.change_keys(
        {FR_PATH: LINGUI_DUPLICATE}, set_keys={"*": {"Assets": "Les actifs", "Zebra": "Zèbre"}}, get=["Assets"]
    )[0]
    assert out.changed == ["Assets"] and out.added == ["Zebra"]
    assert _changed_lines(LINGUI_DUPLICATE, out.text) == [
        '-msgstr "Actifs"',
        '+msgstr "Les actifs"',
        "+",
        '+msgid "Zebra"',
        '+msgstr "Zèbre"',
    ]
    assert out.text.count('msgid "{from} to {to}"') == 2


@pytest.mark.parametrize(
    "call",
    [
        {"set_keys": {"*": {"{from} to {to}": "x"}}},
        {"get": ["{from} to {to}"]},
        {"remove": ["{from} to {to}"]},
        {"rename": {"{from} to {to}": "y"}},
        {"set_keys": {"*": {"z": "Z"}}, "place": {"z": {"after": "{from} to {to}"}}},
    ],
)
def test_a_call_naming_a_msgid_present_twice_is_refused_with_its_lines(call):
    with pytest.raises(jsonkeys.JsonKeysError, match=r"twice \(lines 4, 8\)"):
        jsonkeys.change_keys({FR_PATH: LINGUI_DUPLICATE}, **call)


@pytest.mark.parametrize("value", ["a\nb", 'q"uote', "back\\slash", "tab\tt", "l’apostrophe"])
def test_string_escapes_round_trip(value):
    assert pocatalog.decode_string(pocatalog.encode_string(value)[1:-1]) == value


def test_json_and_script_catalogs_are_unchanged_by_the_dispatch():
    assert pocatalog.is_po_catalog("a/messages.po") and pocatalog.is_po_catalog("a/messages.pot")
    assert not pocatalog.is_po_catalog("fr.json") and not pocatalog.is_po_catalog("fr.ts")
    out = jsonkeys.change_keys({"fr.json": '{\n  "a": "A"\n}\n'}, set_keys={"*": {"b": "B"}})[0]
    assert out.text == '{\n  "a": "A",\n  "b": "B"\n}\n'
