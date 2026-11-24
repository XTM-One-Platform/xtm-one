"""The OpenCTI notifier templates parse and render to JSON (#1865).

OpenCTI renders a Generic webhook template through safeEjs: one ``<%= %>`` or
``<%- %>`` expression that does not parse refuses the whole notification
("Invalid javascript") and nothing is delivered. CI has no Node, so these tests
parse every expression with a small parser of the JavaScript subset the
templates use (members, string-keyed brackets, calls, ``||``, ``{}``, parentheses)
and render the template with EJS's escaping, then ``json.loads`` the body.
"""

from __future__ import annotations

import json
import re
from typing import Any

import pytest

from app.services import opencti_trigger_provisioning as prov

PIR_EXTENSION = "extension-definition--ea279b3e-5c71-4632-ac08-831c66a786ba"
TEMPLATES = {"WEBHOOK_TEMPLATE": prov.WEBHOOK_TEMPLATE, "PIR_WEBHOOK_TEMPLATE": prov.PIR_WEBHOOK_TEMPLATE}
Node = tuple[Any, ...]
_TAG = re.compile(r"<%([=-])(.*?)%>", re.S)
_TOKEN = re.compile(r"\s*(?:(\|\|)|([A-Za-z_$][\w$]*)|('(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\")|(\d+)|([.\[\](){},]))")


class _Undefined:
    def __repr__(self) -> str:
        return "undefined"


UNDEFINED = _Undefined()


def _stringify(*args: Any) -> Any:
    value = args[0] if args else UNDEFINED
    if value is UNDEFINED:
        return UNDEFINED
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def _tokens(source: str) -> list[str]:
    out, pos = [], 0
    source = source.rstrip()
    while pos < len(source):
        match = _TOKEN.match(source, pos)
        if not match or match.end() == pos:
            raise SyntaxError(f"unexpected character at {pos}: {source[pos : pos + 20]!r}")
        out.append(match.group(0).strip())
        pos = match.end()
    return out


class _Parser:
    """Recursive descent over the template subset; ``parse`` raises SyntaxError like safeEjs."""

    def __init__(self, source: str) -> None:
        self.toks = _tokens(source)
        self.i = 0

    def peek(self) -> str | None:
        return self.toks[self.i] if self.i < len(self.toks) else None

    def take(self, expected: str | None = None) -> str:
        tok = self.peek()
        if tok is None or (expected is not None and tok != expected):
            raise SyntaxError(f"expected {expected or 'a token'}, got {tok!r}")
        self.i += 1
        return tok

    def parse(self) -> Node:
        node = self.or_expr()
        if self.peek() is not None:
            raise SyntaxError(f"unexpected {self.peek()!r} after the expression")
        return node

    def or_expr(self) -> Node:
        node = self.postfix()
        while self.peek() == "||":
            self.take()
            node = ("or", node, self.postfix())
        return node

    def postfix(self) -> Node:
        node = self.primary()
        while self.peek() in (".", "[", "("):
            tok = self.take()
            if tok == ".":
                name = self.take()
                if not re.fullmatch(r"[A-Za-z_$][\w$]*", name):
                    raise SyntaxError(f"bad member name {name!r}")
                node = ("get", node, ("lit", name))
            elif tok == "[":
                key = self.or_expr()
                self.take("]")
                node = ("get", node, key)
            else:
                args = []
                if self.peek() != ")":
                    args.append(self.or_expr())
                    while self.peek() == ",":
                        self.take()
                        args.append(self.or_expr())
                self.take(")")
                node = ("call", node, args)
        return node

    def primary(self) -> Node:
        tok = self.take()
        if tok == "(":
            node = self.or_expr()
            self.take(")")
            return node
        if tok == "{":
            self.take("}")
            return ("lit", {})
        if tok[0] in "'\"":
            return ("lit", json.loads('"' + tok[1:-1].replace('"', '\\"') + '"'))
        if tok.isdigit():
            return ("lit", int(tok))
        if re.fullmatch(r"[A-Za-z_$][\w$]*", tok):
            return ("var", tok)
        raise SyntaxError(f"unexpected {tok!r}")


