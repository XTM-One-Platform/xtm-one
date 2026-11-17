# SPDX-License-Identifier: Apache-2.0

"""Viewer-safe gradients in a PDF printed by Chromium (issue #10727).

Chromium's PDF backend writes a CSS gradient with a translucent stop
(``transparent``, ``color-mix(..., transparent)``, an ``rgba()`` colour) as a
Type 1 (function-based) shading whose colour is a PostScript calculator
function (FunctionType 4): the gradient's premultiplied colour, divided back
by its alpha with ``roll`` and ``div``. pdfium and Chrome paint it right; other
viewers (the Notion PDF viewer among them) paint it purple or magenta.
Opaque gradients come out as plain Type 2 / Type 3 shadings and need nothing.

:func:`make_shadings_viewer_safe` rewrites each such shading as the shading
it stands for: a Type 2 (axial) shading when its colour depends on ``x``
alone, a Type 3 (radial) one when it depends on the distance to the origin
alone, with a FunctionType 3 stitching of FunctionType 2 (linear) pieces in
the shading's own colour space, sampled from the calculator function. The
geometry stays where it was: the pattern's matrix is untouched, the shading's
domain becomes its ``/BBox``, and both ends extend as the calculator function
clamps. Which form a shading takes is never guessed from its program text: the
program is evaluated (:func:`evaluate_calculator`) over the shading's domain
and the rewrite happens only when the colour proves to be a function of ``x``
or of the radius, clamped past both ends; anything else (a repeating or
focal gradient) is left as Chromium wrote it.
"""

from __future__ import annotations

import io
import itertools
import logging
import math
import re
from collections.abc import Callable, Sequence
from typing import Any

logger = logging.getLogger(__name__)

#: Grid of the sampled colour function over ``t`` in ``[0, 1]``: a stop that
#: falls between two grid points is bent over at most 1/512 of the gradient.
SAMPLES = 512

#: Largest gap, in colour units, between a linear piece and the sampled
#: colour (half a step of an 8-bit channel).
TOLERANCE = 0.5 / 255

#: Points of the domain the colour is checked on before a rewrite, per axis.
CHECK_GRID = 9

#: Largest gap between the calculator function at a point and the 1-D
#: function it is rewritten as, read at the same ``t``, and between a
#: gradient's end colour and its colour past the end: Chromium writes the
#: stop colours and the slopes that reach them rounded apart (``.52 * .031674``
#: against ``.0165``), so one 8-bit step, not floating-point noise.
CHECK_EPSILON = 1 / 255

_TOKEN = re.compile(r"[{}]|[^\s{}]+")


class CalculatorError(ValueError):
    """A calculator function this module cannot evaluate or rewrite."""


def parse_calculator(source: bytes | str) -> list[Any]:
    """The body of a PostScript calculator function as nested lists.

    Numbers become floats, operators stay strings, ``{ ... }`` becomes a list.
    """
    text = source.decode("latin-1") if isinstance(source, bytes) else source
    stack: list[list[Any]] = [[]]
    for word in _TOKEN.findall(text):
        if word == "{":
            stack.append([])
        elif word == "}":
            if len(stack) < 2:
                raise CalculatorError("unbalanced braces")
            done = stack.pop()
            stack[-1].append(done)
        else:
            try:
                stack[-1].append(float(word))
            except ValueError:
                stack[-1].append(word)
    if len(stack) != 1 or len(stack[0]) != 1 or not isinstance(stack[0][0], list):
        raise CalculatorError("a calculator function is one procedure")
    return stack[0][0]


def _binary(op: Callable[[float, float], float]) -> Callable[[list[Any]], None]:
    def run(stack: list[Any]) -> None:
        b = stack.pop()
        a = stack.pop()
        stack.append(op(a, b))

    return run


def _unary(op: Callable[[float], float]) -> Callable[[list[Any]], None]:
    def run(stack: list[Any]) -> None:
        stack.append(op(stack.pop()))

    return run


def _roll(stack: list[Any]) -> None:
    j = int(stack.pop())
    n = int(stack.pop())
    if n < 0 or n > len(stack):
        raise CalculatorError("roll past the stack")
    if n:
        top = stack[-n:]
        j %= n
        stack[-n:] = top[-j:] + top[:-j] if j else top


def _copy(stack: list[Any]) -> None:
    n = int(stack.pop())
    if n < 0 or n > len(stack):
        raise CalculatorError("copy past the stack")
    stack.extend(stack[-n:] if n else [])


