# SPDX-License-Identifier: Apache-2.0

"""get_github_job_logs failures_only keeps every finding line (file, line, rule) of a linter, compiler or test runner.

On an OpenCTI eslint job the failures-only read kept the rule timing table under
"✖ 1 problem" and dropped the finding just above it; a 1.6 MB download and a grep
were needed. Each fixture below is a tool's own output in a GitHub Actions step;
every finding line must be in the failures_only answer.
"""

from __future__ import annotations

import pytest

from app.integrations import ci_logs


def _step(name: str, body: str, *, stamp: bool = False) -> str:
    lines = body.strip("\n").splitlines()
    if stamp:
        lines = [f"2026-11-15T11:22:01.508{i:04d}Z {ln}" for i, ln in enumerate(lines)]
    return "\n".join(
        [f"##[group]Run {name}", name, "##[endgroup]", *lines, "##[error]Process completed with exit code 1.", ""]
    )


def _answer(log: str) -> str:
    out = ci_logs.job_log_answer(log, title="job", failures_only=True)
    assert out is not None
    return out


# The OpenCTI job of the report: the stylish finding sits above "✖ 1 problem" and an eslint TIMING table.
OPENCTI_ESLINT = _step(
    "./scripts/ci-front.sh",
    """
+ cd /home/workspace/opencti-platform/opencti-front
+ yarn check-ts
! Corepack is about to download https://repo.yarnpkg.com/4.18.1/packages/yarnpkg-cli/bin/yarn.js
+ yarn lint

/home/workspace/opencti-platform/opencti-front/src/private/components/data/playbooks/utils/playbookRewiring.ts
  11:1  error  Expected indentation of 4 spaces  @stylistic/indent-binary-ops

✖ 1 problem (1 error, 0 warnings)
  1 error and 0 warnings potentially fixable with the `--fix` option.

Rule                                 | Time (ms) | Relative
:------------------------------------|----------:|--------:
import/namespace                     | 24957.584 |    32.5%
@typescript-eslint/naming-convention |  7662.878 |    10.0%
import/default                       |  6633.658 |     8.6%
@stylistic/indent                    |  5756.578 |     7.5%
react/no-direct-mutation-state       |  3010.237 |     3.9%
react/display-name                   |  2933.686 |     3.8%
react/no-deprecated                  |  2895.863 |     3.8%
import/no-extraneous-dependencies    |  1837.612 |     2.4%
react/require-render-return          |  1547.005 |     2.0%
no-useless-assignment                |   966.930 |     1.3%
""",
    stamp=True,
)

FIXTURES = {
    "eslint-stylish (opencti)": (
        OPENCTI_ESLINT,
        [
            "/home/workspace/opencti-platform/opencti-front/src/private/components/data/playbooks/utils/playbookRewiring.ts",
            "  11:1  error  Expected indentation of 4 spaces  @stylistic/indent-binary-ops",
        ],
    ),
    "eslint-compact": (
        _step(
            "yarn lint --format compact",
            """
/repo/src/a.ts: line 3, col 5, Error - Unexpected any. Specify a different type. (@typescript-eslint/no-explicit-any)
/repo/src/b.tsx: line 40, col 1, Error - Expected indentation of 2 spaces (@stylistic/indent)

2 problems
""",
        ),
        [
            "/repo/src/a.ts: line 3, col 5, Error - Unexpected any. Specify a different type. (@typescript-eslint/no-explicit-any)",
            "/repo/src/b.tsx: line 40, col 1, Error - Expected indentation of 2 spaces (@stylistic/indent)",
        ],
    ),
    "oxlint": (
        _step(
            "yarn oxlint",
            """
  × eslint(no-unused-vars): Variable 'x' is declared but never used.
   ╭─[src/lib/a.ts:3:7]
 2 │
 3 │ const x = 1;
   ·       ┬
   ╰────

src/lib/b.ts:9:3: Unexpected console statement. [Error/eslint(no-console)]
Found 0 warnings and 2 errors.
""",
        ),
        [
            "  × eslint(no-unused-vars): Variable 'x' is declared but never used.",
            "   ╭─[src/lib/a.ts:3:7]",
            "src/lib/b.ts:9:3: Unexpected console statement. [Error/eslint(no-console)]",
        ],
    ),
    "tsc": (
        _step(
            "yarn typecheck",
            """
src/pages/a.tsx(12,5): error TS2322: Type 'string' is not assignable to type 'number'.
src/pages/b.tsx:30:9 - error TS2550: Property 'at' does not exist on type 'string[]'.
Found 2 errors in 2 files.
""",
        ),
        [
            "src/pages/a.tsx(12,5): error TS2322: Type 'string' is not assignable to type 'number'.",
            "src/pages/b.tsx:30:9 - error TS2550: Property 'at' does not exist on type 'string[]'.",
        ],
    ),
    "ruff": (
        _step(
            "uv run ruff check .",
            """
app/x.py:3:8: F401 [*] `os` imported but unused
E501 Line too long (190 > 180)
 --> app/y.py:12:181
   |
12 | value = "..."
   |
Found 2 errors.
[*] 1 fixable with the `--fix` option.
""",
        ),
        ["app/x.py:3:8: F401 [*] `os` imported but unused", "E501 Line too long (190 > 180)", " --> app/y.py:12:181"],
    ),
    "mypy": (
        _step(
            "uv run mypy app",
            """
app/services/a.py:12: error: Incompatible return value type (got "str", expected "int")  [return-value]
app/services/b.py:40:5: error: Name "y" is not defined  [name-defined]
Found 2 errors in 2 files (checked 900 source files)
""",
        ),
        [
            'app/services/a.py:12: error: Incompatible return value type (got "str", expected "int")  [return-value]',
            'app/services/b.py:40:5: error: Name "y" is not defined  [name-defined]',
        ],
    ),
    "pytest": (
        _step(
            "uv run pytest",
            """
tests/test_a.py::test_one PASSED
tests/test_a.py::test_two FAILED
=================================== FAILURES ===================================
___________________________________ test_two ___________________________________
    def test_two():
>       assert 1 == 2
E       assert 1 == 2
tests/test_a.py:7: AssertionError
=========================== short test summary info ============================
FAILED tests/test_a.py::test_two - assert 1 == 2
========================= 1 failed, 1 passed in 0.12s ==========================
""",
        ),
        ["tests/test_a.py:7: AssertionError", "FAILED tests/test_a.py::test_two - assert 1 == 2"],
    ),
    "vitest": (
        _step(
            "yarn vitest run",
            """
 ✓ src/ok.test.ts (3 tests) 4ms
 ❯ src/a.test.ts (2 tests | 1 failed) 9ms
   × suite > renders the label 5ms
     → expected 'Save' to be 'Saved'
 FAIL  src/a.test.ts > suite > renders the label
AssertionError: expected 'Save' to be 'Saved'
 ❯ src/a.test.ts:10:15
 Test Files  1 failed | 1 passed (2)
""",
        ),
        [
            "   × suite > renders the label 5ms",
            " FAIL  src/a.test.ts > suite > renders the label",
            " ❯ src/a.test.ts:10:15",
        ],
    ),
    "jest": (
        _step(
            "yarn jest",
            """
 PASS  src/ok.test.js
 FAIL  src/a.test.js
  ● suite › renders the label

    expect(received).toBe(expected) // Object.is equality

      at Object.<anonymous> (src/a.test.js:10:15)

Tests:       1 failed, 3 passed, 4 total
""",
        ),
        [" FAIL  src/a.test.js", "  ● suite › renders the label"],
    ),
}


