"""``sandbox_json_keys`` on TypeScript / JavaScript object-literal catalogs (``tscatalog.py``)."""

from __future__ import annotations

import difflib

import pytest

from app.services.sandbox_workspace import jsonkeys, tscatalog

FR = (
    "import type { Catalog } from './index';\n"
    "\n"
    "export const fr: Catalog = {\n"
    "  '(no output)': '(pas de sortie)',\n"
    "  'Ask Assistant': 'Demander à l’assistant',\n"
    "  Approved: 'Approuvé',\n"
    "  // A comment kept with the entry below.\n"
    "  'Image preview': 'Aperçu de l’image',\n"
    "  \"It's\": 'C\\'est',\n"
    "  Back: 'Retour',\n"
    "};\n"
)
JA = (
    "import type { Catalog } from './index';\n"
    "\n"
    "export const ja: Catalog = {\n"
    "  '(no output)': '（出力なし）',\n"
    "  'Image preview': '画像のプレビュー',\n"
    "  Back: '戻る',\n"
    "};\n"
)


def _changed_lines(before: str, after: str) -> list[str]:
    return [
        line
        for line in difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="", n=0)
        if line[:1] in "+-" and not line.startswith(("+++", "---"))
    ]


def test_one_call_adds_a_key_after_an_anchor_with_a_value_per_locale_touching_only_that_line():
    out = {
        c.path: c
        for c in jsonkeys.change_keys(
            {"src/i18n/fr.ts": FR, "src/i18n/ja.ts": JA},
            set_keys={
                "src/i18n/fr.ts": {"Previous image": "Image précédente"},
                "src/i18n/ja.ts": {"Previous image": "前の画像"},
            },
            place={"Previous image": {"after": "Image preview"}},
            get=["Previous image", "Back"],
        )
    }
    assert _changed_lines(FR, out["src/i18n/fr.ts"].text) == ["+  'Previous image': 'Image précédente',"]
    assert _changed_lines(JA, out["src/i18n/ja.ts"].text) == ["+  'Previous image': '前の画像',"]
    assert out["src/i18n/fr.ts"].text.index("'Image preview'") < out["src/i18n/fr.ts"].text.index("'Previous image'")
    assert out["src/i18n/fr.ts"].values == {"Previous image": "Image précédente", "Back": "Retour"}
    assert out["src/i18n/fr.ts"].added == ["Previous image"]


def test_bare_identifier_keys_are_written_bare_and_typographic_apostrophes_kept():
    out = jsonkeys.change_keys({"fr.ts": FR}, set_keys={"fr.ts": {"Retry": "Réessayer l’envoi", "Two words": "Deux"}})[
        0
    ]
    assert _changed_lines(FR, out.text) == ["+  Retry: 'Réessayer l’envoi',", "+  'Two words': 'Deux',"]
    assert out.text.endswith("  Back: 'Retour',\n  Retry: 'Réessayer l’envoi',\n  'Two words': 'Deux',\n};\n")


def test_an_existing_key_changes_its_value_in_place_and_escapes_the_quote():
    out = jsonkeys.change_keys({"fr.ts": FR}, set_keys={"fr.ts": {"Approved": "C'est approuvé", "Back": "Retour"}})[0]
    assert out.changed == ["Approved"]
    assert _changed_lines(FR, out.text) == ["-  Approved: 'Approuvé',", "+  Approved: 'C\\'est approuvé',"]


def test_get_decodes_escapes_and_reads_quoted_keys():
    out = jsonkeys.change_keys({"fr.ts": FR}, get=["It's", "Ask Assistant", "Nope"])[0]
    assert out.text == FR and not out.modified
    assert out.values["It's"] == "C'est" and out.values["Ask Assistant"] == "Demander à l’assistant"
    assert jsonkeys.describe_values(out)[2] == '  "Nope": (absent)'


def test_rename_and_remove_touch_only_their_lines_and_keep_the_comment_with_its_neighbour():
    out = jsonkeys.change_keys({"fr.ts": FR}, rename={"Back": "Go back"}, remove=["Image preview"])[0]
    assert out.renamed == ["Back -> Go back"] and out.removed == ["Image preview"]
    assert _changed_lines(FR, out.text) == [
        "-  'Image preview': 'Aperçu de l’image',",
        "-  Back: 'Retour',",
        "+  'Go back': 'Retour',",
    ]
    assert '// A comment kept with the entry below.\n  "It\'s"' in out.text


