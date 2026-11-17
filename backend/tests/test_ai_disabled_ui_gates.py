"""UI actions must be *visibly* unavailable when AI is off (T071).

FR-018 asks for two things: an AI action must not be startable, and it must be
visibly unavailable with a consistent explanation. The backend gate delivers the
first. This file guards the second — a button that stays enabled and only
explains itself after a failed click satisfies half the requirement.

The endpoint list is read from **FastAPI's own route table**, not from a
hand-written list of paths. That is deliberate: every hand-written list in this
feature went stale (see ``_NON_AI_MODULES`` in
``test_ai_disabled_read_surface.py`` for the same lesson).
"""

from __future__ import annotations

import functools
import inspect
import re
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
FRONTEND = BACKEND.parent / "frontend"

_GATE_DEPENDENCIES = {
    "require_ai_enabled",
    "require_agentic_ai",
    "require_chat_enabled",
}

#: Markers that show a component has consulted the AI state at all.
_GATE_MARKERS = (
    "useAiActionDisabled",
    "AiActionGate",
    "AiDisabledTip",
    "useAiEnabled",
    # ``AgenticGateDialog`` IS an AI gate: besides the licence question it calls
    # ``useAiActionDisabled()`` and wraps its children in ``AiActionGate`` when
    # AI is off. An earlier note here claimed it was licence-only — wrong, and
    # from reading the first 60 lines of a 200-line file. It is deliberately not
    # in this *file-level* list all the same, because file-level markers are what
    # let an ungated button hide beside a gated one; it is recognised per control
    # in ``_ungated_controls`` instead.
    "AgenticGateDialog",
    # A real gate for chat surfaces: ``chat_enabled`` in the platform config is
    # computed by ``is_chat_enabled``, which short-circuits on the AI state, so
    # a component honouring this is already disabled when AI is off.
    "useChatSendEnabled",
)

#: Call sites still to gate, from the sweep on 2026-08-28. **This list may only
#: shrink.** It exists so the guard can be added before the work is finished
#: without turning the suite red, and so a *new* ungated call site fails
#: immediately rather than joining a silent backlog.
#:
#: Found by manual testing, not by any test: an assignment could be Run from the
#: UI on an AI-disabled platform. Remove an entry as its buttons are gated.
#:
#: Five entries left on 2026-08-28 without any UI change: their endpoints were
#: un-gated instead, because they operate on an **existing run** (FR-013 lets
#: work in flight finish) or on own-conversation metadata. The button was never
#: wrong there; the backend gate was.
#: **Empty, and it should stay that way.** It held the 58 call sites the sweep
#: on 2026-08-28 found ungated, so the guard could land before the work was
#: finished. All 58 are now resolved: most by gating the control, five by
#: un-gating the *endpoint* (they operate on an existing run, or on
#: own-conversation metadata), and the rest by teaching this file that
#: ``useChatSendEnabled`` is a real gate and that a page can be gated through a
#: shared component.
#:
#: A file appearing here again means a new ungated call site. Gate the control
#: rather than adding the name back.
_KNOWN_UNGATED_CALL_SITES: set[str] = set()


def _words(source: str) -> frozenset[str]:
    """Every identifier-like run in ``source``.

    ``name in _words(source)`` is ``re.search(rf"\\b{name}\\b", source)`` for a
    name made of word characters, read once per file instead of once per name:
    a pattern that opens on ``\\b`` has no literal prefix to scan for, so each
    of those searches tried every offset of the file.
    """
    return frozenset(re.findall(r"\w+", source))


@functools.cache
def _tsx_sources() -> tuple[tuple[Path, str], ...]:
    """Every component of the app; a component test (``*.test.tsx``) renders no button of its own."""
    return tuple(
        (tsx, tsx.read_text(encoding="utf-8"))
        for tsx in sorted((FRONTEND / "src").rglob("*.tsx"))
        if not tsx.name.endswith(".test.tsx")
    )


