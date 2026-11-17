"""The widget says what it waits for after a cut stream, and says when it gives up.

A stream cut after the message was stored is answered by the turn that
resumes it; the widget polls the session meanwhile and its thinking row
reads "Resuming the answer…" instead of cycling to "Composing answer…".
A poll that runs past ``MAX_POLL_DURATION`` leaves a one-line
notice under the conversation instead of dropping the thinking row silently.

The checks run the ``poll-copy`` block of ``widget.js`` with Node, exactly as
the browser runs it, and read the call sites that use it.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

WIDGET_JS = Path(__file__).resolve().parents[1] / "app" / "static" / "widget.js"


def _node() -> str:
    node = shutil.which("node")
    if not node:
        if os.environ.get("CI"):
            pytest.fail("node is not installed on the CI runner; the Backend job must set it up")
        pytest.skip("node is not installed")
    return node


def _source() -> str:
    return WIDGET_JS.read_text(encoding="utf-8")


def _block() -> str:
    source = _source()
    start = source.index("// poll-copy:start")
    end = source.index("// poll-copy:end")
    return source[start:end]


def _run(expression: str) -> object:
    program = f"{_block()}\nconsole.log(JSON.stringify({expression}));\n"
    result = subprocess.run([_node(), "-e", program], capture_output=True, text=True, check=False)  # noqa: S603
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _function_body(name: str) -> str:
    source = _source()
    start = source.index(f"function {name}(")
    end = source.index("\n  }\n", start)
    return source[start:end]


def test_cut_wait_label_says_the_answer_is_resumed() -> None:
    assert _run("CUT_WAIT_LABEL") == "Resuming the answer\u2026"


def test_cut_wait_sets_the_label_instead_of_cycling_phases() -> None:
    body = _function_body("waitForCutAnswer")
    assert "updateThinkingLabel(CUT_WAIT_LABEL)" in body
    assert "startThinking()" not in body


def test_poll_timeout_notice_copy() -> None:
    assert _run("POLL_TIMEOUT_NOTICE") == "The answer is taking longer than expected. Reload to check again."


def test_poll_times_out_only_past_the_max_duration() -> None:
    assert _run("[pollTimedOut(0, MAX_POLL_DURATION), pollTimedOut(0, MAX_POLL_DURATION + 1)]") == [False, True]


def test_timed_out_poll_leaves_the_notice() -> None:
    body = _function_body("pollForResponse")
    timeout_branch = body[body.index("if (pollTimedOut(") : body.index("return;")]
    assert "pollNotice = POLL_TIMEOUT_NOTICE" in timeout_branch
    assert timeout_branch.index("pollNotice = POLL_TIMEOUT_NOTICE") < timeout_branch.index("renderMessages()")


def test_notice_is_rendered_as_a_status_line_and_cleared_on_send() -> None:
    render = _function_body("renderMessages")
    assert re.search(r'notice\.setAttribute\("role", "status"\)', render)
    assert "notice.textContent = pollNotice" in render
    send = _source()[_source().index("async function sendMessage(") :]
    assert "pollNotice = null;" in send[: send.index("var sentFiles")]


def _luminance(hex_colour: str) -> float:
    channels = [int(hex_colour[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def test_muted_text_reads_at_4_5_on_the_panel() -> None:
    # The notice and the thinking label are fgMuted on the panel
    # (the single dark palette is kept, its muted text stays readable).
    tokens = dict(re.findall(r'^\s+(\w+): "(#[0-9a-fA-F]{6})",$', _source(), flags=re.MULTILINE))
    fg = _luminance(tokens["fgMuted"])
    for surface in ("bg", "bgPanel"):
        bg = _luminance(tokens[surface])
        ratio = (max(fg, bg) + 0.05) / (min(fg, bg) + 0.05)
        assert ratio >= 4.5, (surface, ratio)
