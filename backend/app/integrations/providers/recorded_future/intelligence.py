"""Recorded Future finished-intelligence + detection-engineering tools.

* Analyst Notes (Insikt Group finished intelligence): ``search_rf_analyst_notes``
  and ``get_rf_analyst_note`` (Analyst Note API ``/analyst-note``).
* Detection Rules: ``search_rf_detection_rules`` - discover Insikt Group YARA /
  Sigma / Snort detection rules (Detection Rule API ``/detection-rule``); it
  returns a per-rule-set summary (type / title / id / files), not the rule bodies.
* Links graph: ``search_rf_links`` - validated relationships between entities
  (Links API ``/links``) for pivoting during investigations.
* Adversaries: ``search_rf_threat_actors``, ``get_rf_threat_map`` (actors) and
  ``get_rf_malware_threat_map`` - the threat-actor database and the org-scoped
  threat maps (Threat API ``/threat``).
* Risk History: ``get_rf_risk_history`` - risk score / rule change events over
  time for any entity (Risk API ``/risk/history``).
* Publishing: ``publish_rf_analyst_note`` - push finished intelligence back
  into Recorded Future (Analyst Note API ``/analyst-note/publish``).
* Sandbox corpus: ``query_rf_malware_intelligence`` - natural-language search
  over Recorded Future's Malware Intelligence sandbox data
  (``/malware-intelligence/v1/query_nl``).
"""

from __future__ import annotations

from typing import Any

from app.integrations.http import format_entity_list, safe_request
from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import _PROVIDER, _SOURCE, clamp, client, csv_list, dt, trunc

_RULE_TYPES = frozenset({"yara", "sigma", "snort"})

# Malware Intelligence query fields (``QueryField`` enum, abridged to the
# high-signal set an agent actually pivots on).
_MI_FIELDS = frozenset(
    {
        "sha256",
        "ip_address",
        "domain",
        "url",
        "command_line",
        "ttp",
        "dst_port",
        "signature",
        "ioc",
        "tag",
    }
)