@functools.cache
def _gated_endpoints() -> frozenset[tuple[str, str]]:
    """``(METHOD, path)`` for every mutating route behind an AI gate.

    Walks the resolved dependency tree, so a gate applied on a router or
    inherited through a sub-dependency counts.
    """
    from app.main import application

    gated: set[tuple[str, str]] = set()
    for route in application.routes:
        methods = set(getattr(route, "methods", None) or set())
        mutating = methods & {"POST", "PUT", "PATCH", "DELETE"}
        if not mutating:
            continue
        names: set[str] = set()
        stack = [(getattr(route, "dependant", None), 0)]
        while stack:
            dep, depth = stack.pop()
            if dep is None or depth > 5:
                continue
            call = getattr(dep, "call", None)
            if call is not None:
                names.add(getattr(call, "__name__", ""))
            stack.extend(
                (sub, depth + 1) for sub in (getattr(dep, "dependencies", []) or [])
            )
        if names & _GATE_DEPENDENCIES or _refuses_inline(route):
            path = re.sub(r"\{[^}]*\}", "{}", route.path.rstrip("/"))
            gated.update((m, path) for m in mutating)
    return frozenset(gated)


def _refuses_inline(route) -> bool:
    """Whether the handler refuses on the AI state in its own body.

    ``Depends(...)`` is not the only way a route is gated: twelve handlers call
    ``is_ai_enabled`` inline, three of them UI-reachable. A dependency-tree walk
    cannot see those, so T071's sweep never considered the buttons that reach
    them — which is how the flow-builder Run button stayed enabled for a flow
    that cannot run (T072). Read the handler's source instead.
    """
    endpoint = getattr(route, "endpoint", None)
    if endpoint is None:
        return False
    try:
        return "is_ai_enabled" in inspect.getsource(endpoint)
    except (OSError, TypeError):
        return False


@functools.cache
def _hooks_hitting_gated_endpoints() -> frozenset[str]:
    """Mutation hooks whose request lands on a gated endpoint."""
    gated = _gated_endpoints()
    method_of = {"post": "POST", "put": "PUT", "patch": "PATCH", "delete": "DELETE"}
    hooks: set[str] = set()
    for path in (FRONTEND / "src" / "hooks" / "api").glob("*.ts*"):
        source = path.read_text(encoding="utf-8")
        for match in re.finditer(r"export function (use\w+)\(", source):
            name, start = match.group(1), match.end()
            nxt = source.find("\nexport ", start)
            body = source[start : nxt if nxt > 0 else len(source)]
            if "useMutation" not in body:
                continue
            # ``api.post<T>(`` — the generic is optional but common.
            for call in re.finditer(
                r"api\.(post|put|patch|delete)(?:<[^>(]*>)?\(\s*[`\"']([^`\"']+)", body
            ):
                endpoint = re.sub(r"\$\{[^}]*\}", "{}", call.group(2)).rstrip("/")
                endpoint = endpoint.split("?")[0].rstrip("/")
                # A hook may append an interpolated query string to the path
                # (``/documents/reprocess${qs}``), which normalises to a
                # trailing ``{}`` and matched no route — so ``useReprocessDocuments``
                # looked ungated and its button went unchecked for a whole
                # convergence pass. Try the trimmed form too; over-matching only
                # ever asks for *more* gating, which is the safe direction.
                candidates = {endpoint}
                if endpoint.endswith("{}"):
                    candidates.add(endpoint[:-2].rstrip("/"))
                if any(
                    (method_of[call.group(1)], "/api/v1" + c) in gated
                    for c in candidates
                ):
                    hooks.add(name)
    return frozenset(hooks)


@functools.cache
def _gated_components() -> frozenset[str]:
    """Components that consult the AI state themselves.

    A page that renders one of these is gated *through* it — the shared
    ``EntityConfigCard`` carries the gate for five config tabs, so a file-level
    marker scan would report all five as ungated forever. Mirrors the backend
    guard's handling of helpers that enforce the gate for their callers.
    """
    names: set[str] = set()
    for _tsx, source in _tsx_sources():
        if not any(marker in source for marker in _GATE_MARKERS):
            continue
        names.update(re.findall(r"export function ([A-Z]\w+)", source))
        names.update(re.findall(r"export const ([A-Z]\w+)", source))
    return frozenset(names)


