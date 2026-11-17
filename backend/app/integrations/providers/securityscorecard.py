# SPDX-License-Identifier: Apache-2.0

"""SecurityScorecard integration - external attack surface and third-party ratings.

An organisation's security rating seen from the outside, the factors behind
it and the issues (findings) each factor counts, plus the portfolios of
vendors an exposure-management team follows:

  - Scorecards: a company's score and grade, its ten factors with the issue
    types each one counts, the findings of one issue type, score and factor
    history, an industry's average score.
  - Portfolios: list them and their companies, create / delete one, add or
    remove a company.
  - Reports: list the recent ones, generate one (it is built asynchronously
    and then listed with a download link).

Authentication is a single API token sent as ``Authorization: Token <token>``
(no OAuth). Every call targets the fixed host ``https://api.securityscorecard.io``;
every value that lands in a URL path (domain, portfolio id, issue type,
industry, report type) is matched against a strict pattern first.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

import httpx

from app.core.egress import egress_client
from app.integrations.http import (
    TRANSIENT_RETRY_STATUSES,
    clip_text,
    format_entity_list,
    safe_request,
)
from app.tools.base import CallableToolExecutor, ToolDefinition

_PROVIDER = "SecurityScorecard"
_SOURCE = "integration:securityscorecard"

SECURITYSCORECARD_API = "https://api.securityscorecard.io"

_TIMEOUT = 30
_DEFAULT_LIMIT = 25
_MAX_LIMIT = 100

# A domain is a scorecard's identifier and is put in the path: hostname
# labels only, so it can carry no ``/``, ``?``, ``#`` or ``..``.
_DOMAIN_RE = re.compile(
    r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,62}"
)
_PORTFOLIO_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
_SLUG_RE = re.compile(r"[a-z0-9][a-z0-9_.-]{0,99}")

#: Company report types ``POST /reports/{type}`` builds from a scorecard.
REPORT_TYPES = (
    "summary",
    "full-scorecard-json",
    "issues",
    "detailed",
    "events-json",
    "scorecard-footprint",
)

_PORTFOLIO_PRIVACY = ("private", "shared", "team")


def build_securityscorecard_headers(api_token: str) -> dict[str, str]:
    """The one header builder, shared with the connectivity test."""
    return {
        "Authorization": f"Token {(api_token or '').strip()}",
        "Accept": "application/json",
    }


def normalize_domain(value: Any) -> str | None:
    """*value* as a bare lower-case domain, or ``None`` when it is not one.

    A pasted URL (``https://www.example.com/path``) is reduced to its host,
    which is what SecurityScorecard keys a scorecard by.
    """
    text = str(value or "").strip().lower()
    if "://" in text:
        text = text.split("://", 1)[1]
    text = text.split("/", 1)[0].rstrip(".")
    return text if _DOMAIN_RE.fullmatch(text) else None


def _clean(value: Any, limit: int = 200) -> str:
    """A vendor string on one line, cut at *limit*."""
    text = " ".join(str(value if value is not None else "").split())
    return clip_text(text, limit) if text else "-"


def _json(resp: httpx.Response) -> dict[str, Any]:
    try:
        data = resp.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _entries(data: dict[str, Any]) -> list[dict[str, Any]]:
    return [e for e in (data.get("entries") or []) if isinstance(e, dict)]


def _bounded(value: Any, default: int, ceiling: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(n, ceiling))


def _limit(value: Any) -> int:
    return _bounded(value, _DEFAULT_LIMIT, _MAX_LIMIT)


def _format_company(data: dict[str, Any]) -> str:
    change = data.get("last30day_score_change")
    lines = [
        f"{_clean(data.get('name'))} ({_clean(data.get('domain'))})",
        f"  Score: {data.get('score', '?')} | Grade: {_clean(data.get('grade'))}"
        + (f" | 30-day change: {change:+}" if isinstance(change, int | float) else ""),
        f"  Industry: {_clean(data.get('industry'))} | Size: {_clean(data.get('size'))}",
    ]
    if data.get("disputed"):
        lines.append("  The scorecard is disputed.")
    return "\n".join(lines)


def _format_factor(factor: dict[str, Any]) -> str:
    lines = [
        f"- {_clean(factor.get('name'))}: score {factor.get('score', '?')} "
        f"(grade {_clean(factor.get('grade'))})"
    ]
    for issue in factor.get("issue_summary") or []:
        if not isinstance(issue, dict):
            continue
        impact = issue.get("total_score_impact")
        lines.append(
            f"    {_clean(issue.get('type'), 100)}: {issue.get('count', '?')} finding(s), "
            f"severity {_clean(issue.get('severity'), 20)}"
            + (f", score impact {impact}" if impact is not None else "")
        )
    return "\n".join(lines)


# The fields a finding is shown with, in order, when the issue type has them:
# the shape of a finding changes with its type.
_FINDING_FIELDS = (
    "hostname",
    "domain",
    "subdomain",
    "ip_address",
    "port",
    "protocol",
    "url",
    "product",
    "version",
    "cve",
    "severity",
    "first_seen_time",
    "last_seen_time",
    "status",
    "issue_id",
)


def _format_finding(entry: dict[str, Any]) -> str:
    parts = [
        f"{name}: {_clean(entry[name], 120)}"
        for name in _FINDING_FIELDS
        if entry.get(name) not in (None, "", [])
    ]
    return "- " + (" | ".join(parts) if parts else "(no detail)")


def build_tools(
    token: str, executor: CallableToolExecutor, **_kwargs: Any
) -> list[ToolDefinition]:
    api_token = (token or "").strip()
    if not api_token:
        return []
    headers = build_securityscorecard_headers(api_token)

    def _client() -> httpx.AsyncClient:
        return egress_client(timeout=_TIMEOUT)

    async def _get(path: str, action: str, params: dict | None = None):
        async with _client() as client:
            return await safe_request(
                client,
                "get",
                f"{SECURITYSCORECARD_API}{path}",
                _PROVIDER,
                action,
                headers=headers,
                params=params,
                retry_statuses=TRANSIENT_RETRY_STATUSES,
            )

    async def _send(method: str, path: str, action: str, body: dict | None = None):
        async with _client() as client:
            return await safe_request(
                client,
                method,
                f"{SECURITYSCORECARD_API}{path}",
                _PROVIDER,
                action,
                headers=headers,
                json=body,
            )

    def _domain_error(domain: Any) -> str:
        return f"Error: '{_clean(domain, 100)}' is not a domain (e.g. example.com)."

    # -- Scorecards -----------------------------------------------------

    async def get_securityscorecard_company(domain: str) -> str:
        d = normalize_domain(domain)
        if not d:
            return _domain_error(domain)
        resp, err = await _get(f"/companies/{d}", f"get the scorecard of {d}")
        if err:
            return err
        return _format_company(_json(resp))

    async def list_securityscorecard_factors(domain: str) -> str:
        d = normalize_domain(domain)
        if not d:
            return _domain_error(domain)
        resp, err = await _get(f"/companies/{d}/factors", f"list the factors of {d}")
        if err:
            return err
        factors = _entries(_json(resp))
        if not factors:
            return f"No factors found for {d}."
        body = "\n".join(_format_factor(f) for f in factors)
        return (
            f"Factors of {d} (issue types are listed under their factor; read the "
            f"findings with list_securityscorecard_issues):\n{body}"
        )

    async def list_securityscorecard_issues(
        domain: str, issue_type: str, limit: int = _DEFAULT_LIMIT
    ) -> str:
        d = normalize_domain(domain)
        if not d:
            return _domain_error(domain)
        kind = str(issue_type or "").strip()
        if not _SLUG_RE.fullmatch(kind):
            return (
                "Error: issue_type must be an issue type key as listed by "
                "list_securityscorecard_factors (e.g. patching_cadence_high)."
            )
        resp, err = await _get(
            f"/companies/{d}/issues/{kind}", f"list the {kind} findings of {d}"
        )
        if err:
            return err
        findings = _entries(_json(resp))
        n = _limit(limit)
        out = format_entity_list(
            findings[:n],
            _format_finding,
            noun="finding",
            total=len(findings),
            empty_msg=f"No {kind} findings for {d}.",
        )
        if len(findings) > n:
            out += f"\n[{len(findings) - n} more not shown: raise limit (max {_MAX_LIMIT}).]"
        return out

    async def get_securityscorecard_score_history(domain: str, days: int = 90) -> str:
        d = normalize_domain(domain)
        if not d:
            return _domain_error(domain)
        resp, err = await _get(f"/companies/{d}/history/score", f"read the score history of {d}")
        if err:
            return err
        entries = _entries(_json(resp))
        span = _bounded(days, 90, 365)
        entries = entries[-span:]
        if not entries:
            return f"No score history for {d}."
        lines = [f"- {_clean(e.get('date'), 30)}: {e.get('score', '?')}" for e in entries]
        return f"Daily score of {d} ({len(lines)} day(s)):\n" + "\n".join(lines)

    async def get_securityscorecard_factor_history(domain: str, days: int = 30) -> str:
        d = normalize_domain(domain)
        if not d:
            return _domain_error(domain)
        resp, err = await _get(
            f"/companies/{d}/history/factors/score", f"read the factor history of {d}"
        )
        if err:
            return err
        entries = _entries(_json(resp))
        span = _bounded(days, 30, 365)
        entries = entries[-span:]
        if not entries:
            return f"No factor history for {d}."
        lines = []
        for e in entries:
            factors = ", ".join(
                f"{_clean(f.get('name'), 60)} {f.get('score', '?')}"
                for f in e.get("factors") or []
                if isinstance(f, dict)
            )
            lines.append(f"- {_clean(e.get('date'), 30)}: {factors or '-'}")
        return f"Factor scores of {d} ({len(lines)} day(s)):\n" + "\n".join(lines)

    async def get_securityscorecard_industry_score(industry: str) -> str:
        key = str(industry or "").strip().lower()
        if not _SLUG_RE.fullmatch(key):
            return "Error: industry must be an industry key (e.g. technology, financial_services)."
        resp, err = await _get(f"/industries/{key}/score", f"read the {key} industry score")
        if err:
            return err
        data = _json(resp)
        return f"Average score of the {key} industry: {data.get('score', '?')}"

    # -- Portfolios -------------------------------------------------------

    async def list_securityscorecard_portfolios() -> str:
        resp, err = await _get("/portfolios", "list portfolios")
        if err:
            return err
        portfolios = _entries(_json(resp))
        return format_entity_list(
            portfolios,
            lambda p: (
                f"- {_clean(p.get('name'))} (id: {_clean(p.get('id'), 64)}, "
                f"privacy: {_clean(p.get('privacy'), 20)}"
                + (", read-only" if p.get("read_only") else "")
                + ")"
            ),
            noun="portfolio",
        )

    async def list_securityscorecard_portfolio_companies(
        portfolio_id: str, limit: int = _DEFAULT_LIMIT
    ) -> str:
        pid = str(portfolio_id or "").strip()
        if not _PORTFOLIO_ID_RE.fullmatch(pid):
            return "Error: portfolio_id must be a portfolio id from list_securityscorecard_portfolios."
        resp, err = await _get(f"/portfolios/{pid}/companies", f"list the companies of portfolio {pid}")
        if err:
            return err
        companies = _entries(_json(resp))
        if not companies:
            return f"Portfolio {pid} has no companies."
        n = _limit(limit)
        lines = [
            f"- {_clean(c.get('name'))} ({_clean(c.get('domain'), 100)}): "
            f"score {c.get('score', '?')}, grade {_clean(c.get('grade'), 5)}, "
            f"industry {_clean(c.get('industry'), 60)}"
            for c in companies[:n]
        ]
        out = f"Portfolio {pid} has {len(companies)} company(ies):\n" + "\n".join(lines)
        if len(companies) > n:
            out += f"\n[{len(companies) - n} more not shown: raise limit (max {_MAX_LIMIT}).]"
        return out

    async def create_securityscorecard_portfolio(
        name: str, description: str = "", privacy: str = "private"
    ) -> str:
        title = " ".join(str(name or "").split())
        if not title:
            return "Error: name is required."
        if privacy not in _PORTFOLIO_PRIVACY:
            return f"Error: privacy must be one of {', '.join(_PORTFOLIO_PRIVACY)}."
        body: dict[str, Any] = {"name": title[:200], "privacy": privacy}
        if description:
            body["description"] = str(description)[:2000]
        resp, err = await _send("post", "/portfolios", f"create portfolio {title[:60]!r}", body)
        if err:
            return err
        data = _json(resp)
        return f"Created portfolio {_clean(data.get('name') or title)} (id: {_clean(data.get('id'), 64)})."

    async def delete_securityscorecard_portfolio(portfolio_id: str) -> str:
        pid = str(portfolio_id or "").strip()
        if not _PORTFOLIO_ID_RE.fullmatch(pid):
            return "Error: portfolio_id must be a portfolio id from list_securityscorecard_portfolios."
        _resp, err = await _send("delete", f"/portfolios/{pid}", f"delete portfolio {pid}")
        return err or f"Deleted portfolio {pid}."

    async def add_securityscorecard_portfolio_company(portfolio_id: str, domain: str) -> str:
        pid = str(portfolio_id or "").strip()
        if not _PORTFOLIO_ID_RE.fullmatch(pid):
            return "Error: portfolio_id must be a portfolio id from list_securityscorecard_portfolios."
        d = normalize_domain(domain)
        if not d:
            return _domain_error(domain)
        _resp, err = await _send(
            "put", f"/portfolios/{pid}/companies/{d}", f"add {d} to portfolio {pid}"
        )
        return err or f"Added {d} to portfolio {pid}."

    async def remove_securityscorecard_portfolio_company(portfolio_id: str, domain: str) -> str:
        pid = str(portfolio_id or "").strip()
        if not _PORTFOLIO_ID_RE.fullmatch(pid):
            return "Error: portfolio_id must be a portfolio id from list_securityscorecard_portfolios."
        d = normalize_domain(domain)
        if not d:
            return _domain_error(domain)
        _resp, err = await _send(
            "delete", f"/portfolios/{pid}/companies/{d}", f"remove {d} from portfolio {pid}"
        )
        return err or f"Removed {d} from portfolio {pid}."

    # -- Reports ----------------------------------------------------------

    async def list_securityscorecard_reports(limit: int = _DEFAULT_LIMIT) -> str:
        resp, err = await _get("/reports/recent", "list recent reports")
        if err:
            return err
        reports = _entries(_json(resp))
        n = _limit(limit)
        return format_entity_list(
            reports[:n],
            lambda r: (
                f"- {_clean(r.get('report_type'), 60)} (id: {_clean(r.get('id'), 64)}): "
                f"status {_clean(r.get('status'), 30)}"
                + (f", completed {_clean(r.get('completed_at'), 40)}" if r.get("completed_at") else "")
                + (f", download: {_clean(r.get('download_url'), 500)}" if r.get("download_url") else "")
            ),
            noun="report",
            total=len(reports),
        )

    async def generate_securityscorecard_report(domain: str, report_type: str = "summary") -> str:
        d = normalize_domain(domain)
        if not d:
            return _domain_error(domain)
        if report_type not in REPORT_TYPES:
            return f"Error: report_type must be one of {', '.join(REPORT_TYPES)}."
        resp, err = await _send(
            "post",
            f"/reports/{quote(report_type)}",
            f"generate a {report_type} report for {d}",
            {"scorecard_identifier": d},
        )
        if err:
            return err
        data = _json(resp)
        return (
            f"Requested a {report_type} report for {d} (id: {_clean(data.get('id'), 64)}). "
            "It is built in the background: list_securityscorecard_reports shows it "
            "with its download link once completed."
        )

    handlers = {
        "get_securityscorecard_company": get_securityscorecard_company,
        "list_securityscorecard_factors": list_securityscorecard_factors,
        "list_securityscorecard_issues": list_securityscorecard_issues,
        "get_securityscorecard_score_history": get_securityscorecard_score_history,
        "get_securityscorecard_factor_history": get_securityscorecard_factor_history,
        "get_securityscorecard_industry_score": get_securityscorecard_industry_score,
        "list_securityscorecard_portfolios": list_securityscorecard_portfolios,
        "list_securityscorecard_portfolio_companies": list_securityscorecard_portfolio_companies,
        "create_securityscorecard_portfolio": create_securityscorecard_portfolio,
        "delete_securityscorecard_portfolio": delete_securityscorecard_portfolio,
        "add_securityscorecard_portfolio_company": add_securityscorecard_portfolio_company,
        "remove_securityscorecard_portfolio_company": remove_securityscorecard_portfolio_company,
        "list_securityscorecard_reports": list_securityscorecard_reports,
        "generate_securityscorecard_report": generate_securityscorecard_report,
    }
    for name, fn in handlers.items():
        executor.register(name, fn)

    domain_prop = {
        "type": "string",
        "description": "The company's domain, its scorecard identifier (e.g. example.com)",
    }
    portfolio_prop = {
        "type": "string",
        "description": "Portfolio id from list_securityscorecard_portfolios",
    }
    limit_prop = {
        "type": "integer",
        "description": f"Most items returned (default {_DEFAULT_LIMIT}, max {_MAX_LIMIT})",
    }

    def _tool(name: str, description: str, properties: dict, required: list[str]) -> ToolDefinition:
        return ToolDefinition(
            name=name,
            description=description,
            input_schema={"type": "object", "properties": properties, "required": required},
            source=_SOURCE,
        )

    return [
        _tool(
            "get_securityscorecard_company",
            "Read a company's SecurityScorecard rating: score (0-100), grade (A-F), "
            "30-day change, industry and size. Works for your own organisation and "
            "for any vendor or third party by domain.",
            {"domain": domain_prop},
            ["domain"],
        ),
        _tool(
            "list_securityscorecard_factors",
            "List the ten risk factors of a company's scorecard (network security, "
            "DNS health, patching cadence, endpoint security, IP reputation, "
            "application security, cubit score, hacker chatter, information leak, "
            "social engineering) with each factor's score and the issue types it "
            "counts (finding count, severity, score impact). Start here to see what "
            "drags a rating down, then read the findings with list_securityscorecard_issues.",
            {"domain": domain_prop},
            ["domain"],
        ),
        _tool(
            "list_securityscorecard_issues",
            "List the findings of one issue type on a company's scorecard: the "
            "exposed hosts, IPs, ports, products, CVEs and when each was first and "
            "last seen. issue_type is a key from list_securityscorecard_factors.",
            {
                "domain": domain_prop,
                "issue_type": {
                    "type": "string",
                    "description": "Issue type key (e.g. patching_cadence_high, tlscert_expired)",
                },
                "limit": limit_prop,
            },
            ["domain", "issue_type"],
        ),
        _tool(
            "get_securityscorecard_score_history",
            "Read a company's daily overall score over the last days (default 90, max 365).",
            {"domain": domain_prop, "days": {"type": "integer", "description": "Days back (max 365)"}},
            ["domain"],
        ),
        _tool(
            "get_securityscorecard_factor_history",
            "Read a company's daily factor scores over the last days (default 30, max 365).",
            {"domain": domain_prop, "days": {"type": "integer", "description": "Days back (max 365)"}},
            ["domain"],
        ),
        _tool(
            "get_securityscorecard_industry_score",
            "Read the average SecurityScorecard score of an industry, to compare a company with its peers.",
            {
                "industry": {
                    "type": "string",
                    "description": "Industry key as a scorecard names it (e.g. technology, financial_services)",
                }
            },
            ["industry"],
        ),
        _tool(
            "list_securityscorecard_portfolios",
            "List the SecurityScorecard portfolios (vendor or subsidiary groups) the token can see.",
            {},
            [],
        ),
        _tool(
            "list_securityscorecard_portfolio_companies",
            "List the companies of a portfolio with their score, grade and industry: "
            "the third parties a vendor-risk review covers.",
            {"portfolio_id": portfolio_prop, "limit": limit_prop},
            ["portfolio_id"],
        ),
        _tool(
            "create_securityscorecard_portfolio",
            "Create a SecurityScorecard portfolio.",
            {
                "name": {"type": "string", "description": "Portfolio name"},
                "description": {"type": "string", "description": "Optional description"},
                "privacy": {
                    "type": "string",
                    "enum": list(_PORTFOLIO_PRIVACY),
                    "description": "Who sees it (default private)",
                },
            },
            ["name"],
        ),
        _tool(
            "delete_securityscorecard_portfolio",
            "Delete a SecurityScorecard portfolio (the companies' scorecards stay).",
            {"portfolio_id": portfolio_prop},
            ["portfolio_id"],
        ),
        _tool(
            "add_securityscorecard_portfolio_company",
            "Add a company to a portfolio by domain, so its rating is followed with the others.",
            {"portfolio_id": portfolio_prop, "domain": domain_prop},
            ["portfolio_id", "domain"],
        ),
        _tool(
            "remove_securityscorecard_portfolio_company",
            "Remove a company from a portfolio.",
            {"portfolio_id": portfolio_prop, "domain": domain_prop},
            ["portfolio_id", "domain"],
        ),
        _tool(
            "list_securityscorecard_reports",
            "List the recently generated reports with their status and download link.",
            {"limit": limit_prop},
            [],
        ),
        _tool(
            "generate_securityscorecard_report",
            "Ask SecurityScorecard to build a report on a company's scorecard "
            "(summary PDF, full JSON, issues CSV, detailed, events JSON or footprint). "
            "It is built in the background; list_securityscorecard_reports gives its link.",
            {
                "domain": domain_prop,
                "report_type": {
                    "type": "string",
                    "enum": list(REPORT_TYPES),
                    "description": "Report type (default summary)",
                },
            },
            ["domain"],
        ),
    ]
