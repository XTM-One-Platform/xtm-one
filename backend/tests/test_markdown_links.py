"""A link label that is somebody else's text cannot break out of its link."""

from __future__ import annotations

import re

import pytest

from app.core.markdown_links import markdown_link, markdown_link_label

URL = "https://xtm.example/chat?c=0f0f"
#: A label is safe when no markdown special is left unescaped and it is one line.
_UNESCAPED = re.compile(r"(?<!\\)[\[\]()<>`*_#!|~{}+]")


@pytest.mark.parametrize(
    ("title", "label"),
    [
        ("Plan choice", "Plan choice"),
        ("x](https://evil.example) [y", r"x\]\(https://evil.example\) \[y"),
        ("a [b] c", r"a \[b\] c"),
        ("(paren)", r"\(paren\)"),
        ("line one\nline two", "line one line two"),
        ("tab\tand\r\nCRLF", "tab and CRLF"),
        ("`code`", r"\`code\`"),
        ("<img src=x onerror=alert(1)>", r"\<img src=x onerror=alert\(1\)\>"),
        ("**bold** _it_", r"\*\*bold\*\* \_it\_"),
        ("# heading", r"\# heading"),
        ("![image](u)", r"\!\[image\]\(u\)"),
        ("back\\slash", r"back\\slash"),
        ("pipe | table", r"pipe \| table"),
        (" separator", "separator"),
    ],
)
def test_every_special_is_escaped_and_the_label_is_one_line(title, label):
    escaped = markdown_link_label(title, fallback="Untitled")
    assert escaped == label
    assert "\n" not in escaped and "\r" not in escaped
    assert not _UNESCAPED.search(escaped.replace("\\\\", ""))


@pytest.mark.parametrize("blank", [None, "", "   ", "\n\t"])
def test_a_blank_title_reads_as_the_fallback(blank):
    assert markdown_link_label(blank, fallback="Untitled conversation") == "Untitled conversation"


def test_a_long_title_is_bounded():
    assert len(markdown_link_label("a" * 500, fallback="x", max_chars=120)) == 120


def test_the_link_keeps_the_platform_url_as_given():
    assert markdown_link("x](evil)", URL, fallback="x") == rf"[x\]\(evil\)]({URL})"