@functools.cache
def _ungated_call_sites() -> dict[str, list[str]]:
    """UI files using such a hook with no sign of consulting the AI state."""
    ai_hooks = _hooks_hitting_gated_endpoints()
    gated_components = _gated_components()
    offenders: dict[str, list[str]] = {}
    for tsx, source in _tsx_sources():
        used = sorted(ai_hooks & _words(source))
        if not used or any(marker in source for marker in _GATE_MARKERS):
            continue
        # Gated one level down, through a component that carries it:
        # ``<Name\b`` for a word-character name is ``<`` then that whole word.
        if gated_components & set(re.findall(r"<(\w+)", source)):
            continue
        offenders[str(tsx.relative_to(FRONTEND / "src"))] = used
    return offenders


_GATE_IDENTIFIER = re.compile(r"\baiOff\b|\baiDisabled\b")
#: Clickable things. `NoticeAction` is here because the dashboard's "Re-embed
#: all" is one, and a hardcoded list of tag names is exactly the kind of list
#: this feature keeps getting caught by — add to it when a new control appears.
_CONTROL_TAGS = (
    "Button",
    "DropdownMenuItem",
    "MenuItem",
    "IconButton",
    "NoticeAction",
)
_CONTROL_OPEN = re.compile(r"<(" + "|".join(_CONTROL_TAGS) + r")\b")


def _control_tags(source: str):
    """``(offset, opening-tag-text)`` for every control, brace-aware.

    A regex cannot do this: `[^>]*?>` stops at the `>` of an arrow function in
    `onClick={() => …}`, so the tag text was truncated before the mutate call
    and the control looked harmless. That is precisely how the knowledge-base
    "Reprocess" button — a re-embed, i.e. a real provider call — stayed enabled.
    """
    for match in _CONTROL_OPEN.finditer(source):
        depth, i = 0, match.end()
        while i < len(source):
            char = source[i]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
            elif char == ">" and depth == 0 and source[i - 1] != "=":
                yield match.start(), source[match.start() : i + 1]
                break
            i += 1


def _handlers_reaching(source: str, variables: set[str]) -> set[str]:
    """Handler names whose body calls ``.mutate`` on one of ``variables``."""
    found: set[str] = set()
    for match in re.finditer(r"const (\w+)\s*=\s*(?:async\s*)?\([^)]*\)\s*=>\s*\{", source):
        name, start = match.group(1), match.end()
        depth, i = 1, start
        while i < len(source) and depth:
            depth += (source[i] == "{") - (source[i] == "}")
            i += 1
        body = source[start:i]
        if any(re.search(rf"\b{v}\.(?:mutate|mutateAsync)\s*\(", body) for v in variables):
            found.add(name)
    return found


def _ungated_controls() -> dict[str, list[int]]:
    """Controls **declared in this file** that start an AI action with no gate.

    Sharper than :func:`_ungated_call_sites`, which asks only whether a file
    consults the AI state *anywhere*. That granularity is what let the "Test
    Tool" button ship enabled in ``CustomToolDetail.tsx``: the file gates its
    Delete button, so the whole file counted as gated.

    Deliberately limited to controls in the same file as the handler. Pages like
    ``agent-detail/ConfigTab.tsx`` hold 25 mutations and *no* control — the
    buttons live in child components that carry their own gate — so following
    props across components would report those as offenders forever. Undecidable
    statically, so this check stays quiet about them rather than crying wolf.
    """
    ai_hooks = _hooks_hitting_gated_endpoints()
    offenders: dict[str, list[int]] = {}
    for tsx, source in _tsx_sources():
        used = sorted(ai_hooks & _words(source))
        if not used:
            continue
        variables: set[str] = set()
        for hook in used:
            variables.update(
                re.findall(rf"(?:const|let)\s+(\w+)\s*=\s*{hook}\s*\(", source)
            )
        if not variables:
            continue
        handlers = _handlers_reaching(source, variables)
        # A control may gate on a variable *derived* from the hook rather than on
        # `aiOff` itself — `const blocked = isPending || aiOff` exists because
        # folding the AI state into a variable that also drives spinners and
        # labels made a form claim a save was in flight forever (/code-review,
        # 2026-08-28). One level of derivation is followed; naming the variable
        # is the author's business, gating is what this checks.
        derived = {
            m.group(1)
            for m in re.finditer(r"(?:const|let)\s+(\w+)\s*=\s*[^;\n]*\b(?:aiOff|aiDisabled)\b", source)
        }
        gate_re = (
            re.compile(r"\b(?:aiOff|aiDisabled|" + "|".join(map(re.escape, derived)) + r")\b")
            if derived
            else _GATE_IDENTIFIER
        )
        bad: list[int] = []
        for offset, tag in _control_tags(source):
            if "onClick" not in tag and "onSelect" not in tag:
                continue
            starts_ai = any(re.search(rf"\b{h}\b", tag) for h in handlers) or any(
                re.search(rf"\b{v}\.(?:mutate|mutateAsync)", tag) for v in variables
            )
            if not starts_ai or gate_re.search(tag):
                continue
            before = source[max(0, offset - 260) : offset]
            if any(
                w in before
                for w in ("AiDisabledTip", "AiActionGate", "AgenticGateDialog")
            ):
                continue
            bad.append(source[:offset].count("\n") + 1)
        if bad:
            offenders[str(tsx.relative_to(FRONTEND / "src"))] = bad
    return offenders