@pytest.mark.parametrize("trailing", [True, False])
def test_the_trailing_comma_style_is_followed(trailing):
    text = "export default {\n  a: 'A',\n  b: 'B'" + ("," if trailing else "") + "\n};\n"
    out = jsonkeys.change_keys({"en.js": text}, set_keys={"*": {"c": "C"}})[0]
    assert out.text == "export default {\n  a: 'A',\n  b: 'B',\n  c: 'C'" + ("," if trailing else "") + "\n};\n"
    removed = jsonkeys.change_keys({"en.js": text}, remove=["b"])[0]
    assert removed.text == "export default {\n  a: 'A'" + ("," if trailing else "") + "\n};\n"


def test_double_quotes_crlf_and_as_const_are_kept():
    text = 'const en = {\r\n    "Save": "Save",\r\n    "Cancel": "Cancel",\r\n} as const;\r\nexport default en;\r\n'
    out = jsonkeys.change_keys(
        {"en.mjs": text}, set_keys={"en.mjs": {'Say "hi"': "Hi"}}, place={'Say "hi"': {"before": "Cancel"}}
    )[0]
    assert out.text == (
        'const en = {\r\n    "Save": "Save",\r\n    "Say \\"hi\\"": "Hi",\r\n    "Cancel": "Cancel",\r\n} as const;\r\n'
        "export default en;\r\n"
    )


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("const a = {\n  ...base,\n  b: 'B',\n};\n", "spread or computed"),
        ("const a = {\n  [k]: 'B',\n};\n", "spread or computed"),
        ("const a = {\n  b,\n};\n", "shorthand"),
        ("const a = {\n  b: 'B', c: 'C',\n};\n", "two entries"),
        ("const a = {\n  b: 'B',\n};\nconst c = {\n  d: 'D',\n};\n", "2 object literals"),
        ("const a = {\n  b: 'B',\n  b: 'C',\n};\n", "twice"),
        ("const a = { b: 'B' };\n", "one line"),
        ("const a = {\n  b: 'B\n};\n", "not closed"),
    ],
)
def test_a_file_that_is_not_a_line_per_entry_catalog_is_refused(text, message):
    with pytest.raises(jsonkeys.JsonKeysError, match=message):
        jsonkeys.change_keys({"x.ts": text}, set_keys={"*": {"z": "Z"}})


def test_a_non_string_value_and_a_missing_anchor_refuse_the_whole_call():
    with pytest.raises(jsonkeys.JsonKeysError, match="must be a string"):
        jsonkeys.change_keys({"fr.ts": FR}, set_keys={"fr.ts": {"n": 3}})
    with pytest.raises(jsonkeys.JsonKeysError, match="to place"):
        jsonkeys.change_keys({"fr.ts": FR, "ja.ts": JA}, set_keys={"*": {"z": "Z"}}, place={"z": {"after": "Approved"}})


def test_braces_and_comments_inside_strings_and_templates_do_not_confuse_the_scanner():
    text = (
        "// { not an object }\n"
        "const help = `see ${'{'} and /* this */`;\n"
        "export const en = {\n"
        "  'Open {name}': 'Open {name} // now', /* trailing */\n"
        "  nested: { a: 1 },\n"
        "};\n"
    )
    out = jsonkeys.change_keys({"en.ts": text}, set_keys={"*": {"z": "Z"}}, get=["Open {name}", "nested"])[0]
    assert _changed_lines(text, out.text) == ["+  z: 'Z',"]
    assert out.values["Open {name}"] == "Open {name} // now"
    assert str(out.values["nested"]) == "(expression) { a: 1 }"


@pytest.mark.parametrize(
    ("literal", "value"),
    [("'a\\nb'", "a\nb"), ("'\\u2019'", "\u2019"), ("'\\u{1F600}'", "\U0001f600"), ("'\\x41'", "A"), ('"\\""', '"')],
)
def test_string_escapes_round_trip(literal, value):
    assert tscatalog.decode_string(literal) == value
    assert tscatalog.decode_string(tscatalog.encode_string(value, literal[0])) == value


def test_json_catalogs_are_unchanged_by_the_dispatch():
    assert not tscatalog.is_script_catalog("fr.json") and tscatalog.is_script_catalog("src/i18n/fr.ts")