def _index(stack: list[Any]) -> None:
    n = int(stack.pop())
    if n < 0 or n >= len(stack):
        raise CalculatorError("index past the stack")
    stack.append(stack[-1 - n])


def _div(a: float, b: float) -> float:
    if b == 0:
        raise CalculatorError("division by zero")
    return a / b


def _pop(stack: list[Any]) -> None:
    stack.pop()


def _exch(stack: list[Any]) -> None:
    stack[-1], stack[-2] = stack[-2], stack[-1]


def _atan(stack: list[Any]) -> None:
    den = stack.pop()
    num = stack.pop()
    stack.append(math.degrees(math.atan2(num, den)) % 360)


_OPERATORS: dict[str, Callable[[list[Any]], None]] = {
    "add": _binary(lambda a, b: a + b),
    "sub": _binary(lambda a, b: a - b),
    "mul": _binary(lambda a, b: a * b),
    "div": _binary(_div),
    "idiv": _binary(lambda a, b: float(int(a / b)) if b else _div(a, b)),
    "mod": _binary(lambda a, b: float(math.fmod(int(a), int(b))) if int(b) else _div(a, 0)),
    "exp": _binary(lambda a, b: a**b),
    "max": _binary(max),
    "min": _binary(min),
    "eq": _binary(lambda a, b: a == b),
    "ne": _binary(lambda a, b: a != b),
    "gt": _binary(lambda a, b: a > b),
    "ge": _binary(lambda a, b: a >= b),
    "lt": _binary(lambda a, b: a < b),
    "le": _binary(lambda a, b: a <= b),
    "and": _binary(lambda a, b: a and b if isinstance(a, bool) else float(int(a) & int(b))),
    "or": _binary(lambda a, b: a or b if isinstance(a, bool) else float(int(a) | int(b))),
    "xor": _binary(lambda a, b: a != b if isinstance(a, bool) else float(int(a) ^ int(b))),
    "abs": _unary(abs),
    "neg": _unary(lambda a: -a),
    "sqrt": _unary(lambda a: math.sqrt(a) if a >= 0 else _div(0, 0)),
    "sin": _unary(lambda a: math.sin(math.radians(a))),
    "cos": _unary(lambda a: math.cos(math.radians(a))),
    "ln": _unary(lambda a: math.log(a) if a > 0 else _div(0, 0)),
    "log": _unary(lambda a: math.log10(a) if a > 0 else _div(0, 0)),
    "cvi": _unary(lambda a: float(int(a))),
    "cvr": _unary(float),
    "floor": _unary(lambda a: float(math.floor(a))),
    "ceiling": _unary(lambda a: float(math.ceil(a))),
    "round": _unary(lambda a: float(math.floor(a + 0.5))),
    "truncate": _unary(lambda a: float(math.trunc(a))),
    "not": _unary(lambda a: not a if isinstance(a, bool) else float(~int(a))),
    "pop": _pop,
    "dup": lambda s: s.append(s[-1]),
    "exch": _exch,
    "roll": _roll,
    "copy": _copy,
    "index": _index,
    "atan": _atan,
    "true": lambda s: s.append(True),
    "false": lambda s: s.append(False),
}


def _execute(proc: list[Any], stack: list[Any]) -> None:
    i = 0
    while i < len(proc):
        item = proc[i]
        if isinstance(item, (float, list)):
            stack.append(item)
        elif item == "if":
            body = stack.pop()
            if stack.pop() is True:
                _execute(body, stack)
        elif item == "ifelse":
            otherwise = stack.pop()
            then = stack.pop()
            _execute(then if stack.pop() is True else otherwise, stack)
        elif item in _OPERATORS:
            _OPERATORS[item](stack)
        else:
            raise CalculatorError(f"operator {item!r} is not supported")
        i += 1


def evaluate_calculator(program: list[Any], inputs: Sequence[float], outputs: int) -> tuple[float, ...]:
    """Run *program* (from :func:`parse_calculator`) on *inputs*; its last *outputs* values, clamped to [0, 1]."""
    stack: list[Any] = [float(v) for v in inputs]
    try:
        _execute(program, stack)
    except (IndexError, TypeError, OverflowError) as exc:
        raise CalculatorError(f"the program failed: {exc}") from exc
    if len(stack) < outputs or any(not isinstance(v, float) for v in stack[-outputs:]):
        raise CalculatorError("the program left no colour")
    return tuple(min(1.0, max(0.0, v)) for v in stack[-outputs:])


