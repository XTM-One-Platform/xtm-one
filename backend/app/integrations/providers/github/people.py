"""GitHub people tools - delivery metrics, AI usage and identities per login.

Three reads an engineering review needs and no other tool answers in one call:

* ``get_github_engineering_metrics``: per login and per period bucket, the
  pull requests opened, merged and closed unmerged, the median lead time and
  size of the merged ones, the reviews they received, the pull requests the
  login reviewed for others, and the merged count with backports collapsed by
  the issue they close. GraphQL ``search`` counts in batches of aliases, and
  one paged search of merged pull requests per login and year.
* ``get_github_ai_usage``: the organization's Copilot seats with their last
  activity, its per-user Copilot usage report when the token may read it, and
  the AI co-author trailers (``Co-authored-by: Claude ...``) in each login's
  commits over a period.
* ``lookup_github_identities``: employee email to GitHub login and back, from
  the organization's SAML external identities, else the members' verified
  domain emails, else the public profile email; each row names its source.

All three only read.
"""

from __future__ import annotations

import asyncio
import json
import re
import statistics
from datetime import UTC, date, datetime, timedelta
from typing import Any

from app.core.egress import egress_client
from app.tools.base import CallableToolExecutor, ToolDefinition

from . import _common
from ._common import _BASE_URL, _PROVIDER, _coerce_str, login_error, parse_repo

_SOURCE = "integration:github"

#: Most logins and buckets one metrics call reads (10 logins x 3 years by
#: quarter is 120 cells, well inside).
_MAX_LOGINS = 20
_MAX_BUCKETS = 60
#: Search aliases per GraphQL request, and requests in flight at once.
_ALIASES_PER_QUERY = 30
_CONCURRENCY = 4
#: GitHub search answers at most 1,000 results per query: merged pull
#: requests are read one year at a time, 100 per page.
_MERGED_PAGES_PER_YEAR = 10
_AI_TRAILER_PAGES = 5

_COPILOT_HINT = (
    "the token needs the 'manage_billing:copilot' or 'read:org' scope (GitHub App: "
    "'Organization -> Administration: Read' or 'GitHub Copilot Business: Read'), and the "
    "organization must allow the Copilot metrics API."
)
_IDENTITY_HINT = (
    "SAML identities need an organization owner's token with 'admin:org' (or 'read:org' for verified domain emails)."
)

#: AI coding agents recognised in a ``Co-authored-by`` trailer: name, and the
#: words of the trailer's name or email that identify it.
_AI_AGENTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Claude", ("claude", "anthropic")),
    ("Copilot", ("copilot",)),
    ("Codex", ("codex", "openai")),
    ("Cursor", ("cursor",)),
    ("Gemini", ("gemini",)),
    ("Devin", ("devin",)),
    ("Aider", ("aider",)),
)
_TRAILER_RE = re.compile(r"(?im)^\s*co-authored-by:\s*(.+)$")


# ---------------------------------------------------------------------------
# Period buckets
# ---------------------------------------------------------------------------


def _parse_day(value: str, field: str) -> tuple[date | None, str | None]:
    raw = _coerce_str(value).strip()
    if not raw:
        return None, None
    try:
        return date.fromisoformat(raw[:10]), None
    except ValueError:
        return None, f"Error: {field} must be an ISO date (YYYY-MM-DD)."


