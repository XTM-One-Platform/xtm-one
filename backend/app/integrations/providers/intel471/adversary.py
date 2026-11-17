"""Intel 471 adversary + finished-intelligence read tools.

The foundation slice of the Titan API surface - the human-analyst
(adversary-intelligence) and finished-intelligence collections:

* ``search_intel471`` - the Global Search across every Titan collection at once
  (the broad "what do we have on X?" entry point).
* ``list_intel471_actors`` / ``get_intel471_actor`` - threat-actor handles and
  their full profile (aliases, forums, contact info).
* ``search_intel471_reports`` - finished information (intelligence) reports.
* ``get_intel471_report`` - fetch ONE report of any type by uid
  (report / breach_alert / spot_report / situation_report / news /
  malware_report / cve_report).
* ``search_intel471_breach_alerts`` / ``_spot_reports`` / ``_situation_reports``
  - the specialised finished-intelligence feeds.
* ``search_intel471_entities`` - resolve emails / handles / other observables to
  the actors and reports that reference them.
* ``search_intel471_news`` - underground/security news items.
* ``list_intel471_girs`` - the General Intelligence Requirements catalogue (the
  ``gir`` codes every other search accepts).
* ``list_intel471_tags`` - the report tag catalogue.

All are READ tools (priority 0). Titan search endpoints paginate with
``offset`` + ``count`` and accept the shared time-window filters.
"""

from __future__ import annotations

from typing import Any

from app.integrations.http import format_entity_list
from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import (
    _SOURCE,
    PAGE_PROPS,
    SORT_PROP,
    TIME_FILTER_PROPS,
    add_time_filters,
    clamp_count,
    clamp_offset,
    extract_items,
    extract_total,
    more_hint,
    ms_to_dt,
    norm_sort,
    safe_seg,
    titan_get,
    trunc,
)

# Report families reachable through the consolidated get_intel471_report tool,
# mapped to their Titan detail path. The uid-detail endpoints all take the
# report uid as the final path segment.
_REPORT_PATHS: dict[str, str] = {
    "report": "/reports/{uid}",
    "breach_alert": "/breachAlerts/{uid}",
    "spot_report": "/spotReports/{uid}",
    "situation_report": "/situationReports/{uid}",
    "news": "/news/{uid}",
    "malware_report": "/malwareReports/{uid}",
    "cve_report": "/cve/reports/{uid}",
}


def _uid(obj: dict[str, Any]) -> str:
    return str(obj.get("uid") or obj.get("id") or "?")


def _title_of(obj: dict[str, Any]) -> str:
    """Best-effort human title across the various Titan record shapes."""
    data = obj.get("data") if isinstance(obj.get("data"), dict) else {}
    for holder in (obj, data):
        for key in ("subject", "title", "name", "reportTitle"):
            val = holder.get(key)
            if val:
                return trunc(val, 140)
    # breachAlert / spotReport nest the title one level deeper.
    for nested_key in ("breachAlert", "spotReport", "situationReport"):
        nested = data.get(nested_key) if isinstance(data.get(nested_key), dict) else {}
        if nested.get("title"):
            return trunc(nested["title"], 140)
    return "(untitled)"


def _actor_line(obj: dict[str, Any]) -> str:
    handle = obj.get("handle") or obj.get("actor") or "?"
    links = obj.get("links") or {}
    forums = links.get("forums") if isinstance(links, dict) else None
    forum_ct = len(forums) if isinstance(forums, list) else 0
    forum_part = f", active on {forum_ct} forum(s)" if forum_ct else ""
    return f"  {handle} (uid: {_uid(obj)}){forum_part}"


def _report_line(obj: dict[str, Any]) -> str:
    created = obj.get("created") or obj.get("released") or ""
    date = ms_to_dt(created)
    admiralty = obj.get("admiraltyCode") or ""
    meta = " | ".join(p for p in (date, f"admiralty {admiralty}" if admiralty else "") if p)
    tail = f"\n    {meta}" if meta else ""
    return f"  [{_uid(obj)}] {_title_of(obj)}{tail}"