def _axial(x: float, y: float) -> float:
    return x


def _radial(x: float, y: float) -> float:
    return math.hypot(x, y)


def _profile(
    program: list[Any], outputs: int, domain: Sequence[float]
) -> tuple[str, Callable[[float], tuple[float, ...]]] | None:
    """``("axial" | "radial", colour of t)`` when the program is one, else ``None``.

    The colour of *t* is the program read at ``(t, 0)``; it is the shading's
    colour when, at every checked point of the domain, the program answers
    the colour of that point's ``t``. Past ``t = 1`` (and below ``t = 0``
    for an axial one) it must hold the end's colour, as an extended Type 2 /
    Type 3 shading paints it.
    """
    x0, x1, y0, y1 = domain

    def colour(t: float) -> tuple[float, ...]:
        return evaluate_calculator(program, (t, 0.0), outputs)

    points = [
        (x0 + (x1 - x0) * i / (CHECK_GRID - 1), y0 + (y1 - y0) * j / (CHECK_GRID - 1))
        for i in range(CHECK_GRID)
        for j in range(CHECK_GRID)
    ]
    for kind, to_t in (("axial", _axial), ("radial", _radial)):
        if all(
            _close(evaluate_calculator(program, point, outputs), colour(to_t(*point)), CHECK_EPSILON)
            for point in points
        ) and _clamped(colour, kind):
            return kind, colour
    return None


def _clamped(colour: Callable[[float], tuple[float, ...]], kind: str) -> bool:
    end = colour(1.0)
    if not all(_close(colour(1.0 + d), end, CHECK_EPSILON) for d in (0.25, 1.0, 4.0)):
        return False
    if kind == "radial":
        return True
    start = colour(0.0)
    return all(_close(colour(-d), start, CHECK_EPSILON) for d in (0.25, 1.0, 4.0))


def _close(a: Sequence[float], b: Sequence[float], epsilon: float) -> bool:
    return all(abs(p - q) <= epsilon for p, q in zip(a, b, strict=True))


def linear_pieces(values: Sequence[Sequence[float]], tolerance: float = TOLERANCE) -> list[int]:
    """The grid indices that cut *values* (sampled on an even grid) into linear pieces.

    Greedy: each piece runs as far as every sample it covers stays within
    *tolerance* of the straight line between its two ends. Always starts
    with 0 and ends with ``len(values) - 1``.
    """
    last = len(values) - 1
    cuts = [0]
    start = 0
    while start < last:
        end = start + 1
        while end < last and _fits(values, start, end + 1, tolerance):
            end += 1
        cuts.append(end)
        start = end
    return cuts


def _fits(values: Sequence[Sequence[float]], start: int, end: int, tolerance: float) -> bool:
    a, b = values[start], values[end]
    span = end - start
    for k in range(start + 1, end):
        f = (k - start) / span
        if any(abs(av + (bv - av) * f - v) > tolerance for av, bv, v in zip(a, b, values[k], strict=True)):
            return False
    return True


