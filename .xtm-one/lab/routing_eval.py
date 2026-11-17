"""The live half of the out-of-the-box routing eval: replay the golden tasks on a running XTM One.

``backend/tests/test_ootb_routing_eval.py`` pins, without a model, the agent each golden task should
reach and the handovers it may take. This runner asks the same tasks of a live instance (a lab started
with ``sandbox_start_service lab-live``, or staging) through the chat API and reads each conversation's
run traces back: per task, the agent that answered, the handovers taken, tokens, wall time and tool
calls, as one JSON document (``dev-docs/ootb-routing-eval.md``, "The live half (lab)").

Standard library only, so it runs in the lab pod (the declared check ``lab-routing-eval``:
``sandbox_run_command(check="lab-routing-eval", service_id=<the lab>)``, a check container having no
route to a lab pod) or on any machine that reaches the instance::

    python .xtm-one/lab/routing_eval.py --base http://127.0.0.1:8100 \\
        --email lab@filigran.io --password lab-only-password --surface xtm_one

Nothing runs on its own and nothing is sent anywhere but the instance named by ``--base``. A task is a
success when the conversation ends on the agent the golden task names, the turn reports no error and
the handovers stay within the baseline; a turn whose traces record no generation within
``TRACE_SETTLE_SECONDS`` is ``unmeasured`` (``success`` null, exit 1), never a pass; ``verified``
(the deliverable read back, e.g. the dashboard and its widgets in OpenCTI) is ``null`` unless
``--verify`` reads it back from the lab's OpenCTI (true: the dashboard the prompt names exists with a
widget; false fails the row). ``--confirm TEXT`` sends the approval a creator agent asks for as a
second turn of the same conversation.
"""

from __future__ import annotations

import argparse
import ast
import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
EVAL = ROOT / "backend" / "tests" / "test_ootb_routing_eval.py"

#: The tools a model hands a turn over through (``services/builtin_tools/handover.py``
#: ``HANDOVER_TOOL_NAMES``): each call is one hop.
HANDOVER_TOOLS = frozenset({"transfer_to_agent", "consult_agent", "spawn_background_task"})

#: The exit code of a run stopped because the lab's LLM proxy key is over its quota (1: a task failed,
#: 2: the lab was not ready).
QUOTA_EXIT = 3

#: The exit code of ``--last`` when no run kept a table in this checkout.
NO_LAST = 4

#: The exit code of a run whose ``--task`` / ``--surface`` filters select no golden task: nothing is asked.
NO_TASK = 5

#: What the proxy's ``QuotaExceededError`` says (``services/quota.py``): ``llm_proxy quota exceeded: 201/200 (daily)``.
QUOTA_PATTERN = re.compile(r"llm_proxy quota exceeded(?: for model [^:]+)?: (\d+)/(\d+) \((\w+)\)")


def quota_message(text: str, retry_after: str | None = None, now: datetime | None = None) -> str | None:
    """``llm_proxy quota exceeded N/M (period), resets at <UTC>`` when *text* carries the proxy's quota refusal."""
    match = QUOTA_PATTERN.search(text or "")
    if match is None:
        return None
    current, limit, period = match.groups()
    message = f"llm_proxy quota exceeded {current}/{limit} ({period})"
    now = now or datetime.now(UTC)
    if retry_after and retry_after.strip().isdigit():
        resets = now + timedelta(seconds=int(retry_after.strip()))
    elif period == "daily":
        resets = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    else:
        return message
    return f"{message}, resets at {resets.strftime('%Y-%m-%dT%H:%MZ')}"