def _entity_line(obj: dict[str, Any]) -> str:
    value = obj.get("value") or obj.get("entity") or "?"
    etype = obj.get("type") or "?"
    links = obj.get("links") or {}
    actors = links.get("actors") if isinstance(links, dict) else None
    reports = links.get("reports") if isinstance(links, dict) else None
    refs = []
    if isinstance(actors, list) and actors:
        refs.append(f"{len(actors)} actor(s)")
    if isinstance(reports, list) and reports:
        refs.append(f"{len(reports)} report(s)")
    tail = f" - referenced by {', '.join(refs)}" if refs else ""
    return f"  {value} ({etype}){tail}"


def register(
    headers: dict[str, str], executor: CallableToolExecutor
) -> list[ToolDefinition]:
    """Register the adversary + finished-intelligence tools; return definitions."""

    # --- Global search ---------------------------------------------------
    async def search_intel471(
        text: str = "",
        actor: str = "",
        ioc: str = "",
        report: str = "",
        malware_family: str = "",
        gir: str = "",
        from_time: str = "",
        until_time: str = "",
        last_updated_from: str = "",
        last_updated_until: str = "",
        sort: str = "relevance",
        offset: int = 0,
        count: int = 10,
    ) -> str:
        """Global search across EVERY Titan collection at once (the broad entry point).

        Use when you do not yet know which collection holds the answer - it spans
        actors, forum posts, reports, IOCs, indicators, events, credentials,
        marketplaces and more. Provide ``text`` for a free-text sweep, or narrow
        with a typed field (``actor``, ``ioc``, ``report``, ``malware_family``).
        For deep, structured results in one collection, prefer the dedicated
        search tools (e.g. list_intel471_actors, search_intel471_reports).
        """
        params: dict[str, Any] = {
            "sort": norm_sort(sort, "relevance"),
            "offset": clamp_offset(offset),
            "count": clamp_count(count),
        }
        for value, key in (
            (text, "text"),
            (actor, "actor"),
            (ioc, "ioc"),
            (report, "report"),
            (malware_family, "malwareFamily"),
            (gir, "gir"),
        ):
            if value and str(value).strip():
                params[key] = str(value).strip()
        add_time_filters(
            params,
            from_=from_time,
            until=until_time,
            last_updated_from=last_updated_from,
            last_updated_until=last_updated_until,
        )
        if not any(k in params for k in ("text", "actor", "ioc", "report", "malwareFamily", "gir")):
            return (
                "Provide at least one search term: 'text' for a free-text sweep, "
                "or a typed field (actor, ioc, report, malware_family, gir)."
            )
        payload, err = await titan_get(headers, "/search", "global search", params=params)
        if err:
            return err

        def _fmt(item: dict[str, Any]) -> str:
            label = (
                item.get("subject")
                or item.get("handle")
                or item.get("value")
                or item.get("title")
                or item.get("uid")
                or "?"
            )
            return f"  [{item.get('type', '?')}] {trunc(label, 140)} (uid: {_uid(item)})"

        items = extract_items(payload, "search", "results")
        total = extract_total(payload, len(items))
        out = format_entity_list(
            items, _fmt, noun="result", total=total,
            empty_msg="No Titan records matched the search.",
        )
        return out + more_hint(total, len(items), clamp_offset(offset))

    # --- Actors ----------------------------------------------------------
    async def list_intel471_actors(
        actor: str = "",
        forum: str = "",
        from_time: str = "",
        until_time: str = "",
        last_updated_from: str = "",
        last_updated_until: str = "",
        sort: str = "latest",
        offset: int = 0,
        count: int = 10,
    ) -> str:
        """Search threat actors by handle or by the forum they are active on.

        At least one of ``actor`` (handle substring) or ``forum`` is required.
        Returns actor handles and their uids - feed a uid to get_intel471_actor
        for the profile summary (forum memberships, activity window).
        """
        if not (actor.strip() or forum.strip()):
            return "At least one of 'actor' (handle) or 'forum' is required."
        params: dict[str, Any] = {
            "sort": norm_sort(sort, "latest"),
            "offset": clamp_offset(offset),
            "count": clamp_count(count),
        }
        if actor.strip():
            params["actor"] = actor.strip()
        if forum.strip():
            params["forum"] = forum.strip()
        add_time_filters(
            params, from_=from_time, until=until_time,
            last_updated_from=last_updated_from, last_updated_until=last_updated_until,
        )
        payload, err = await titan_get(headers, "/actors", "search actors", params=params)
        if err:
            return err
        items = extract_items(payload, "actors")
        total = extract_total(payload, len(items))
        out = format_entity_list(
            items, _actor_line, noun="actor", total=total,
            empty_msg="No actors matched the search.",
        )
        return out + more_hint(total, len(items), clamp_offset(offset))

    async def get_intel471_actor(uid: str) -> str:
        """Get a threat actor's profile summary by uid (from list_intel471_actors).

        Returns the handle, the forums the actor is active on, and the activity
        window (first/last seen).
        """
        uid = (uid or "").strip()
        if not safe_seg(uid):
            return "Error: 'uid' must be a plain actor uid (no path separators)."
        payload, err = await titan_get(
            headers, f"/actors/{uid}", f"get actor {uid}", expected_statuses={404}
        )
        if err:
            return f"No actor found with uid '{uid}'." if _is_404(payload) else err
        actor = _first_record(payload, "actors")
        if not actor:
            return f"No actor found with uid '{uid}'."
        handle = actor.get("handle") or "?"
        lines = [f"Actor: {handle} (uid: {_uid(actor)})"]
        links = actor.get("links") or {}
        forums = links.get("forums") if isinstance(links, dict) else None
        if isinstance(forums, list) and forums:
            names = [f.get("name") or f.get("title") or "?" for f in forums if isinstance(f, dict)]
            lines.append("Active on forums: " + ", ".join(str(n) for n in names[:15]))
        if actor.get("activeFrom"):
            lines.append(f"Active from: {ms_to_dt(actor.get('activeFrom'))}")
        if actor.get("activeUntil"):
            lines.append(f"Active until: {ms_to_dt(actor.get('activeUntil'))}")
        return "\n".join(lines)

    # --- Reports (finished intelligence) --------------------------------
    async def search_intel471_reports(
        report: str = "",
        report_title: str = "",
        victim: str = "",
        report_location: str = "",
        report_tag: str = "",
        document_type: str = "",
        gir: str = "",
        from_time: str = "",
        until_time: str = "",
        last_updated_from: str = "",
        last_updated_until: str = "",
        sort: str = "relevance",
        offset: int = 0,
        count: int = 10,
    ) -> str:
        """Search Intel 471 finished information (intelligence) reports.

        Free-text with ``report`` or narrow with ``report_title``, ``victim``,
        ``report_location`` (country/region), ``report_tag``, ``document_type``,
        or a ``gir`` code (see list_intel471_girs). Returns report uids - fetch
        one with get_intel471_report (report_type='report').
        """
        params: dict[str, Any] = {
            "sort": norm_sort(sort, "relevance"),
            "offset": clamp_offset(offset),
            "count": clamp_count(count),
        }
        for value, key in (
            (report, "report"),
            (report_title, "reportTitle"),
            (victim, "victim"),
            (report_location, "reportLocation"),
            (report_tag, "reportTag"),
            (document_type, "documentType"),
            (gir, "gir"),
        ):
            if value and str(value).strip():
                params[key] = str(value).strip()
        add_time_filters(
            params, from_=from_time, until=until_time,
            last_updated_from=last_updated_from, last_updated_until=last_updated_until,
        )
        payload, err = await titan_get(headers, "/reports", "search reports", params=params)
        if err:
            return err
        items = extract_items(payload, "reports")
        total = extract_total(payload, len(items))
        out = format_entity_list(
            items, _report_line, noun="report", total=total,
            empty_msg="No reports matched the search.",
        )
        return out + more_hint(total, len(items), clamp_offset(offset))

    async def get_intel471_report(uid: str, report_type: str = "report") -> str:
        """Fetch ONE Intel 471 report/document of any type by uid.

        ``report_type`` selects the collection: 'report' (information report),
        'breach_alert', 'spot_report', 'situation_report', 'news',
        'malware_report', or 'cve_report'. Use the uid returned by the matching
        search tool. Returns the title, creation date and body text (truncated).
        """
        uid = (uid or "").strip()
        rtype = (report_type or "report").strip().lower()
        if rtype not in _REPORT_PATHS:
            return (
                "Error: report_type must be one of "
                f"{', '.join(sorted(_REPORT_PATHS))}."
            )
        if not safe_seg(uid):
            return "Error: 'uid' must be a plain report uid (no path separators)."
        path = _REPORT_PATHS[rtype].format(uid=uid)
        payload, err = await titan_get(
            headers, path, f"get {rtype} {uid}", expected_statuses={404}
        )
        if err:
            return f"No {rtype} found with uid '{uid}'." if _is_404(payload) else err
        obj = payload if isinstance(payload, dict) else {}
        # A detail endpoint may return the record directly or under a wrapper
        # key. Collect every dict layer so the title/body/date lookups below can
        # read whichever shape Titan returns (the top-level obj carries the
        # ``data.<nested>.title`` shape that _title_of understands).
        wrapped = obj.get("report") or obj.get("data")
        layers = [obj]
        if isinstance(wrapped, dict):
            layers.append(wrapped)
        lines = [f"{rtype.replace('_', ' ').title()}: {_title_of(obj)}"]
        lines.append(f"uid: {uid}")
        body = ""
        for holder in layers:
            for key in ("text", "rawText", "rawTextTranslated", "researcherComments"):
                if holder.get(key):
                    body = str(holder[key])
                    break
            if body:
                break
        created = next(
            (h.get("created") for h in layers if h.get("created")), ""
        )
        if created:
            lines.append(f"created: {ms_to_dt(created)}")
        if body:
            lines.append("")
            lines.append(trunc(body, 2000))
        return "\n".join(lines)

    # --- Specialised finished-intelligence feeds ------------------------
    async def search_intel471_breach_alerts(
        breach_alert: str = "",
        actor: str = "",
        victim: str = "",
        confidence: str = "",
        gir: str = "",
        from_time: str = "",
        until_time: str = "",
        last_updated_from: str = "",
        last_updated_until: str = "",
        sort: str = "latest",
        offset: int = 0,
        count: int = 10,
    ) -> str:
        """Search Intel 471 breach alerts (early warning of a compromised org).

        At least one of ``breach_alert`` (free text), ``actor``, or ``victim`` is
        required. Optional ``confidence`` and ``gir`` filters. Returns breach
        alert uids - fetch one with get_intel471_report (report_type='breach_alert').
        """
        if not (breach_alert.strip() or actor.strip() or victim.strip()):
            return "At least one of 'breach_alert', 'actor', or 'victim' is required."
        params: dict[str, Any] = {
            "sort": norm_sort(sort, "latest"),
            "offset": clamp_offset(offset),
            "count": clamp_count(count),
        }
        for value, key in (
            (breach_alert, "breachAlert"),
            (actor, "actor"),
            (victim, "victim"),
            (confidence, "confidence"),
            (gir, "gir"),
        ):
            if value and str(value).strip():
                params[key] = str(value).strip()
        add_time_filters(
            params, from_=from_time, until=until_time,
            last_updated_from=last_updated_from, last_updated_until=last_updated_until,
        )
        payload, err = await titan_get(
            headers, "/breachAlerts", "search breach alerts", params=params
        )
        if err:
            return err
        items = extract_items(payload, "breachAlerts")
        total = extract_total(payload, len(items))
        out = format_entity_list(
            items, _report_line, noun="breach alert", total=total,
            empty_msg="No breach alerts matched the search.",
        )
        return out + more_hint(total, len(items), clamp_offset(offset))

    async def search_intel471_spot_reports(
        spot_report: str = "",
        victim: str = "",
        gir: str = "",
        from_time: str = "",
        until_time: str = "",
        last_updated_from: str = "",
        last_updated_until: str = "",
        sort: str = "latest",
        offset: int = 0,
        count: int = 10,
    ) -> str:
        """Search Intel 471 spot reports (rapid alerts on notable developments).

        ``spot_report`` is a free-text query; narrow with ``victim`` or a ``gir``
        code. Returns spot report uids - fetch with get_intel471_report
        (report_type='spot_report').
        """
        params: dict[str, Any] = {
            "sort": norm_sort(sort, "latest"),
            "offset": clamp_offset(offset),
            "count": clamp_count(count),
        }
        for value, key in (
            (spot_report, "spotReport"),
            (victim, "victim"),
            (gir, "gir"),
        ):
            if value and str(value).strip():
                params[key] = str(value).strip()
        add_time_filters(
            params, from_=from_time, until=until_time,
            last_updated_from=last_updated_from, last_updated_until=last_updated_until,
        )
        payload, err = await titan_get(
            headers, "/spotReports", "search spot reports", params=params
        )
        if err:
            return err
        items = extract_items(payload, "spotReports")
        total = extract_total(payload, len(items))
        out = format_entity_list(
            items, _report_line, noun="spot report", total=total,
            empty_msg="No spot reports matched the search.",
        )
        return out + more_hint(total, len(items), clamp_offset(offset))

    async def search_intel471_situation_reports(
        situation_report: str = "",
        victim: str = "",
        gir: str = "",
        from_time: str = "",
        until_time: str = "",
        last_updated_from: str = "",
        last_updated_until: str = "",
        sort: str = "latest",
        offset: int = 0,
        count: int = 10,
    ) -> str:
        """Search Intel 471 situation reports (ongoing-event tracking).

        ``situation_report`` is a free-text query; narrow with ``victim`` or a
        ``gir`` code. Returns situation report uids - fetch with
        get_intel471_report (report_type='situation_report').
        """
        params: dict[str, Any] = {
            "sort": norm_sort(sort, "latest"),
            "offset": clamp_offset(offset),
            "count": clamp_count(count),
        }
        for value, key in (
            (situation_report, "situationReport"),
            (victim, "victim"),
            (gir, "gir"),
        ):
            if value and str(value).strip():
                params[key] = str(value).strip()
        add_time_filters(
            params, from_=from_time, until=until_time,
            last_updated_from=last_updated_from, last_updated_until=last_updated_until,
        )
        payload, err = await titan_get(
            headers, "/situationReports", "search situation reports", params=params
        )
        if err:
            return err
        items = extract_items(payload, "situationReports")
        total = extract_total(payload, len(items))
        out = format_entity_list(
            items, _report_line, noun="situation report", total=total,
            empty_msg="No situation reports matched the search.",
        )
        return out + more_hint(total, len(items), clamp_offset(offset))

    # --- Entities + news + reference catalogues -------------------------
    async def search_intel471_entities(
        entity: str,
        entity_type: str = "",
        from_time: str = "",
        until_time: str = "",
        last_updated_from: str = "",
        last_updated_until: str = "",
        sort: str = "relevance",
        offset: int = 0,
        count: int = 10,
    ) -> str:
        """Resolve an observable (email, handle, ...) to the actors/reports citing it.

        ``entity`` is the value to look up (required). Optionally constrain with
        ``entity_type`` (e.g. 'EmailAddress', 'Handle'). Returns each matching
        entity and how many actors/reports reference it - the pivot from an
        indicator to the human intelligence around it.
        """
        entity = (entity or "").strip()
        if not entity:
            return "Error: 'entity' (the value to look up) is required."
        params: dict[str, Any] = {
            "entity": entity,
            "sort": norm_sort(sort, "relevance"),
            "offset": clamp_offset(offset),
            "count": clamp_count(count),
        }
        if entity_type.strip():
            params["entityType"] = entity_type.strip()
        add_time_filters(
            params, from_=from_time, until=until_time,
            last_updated_from=last_updated_from, last_updated_until=last_updated_until,
        )
        payload, err = await titan_get(headers, "/entities", "search entities", params=params)
        if err:
            return err
        items = extract_items(payload, "entities")
        total = extract_total(payload, len(items))
        out = format_entity_list(
            items, _entity_line, noun="entity", total=total,
            empty_msg=f"No entities matched '{entity}'.",
        )
        return out + more_hint(total, len(items), clamp_offset(offset))

    async def search_intel471_news(
        news: str = "",
        news_type: str = "",
        from_time: str = "",
        until_time: str = "",
        last_updated_from: str = "",
        last_updated_until: str = "",
        sort: str = "latest",
        offset: int = 0,
        count: int = 10,
    ) -> str:
        """Search underground/security news items collected by Intel 471.

        ``news`` is a free-text query; narrow with ``news_type``. Returns news
        uids - fetch the full item with get_intel471_report (report_type='news').
        """
        params: dict[str, Any] = {
            "sort": norm_sort(sort, "latest"),
            "offset": clamp_offset(offset),
            "count": clamp_count(count),
        }
        if news.strip():
            params["news"] = news.strip()
        if news_type.strip():
            params["newsType"] = news_type.strip()
        add_time_filters(
            params, from_=from_time, until=until_time,
            last_updated_from=last_updated_from, last_updated_until=last_updated_until,
        )
        payload, err = await titan_get(headers, "/news", "search news", params=params)
        if err:
            return err
        items = extract_items(payload, "news")
        total = extract_total(payload, len(items))
        out = format_entity_list(
            items, _report_line, noun="news item", total=total,
            empty_msg="No news items matched the search.",
        )
        return out + more_hint(total, len(items), clamp_offset(offset))

    async def list_intel471_girs(
        gir_path: str = "",
        gir_name: str = "",
        gir_description: str = "",
        offset: int = 0,
        count: int = 10,
    ) -> str:
        """List General Intelligence Requirements (GIR) codes for filtering searches.

        GIRs are Intel 471's taxonomy of intelligence topics; their paths (e.g.
        '1.1.3') are the ``gir`` filter accepted by the report / indicator / CVE
        searches. Filter the catalogue by ``gir_path``, ``gir_name``, or
        ``gir_description``.
        """
        params: dict[str, Any] = {"offset": clamp_offset(offset), "count": clamp_count(count)}
        if gir_path.strip():
            params["girPath"] = gir_path.strip()
        if gir_name.strip():
            params["girName"] = gir_name.strip()
        if gir_description.strip():
            params["girDescription"] = gir_description.strip()
        payload, err = await titan_get(headers, "/girs", "list GIRs", params=params)
        if err:
            return err

        def _fmt(item: dict[str, Any]) -> str:
            data = item.get("data") if isinstance(item.get("data"), dict) else item
            gir = data.get("gir") if isinstance(data.get("gir"), dict) else data
            path = gir.get("path") or item.get("path") or "?"
            name = gir.get("name") or item.get("name") or ""
            return f"  {path} - {trunc(name, 120)}"

        items = extract_items(payload, "girs")
        total = extract_total(payload, len(items))
        out = format_entity_list(
            items, _fmt, noun="GIR", total=total,
            empty_msg="No GIRs matched the filters.",
        )
        return out + more_hint(total, len(items), clamp_offset(offset))

    async def list_intel471_tags(used_only: bool = False) -> str:
        """List the report tags in the account (optionally only tags in use).

        Tags are the values accepted by search_intel471_reports' ``report_tag``
        filter. Set ``used_only`` to show only tags with a non-zero use count.
        """
        params: dict[str, Any] = {}
        if used_only:
            params["used"] = "true"
        payload, err = await titan_get(headers, "/tags", "list tags", params=params)
        if err:
            return err

        def _fmt(item: dict[str, Any]) -> str:
            name = item.get("name") or item.get("tag") or "?"
            use_count = item.get("useCount")
            tail = f" ({use_count} uses)" if use_count is not None else ""
            return f"  {name}{tail}"

        # /tags may return a bare list rather than an envelope.
        items = payload if isinstance(payload, list) else extract_items(payload, "tags")
        items = [i for i in items if isinstance(i, dict)]
        return format_entity_list(
            items, _fmt, noun="tag", total=len(items),
            empty_msg="No tags found.",
        )

    handlers = {
        "search_intel471": search_intel471,
        "list_intel471_actors": list_intel471_actors,
        "get_intel471_actor": get_intel471_actor,
        "search_intel471_reports": search_intel471_reports,
        "get_intel471_report": get_intel471_report,
        "search_intel471_breach_alerts": search_intel471_breach_alerts,
        "search_intel471_spot_reports": search_intel471_spot_reports,
        "search_intel471_situation_reports": search_intel471_situation_reports,
        "search_intel471_entities": search_intel471_entities,
        "search_intel471_news": search_intel471_news,
        "list_intel471_girs": list_intel471_girs,
        "list_intel471_tags": list_intel471_tags,
    }
    for name, fn in handlers.items():
        executor.register(name, fn)

    return _definitions()


