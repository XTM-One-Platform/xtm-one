"""``list_skills`` and ``list_prompts``: a name filter, a one-line mode and paging (#8692).

One id lookup by name used to answer every row with its full description
(about 28,700 characters for 54 skills). The shared page builder now keeps
the rows whose name (else description) holds ``search``, answers one line
per row with ``compact``, and pages with the shared paging line.
"""

from __future__ import annotations

import jsonschema

from app.services.builtin_tools import agent_builder as ab


def _rows(n: int) -> list[tuple[str, str, str, str]]:
    rows = []
    for i in range(n):
        name = f"Skill {i:02d}"
        desc = f"Long description number {i} " + "x" * 400
        rows.append((name, desc, f"• **{name}** (id: `id-{i}`)\n  {desc}", f"id-{i} | {name} | enabled"))
    return rows


def _named() -> list[tuple[str, str, str, str]]:
    return [
        ("[CEO Office] Chief of staff", "Runs the week", "• **Chief of staff** block " + "y" * 600, "cos | Chief of staff"),
        ("Mail secretary", "Sorts the inbox, chief of staff duties", "• **Mail secretary** block", "mail | Mail secretary"),
        ("Writer", "Writes", "• **Writer** block", "writer | Writer"),
    ]


def test_no_argument_reads_as_before():
    out = ab._catalog_list_page(_rows(3), noun="skill")
    assert out.startswith("Found 3 skill(s):\n")
    assert "showing" not in out
    assert out.count("• **Skill") == 3


def test_empty_listing_keeps_its_answer():
    assert ab._catalog_list_page([], noun="prompt") == "No prompts found."


def test_search_matches_the_name_in_any_case_first():
    out = ab._catalog_list_page(_named(), noun="skill", search="chief OF staff")
    assert "Found 1 skill(s) whose name matches" in out
    assert "Mail secretary" not in out
    assert len(out) < 1_000


def test_search_falls_back_to_the_description_when_no_name_matches():
    out = ab._catalog_list_page(_named(), noun="skill", search="inbox")
    assert "whose description matches" in out
    assert "Mail secretary" in out and "Writer" not in out


def test_search_without_a_hit_says_so():
    assert ab._catalog_list_page(_named(), noun="skill", search="nothing") == "No skills found matching `nothing`."


def test_compact_answers_one_line_per_row():
    out = ab._catalog_list_page(_rows(54), noun="skill", compact=True)
    lines = out.splitlines()
    assert lines[0] == "Found 54 skill(s):"
    assert lines[1:] == [f"id-{i} | Skill {i:02d} | enabled" for i in range(54)]
    assert "Long description" not in out


def test_a_cut_page_opens_with_the_paging_line():
    out = ab._catalog_list_page(_rows(54), noun="skill", compact=True, limit=20, offset=20)
    first = out.splitlines()[0]
    assert first == "showing 21-40 of 54 skills, offset=40 for the next page."
    assert "id-20 |" in out and "id-40 |" not in out


def test_the_last_page_says_so():
    out = ab._catalog_list_page(_rows(54), noun="skill", compact=True, limit=20, offset=40)
    assert out.splitlines()[0] == "showing 41-54 of 54 skills, the last page."


def test_a_bad_limit_is_refused():
    assert ab._catalog_list_page(_rows(3), noun="skill", limit="many").startswith("limit must be an integer")


def test_schema_names_the_arguments_and_refuses_an_unknown_one():
    defs = {d.name: d for d in ab.build_agent_builder_tool(None, "")[0]}
    for name in ("list_skills", "list_prompts"):
        schema = defs[name].input_schema
        assert set(schema["properties"]) == {"search", "compact", "limit", "offset"}
        jsonschema.validate({"search": "x", "compact": True, "limit": 5, "offset": 0}, schema)
        try:
            jsonschema.validate({"name": "x"}, schema)
        except jsonschema.ValidationError as exc:
            assert "name" in exc.message
        else:  # pragma: no cover - the refusal is the contract
            raise AssertionError("an unknown argument must be refused")
    assert defs["list_skills"].input_schema is not defs["list_prompts"].input_schema
