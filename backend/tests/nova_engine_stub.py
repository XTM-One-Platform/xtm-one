"""A ``nova`` package on disk, standing in for ``nova-hunting`` (not installed in CI).

The full engine parses and matches in the Nova matching worker, a spawned
process, so a stub has to be importable there: it is written as a package under
a test's ``tmp_path``, put on ``sys.path`` (which a spawned process inherits),
and the worker is restarted so the next one sees it.

Two behaviours:

* ``verdict`` given: every text parses to a rule with no ``semantics:`` or
  ``llm:`` pattern and every match answers ``verdict`` with ``keywords`` as its
  ``matching_keywords`` (the shape ``test_nova_engine.py`` checks);
* ``verdict`` omitted: a rule is JSON - ``name``, ``keywords`` (substrings),
  ``semantics`` (``{var: [text, threshold]}``), ``llms`` (``{var: text}`` or
  ``{var: [text, threshold]}``) - and a match reads the keywords and asks the
  injected evaluators the way nova-hunting 0.3.1 does (an ``llm:`` pattern's
  threshold passed as ``temperature``); a
  rule named ``Raise...`` makes the matcher raise, text that is not JSON does
  not parse.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_BASE = """
class SemanticEvaluator:
    pass


class LLMEvaluator:
    pass
"""

_PARSER = """
import json

FIXED = {fixed!r}


class _Pattern:
    def __init__(self, pattern, threshold=None):
        self.pattern = pattern
        self.threshold = threshold


class _Rule:
    def __init__(self, data):
        self.name = data.get("name", "stub")
        self.keywords = list(data.get("keywords", []))
        self.semantics = {{k: _Pattern(*v) for k, v in data.get("semantics", {{}}).items()}}
        self.llms = {{k: _Pattern(*v) if isinstance(v, list) else _Pattern(v) for k, v in data.get("llms", {{}}).items()}}


class NovaParser:
    def parse(self, text):
        return _Rule({{}} if FIXED else json.loads(text))
"""

_MATCHER = """
VERDICT = {verdict!r}
KEYWORDS = {keywords!r}


class NovaMatcher:
    def __init__(self, rule, semantic_evaluator=None, llm_evaluator=None, create_llm_evaluator=True):
        self.rule = rule
        self.semantic_evaluator = semantic_evaluator
        self.llm_evaluator = llm_evaluator

    def check_prompt(self, prompt):
        if VERDICT is not None:
            return {{"matched": VERDICT, "matching_keywords": KEYWORDS}}
        if self.rule.name.startswith("Raise"):
            raise RuntimeError("stub matcher failure")
        hits = {{k: k in prompt.lower() for k in self.rule.keywords}}
        semantic = any(self.semantic_evaluator.evaluate(p, prompt)[0] for p in self.rule.semantics.values())
        # As nova-hunting 0.3.1 does: the pattern's threshold is the temperature.
        llm = any(
            self.llm_evaluator.evaluate_prompt(p.pattern, prompt, temperature=p.threshold)[0]
            for p in self.rule.llms.values()
        )
        return {{"matched": any(hits.values()) or semantic or llm, "matching_keywords": hits}}
"""


def install_nova_stub(
    root: Path, monkeypatch: pytest.MonkeyPatch, *, verdict: bool | None = None, keywords: dict | None = None
) -> None:
    """Write the stub under *root*, put it on ``sys.path`` and restart the worker."""
    from app.services import nova_engine, nova_matching

    package = root / "nova"
    files = {
        package / "__init__.py": "",
        package / "core" / "__init__.py": "",
        package / "evaluators" / "__init__.py": "",
        package / "evaluators" / "base.py": _BASE,
        package / "core" / "parser.py": _PARSER.format(fixed=verdict is not None),
        package / "core" / "matcher.py": _MATCHER.format(verdict=verdict, keywords=keywords or {}),
    }
    for path, source in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
    monkeypatch.syspath_prepend(str(root))
    monkeypatch.setattr(nova_engine, "NOVA_HUNTING_AVAILABLE", True)
    nova_matching.shutdown_nova_matching_worker()


def remove_nova_stub() -> None:
    """Forget the stub: the worker that imported it, and any import of it here."""
    from app.services import nova_matching

    nova_matching.shutdown_nova_matching_worker()
    for name in [n for n in sys.modules if n == "nova" or n.startswith("nova.")]:
        del sys.modules[name]