@pytest.mark.skipif(not FRONTEND.is_dir(), reason="frontend not checked out")
def test_a_conditional_refusal_gives_the_ui_something_to_gate_on():
    """A route that refuses on more than the AI state needs a client-side signal.

    ``run_flow`` refuses only when the flow's graph needs AI, so ``aiOff`` alone
    cannot disable its button — the UI must derive the same verdict. It does, as
    ``heldBackByAi`` in the flow builder, which mirrors the backend walk and
    deliberately excludes ``flow.call`` because the callee is not visible client
    side. If a second conditional gate appears, it needs the same treatment, so
    this pins the set rather than letting one arrive unnoticed.
    """
    conditional = {"run_flow"}
    builder = (FRONTEND / "src" / "pages" / "flow-builder" / "index.tsx").read_text(
        encoding="utf-8"
    )
    assert "heldBackByAi" in builder, "flow builder lost its AI-dependency signal"
    source = inspect.getsource(__import__("app.api.v1.flow_runs", fromlist=["x"]))
    assert "flow_requires_ai" in source, (
        f"the conditional gate for {conditional} moved — re-check the UI signal"
    )


@pytest.mark.skipif(not FRONTEND.is_dir(), reason="frontend not checked out")
def test_no_control_starts_an_ai_action_without_a_gate():
    """FR-018, per control rather than per file.

    The file-level check above cannot see a page that gates one button and not
    the next, which is exactly how the flow-builder Run button and eleven others
    survived T071's sweep.
    """
    offenders = _ungated_controls()
    assert not offenders, (
        "these controls start an AI action but stay enabled with AI off "
        f"(file: lines): {offenders}"
    )


@pytest.mark.skipif(not FRONTEND.is_dir(), reason="frontend not checked out")
def test_the_endpoint_list_comes_from_the_app_not_a_hand_written_list():
    """The premise of this file. If the route walk breaks, every assertion below
    passes vacuously — which is how a guard rots without anyone noticing."""
    gated = _gated_endpoints()
    assert len(gated) > 100, f"only {len(gated)} gated routes found — walk broken?"
    hooks = _hooks_hitting_gated_endpoints()
    assert len(hooks) > 50, f"only {len(hooks)} hooks matched — matcher broken?"


@pytest.mark.skipif(not FRONTEND.is_dir(), reason="frontend not checked out")
def test_no_new_ungated_ui_call_site_is_added():
    """FR-018: an AI action must be visibly unavailable, not merely refused.

    A file appearing here is a button that stays enabled on an AI-disabled
    platform and fails when used. Gate it with ``useAiActionDisabled`` +
    ``AiDisabledTip`` rather than adding it to the known list.
    """
    new = set(_ungated_call_sites()) - _KNOWN_UNGATED_CALL_SITES
    assert not new, (
        "these call a gated endpoint with no AI gate — disable the button "
        f"instead of letting it refuse on click: {sorted(new)}"
    )


@pytest.mark.skipif(not FRONTEND.is_dir(), reason="frontend not checked out")
def test_the_known_gap_list_only_shrinks():
    """An entry that no longer matches has been fixed — drop it.

    Keeping stale entries would let a real regression hide behind a name that
    was already dealt with.
    """
    stale = _KNOWN_UNGATED_CALL_SITES - set(_ungated_call_sites())
    assert not stale, (
        f"gated since the list was written — remove from the list: {sorted(stale)}"
    )