def _bucket_start(day: date, bucket: str) -> date:
    if bucket == "year":
        return date(day.year, 1, 1)
    if bucket == "quarter":
        return date(day.year, 3 * ((day.month - 1) // 3) + 1, 1)
    return date(day.year, day.month, 1)


def _next_bucket(start: date, bucket: str) -> date:
    months = {"month": 1, "quarter": 3, "year": 12}[bucket]
    month = start.month - 1 + months
    return date(start.year + month // 12, month % 12 + 1, 1)


def _bucket_label(start: date, bucket: str) -> str:
    if bucket == "year":
        return str(start.year)
    if bucket == "quarter":
        return f"{start.year}-Q{(start.month - 1) // 3 + 1}"
    return f"{start.year}-{start.month:02d}"


def period_buckets(since: date, until: date, bucket: str) -> list[tuple[str, date, date]]:
    """``(label, first day, last day)`` of each bucket from *since* to *until*."""
    out: list[tuple[str, date, date]] = []
    start = _bucket_start(since, bucket)
    while start <= until:
        nxt = _next_bucket(start, bucket)
        out.append((_bucket_label(start, bucket), max(start, since), min(nxt - timedelta(days=1), until)))
        start = nxt
    return out


def _where(org: str, repo: str) -> str:
    return f"repo:{repo}" if repo else f"org:{org}"


def _logins_error(logins: Any) -> tuple[list[str], str | None]:
    if isinstance(logins, str):
        logins = [part for part in re.split(r"[\s,]+", logins) if part]
    if not isinstance(logins, list) or not logins:
        return [], "Error: 'logins' is required: a list of GitHub logins."
    clean: list[str] = []
    for login in logins:
        if err := login_error(login, "login"):
            return [], err
        value = _coerce_str(login).strip()
        if value.lower() not in {c.lower() for c in clean}:
            clean.append(value)
    if len(clean) > _MAX_LOGINS:
        return [], f"Error: at most {_MAX_LOGINS} logins per call."
    return clean, None


def _scope_error(org: str, repo: str) -> tuple[str, str, str | None]:
    org = _coerce_str(org).strip()
    repo = _coerce_str(repo).strip()
    if bool(org) == bool(repo):
        return "", "", "Error: give exactly one of 'org' (organization login) or 'repo' (owner/name)."
    if repo:
        owner, name, err = parse_repo(repo)
        if err:
            return "", "", err
        return "", f"{owner}/{name}", None
    if err := login_error(org, "org"):
        return "", "", err
    return org, "", None


def _median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def collapse_backports(prs: list[dict[str, Any]]) -> int:
    """Merged pull requests counted once per closed issue.

    A pull request that closes an issue another one of the list already
    closed (a backport of the same fix to a release branch) is not counted
    again; one closing no issue always counts.
    """
    seen: set[int] = set()
    count = 0
    for pr in sorted(prs, key=lambda p: p.get("mergedAt") or ""):
        issues = {n for n in pr.get("issues") or [] if isinstance(n, int)}
        if issues and issues <= seen:
            continue
        seen |= issues
        count += 1
    return count


# ---------------------------------------------------------------------------
# GraphQL search helpers
# ---------------------------------------------------------------------------


async def _search_counts(
    client: Any, headers: dict[str, str], queries: dict[str, str], semaphore: asyncio.Semaphore
) -> tuple[dict[str, int], list[str]]:
    """``issueCount`` of each search query, by key, batched as GraphQL aliases."""
    keys = list(queries)
    counts: dict[str, int] = {}
    errors: list[str] = []

    async def run(chunk: list[str]) -> None:
        aliases = {f"q{i}": key for i, key in enumerate(chunk)}
        variables = {f"v{i}": queries[key] for i, key in enumerate(chunk)}
        decl = ",".join(f"$v{i}:String!" for i in range(len(chunk)))
        body = " ".join(f"q{i}:search(query:$v{i},type:ISSUE,first:0){{issueCount}}" for i in range(len(chunk)))
        async with semaphore:
            data, err = await _common.gh_graphql(
                client, headers, f"query({decl}){{{body}}}", variables, action="count pull requests"
            )
        if err or not data:
            errors.append(err or "Error: empty search answer.")
            return
        for alias, key in aliases.items():
            node = data.get(alias) or {}
            if isinstance(node.get("issueCount"), int):
                counts[key] = node["issueCount"]

    chunks = [keys[i : i + _ALIASES_PER_QUERY] for i in range(0, len(keys), _ALIASES_PER_QUERY)]
    await asyncio.gather(*(run(chunk) for chunk in chunks))
    return counts, errors


_MERGED_QUERY = (
    "query($q:String!,$after:String){search(query:$q,type:ISSUE,first:100,after:$after){"
    "issueCount pageInfo{hasNextPage endCursor} nodes{... on PullRequest{number createdAt mergedAt "
    "additions deletions reviews{totalCount} closingIssuesReferences(first:5){nodes{number}}}}}}"
)


async def _merged_prs(
    client: Any,
    headers: dict[str, str],
    login: str,
    where: str,
    since: date,
    until: date,
    semaphore: asyncio.Semaphore,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Every pull request *login* merged in *where* between two days."""
    prs: list[dict[str, Any]] = []
    errors: list[str] = []
    start = since
    while start <= until:
        end = min(date(start.year, 12, 31), until)
        query = f"is:pr is:merged author:{login} {where} merged:{start.isoformat()}..{end.isoformat()}"
        after: str | None = None
        for _ in range(_MERGED_PAGES_PER_YEAR):
            async with semaphore:
                data, err = await _common.gh_graphql(
                    client, headers, _MERGED_QUERY, {"q": query, "after": after}, action=f"read merged PRs of {login}"
                )
            if err or not data:
                errors.append(err or "Error: empty search answer.")
                break
            search = data.get("search") or {}
            for node in search.get("nodes") or []:
                if not isinstance(node, dict) or not node.get("mergedAt"):
                    continue
                refs = (node.get("closingIssuesReferences") or {}).get("nodes") or []
                prs.append(
                    {
                        "createdAt": node.get("createdAt"),
                        "mergedAt": node.get("mergedAt"),
                        "size": int(node.get("additions") or 0) + int(node.get("deletions") or 0),
                        "reviews": int((node.get("reviews") or {}).get("totalCount") or 0),
                        "issues": [r.get("number") for r in refs if isinstance(r, dict)],
                    }
                )
            page = search.get("pageInfo") or {}
            if not page.get("hasNextPage"):
                break
            after = page.get("endCursor")
        start = date(start.year + 1, 1, 1)
    return prs, errors


def _hours(created: str, merged: str) -> float | None:
    try:
        a = datetime.fromisoformat(created.replace("Z", "+00:00"))
        b = datetime.fromisoformat(merged.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None
    return (b - a).total_seconds() / 3600


def _fmt(value: float | None, unit: str = "") -> str:
    if value is None:
        return "-"
    return f"{value:.1f}{unit}" if value < 100 else f"{value:.0f}{unit}"


def _ai_agent(trailer: str) -> str | None:
    low = trailer.lower()
    for agent, words in _AI_AGENTS:
        if any(word in low for word in words):
            return agent
    return None


def _usage_rows(text: str) -> list[dict[str, Any]]:
    """Records of a Copilot usage report: NDJSON, or one JSON array or object."""
    text = text.strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except ValueError:
        rows = []
        for line in text.splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
        return [r for r in rows if isinstance(r, dict)]
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    return [data] if isinstance(data, dict) else []


def _row_login(row: dict[str, Any]) -> str:
    for key in ("user_login", "login", "user", "assignee_login"):
        value = row.get(key)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, dict) and isinstance(value.get("login"), str):
            return str(value["login"])
    return ""


def register(
    headers: dict[str, str],
    executor: CallableToolExecutor,
    mark_self_action: Any,
) -> list[ToolDefinition]:
    """Build the GitHub people tools and register their callables."""

    async def get_github_engineering_metrics(
        logins: Any = None,
        org: str = "",
        repo: str = "",
        since: str = "",
        until: str = "",
        bucket: str = "quarter",
    ) -> str:
        names, err = _logins_error(logins)
        if err:
            return err
        org, repo, err = _scope_error(org, repo)
        if err:
            return err
        bucket = _coerce_str(bucket).strip().lower() or "quarter"
        if bucket not in ("month", "quarter", "year"):
            return "Error: bucket must be month, quarter or year."
        start, err = _parse_day(since, "since")
        if err:
            return err
        end, err = _parse_day(until, "until")
        if err:
            return err
        end = end or datetime.now(UTC).date()
        start = start or date(end.year - 1, end.month, 1)
        if end < start:
            return "Error: until is before since."
        buckets = period_buckets(start, end, bucket)
        if len(buckets) > _MAX_BUCKETS:
            return f"Error: {len(buckets)} buckets; at most {_MAX_BUCKETS} (use a larger bucket)."
        where = _where(org, repo)
        queries: dict[str, str] = {}
        for login in names:
            for label, first, last in buckets:
                span = f"{first.isoformat()}..{last.isoformat()}"
                queries[f"{login}|{label}|opened"] = f"is:pr author:{login} {where} created:{span}"
                queries[f"{login}|{label}|unmerged"] = (
                    f"is:pr is:unmerged is:closed author:{login} {where} closed:{span}"
                )
                queries[f"{login}|{label}|reviewed"] = (
                    f"is:pr reviewed-by:{login} -author:{login} {where} created:{span}"
                )
        semaphore = asyncio.Semaphore(_CONCURRENCY)
        async with egress_client(timeout=60) as client:
            counts_task = _search_counts(client, headers, queries, semaphore)
            merged_tasks = [_merged_prs(client, headers, login, where, start, end, semaphore) for login in names]
            (counts, errors), *merged = await asyncio.gather(counts_task, *merged_tasks)
        errors = list(errors)
        lines = [
            f"Engineering metrics in {where}, {start.isoformat()} to {end.isoformat()}, by {bucket} "
            "(merged = merged in the bucket; lead time = created to merged, hours; size = lines added + deleted; "
            "reviews in = reviews on the merged PRs; reviewed = others' PRs created in the bucket it reviewed; "
            "unique = merged with backports collapsed by closed issue).",
            "",
            "| login | bucket | opened | merged | unique | closed unmerged | median lead h | median size | "
            "reviews in | reviewed |",
            "|---|---|---|---|---|---|---|---|---|---|",
        ]
        for login, (prs, merged_errors) in zip(names, merged, strict=True):
            errors.extend(merged_errors)
            total = {"opened": 0, "merged": 0, "unique": 0, "unmerged": 0, "reviews": 0, "reviewed": 0}
            for label, first, last in buckets:
                in_bucket = [p for p in prs if first.isoformat() <= (p["mergedAt"] or "")[:10] <= last.isoformat()]
                leads = [h for p in in_bucket if (h := _hours(p["createdAt"], p["mergedAt"])) is not None]
                cell = {
                    "opened": counts.get(f"{login}|{label}|opened"),
                    "unmerged": counts.get(f"{login}|{label}|unmerged"),
                    "reviewed": counts.get(f"{login}|{label}|reviewed"),
                }
                reviews_in = sum(p["reviews"] for p in in_bucket)
                unique = collapse_backports(in_bucket)
                for key in ("opened", "unmerged", "reviewed"):
                    total[key] += cell[key] or 0
                total["merged"] += len(in_bucket)
                total["unique"] += unique
                total["reviews"] += reviews_in
                lines.append(
                    f"| {login} | {label} | {_dash(cell['opened'])} | {len(in_bucket)} | {unique} | "
                    f"{_dash(cell['unmerged'])} | {_fmt(_median(leads))} | "
                    f"{_fmt(_median([float(p['size']) for p in in_bucket]))} | {reviews_in} | {_dash(cell['reviewed'])} |"
                )
            all_leads = [h for p in prs if (h := _hours(p["createdAt"], p["mergedAt"])) is not None]
            lines.append(
                f"| **{login}** | **total** | {total['opened']} | {total['merged']} | {total['unique']} | "
                f"{total['unmerged']} | {_fmt(_median(all_leads))} | "
                f"{_fmt(_median([float(p['size']) for p in prs]))} | {total['reviews']} | {total['reviewed']} |"
            )
        if errors:
            lines += ["", f"Incomplete: {len(errors)} search(es) failed ('-' cells); first: {errors[0]}"]
        return "\n".join(lines)

    async def get_github_ai_usage(
        org: str,
        logins: Any = None,
        since: str = "",
        until: str = "",
        repo: str = "",
    ) -> str:
        if err := login_error(org, "org"):
            return err
        names: list[str] = []
        if logins:
            names, err = _logins_error(logins)
            if err:
                return err
        wanted = {n.lower() for n in names}
        start, err = _parse_day(since, "since")
        if err:
            return err
        end, err = _parse_day(until, "until")
        if err:
            return err
        end = end or datetime.now(UTC).date()
        start = start or end - timedelta(days=90)
        where = f"org:{org}"
        if _coerce_str(repo).strip():
            owner, name, err = parse_repo(repo)
            if err:
                return err
            where = f"repo:{owner}/{name}"
        lines: list[str] = [f"AI usage in {org}:"]
        async with egress_client(timeout=60) as client:
            # 1. Copilot seats with their last activity.
            seats: list[dict[str, Any]] = []
            seat_err = None
            for page in range(1, 11):
                resp, seat_err = await _common.safe_request(
                    client,
                    "get",
                    f"{_BASE_URL}/orgs/{org}/copilot/billing/seats",
                    _PROVIDER,
                    f"list Copilot seats of {org}",
                    headers=headers,
                    params={"per_page": 100, "page": page},
                )
                if seat_err or resp is None:
                    break
                body = resp.json() if resp.content else {}
                batch = body.get("seats") or [] if isinstance(body, dict) else []
                seats += [s for s in batch if isinstance(s, dict)]
                if len(batch) < 100:
                    break
            lines.append("")
            if seat_err:
                lines.append(f"Copilot seats: not readable. {_common.with_permission_hint(seat_err, _COPILOT_HINT)}")
            else:
                shown = [
                    s for s in seats if not wanted or ((s.get("assignee") or {}).get("login") or "").lower() in wanted
                ]
                lines.append(f"Copilot seats ({len(shown)} shown of {len(seats)}):")
                for s in sorted(shown, key=lambda s: ((s.get("assignee") or {}).get("login") or "").lower()):
                    login = (s.get("assignee") or {}).get("login") or "?"
                    lines.append(
                        f"  - {login} | plan {s.get('plan_type') or '-'} | since {(s.get('created_at') or '-')[:10]} | "
                        f"last activity {s.get('last_activity_at') or 'never'} | editor {s.get('last_activity_editor') or '-'}"
                    )
                if wanted:
                    seated = {((s.get("assignee") or {}).get("login") or "").lower() for s in seats}
                    missing = [n for n in names if n.lower() not in seated]
                    if missing:
                        lines.append(f"  No Copilot seat: {', '.join(missing)}")
            # 2. Per-user Copilot usage report (latest 28 days).
            lines.append("")
            resp, rep_err = await _common.safe_request(
                client,
                "get",
                f"{_BASE_URL}/orgs/{org}/copilot/metrics/reports/users-28-day/latest",
                _PROVIDER,
                f"read the Copilot users report of {org}",
                headers=headers,
            )
            if rep_err or resp is None:
                lines.append(
                    "Copilot usage report: not readable. "
                    + _common.with_permission_hint(rep_err or "Error: empty answer.", _COPILOT_HINT)
                )
            else:
                body = resp.json() if resp.content else {}
                links = [u for u in (body.get("download_links") or []) if isinstance(u, str)][:5]
                rows: list[dict[str, Any]] = []
                for link in links:
                    dl, dl_err = await _common.safe_request(
                        client, "get", link, _PROVIDER, "download the Copilot report"
                    )
                    if not dl_err and dl is not None:
                        rows += _usage_rows(dl.text)
                per_user: dict[str, dict[str, float]] = {}
                for row in rows:
                    login = _row_login(row)
                    if not login or (wanted and login.lower() not in wanted):
                        continue
                    agg = per_user.setdefault(login, {})
                    for key, value in row.items():
                        if isinstance(value, (int, float)) and not isinstance(value, bool):
                            agg[key] = agg.get(key, 0) + value
                period = f"{body.get('report_start_day', '?')} to {body.get('report_end_day', '?')}"
                lines.append(f"Copilot usage report, {period} ({len(per_user)} user(s)):")
                for login in sorted(per_user, key=str.lower):
                    top = sorted(per_user[login].items(), key=lambda kv: -kv[1])[:8]
                    lines.append(f"  - {login}: " + ", ".join(f"{k} {v:g}" for k, v in top))
            # 3. AI co-author trailers per login.
            if names:
                lines.append("")
                lines.append(
                    f"AI co-author trailers in {where}, commits authored {start.isoformat()} to {end.isoformat()}:"
                )
                span = f"{start.isoformat()}..{end.isoformat()}"
                for login in names:
                    resp, err = await _common.safe_request(
                        client,
                        "get",
                        f"{_BASE_URL}/search/commits",
                        _PROVIDER,
                        f"count commits of {login}",
                        headers=headers,
                        params={"q": f"author:{login} {where} author-date:{span}", "per_page": 1},
                    )
                    if err or resp is None:
                        lines.append(f"  - {login}: not readable ({err})")
                        continue
                    total = int((resp.json() or {}).get("total_count") or 0)
                    agents: dict[str, int] = {}
                    with_ai = 0
                    for page in range(1, _AI_TRAILER_PAGES + 1):
                        resp, err = await _common.safe_request(
                            client,
                            "get",
                            f"{_BASE_URL}/search/commits",
                            _PROVIDER,
                            f"read co-authored commits of {login}",
                            headers=headers,
                            params={
                                "q": f'"co-authored-by" author:{login} {where} author-date:{span}',
                                "per_page": 100,
                                "page": page,
                            },
                        )
                        if err or resp is None:
                            break
                        items = (resp.json() or {}).get("items") or []
                        for item in items:
                            message = ((item or {}).get("commit") or {}).get("message") or ""
                            found = {a for t in _TRAILER_RE.findall(message) if (a := _ai_agent(t))}
                            if found:
                                with_ai += 1
                                for agent in found:
                                    agents[agent] = agents.get(agent, 0) + 1
                        if len(items) < 100:
                            break
                    share = f"{100 * with_ai / total:.0f}%" if total else "-"
                    detail = ", ".join(f"{a} {n}" for a, n in sorted(agents.items(), key=lambda kv: -kv[1])) or "none"
                    lines.append(f"  - {login}: {total} commit(s), {with_ai} with an AI co-author ({share}): {detail}")
        return "\n".join(lines)

    async def lookup_github_identities(org: str, emails: Any = None, logins: Any = None) -> str:
        if err := login_error(org, "org"):
            return err
        email_list = _str_list(emails)
        login_list = _str_list(logins)
        if not email_list and not login_list:
            return "Error: give 'emails' and/or 'logins' to resolve."
        for login in login_list:
            if err := login_error(login, "login"):
                return err
        by_email: dict[str, tuple[str, str]] = {}
        by_login: dict[str, tuple[str, str]] = {}
        notes: list[str] = []
        async with egress_client(timeout=60) as client:
            # 1. SAML external identities (the organization's identity provider).
            after = None
            saml_err = None
            for _ in range(30):
                data, saml_err = await _common.gh_graphql(
                    client,
                    headers,
                    "query($org:String!,$after:String){organization(login:$org){samlIdentityProvider{"
                    "externalIdentities(first:100,after:$after){pageInfo{hasNextPage endCursor} nodes{"
                    "samlIdentity{nameId emails{value}} user{login}}}}}}",
                    {"org": org, "after": after},
                    action=f"read SAML identities of {org}",
                )
                if saml_err or not data:
                    break
                provider = (data.get("organization") or {}).get("samlIdentityProvider")
                if not provider:
                    saml_err = "no SAML identity provider on this organization"
                    break
                conn = provider.get("externalIdentities") or {}
                for node in conn.get("nodes") or []:
                    member = str(((node or {}).get("user") or {}).get("login") or "")
                    ident = (node or {}).get("samlIdentity") or {}
                    mails = [ident.get("nameId")] + [
                        e.get("value") for e in ident.get("emails") or [] if isinstance(e, dict)
                    ]
                    for mail in mails:
                        if isinstance(mail, str) and "@" in mail and member:
                            by_email.setdefault(mail.lower(), (member, "SAML"))
                            by_login.setdefault(member.lower(), (mail, "SAML"))
                page = conn.get("pageInfo") or {}
                if not page.get("hasNextPage"):
                    break
                after = page.get("endCursor")
            if saml_err:
                notes.append(f"SAML identities not used: {_common.with_permission_hint(saml_err, _IDENTITY_HINT)}")
            # 2. Members' verified domain emails, for what SAML did not answer.
            todo = [e for e in email_list if e.lower() not in by_email] or [
                lg for lg in login_list if lg.lower() not in by_login
            ]
            after = None
            members_err = None
            while todo:
                data, members_err = await _common.gh_graphql(
                    client,
                    headers,
                    "query($org:String!,$after:String){organization(login:$org){membersWithRole(first:100,after:$after){"
                    "pageInfo{hasNextPage endCursor} nodes{login email organizationVerifiedDomainEmails(login:$org)}}}}",
                    {"org": org, "after": after},
                    action=f"read member emails of {org}",
                )
                if members_err or not data:
                    break
                conn = (data.get("organization") or {}).get("membersWithRole") or {}
                for node in conn.get("nodes") or []:
                    member = str((node or {}).get("login") or "")
                    verified = [
                        m for m in (node or {}).get("organizationVerifiedDomainEmails") or [] if isinstance(m, str)
                    ]
                    for mail in verified:
                        if member:
                            by_email.setdefault(mail.lower(), (member, "verified domain email"))
                            by_login.setdefault(member.lower(), (mail, "verified domain email"))
                    public = (node or {}).get("email")
                    if member and isinstance(public, str) and public:
                        by_email.setdefault(public.lower(), (member, "public profile email"))
                        by_login.setdefault(member.lower(), (public, "public profile email"))
                page = conn.get("pageInfo") or {}
                if not page.get("hasNextPage"):
                    break
                after = page.get("endCursor")
            if members_err:
                notes.append(f"Member emails not used: {_common.with_permission_hint(members_err, _IDENTITY_HINT)}")
        lines = [f"Identities in {org}:"]
        for mail in email_list:
            hit = by_email.get(mail.lower())
            lines.append(f"  - {mail} -> {hit[0]} ({hit[1]})" if hit else f"  - {mail} -> not found")
        for login in login_list:
            hit = by_login.get(login.lower())
            lines.append(f"  - {login} -> {hit[0]} ({hit[1]})" if hit else f"  - {login} -> not found")
        if any(not by_email.get(m.lower()) for m in email_list) or any(
            not by_login.get(lg.lower()) for lg in login_list
        ):
            lines.append(
                "  Not found: the person has no SAML identity, verified domain email or public email in this "
                "organization; resolve them from the HR record (a GitHub field) or ask them."
            )
        return "\n".join(lines + [f"  Note: {n}" for n in notes])

    executor.register("get_github_engineering_metrics", get_github_engineering_metrics)
    executor.register("get_github_ai_usage", get_github_ai_usage)
    executor.register("lookup_github_identities", lookup_github_identities)

    _logins = {"type": "array", "items": {"type": "string"}, "description": "GitHub logins"}
    return [
        ToolDefinition(
            name="get_github_engineering_metrics",
            description=(
                "Delivery metrics of several GitHub logins in one call, per month, quarter or year: PRs opened, "
                "merged, closed unmerged, median lead time and size of merged PRs, reviews received, PRs reviewed "
                "for others, merged with backports collapsed by closed issue. One cohort table (up to 20 logins)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "logins": {**_logins, "description": "GitHub logins (at most 20)"},
                    "org": {"type": "string", "description": "Organization login (or repo)", "default": ""},
                    "repo": {"type": "string", "description": "owner/name (or org)", "default": ""},
                    "since": {
                        "type": "string",
                        "description": "First day, YYYY-MM-DD (default: a year ago)",
                        "default": "",
                    },
                    "until": {"type": "string", "description": "Last day, YYYY-MM-DD (default: today)", "default": ""},
                    "bucket": {"type": "string", "enum": ["month", "quarter", "year"], "default": "quarter"},
                },
                "required": ["logins"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="get_github_ai_usage",
            description=(
                "AI coding usage in an organization: Copilot seats with last activity, the per-user Copilot usage "
                "report (latest 28 days) when the token may read it, and per login the commits carrying an AI "
                "Co-authored-by trailer (Claude, Copilot, Codex, Cursor, Gemini...) over a period."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "org": {"type": "string", "description": "Organization login"},
                    "logins": {**_logins, "description": "Logins to keep (trailers are counted for these only)"},
                    "since": {
                        "type": "string",
                        "description": "First day, YYYY-MM-DD (default: 90 days ago)",
                        "default": "",
                    },
                    "until": {"type": "string", "description": "Last day, YYYY-MM-DD (default: today)", "default": ""},
                    "repo": {
                        "type": "string",
                        "description": "owner/name to count trailers in one repository",
                        "default": "",
                    },
                },
                "required": ["org"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="lookup_github_identities",
            description=(
                "Map employee emails to GitHub logins and logins to emails in an organization: SAML external "
                "identities first, then members' verified domain emails, then public profile emails; each row "
                "names its source."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "org": {"type": "string", "description": "Organization login"},
                    "emails": {"type": "array", "items": {"type": "string"}, "description": "Emails to resolve"},
                    "logins": {**_logins, "description": "Logins to resolve"},
                },
                "required": ["org"],
            },
            source=_SOURCE,
            priority=1,
        ),
    ]


def _dash(value: int | None) -> str:
    return "-" if value is None else str(value)


def _str_list(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [part for part in re.split(r"[\s,;]+", value) if part]
    if not isinstance(value, list):
        return []
    return [v.strip() for v in value if isinstance(v, str) and v.strip()]