def _is_404(resp: Any) -> bool:
    """True when a ``safe_request`` (resp, err) tuple carried a 404 response."""
    return getattr(resp, "status_code", None) == 404


def _first_record(payload: Any, *keys: str) -> dict[str, Any] | None:
    """Return a single record from a detail response (direct or list-wrapped)."""
    if isinstance(payload, dict):
        items = extract_items(payload, *keys)
        if items:
            return items[0]
        # A detail endpoint may return the object directly.
        if payload.get("uid") or payload.get("handle") or payload.get("data"):
            return payload
    return None


def _definitions() -> list[ToolDefinition]:
    """Static ToolDefinition metadata for the adversary + finished-intel tools."""

    def _td(name: str, description: str, properties: dict[str, Any], required: list[str]) -> ToolDefinition:
        return ToolDefinition(
            name=name,
            description=description,
            input_schema={"type": "object", "properties": properties, "required": required},
            source=_SOURCE,
            priority=0,
        )

    return [
        _td(
            "search_intel471",
            "Global search across every Intel 471 (Titan) collection at once - "
            "actors, forum posts, reports, IOCs, indicators, events, credentials, "
            "marketplaces and more. The broad 'what do we have on X?' entry point "
            "when you do not yet know which collection holds the answer; for deep "
            "structured results prefer the dedicated search tools. Provide 'text' "
            "or a typed field.",
            {
                "text": {"type": "string", "description": "Free-text search across all collections."},
                "actor": {"type": "string", "description": "Actor handle to search for."},
                "ioc": {"type": "string", "description": "Indicator of compromise value."},
                "report": {"type": "string", "description": "Report free-text search."},
                "malware_family": {"type": "string", "description": "Malware family (e.g. 'trickbot')."},
                "gir": {"type": "string", "description": "GIR code filter (see list_intel471_girs)."},
                **TIME_FILTER_PROPS,
                **SORT_PROP,
                **PAGE_PROPS,
            },
            [],
        ),
        _td(
            "list_intel471_actors",
            "Search Intel 471 threat actors by handle or by the forum they are "
            "active on (at least one of 'actor' or 'forum' is required). Returns "
            "actor handles + uids; feed a uid to get_intel471_actor for the full "
            "profile.",
            {
                "actor": {"type": "string", "description": "Actor handle substring to search."},
                "forum": {"type": "string", "description": "Show actors active on this forum."},
                **TIME_FILTER_PROPS,
                **SORT_PROP,
                **PAGE_PROPS,
            },
            [],
        ),
        _td(
            "get_intel471_actor",
            "Get a single Intel 471 threat actor's profile summary by uid "
            "(handle, forum memberships, activity window). Use a uid from "
            "list_intel471_actors.",
            {"uid": {"type": "string", "description": "Actor uid from list_intel471_actors."}},
            ["uid"],
        ),
        _td(
            "search_intel471_reports",
            "Search Intel 471 finished information (intelligence) reports. Free-text "
            "with 'report' or narrow by title, victim, location, tag, document type, "
            "or GIR code. Returns report uids; fetch one with get_intel471_report.",
            {
                "report": {"type": "string", "description": "Free-text report search."},
                "report_title": {"type": "string", "description": "Search by report title."},
                "victim": {"type": "string", "description": "Search by purported victim."},
                "report_location": {"type": "string", "description": "Country or region."},
                "report_tag": {"type": "string", "description": "Report tag (see list_intel471_tags)."},
                "document_type": {"type": "string", "description": "Filter by document type."},
                "gir": {"type": "string", "description": "GIR code filter (see list_intel471_girs)."},
                **TIME_FILTER_PROPS,
                **SORT_PROP,
                **PAGE_PROPS,
            },
            [],
        ),
        _td(
            "get_intel471_report",
            "Fetch ONE Intel 471 report/document by uid. report_type selects the "
            "collection: report / breach_alert / spot_report / situation_report / "
            "news / malware_report / cve_report. Returns the title, creation date "
            "and body text.",
            {
                "uid": {"type": "string", "description": "Document uid from the matching search."},
                "report_type": {
                    "type": "string",
                    "enum": sorted(_REPORT_PATHS),
                    "default": "report",
                    "description": "Which finished-intelligence collection the uid belongs to.",
                },
            },
            ["uid"],
        ),
        _td(
            "search_intel471_breach_alerts",
            "Search Intel 471 breach alerts (early warning that an organization has "
            "been compromised). At least one of 'breach_alert', 'actor', or "
            "'victim' is required. Fetch one with get_intel471_report "
            "(report_type='breach_alert').",
            {
                "breach_alert": {"type": "string", "description": "Free-text breach-alert search."},
                "actor": {"type": "string", "description": "Actor / actor group name."},
                "victim": {"type": "string", "description": "Purported victim organization."},
                "confidence": {"type": "string", "description": "Confidence level filter."},
                "gir": {"type": "string", "description": "GIR code filter."},
                **TIME_FILTER_PROPS,
                **SORT_PROP,
                **PAGE_PROPS,
            },
            [],
        ),
        _td(
            "search_intel471_spot_reports",
            "Search Intel 471 spot reports (rapid alerts on notable developments). "
            "Free-text 'spot_report'; narrow by victim or GIR. Fetch one with "
            "get_intel471_report (report_type='spot_report').",
            {
                "spot_report": {"type": "string", "description": "Free-text spot-report search."},
                "victim": {"type": "string", "description": "Purported victim."},
                "gir": {"type": "string", "description": "GIR code filter."},
                **TIME_FILTER_PROPS,
                **SORT_PROP,
                **PAGE_PROPS,
            },
            [],
        ),
        _td(
            "search_intel471_situation_reports",
            "Search Intel 471 situation reports (ongoing-event tracking). Free-text "
            "'situation_report'; narrow by victim or GIR. Fetch one with "
            "get_intel471_report (report_type='situation_report').",
            {
                "situation_report": {"type": "string", "description": "Free-text situation-report search."},
                "victim": {"type": "string", "description": "Purported victim."},
                "gir": {"type": "string", "description": "GIR code filter."},
                **TIME_FILTER_PROPS,
                **SORT_PROP,
                **PAGE_PROPS,
            },
            [],
        ),
        _td(
            "search_intel471_entities",
            "Resolve an observable (email address, handle, or other entity value) "
            "to the Intel 471 actors and reports that reference it - the pivot from "
            "an indicator to the human intelligence around it. 'entity' (the value) "
            "is required.",
            {
                "entity": {"type": "string", "description": "The entity value to look up (required)."},
                "entity_type": {"type": "string", "description": "Optional type filter, e.g. 'EmailAddress'."},
                **TIME_FILTER_PROPS,
                **SORT_PROP,
                **PAGE_PROPS,
            },
            ["entity"],
        ),
        _td(
            "search_intel471_news",
            "Search underground/security news items collected by Intel 471. "
            "Free-text 'news'; narrow by 'news_type'. Fetch the full item with "
            "get_intel471_report (report_type='news').",
            {
                "news": {"type": "string", "description": "Free-text news search."},
                "news_type": {"type": "string", "description": "News type filter."},
                **TIME_FILTER_PROPS,
                **SORT_PROP,
                **PAGE_PROPS,
            },
            [],
        ),
        _td(
            "list_intel471_girs",
            "List Intel 471 General Intelligence Requirements (GIR) codes - the "
            "topic taxonomy whose paths (e.g. '1.1.3') are the 'gir' filter accepted "
            "by the report / indicator / CVE searches. Filter by path, name, or "
            "description.",
            {
                "gir_path": {"type": "string", "description": "Filter by GIR path, e.g. '1.1'."},
                "gir_name": {"type": "string", "description": "Filter by GIR name."},
                "gir_description": {"type": "string", "description": "Filter by GIR description."},
                **PAGE_PROPS,
            },
            [],
        ),
        _td(
            "list_intel471_tags",
            "List the report tags in the account - the values accepted by "
            "search_intel471_reports' 'report_tag' filter. Set used_only to show "
            "only tags currently in use.",
            {
                "used_only": {
                    "type": "boolean",
                    "default": False,
                    "description": "Only show tags with a non-zero use count.",
                }
            },
            [],
        ),
    ]