def preflight_model(
    env: Mapping[str, str] | None = None,
    opener: Callable[..., Any] = urllib.request.urlopen,
    now: datetime | None = None,
) -> str | None:
    """The quota message when the lab's live model key is refused for its quota, else ``None``.

    A lab started with ``LAB_MODEL = "live"`` holds ``LAB_MODEL_API_KEY``, ``LAB_MODEL_BASE_URL`` and
    ``LAB_MODEL_NAMES`` (``services/sandbox_workspace/model_keys.py``). One completion of one token is
    asked of the proxy with that key: a 429 naming the quota stops the eval before its first task
    instead of letting every task time out. A stub-model lab (no key) and any other answer go on.
    """
    env = os.environ if env is None else env
    key, base = env.get("LAB_MODEL_API_KEY", ""), env.get("LAB_MODEL_BASE_URL", "").rstrip("/")
    model = next((name.strip() for name in env.get("LAB_MODEL_NAMES", "").split(",") if name.strip()), "")
    if not (key and base.startswith(("http://", "https://")) and model):
        return None
    request = urllib.request.Request(  # noqa: S310 - scheme checked above
        f"{base}/chat/completions",
        data=json.dumps({"model": model, "max_tokens": 1, "messages": [{"role": "user", "content": "ping"}]}).encode(),
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    try:
        with opener(request, timeout=20):
            return None
    except urllib.error.HTTPError as exc:
        if exc.code != 429:
            return None
        body = exc.read()[:500].decode("utf-8", "replace")
        return quota_message(body, exc.headers.get("Retry-After") if exc.headers else None, now)
    except (OSError, ValueError):
        return None


def model_quota(
    env: Mapping[str, str] | None = None,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> dict[str, Any]:
    """The lab model key's LLM proxy quota usage, asked of ``GET /v1/quota``: no completion, no call spent.

    ``{"live": true, "used", "limit", "period", "period_key", "resets_at", "per_model"}`` on a lab started
    with ``LAB_MODEL = "live"``; ``{"live": false}`` on a stub-model lab; ``{"live": true, "error": ...}`` when
    the proxy did not answer. Read before and after a step to prove it spent no call (the declared check
    ``lab-model-quota``, ``dev-docs/ootb-routing-eval.md``).
    """
    env = os.environ if env is None else env
    key, base = env.get("LAB_MODEL_API_KEY", ""), env.get("LAB_MODEL_BASE_URL", "").rstrip("/")
    if not (key and base.startswith(("http://", "https://"))):
        return {"live": False}
    request = urllib.request.Request(  # noqa: S310 - scheme checked above
        f"{base}/quota", method="GET", headers={"Authorization": f"Bearer {key}"}
    )
    try:
        with opener(request, timeout=20) as response:
            return {"live": True, **json.loads(response.read().decode("utf-8"))}
    except urllib.error.HTTPError as exc:
        return {"live": True, "error": f"GET /quota: {exc.code} {exc.read()[:300].decode('utf-8', 'replace')}"}
    except (OSError, ValueError) as exc:
        return {"live": True, "error": f"GET /quota: {exc}"}


#: The suite platforms a golden task acts on: its surface, or the product its label names (``create an
#: OpenCTI dashboard from XTM One`` needs OpenCTI). Each must be a company-managed integration of the
#: instance before the first task runs, else the specialist answers "not connected" and the eval reports a
#: routing failure that is a readiness one.
PRODUCTS: dict[str, str] = {"opencti": "OpenCTI", "openaev": "OpenAEV", "opencrq": "OpenCRQ"}

#: Seconds the runner waits for those integrations by default, leaving room for a task within the
#: ``lab-routing-eval`` check's timeout (270 s). A lab's own OpenCTI registers 30 s after boot, then every
#: 5 minutes: past the wait the runner exits 2 and the check is simply run again later.
WAIT_SECONDS = 120.0

#: Exit code when an integration a task needs is not registered in time: not a routing result.
NOT_READY = 2

#: Golden task label -> the message typed into the surface's chat. A label without a prompt is
#: reported as skipped, never guessed.
PROMPTS: dict[str, str] = {
    "create a dashboard with widgets": "Create a dashboard named 'Routing eval' with a widget of the reports of the last 30 days.",
    "add a widget to a dashboard": "Add a widget of the indicators created this week to my dashboard 'Routing eval'.",
    "search an entity": "Find the intrusion set APT28.",
    "create an indicator or a relationship": "Create an IPv4 indicator for 198.51.100.7 with the pattern [ipv4-addr:value = '198.51.100.7'].",
    "summarize a report": "Summarize the latest report.",
    "entity insights": "Give me insights on the malware Emotet.",
    "threat hunt": "Start a threat hunt on the techniques used by APT28.",
    "create a scenario": "Create a scenario that simulates a phishing campaign against the finance team.",
    "publish a scenario": "Publish my latest scenario.",
    "explain an expectation result": "Explain why the last expectation of my latest simulation failed.",
    "control gap analysis": "Which controls have the largest gaps?",
    "risk scenario": "Draft a ransomware risk scenario.",
    "assessment summary": "Summarize my latest assessment.",
    "posture question": "What is our cyber posture today?",
    "which CVEs do we fix Monday": "Which CVEs do we fix on Monday?",
    "plain small talk": "Hello, how are you?",
    "create an OpenCTI dashboard from XTM One": "Create an OpenCTI dashboard named 'Routing eval' with a widget of the reports of the last 30 days.",
    "create an OpenAEV scenario from XTM One": "Create an OpenAEV scenario that simulates a phishing campaign against the finance team.",
    "a threat intelligence question asked in OpenAEV": "Which threat actors target the energy sector?",
    "an OpenCTI dashboard asked in OpenCRQ": "Create an OpenCTI dashboard named 'Routing eval' with a widget of the reports of the last 30 days.",
}


def golden_tasks(path: Path = EVAL) -> tuple[list[tuple[str, str, str, int]], dict[str, str]]:
    """``(GOLDEN_TASKS, ENTRY_AGENTS)`` read from the deterministic eval without importing the backend."""
    found: dict[str, Any] = {}
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        target = (
            node.target
            if isinstance(node, ast.AnnAssign)
            else (node.targets[0] if isinstance(node, ast.Assign) else None)
        )
        if isinstance(target, ast.Name) and target.id in {"GOLDEN_TASKS", "ENTRY_AGENTS"} and node.value is not None:
            found[target.id] = ast.literal_eval(node.value)
    missing = {"GOLDEN_TASKS", "ENTRY_AGENTS"} - found.keys()
    if missing:
        raise ValueError(f"{path}: no {', '.join(sorted(missing))}")
    return [tuple(task) for task in found["GOLDEN_TASKS"]], dict(found["ENTRY_AGENTS"])


def select_tasks(
    tasks: Iterable[tuple[str, str, str, int]],
    texts: Iterable[str] | None = None,
    surfaces: Iterable[str] | None = None,
) -> list[tuple[str, str, str, int]]:
    """The golden tasks a run asks: on one of ``surfaces`` and whose label or prompt contains one of ``texts``.

    Case-insensitive; no filter keeps every task. A brief quotes either the label ("summarize a report") or
    the prompt the lab is sent ("Summarize the latest report."), so both are matched.
    """
    wanted = [text.lower() for text in texts or ()]
    allowed = set(surfaces or ())
    return [
        task
        for task in tasks
        if (not allowed or task[0] in allowed)
        and (not wanted or any(text in task[1].lower() or text in PROMPTS.get(task[1], "").lower() for text in wanted))
    ]


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _tool_name(span: Mapping[str, Any]) -> str:
    metadata = span.get("metadata")
    if isinstance(metadata, Mapping) and isinstance(metadata.get("tool_name"), str):
        return metadata["tool_name"]
    return str(span.get("name") or "tool").removeprefix("tool:")


def summarize(spans: Iterable[Any]) -> dict[str, Any]:
    """Handovers, tokens, generations and tool calls of a conversation's spans (the run rollup's reading)."""
    handovers: list[str] = []
    tools: dict[str, int] = {}
    input_tokens = output_tokens = generations = tool_calls = tool_errors = 0
    for span in spans:
        if not isinstance(span, Mapping):
            continue
        kind = span.get("type")
        if kind == "GENERATION":
            generations += 1
            input_tokens += _int(span.get("input_tokens"))
            output_tokens += _int(span.get("output_tokens"))
        elif kind == "TOOL":
            name = _tool_name(span)
            tool_calls += 1
            tool_errors += int(str(span.get("level") or "").upper() == "ERROR")
            tools[name] = tools.get(name, 0) + 1
            if name in HANDOVER_TOOLS:
                handovers.append(name)
    return {
        "handovers": len(handovers),
        "handover_tools": handovers,
        "generations": generations,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "tool_calls": tool_calls,
        "tool_errors": tool_errors,
        "tools": tools,
    }


def needed_providers(tasks: Iterable[tuple[str, str, str, int]]) -> list[str]:
    """The suite platforms the chosen tasks act on (their surface, or a product their label names), sorted."""
    needed = {
        provider
        for surface, label, _, _ in tasks
        for provider, product in PRODUCTS.items()
        if surface == provider or product.lower() in label.lower()
    }
    return sorted(needed)


def verdict(expected: str, baseline: int, final_agent: str | None, error: str | None, hops: int) -> bool:
    """A task passes when it ends on the agent it names, with no error, within its baseline hops."""
    return error is None and final_agent == expected and hops <= baseline


#: Markup a chat answer must not carry: the chat renders Markdown, so these reach the user as raw tags.
RAW_HTML_TAG = re.compile(r"<\s*/?\s*(h[1-6]|p|ul|ol|li|table|tr|td|th|div|span|meta|br|strong|em)\b[^>]*>", re.I)


def raw_html_tags(text: str | None) -> list[str]:
    """The raw HTML tags a chat answer shows its user (code spans and fences left out), first five."""
    if not text:
        return []
    prose = re.sub(r"```.*?```|`[^`\n]*`", "", text, flags=re.S)
    return [m.group(0) for m in RAW_HTML_TAG.finditer(prose)][:5]


#: The answer formats ``--expect-format`` asserts: ``markdown`` (the chat's own, the default rule) fails a row on
#: any raw tag; ``html`` (an agent seeded with ``output_format`` ``html``, asked directly with ``--entry``) fails
#: a row whose answer carries none.
ANSWER_FORMATS = ("markdown", "html")


def format_failure(expect_format: str | None, tags: list[str]) -> str | None:
    """Why the answer breaks the expected format (``None``: it holds), from the raw tags it carries."""
    if expect_format == "html":
        return None if tags else "no HTML tag in the answer"
    return f"raw HTML in the answer: {' '.join(tags)}" if tags else None


def entry_task(task: tuple[str, str, str, int], entry: str) -> tuple[str, str, str, int]:
    """The golden task asked of *entry* directly: it must end there with no handover."""
    surface, label, _, _ = task
    return surface, label, entry, 0


#: How long a task's traces are re-read after its turn answered. The run's trace and spans are written as the
#: run ends, so a read right after the answer can find none: such a row would fall back to the entry agent
#: with 0 tokens and pass or fail unmeasured (9 of the 20 golden tasks on 11 Nov 2026).
TRACE_SETTLE_SECONDS = 45.0
TRACE_SETTLE_STEP = 3.0


def settled_spans(
    client: Any,
    conversation_id: str,
    *,
    budget: float = TRACE_SETTLE_SECONDS,
    step: float = TRACE_SETTLE_STEP,
) -> tuple[list[dict], list[dict]]:
    """The conversation's traces and spans, re-read until a generation is recorded or ``budget`` runs out."""
    sleep = getattr(client, "sleep", time.sleep)
    clock = getattr(client, "clock", time.monotonic)
    started = clock()
    while True:
        traces, spans = client.conversation_spans(conversation_id)
        if any(isinstance(s, Mapping) and s.get("type") == "GENERATION" for s in spans):
            return traces, spans
        if clock() - started + step > budget:
            return traces, spans
        sleep(step)


#: How long a call that is safe to repeat (a read, the sign-in) retries a 503 before it raises. A lab whose
#: database pool is briefly taken (background runs waiting on a refused model) answers 503 "The database is
#: temporarily unavailable"; that is not a failed task, so exit 1 is not spent on it.
UNAVAILABLE_BUDGET = 60.0
UNAVAILABLE_STEP = 5.0


class Client:
    """The few JSON calls the runner makes, with the signed-in user's Bearer token."""

    def __init__(
        self,
        base: str,
        timeout: float = 600.0,
        *,
        opener: Callable[..., Any] = urllib.request.urlopen,
        sleep: Callable[[float], Any] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        unavailable_budget: float = UNAVAILABLE_BUDGET,
    ) -> None:
        if not base.startswith(("http://", "https://")):
            raise ValueError(f"--base must be an http(s) URL, not {base!r}")
        self.base = base.rstrip("/")
        self.timeout = timeout
        self.token: str | None = None
        self.opener, self.sleep, self.clock = opener, sleep, clock
        self.unavailable_budget = unavailable_budget

    def call(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        query: str = "",
        *,
        retry_unavailable: bool | None = None,
        timeout: float | None = None,
    ) -> Any:
        """One JSON call. A 503 is retried every ``UNAVAILABLE_STEP`` seconds within ``unavailable_budget`` when
        the call is safe to repeat: a GET, or ``retry_unavailable=True`` (the sign-in). A task's POST is sent once.
        ``timeout`` replaces the client's socket timeout for this call (a task's turn, see ``run_task``).
        """
        retry = method == "GET" if retry_unavailable is None else retry_unavailable
        started = self.clock()
        while True:
            request = urllib.request.Request(  # noqa: S310 - base checked http(s) in __init__
                f"{self.base}/api/v1{path}{query}",
                data=json.dumps(body).encode() if body is not None else None,
                method=method,
                headers={
                    "Content-Type": "application/json",
                    **({"Authorization": f"Bearer {self.token}"} if self.token else {}),
                },
            )
            try:
                with self.opener(request, timeout=timeout or self.timeout) as response:
                    return json.loads(response.read() or b"null")
            except urllib.error.HTTPError as exc:
                detail = exc.read()[:300].decode("utf-8", "replace")
                if retry and exc.code == 503 and self.clock() - started + UNAVAILABLE_STEP <= self.unavailable_budget:
                    sys.stderr.write(f"{method} {path}: 503, retrying in {UNAVAILABLE_STEP:.0f} s\n")
                    self.sleep(UNAVAILABLE_STEP)
                    continue
                raise RuntimeError(f"{method} {path}: {exc.code} {detail}") from exc

    def sign_in(self, email: str, password: str) -> None:
        answer = self.call("POST", "/auth/login", {"email": email, "password": password}, retry_unavailable=True)
        self.token = answer.get("access_token") if isinstance(answer, dict) else None
        if not self.token:
            raise RuntimeError("POST /auth/login answered no access_token")

    def agents_by_slug(self) -> dict[str, str]:
        rows = self.call("GET", "/agents")
        rows = rows.get("items", []) if isinstance(rows, dict) else rows
        return {str(row["slug"]): str(row["id"]) for row in rows if isinstance(row, dict) and row.get("slug")}

    def company_integrations(self) -> dict[str, str]:
        """Provider -> id of each company-managed integration the instance lists."""
        rows = self.call("GET", "/integrations")
        rows = rows.get("items", []) if isinstance(rows, dict) else rows
        return {
            str(row["provider"]): str(row.get("id"))
            for row in rows or []
            if isinstance(row, dict) and row.get("provider") and row.get("is_company_managed")
        }

    def conversation_spans(self, conversation_id: str) -> tuple[list[dict], list[dict]]:
        listing = self.call("GET", "/traces", query=f"?conversation_id={conversation_id}&limit=200")
        traces = listing.get("items", []) if isinstance(listing, dict) else []
        spans: list[dict] = []
        for trace in traces:
            answer = self.call("GET", f"/traces/{trace['id']}/spans")
            spans += answer.get("items", []) if isinstance(answer, dict) else []
        return traces, spans


def wait_for_integrations(
    client: Any,
    providers: list[str],
    wait: float,
    *,
    sleep: Any = time.sleep,
    clock: Any = time.monotonic,
    step: float = 5.0,
) -> dict[str, Any]:
    """Poll ``GET /integrations`` until each provider has a company-managed row or ``wait`` seconds pass.

    Answers ``{"ready": bool, "waited_seconds": float, "integrations": {provider: id or None}}``, recorded
    in the report so a reader sees the state the tasks ran against.
    """
    started = clock()
    while True:
        found = client.company_integrations() if providers else {}
        state = {provider: found.get(provider) for provider in providers}
        waited = round(clock() - started, 1)
        if all(state.values()) or waited >= wait:
            return {"ready": all(state.values()), "waited_seconds": waited, "integrations": state}
        sleep(min(step, max(wait - waited, 0.1)))


#: Every run's table, kept in the checkout the runner runs from (the lab's copy) whatever ``--out`` says, and
#: rewritten after each task: a calling turn handed back mid-call reads it with ``--last`` (the declared check
#: ``lab-routing-eval-last``), no task asked again and no LLM call spent.
LAST_REPORT = ROOT / ".xtm" / "routing-eval" / "last.json"

#: The variable that moves ``LAST_REPORT`` (the tests point it at a temporary directory).
LAST_REPORT_ENV = "ROUTING_EVAL_LAST_REPORT"


def last_report_path() -> Path:
    return Path(os.environ.get(LAST_REPORT_ENV) or LAST_REPORT)


def save_last(report: dict[str, Any], path: Path | None = None) -> None:
    """Write ``report`` to the last-run file (its directory created), through a temporary file and a rename."""
    target = path or last_report_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".tmp")
    partial.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    partial.replace(target)


def show_last(path: Path | None = None) -> int:
    """Print the last run's table: 0 when it is complete, 1 when the run stopped mid-way, 4 when none was kept."""
    target = path or last_report_path()
    if not target.is_file():
        sys.stderr.write(f"no routing eval table kept at {target}: no run finished a task in this checkout\n")
        return NO_LAST
    text = target.read_text(encoding="utf-8")
    sys.stdout.write(text if text.endswith("\n") else text + "\n")
    try:
        partial = bool(json.loads(text).get("partial"))
    except (ValueError, AttributeError):
        partial = True
    if partial:
        sys.stderr.write("the run that wrote this table stopped before its end: it lists the tasks it finished\n")
    return 1 if partial else 0


def write_report(report: dict[str, Any], out: str | None) -> None:
    """The JSON to stdout, to the last-run file, and to ``out`` when given (its directory created)."""
    text = json.dumps(report, indent=2)
    save_last(report)
    if out:
        path = Path(out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + "\n", encoding="utf-8")
    sys.stdout.write(text + "\n")


#: Seconds one task's turn may take before the runner stops waiting for its answer, records the row as
#: ``timed_out`` (a failure: no answer within the budget) and goes on to the next task. Kept under the
#: ``lab-routing-eval`` check's 270 s so a turn that waits on a delegation never costs the whole table.
TASK_TIMEOUT_SECONDS = 150.0


def _timed_out(exc: BaseException) -> bool:
    """A socket timeout, raised bare or wrapped in a ``URLError`` by ``urlopen``."""
    reason = getattr(exc, "reason", None)
    return isinstance(exc, TimeoutError) or isinstance(reason, TimeoutError)


#: The lab's own OpenCTI ([service.lab-opencti]: APP__PORT 4000, APP__ADMIN__TOKEN), read by ``--verify``.
OPENCTI_URL = "http://127.0.0.1:4000"
OPENCTI_TOKEN = "00000000-0000-4000-8000-000000000001"

#: Seconds ``--verify`` re-reads the dashboard for widgets: they are added by the Dashboard Creator's
#: background tasks, which finish after the turn answered.
VERIFY_WAIT_SECONDS = 90.0
VERIFY_STEP = 5.0

#: The dashboard a prompt names (``Create a dashboard named 'Routing eval' ...``).
DASHBOARD_NAME = re.compile(r"dashboard named '([^']+)'|dashboard '([^']+)'", re.I)

WORKSPACES_QUERY = (
    "query RoutingEvalDashboards($search: String) { workspaces(search: $search, first: 50, "
    'filters: {mode: and, filters: [{key: "type", values: ["dashboard"]}], filterGroups: []}) '
    "{ edges { node { id name created_at manifest } } } }"
)


def dashboard_name(prompt: str | None) -> str | None:
    """The dashboard name a golden task's prompt quotes, ``None`` when it names none."""
    match = DASHBOARD_NAME.search(prompt or "")
    return (match.group(1) or match.group(2)) if match else None


def manifest_widgets(manifest: Any) -> int:
    """The widget count of an OpenCTI workspace manifest: base64 of a JSON object (or the JSON itself)."""
    if not isinstance(manifest, str) or not manifest.strip():
        return 0
    text = manifest
    try:
        text = base64.b64decode(manifest, validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        pass
    try:
        widgets = json.loads(text).get("widgets")
    except (ValueError, AttributeError):
        return 0
    return len(widgets) if isinstance(widgets, (dict, list)) else 0


def opencti_graphql(url: str, token: str, query: str, variables: dict[str, Any], *, opener: Any = None) -> Any:
    """One GraphQL call to an OpenCTI with its Bearer token; raises on an HTTP or a GraphQL error."""
    request = urllib.request.Request(  # noqa: S310 - the lab's own OpenCTI, given by --opencti-url
        f"{url.rstrip('/')}/graphql",
        data=json.dumps({"query": query, "variables": variables}).encode(),
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    with (opener or urllib.request.urlopen)(request, timeout=30) as response:
        answer = json.loads(response.read() or b"null")
    if not isinstance(answer, dict) or answer.get("errors"):
        raise RuntimeError(f"OpenCTI GraphQL: {str((answer or {}).get('errors') if isinstance(answer, dict) else answer)[:300]}")
    return answer.get("data")


def verify_dashboard(
    name: str,
    since: str,
    graphql: Callable[[str, dict[str, Any]], Any],
    *,
    wait: float = VERIFY_WAIT_SECONDS,
    step: float = VERIFY_STEP,
    sleep: Callable[[float], Any] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """The newest OpenCTI dashboard called *name* created at or after *since* (ISO), re-read until it holds a widget.

    Answers ``{"verified": bool, "dashboard_id", "widgets", "waited_seconds"}`` (``verify_error`` when OpenCTI
    could not be read): verified is true when such a dashboard exists with at least one widget.
    """
    started = clock()
    found: dict[str, Any] | None = None
    while True:
        try:
            data = graphql(WORKSPACES_QUERY, {"search": name})
        except Exception as exc:  # noqa: BLE001 - reported in the row, never a crash of the table
            return {"verified": False, "verify_error": str(exc)[:300], "waited_seconds": round(clock() - started, 1)}
        edges = (((data or {}).get("workspaces") or {}).get("edges")) or []
        nodes = [
            edge["node"]
            for edge in edges
            if isinstance(edge, Mapping) and isinstance(edge.get("node"), Mapping)
            and edge["node"].get("name") == name and str(edge["node"].get("created_at") or "") >= since
        ]
        newest = max(nodes, key=lambda node: str(node.get("created_at") or ""), default=None)
        if newest is not None:
            found = {"dashboard_id": newest.get("id"), "widgets": manifest_widgets(newest.get("manifest"))}
        waited = round(clock() - started, 1)
        if (found and found["widgets"]) or waited + step > wait:
            return {
                "verified": bool(found and found["widgets"]),
                "dashboard_id": (found or {}).get("dashboard_id"),
                "widgets": (found or {}).get("widgets", 0),
                "waited_seconds": waited,
            }
        sleep(step)


def opencti_verifier(url: str, token: str, wait: float) -> Callable[[str, str], dict[str, Any]]:
    """``verify(name, since)`` for :func:`run_task`, reading the OpenCTI at *url* with *token*."""

    def graphql(query: str, variables: dict[str, Any]) -> Any:
        return opencti_graphql(url, token, query, variables)

    def verify(name: str, since: str) -> dict[str, Any]:
        return verify_dashboard(name, since, graphql, wait=wait)

    return verify


def run_task(
    client: Client,
    agents: dict[str, str],
    entry: str,
    task: tuple[str, str, str, int],
    *,
    timeout: float = TASK_TIMEOUT_SECONDS,
    expect_format: str | None = None,
    confirm: str | None = None,
    verify: Callable[[str, str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Ask one golden task and read its conversation back.

    ``confirm``: a second turn sent in the same conversation once the first answered with no error (the
    approval a creator agent asks for before it builds). ``verify(name, since)``: reads the dashboard the prompt
    names back from the product and fills ``verified``; called only when the prompt names one.
    """
    surface, label, expected, baseline = task
    row: dict[str, Any] = {
        "surface": surface,
        "task": label,
        "entry_agent": entry,
        "expected_agent": expected,
        "baseline_hops": baseline,
        "verified": None,
    }
    prompt = PROMPTS.get(label)
    if prompt is None or entry not in agents:
        return {**row, "skipped": "no prompt for this task" if prompt is None else f"agent {entry!r} is not seeded"}
    slug_of = {agent_id: slug for slug, agent_id in agents.items()}
    # A few seconds early: OpenCTI writes created_at with milliseconds, which sort before a bare "Z".
    since = (datetime.now(UTC) - timedelta(seconds=5)).isoformat(timespec="seconds").replace("+00:00", "Z")
    conversation = client.call(
        "POST", "/chat/conversations", {"agent_id": agents[entry], "title": f"routing eval: {label}"}
    )
    started = time.monotonic()
    error: str | None = None
    transfer: str | None = None
    tags: list[str] = []
    timed_out = False
    try:
        answer = client.call(
            "POST", f"/chat/conversations/{conversation['id']}/messages", {"content": prompt}, timeout=timeout
        )
        error = answer.get("error") if isinstance(answer, dict) else None
        transfer = answer.get("transfer_agent_id") if isinstance(answer, dict) else None
        if confirm and error is None:
            answer = client.call(
                "POST", f"/chat/conversations/{conversation['id']}/messages", {"content": confirm}, timeout=timeout
            )
            error = answer.get("error") if isinstance(answer, dict) else None
            transfer = (answer.get("transfer_agent_id") if isinstance(answer, dict) else None) or transfer
        reply = answer.get("assistant_message") if isinstance(answer, dict) else None
        tags = raw_html_tags(reply.get("content") if isinstance(reply, dict) else None)
    except RuntimeError as exc:
        error = str(exc)
    except (TimeoutError, urllib.error.URLError) as exc:
        if not _timed_out(exc):
            raise
        timed_out = True
        error = f"no answer within {timeout:.0f} s: the turn was still running"
    seconds = round(time.monotonic() - started, 1)
    traces, spans = settled_spans(client, conversation["id"]) if error is None else client.conversation_spans(
        conversation["id"]
    )
    measured = summarize(spans)
    last = max(traces, key=lambda t: str(t.get("started_at") or ""), default=None)
    final_agent = slug_of.get(str(transfer or (last or {}).get("agent_id") or agents[entry]))
    result = {
        **row,
        "conversation_id": conversation["id"],
        "final_agent": final_agent,
        "error": error,
        "seconds": seconds,
        "traces": len(traces),
        **measured,
        "success": verdict(expected, baseline, final_agent, error, measured["handovers"]),
    }
    if confirm:
        result["confirm"] = confirm
    if timed_out:
        # A failure on time, read with what the traces held when the runner gave up (hops, tokens so far).
        result["timed_out"] = True
    name = dashboard_name(prompt)
    if verify is not None and name and error is None:
        # The deliverable read back from the product: a route that answered but built nothing is a failure.
        result.update(verify(name, since))
        if result.get("verified") is False:
            result["success"] = False
    if error is None and not measured["generations"]:
        # No generation recorded: the agent that answered and the hops are unknown, so the row is neither a
        # pass nor a routing failure.
        result["success"] = None
        result["unmeasured"] = f"no generation recorded for the turn within {TRACE_SETTLE_SECONDS:.0f} s"
    if expect_format:
        result["expected_format"] = expect_format
    if tags:
        result["raw_html"] = tags
    broken = format_failure(expect_format, tags) if error is None else None
    if broken:
        # Markdown expected: the chat shows these tags as raw markup, the user does not get a readable answer.
        # HTML expected: the agent lost its own output format. Either way a failure, whatever the route.
        result["format_error"] = broken
        result["success"] = False
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", help="the instance, e.g. http://127.0.0.1:8100")
    parser.add_argument("--email")
    parser.add_argument("--password")
    parser.add_argument(
        "--last", action="store_true", help="print the table the last run kept in this checkout; asks no task"
    )
    parser.add_argument(
        "--quota",
        action="store_true",
        help="print the lab model key's LLM proxy quota usage (GET /v1/quota); asks no task, spends no call",
    )
    parser.add_argument(
        "--surface", action="append", help="only these surfaces (repeatable): opencti, openaev, opencrq, xtm_one"
    )
    parser.add_argument(
        "--task",
        action="append",
        help="only the golden tasks whose label or prompt contains this text (repeatable); exit 5 when none does",
    )
    parser.add_argument(
        "--entry",
        help="ask the chosen tasks of this agent (slug) directly; each must end on it with no handover",
    )
    parser.add_argument(
        "--expect-format",
        choices=ANSWER_FORMATS,
        help="the answer's format: markdown (default rule: no raw HTML tag) or html (at least one tag)",
    )
    parser.add_argument("--out", help="write the JSON here as well as to stdout (its directory is created)")
    parser.add_argument(
        "--task-timeout",
        type=float,
        default=TASK_TIMEOUT_SECONDS,
        help="seconds one task's turn may take (default %(default)s); past it the row is timed_out and the next runs",
    )
    parser.add_argument(
        "--confirm",
        help="a second turn sent in the same conversation once the first answered (e.g. 'Build it as it is.')",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="read the dashboard a task names back from the lab's OpenCTI GraphQL: verified, dashboard_id, widgets",
    )
    parser.add_argument("--opencti-url", default=OPENCTI_URL, help="the OpenCTI --verify reads (default %(default)s)")
    parser.add_argument(
        "--opencti-token", default=OPENCTI_TOKEN, help="its API token (default: the lab OpenCTI's throwaway admin token)"
    )
    parser.add_argument(
        "--verify-wait",
        type=float,
        default=VERIFY_WAIT_SECONDS,
        help="seconds --verify re-reads the dashboard for a widget (default %(default)s)",
    )
    parser.add_argument(
        "--wait",
        type=float,
        default=WAIT_SECONDS,
        help="seconds to wait for the integrations the tasks need (default %(default)s); exit 2 past it",
    )
    args = parser.parse_args(argv)
    if args.last:
        return show_last()
    if args.quota:
        sys.stdout.write(json.dumps(model_quota(), indent=2) + "\n")
        return 0
    if not (args.base and args.email and args.password):
        parser.error("--base, --email and --password are required unless --last or --quota is given")
    tasks, entries = golden_tasks()
    chosen = select_tasks(tasks, args.task, args.surface)
    if not chosen:
        # Before the sign-in: a filter that selects nothing never spends a lab call or passes with ran 0.
        labels = "\n".join(f"  {surface}: {label}" for surface, label, _, _ in tasks)
        sys.stderr.write(
            f"no golden task matches --task {args.task or []} --surface {args.surface or []}; "
            f"--task matches a label or a prompt, the labels are:\n{labels}\n"
        )
        return NO_TASK
    client = Client(args.base)
    try:
        client.sign_in(args.email, args.password)
    except RuntimeError as exc:
        if ": 503 " not in str(exc):
            raise
        # Still unavailable past the retry budget: the lab is not ready, no task failed.
        reason = f"the lab answered 503 to the sign-in for {UNAVAILABLE_BUDGET:.0f} s ({exc}): no task ran"
        write_report({"base": args.base, "not_ready": reason, "tasks": []}, args.out)
        sys.stderr.write(reason + "\n")
        return NOT_READY
    agents = client.agents_by_slug()
    # The model first: an exhausted proxy key fails every task, so the integrations are not waited for.
    exhausted = preflight_model()
    if exhausted:
        write_report({"base": args.base, "quota_exceeded": exhausted, "tasks": []}, args.out)
        sys.stderr.write(f"{exhausted}: no task ran\n")
        return QUOTA_EXIT
    readiness = wait_for_integrations(client, needed_providers(chosen), args.wait)
    if not readiness["ready"]:
        missing = ", ".join(PRODUCTS[p] for p, row in readiness["integrations"].items() if not row)
        reason = f"the lab's {missing} is not registered yet (waited {readiness['waited_seconds']} s): no task ran"
        write_report({"base": args.base, "readiness": readiness, "not_ready": reason, "tasks": []}, args.out)
        sys.stderr.write(reason + "\n")
        return NOT_READY
    verifier = opencti_verifier(args.opencti_url, args.opencti_token, args.verify_wait) if args.verify else None
    rows: list[dict[str, Any]] = []
    for task in chosen:
        entry = args.entry or entries[task[0]]
        asked = entry_task(task, entry) if args.entry else task
        row = run_task(
            client,
            agents,
            entry,
            asked,
            timeout=args.task_timeout,
            expect_format=args.expect_format,
            confirm=args.confirm,
            verify=verifier,
        )
        rows.append(row)
        # Kept after each task: a run cut before its end leaves the rows it paid for.
        save_last({"base": args.base, "readiness": readiness, "partial": True, "tasks": rows})
        exhausted = quota_message(str(row.get("error") or ""))
        if exhausted:
            break
    ran = [row for row in rows if "skipped" not in row]
    unmeasured = sum(1 for row in ran if row.get("success") is None)
    report = {
        "base": args.base,
        "readiness": readiness,
        **({"quota_exceeded": exhausted} if exhausted else {}),
        "tasks": rows,
        "summary": {
            "ran": len(ran),
            "passed": sum(1 for row in ran if row["success"]),
            "failed": sum(1 for row in ran if row["success"] is False),
            "unmeasured": unmeasured,
            "timed_out": sum(1 for row in ran if row.get("timed_out")),
            "skipped": len(rows) - len(ran),
            "input_tokens": sum(row["input_tokens"] for row in ran),
            "output_tokens": sum(row["output_tokens"] for row in ran),
            "seconds": round(sum(row["seconds"] for row in ran), 1),
        },
    }
    write_report(report, args.out)
    if exhausted:
        sys.stderr.write(f"{exhausted}: the eval stopped, the tasks left were not run\n")
        return QUOTA_EXIT
    return 0 if report["summary"]["passed"] == len(ran) else 1


if __name__ == "__main__":
    sys.exit(main())
