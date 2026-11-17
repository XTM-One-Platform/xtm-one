# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""Declared tests of a custom tool: data cases, test code, and the dry run.

A custom tool declares its tests in two forms, both optional:

- **Data cases** (``CustomTool.test_cases``): a name, the inputs, whether the
  case is a dry run, and expectations on the output (``equals``,
  ``contains``, ``not_contains``, ``regex``, ``json_subset``) or on a failure
  (``expect_error`` / ``error_contains``). Each case is one sandboxed run of
  the tool, exactly as an agent runs it.
- **Test code** (``CustomTool.test_code``): test functions in the tool's
  language, run by a small harness in the same sandbox. Python collects
  ``test_*`` functions and ``unittest.TestCase`` classes; JavaScript collects
  the ``test(name, fn)`` / ``it(name, fn)`` calls and offers ``node:assert``
  as ``assert``. Both call the tool through ``run_tool(inputs, dry_run=...)``
  (``runTool(inputs, { dryRun })`` in JavaScript), which returns what the
  tool printed and raises ``ToolError`` when the tool fails. Only the
  standard library is available: the sandbox images carry no pytest.

**The dry run is a contract, not a guarantee.** Every run of a tool receives a
``dry_run`` variable (``$dry_run`` in JavaScript): ``False`` for an agent's
call, ``True`` for a dry-run case. A tool that writes must check it and skip
its side effects; the sandbox cannot do it for the tool, because a tool needs
the network to read too. :func:`app.services.custom_tool_executor.execute_tool_auto`
injects it, so the variable exists on every path.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

#: Most data cases one tool may declare.
MAX_TEST_CASES = 50
#: Longest test code, in characters.
MAX_TEST_CODE_CHARS = 200_000
#: Longest output or message kept per result.
RESULT_TEXT_MAX_CHARS = 4000
#: The line the test-code harness prints its results on.
RESULTS_MARKER = "__XTM_TEST_RESULTS__"
#: A test-code run executes several tool runs: its timeout is the tool's,
#: multiplied, within this ceiling (seconds).
CODE_TESTS_TIMEOUT_FACTOR = 3
CODE_TESTS_TIMEOUT_MAX = 1800

#: A run's overall status. ``no_tests`` - nothing was declared (or selected).
TEST_RUN_STATUSES = ("passed", "failed", "error", "no_tests")
#: Statuses that let the repository sync activate a new version of a tool.
ACTIVATING_STATUSES = frozenset({"passed", "no_tests"})


# ---------------------------------------------------------------------------
# Data cases
# ---------------------------------------------------------------------------


def _as_list(value: Any) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return list(value)
    raise ValueError("must be a string or a list of strings")


