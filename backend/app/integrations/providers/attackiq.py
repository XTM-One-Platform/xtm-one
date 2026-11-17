# SPDX-License-Identifier: Apache-2.0

"""AttackIQ integration - Breach & Attack Simulation / Continuous Threat Exposure Management.

Full-fledged AttackIQ (BAS / CTEM) integration covering the security-validation
loop as a best-in-class building block for an agentic security team:

  - Assessments (projects): list/search, get, run all tests, execution status,
    run history, and a consolidated ``manage_attackiq_assessment`` for the
    lifecycle long tail (activate / create-from-template / assign assets &
    tags / delete)
  - Tests (ScenarioMasterJobs / "master jobs"): list per assessment, get,
    pass/fail/detected status
  - Scenarios (attack content library): list/search, get
  - Results (validation outcomes): list (optionally by run), get, per-phase
    results
  - Assets (on-prem endpoints / test points): list, get
  - Tags: list, and a consolidated ``manage_attackiq_tag`` (create / delete a
    tag, apply / remove a tag on an object)
  - Detection rules (unified mitigations): list, get
  - Connectors (platform integrations configured in AttackIQ): list, get

Authentication is an AttackIQ **account API token** sent in the
``Authorization`` header with the ``Token`` scheme (NOT ``Bearer``), against the
customer's tenant instance (``https://<tenant>.attackiq.com``). The integration
stores the tenant base URL + API token; the compound ``base_url||api_token``
token is assembled by ``integration_tools._get_token`` and every call targets
``{base_url}/v1/...`` (a few endpoints use ``/v2/...``). Responses follow the
Django REST Framework envelope ``{count, next, previous, results}``.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from app.core.egress import egress_client
from app.integrations.http import (
    format_entity_list,
    format_pagination_hint,
    parse_compound_token,
    safe_request,
)
from app.tools.base import CallableToolExecutor, ToolDefinition

_PROVIDER = "AttackIQ"
_DEFAULT_TIMEOUT = 30
_DEFAULT_PAGE_SIZE = 20
_MAX_PAGE_SIZE = 100

# Long-tail action enums for the consolidated manage_* tools.
_ASSESSMENT_ACTIONS: frozenset[str] = frozenset(
    {"activate", "create_from_template", "update_defaults", "delete"}
)
_TAG_ACTIONS: frozenset[str] = frozenset({"create", "delete", "apply", "remove"})


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _trunc(text: Any, max_len: int = 200) -> str:
    if not text:
        return ""
    text = str(text)
    return text[:max_len] + ("…" if len(text) > max_len else "")


def _dt(value: Any) -> str:
    """Trim an ISO timestamp to second precision for display."""
    return str(value or "")[:19]


def _clamp_page_size(value: Any) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return _DEFAULT_PAGE_SIZE
    return max(1, min(n, _MAX_PAGE_SIZE))


def _clamp_page(value: Any) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 1
    return max(1, n)


def _results(resp: dict | list | None) -> list:
    """Return the ``results`` list from a DRF paginated envelope.

    Falls back to the payload itself when the endpoint returns a bare list
    (a few AttackIQ endpoints are non-paginated).
    """
    if isinstance(resp, dict):
        results = resp.get("results")
        if isinstance(results, list):
            return results
        return []
    if isinstance(resp, list):
        return resp
    return []


def _count(resp: dict | list | None) -> int | None:
    if isinstance(resp, dict):
        count = resp.get("count")
        if isinstance(count, int):
            return count
    return None


def _next_page_hint(resp: dict | list | None, page: int) -> str:
    """Build a pagination hint when the DRF ``next`` link is present."""
    if isinstance(resp, dict) and resp.get("next"):
        return format_pagination_hint(str(page + 1), param_name="page")
    return ""


def _detail(resp: dict | list | None) -> str:
    """Return a ``detail`` message from an AttackIQ error/success envelope."""
    if isinstance(resp, dict):
        detail = resp.get("detail") or resp.get("message")
        if isinstance(detail, str):
            return detail
    return ""


# ---------------------------------------------------------------------------
# Tool builder
# ---------------------------------------------------------------------------


def build_tools(
    token: str,
    executor: CallableToolExecutor,
    **_kwargs: Any,
) -> list[ToolDefinition]:
    """Build AttackIQ tools.

    ``token`` is the compound ``base_url||api_token`` assembled by
    ``integration_tools._get_token`` from the integration's stored tenant URL
    and account API token.
    """
    base_url, api_token = parse_compound_token(token, 2)
    base_url = (base_url or "").rstrip("/")
    headers = {
        "Authorization": f"Token {api_token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

    async def _api(
        method: str,
        path: str,
        action: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any | None = None,
    ) -> tuple[dict | list | None, str | None]:
        """Authenticated request helper returning ``(json, error)``."""
        if not base_url:
            return None, (
                "Error: AttackIQ tenant base URL is not configured. Edit the "
                "integration and set the base URL (e.g. "
                "https://yourcompany.attackiq.com)."
            )
        if not api_token:
            return None, (
                "Error: AttackIQ API token is not configured. Edit the "
                "integration and set the account API token."
            )
        url = f"{base_url}/{path.lstrip('/')}"
        if params:
            # Drop None values so optional filters don't leak empty params.
            clean = {k: v for k, v in params.items() if v not in (None, "")}
            if clean:
                url = f"{url}?{urlencode(clean, doseq=True)}"
        async with egress_client(timeout=_DEFAULT_TIMEOUT) as client:
            resp, err = await safe_request(
                client,
                method,
                url,
                _PROVIDER,
                action,
                headers=headers,
                json=json_body,
            )
            if err:
                return None, err
            if resp is None or resp.status_code == 204 or not resp.content:
                return {}, None
            try:
                return resp.json(), None
            except Exception:
                return {}, None

    # ══════════════════════════════════════════════════════════════════════
    # 0. ASSESSMENTS (projects)
    # ══════════════════════════════════════════════════════════════════════

    async def list_attackiq_assessments(
        search: str = "",
        page: int = 1,
        page_size: int = _DEFAULT_PAGE_SIZE,
        ordering: str = "-modified",
    ) -> str:
        """List/search AttackIQ assessments (projects)."""
        params: dict[str, Any] = {
            "page": _clamp_page(page),
            "page_size": _clamp_page_size(page_size),
        }
        if search:
            params["search"] = search
        if ordering:
            params["ordering"] = ordering
        resp, err = await _api(
            "get", "/v1/assessments", "list assessments", params=params
        )
        if err:
            return err

        def _fmt(a: dict) -> str:
            return (
                f"  {a.get('name', 'Unnamed')} "
                f"[{a.get('project_state', 'N/A')}]\n"
                f"    id: {a.get('id', '?')} | "
                f"Tests: {a.get('master_job_count', 'N/A')} | "
                f"Assets: {a.get('default_asset_count', 'N/A')} | "
                f"Owner: {a.get('owner', 'N/A')} | "
                f"Modified: {_dt(a.get('modified'))}"
            )

        body = format_entity_list(
            _results(resp), _fmt, noun="assessment", total=_count(resp)
        )
        return body + _next_page_hint(resp, params["page"])

    async def get_attackiq_assessment(assessment_id: str) -> str:
        """Get full detail of an AttackIQ assessment by ID."""
        resp, err = await _api(
            "get",
            f"/v1/assessments/{assessment_id}",
            f"get assessment {assessment_id}",
        )
        if err:
            return err
        if not resp:
            return f"Assessment {assessment_id} not found."
        template = resp.get("project_template") or {}
        lines = [
            f"Assessment: {resp.get('name', 'Unnamed')}",
            f"ID: {resp.get('id', '?')}",
            f"State: {resp.get('project_state', 'N/A')} | "
            f"Version: {resp.get('version', 'N/A')}",
            f"Description: {_trunc(resp.get('description'), 400) or 'None'}",
            f"Template: {template.get('template_name', 'N/A')} "
            f"({template.get('id', 'N/A')})",
            f"Tests (master jobs): {resp.get('master_job_count', 'N/A')} | "
            f"Default assets: {resp.get('default_asset_count', 'N/A')} | "
            f"Default asset groups: {resp.get('default_asset_group_count', 'N/A')}",
            f"Default schedule (cron): {resp.get('default_schedule', 'N/A')}",
            f"Owner: {resp.get('owner', 'N/A')} | "
            f"Creator: {resp.get('creator', 'N/A')} | "
            f"User: {resp.get('user', 'N/A')}",
            f"Start: {_dt(resp.get('start_date'))} | "
            f"End: {_dt(resp.get('end_date'))}",
            f"Created: {_dt(resp.get('created'))} | "
            f"Modified: {_dt(resp.get('modified'))}",
        ]
        return "\n".join(lines)

    async def get_attackiq_assessment_status(assessment_id: str) -> str:
        """Check whether an AttackIQ assessment is currently running on-demand."""
        resp, err = await _api(
            "get",
            f"/v1/assessments/{assessment_id}/is_on_demand_running",
            f"get execution status for assessment {assessment_id}",
        )
        if err:
            return err
        # The endpoint returns ``{"message": <bool>}`` (per the XSOAR and
        # Swimlane references). Parse the boolean explicitly - a non-boolean
        # payload (e.g. a human-readable status string) must not be coerced
        # via truthiness into "running".
        message = resp.get("message") if isinstance(resp, dict) else None
        if isinstance(message, bool):
            state = "running" if message else "not running"
            return f"Assessment {assessment_id} is currently {state} on-demand."
        if isinstance(message, str) and message.strip():
            return (
                f"Assessment {assessment_id} on-demand execution status: "
                f"{message.strip()}"
            )
        return (
            f"Assessment {assessment_id} on-demand execution status is "
            f"unknown (unexpected API response)."
        )

    async def list_attackiq_assessment_runs(
        assessment_id: str,
        page: int = 1,
        page_size: int = _DEFAULT_PAGE_SIZE,
    ) -> str:
        """List the historical runs of an AttackIQ assessment."""
        params = {
            "page": _clamp_page(page),
            "page_size": _clamp_page_size(page_size),
        }
        resp, err = await _api(
            "get",
            f"/v1/public/assessment/{assessment_id}/runs",
            f"list runs for assessment {assessment_id}",
            params=params,
        )
        if err:
            return err

        def _fmt(r: dict) -> str:
            return (
                f"  Run {r.get('id', '?')} — "
                f"State: {r.get('state', r.get('status', 'N/A'))} | "
                f"Started: {_dt(r.get('created') or r.get('created_at'))} | "
                f"Ended: {_dt(r.get('modified') or r.get('ended_at'))}"
            )

        body = format_entity_list(
            _results(resp), _fmt, noun="run", total=_count(resp)
        )
        return body + _next_page_hint(resp, params["page"])

    async def run_attackiq_assessment(assessment_id: str) -> str:
        """Run (launch) all tests in an AttackIQ assessment.

        Kicks off a full on-demand execution of every runnable test in the
        assessment - the core "emulate the adversary" action. Returns the
        started run's ID when available; poll ``get_attackiq_assessment_status``
        or ``list_attackiq_assessment_runs`` to track progress. This does not
        wait for completion.
        """
        resp, err = await _api(
            "post",
            f"/v1/assessments/{assessment_id}/run_all",
            f"run assessment {assessment_id}",
            json_body={},
        )
        if err:
            return err
        run_id = resp.get("run_id") if isinstance(resp, dict) else None
        if run_id:
            return (
                f"Assessment {assessment_id} started. Run ID: {run_id}. "
                f"Use get_attackiq_assessment_status or "
                f"list_attackiq_assessment_runs to track progress."
            )
        msg = _detail(resp)
        return (
            f"Run requested for assessment {assessment_id}."
            + (f" {msg}" if msg else "")
        )

    async def manage_attackiq_assessment(
        action: str,
        assessment_id: str = "",
        name: str = "",
        template_id: str = "",
        assets: str = "",
        asset_groups: str = "",
        tags: str = "",
    ) -> str:
        """Manage the AttackIQ assessment lifecycle long tail.

        ``action`` is one of:
          - ``activate``: activate an assessment (required before some
            executions; needs ``assessment_id``).
          - ``create_from_template``: create a new assessment from a template
            (needs ``name`` and ``template_id``).
          - ``update_defaults``: assign default assets, asset groups, and/or
            asset tags to an assessment (needs ``assessment_id`` and at least
            one of ``assets`` / ``asset_groups`` / ``tags``, each a
            comma-separated UUID list).
          - ``delete``: permanently delete an assessment (needs
            ``assessment_id``). This is destructive and cannot be undone.
        """
        action = (action or "").strip().lower()
        if action not in _ASSESSMENT_ACTIONS:
            return (
                f"Error: invalid action '{action}'. Use one of: "
                f"{', '.join(sorted(_ASSESSMENT_ACTIONS))}."
            )

        def _split(csv: str) -> list[str]:
            return [x.strip() for x in (csv or "").split(",") if x.strip()]

        if action == "activate":
            if not assessment_id:
                return "Error: 'assessment_id' is required for action 'activate'."
            resp, err = await _api(
                "post",
                f"/v1/assessments/{assessment_id}/activate",
                f"activate assessment {assessment_id}",
                json_body={},
            )
            if err:
                return err
            return _detail(resp) or f"Assessment {assessment_id} activated."

        if action == "create_from_template":
            if not name or not template_id:
                return (
                    "Error: both 'name' and 'template_id' are required for "
                    "action 'create_from_template'."
                )
            resp, err = await _api(
                "post",
                "/v1/assessments/project_from_template",
                "create assessment from template",
                json_body={"project_name": name, "template": template_id},
            )
            if err:
                return err
            new_id = resp.get("project_id") if isinstance(resp, dict) else None
            return (
                f"Assessment '{name}' created"
                + (f" (id: {new_id})." if new_id else ".")
            )

        if action == "update_defaults":
            if not assessment_id:
                return (
                    "Error: 'assessment_id' is required for action "
                    "'update_defaults'."
                )
            body: dict[str, Any] = {}
            if assets:
                body["assets"] = _split(assets)
            if asset_groups:
                body["asset_groups"] = _split(asset_groups)
            if tags:
                body["tags"] = _split(tags)
            if not body:
                return (
                    "Error: provide at least one of 'assets', 'asset_groups', "
                    "or 'tags' for action 'update_defaults'."
                )
            resp, err = await _api(
                "post",
                f"/v1/assessments/{assessment_id}/update_defaults",
                f"update defaults for assessment {assessment_id}",
                json_body=body,
            )
            if err:
                return err
            return (
                _detail(resp)
                or f"Default assets/tags updated for assessment {assessment_id}."
            )

        # delete
        if not assessment_id:
            return "Error: 'assessment_id' is required for action 'delete'."
        _resp, err = await _api(
            "delete",
            f"/v1/assessments/{assessment_id}",
            f"delete assessment {assessment_id}",
        )
        if err:
            return err
        return f"Assessment {assessment_id} deleted."

    # ══════════════════════════════════════════════════════════════════════
    # 1. TESTS (ScenarioMasterJobs / master jobs)
    # ══════════════════════════════════════════════════════════════════════

    async def list_attackiq_tests(
        assessment_id: str,
        page: int = 1,
        page_size: int = _DEFAULT_PAGE_SIZE,
    ) -> str:
        """List the tests (master jobs) inside an AttackIQ assessment."""
        params = {
            "project": assessment_id,
            "page": _clamp_page(page),
            "page_size": _clamp_page_size(page_size),
        }
        resp, err = await _api(
            "get",
            "/v1/tests",
            f"list tests for assessment {assessment_id}",
            params=params,
        )
        if err:
            return err

        def _fmt(t: dict) -> str:
            return (
                f"  {t.get('name', 'Unnamed')}\n"
                f"    id: {t.get('id', '?')} | "
                f"Runnable: {t.get('runnable', 'N/A')} | "
                f"Assets: {t.get('total_asset_count', 'N/A')} | "
                f"Last result: {t.get('last_result', 'N/A')} | "
                f"Modified: {_dt(t.get('modified'))}"
            )

        body = format_entity_list(
            _results(resp), _fmt, noun="test", total=_count(resp)
        )
        return body + _next_page_hint(resp, params["page"])

    async def get_attackiq_test(test_id: str) -> str:
        """Get full detail of an AttackIQ test (master job) by ID."""
        resp, err = await _api(
            "get", f"/v1/tests/{test_id}", f"get test {test_id}"
        )
        if err:
            return err
        if not resp:
            return f"Test {test_id} not found."
        scenarios = resp.get("scenarios") or []
        assets = resp.get("assets") or []
        lines = [
            f"Test: {resp.get('name', 'Unnamed')}",
            f"ID: {resp.get('id', '?')}",
            f"Assessment: {resp.get('project', 'N/A')}",
            f"Description: {_trunc(resp.get('description'), 300) or 'None'}",
            f"Runnable: {resp.get('runnable', 'N/A')} | "
            f"Cron: {resp.get('cron_expression', 'N/A')}",
            f"Total assets: {resp.get('total_asset_count', 'N/A')} | "
            f"Scenarios: {len(scenarios)}",
            f"Last result: {resp.get('last_result', 'N/A')} | "
            f"Modified: {_dt(resp.get('modified'))}",
        ]
        if scenarios:
            lines.append("Scenarios:")
            for s in scenarios[:10]:
                lines.append(f"  - {s.get('name', '?')} ({s.get('id', '?')})")
        if assets:
            lines.append(f"Assets: {len(assets)} assigned")
        return "\n".join(lines)

    async def get_attackiq_test_status(test_id: str) -> str:
        """Get the pass/fail/detected outcome counts for an AttackIQ test."""
        resp, err = await _api(
            "get",
            f"/v1/tests/{test_id}/get_status",
            f"get status for test {test_id}",
        )
        if err:
            return err
        if not isinstance(resp, dict) or not resp:
            return f"No status available for test {test_id}."
        return (
            f"Test {test_id} status:\n"
            f"  Total: {resp.get('total', 'N/A')} | "
            f"Finished: {resp.get('finished', 'N/A')}\n"
            f"  Passed: {resp.get('passed', 'N/A')} | "
            f"Failed: {resp.get('failed', 'N/A')} | "
            f"Detected: {resp.get('detected', 'N/A')} | "
            f"Errored: {resp.get('errored', 'N/A')}"
        )

    # ══════════════════════════════════════════════════════════════════════
    # 2. SCENARIOS (attack content library)
    # ══════════════════════════════════════════════════════════════════════

    async def list_attackiq_scenarios(
        search: str = "",
        page: int = 1,
        page_size: int = _DEFAULT_PAGE_SIZE,
    ) -> str:
        """List/search AttackIQ scenarios (the attack content library)."""
        params: dict[str, Any] = {
            "minimal": "true",
            "page": _clamp_page(page),
            "page_size": _clamp_page_size(page_size),
        }
        if search:
            params["search"] = search
        resp, err = await _api(
            "get", "/v1/scenarios", "list scenarios", params=params
        )
        if err:
            return err

        def _fmt(s: dict) -> str:
            return (
                f"  {s.get('name', 'Unnamed')}\n"
                f"    id: {s.get('id', '?')} | "
                f"Multi-asset: {s.get('is_multi_asset', 'N/A')} | "
                f"Modified: {_dt(s.get('modified'))}"
            )

        body = format_entity_list(
            _results(resp), _fmt, noun="scenario", total=_count(resp)
        )
        return body + _next_page_hint(resp, params["page"])

    async def get_attackiq_scenario(scenario_id: str) -> str:
        """Get full detail of an AttackIQ scenario by ID."""
        resp, err = await _api(
            "get",
            f"/v1/scenarios/{scenario_id}",
            f"get scenario {scenario_id}",
        )
        if err:
            return err
        if not resp:
            return f"Scenario {scenario_id} not found."
        platforms = resp.get("supported_platforms") or {}
        platform_list = (
            list(platforms.keys()) if isinstance(platforms, dict) else platforms
        )
        lines = [
            f"Scenario: {resp.get('name', 'Unnamed')}",
            f"ID: {resp.get('id', '?')}",
            f"Description: {_trunc(resp.get('description'), 400) or 'None'}",
            f"Supported platforms: {', '.join(platform_list) or 'N/A'}",
            f"Multi-asset: {resp.get('is_multi_asset', 'N/A')} | "
            f"Runnable: {resp.get('runnable', 'N/A')}",
            f"Modified: {_dt(resp.get('modified'))}",
        ]
        return "\n".join(lines)

    # ══════════════════════════════════════════════════════════════════════
    # 3. RESULTS (validation outcomes)
    # ══════════════════════════════════════════════════════════════════════

    async def list_attackiq_results(
        assessment_id: str = "",
        run_id: str = "",
        search: str = "",
        page: int = 1,
        page_size: int = _DEFAULT_PAGE_SIZE,
    ) -> str:
        """List AttackIQ result summaries, optionally scoped to an assessment/run.

        When ``assessment_id`` is provided the results are that assessment's
        summaries across all runs (prevention/detection outcome per
        scenario+asset); add ``run_id`` to narrow to a single run.
        """
        params: dict[str, Any] = {
            "page": _clamp_page(page),
            "page_size": _clamp_page_size(page_size),
        }
        if assessment_id:
            params["project_id"] = assessment_id
        if run_id:
            params["run_id"] = run_id
        if assessment_id or run_id:
            # Assessment-scoped result summaries (per the AttackIQ API
            # reference this flag accompanies project_id/run_id filters).
            params["assessment_results"] = "true"
        if search:
            params["search"] = search
        resp, err = await _api(
            "get", "/v1/results", "list results", params=params
        )
        if err:
            return err

        def _fmt(r: dict) -> str:
            asset = r.get("asset")
            hostname = (
                asset.get("hostname") if isinstance(asset, dict) else None
            ) or r.get("asset_hostname", "N/A")
            scenario = r.get("scenario")
            scenario_name = (
                scenario.get("name") if isinstance(scenario, dict) else None
            ) or r.get("scenario_name", "N/A")
            return (
                f"  {scenario_name} on {hostname}\n"
                f"    id: {r.get('id', '?')} | "
                f"Outcome: {r.get('outcome_name', r.get('outcome', 'N/A'))} | "
                f"Detection: {r.get('detection_outcome', 'N/A')} | "
                f"Modified: {_dt(r.get('modified'))}"
            )

        body = format_entity_list(
            _results(resp), _fmt, noun="result", total=_count(resp)
        )
        return body + _next_page_hint(resp, params["page"])

    async def get_attackiq_result(result_id: str) -> str:
        """Get full detail of a single AttackIQ result by ID."""
        resp, err = await _api(
            "get", f"/v1/results/{result_id}", f"get result {result_id}"
        )
        if err:
            return err
        if not resp:
            return f"Result {result_id} not found."
        # Result payloads are large and free-form; surface a compact,
        # readable subset of the fields agents act on.
        asset = resp.get("asset")
        hostname = (
            asset.get("hostname") if isinstance(asset, dict) else asset
        ) or "N/A"
        scenario = resp.get("scenario")
        scenario_name = (
            scenario.get("name") if isinstance(scenario, dict) else scenario
        ) or "N/A"
        lines = [
            f"Result: {result_id}",
            f"Scenario: {scenario_name}",
            f"Asset: {hostname}",
            f"Outcome: {resp.get('outcome_name', resp.get('outcome', 'N/A'))} | "
            f"Detection: {resp.get('detection_outcome', 'N/A')}",
            f"Job state: {resp.get('job_state_name', 'N/A')}",
            f"Modified: {_dt(resp.get('modified'))}",
        ]
        return "\n".join(lines)

    async def list_attackiq_phase_results(
        assessment_id: str,
        run_id: str = "",
        page: int = 1,
        page_size: int = _DEFAULT_PAGE_SIZE,
    ) -> str:
        """List per-phase results for an AttackIQ assessment (optionally a run)."""
        params: dict[str, Any] = {
            "project_id": assessment_id,
            "page": _clamp_page(page),
            "page_size": _clamp_page_size(page_size),
        }
        if run_id:
            params["project_run_id"] = run_id
        resp, err = await _api(
            "get",
            "/v1/phase_results",
            f"list phase results for assessment {assessment_id}",
            params=params,
        )
        if err:
            return err

        def _fmt(p: dict) -> str:
            return (
                f"  Phase: {p.get('phase_name', p.get('name', 'N/A'))}\n"
                f"    id: {p.get('id', '?')} | "
                f"Outcome: {p.get('outcome', p.get('outcome_name', 'N/A'))} | "
                f"Modified: {_dt(p.get('modified'))}"
            )

        body = format_entity_list(
            _results(resp), _fmt, noun="phase result", total=_count(resp)
        )
        return body + _next_page_hint(resp, params["page"])

    # ══════════════════════════════════════════════════════════════════════
    # 4. ASSETS (on-prem endpoints / test points)
    # ══════════════════════════════════════════════════════════════════════

    async def list_attackiq_assets(
        assessment_id: str = "",
        page: int = 1,
        page_size: int = _DEFAULT_PAGE_SIZE,
    ) -> str:
        """List AttackIQ assets (on-prem endpoints / test points).

        Pass ``assessment_id`` to list the assets assigned to that
        assessment's tests; omit it to list all assets in the tenant.
        """
        params: dict[str, Any] = {
            "page": _clamp_page(page),
            "page_size": _clamp_page_size(page_size),
        }

        def _fmt(a: dict) -> str:
            return (
                f"  {a.get('hostname', 'Unknown')} "
                f"[{a.get('status', 'N/A')}]\n"
                f"    id: {a.get('id', '?')} | "
                f"IPv4: {a.get('ipv4_address', 'N/A')} | "
                f"OS: {a.get('product_name', 'N/A')} | "
                f"User: {a.get('user', 'N/A')} | "
                f"Modified: {_dt(a.get('modified'))}"
            )

        if assessment_id:
            # ``/v2/assets`` has no documented assessment filter (the official
            # aiq-platform-api SDK and the XSOAR/Swimlane references never
            # filter assets by project, and DRF silently ignores unknown
            # params — the whole tenant would come back mislabelled). Scope
            # via the assessment's tests instead: each ``/v1/tests?project=``
            # row embeds its assigned assets (per the XSOAR reference).
            params["project"] = assessment_id
            resp, err = await _api(
                "get",
                "/v1/tests",
                f"list assets for assessment {assessment_id}",
                params=params,
            )
            if err:
                return err
            seen: dict[str, dict] = {}
            for test in _results(resp):
                for asset in test.get("assets") or []:
                    if isinstance(asset, dict):
                        seen.setdefault(str(asset.get("id")), asset)
            body = format_entity_list(
                list(seen.values()),
                _fmt,
                noun="asset",
                empty_msg=(
                    f"No assets assigned to the tests of assessment "
                    f"{assessment_id} (tests using the assessment's default "
                    f"assets may not list them individually)."
                ),
            )
            # The pagination walks the assessment's TESTS — a next page of
            # tests may carry more assets.
            return body + _next_page_hint(resp, params["page"])

        resp, err = await _api(
            "get", "/v2/assets", "list assets", params=params
        )
        if err:
            return err
        body = format_entity_list(
            _results(resp), _fmt, noun="asset", total=_count(resp)
        )
        return body + _next_page_hint(resp, params["page"])

    async def get_attackiq_asset(asset_id: str) -> str:
        """Get full detail of an AttackIQ asset (test point) by ID."""
        resp, err = await _api(
            "get", f"/v2/assets/{asset_id}", f"get asset {asset_id}"
        )
        if err:
            return err
        if not resp:
            return f"Asset {asset_id} not found."
        groups = resp.get("asset_groups") or []
        lines = [
            f"Asset: {resp.get('hostname', 'Unknown')}",
            f"ID: {resp.get('id', '?')}",
            f"Status: {resp.get('status', 'N/A')}",
            f"IPv4: {resp.get('ipv4_address', 'N/A')} | "
            f"IPv6: {resp.get('ipv6_address', 'N/A')} | "
            f"MAC: {resp.get('mac_address', 'N/A')}",
            f"OS: {resp.get('product_name', 'N/A')} "
            f"({resp.get('processor_arch', 'N/A')})",
            f"Domain: {resp.get('domain_name', 'N/A')} | "
            f"User: {resp.get('user', 'N/A')}",
            f"Groups: {', '.join(g.get('name', '?') for g in groups) or 'None'}",
            f"Modified: {_dt(resp.get('modified'))}",
        ]
        return "\n".join(lines)

    # ══════════════════════════════════════════════════════════════════════
    # 5. TAGS
    # ══════════════════════════════════════════════════════════════════════

    async def list_attackiq_tags(
        search: str = "",
        page: int = 1,
        page_size: int = _DEFAULT_PAGE_SIZE,
    ) -> str:
        """List/search AttackIQ tags (labels for organising content and assets)."""
        params: dict[str, Any] = {
            "page": _clamp_page(page),
            "page_size": _clamp_page_size(page_size),
        }
        if search:
            params["search"] = search
        resp, err = await _api("get", "/v1/tags", "list tags", params=params)
        if err:
            return err

        def _fmt(t: dict) -> str:
            tag_set = t.get("tag_set")
            tag_set_name = (
                tag_set.get("name") if isinstance(tag_set, dict) else tag_set
            )
            return (
                f"  {t.get('display_name') or t.get('name', 'Unnamed')} "
                f"(id: {t.get('id', '?')}) | Set: {tag_set_name or 'N/A'}"
            )

        body = format_entity_list(
            _results(resp), _fmt, noun="tag", total=_count(resp)
        )
        return body + _next_page_hint(resp, params["page"])

    async def manage_attackiq_tag(
        action: str,
        name: str = "",
        tag_set: str = "",
        display_name: str = "",
        tag_id: str = "",
        content_type: str = "",
        object_id: str = "",
        tagged_item_id: str = "",
    ) -> str:
        """Manage AttackIQ tags and tag assignments.

        ``action`` is one of:
          - ``create``: create a tag (needs ``name`` and ``tag_set`` [the tag
            set UUID]; ``display_name`` optional).
          - ``delete``: delete a tag (needs ``tag_id``). Destructive.
          - ``apply``: apply a tag to an object / create a tagged item (needs
            ``tag_id``, ``content_type`` [e.g. ``asset``, ``project``,
            ``scenario``] and ``object_id``). Returns the tagged-item ID.
          - ``remove``: remove a tag from an object by deleting the tagged
            item (needs ``tagged_item_id``, the ID returned by ``apply``).
            Destructive.
        """
        action = (action or "").strip().lower()
        if action not in _TAG_ACTIONS:
            return (
                f"Error: invalid action '{action}'. Use one of: "
                f"{', '.join(sorted(_TAG_ACTIONS))}."
            )

        if action == "create":
            if not name or not tag_set:
                return (
                    "Error: both 'name' and 'tag_set' (the tag set UUID) are "
                    "required for action 'create'."
                )
            resp, err = await _api(
                "post",
                "/v1/tags",
                f"create tag {name}",
                json_body={
                    "name": name,
                    "display_name": display_name or name,
                    "tag_set": tag_set,
                },
            )
            if err:
                return err
            new_id = resp.get("id") if isinstance(resp, dict) else None
            return f"Tag '{name}' created" + (f" (id: {new_id})." if new_id else ".")

        if action == "delete":
            if not tag_id:
                return "Error: 'tag_id' is required for action 'delete'."
            _resp, err = await _api(
                "delete", f"/v1/tags/{tag_id}", f"delete tag {tag_id}"
            )
            if err:
                return err
            return f"Tag {tag_id} deleted."

        if action == "apply":
            if not tag_id or not content_type or not object_id:
                return (
                    "Error: 'tag_id', 'content_type', and 'object_id' are all "
                    "required for action 'apply'."
                )
            resp, err = await _api(
                "post",
                "/v1/tagged_items",
                f"apply tag {tag_id} to {content_type} {object_id}",
                json_body={
                    "tag": tag_id,
                    "content_type": content_type,
                    "object_id": object_id,
                },
            )
            if err:
                return err
            item_id = resp.get("id") if isinstance(resp, dict) else None
            return (
                f"Tag {tag_id} applied to {content_type} {object_id}"
                + (f" (tagged item: {item_id})." if item_id else ".")
            )

        # remove
        # Accept legacy calls that passed the tagged-item ID via tag_id (the
        # pre-tagged_item_id interface) so older agent transcripts still work.
        item_id = tagged_item_id or tag_id
        if not item_id:
            return (
                "Error: 'tagged_item_id' (the tagged-item ID returned by "
                "action 'apply') is required for action 'remove'."
            )
        _resp, err = await _api(
            "delete",
            f"/v1/tagged_items/{item_id}",
            f"remove tagged item {item_id}",
        )
        if err:
            return err
        return f"Tagged item {item_id} removed."

    # ══════════════════════════════════════════════════════════════════════
    # 6. DETECTION RULES (unified mitigations)
    # ══════════════════════════════════════════════════════════════════════

    async def list_attackiq_detection_rules(
        search: str = "",
        page: int = 1,
        page_size: int = _DEFAULT_PAGE_SIZE,
    ) -> str:
        """List/search AttackIQ detection rules (unified mitigations)."""
        params: dict[str, Any] = {
            "page": _clamp_page(page),
            "page_size": _clamp_page_size(page_size),
        }
        if search:
            params["search"] = search
        resp, err = await _api(
            "get",
            "/v1/unified_mitigations",
            "list detection rules",
            params=params,
        )
        if err:
            return err

        def _fmt(m: dict) -> str:
            return (
                f"  {m.get('name', 'Unnamed')}\n"
                f"    id: {m.get('id', '?')} | "
                f"Type: {m.get('mitigation_type', m.get('type', 'N/A'))} | "
                f"Modified: {_dt(m.get('modified'))}"
            )

        body = format_entity_list(
            _results(resp), _fmt, noun="detection rule", total=_count(resp)
        )
        return body + _next_page_hint(resp, params["page"])

    async def get_attackiq_detection_rule(rule_id: str) -> str:
        """Get full detail of an AttackIQ detection rule (unified mitigation)."""
        resp, err = await _api(
            "get",
            f"/v1/unified_mitigations/{rule_id}",
            f"get detection rule {rule_id}",
        )
        if err:
            return err
        if not resp:
            return f"Detection rule {rule_id} not found."
        content = resp.get("content") or resp.get("rule") or resp.get("query")
        lines = [
            f"Detection rule: {resp.get('name', 'Unnamed')}",
            f"ID: {resp.get('id', '?')}",
            f"Type: {resp.get('mitigation_type', resp.get('type', 'N/A'))}",
            f"Description: {_trunc(resp.get('description'), 300) or 'None'}",
            f"Modified: {_dt(resp.get('modified'))}",
        ]
        if content:
            lines.append(f"Content:\n{_trunc(content, 800)}")
        return "\n".join(lines)

    # ══════════════════════════════════════════════════════════════════════
    # 7. CONNECTORS (platform integrations configured in AttackIQ)
    # ══════════════════════════════════════════════════════════════════════

    async def list_attackiq_connectors(
        page: int = 1,
        page_size: int = _DEFAULT_PAGE_SIZE,
    ) -> str:
        """List the connectors/integrations configured in the AttackIQ platform."""
        params = {
            "page": _clamp_page(page),
            "page_size": _clamp_page_size(page_size),
        }
        resp, err = await _api(
            "get", "/v1/company_connectors", "list connectors", params=params
        )
        if err:
            return err

        def _fmt(c: dict) -> str:
            return (
                f"  {c.get('name', 'Unnamed')} "
                f"[{c.get('connector_type', c.get('type', 'N/A'))}]\n"
                f"    id: {c.get('id', '?')} | "
                f"Enabled: {c.get('enabled', 'N/A')} | "
                f"Modified: {_dt(c.get('modified'))}"
            )

        body = format_entity_list(
            _results(resp), _fmt, noun="connector", total=_count(resp)
        )
        return body + _next_page_hint(resp, params["page"])

    async def get_attackiq_connector(connector_id: str) -> str:
        """Get full detail of an AttackIQ platform connector by ID."""
        resp, err = await _api(
            "get",
            f"/v1/company_connectors/{connector_id}",
            f"get connector {connector_id}",
        )
        if err:
            return err
        if not resp:
            return f"Connector {connector_id} not found."
        lines = [
            f"Connector: {resp.get('name', 'Unnamed')}",
            f"ID: {resp.get('id', '?')}",
            f"Type: {resp.get('connector_type', resp.get('type', 'N/A'))}",
            f"Enabled: {resp.get('enabled', 'N/A')}",
            f"Description: {_trunc(resp.get('description'), 300) or 'None'}",
            f"Modified: {_dt(resp.get('modified'))}",
        ]
        return "\n".join(lines)

    # ══════════════════════════════════════════════════════════════════════
    # REGISTER ALL TOOLS
    # ══════════════════════════════════════════════════════════════════════

    executor.register("list_attackiq_assessments", list_attackiq_assessments)
    executor.register("get_attackiq_assessment", get_attackiq_assessment)
    executor.register(
        "get_attackiq_assessment_status", get_attackiq_assessment_status
    )
    executor.register(
        "list_attackiq_assessment_runs", list_attackiq_assessment_runs
    )
    executor.register("run_attackiq_assessment", run_attackiq_assessment)
    executor.register("manage_attackiq_assessment", manage_attackiq_assessment)
    executor.register("list_attackiq_tests", list_attackiq_tests)
    executor.register("get_attackiq_test", get_attackiq_test)
    executor.register("get_attackiq_test_status", get_attackiq_test_status)
    executor.register("list_attackiq_scenarios", list_attackiq_scenarios)
    executor.register("get_attackiq_scenario", get_attackiq_scenario)
    executor.register("list_attackiq_results", list_attackiq_results)
    executor.register("get_attackiq_result", get_attackiq_result)
    executor.register("list_attackiq_phase_results", list_attackiq_phase_results)
    executor.register("list_attackiq_assets", list_attackiq_assets)
    executor.register("get_attackiq_asset", get_attackiq_asset)
    executor.register("list_attackiq_tags", list_attackiq_tags)
    executor.register("manage_attackiq_tag", manage_attackiq_tag)
    executor.register(
        "list_attackiq_detection_rules", list_attackiq_detection_rules
    )
    executor.register(
        "get_attackiq_detection_rule", get_attackiq_detection_rule
    )
    executor.register("list_attackiq_connectors", list_attackiq_connectors)
    executor.register("get_attackiq_connector", get_attackiq_connector)

    src = "integration:attackiq"
    return [
        # ── 0. Assessments ────────────────────────────────────────────────
        ToolDefinition(
            name="list_attackiq_assessments",
            description=(
                "List and search AttackIQ assessments (projects) - the "
                "top-level Breach & Attack Simulation exercises. Optional "
                "free-text search, page/page_size pagination, and ordering "
                "(e.g. -modified). Returns each assessment's ID, state, test "
                "and asset counts, owner, and last-modified time. Use the "
                "returned assessment ID with get_attackiq_assessment, "
                "run_attackiq_assessment, or list_attackiq_tests."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "search": {"type": "string"},
                    "page": {"type": "integer", "default": 1},
                    "page_size": {
                        "type": "integer",
                        "default": _DEFAULT_PAGE_SIZE,
                        "description": f"Max {_MAX_PAGE_SIZE}",
                    },
                    "ordering": {
                        "type": "string",
                        "default": "-modified",
                        "description": "e.g. -modified, name, -created",
                    },
                },
                "required": [],
            },
            source=src,
            priority=0,
        ),
        ToolDefinition(
            name="get_attackiq_assessment",
            description=(
                "Get full detail of one AttackIQ assessment by ID: state, "
                "version, description, template, test/asset counts, schedule, "
                "owner, and timestamps."
            ),
            input_schema={
                "type": "object",
                "properties": {"assessment_id": {"type": "string"}},
                "required": ["assessment_id"],
            },
            source=src,
            priority=0,
        ),
        ToolDefinition(
            name="get_attackiq_assessment_status",
            # Polled: the same call later answers a state that moved on its own.
            live_state=True,
            description=(
                "Check whether an AttackIQ assessment is currently running "
                "on-demand. Use after run_attackiq_assessment to poll for "
                "completion."
            ),
            input_schema={
                "type": "object",
                "properties": {"assessment_id": {"type": "string"}},
                "required": ["assessment_id"],
            },
            source=src,
            priority=0,
        ),
        ToolDefinition(
            name="list_attackiq_assessment_runs",
            description=(
                "List the historical runs of an AttackIQ assessment (each "
                "execution with its state and start/end times). Use a run's ID "
                "with list_attackiq_results to fetch that run's outcomes."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "assessment_id": {"type": "string"},
                    "page": {"type": "integer", "default": 1},
                    "page_size": {
                        "type": "integer",
                        "default": _DEFAULT_PAGE_SIZE,
                    },
                },
                "required": ["assessment_id"],
            },
            source=src,
            priority=0,
        ),
        ToolDefinition(
            name="run_attackiq_assessment",
            description=(
                "Run (launch) all tests in an AttackIQ assessment - the core "
                "'emulate the adversary' action that executes the assessment's "
                "scenarios against its assigned assets. Returns the started "
                "run's ID; it does NOT wait for completion (poll "
                "get_attackiq_assessment_status or list_attackiq_assessment_runs). "
                "Only runnable, activated assessments with active assets will "
                "execute."
            ),
            input_schema={
                "type": "object",
                "properties": {"assessment_id": {"type": "string"}},
                "required": ["assessment_id"],
            },
            source=src,
            priority=1,
        ),
        ToolDefinition(
            name="manage_attackiq_assessment",
            description=(
                "Manage the AttackIQ assessment lifecycle long tail via an "
                "action enum. action='activate' activates an assessment; "
                "action='create_from_template' creates a new assessment (needs "
                "name + template_id); action='update_defaults' assigns default "
                "assets/asset_groups/tags (comma-separated UUID lists); "
                "action='delete' PERMANENTLY deletes an assessment (cannot be "
                "undone). Prefer the dedicated run_attackiq_assessment tool to "
                "launch tests."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": sorted(_ASSESSMENT_ACTIONS),
                    },
                    "assessment_id": {"type": "string"},
                    "name": {
                        "type": "string",
                        "description": "New assessment name (create_from_template)",
                    },
                    "template_id": {
                        "type": "string",
                        "description": "Template UUID (create_from_template)",
                    },
                    "assets": {
                        "type": "string",
                        "description": "Comma-separated asset UUIDs (update_defaults)",
                    },
                    "asset_groups": {
                        "type": "string",
                        "description": "Comma-separated asset-group UUIDs (update_defaults)",
                    },
                    "tags": {
                        "type": "string",
                        "description": "Comma-separated tag UUIDs (update_defaults)",
                    },
                },
                "required": ["action"],
            },
            source=src,
            priority=2,
        ),
        # ── 1. Tests (master jobs) ────────────────────────────────────────
        ToolDefinition(
            name="list_attackiq_tests",
            description=(
                "List the tests (ScenarioMasterJobs / 'master jobs') inside an "
                "AttackIQ assessment. Each test bundles scenarios and assets. "
                "Returns test IDs, runnable flag, asset count, and last result."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "assessment_id": {"type": "string"},
                    "page": {"type": "integer", "default": 1},
                    "page_size": {
                        "type": "integer",
                        "default": _DEFAULT_PAGE_SIZE,
                    },
                },
                "required": ["assessment_id"],
            },
            source=src,
            priority=0,
        ),
        ToolDefinition(
            name="get_attackiq_test",
            description=(
                "Get full detail of one AttackIQ test (master job) by ID: "
                "assessment, description, runnable/cron, asset count, and the "
                "scenarios it runs."
            ),
            input_schema={
                "type": "object",
                "properties": {"test_id": {"type": "string"}},
                "required": ["test_id"],
            },
            source=src,
            priority=0,
        ),
        ToolDefinition(
            name="get_attackiq_test_status",
            # Polled: the same call later answers a state that moved on its own.
            live_state=True,
            description=(
                "Get the outcome counts for an AttackIQ test: total, finished, "
                "passed, failed, detected, and errored. Use to summarise how a "
                "test performed."
            ),
            input_schema={
                "type": "object",
                "properties": {"test_id": {"type": "string"}},
                "required": ["test_id"],
            },
            source=src,
            priority=0,
        ),
        # ── 2. Scenarios ──────────────────────────────────────────────────
        ToolDefinition(
            name="list_attackiq_scenarios",
            description=(
                "List and search AttackIQ scenarios - the attack content "
                "library (individual TTPs / adversary behaviours). Optional "
                "free-text search + pagination. Returns scenario IDs, "
                "multi-asset flag, and last-modified time; use the ID with "
                "get_attackiq_scenario for platform support and full detail."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "search": {"type": "string"},
                    "page": {"type": "integer", "default": 1},
                    "page_size": {
                        "type": "integer",
                        "default": _DEFAULT_PAGE_SIZE,
                    },
                },
                "required": [],
            },
            source=src,
            priority=0,
        ),
        ToolDefinition(
            name="get_attackiq_scenario",
            description=(
                "Get full detail of one AttackIQ scenario by ID: description, "
                "supported platforms, multi-asset/runnable flags, and "
                "timestamps."
            ),
            input_schema={
                "type": "object",
                "properties": {"scenario_id": {"type": "string"}},
                "required": ["scenario_id"],
            },
            source=src,
            priority=0,
        ),
        # ── 3. Results ────────────────────────────────────────────────────
        ToolDefinition(
            name="list_attackiq_results",
            description=(
                "List AttackIQ result summaries (per scenario+asset "
                "prevention/detection outcomes). Pass assessment_id to scope "
                "to one assessment (all runs) and/or run_id to scope to a "
                "specific run (from list_attackiq_assessment_runs); omit both "
                "for the tenant-wide recent results. Optional free-text "
                "search + pagination."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "assessment_id": {
                        "type": "string",
                        "description": "Scope to one assessment (all runs)",
                    },
                    "run_id": {
                        "type": "string",
                        "description": "Scope to a specific assessment run",
                    },
                    "search": {"type": "string"},
                    "page": {"type": "integer", "default": 1},
                    "page_size": {
                        "type": "integer",
                        "default": _DEFAULT_PAGE_SIZE,
                    },
                },
                "required": [],
            },
            source=src,
            priority=0,
        ),
        ToolDefinition(
            name="get_attackiq_result",
            description=(
                "Get detail of a single AttackIQ result by ID: scenario, "
                "asset, prevention/detection outcome, and job state."
            ),
            input_schema={
                "type": "object",
                "properties": {"result_id": {"type": "string"}},
                "required": ["result_id"],
            },
            source=src,
            priority=0,
        ),
        ToolDefinition(
            name="list_attackiq_phase_results",
            description=(
                "List per-phase results for an AttackIQ assessment (the "
                "phase-by-phase breakdown within an assessment run). Pass "
                "run_id to scope to a specific run."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "assessment_id": {"type": "string"},
                    "run_id": {"type": "string"},
                    "page": {"type": "integer", "default": 1},
                    "page_size": {
                        "type": "integer",
                        "default": _DEFAULT_PAGE_SIZE,
                    },
                },
                "required": ["assessment_id"],
            },
            source=src,
            priority=0,
        ),
        # ── 4. Assets ─────────────────────────────────────────────────────
        ToolDefinition(
            name="list_attackiq_assets",
            description=(
                "List AttackIQ assets - the on-prem endpoints / test points "
                "where scenarios execute. Pass assessment_id to list the "
                "assets assigned to that assessment's tests; omit it for all "
                "tenant assets. Returns hostname, status, IP, OS, and user."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "assessment_id": {
                        "type": "string",
                        "description": "Optional: scope to one assessment",
                    },
                    "page": {"type": "integer", "default": 1},
                    "page_size": {
                        "type": "integer",
                        "default": _DEFAULT_PAGE_SIZE,
                    },
                },
                "required": [],
            },
            source=src,
            priority=0,
        ),
        ToolDefinition(
            name="get_attackiq_asset",
            description=(
                "Get full detail of one AttackIQ asset (test point) by ID: "
                "hostname, status, IPs, MAC, OS/arch, domain, user, and asset "
                "groups."
            ),
            input_schema={
                "type": "object",
                "properties": {"asset_id": {"type": "string"}},
                "required": ["asset_id"],
            },
            source=src,
            priority=0,
        ),
        # ── 5. Tags ───────────────────────────────────────────────────────
        ToolDefinition(
            name="list_attackiq_tags",
            description=(
                "List and search AttackIQ tags (labels used to organise "
                "scenarios, assessments, and assets). Returns tag IDs, display "
                "names, and their tag set."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "search": {"type": "string"},
                    "page": {"type": "integer", "default": 1},
                    "page_size": {
                        "type": "integer",
                        "default": _DEFAULT_PAGE_SIZE,
                    },
                },
                "required": [],
            },
            source=src,
            priority=0,
        ),
        ToolDefinition(
            name="manage_attackiq_tag",
            description=(
                "Manage AttackIQ tags and tag assignments via an action enum. "
                "action='create' creates a tag (needs name + tag_set UUID); "
                "action='delete' deletes a tag by tag_id (destructive); "
                "action='apply' applies a tag to an object (needs tag_id + "
                "content_type [asset|project|scenario|...] + object_id) and "
                "returns a tagged-item ID; action='remove' removes a tag from "
                "an object by deleting that tagged item (needs "
                "tagged_item_id; destructive)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": sorted(_TAG_ACTIONS),
                    },
                    "name": {"type": "string"},
                    "tag_set": {
                        "type": "string",
                        "description": "Tag set UUID (create)",
                    },
                    "display_name": {"type": "string"},
                    "tag_id": {
                        "type": "string",
                        "description": "Tag UUID (delete/apply)",
                    },
                    "content_type": {
                        "type": "string",
                        "description": "e.g. asset, project, scenario (apply)",
                    },
                    "object_id": {
                        "type": "string",
                        "description": "UUID of the object to tag (apply)",
                    },
                    "tagged_item_id": {
                        "type": "string",
                        "description": (
                            "Tagged-item ID returned by action 'apply' (remove)"
                        ),
                    },
                },
                "required": ["action"],
            },
            source=src,
            priority=2,
        ),
        # ── 6. Detection rules ────────────────────────────────────────────
        ToolDefinition(
            name="list_attackiq_detection_rules",
            description=(
                "List and search AttackIQ detection rules (unified "
                "mitigations) - the SIEM/EDR detection content (Sigma, YARA, "
                "SPL, KQL, ...) AttackIQ tracks and validates. Returns rule "
                "IDs, type, and last-modified time."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "search": {"type": "string"},
                    "page": {"type": "integer", "default": 1},
                    "page_size": {
                        "type": "integer",
                        "default": _DEFAULT_PAGE_SIZE,
                    },
                },
                "required": [],
            },
            source=src,
            priority=0,
        ),
        ToolDefinition(
            name="get_attackiq_detection_rule",
            description=(
                "Get full detail of one AttackIQ detection rule (unified "
                "mitigation) by ID: type, description, and rule content/query."
            ),
            input_schema={
                "type": "object",
                "properties": {"rule_id": {"type": "string"}},
                "required": ["rule_id"],
            },
            source=src,
            priority=0,
        ),
        # ── 7. Connectors ─────────────────────────────────────────────────
        ToolDefinition(
            name="list_attackiq_connectors",
            description=(
                "List the connectors/integrations configured in the AttackIQ "
                "platform (e.g. SIEM/EDR integrations used for detection "
                "validation). Returns connector IDs, type, and enabled state."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "page": {"type": "integer", "default": 1},
                    "page_size": {
                        "type": "integer",
                        "default": _DEFAULT_PAGE_SIZE,
                    },
                },
                "required": [],
            },
            source=src,
            priority=0,
        ),
        ToolDefinition(
            name="get_attackiq_connector",
            description=(
                "Get full detail of one AttackIQ platform connector by ID: "
                "type, enabled state, and description."
            ),
            input_schema={
                "type": "object",
                "properties": {"connector_id": {"type": "string"}},
                "required": ["connector_id"],
            },
            source=src,
            priority=0,
        ),
    ]