def _rewrite(shading: Any, function: Any) -> bool:
    """Rewrite one Type 1 shading in place; whether it was rewritten."""
    from pypdf.generic import ArrayObject, BooleanObject, DictionaryObject, FloatObject, NameObject, NumberObject

    if "/Matrix" in shading:
        matrix = [float(v) for v in shading["/Matrix"]]
        if matrix != [1.0, 0.0, 0.0, 1.0, 0.0, 0.0]:
            return False
    domain = [float(v) for v in shading.get("/Domain", [0, 1, 0, 1])]
    outputs = len(function.get("/Range", [])) // 2
    if len(domain) != 4 or outputs < 1:
        return False
    program = parse_calculator(function.get_data())
    found = _profile(program, outputs, domain)
    if found is None:
        return False
    kind, colour = found
    values = [colour(i / SAMPLES) for i in range(SAMPLES + 1)]
    cuts = linear_pieces(values)

    def number(v: float) -> FloatObject:
        return FloatObject(round(v, 6))

    pieces = ArrayObject()
    for start, end in itertools.pairwise(cuts):
        pieces.append(
            DictionaryObject(
                {
                    NameObject("/FunctionType"): NumberObject(2),
                    NameObject("/Domain"): ArrayObject([NumberObject(0), NumberObject(1)]),
                    NameObject("/C0"): ArrayObject(number(v) for v in values[start]),
                    NameObject("/C1"): ArrayObject(number(v) for v in values[end]),
                    NameObject("/N"): NumberObject(1),
                }
            )
        )
    stitched = DictionaryObject(
        {
            NameObject("/FunctionType"): NumberObject(3),
            NameObject("/Domain"): ArrayObject([NumberObject(0), NumberObject(1)]),
            NameObject("/Functions"): pieces,
            NameObject("/Bounds"): ArrayObject(number(c / SAMPLES) for c in cuts[1:-1]),
            NameObject("/Encode"): ArrayObject(NumberObject(v) for _ in pieces for v in (0, 1)),
        }
    )
    colour_space = shading["/ColorSpace"]
    x0, x1, y0, y1 = domain
    coords = [0, 0, 1, 0] if kind == "axial" else [0, 0, 0, 0, 0, 1]
    shading.clear()
    shading.update(
        {
            NameObject("/ShadingType"): NumberObject(2 if kind == "axial" else 3),
            NameObject("/ColorSpace"): colour_space,
            NameObject("/Coords"): ArrayObject(NumberObject(v) for v in coords),
            NameObject("/Domain"): ArrayObject([NumberObject(0), NumberObject(1)]),
            NameObject("/Extend"): ArrayObject([BooleanObject(True), BooleanObject(True)]),
            NameObject("/BBox"): ArrayObject(number(v) for v in (x0, y0, x1, y1)),
            NameObject("/Function"): stitched,
        }
    )
    return True


def _calculator_shadings(root: Any) -> list[tuple[Any, Any]]:
    """Every Type 1 shading reachable from *root* whose function is a calculator function."""
    from pypdf.generic import ArrayObject, DictionaryObject, IndirectObject

    found: list[tuple[Any, Any]] = []
    seen: set[int] = set()
    pending: list[Any] = [root]
    while pending:
        obj = pending.pop()
        if isinstance(obj, IndirectObject):
            obj = obj.get_object()
        if id(obj) in seen:
            continue
        seen.add(id(obj))
        if isinstance(obj, DictionaryObject):
            if obj.get("/ShadingType") == 1:
                function = obj.get("/Function")
                function = function.get_object() if function is not None else None
                if isinstance(function, DictionaryObject) and function.get("/FunctionType") == 4:
                    found.append((obj, function))
            pending.extend(value for key, value in obj.items() if key != "/Parent")
        elif isinstance(obj, ArrayObject):
            pending.extend(obj)
    return found


#: Keys whose value is a transfer function (or an array of them): a soft
#: mask's ``/TR`` and an ExtGState's ``/TR`` / ``/TR2``.
_TRANSFER_KEYS = ("/TR", "/TR2")


def _sampled_function(function: Any) -> Any | None:
    """A FunctionType 2 / 3 equal to the 1-input calculator *function*, or ``None``.

    The program is evaluated on :data:`SAMPLES` + 1 points of its domain and
    cut into linear pieces (:func:`linear_pieces`): Chromium's soft-mask
    transfer ``{1 exch sub}`` (alpha inversion) is one piece, written as
    ``/FunctionType 2 /C0 [1] /C1 [0] /N 1``. Only a function whose range is
    ``[0, 1]`` per output is rewritten, as :func:`evaluate_calculator` clamps there.
    """
    from pypdf.generic import ArrayObject, DictionaryObject, FloatObject, NameObject, NumberObject

    domain = [float(v) for v in function.get("/Domain", [])]
    bounds = [float(v) for v in function.get("/Range", [])]
    outputs = len(bounds) // 2
    if len(domain) != 2 or domain[1] <= domain[0] or outputs < 1 or bounds != [0.0, 1.0] * outputs:
        return None
    program = parse_calculator(function.get_data())
    low, high = domain
    values = [evaluate_calculator(program, (low + (high - low) * i / SAMPLES,), outputs) for i in range(SAMPLES + 1)]
    cuts = linear_pieces(values)

    def number(v: float) -> FloatObject:
        return FloatObject(round(v, 6))

    def linear(start: int, end: int, piece_domain: Sequence[float]) -> Any:
        return DictionaryObject(
            {
                NameObject("/FunctionType"): NumberObject(2),
                NameObject("/Domain"): ArrayObject(number(v) for v in piece_domain),
                NameObject("/C0"): ArrayObject(number(v) for v in values[start]),
                NameObject("/C1"): ArrayObject(number(v) for v in values[end]),
                NameObject("/N"): NumberObject(1),
            }
        )

    if len(cuts) == 2:
        return linear(0, SAMPLES, domain)
    return DictionaryObject(
        {
            NameObject("/FunctionType"): NumberObject(3),
            NameObject("/Domain"): ArrayObject(number(v) for v in domain),
            NameObject("/Functions"): ArrayObject(linear(s, e, (0, 1)) for s, e in itertools.pairwise(cuts)),
            NameObject("/Bounds"): ArrayObject(number(low + (high - low) * c / SAMPLES) for c in cuts[1:-1]),
            NameObject("/Encode"): ArrayObject(NumberObject(v) for _ in cuts[1:] for v in (0, 1)),
        }
    )


