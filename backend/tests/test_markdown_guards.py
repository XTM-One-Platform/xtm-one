"""Line guards of the outgoing Markdown renderers (Teams HTML, Teams text, email).

Python-Markdown renders ``#9195 is ...`` (no space after ``#``) as a heading,
where CommonMark and the agent writing it mean a paragraph; and a pipe table
written without its ``| --- |`` separator row reached Teams as raw pipes.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from app.services.email_html import markdown_to_email_html
from app.services.markdown_guards import add_missing_table_delimiters, escape_glued_hash
from app.services.teams_format import build_graph_chat_body, markdown_to_teams_html, markdown_to_teams_text

# -- gap 1: a line-start ``#`` glued to its text --------------------------


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("#9195 is the refill PR", "\\#9195 is the refill PR"),
        ("##12 two hashes", "\\##12 two hashes"),
        ("- #9195 in a list", "- \\#9195 in a list"),
        ("> #9195 quoted", "> \\#9195 quoted"),
        ("# Title", "# Title"),
        ("## Title", "## Title"),
        ("#", "#"),
        ("see #9195 inline", "see #9195 inline"),
    ],
)
def test_escape_glued_hash(line: str, expected: str) -> None:
    assert escape_glued_hash(line) == expected


@pytest.mark.parametrize("render", [markdown_to_teams_html, markdown_to_email_html])
def test_a_line_start_issue_number_is_a_paragraph_not_a_heading(render: Callable[[str], str]) -> None:
    html = render("#9195 is the refill PR\n\nnext")
    assert "<h1" not in html
    assert "#9195 is the refill PR" in html
    assert "\\#" not in html


@pytest.mark.parametrize("render", [markdown_to_teams_html, markdown_to_email_html])
def test_real_headings_are_unchanged(render: Callable[[str], str]) -> None:
    html = render("# Title\n\n## Section\n\ntext")
    assert "<h1" in html and "Title" in html
    assert "<h2" in html and "Section" in html


@pytest.mark.parametrize("render", [markdown_to_teams_html, markdown_to_email_html])
def test_a_hash_line_inside_a_fence_is_unchanged(render: Callable[[str], str]) -> None:
    html = render("```\n#9195 comment\n# shell\n```")
    assert "<h1" not in html
    assert "#9195 comment" in html and "\\#" not in html


def test_text_surface_keeps_the_issue_number_as_written() -> None:
    text = markdown_to_teams_text("#9195 is the refill PR")
    assert "#9195 is the refill PR" in text and "**" not in text


def test_graph_chat_body_has_no_heading_for_an_issue_number() -> None:
    body = build_graph_chat_body("#9195 is the refill PR")
    assert "<h1" not in body["content"]


# -- gap 2: a pipe table without its separator row -------------------------

NO_SEPARATOR = "Status:\n\n| PR | State |\n| #11512 | green |\n| #9195 | red |\n\nDone."


@pytest.mark.parametrize("render", [markdown_to_teams_html, markdown_to_email_html])
def test_a_table_without_separator_renders_as_a_table(render: Callable[[str], str]) -> None:
    html = render(NO_SEPARATOR)
    assert "<table" in html.lower()
    assert "<th" in html.lower() and "PR" in html and "#11512" in html
    assert "| PR |" not in html


def test_a_table_without_separator_becomes_records_on_the_text_surface() -> None:
    text = markdown_to_teams_text(NO_SEPARATOR)
    assert "| PR |" not in text
    assert "- **PR:** #11512, **State:** green" in text


@pytest.mark.parametrize("render", [markdown_to_teams_html, markdown_to_email_html])
def test_a_table_with_its_separator_is_unchanged(render: Callable[[str], str]) -> None:
    body = "| PR | State |\n| --- | --- |\n| #11512 | green |"
    html = render(body)
    assert html.lower().count("<table") == 1 and html.lower().count("<tr") == 2


def test_a_single_pipe_line_is_untouched() -> None:
    lines = ["| x |"]
    assert add_missing_table_delimiters(lines, [False]) == (["| x |"], [False])
    assert "<table" not in markdown_to_teams_html("| x |").lower()


def test_rows_of_different_widths_are_not_a_table() -> None:
    lines = ["| a | b |", "| c |"]
    assert add_missing_table_delimiters(lines, [False, False])[0] == lines


@pytest.mark.parametrize("render", [markdown_to_teams_html, markdown_to_email_html])
def test_pipes_inside_a_fence_are_untouched(render: Callable[[str], str]) -> None:
    html = render("```\n| a | b |\n| c | d |\n```")
    assert "<table" not in html.lower()
    assert "| a | b |" in html and "---" not in html


def test_the_delimiter_is_inserted_once_and_the_mask_follows() -> None:
    lines = ["| a | b |", "| 1 | 2 |", "| 3 | 4 |"]
    out, code = add_missing_table_delimiters(lines, [False] * 3)
    assert out == ["| a | b |", "| --- | --- |", "| 1 | 2 |", "| 3 | 4 |"]
    assert code == [False] * 4
    # Idempotent: a second pass sees the separator.
    assert add_missing_table_delimiters(out, code)[0] == out