def register(
    api: str, headers: dict[str, str], executor: CallableToolExecutor
) -> list[ToolDefinition]:
    """Register intelligence + detection + adversary tools and return defs."""

    # ── analyst notes ─────────────────────────────────────────────────────
    def _fmt_note(note: dict[str, Any]) -> str:
        attrs = note.get("attributes") or {}
        source = note.get("source") or {}
        return (
            f"  {trunc(attrs.get('title'), 120)}\n"
            f"    id: {note.get('id', '?')} | source: {source.get('name', '?')} | "
            f"published: {dt(attrs.get('published'))}"
        )

    async def search_rf_analyst_notes(
        published: str = "-7d",
        entity: str = "",
        topic: str = "",
        title: str = "",
        enterprise_only: bool = False,
        limit: int = 20,
    ) -> str:
        """Search Recorded Future analyst notes (Insikt Group finished intelligence).

        Filter by ``published`` (absolute YYYY-MM-DD or relative like '-7d'),
        an ``entity`` id (notes referencing it), a ``topic`` entity id, a
        ``title`` substring, or set ``enterprise_only`` to exclude Insikt Group
        notes. Returns note ids for get_rf_analyst_note.
        """
        body: dict[str, Any] = {
            "limit": clamp(limit, 20, hi=100),
            "serialization": "full",
        }
        if published.strip():
            body["published"] = published.strip()
        if entity.strip():
            body["entity"] = entity.strip()
        if topic.strip():
            body["topic"] = topic.strip()
        if title.strip():
            body["title"] = title.strip()
        if enterprise_only:
            body["enterprise_only"] = True
        async with client() as c:
            resp, err = await safe_request(
                c,
                "post",
                f"{api}/analyst-note/search",
                _PROVIDER,
                "search analyst notes",
                headers=headers,
                json=body,
            )
            if err:
                return err
            payload = resp.json() or {}
        notes = payload.get("data") or []
        total = (payload.get("counts") or {}).get("total", len(notes))
        return format_entity_list(
            notes, _fmt_note, noun="analyst note", total=total, empty_msg="No analyst notes found."
        )

    async def get_rf_analyst_note(note_id: str) -> str:
        """Get a single analyst note by id: title, full text, referenced entities, and topic."""
        nid = (note_id or "").strip()
        if not nid:
            return "Error: note_id is required."
        async with client() as c:
            resp, err = await safe_request(
                c,
                "post",
                f"{api}/analyst-note/lookup/{nid}",
                _PROVIDER,
                f"get analyst note {nid}",
                headers=headers,
                json={"serialization": "full"},
            )
            if err:
                return err
            payload = resp.json() or {}
        note = payload.get("data", payload) if isinstance(payload, dict) else {}
        attrs = note.get("attributes") or {}
        source = note.get("source") or {}
        lines = [
            f"Analyst note: {trunc(attrs.get('title'), 200)}",
            f"id: {note.get('id', nid)} | source: {source.get('name', '?')} | "
            f"published: {dt(attrs.get('published'))}",
        ]
        topics = attrs.get("topic")
        if isinstance(topics, list) and topics:
            lines.append(
                "Topics: " + ", ".join(t.get("name", "?") for t in topics if isinstance(t, dict))
            )
        entities = attrs.get("note_entities")
        if isinstance(entities, list) and entities:
            names = ", ".join(e.get("name", "?") for e in entities[:15] if isinstance(e, dict))
            lines.append(f"Entities: {names}")
        if attrs.get("text"):
            lines.append(f"\n{trunc(attrs.get('text'), 1800)}")
        urls = attrs.get("validation_urls")
        if isinstance(urls, list) and urls:
            lines.append(
                "Sources: " + ", ".join(u.get("name", "?") for u in urls[:5] if isinstance(u, dict))
            )
        return "\n".join(lines)

    # ── detection rules ───────────────────────────────────────────────────
    async def search_rf_detection_rules(
        rule_types: str = "",
        entity_ids: str = "",
        include_content: bool = False,
        limit: int = 10,
    ) -> str:
        """Search Insikt Group YARA / Sigma / Snort detection rules.

        Filter by ``rule_types`` (comma-separated yara/sigma/snort) and/or
        ``entity_ids`` (comma-separated RF entity ids - resolve names with
        resolve_rf_entity, e.g. a malware family or CVE) to find rules that
        detect a specific threat. By default returns a discovery summary per
        matching rule set (type, title, RF id, last-updated date, file names);
        set ``include_content=true`` to also return the deployable rule source
        code (truncated per rule) - use a small limit when doing so.
        """
        rfilter: dict[str, Any] = {}
        raw_types = csv_list(rule_types)
        types = [t for t in (s.lower() for s in raw_types) if t in _RULE_TYPES]
        if raw_types and not types:
            return f"Error: rule_types must be a subset of {', '.join(sorted(_RULE_TYPES))}."
        if types:
            rfilter["types"] = types
        ents = csv_list(entity_ids)
        if ents:
            rfilter["entities"] = ents
        body: dict[str, Any] = {"limit": clamp(limit, 10, hi=100)}
        if rfilter:
            body["filter"] = rfilter
        async with client(timeout=60) as c:
            resp, err = await safe_request(
                c,
                "post",
                f"{api}/detection-rule/search",
                _PROVIDER,
                "search detection rules",
                headers=headers,
                json=body,
            )
            if err:
                return err
            payload = resp.json() or {}
        results = payload.get("result") or []
        total = payload.get("total_count", len(results))

        def _fmt(r: dict[str, Any]) -> str:
            sub = r.get("rules") or []
            files = ", ".join(
                s.get("file_name", "?") for s in sub[:3] if isinstance(s, dict)
            )
            out = (
                f"  [{r.get('type', '?')}] {trunc(r.get('title'), 110)}\n"
                f"    id: {r.get('id', '?')} | updated: {dt(r.get('updated'))}"
                + (f" | files: {files}" if files else "")
            )
            if include_content:
                for s in sub[:2]:
                    if not isinstance(s, dict) or not s.get("content"):
                        continue
                    # Rule source is multi-line; keep it verbatim (no trunc()
                    # whitespace collapsing) but bounded.
                    content = str(s.get("content"))
                    if len(content) > 3000:
                        content = content[:3000] + "\n... [rule truncated]"
                    out += f"\n    --- {s.get('file_name', 'rule')} ---\n{content}"
            return out

        out = format_entity_list(
            results, _fmt, noun="detection rule set", total=total, empty_msg="No detection rules found."
        )
        if total and total > len(results):
            out += f"\n[Showing {len(results)} of {total}; raise limit to see more.]"
        return out

    # ── links graph ───────────────────────────────────────────────────────
    async def search_rf_links(
        entity_ids: str, timeframe: str = "-90d", limit: int = 50
    ) -> str:
        """Find validated relationships for entities in the Recorded Future Links graph.

        ``entity_ids`` is a comma-separated list of RF entity ids (resolve names
        with resolve_rf_entity). Use to pivot during an investigation - e.g.
        find infrastructure, malware, or actors linked to a given indicator.
        ``timeframe`` bounds technical links (e.g. '-90d').
        """
        ents = csv_list(entity_ids)
        if not ents:
            return "Error: provide at least one RF entity id in entity_ids."
        body: dict[str, Any] = {
            "entities": ents,
            "filters": {"technical": {"timeframe": timeframe.strip() or "-90d"}},
            "limits": {"per_entity_type": clamp(limit, 50, hi=200)},
        }
        async with client(timeout=60) as c:
            resp, err = await safe_request(
                c,
                "post",
                f"{api}/links/search",
                _PROVIDER,
                "search links",
                headers=headers,
                json=body,
            )
            if err:
                return err
            payload = resp.json() or {}
        data = payload.get("data") or []
        if not data:
            return "No links found for the given entities."
        lines: list[str] = []
        for group in data:
            if not isinstance(group, dict):
                continue
            ent = group.get("entity") or {}
            links = group.get("links") or []
            lines.append(
                f"{ent.get('name', '?')} ({ent.get('type', '?')}) - {len(links)} link(s):"
            )
            for link in links[:15]:
                if not isinstance(link, dict):
                    continue
                lines.append(
                    f"  -> {link.get('name', '?')} ({link.get('type', '?')}) "
                    f"[{link.get('source', '?')}]"
                )
            if len(links) > 15:
                lines.append(f"  ... and {len(links) - 15} more")
        return "\n".join(lines)

    # ── threat actors + threat map ────────────────────────────────────────
    async def search_rf_threat_actors(name: str = "", limit: int = 25) -> str:
        """Search Recorded Future's threat-actor database by name, alias, or classification.

        ``name`` does a case-insensitive fuzzy search of names/aliases; prefix it
        with 'exact:' / 'prefix:' for stricter matching. Returns actor entity ids
        you can enrich with lookup_rf_malware-style lookups or use in
        search_rf_links.
        """
        body: dict[str, Any] = {"limit": clamp(limit, 25, hi=1000)}
        if name.strip():
            body["name"] = name.strip()
        async with client(timeout=45) as c:
            resp, err = await safe_request(
                c,
                "post",
                f"{api}/threat/actor/search",
                _PROVIDER,
                "search threat actors",
                headers=headers,
                json=body,
            )
            if err:
                return err
            payload = resp.json() or {}
        actors = payload.get("data") or []
        total = (payload.get("counts") or {}).get("total", len(actors))

        def _fmt(a: dict[str, Any]) -> str:
            attrs = a.get("attributes") or {}
            aliases = attrs.get("common_names") or attrs.get("alias") or []
            alias_str = ", ".join(str(x) for x in aliases[:4]) if isinstance(aliases, list) else ""
            return (
                f"  {attrs.get('name', a.get('name', '?'))} ({a.get('type', '?')}) "
                f"- id: {a.get('id', '?')}" + (f" | aka {alias_str}" if alias_str else "")
            )

        return format_entity_list(
            actors, _fmt, noun="threat actor", total=total, empty_msg="No threat actors found."
        )

    def _fmt_map_entry(a: dict[str, Any], score_keys: tuple[str, str]) -> str:
        attrs = a.get("attributes") or a
        name = attrs.get("name") or a.get("name", "?")
        scores = []
        for key in score_keys:
            value = a.get(key) if a.get(key) is not None else attrs.get(key)
            if value is not None:
                scores.append(f"{key} {value}")
        score_str = f" ({', '.join(scores)})" if scores else ""
        return f"  {name} - id: {a.get('id', '?')}{score_str}"

    async def _threat_map(
        kind: str, entity_ids: str, category_ids: str, limit: int
    ) -> str:
        # The Threat API's map endpoints are POSTs with an optional filter
        # body; an empty body returns the full map.
        body: dict[str, Any] = {}
        ents = csv_list(entity_ids)
        if ents:
            body["id"] = ents
        cats = csv_list(category_ids)
        if cats:
            body["category_id"] = cats
        async with client(timeout=45) as c:
            resp, err = await safe_request(
                c,
                "post",
                f"{api}/threat/map/{kind}",
                _PROVIDER,
                f"get {kind} threat map",
                headers=headers,
                json=body,
            )
            if err:
                return err
            payload = resp.json() or {}
        entries = payload.get("data") or []
        if isinstance(entries, dict):
            entries = entries.get("threat_map", []) or entries.get("results", [])
        shown = entries[: clamp(limit, 25, hi=100)]
        score_keys = (
            ("intent", "opportunity") if kind == "actors" else ("prevalence", "opportunity")
        )
        noun = "ranked threat actor" if kind == "actors" else "ranked malware family"
        return format_entity_list(
            shown,
            lambda a: _fmt_map_entry(a, score_keys),
            noun=noun,
            total=len(entries),
            empty_msg=(
                "No threat map data available (an org-scoped threat map may "
                "not be configured)."
            ),
        )

    async def get_rf_threat_map(
        actor_ids: str = "", category_ids: str = "", limit: int = 25
    ) -> str:
        """Get your organisation's threat map: threat actors ranked by risk to you.

        Returns the actors most relevant to your primary organisation, ranked by
        Recorded Future's intent and opportunity scoring (0-100 each) - a
        prioritised view of who is most likely to target you. Optionally scope
        with ``actor_ids`` (comma-separated, from search_rf_threat_actors) or
        ``category_ids`` to keep the response small.
        """
        return await _threat_map("actors", actor_ids, category_ids, limit)

    async def get_rf_malware_threat_map(
        malware_ids: str = "", category_ids: str = "", limit: int = 25
    ) -> str:
        """Get your organisation's malware threat map: families ranked by relevance.

        Returns malware families ranked by global prevalence and opportunity to
        target your organisation (0-100 each). Optionally scope with
        ``malware_ids`` (comma-separated RF ids, from resolve_rf_entity) or
        ``category_ids``.
        """
        return await _threat_map("malware", malware_ids, category_ids, limit)

    # ── risk history ──────────────────────────────────────────────────────
    async def get_rf_risk_history(
        entity_ids: str, from_date: str = "", to_date: str = "", max_rules: int = 10
    ) -> str:
        """Get risk score / risk rule change events for entities over time.

        Answers "when did this indicator become malicious and why". Returns
        change events (not daily snapshots): each risk score, criticality
        level, and individual risk rule with the timestamps it became active
        and was replaced. ``entity_ids`` is comma-separated RF entity ids (e.g.
        'ip:1.2.3.4', or resolve names with resolve_rf_entity). Dates are ISO
        (YYYY-MM-DD); the window defaults to the last 30 days.
        """
        ents = csv_list(entity_ids)
        if not ents:
            return "Error: provide at least one RF entity id in entity_ids."
        body: dict[str, Any] = {"entities": ents[:20]}
        if from_date.strip():
            body["from"] = from_date.strip()
        if to_date.strip():
            body["to"] = to_date.strip()
        async with client(timeout=60) as c:
            resp, err = await safe_request(
                c,
                "post",
                f"{api}/risk/history",
                _PROVIDER,
                "search risk history",
                headers=headers,
                json=body,
            )
            if err:
                return err
            payload = resp.json() or {}
        data = payload.get("data") or []
        if not data:
            return "No risk history returned for the given entities."
        cap = clamp(max_rules, 10, hi=50)
        lines: list[str] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            attrs = item.get("attributes") if isinstance(item.get("attributes"), dict) else item
            ent = item.get("entity") or attrs.get("entity") or {}
            lines.append(
                f"{ent.get('name', '?')} ({ent.get('type', '?')}) - id: {ent.get('id', '?')}"
            )
            scores = attrs.get("scores") or []
            if isinstance(scores, list) and scores:
                lines.append("  Risk score changes:")
                for s in scores[-8:]:
                    if not isinstance(s, dict):
                        continue
                    until = dt(s.get("removed")) or "now"
                    lines.append(
                        f"    {s.get('score', '?')} from {dt(s.get('added'))} to {until}"
                    )
            rules = attrs.get("risk_rules") or []
            if isinstance(rules, list) and rules:
                lines.append(f"  Risk rule changes ({len(rules)} total):")
                for r in rules[:cap]:
                    if not isinstance(r, dict):
                        continue
                    until = dt(r.get("removed")) or "active"
                    lines.append(
                        f"    {r.get('rule', r.get('name', '?'))} "
                        f"[{dt(r.get('added'))} -> {until}]"
                    )
                if len(rules) > cap:
                    lines.append(f"    ... and {len(rules) - cap} more rule changes")
        return "\n".join(lines) if lines else "No risk history returned."

    # ── analyst note publishing ───────────────────────────────────────────
    async def publish_rf_analyst_note(
        title: str,
        text: str,
        source: str = "",
        entity_ids: str = "",
        topic: str = "",
    ) -> str:
        """Publish an analyst note to Recorded Future (visible to your organisation).

        Creates and immediately publishes a note with ``title`` and ``text``.
        ``source`` is your organisation's source id (source:...; ask your RF
        admin or reuse the source of an existing enterprise note). Optionally
        attach ``entity_ids`` (comma-separated RF ids the note references -
        resolve names with resolve_rf_entity) and a ``topic`` id. Returns the
        published note id for get_rf_analyst_note.
        """
        title = (title or "").strip()
        text = (text or "").strip()
        if not title or not text:
            return "Error: title and text are both required."
        attributes: dict[str, Any] = {"title": title[:300], "text": text[:50000]}
        ents = csv_list(entity_ids)
        if ents:
            attributes["note_entities"] = ents
        if topic.strip():
            attributes["topic"] = topic.strip()
        body: dict[str, Any] = {"attributes": attributes, "resolve_entities": True}
        if source.strip():
            body["source"] = source.strip()
        async with client(timeout=45) as c:
            resp, err = await safe_request(
                c,
                "post",
                f"{api}/analyst-note/publish",
                _PROVIDER,
                "publish analyst note",
                headers=headers,
                json=body,
            )
            if err:
                return err
            payload = resp.json() or {}
        note_id = payload.get("note_id") or payload.get("document_id") or "?"
        return f"Published analyst note '{trunc(title, 80)}' (id {note_id})."

    # ── malware intelligence (sandbox corpus) ─────────────────────────────
    async def query_rf_malware_intelligence(
        query: str,
        field: str = "sha256",
        start_date: str = "-30d",
        end_date: str = "",
        page: int = 1,
    ) -> str:
        """Search Recorded Future's malware sandbox corpus with a natural-language query.

        Describe what you are hunting in plain English (e.g. 'samples that
        talked to port 5353', 'LummaC2 samples dropping scheduled tasks') and
        Recorded Future translates it into a structured query over its Malware
        Intelligence sandbox data. ``field`` selects what to return (sha256,
        ip_address, domain, url, command_line, ttp, dst_port, signature, ioc,
        tag). Requires the Malware Intelligence module.
        """
        query = (query or "").strip()
        if not query:
            return "Error: query is required."
        field = (field or "sha256").strip().lower()
        if field not in _MI_FIELDS:
            return f"Error: field must be one of {', '.join(sorted(_MI_FIELDS))}."
        body: dict[str, Any] = {
            "query": query,
            "field": field,
            "start_date": start_date.strip() or "-30d",
            "page": max(1, int(page) if str(page).isdigit() else 1),
        }
        if end_date.strip():
            body["end_date"] = end_date.strip()
        async with client(timeout=90) as c:
            resp, err = await safe_request(
                c,
                "post",
                f"{api}/malware-intelligence/v1/query_nl",
                _PROVIDER,
                "query malware intelligence",
                headers=headers,
                json=body,
            )
            if err:
                return err
            payload = resp.json() or {}
        rows = payload.get("data") or payload.get("results") or []
        executed = payload.get("executedQuery") or payload.get("executed_query")
        total_pages = payload.get("totalPages") or payload.get("total_pages")

        def _fmt(row: Any) -> str:
            if isinstance(row, dict):
                value = row.get("value") or row.get(field) or row.get("name")
                count = row.get("count")
                extra = f" ({count} samples)" if count is not None else ""
                return f"  {value}{extra}" if value else f"  {trunc(row, 160)}"
            return f"  {row}"

        out = format_entity_list(
            rows,
            _fmt,
            noun=f"{field} result",
            total=len(rows),
            empty_msg="No sandbox artifacts matched the query.",
        )
        if executed:
            out += f"\n[Executed query: {trunc(executed, 250)}]"
        if total_pages and int(total_pages) > body["page"]:
            out += f"\n[Page {body['page']} of {total_pages}; raise page to see more.]"
        return out

    executor.register("search_rf_analyst_notes", search_rf_analyst_notes)
    executor.register("get_rf_analyst_note", get_rf_analyst_note)
    executor.register("search_rf_detection_rules", search_rf_detection_rules)
    executor.register("search_rf_links", search_rf_links)
    executor.register("search_rf_threat_actors", search_rf_threat_actors)
    executor.register("get_rf_threat_map", get_rf_threat_map)
    executor.register("get_rf_malware_threat_map", get_rf_malware_threat_map)
    executor.register("get_rf_risk_history", get_rf_risk_history)
    executor.register("publish_rf_analyst_note", publish_rf_analyst_note)
    executor.register("query_rf_malware_intelligence", query_rf_malware_intelligence)

    return [
        ToolDefinition(
            name="search_rf_analyst_notes",
            description=(
                "Search Recorded Future analyst notes (Insikt Group finished "
                "intelligence). Filter by published date (absolute or relative "
                "like '-7d'), a referenced entity id, a topic id, a title "
                "substring, or enterprise_only. Returns note ids."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "published": {
                        "type": "string",
                        "default": "-7d",
                        "description": "Published-after date (YYYY-MM-DD or relative like -7d)",
                    },
                    "entity": {
                        "type": "string",
                        "description": "RF entity id that notes must reference",
                    },
                    "topic": {"type": "string", "description": "Topic entity id"},
                    "title": {"type": "string", "description": "Title substring"},
                    "enterprise_only": {
                        "type": "boolean",
                        "default": False,
                        "description": "Exclude Insikt Group notes (only your org's notes)",
                    },
                    "limit": {"type": "integer", "default": 20},
                },
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_rf_analyst_note",
            description=(
                "Get a single Recorded Future analyst note by id: title, full "
                "text, referenced entities, topics, and validation sources."
            ),
            input_schema={
                "type": "object",
                "properties": {"note_id": {"type": "string", "description": "Note id, e.g. doc:vvpzg4"}},
                "required": ["note_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="search_rf_detection_rules",
            description=(
                "Search Insikt Group YARA / Sigma / Snort detection rules. Filter "
                "by rule_types (yara/sigma/snort) and/or entity_ids (RF ids from "
                "resolve_rf_entity, e.g. a malware family or CVE) to find detections "
                "for a specific threat. Returns a summary per rule set (type, title, "
                "RF id, last-updated, file names) for discovery, not the rule source."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "rule_types": {
                        "type": "string",
                        "description": "Comma-separated rule types: yara, sigma, snort",
                    },
                    "entity_ids": {
                        "type": "string",
                        "description": "Comma-separated RF entity ids the rules must be associated with",
                    },
                    "include_content": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "Also return the deployable rule source code "
                            "(truncated); use a small limit with this"
                        ),
                    },
                    "limit": {"type": "integer", "default": 10},
                },
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="search_rf_links",
            description=(
                "Find validated relationships for entities in the Recorded Future "
                "Links graph - pivot from an indicator to linked infrastructure, "
                "malware, or actors during an investigation. entity_ids is "
                "comma-separated RF ids (resolve names with resolve_rf_entity)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "entity_ids": {
                        "type": "string",
                        "description": "Comma-separated RF entity ids to pivot from",
                    },
                    "timeframe": {
                        "type": "string",
                        "default": "-90d",
                        "description": "Bound technical links to this window, e.g. -90d",
                    },
                    "limit": {"type": "integer", "default": 50},
                },
                "required": ["entity_ids"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="search_rf_threat_actors",
            description=(
                "Search Recorded Future's threat-actor database by name, alias, "
                "or classification. Prefix the query with 'exact:' or 'prefix:' "
                "for stricter matching. Returns actor entity ids."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Name/alias to search, e.g. 'Fancy Bear' or 'exact:Lorec53'",
                    },
                    "limit": {"type": "integer", "default": 25},
                },
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_rf_threat_map",
            description=(
                "Get your organisation's Recorded Future threat map: threat "
                "actors ranked by their risk to you (intent x opportunity, "
                "0-100 each) - a prioritised view of who is most likely to "
                "target your org. Optionally scope by actor_ids or "
                "category_ids to keep the response focused. For malware "
                "families use get_rf_malware_threat_map."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "actor_ids": {
                        "type": "string",
                        "description": "Optional comma-separated actor entity ids to scope the map",
                    },
                    "category_ids": {
                        "type": "string",
                        "description": "Optional comma-separated actor category ids",
                    },
                    "limit": {"type": "integer", "default": 25},
                },
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_rf_malware_threat_map",
            description=(
                "Get your organisation's Recorded Future malware threat map: "
                "malware families ranked by global prevalence and opportunity "
                "to target you (0-100 each). Optionally scope by malware_ids "
                "(from resolve_rf_entity) or category_ids. For threat actors "
                "use get_rf_threat_map."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "malware_ids": {
                        "type": "string",
                        "description": "Optional comma-separated malware entity ids to scope the map",
                    },
                    "category_ids": {
                        "type": "string",
                        "description": "Optional comma-separated malware category ids",
                    },
                    "limit": {"type": "integer", "default": 25},
                },
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_rf_risk_history",
            description=(
                "Get Recorded Future risk score and risk rule CHANGE HISTORY "
                "for entities over a date window - answers 'when did this "
                "indicator become malicious and which rules fired when'. "
                "entity_ids is comma-separated RF ids (e.g. 'ip:1.2.3.4'; "
                "resolve names with resolve_rf_entity). Use lookup tools for "
                "the current risk state; use this for the timeline."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "entity_ids": {
                        "type": "string",
                        "description": "Comma-separated RF entity ids (max 20)",
                    },
                    "from_date": {
                        "type": "string",
                        "description": "Window start, ISO date (default: 30 days ago)",
                    },
                    "to_date": {
                        "type": "string",
                        "description": "Window end, ISO date (default: now)",
                    },
                    "max_rules": {
                        "type": "integer",
                        "default": 10,
                        "description": "Max rule-change entries shown per entity",
                    },
                },
                "required": ["entity_ids"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="publish_rf_analyst_note",
            description=(
                "Publish an analyst note to Recorded Future, making it "
                "immediately visible to your organisation and searchable via "
                "search_rf_analyst_notes. Use to push finished intelligence "
                "(investigation findings, triage conclusions) back into the "
                "Intelligence Cloud. Optionally reference RF entities and a "
                "topic. Returns the new note id."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Note title"},
                    "text": {"type": "string", "description": "Note body text"},
                    "source": {
                        "type": "string",
                        "description": "Your organisation's source id (source:...)",
                    },
                    "entity_ids": {
                        "type": "string",
                        "description": (
                            "Optional comma-separated RF entity ids the note "
                            "references (resolve names with resolve_rf_entity)"
                        ),
                    },
                    "topic": {"type": "string", "description": "Optional topic entity id"},
                },
                "required": ["title", "text"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="query_rf_malware_intelligence",
            description=(
                "Search Recorded Future's Malware Intelligence sandbox corpus "
                "with a plain-English query (e.g. 'samples that talked to port "
                "5353', 'LummaC2 samples dropping scheduled tasks'). field "
                "selects what to return (sha256, ip_address, domain, url, "
                "command_line, ttp, dst_port, signature, ioc, tag). Requires "
                "the Malware Intelligence module."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Natural-language hunt query",
                    },
                    "field": {
                        "type": "string",
                        "enum": sorted(_MI_FIELDS),
                        "default": "sha256",
                    },
                    "start_date": {
                        "type": "string",
                        "default": "-30d",
                        "description": "Window start (ISO date or relative like -30d)",
                    },
                    "end_date": {
                        "type": "string",
                        "description": "Optional window end (ISO date)",
                    },
                    "page": {"type": "integer", "default": 1},
                },
                "required": ["query"],
            },
            source=_SOURCE,
            priority=0,
        ),
    ]
