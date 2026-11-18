"""``sandbox_run_command`` findings of a vitest run: one cause failing many tests reads as one line.

A testing-library query matching several elements failed 22 tests in one run; each finding echoed
every matching element whole, colour codes included, and vitest's own "Failed Tests" section
printed them all again on stdout (26.9k characters for one root cause). Pure tests of
:func:`checks.finding_message`, :func:`checks.fold_same_message`, :func:`checks.parse_junit`
and :func:`checks.condense`.
"""

from __future__ import annotations

from xml.sax.saxutils import quoteattr

from app.services.sandbox_workspace import checks

RULE = "\u23af"
DOM_MESSAGE = (
    "Found multiple elements with the text: /Edit/\n\n"
    "Here are the matching elements:\n\n"
    "Ignored nodes: comments, script, style\n"
    '\x1b[36m<button\x1b[39m\n  \x1b[33mclass\x1b[39m=\x1b[32m"inline-flex"\x1b[39m\n\x1b[36m>\x1b[39m\n'
    + "  Edit\n" * 40
    + "\x1b[36m</button>\x1b[39m"
)


def _junit(count: int, message: str) -> str:
    # A JUnit report cannot carry the escape character (not valid XML): the colour codes stay without it.
    message = message.replace("\x1b", "")
    cases = "".join(
        f"<testcase classname='src/a.test.tsx' name='case {index}' file='src/a.test.tsx'>"
        f"<failure message={quoteattr(message)}>stack</failure></testcase>"
        for index in range(count)
    )
    return f"<testsuite>{cases}<testcase classname='src/a.test.tsx' name='ok'/></testsuite>"


def test_a_dom_dump_is_cut_and_its_colour_codes_stripped() -> None:
    message = checks.finding_message(DOM_MESSAGE)
    assert message == "Found multiple elements with the text: /Edit/ [DOM dump left out: the failures file keeps it]"
    assert "\x1b" not in message
    assert checks.finding_message("[31mexpected 1 to be 2[39m") == "expected 1 to be 2"


def test_a_message_without_a_dom_dump_is_kept_and_bounded() -> None:
    assert checks.finding_message("\x1b[31mexpected 1 to be 2\x1b[39m") == "expected 1 to be 2"
    assert len(checks.finding_message("x" * 1000)) == checks.FINDING_MESSAGE_CHARS


def test_many_tests_failing_with_one_message_fold_into_one_finding() -> None:
    summary = checks.parse_junit(_junit(22, DOM_MESSAGE))
    assert (summary.tests, summary.failed, summary.passed) == (23, 22, 1)
    assert len(summary.findings) == 1
    assert summary.findings[0].message.endswith(
        "(and 21 more test(s) failing with the same message, named in the failures file)"
    )
    # The failures file still carries every test with its whole message.
    assert len(summary.failures) == 22
    assert "Here are the matching elements:" in summary.failures[0][1]


def test_different_messages_stay_one_finding_each() -> None:
    first = checks.Finding(path="a", line=1, rule="", message="boom")
    second = checks.Finding(path="b", line=2, rule="", message="other")
    third = checks.Finding(path="c", line=3, rule="", message="boom")
    folded = checks.fold_same_message([first, second, third])
    assert [finding.path for finding in folded] == ["a", "b"]
    assert folded[0].message == "boom (and 1 more test(s) failing with the same message, named in the failures file)"
    assert folded[1].message == "other"


def test_vitest_failed_tests_section_is_left_out_when_the_findings_carry_it() -> None:
    text = "\n".join(
        [
            " \x1b[31m\u00d7\x1b[39m src/a.test.tsx > case 0",
            f"{RULE * 7} Failed Tests 2 {RULE * 7}",
            "",
            " FAIL  src/a.test.tsx > case 0",
            "TestingLibraryElementError: Found multiple elements with the text: /Edit/",
            "<button>Edit</button>",
            f"{RULE * 3}[1/2]{RULE}",
            " FAIL  src/a.test.tsx > case 1",
            "<button>Edit</button>",
            f"{RULE * 3}[2/2]{RULE}",
            "",
            " Test Files  1 failed (1)",
            "      Tests  2 failed | 1 passed (3)",
        ]
    )
    lines = checks.condense(text, reported=True).splitlines()
    assert lines[0] == " \u00d7 src/a.test.tsx > case 0"
    assert lines[1].startswith("[... vitest Failed Tests 2, 10 line(s), left out: the findings above carry them")
    assert lines[2:] == [" Test Files  1 failed (1)", "      Tests  2 failed | 1 passed (3)"]
    # Without findings the section stays: it is then the only account of the failures.
    assert any("Found multiple elements" in line for line in checks.condense(text).splitlines())


def test_a_section_after_failed_tests_is_kept() -> None:
    text = "\n".join(
        [
            f"{RULE * 3} Failed Tests 1 {RULE * 3}",
            " FAIL  src/a.test.tsx > case 0",
            f"{RULE * 3} Unhandled Errors {RULE * 3}",
            "TypeError: boom",
        ]
    )
    lines = checks.condense(text, reported=True).splitlines()
    assert lines[0].startswith("[... vitest Failed Tests 1, 2 line(s)")
    assert lines[1:] == [f"{RULE * 3} Unhandled Errors {RULE * 3}", "TypeError: boom"]
