"""Declared tests of a custom tool (``services/custom_tool_tests.py``).

The validation of data cases and test code, the expectations, the parsing of
the harness output, the ``dry_run`` injection, and - in the subprocess
sandbox, for real - both harnesses: Python (``test_*`` functions,
``unittest`` classes, async tests) and JavaScript (``test``/``it``,
``node:assert``, ``ToolError``), with and without a forced dry run.
"""

from __future__ import annotations

import shutil

import pytest

from app.services import custom_tool_executor as executor
from app.services.custom_tool_tests import (
    MAX_TEST_CASES,
    RESULTS_MARKER,
    check_expectations,
    json_subset_matches,
    parse_harness_output,
    run_tool_tests,
    summarize,
    validate_test_cases,
    validate_test_code,
)

# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_no_cases_is_none():
    assert validate_test_cases(None) is None
    assert validate_test_cases([]) is None


def test_case_is_normalized():
    cases = validate_test_cases(
        [{"name": " dry ", "inputs": {"a": 1}, "dry_run": True, "expect": {"contains": "ok"}}]
    )
    assert cases == [
        {
            "name": "dry",
            "inputs": {"a": 1},
            "dry_run": True,
            "expect": {"contains": ["ok"]},
            "expect_error": False,
        }
    ]


def test_error_contains_implies_expect_error():
    [case] = validate_test_cases([{"name": "boom", "error_contains": "denied"}])
    assert case["expect_error"] is True


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ({"name": "x"}, "must be a list"),
        ([{"inputs": {}}], "Test case 1"),
        ([{"name": "a"}, {"name": "a"}], "Two test cases are named 'a'"),
        ([{"name": "a", "expect": {"regex": "("}}], "invalid regular expression"),
        ([{"name": "a", "unknown": 1}], "Test case a"),
        ([{"name": f"c{i}"} for i in range(MAX_TEST_CASES + 1)], "at most"),
    ],
)
def test_invalid_cases_are_refused(raw, message):
    with pytest.raises(ValueError, match=message):
        validate_test_cases(raw)


def test_test_code_validation():
    assert validate_test_code(None) is None
    assert validate_test_code("   \n") is None
    assert validate_test_code("def test_x(): pass") == "def test_x(): pass"
    with pytest.raises(ValueError):
        validate_test_code(42)


# ---------------------------------------------------------------------------
# Expectations
# ---------------------------------------------------------------------------


def test_json_subset():
    assert json_subset_matches({"a": 1}, {"a": 1, "b": 2})
    assert not json_subset_matches({"a": 2}, {"a": 1})
    assert json_subset_matches({"items": [{"id": 2}]}, {"items": [{"id": 1}, {"id": 2, "x": 0}]})
    assert not json_subset_matches([2, 1], [1, 2])  # in order
    assert not json_subset_matches({"a": 1}, [1])


def test_expectations():
    assert check_expectations(None, "anything") == []
    assert check_expectations({"equals": "ok"}, " ok\n") == []
    assert check_expectations({"equals": "ok"}, "ko")
    assert check_expectations({"contains": ["a", "b"]}, "ab") == []
    assert len(check_expectations({"contains": ["a", "z"], "not_contains": ["b"]}, "ab")) == 2
    assert check_expectations({"regex": r"^id=\d+$"}, "x\nid=12") == []
    assert check_expectations({"json_subset": {"ok": True}}, '{"ok": true, "n": 1}') == []
    assert check_expectations({"json_subset": {"ok": True}}, "not json") == ["The output is not JSON."]


def test_harness_output_parsing():
    out = f'noise\n{RESULTS_MARKER}[{{"name": "t", "status": "passed", "duration_ms": 3}}, {{"status": "weird"}}]'
    assert parse_harness_output(out) == [
        {"kind": "code", "name": "t", "status": "passed", "message": None, "duration_ms": 3},
        {"kind": "code", "name": "(unnamed)", "status": "error", "message": None, "duration_ms": None},
    ]
    assert parse_harness_output("no marker") is None
    assert parse_harness_output(f"{RESULTS_MARKER}{{}}") is None


def test_summary_status():
    def status(*statuses):
        return summarize([{"status": s} for s in statuses], started=0, dry_run_only=False)["status"]

    assert status() == "no_tests"
    assert status("skipped") == "no_tests"
    assert status("passed", "skipped") == "passed"
    assert status("passed", "failed") == "failed"
    assert status("failed", "error") == "error"


# ---------------------------------------------------------------------------
# Every run receives dry_run
# ---------------------------------------------------------------------------


async def test_execute_tool_auto_injects_dry_run(monkeypatch):
    seen: list[dict] = []

    async def fake_python(code, inputs, timeout):
        seen.append(inputs)
        return "ok"

    async def subprocess_mode():
        return "subprocess"

    monkeypatch.setattr(executor, "_select_sandbox", subprocess_mode)
    monkeypatch.setattr(executor, "execute_python_tool", fake_python)
    await executor.execute_tool_auto("print(1)", {"a": 1}, language="python")
    await executor.execute_tool_auto("print(1)", {"dry_run": True}, language="python")
    assert seen == [{"dry_run": False, "a": 1}, {"dry_run": True}]