def _evaluate(node: Node, scope: dict[str, Any]) -> Any:
    kind = node[0]
    if kind == "lit":
        return node[1]
    if kind == "var":
        if node[1] not in scope:
            raise NameError(node[1])
        return scope[node[1]]
    if kind == "or":
        left = _evaluate(node[1], scope)
        return left if left not in (None, UNDEFINED, False, 0, "") else _evaluate(node[2], scope)
    if kind == "get":
        target, key = _evaluate(node[1], scope), _evaluate(node[2], scope)
        if target is None or target is UNDEFINED:
            raise TypeError(f"cannot read {key!r} of {target!r}")
        if isinstance(target, list) and isinstance(key, int):
            return target[key] if key < len(target) else UNDEFINED
        if isinstance(target, dict):
            return target.get(key, UNDEFINED)
        return UNDEFINED
    fn = _evaluate(node[1], scope)
    return fn(*[_evaluate(arg, scope) for arg in node[2]])


def _escape(text: str) -> str:
    """EJS's ``<%=`` escaping."""
    for raw, entity in (("&", "&amp;"), ("<", "&lt;"), (">", "&gt;"), ('"', "&#34;"), ("'", "&#39;")):
        text = text.replace(raw, entity)
    return text


def render(template: str, scope: dict[str, Any]) -> str:
    def one(match: re.Match[str]) -> str:
        value = _evaluate(_Parser(match.group(2)).parse(), {"JSON": {"stringify": _stringify}, **scope})
        text = "" if value in (None, UNDEFINED) else str(value)
        return _escape(text) if match.group(1) == "=" else text

    return _TAG.sub(one, template)


def _pir_scope(extensions: Any = UNDEFINED) -> dict[str, Any]:
    instance: dict[str, Any] = {"id": "intrusion-set--1", "name": 'APT "X"', "type": "intrusion-set"}
    if extensions is not UNDEFINED:
        instance["extensions"] = extensions
    return {
        "data": [
            {
                "notification_id": "node-1",
                "type": "update",
                "message": 'adds `APT "X"` in PIR <Filigran>',
                "instance": instance,
            }
        ]
    }


@pytest.mark.parametrize("name", sorted(TEMPLATES))
def test_every_expression_of_the_notifier_templates_parses(name: str) -> None:
    expressions = [m.group(2) for m in _TAG.finditer(TEMPLATES[name])]
    assert expressions, name
    for source in expressions:
        _Parser(source).parse()
    assert "<%" not in _TAG.sub("", TEMPLATES[name])


def test_the_parser_refuses_the_unbalanced_expression_that_dropped_every_pir_delivery() -> None:
    broken = f" JSON.stringify((data[0].instance.extensions || {{}})['{PIR_EXTENSION}'] || {{}}).pir_information) "
    with pytest.raises(SyntaxError):
        _Parser(broken).parse()


@pytest.mark.parametrize(
    "extensions",
    [
        {
            PIR_EXTENSION: {
                "pir_information": [{"pir_id": "pir-1", "pir_score": 80, "last_pir_score_date": "2026-11-24"}]
            }
        },
        {PIR_EXTENSION: {}},
        {},
        UNDEFINED,
    ],
)
def test_the_pir_template_renders_a_json_body_keyed_on_the_pir_information(extensions: Any) -> None:
    body = json.loads(render(prov.PIR_WEBHOOK_TEMPLATE, _pir_scope(extensions)))
    assert body["xtm_source"] == "pir_playbook"
    assert body["type"] == "update"
    assert body["data"]["id"] == "intrusion-set--1" and body["data"]["name"] == 'APT "X"'
    assert body["notification_id"].startswith("node-1:intrusion-set--1:update:")
    if isinstance(extensions, dict) and extensions.get(PIR_EXTENSION):
        assert "pir_score&#34;:80" in body["notification_id"]


def test_the_trigger_template_renders_a_json_body() -> None:
    scope = {
        "notification": {"id": "trigger-1"},
        "data": [
            {
                "type": "create",
                "message": "creates report <X> 'quoted'",
                "instance": {"id": "report--1", "modified": "2026-11-24T00:00:00Z", "name": "X"},
            }
        ],
    }
    body = json.loads(render(prov.WEBHOOK_TEMPLATE, scope))
    assert body["notification_id"] == "trigger-1:report--1:create:2026-11-24T00:00:00Z"
    assert body["data"]["id"] == "report--1"


@pytest.mark.parametrize("name", sorted(TEMPLATES))
def test_the_notifier_configuration_carries_the_template_unchanged(name: str) -> None:
    config = json.loads(prov.notifier_configuration("https://x/hook", "s", template=TEMPLATES[name]))
    assert config["template"] == TEMPLATES[name]
