"""The golden-task runner of the live routing eval (``.xtm-one/lab/routing_eval.py``, xtm-one #9646 slice 3).

It reads the golden tasks from the deterministic eval without importing the backend, counts the
handovers of a conversation from its spans the way the run rollup does, and judges a task on the agent
it ends on, its error and its baseline. No network: the HTTP client is replaced by a double.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / ".xtm-one" / "lab" / "routing_eval.py"

pytestmark = pytest.mark.skipif(not SCRIPT.is_file(), reason="no .xtm-one next to the backend")


@pytest.fixture(autouse=True)
def _last_report_in_tmp(tmp_path, monkeypatch):
    """Every run keeps its table; in the tests it goes to a temporary file, never the checkout's ``.xtm/``."""
    monkeypatch.setenv("ROUTING_EVAL_LAST_REPORT", str(tmp_path / "routing-eval-last.json"))


def _module():
    spec = importlib.util.spec_from_file_location("lab_routing_eval", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_golden_tasks_match_the_deterministic_eval():
    from tests.test_ootb_routing_eval import ENTRY_AGENTS, GOLDEN_TASKS

    tasks, entries = _module().golden_tasks()
    assert tasks == [tuple(task) for task in GOLDEN_TASKS]
    assert entries == ENTRY_AGENTS


def test_every_golden_task_has_a_prompt():
    module = _module()
    tasks, _ = module.golden_tasks()
    assert {label for _, label, _, _ in tasks} <= module.PROMPTS.keys()


def test_handover_tools_match_the_platform():
    from app.services.builtin_tools.handover import HANDOVER_TOOL_NAMES

    assert frozenset(HANDOVER_TOOL_NAMES.values()) == _module().HANDOVER_TOOLS


def test_summarize_counts_hops_tokens_and_tools():
    spans = [
        {"type": "GENERATION", "input_tokens": 100, "output_tokens": 20},
        {"type": "TOOL", "name": "tool:transfer_to_agent"},
        {"type": "GENERATION", "input_tokens": 50, "output_tokens": 10},
        {"type": "TOOL", "metadata": {"tool_name": "create_opencti_dashboard"}, "level": "ERROR"},
        {"type": "ITERATION"},
        "not a span",
    ]
    out = _module().summarize(spans)
    assert out["handovers"] == 1 and out["handover_tools"] == ["transfer_to_agent"]
    assert (out["input_tokens"], out["output_tokens"], out["generations"]) == (150, 30, 2)
    assert (out["tool_calls"], out["tool_errors"]) == (2, 1)
    assert out["tools"] == {"transfer_to_agent": 1, "create_opencti_dashboard": 1}


@pytest.mark.parametrize(
    ("final", "error", "hops", "expected"),
    [
        ("opencti-dashboard-creator", None, 1, True),
        ("opencti-dashboard-creator", None, 2, False),
        ("opencti-assistant", None, 0, False),
        ("opencti-dashboard-creator", "quota", 1, False),
    ],
)
def test_verdict(final, error, hops, expected):
    assert _module().verdict("opencti-dashboard-creator", 1, final, error, hops) is expected


class _Double:
    """The Client's calls, answered from a script: one conversation, one transfer, two traces."""

    def __init__(self):
        self.sent: list[tuple[str, str, dict | None]] = []

    def call(self, method, path, body=None, query="", **kwargs):
        self.sent.append((method, path, body))
        if path == "/chat/conversations":
            return {"id": "c1"}
        if path.endswith("/messages"):
            return {"error": None, "transfer_agent_id": "a2"}
        raise AssertionError(path)

    def conversation_spans(self, conversation_id):
        traces = [{"id": "t1", "agent_id": "a1", "started_at": "1"}, {"id": "t2", "agent_id": "a2", "started_at": "2"}]
        spans = [
            {"type": "GENERATION", "input_tokens": 10, "output_tokens": 5},
            {"type": "TOOL", "name": "transfer_to_agent"},
            {"type": "GENERATION", "input_tokens": 30, "output_tokens": 7},
        ]
        return traces, spans


def test_run_task_reads_the_conversation_back():
    module = _module()
    client = _Double()
    agents = {"opencti-assistant": "a1", "opencti-dashboard-creator": "a2"}
    task = ("opencti", "create a dashboard with widgets", "opencti-dashboard-creator", 1)
    row = module.run_task(client, agents, "opencti-assistant", task)
    assert row["final_agent"] == "opencti-dashboard-creator"
    assert row["handovers"] == 1 and row["input_tokens"] == 40 and row["success"] is True
    assert row["verified"] is None
    assert client.sent[0] == (
        "POST",
        "/chat/conversations",
        {"agent_id": "a1", "title": "routing eval: create a dashboard with widgets"},
    )


class _HtmlAnswer(_Double):
    """The same route, its chat answer written in raw HTML."""

    def call(self, method, path, body=None, query="", **kwargs):
        if path.endswith("/messages"):
            self.sent.append((method, path, body))
            content = '<meta name="topics" content="a"><h2>Key findings</h2><p>APT28</p>'
            return {"error": None, "transfer_agent_id": "a2", "assistant_message": {"content": content}}
        return super().call(method, path, body, query)


def test_a_chat_answer_in_raw_html_fails_whatever_the_route():
    agents = {"opencti-assistant": "a1", "opencti-dashboard-creator": "a2"}
    task = ("opencti", "create a dashboard with widgets", "opencti-dashboard-creator", 1)
    row = _module().run_task(_HtmlAnswer(), agents, "opencti-assistant", task)
    assert row["final_agent"] == "opencti-dashboard-creator" and row["handovers"] == 1
    assert row["success"] is False
    assert row["raw_html"] == ['<meta name="topics" content="a">', "<h2>", "</h2>", "<p>", "</p>"]


@pytest.mark.parametrize(
    "text",
    [
        "## Key findings\n\n- **APT28** targets [Acme](https://x/dashboard/id/1)",
        "Use `<p>` for paragraphs.",
        "```html\n<h2>example</h2>\n```",
        "a < b and c > d",
        None,
    ],
)
def test_markdown_and_quoted_markup_are_not_raw_html(text):
    assert _module().raw_html_tags(text) == []


class _DirectHtml(_Double):
    """An intent agent asked directly: no transfer, its answer in HTML (or in Markdown when *html* is false)."""

    def __init__(self, html: bool = True):
        super().__init__()
        self.html = html

    def call(self, method, path, body=None, query="", **kwargs):
        if path.endswith("/messages"):
            self.sent.append((method, path, body))
            content = "<h2>Key findings</h2><p>APT28</p>" if self.html else "## Key findings\n\n- APT28"
            return {"error": None, "transfer_agent_id": None, "assistant_message": {"content": content}}
        return super().call(method, path, body, query)

    def conversation_spans(self, conversation_id):
        return [{"id": "t1", "agent_id": "a2", "started_at": "1"}], [
            {"type": "GENERATION", "input_tokens": 12, "output_tokens": 4}
        ]


_INSIGHTS_AGENTS = {"opencti-assistant": "a1", "opencti-entity-insights": "a2"}
_SUMMARIZE = ("opencti", "summarize a report", "opencti-entity-insights", 1)


def test_an_entry_override_asks_the_named_agent_and_expects_no_handover():
    module = _module()
    task = module.entry_task(_SUMMARIZE, "opencti-entity-insights")
    assert task == ("opencti", "summarize a report", "opencti-entity-insights", 0)
    client = _DirectHtml()
    row = module.run_task(client, _INSIGHTS_AGENTS, "opencti-entity-insights", task, expect_format="html")
    assert client.sent[0][2]["agent_id"] == "a2"
    assert row["entry_agent"] == row["final_agent"] == "opencti-entity-insights"
    assert row["handovers"] == 0 and row["expected_format"] == "html"
    assert row["raw_html"] == ["<h2>", "</h2>", "<p>", "</p>"] and "format_error" not in row
    assert row["success"] is True


def test_html_expected_fails_a_markdown_answer():
    module = _module()
    task = module.entry_task(_SUMMARIZE, "opencti-entity-insights")
    row = module.run_task(
        _DirectHtml(html=False), _INSIGHTS_AGENTS, "opencti-entity-insights", task, expect_format="html"
    )
    assert row["success"] is False and row["format_error"] == "no HTML tag in the answer"


@pytest.mark.parametrize("expect_format", [None, "markdown"])
def test_markdown_expected_fails_raw_html(expect_format):
    module = _module()
    task = module.entry_task(_SUMMARIZE, "opencti-entity-insights")
    row = module.run_task(_DirectHtml(), _INSIGHTS_AGENTS, "opencti-entity-insights", task, expect_format=expect_format)
    assert row["success"] is False and row["format_error"].startswith("raw HTML in the answer: <h2>")


def test_an_entry_not_seeded_is_a_skipped_row():
    module = _module()
    task = module.entry_task(_SUMMARIZE, "no-such-agent")
    row = module.run_task(_DirectHtml(), _INSIGHTS_AGENTS, "no-such-agent", task, expect_format="html")
    assert row["skipped"] == "agent 'no-such-agent' is not seeded"


def test_main_passes_the_entry_and_the_expected_format(monkeypatch, capsys):
    import json

    module = _module()
    seen = []

    class _Client:
        def __init__(self, base):
            self.base = base

        def sign_in(self, email, password):
            return None

        def agents_by_slug(self):
            return {}

    def run_task(client, agents, entry, task, **kwargs):
        seen.append((entry, task, kwargs.get("expect_format")))
        return {"task": task[1], "error": None, "success": True, "input_tokens": 0, "output_tokens": 0, "seconds": 1}

    for name, value in (
        ("Client", _Client),
        ("run_task", run_task),
        ("preflight_model", lambda: None),
        ("wait_for_integrations", lambda *a, **k: {"ready": True}),
    ):
        monkeypatch.setattr(module, name, value)
    argv = ["--base", "http://lab.test", "--email", "e", "--password", "p", "--task", "summarize a report"]
    code = module.main([*argv, "--entry", "opencti-entity-insights", "--expect-format", "html"])
    assert code == 0 and json.loads(capsys.readouterr().out)["summary"]["passed"] == 1
    assert seen == [("opencti-entity-insights", module.entry_task(_SUMMARIZE, "opencti-entity-insights"), "html")]


def test_the_entry_override_is_documented():
    import tomllib

    check = tomllib.loads((ROOT / ".xtm-one" / "checks.toml").read_text())["check"]["lab-routing-eval"]
    docs = (ROOT / "dev-docs" / "ootb-routing-eval.md").read_text()
    for flag in ("--entry", "--expect-format"):
        assert flag in check["description"] and flag in docs


class _LateTraces(_Double):
    """A lab whose trace lands on the third read after the answer."""

    def __init__(self, empty_reads: int):
        super().__init__()
        self.reads = 0
        self.empty_reads = empty_reads
        self.now = 0.0
        self.slept: list[float] = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds

    def call(self, method, path, body=None, query="", **kwargs):
        if path.endswith("/messages"):
            self.sent.append((method, path, body))
            return {"error": None}
        return super().call(method, path, body, query)

    def conversation_spans(self, conversation_id):
        self.reads += 1
        if self.reads <= self.empty_reads:
            return [], []
        return [{"id": "t1", "agent_id": "a1", "started_at": "1"}], [
            {"type": "GENERATION", "input_tokens": 12, "output_tokens": 3}
        ]


_SEARCH = ("opencti", "search an entity", "opencti-assistant", 0)


def test_run_task_rereads_the_traces_until_the_turn_is_recorded():
    client = _LateTraces(empty_reads=2)
    row = _module().run_task(client, {"opencti-assistant": "a1"}, "opencti-assistant", _SEARCH)
    assert client.reads == 3 and len(client.slept) == 2
    assert row["generations"] == 1 and row["input_tokens"] == 12
    assert row["success"] is True and "unmeasured" not in row


def test_a_turn_with_no_trace_is_unmeasured_never_a_pass():
    module = _module()
    client = _LateTraces(empty_reads=1000)
    row = module.run_task(client, {"opencti-assistant": "a1"}, "opencti-assistant", _SEARCH)
    assert row["success"] is None
    assert row["unmeasured"].startswith("no generation recorded")
    assert client.now <= module.TRACE_SETTLE_SECONDS


def test_run_task_skips_an_unseeded_entry():
    row = _module().run_task(
        _Double(), {}, "opencti-assistant", ("opencti", "search an entity", "opencti-assistant", 0)
    )
    assert row["skipped"] == "agent 'opencti-assistant' is not seeded"


class _SlowTurn(_Double):
    """A turn that outlives the task's timeout (a delegation still running), raised as ``urlopen`` does."""

    def __init__(self, raised):
        super().__init__()
        self.raised = raised
        self.timeouts: list[float | None] = []

    def call(self, method, path, body=None, query="", **kwargs):
        if path.endswith("/messages"):
            self.timeouts.append(kwargs.get("timeout"))
            raise self.raised
        return super().call(method, path, body, query)


@pytest.mark.parametrize("wrapped", [False, True])
def test_a_turn_past_the_task_timeout_is_timed_out_and_the_row_kept(wrapped):
    import urllib.error

    raised = urllib.error.URLError(TimeoutError("timed out")) if wrapped else TimeoutError("timed out")
    client = _SlowTurn(raised)
    agents = {"opencti-assistant": "a1", "opencti-entity-insights": "a2"}
    task = ("opencti", "entity insights", "opencti-entity-insights", 1)
    row = _module().run_task(client, agents, "opencti-assistant", task, timeout=12)
    assert client.timeouts == [12]
    assert row["timed_out"] is True and row["success"] is False
    assert row["error"] == "no answer within 12 s: the turn was still running"
    # What the traces held when the runner gave up is still read: hops and tokens so far.
    assert row["handovers"] == 1 and row["input_tokens"] == 40


def test_a_refused_connection_is_not_a_timeout():
    import urllib.error

    client = _SlowTurn(urllib.error.URLError(ConnectionRefusedError("refused")))
    with pytest.raises(urllib.error.URLError):
        _module().run_task(client, {"opencti-assistant": "a1"}, "opencti-assistant", _SEARCH)


def test_main_passes_the_task_timeout_and_counts_timed_out_rows(monkeypatch, capsys):
    module = _module()
    _main_with(monkeypatch, module, preflight=None)
    seen = []

    def run_task(client, agents, entry, task, **kwargs):
        seen.append(kwargs.get("timeout"))
        return {
            "task": task[1],
            "error": "late",
            "success": False,
            "timed_out": True,
            "input_tokens": 0,
            "output_tokens": 0,
            "seconds": 9,
        }

    monkeypatch.setattr(module, "run_task", run_task)
    code = module.main(
        ["--base", "http://lab.test", "--email", "e", "--password", "p", "--surface", "xtm_one", "--task-timeout", "9"]
    )
    report = json.loads(capsys.readouterr().out)
    assert code == 1 and seen and set(seen) == {9.0}
    assert report["summary"]["timed_out"] == len(seen)
    assert module.TASK_TIMEOUT_SECONDS < 270


_LIVE_ENV = {
    "LAB_MODEL_API_KEY": "fcp-lab",
    "LAB_MODEL_BASE_URL": "http://proxy.test/v1/",
    "LAB_MODEL_NAMES": "gpt-lab, other",
}


def _now():
    from datetime import UTC, datetime

    return datetime(2026, 11, 10, 14, 17, tzinfo=UTC)


def test_quota_message_reads_the_proxy_refusal_and_its_reset():
    module = _module()
    detail = "Error code: 429 - {'detail': 'llm_proxy quota exceeded: 201/200 (daily)'}"
    assert module.quota_message(detail, now=_now()) == (
        "llm_proxy quota exceeded 201/200 (daily), resets at 2026-11-11T00:00Z"
    )
    assert module.quota_message(detail, retry_after="600", now=_now()).endswith("resets at 2026-11-10T14:27Z")
    assert module.quota_message("llm_proxy quota exceeded for model gpt-lab: 5/5 (monthly)", now=_now()) == (
        "llm_proxy quota exceeded 5/5 (monthly)"
    )
    assert module.quota_message("POST /chat: 500 boom") is None


def _refusing_opener(code, body, headers=None):
    import email.message
    import io
    import urllib.error

    calls = []

    def opener(request, timeout):
        calls.append((request.full_url, request.get_header("Authorization"), timeout))
        message = email.message.Message()
        for name, value in (headers or {}).items():
            message[name] = value
        raise urllib.error.HTTPError(request.full_url, code, "refused", message, io.BytesIO(body.encode()))

    return opener, calls


def test_preflight_reports_an_exhausted_proxy_key():
    module = _module()
    opener, calls = _refusing_opener(
        429, '{"detail": "llm_proxy quota exceeded: 201/200 (daily)"}', {"Retry-After": "34980"}
    )
    message = module.preflight_model(_LIVE_ENV, opener=opener, now=_now())
    assert message == "llm_proxy quota exceeded 201/200 (daily), resets at 2026-11-11T00:00Z"
    assert calls == [("http://proxy.test/v1/chat/completions", "Bearer fcp-lab", 20)]


def test_preflight_goes_on_for_a_stub_lab_and_any_other_refusal():
    module = _module()
    opener, calls = _refusing_opener(429, '{"detail": "Rate limit exceeded"}')
    assert module.preflight_model(_LIVE_ENV, opener=opener) is None
    opener, _ = _refusing_opener(401, '{"detail": "llm_proxy quota exceeded: 201/200 (daily)"}')
    assert module.preflight_model(_LIVE_ENV, opener=opener) is None
    assert module.preflight_model({}, opener=opener) is None
    assert len(calls) == 1


def test_model_quota_reads_the_proxy_usage_without_a_completion(monkeypatch, capsys):
    import io

    module = _module()
    calls = []
    usage = {"used": 200, "limit": 200, "period": "daily", "period_key": "2026-11-11"}

    def opener(request, timeout):
        calls.append((request.get_method(), request.full_url, request.get_header("Authorization"), request.data))
        return io.BytesIO(json.dumps(usage).encode())

    assert module.model_quota(_LIVE_ENV, opener=opener) == {"live": True, **usage}
    assert calls == [("GET", "http://proxy.test/v1/quota", "Bearer fcp-lab", None)]
    assert module.model_quota({}, opener=opener) == {"live": False}
    assert len(calls) == 1

    opener, _ = _refusing_opener(401, '{"detail": "Invalid API key"}')
    assert module.model_quota(_LIVE_ENV, opener=opener)["error"].startswith("GET /quota: 401 ")

    monkeypatch.setattr(module, "model_quota", lambda: {"live": True, **usage})
    assert module.main(["--quota"]) == 0
    assert json.loads(capsys.readouterr().out) == {"live": True, **usage}


def test_the_quota_check_is_declared():
    import tomllib

    checks = tomllib.loads((ROOT / ".xtm-one" / "checks.toml").read_text())["check"]
    assert checks["lab-model-quota"]["argv"] == ["python", ".xtm-one/lab/routing_eval.py", "--quota"]
    assert "lab-model-quota" in (ROOT / "dev-docs" / "ootb-routing-eval.md").read_text()


def _main_with(monkeypatch, module, *, preflight, error=None):
    ran = []

    class _Client:
        def __init__(self, base):
            self.base = base

        def sign_in(self, email, password):
            return None

        def agents_by_slug(self):
            return {}

    def run_task(client, agents, entry, task, **kwargs):
        ran.append(task[1])
        return {"task": task[1], "error": error, "success": False, "input_tokens": 0, "output_tokens": 0, "seconds": 1}

    for name, value in (
        ("Client", _Client),
        ("run_task", run_task),
        ("preflight_model", lambda: preflight),
        ("wait_for_integrations", lambda *a, **k: {"ready": True}),
    ):
        monkeypatch.setattr(module, name, value, raising=False)
    return ran


def _run_main(module, capsys):
    import json

    code = module.main(["--base", "http://lab.test", "--email", "e", "--password", "p", "--surface", "xtm_one"])
    out = capsys.readouterr()
    return code, json.loads(out.out), out.err


def test_main_runs_no_task_on_an_exhausted_proxy(monkeypatch, capsys):
    module = _module()
    message = "llm_proxy quota exceeded 201/200 (daily), resets at 2026-11-11T00:00Z"
    ran = _main_with(monkeypatch, module, preflight=message)
    code, report, err = _run_main(module, capsys)
    assert code == module.QUOTA_EXIT == 3
    assert ran == [] and report["tasks"] == []
    assert report["quota_exceeded"] == message and message in err


def test_main_stops_at_the_first_task_refused_for_the_quota(monkeypatch, capsys):
    module = _module()
    ran = _main_with(
        monkeypatch,
        module,
        preflight=None,
        error="Error code: 429 - {'detail': 'llm_proxy quota exceeded: 201/200 (daily)'}",
    )
    code, report, _ = _run_main(module, capsys)
    assert code == 3 and len(ran) == 1
    assert report["quota_exceeded"].startswith("llm_proxy quota exceeded 201/200 (daily), resets at ")


def test_needed_providers_reads_the_surface_and_the_product_named():
    module = _module()
    tasks = [
        ("xtm_one", "create an OpenCTI dashboard from XTM One", "opencti-dashboard-creator", 1),
        ("openaev", "create a scenario", "openaev-scenario-creator", 1),
        ("xtm_one", "plain small talk", "xtm-one-assistant", 0),
    ]
    assert module.needed_providers(tasks) == ["openaev", "opencti"]
    assert module.needed_providers(tasks[2:]) == []


class _Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class _Integrations:
    """``company_integrations`` answered empty for the first ``late`` polls, then with OpenCTI."""

    def __init__(self, late):
        self.late = late
        self.polls = 0

    def company_integrations(self):
        self.polls += 1
        return {} if self.polls <= self.late else {"opencti": "i1"}


def test_wait_for_integrations_waits_for_the_registration():
    clock = _Clock()
    client = _Integrations(late=3)
    state = _module().wait_for_integrations(client, ["opencti"], 60, sleep=clock.sleep, clock=clock)
    assert state == {"ready": True, "waited_seconds": 15.0, "integrations": {"opencti": "i1"}}
    assert client.polls == 4


def test_wait_for_integrations_gives_up_at_the_deadline():
    clock = _Clock()
    state = _module().wait_for_integrations(_Integrations(late=99), ["opencti"], 12, sleep=clock.sleep, clock=clock)
    assert state == {"ready": False, "waited_seconds": 12.0, "integrations": {"opencti": None}}


def test_a_task_set_needing_no_platform_does_not_poll():
    client = _Integrations(late=99)
    assert _module().wait_for_integrations(client, [], 60)["ready"] is True
    assert client.polls == 0


def test_main_exits_2_before_any_task_when_opencti_is_not_registered(tmp_path, monkeypatch, capsys):
    module = _module()
    sent: list[str] = []

    class _Client:
        def __init__(self, base):
            self.base = base

        def sign_in(self, email, password):
            sent.append("sign_in")

        def agents_by_slug(self):
            return {"xtm-one-assistant": "a1", "opencti-dashboard-creator": "a2"}

        def company_integrations(self):
            sent.append("integrations")
            return {}

        def call(self, *args, **kwargs):
            raise AssertionError("no task may run before the lab's OpenCTI registers")

    monkeypatch.setattr(module, "Client", _Client)
    out = tmp_path / "fresh" / ".xtm" / "eval.json"
    argv = ["--base", "http://127.0.0.1:8100", "--email", "e", "--password", "p"]
    code = module.main([*argv, "--task", "create an OpenCTI dashboard from XTM One", "--wait", "0", "--out", str(out)])
    assert code == module.NOT_READY == 2
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["tasks"] == [] and "success" not in json.dumps(report)
    assert report["readiness"]["integrations"] == {"opencti": None}
    assert "OpenCTI is not registered yet" in report["not_ready"]
    assert "OpenCTI is not registered yet" in capsys.readouterr().err
    assert sent == ["sign_in", "integrations"]


def test_write_report_creates_the_out_directory(tmp_path, capsys, monkeypatch):
    module = _module()
    monkeypatch.setenv(module.LAST_REPORT_ENV, str(tmp_path / "last" / "last.json"))
    out = tmp_path / ".xtm" / "nested" / "eval.json"
    module.write_report({"ok": 1}, str(out))
    assert json.loads(out.read_text(encoding="utf-8")) == {"ok": 1}
    assert json.loads(capsys.readouterr().out) == {"ok": 1}


def test_last_report_lives_in_the_checkout_the_runner_runs_from():
    module = _module()
    assert module.LAST_REPORT == ROOT / ".xtm" / "routing-eval" / "last.json"
    assert module.LAST_REPORT_ENV == "ROUTING_EVAL_LAST_REPORT"


def test_a_cut_run_leaves_its_finished_tasks_readable_with_no_call(tmp_path, monkeypatch, capsys):
    """The calling turn handed back mid-run: ``--last`` answers the rows already paid for, no client built."""
    module = _module()
    last = tmp_path / "lab" / ".xtm" / "routing-eval" / "last.json"
    monkeypatch.setenv(module.LAST_REPORT_ENV, str(last))
    _main_with(monkeypatch, module, preflight=None)
    finished = []

    def run_task(client, agents, entry, task, **kwargs):
        if finished:
            raise KeyboardInterrupt("the turn was handed back")
        finished.append(task[1])
        return {"task": task[1], "error": None, "success": True, "input_tokens": 3, "output_tokens": 2, "seconds": 1}

    monkeypatch.setattr(module, "run_task", run_task)
    with pytest.raises(KeyboardInterrupt):
        module.main(["--base", "http://lab.test", "--email", "e", "--password", "p", "--surface", "xtm_one"])
    capsys.readouterr()

    def no_client(base):
        raise AssertionError("--last never signs in")

    monkeypatch.setattr(module, "Client", no_client)
    code = module.main(["--last"])
    out = capsys.readouterr()
    report = json.loads(out.out)
    assert code == 1 and report["partial"] is True
    assert [row["task"] for row in report["tasks"]] == finished
    assert "stopped before its end" in out.err


def test_a_finished_run_is_read_back_whole(tmp_path, monkeypatch, capsys):
    module = _module()
    monkeypatch.setenv(module.LAST_REPORT_ENV, str(tmp_path / "last.json"))
    _main_with(monkeypatch, module, preflight=None)
    module.main(["--base", "http://lab.test", "--email", "e", "--password", "p", "--surface", "xtm_one"])
    first = json.loads(capsys.readouterr().out)
    assert module.main(["--last"]) == 0
    assert json.loads(capsys.readouterr().out) == first and "partial" not in first


def test_last_with_no_table_kept_exits_4(tmp_path, monkeypatch, capsys):
    module = _module()
    monkeypatch.setenv(module.LAST_REPORT_ENV, str(tmp_path / "none.json"))
    assert module.main(["--last"]) == module.NO_LAST == 4
    assert "no routing eval table kept" in capsys.readouterr().err


def test_the_last_table_is_a_declared_check_with_no_sign_in():
    from app.services.sandbox_workspace.checks import load_checks

    checks = load_checks((ROOT / ".xtm-one" / "checks.toml").read_text(encoding="utf-8"))
    last = checks["lab-routing-eval-last"]
    assert last.argv == ("python", ".xtm-one/lab/routing_eval.py", "--last")
    assert "--password" not in last.argv and "lab-routing-eval" in checks


def test_a_run_still_needs_the_sign_in(capsys):
    with pytest.raises(SystemExit):
        _module().main(["--surface", "xtm_one"])
    assert "required unless --last" in capsys.readouterr().err


def test_select_tasks_matches_the_label_or_the_prompt():
    module = _module()
    tasks, _ = module.golden_tasks()
    by_label = module.select_tasks(tasks, ["summarize a report"])
    assert [task[1] for task in by_label] == ["summarize a report"]
    # A brief quotes the prompt the lab is sent: it selects the same task, whatever the case.
    assert module.select_tasks(tasks, [module.PROMPTS["summarize a report"].upper()]) == by_label
    assert module.select_tasks(tasks) == tasks
    assert module.select_tasks(tasks, ["no such task anywhere"]) == []
    assert all(task[0] == "xtm_one" for task in module.select_tasks(tasks, surfaces=["xtm_one"]))


def test_a_task_filter_that_selects_nothing_exits_5_before_the_sign_in(monkeypatch, capsys):
    module = _module()

    class _NoCall:
        def __init__(self, base):
            raise AssertionError("a run that selects no task must not reach the lab")

    monkeypatch.setattr(module, "Client", _NoCall)
    argv = ["--base", "http://lab.test", "--email", "e", "--password", "p", "--task", "no such task anywhere"]
    assert module.main(argv) == module.NO_TASK == 5
    err = capsys.readouterr().err
    assert "no golden task matches" in err and "summarize a report" in err


def test_the_task_filter_and_its_exit_are_documented():
    import tomllib

    description = tomllib.loads((ROOT / ".xtm-one" / "checks.toml").read_text())["check"]["lab-routing-eval"][
        "description"
    ]
    docs = (ROOT / "dev-docs" / "ootb-routing-eval.md").read_text()
    assert "golden task label" in description and "exit 5" in description
    assert "exit 5" in docs and "its prompt" in docs


class _Answer:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


def _unavailable_then(answer, refusals):
    """An opener that answers 503 ``refusals`` times, then ``answer``."""
    import email.message
    import io
    import urllib.error

    sent = []

    def opener(request, timeout):
        sent.append((request.get_method(), request.full_url))
        if len(sent) <= refusals:
            body = io.BytesIO(b'{"detail": "The database is temporarily unavailable"}')
            raise urllib.error.HTTPError(request.full_url, 503, "unavailable", email.message.Message(), body)
        return _Answer(answer)

    return opener, sent


def _client(opener, clock, budget=60.0):
    return _module().Client("http://lab.test", opener=opener, sleep=clock.sleep, clock=clock, unavailable_budget=budget)


def test_sign_in_retries_a_503_until_the_lab_answers():
    clock = _Clock()
    opener, sent = _unavailable_then({"access_token": "t"}, refusals=3)
    client = _client(opener, clock)
    client.sign_in("e", "p")
    assert client.token == "t"
    assert sent == [("POST", "http://lab.test/api/v1/auth/login")] * 4
    assert clock.now == 15.0


def test_sign_in_gives_up_on_a_503_past_the_budget():
    clock = _Clock()
    opener, sent = _unavailable_then({"access_token": "t"}, refusals=99)
    with pytest.raises(RuntimeError, match="POST /auth/login: 503 "):
        _client(opener, clock, budget=12).sign_in("e", "p")
    assert len(sent) == 3 and clock.now == 10.0


def test_a_read_retries_a_503_and_a_task_post_does_not():
    clock = _Clock()
    opener, sent = _unavailable_then({"items": []}, refusals=1)
    assert _client(opener, clock).call("GET", "/agents") == {"items": []}
    assert len(sent) == 2
    opener, sent = _unavailable_then({}, refusals=1)
    with pytest.raises(RuntimeError, match="POST /conversations: 503 "):
        _client(opener, clock).call("POST", "/conversations", {})
    assert len(sent) == 1


def test_main_exits_not_ready_when_the_sign_in_stays_unavailable(tmp_path, monkeypatch, capsys):
    module = _module()

    class _Client:
        def __init__(self, base):
            self.base = base

        def sign_in(self, email, password):
            raise RuntimeError('POST /auth/login: 503 {"detail": "The database is temporarily unavailable"}')

        def agents_by_slug(self):
            raise AssertionError("nothing runs after a refused sign-in")

    monkeypatch.setattr(module, "Client", _Client)
    out = tmp_path / "eval.json"
    code = module.main(["--base", "http://lab.test", "--email", "e", "--password", "p", "--out", str(out)])
    assert code == module.NOT_READY == 2
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["tasks"] == [] and "503" in report["not_ready"]
    assert "no task ran" in capsys.readouterr().err


def test_main_still_raises_a_refused_sign_in_that_is_not_a_503(monkeypatch):
    module = _module()

    class _Client:
        def __init__(self, base):
            self.base = base

        def sign_in(self, email, password):
            raise RuntimeError("POST /auth/login: 401 bad credentials")

    monkeypatch.setattr(module, "Client", _Client)
    with pytest.raises(RuntimeError, match="401"):
        module.main(["--base", "http://lab.test", "--email", "e", "--password", "p"])


class _ConfirmDouble(_Double):
    """Two turns in one conversation: the plan, then the build once confirmed."""

    def call(self, method, path, body=None, query="", **kwargs):
        self.sent.append((method, path, body))
        if path == "/chat/conversations":
            return {"id": "c1"}
        if path.endswith("/messages"):
            turn = sum(1 for _, p, _ in self.sent if p.endswith("/messages"))
            return {"error": None, "transfer_agent_id": "a2" if turn == 1 else None}
        raise AssertionError(path)


def test_confirm_sends_a_second_turn_in_the_same_conversation_and_keeps_the_transfer():
    module = _module()
    client = _ConfirmDouble()
    agents = {"opencti-assistant": "a1", "opencti-dashboard-creator": "a2"}
    task = ("opencti", "create a dashboard with widgets", "opencti-dashboard-creator", 1)
    row = module.run_task(client, agents, "opencti-assistant", task, confirm="Build it as it is.")
    turns = [body for _, path, body in client.sent if path.endswith("/messages")]
    assert turns == [{"content": module.PROMPTS[task[1]]}, {"content": "Build it as it is."}]
    assert row["confirm"] == "Build it as it is." and row["final_agent"] == "opencti-dashboard-creator"
    assert row["verified"] is None


def test_verify_fills_verified_and_a_dashboard_without_widget_fails_the_row():
    module = _module()
    task = ("opencti", "create a dashboard with widgets", "opencti-dashboard-creator", 1)
    agents = {"opencti-assistant": "a1", "opencti-dashboard-creator": "a2"}
    asked: list[str] = []

    def built(name, since):
        asked.append(name)
        return {"verified": True, "dashboard_id": "d1", "widgets": 2, "waited_seconds": 0.0}

    row = module.run_task(_Double(), agents, "opencti-assistant", task, verify=built)
    assert asked == ["Routing eval"]
    assert (row["verified"], row["dashboard_id"], row["widgets"], row["success"]) == (True, "d1", 2, True)
    empty = module.run_task(
        _Double(),
        agents,
        "opencti-assistant",
        task,
        verify=lambda name, since: {"verified": False, "dashboard_id": None, "widgets": 0, "waited_seconds": 90.0},
    )
    assert empty["verified"] is False and empty["success"] is False
    # A prompt naming no dashboard is not read back.
    search = ("opencti", "search an entity", "opencti-assistant", 0)
    row = module.run_task(_Double(), {"opencti-assistant": "a1"}, "opencti-assistant", search, verify=built)
    assert row["verified"] is None and asked == ["Routing eval"]


def test_dashboard_name_and_manifest_widgets():
    import base64

    module = _module()
    assert module.dashboard_name(module.PROMPTS["create a dashboard with widgets"]) == "Routing eval"
    assert module.dashboard_name(module.PROMPTS["add a widget to a dashboard"]) == "Routing eval"
    assert module.dashboard_name("Find the intrusion set APT28.") is None
    manifest = base64.b64encode(json.dumps({"widgets": {"w1": {}, "w2": {}}, "config": {}}).encode()).decode()
    assert module.manifest_widgets(manifest) == 2
    assert module.manifest_widgets(json.dumps({"widgets": {}})) == 0
    assert module.manifest_widgets(None) == module.manifest_widgets("not a manifest") == 0


def test_verify_dashboard_rereads_until_a_widget_appears_and_ignores_older_dashboards():
    import base64

    module = _module()

    def node(id_, created, widgets):
        manifest = base64.b64encode(json.dumps({"widgets": {f"w{i}": {} for i in range(widgets)}}).encode()).decode()
        return {"node": {"id": id_, "name": "Routing eval", "created_at": created, "manifest": manifest}}

    answers = iter(
        [
            {"workspaces": {"edges": [node("old", "2026-11-10T00:00:00.000Z", 3)]}},
            {
                "workspaces": {
                    "edges": [node("old", "2026-11-10T00:00:00.000Z", 3), node("new", "2026-11-11T08:00:01.000Z", 0)]
                }
            },
            {"workspaces": {"edges": [node("new", "2026-11-11T08:00:01.000Z", 2)]}},
        ]
    )
    sent: list[dict] = []

    def graphql(query, variables):
        sent.append(variables)
        return next(answers)

    ticks = iter(range(100))
    out = module.verify_dashboard(
        "Routing eval",
        "2026-11-11T08:00:00Z",
        graphql,
        wait=60,
        step=5,
        sleep=lambda _s: None,
        clock=lambda: next(ticks),
    )
    assert out["verified"] is True and out["dashboard_id"] == "new" and out["widgets"] == 2
    assert sent == [{"search": "Routing eval"}] * 3


def test_verify_dashboard_answers_false_past_its_wait_and_reports_an_unreadable_opencti():
    module = _module()
    ticks = iter([0, 0, 100])
    out = module.verify_dashboard(
        "Routing eval",
        "2026-11-11T08:00:00Z",
        lambda q, v: {"workspaces": {"edges": []}},
        wait=10,
        step=5,
        sleep=lambda _s: None,
        clock=lambda: next(ticks),
    )
    assert out["verified"] is False and out["dashboard_id"] is None and out["widgets"] == 0

    def refused(query, variables):
        raise RuntimeError("OpenCTI GraphQL: 401")

    out = module.verify_dashboard("Routing eval", "x", refused, wait=10, sleep=lambda _s: None, clock=lambda: 0)
    assert out["verified"] is False and "401" in out["verify_error"]


def test_opencti_graphql_sends_the_token_and_raises_on_errors():
    module = _module()
    seen: list = []

    class _Response:
        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps(self.body).encode()

    def opener(request, timeout):
        seen.append(request)
        return _Response({"data": {"workspaces": {"edges": []}}})

    data = module.opencti_graphql("http://127.0.0.1:4000/", "tok", "query { x }", {"a": 1}, opener=opener)
    assert data == {"workspaces": {"edges": []}}
    assert seen[0].full_url == "http://127.0.0.1:4000/graphql"
    assert seen[0].get_header("Authorization") == "Bearer tok"
    with pytest.raises(RuntimeError, match="OpenCTI GraphQL"):
        module.opencti_graphql("http://x", "t", "q", {}, opener=lambda r, timeout: _Response({"errors": ["no"]}))


def test_the_verify_defaults_are_the_lab_opencti_of_the_declaration():
    from tests.test_lab_seed_declarations import _services

    module = _module()
    env = _services()["lab-opencti"]["env"]
    assert env["APP__ADMIN__TOKEN"] == module.OPENCTI_TOKEN
    assert f"http://127.0.0.1:{env['APP__PORT']}" == module.OPENCTI_URL