@pytest.mark.parametrize("tool", sorted(FIXTURES))
def test_every_finding_line_is_in_the_failures_only_answer(tool: str) -> None:
    log, findings = FIXTURES[tool]
    out = _answer(log)
    for line in findings:
        assert line in out.splitlines(), f"{tool}: {line!r} missing from:\n{out}"


def test_the_findings_follow_the_pytest_report_and_lead_every_other_block() -> None:
    log = _step(
        "uv run pytest",
        """
app/services/a.py:12: error: Incompatible return value type  [return-value]
=================================== FAILURES ===================================
E       assert 1 == 2
=========================== short test summary info ============================
FAILED tests/test_a.py::test_two - assert 1 == 2
========================= 1 failed in 0.12s ==========================
""",
    )
    out = _answer(log)
    assert out.index("short test summary info") < out.index("--- findings") < out.index("Process completed")


def test_the_findings_come_before_the_opencti_timing_table() -> None:
    out = _answer(OPENCTI_ESLINT)
    assert out.index("@stylistic/indent-binary-ops") < out.index("✖ 1 problem")
    assert out.splitlines()[0].startswith("job: 2 finding line(s)")
    assert "--- findings: 2 line(s) (file, line, rule) ---" in out


def test_findings_are_bounded_and_the_cut_is_counted() -> None:
    body = "\n".join(
        f"src/f{i}.ts({i},1): error TS2322: Type 'string' is not assignable to type 'number'." for i in range(400)
    )
    out = _answer(_step("yarn typecheck", body))
    shown = [ln for ln in out.splitlines() if "error TS2322" in ln]
    assert 0 < len(shown) <= ci_logs.MAX_FINDINGS
    assert f"[{400 - len(shown):,} more finding line(s) not shown" in out
    assert len(out) <= ci_logs.MAX_SUMMARY_CHARS + 300


def test_a_warning_counts_only_when_the_step_has_no_error() -> None:
    log = _step(
        "yarn lint --max-warnings 0",
        """
/repo/src/a.ts
  3:5  warning  Unexpected any  @typescript-eslint/no-explicit-any
""",
    )
    assert "  3:5  warning  Unexpected any  @typescript-eslint/no-explicit-any" in _answer(log).splitlines()
    mixed = OPENCTI_ESLINT.replace("  11:1  error", "  2:1  warning  Unused  no-unused-vars\n  11:1  error")
    assert "warning  Unused" not in _answer(mixed)


def test_the_timing_table_goes_first_when_space_runs_out() -> None:
    # Thirty distinct failures (a name each, not a bare number, which the repeat folding masks).
    filler = "\n".join(f"error: long failure detail case_{i} " + "x" * 500 for i in range(30))
    out = _answer(OPENCTI_ESLINT.replace("+ yarn lint\n", f"+ yarn lint\n{filler}\n", 1))
    assert "@stylistic/indent-binary-ops" in out
    assert "timing table line(s) dropped to fit" in out
    assert "import/namespace                     | 24957.584" not in out


def test_a_bundler_warning_frame_is_no_finding() -> None:
    log = _step(
        "yarn build",
        """
[MODULE_LEVEL_DIRECTIVE] The semantics of the module level directive "use no memo" in node_modules/graphiql/dist/index.js
   ╭─[ node_modules/graphiql/dist/index.js:1:1 ]
   │
 1 │ "use no memo";
error: build failed
""",
    )
    out = _answer(log)
    assert "--- findings" not in out
    assert "error: build failed" in out


def test_a_finding_is_kept_in_the_default_read_too() -> None:
    out = ci_logs.job_log_answer(OPENCTI_ESLINT, title="job", failures_only=False)
    assert out is not None
    assert "  11:1  error  Expected indentation of 4 spaces  @stylistic/indent-binary-ops" in out.splitlines()