class ToolTestExpectation(BaseModel):
    """What a case's output must look like. Every given check must hold."""

    model_config = {"extra": "forbid"}

    equals: str | None = None
    contains: list[str] | None = None
    not_contains: list[str] | None = None
    regex: str | None = Field(default=None, max_length=2000)
    json_subset: Any = None

    @field_validator("contains", "not_contains", mode="before")
    @classmethod
    def _listify(cls, value: Any) -> list[str] | None:
        return _as_list(value)

    @field_validator("regex")
    @classmethod
    def _compiles(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                re.compile(value)
            except re.error as exc:
                raise ValueError(f"invalid regular expression: {exc}") from None
        return value


class ToolTestCase(BaseModel):
    """One data case: the inputs of a run and what it must produce."""

    model_config = {"extra": "forbid"}

    name: str = Field(min_length=1, max_length=200)
    inputs: dict[str, Any] = Field(default_factory=dict)
    dry_run: bool = False
    expect: ToolTestExpectation | None = None
    expect_error: bool = False
    error_contains: str | None = None

    @field_validator("name")
    @classmethod
    def _strip(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("a test case needs a name")
        return value

    @model_validator(mode="after")
    def _error_contains_needs_expect_error(self) -> ToolTestCase:
        if self.error_contains and not self.expect_error:
            self.expect_error = True
        return self


def validate_test_cases(raw: Any) -> list[dict[str, Any]] | None:
    """Normalize declared data cases, or raise ``ValueError`` naming the bad one.

    ``None`` and an empty list both mean "no data cases" and return ``None``.
    """
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise ValueError("Test cases must be a list.")
    if len(raw) > MAX_TEST_CASES:
        raise ValueError(f"A tool declares at most {MAX_TEST_CASES} test cases.")
    cases: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        try:
            case = ToolTestCase.model_validate(item)
        except ValidationError as exc:
            first = exc.errors()[0]
            where = ".".join(str(p) for p in first.get("loc", ()))
            label = item.get("name") if isinstance(item, dict) else None
            raise ValueError(
                f"Test case {label or index + 1}: {where + ': ' if where else ''}{first.get('msg')}"
            ) from None
        if case.name in seen:
            raise ValueError(f"Two test cases are named '{case.name}'.")
        seen.add(case.name)
        cases.append(case.model_dump(exclude_none=True, exclude_defaults=False))
    return cases or None


def validate_test_code(raw: Any) -> str | None:
    """Normalize declared test code (blank is none), or raise ``ValueError``."""
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ValueError("Test code must be text.")
    if len(raw) > MAX_TEST_CODE_CHARS:
        raise ValueError(f"Test code is limited to {MAX_TEST_CODE_CHARS} characters.")
    return raw if raw.strip() else None


def json_subset_matches(expected: Any, actual: Any) -> bool:
    """Whether *actual* contains *expected*.

    An object matches when each expected key is present with a matching value;
    a list when each expected item matches a distinct actual item, in order;
    anything else by equality.
    """
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            key in actual and json_subset_matches(value, actual[key])
            for key, value in expected.items()
        )
    if isinstance(expected, list):
        if not isinstance(actual, list):
            return False
        position = 0
        for item in expected:
            while position < len(actual) and not json_subset_matches(item, actual[position]):
                position += 1
            if position == len(actual):
                return False
            position += 1
        return True
    return bool(expected == actual)


def check_expectations(expect: dict[str, Any] | None, output: str) -> list[str]:
    """The failed checks of *expect* against *output* (empty: all hold)."""
    if not expect:
        return []
    failures: list[str] = []
    text = output.strip()
    if "equals" in expect and expect["equals"] is not None and text != str(expect["equals"]).strip():
        failures.append("The output is not equal to the expected text.")
    for needle in expect.get("contains") or []:
        if needle not in output:
            failures.append(f"The output does not contain {needle!r}.")
    for needle in expect.get("not_contains") or []:
        if needle in output:
            failures.append(f"The output contains {needle!r}.")
    pattern = expect.get("regex")
    if pattern and not re.search(pattern, output, re.MULTILINE):
        failures.append(f"The output does not match /{pattern}/.")
    if "json_subset" in expect and expect["json_subset"] is not None:
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            failures.append("The output is not JSON.")
        else:
            if not json_subset_matches(expect["json_subset"], parsed):
                failures.append("The JSON output does not contain the expected values.")
    return failures


# ---------------------------------------------------------------------------
# Test-code harnesses
# ---------------------------------------------------------------------------


def build_python_harness(tool_code: str, test_code: str, *, force_dry_run: bool) -> str:
    """A Python program that runs *test_code* against *tool_code*.

    It runs as a custom tool itself (same sandbox, same import blocklist) and
    prints one ``RESULTS_MARKER`` line with the results as JSON.
    """
    return f'''\
import contextlib as _xtm_contextlib
import io as _xtm_io
import json as _xtm_results_json
import keyword as _xtm_keyword
import re as _xtm_re
import time as _xtm_time
import traceback as _xtm_traceback
import types as _xtm_types
import unittest

_XTM_TOOL_SRC = {tool_code!r}
_XTM_TEST_SRC = {test_code!r}
_XTM_FORCE_DRY_RUN = {bool(force_dry_run)!r}


class ToolError(Exception):
    """The tool failed: it raised, or exited with a non-zero status."""


def _xtm_identifier(key, used):
    candidate = _xtm_re.sub(r"[^a-zA-Z0-9_]", "_", key)
    if not candidate or candidate[0].isdigit():
        candidate = "_" + candidate
    if _xtm_keyword.iskeyword(candidate):
        candidate = candidate + "_"
    if candidate in used:
        suffix = 2
        while candidate + "_" + str(suffix) in used:
            suffix += 1
        candidate = candidate + "_" + str(suffix)
    used.add(candidate)
    return candidate


def run_tool(inputs=None, dry_run=False):
    """Run the tool with *inputs*; return what it printed."""
    values = dict(inputs or {{}})
    values.setdefault("$files", [])
    values["dry_run"] = bool(dry_run or _XTM_FORCE_DRY_RUN)
    namespace = {{"__name__": "__xtm_tool__", "__xtm_tool_inputs__": values}}
    used = {{"__xtm_tool_inputs__"}}
    for key, value in values.items():
        namespace[_xtm_identifier(key, used)] = value
    buffer = _xtm_io.StringIO()
    try:
        with _xtm_contextlib.redirect_stdout(buffer):
            exec(compile(_XTM_TOOL_SRC, "xtm_tool.py", "exec"), namespace)
    except SystemExit as exc:
        if exc.code not in (None, 0):
            raise ToolError("The tool exited with status " + str(exc.code)) from None
    except Exception as exc:
        raise ToolError(type(exc).__name__ + ": " + str(exc)) from exc
    return buffer.getvalue().strip()


def _xtm_message(exc):
    text = "".join(_xtm_traceback.format_exception_only(type(exc), exc)).strip()
    lines = [f.lineno for f in _xtm_traceback.extract_tb(exc.__traceback__) if f.filename == "xtm_tests.py"]
    return text + (" (line " + str(lines[-1]) + ")" if lines else "")


_xtm_results = []


def _xtm_record(name, started, status, message=None):
    entry = {{"name": name, "status": status, "duration_ms": int((_xtm_time.monotonic() - started) * 1000)}}
    if message:
        entry["message"] = message[-{RESULT_TEXT_MAX_CHARS}:]
    _xtm_results.append(entry)


_xtm_ns = {{"__name__": "__xtm_tests__", "run_tool": run_tool, "ToolError": ToolError, "unittest": unittest}}
_xtm_started = _xtm_time.monotonic()
try:
    exec(compile(_XTM_TEST_SRC, "xtm_tests.py", "exec"), _xtm_ns)
except BaseException as _xtm_exc:
    _xtm_record("(loading the tests)", _xtm_started, "error", _xtm_message(_xtm_exc))
else:
    for _xtm_name, _xtm_obj in list(_xtm_ns.items()):
        if isinstance(_xtm_obj, type) and issubclass(_xtm_obj, unittest.TestCase) and _xtm_obj is not unittest.TestCase:
            for _xtm_test in unittest.TestLoader().loadTestsFromTestCase(_xtm_obj):
                _xtm_started = _xtm_time.monotonic()
                _xtm_result = unittest.TestResult()
                _xtm_test.run(_xtm_result)
                _xtm_label = _xtm_obj.__name__ + "." + _xtm_test._testMethodName
                if _xtm_result.failures:
                    _xtm_record(_xtm_label, _xtm_started, "failed", _xtm_result.failures[0][1].strip())
                elif _xtm_result.errors:
                    _xtm_record(_xtm_label, _xtm_started, "error", _xtm_result.errors[0][1].strip())
                elif _xtm_result.skipped:
                    _xtm_record(_xtm_label, _xtm_started, "skipped", str(_xtm_result.skipped[0][1]))
                else:
                    _xtm_record(_xtm_label, _xtm_started, "passed")
        elif (
            _xtm_name.startswith("test")
            and isinstance(_xtm_obj, _xtm_types.FunctionType)
            and _xtm_obj.__module__ == "__xtm_tests__"
        ):
            _xtm_started = _xtm_time.monotonic()
            try:
                if _xtm_obj.__code__.co_flags & 0x80:  # a coroutine function
                    import asyncio as _xtm_asyncio

                    _xtm_asyncio.run(_xtm_obj())
                else:
                    _xtm_obj()
            except AssertionError as _xtm_exc:
                _xtm_record(_xtm_name, _xtm_started, "failed", _xtm_message(_xtm_exc))
            except Exception as _xtm_exc:
                _xtm_record(_xtm_name, _xtm_started, "error", _xtm_message(_xtm_exc))
            else:
                _xtm_record(_xtm_name, _xtm_started, "passed")

print("\\n{RESULTS_MARKER}" + _xtm_results_json.dumps(_xtm_results))
'''


def build_js_harness(tool_code: str, test_code: str, *, force_dry_run: bool) -> str:
    """A JavaScript body that runs *test_code* against *tool_code*.

    It runs as a custom tool itself (the executor wraps it in an async
    function) and prints one ``RESULTS_MARKER`` line with the results.
    """
    return f"""\
const __xtmToolSrc = {json.dumps(tool_code)};
const __xtmTestSrc = {json.dumps(test_code)};
const __xtmForceDryRun = {json.dumps(bool(force_dry_run))};
const __xtmAsyncFunction = Object.getPrototypeOf(async function () {{}}).constructor;
class ToolError extends Error {{}}
const __xtmTests = [];
const __xtmRegister = (name, fn) => {{ __xtmTests.push({{ name: String(name), fn }}); }};
const __xtmAssert = require('node:assert/strict');

async function runTool(inputs = {{}}, options = {{}}) {{
  const values = {{ ...inputs }};
  if (values.$files === undefined) values.$files = [];
  values.dry_run = !!(options.dryRun || __xtmForceDryRun);
  const names = [];
  const args = [];
  const used = new Set();
  for (const [key, value] of Object.entries(values)) {{
    let candidate = key.replace(/[^a-zA-Z0-9_]/g, '_');
    if (used.has(candidate)) {{
      let suffix = 2;
      while (used.has(`${{candidate}}_${{suffix}}`)) suffix += 1;
      candidate = `${{candidate}}_${{suffix}}`;
    }}
    used.add(candidate);
    names.push('$' + candidate);
    args.push(value);
  }}
  let fn;
  try {{
    fn = new __xtmAsyncFunction('require', ...names, __xtmToolSrc);
  }} catch (err) {{
    throw new ToolError(`The tool does not compile: ${{err.message}}`);
  }}
  const chunks = [];
  const previousWrite = process.stdout.write;
  process.stdout.write = (chunk, ...rest) => {{
    chunks.push(typeof chunk === 'string' ? chunk : Buffer.from(chunk).toString('utf8'));
    const callback = rest.find((r) => typeof r === 'function');
    if (callback) callback();
    return true;
  }};
  let value;
  try {{
    value = await fn(require, ...args);
  }} catch (err) {{
    throw new ToolError((err && err.message) || String(err));
  }} finally {{
    process.stdout.write = previousWrite;
  }}
  let out = chunks.join('');
  if (!out && value !== undefined) out = typeof value === 'string' ? value : JSON.stringify(value);
  return String(out).trim();
}}

const __xtmResults = [];
const __xtmRecord = (name, started, status, message) => {{
  const entry = {{ name, status, duration_ms: Date.now() - started }};
  if (message) entry.message = String(message).slice(-{RESULT_TEXT_MAX_CHARS});
  __xtmResults.push(entry);
}};
let __xtmStarted = Date.now();
try {{
  const load = new __xtmAsyncFunction('require', 'test', 'it', 'assert', 'runTool', 'ToolError', __xtmTestSrc);
  await load(require, __xtmRegister, __xtmRegister, __xtmAssert, runTool, ToolError);
}} catch (err) {{
  __xtmRecord('(loading the tests)', __xtmStarted, 'error', (err && err.stack) || String(err));
}}
for (const t of __xtmTests) {{
  __xtmStarted = Date.now();
  try {{
    await t.fn();
    __xtmRecord(t.name, __xtmStarted, 'passed');
  }} catch (err) {{
    const failed = err && (err.code === 'ERR_ASSERTION' || err.name === 'AssertionError');
    __xtmRecord(t.name, __xtmStarted, failed ? 'failed' : 'error', (err && err.message) || String(err));
  }}
}}
process.stdout.write('\\n{RESULTS_MARKER}' + JSON.stringify(__xtmResults) + '\\n');
"""


def parse_harness_output(output: str) -> list[dict[str, Any]] | None:
    """The results a harness printed, or ``None`` when it printed none."""
    for line in reversed(output.splitlines()):
        line = line.strip()
        if not line.startswith(RESULTS_MARKER):
            continue
        try:
            raw = json.loads(line[len(RESULTS_MARKER) :])
        except json.JSONDecodeError:
            return None
        if not isinstance(raw, list):
            return None
        results: list[dict[str, Any]] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            status = item.get("status")
            results.append(
                {
                    "kind": "code",
                    "name": str(item.get("name") or "(unnamed)")[:200],
                    "status": status if status in ("passed", "failed", "error", "skipped") else "error",
                    "message": _clip(item.get("message")),
                    "duration_ms": item.get("duration_ms") if isinstance(item.get("duration_ms"), int) else None,
                }
            )
        return results
    return None


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------


def _clip(text: Any) -> str | None:
    if text is None:
        return None
    text = str(text)
    if len(text) <= RESULT_TEXT_MAX_CHARS:
        return text
    return text[: RESULT_TEXT_MAX_CHARS - 1] + "…"


def _failure_message(exc: BaseException) -> str:
    """What a failed run says. The executor raises ``RuntimeError`` with the
    tool's own error; anything else is named by its type only."""
    if isinstance(exc, RuntimeError):
        return str(exc)
    return f"Unexpected {type(exc).__name__} while running the tool."


async def run_tool_tests(
    *,
    code: str,
    language: str,
    timeout: int,
    test_cases: list[dict[str, Any]] | None,
    test_code: str | None,
    user_id: uuid.UUID | None = None,
    db: AsyncSession | None = None,
    case_names: list[str] | None = None,
    include_code_tests: bool = True,
    dry_run_only: bool = False,
) -> dict[str, Any]:
    """Run the declared tests of a tool and return the report.

    *code* and *test_code* already have their platform variables substituted.
    *case_names* restricts the data cases run (``None``: all). *dry_run_only*
    skips the cases that are not dry runs and forces ``dry_run`` on every
    ``run_tool`` call of the test code.
    """
    from app.services.custom_tool_executor import execute_tool_auto

    started = time.monotonic()
    results: list[dict[str, Any]] = []
    language = language or "javascript"

    for case in test_cases or []:
        name = case.get("name", "")
        if case_names is not None and name not in case_names:
            continue
        dry_run = bool(case.get("dry_run"))
        if dry_run_only and not dry_run:
            results.append(
                {"kind": "case", "name": name, "status": "skipped", "dry_run": False,
                 "message": "Not a dry run: skipped by a dry-run-only run."}
            )
            continue
        inputs = dict(case.get("inputs") or {})
        inputs["dry_run"] = dry_run
        inputs.setdefault("$files", [])
        case_started = time.monotonic()
        output: str | None = None
        error: str | None = None
        try:
            output = await execute_tool_auto(
                code, inputs, language=language, timeout=timeout, user_id=user_id, db=db
            )
        except Exception as exc:  # the tool's failure is the case's outcome
            error = _failure_message(exc)
        failures: list[str]
        if case.get("expect_error"):
            if error is None:
                failures = ["The tool succeeded, but the case expects it to fail."]
            elif case.get("error_contains") and case["error_contains"] not in error:
                failures = [f"The error does not contain {case['error_contains']!r}."]
            else:
                failures = []
        elif error is not None:
            failures = [error]
        else:
            failures = check_expectations(case.get("expect"), output or "")
        results.append(
            {
                "kind": "case",
                "name": name,
                "status": "failed" if failures else "passed",
                "dry_run": dry_run,
                "message": _clip("\n".join(failures)) if failures else None,
                "output": _clip(output if output is not None else error),
                "duration_ms": int((time.monotonic() - case_started) * 1000),
            }
        )

    if include_code_tests and test_code and test_code.strip():
        build = build_python_harness if language == "python" else build_js_harness
        harness = build(code, test_code, force_dry_run=dry_run_only)
        code_started = time.monotonic()
        try:
            output = await execute_tool_auto(
                harness,
                {},
                language=language,
                timeout=min(max(timeout, 1) * CODE_TESTS_TIMEOUT_FACTOR, CODE_TESTS_TIMEOUT_MAX),
                user_id=user_id,
                db=db,
            )
        except Exception as exc:
            results.append(
                {"kind": "code", "name": "(test code)", "status": "error",
                 "message": _clip(_failure_message(exc)),
                 "duration_ms": int((time.monotonic() - code_started) * 1000)}
            )
        else:
            parsed = parse_harness_output(output)
            if parsed is None:
                results.append(
                    {"kind": "code", "name": "(test code)", "status": "error",
                     "message": "The test code printed no results.", "output": _clip(output)}
                )
            elif not parsed:
                results.append(
                    {"kind": "code", "name": "(test code)", "status": "skipped",
                     "message": "The test code declares no tests."}
                )
            else:
                results.extend(parsed)

    return summarize(results, started=started, dry_run_only=dry_run_only)


def summarize(results: list[dict[str, Any]], *, started: float, dry_run_only: bool) -> dict[str, Any]:
    """The report of *results*: counts and the overall status."""
    counts = {"passed": 0, "failed": 0, "error": 0, "skipped": 0}
    for result in results:
        counts[result["status"]] = counts.get(result["status"], 0) + 1
    if counts["error"]:
        status = "error"
    elif counts["failed"]:
        status = "failed"
    elif counts["passed"]:
        status = "passed"
    else:
        status = "no_tests"
    return {
        "status": status,
        "ran_at": datetime.now(UTC).isoformat(),
        "dry_run_only": dry_run_only,
        "duration_ms": int((time.monotonic() - started) * 1000),
        "summary": {**counts, "total": len(results)},
        "results": [{k: v for k, v in r.items() if v is not None} for r in results],
    }


def store_test_report(tool: Any, report: dict[str, Any]) -> None:
    """Keep *report* as the tool's last test run."""
    tool.last_test_status = report["status"]
    tool.last_test_run_at = datetime.now(UTC)
    tool.last_test_results = report