# ---------------------------------------------------------------------------
# The harnesses, run for real in the subprocess sandbox
# ---------------------------------------------------------------------------


@pytest.fixture
def subprocess_sandbox(monkeypatch):
    async def subprocess_mode():
        return "subprocess"

    monkeypatch.setattr(executor, "_select_sandbox", subprocess_mode)
    monkeypatch.setattr(executor, "_custom_tools_disabled", lambda: False)


PY_TOOL = 'if dry_run:\n    print("DRY " + domain)\nelse:\n    print("WROTE " + domain)\n'


async def test_python_cases_and_code(subprocess_sandbox):
    cases = validate_test_cases(
        [
            {"name": "dry", "inputs": {"domain": "x.org"}, "dry_run": True, "expect": {"equals": "DRY x.org"}},
            {"name": "missing input", "error_contains": "NameError"},
            {"name": "write", "inputs": {"domain": "x.org"}, "expect": {"equals": "DRY x.org"}},
        ]
    )
    code = """
def test_dry():
    assert run_tool({"domain": "a"}, dry_run=True) == "DRY a"

def test_fails():
    assert run_tool({"domain": "a"}) == "nope", "mismatch"

async def test_async():
    assert run_tool({"domain": "a"}).startswith("WROTE")

class Raises(unittest.TestCase):
    def test_tool_error(self):
        with self.assertRaises(ToolError):
            run_tool({})
"""
    report = await run_tool_tests(
        code=PY_TOOL, language="python", timeout=60, test_cases=cases, test_code=code
    )
    by_name = {r["name"]: r for r in report["results"]}
    assert by_name["dry"]["status"] == "passed"
    assert by_name["missing input"]["status"] == "passed"
    assert by_name["write"]["status"] == "failed"
    assert by_name["write"]["output"] == "WROTE x.org"
    assert by_name["test_dry"]["status"] == "passed"
    assert by_name["test_fails"]["status"] == "failed"
    assert "mismatch" in by_name["test_fails"]["message"]
    assert by_name["test_async"]["status"] == "passed"
    assert by_name["Raises.test_tool_error"]["status"] == "passed"
    assert report["status"] == "failed"
    assert report["summary"] == {"passed": 5, "failed": 2, "error": 0, "skipped": 0, "total": 7}


async def test_python_dry_run_only_forces_dry_run(subprocess_sandbox):
    cases = validate_test_cases(
        [
            {"name": "dry", "inputs": {"domain": "x"}, "dry_run": True},
            {"name": "write", "inputs": {"domain": "x"}},
        ]
    )
    code = 'def test_forced():\n    assert run_tool({"domain": "a"}) == "DRY a"\n'
    report = await run_tool_tests(
        code=PY_TOOL, language="python", timeout=60, test_cases=cases, test_code=code, dry_run_only=True
    )
    assert [(r["name"], r["status"]) for r in report["results"]] == [
        ("dry", "passed"),
        ("write", "skipped"),
        ("test_forced", "passed"),
    ]
    assert report["status"] == "passed"


async def test_python_test_code_that_does_not_load(subprocess_sandbox):
    report = await run_tool_tests(
        code=PY_TOOL, language="python", timeout=60, test_cases=None, test_code="def broken(:\n"
    )
    assert report["status"] == "error"
    assert report["results"][0]["name"] == "(loading the tests)"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
async def test_javascript_cases_and_code(subprocess_sandbox):
    tool = 'if ($dry_run) { console.log("DRY " + $domain); } else { return { wrote: $domain }; }'
    cases = validate_test_cases(
        [{"name": "json", "inputs": {"domain": "z"}, "expect": {"json_subset": {"wrote": "z"}}}]
    )
    code = """
test("dry", async () => { assert.equal(await runTool({ domain: "a" }, { dryRun: true }), "DRY a"); });
it("json", async () => { assert.deepEqual(JSON.parse(await runTool({ domain: "b" })), { wrote: "b" }); });
test("fails", async () => { assert.equal(1, 2); });
test("tool error", async () => { await assert.rejects(runTool({}), ToolError); });
"""
    report = await run_tool_tests(
        code=tool, language="javascript", timeout=60, test_cases=cases, test_code=code
    )
    assert [(r["name"], r["status"]) for r in report["results"]] == [
        ("json", "passed"),
        ("dry", "passed"),
        ("json", "passed"),
        ("fails", "failed"),
        ("tool error", "passed"),
    ]
    forced = await run_tool_tests(
        code=tool, language="javascript", timeout=60, test_cases=None,
        test_code='test("forced", async () => { assert.equal(await runTool({ domain: "c" }), "DRY c"); });',
        dry_run_only=True,
    )
    assert forced["status"] == "passed"
