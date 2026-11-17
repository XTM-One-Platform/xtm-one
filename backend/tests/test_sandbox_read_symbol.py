"""sandbox_read_symbol: one definition whole, by name, in one call.

The spans come from ``reading.symbols`` (pure): Python through ``ast`` with
decorators and the exact end line, nested and qualified names; the other
outlined languages run a definition to the next one at its depth. The tool is
covered end to end in ``test_sandbox_workspace_tools.py``.
"""

from __future__ import annotations

from app.services.sandbox_workspace import reading

PY = '''"""Module."""

import os

CONST = 1


@decorator
def top(a):
    return a


class Service:
    """A service."""

    def run(self):
        x = 1
        return x

    async def stop(self):
        return None


def factory():
    async def run(arg):
        return arg

    return run


if os.environ.get("X"):
    def guarded():
        return 2
'''


def _spans(path: str, text: str) -> dict[str, tuple[int, int, str]]:
    found = reading.symbols(path, text)
    assert found is not None
    return {s.name: (s.first, s.last, s.kind) for s in found}


def test_python_spans_are_exact_with_decorators_and_qualified_names():
    spans = _spans("m.py", PY)
    assert spans["top"] == (8, 10, "def")  # the decorator line opens it
    assert spans["Service"] == (13, 21, "class")
    assert spans["Service.run"] == (16, 18, "def")
    assert spans["Service.stop"] == (20, 21, "async def")
    assert spans["factory"] == (24, 28, "def")
    assert spans["factory.run"] == (25, 26, "async def")
    assert spans["guarded"] == (32, 33, "def")
    assert "CONST" not in spans


def test_a_bare_name_matches_every_definition_ending_with_it_and_a_qualified_name_one():
    found = reading.symbols("m.py", PY) or []
    assert [s.name for s in reading.find_symbol(found, "run")] == ["Service.run", "factory.run"]
    assert [s.name for s in reading.find_symbol(found, "Service.run")] == ["Service.run"]
    assert [s.name for s in reading.find_symbol(found, " top ")] == ["top"]
    assert reading.find_symbol(found, "nope") == []


def test_the_choices_list_every_match_with_its_lines():
    found = reading.symbols("m.py", PY) or []
    out = reading.format_symbol_choices("workspace/m.py", "run", reading.find_symbol(found, "run"))
    assert out.startswith("workspace/m.py: 2 symbols named 'run'")
    assert "16-18  def Service.run" in out and "25-26  async def factory.run" in out


def test_a_python_file_that_does_not_parse_falls_back_to_the_scan():
    spans = _spans("broken.py", "def ok():\n    return 1\n\ndef bad(:\n    pass\n")
    assert spans["ok"][0] == 1 and spans["ok"][1] == 2
    assert "bad" not in spans or spans["bad"][0] == 4


def test_a_typescript_definition_runs_to_the_next_one_at_its_depth():
    ts = (
        "export function a() {\n  return 1;\n}\n\n"
        "export class B {\n  m() {}\n}\n\n"
        "export const c = () => 3;\n"
    )
    spans = _spans("x.ts", ts)
    assert spans["a"][:2] == (1, 3)
    assert spans["B"][:2] == (5, 7)
    assert spans["c"][:2] == (9, 9)


TSX = """import { memo } from 'react';

const title = (
  'x'
);

const ConversationRow = ({
  conversation,
  active,
  onSelect,
}: {
  conversation: { id: string; title: string };
  active: boolean;
  onSelect: (id: string) => void;
}) => {
  const label = (
    <span>{conversation.title}</span>
  );
  return <button onClick={() => onSelect(conversation.id)}>{label}</button>;
};

export const Loader = async <T,>(
  url: string,
): Promise<T> => {
  return fetch(url) as T;
};

export const Card = memo(({ title }: { title: string }) => <div>{title}</div>);

const Field = React.forwardRef<HTMLInputElement, Props>((props, ref) => <input ref={ref} {...props} />);

export function Sidebar() {
  return null;
}
"""


def test_a_tsx_const_arrow_with_a_multi_line_destructured_header_is_a_symbol():
    spans = _spans("ConversationSidebar.tsx", TSX)
    assert "title" not in spans  # a parenthesised expression, not an arrow
    assert spans["ConversationRow"] == (7, 20, "const")
    assert spans["Loader"] == (22, 26, "const")
    assert spans["Card"][:2] == (28, 28)
    assert spans["Field"][:2] == (30, 30)
    assert spans["Sidebar"][:2] == (32, 34)
    names = [d.name for d in reading.outline("ConversationSidebar.tsx", TSX) or []]
    assert names == ["ConversationRow", "Loader", "Card", "Field", "Sidebar"]
    lines = TSX.splitlines()
    first, last, _kind = spans["ConversationRow"]
    body = "\n".join(lines[first - 1 : last])
    assert body.startswith("const ConversationRow = ({") and body.endswith("};")
    assert "onSelect(conversation.id)" in body


def test_an_unclosed_header_before_the_next_declaration_is_not_a_symbol():
    ts = "const config = (\n  value\n\nexport function next() {\n  return 1;\n}\n"
    spans = _spans("x.ts", ts)
    assert "config" not in spans
    assert spans["next"][:2] == (4, 6)


def test_no_outline_means_no_symbols():
    assert reading.symbols("notes.txt", "hello") is None


def test_the_occurrence_is_a_positive_integer_or_zero():
    assert reading.clamp_occurrence(2) == 2
    assert reading.clamp_occurrence("3") == 3
    assert reading.clamp_occurrence(-1) == 0
    assert reading.clamp_occurrence(True) == 0
    assert reading.clamp_occurrence(None) == 0