def _rewrite_transfers(root: Any) -> tuple[int, int]:
    """Replace every calculator transfer function reachable from *root*; ``(rewritten, kept)``.

    Chromium writes the alpha inversion of a soft mask (any translucent
    element, not only a gradient) as one shared FunctionType 4 object
    referenced as ``/TR`` from each ``/SMask``. Each reference is pointed at
    the sampled equivalent; the calculator object is left an orphan and
    dropped when the file is written.
    """
    from pypdf.generic import ArrayObject, DictionaryObject, IndirectObject, NameObject

    replacements: dict[int, Any] = {}
    rewritten = kept = 0

    def replacement(value: Any) -> Any | None:
        nonlocal rewritten, kept
        function = value.get_object() if isinstance(value, IndirectObject) else value
        if not isinstance(function, DictionaryObject) or function.get("/FunctionType") != 4:
            return None
        key = id(function)
        if key not in replacements:
            try:
                replacements[key] = _sampled_function(function)
            except CalculatorError as exc:
                logger.debug("PDF transfer function left as printed: %s", exc)
                replacements[key] = None
            if replacements[key] is None:
                kept += 1
            else:
                rewritten += 1
        return replacements[key]

    seen: set[int] = set()
    pending: list[Any] = [root]
    while pending:
        obj = pending.pop()
        if isinstance(obj, IndirectObject):
            obj = obj.get_object()
        if id(obj) in seen:
            continue
        seen.add(id(obj))
        if isinstance(obj, DictionaryObject):
            for name in _TRANSFER_KEYS:
                value = obj.get(name)
                if value is None:
                    continue
                resolved = value.get_object() if isinstance(value, IndirectObject) else value
                if isinstance(resolved, ArrayObject):
                    for i, item in enumerate(resolved):
                        new = replacement(item)
                        if new is not None:
                            resolved[i] = new
                else:
                    new = replacement(value)
                    if new is not None:
                        obj[NameObject(name)] = new
            pending.extend(value for key, value in obj.items() if key != "/Parent")
        elif isinstance(obj, ArrayObject):
            pending.extend(obj)
    return rewritten, kept


def make_shadings_viewer_safe(pdf: bytes) -> bytes:
    """*pdf* with every calculator function rewritten as an equivalent plain function or shading.

    Calculator-function gradients become axial or radial shadings; calculator
    transfer functions (a soft mask's ``/TR``, issue #10748) become
    FunctionType 2 / 3 functions. Returns *pdf* itself, untouched, when it
    holds no calculator function the rewrite can prove equivalent; otherwise
    the rewritten file, with the replaced calculator functions dropped.
    """
    if b"/FunctionType" not in pdf and b"/ObjStm" not in pdf:
        return pdf
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(pdf)))
    rewritten = kept = 0
    for shading, function in _calculator_shadings(writer.root_object):
        try:
            done = _rewrite(shading, function)
        except CalculatorError as exc:
            logger.debug("PDF shading left as printed: %s", exc)
            done = False
        if done:
            rewritten += 1
        else:
            kept += 1
    transfers, transfers_kept = _rewrite_transfers(writer.root_object)
    if not rewritten and not transfers:
        return pdf
    if kept or transfers_kept:
        logger.info(
            "PDF shadings: %d rewritten, %d kept; transfer functions: %d rewritten, %d kept as printed",
            rewritten,
            kept,
            transfers,
            transfers_kept,
        )
    writer.compress_identical_objects(remove_identicals=False, remove_orphans=True)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()
