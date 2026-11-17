"""Recorded Future list management, detection sharing, and identity tools.

* ``manage_rf_list`` - consolidated List API (``/list``) management: create a
  list, search lists, read info/status/entities, and add/remove entities
  (IOC watchlists / block lists - "creating intelligence").
* ``submit_rf_collective_insights`` - push a detection from your stack back into
  Recorded Future Collective Insights (``/collective-insights/detections``).
* ``search_rf_collective_insights`` - query the org's enriched detection
  history across all Collective Insights sources
  (``/collective-insights/search``).
* ``search_rf_leaked_credentials`` - Identity Intelligence (``/identity``):
  find compromised identities for your domains across breaches and stealer logs.
* ``lookup_rf_identity_exposures`` - full exposure history for specific email
  addresses (``/identity/credentials/lookup``).
* ``check_rf_password_exposure`` - k-anonymity password-hash exposure check
  (``/identity/password/lookup``).
"""

from __future__ import annotations

from typing import Any

from app.integrations.http import format_entity_list, safe_request
from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import _PROVIDER, _SOURCE, clamp, client, csv_list, dt, trunc

_LIST_ACTIONS = frozenset(
    {"create", "search", "info", "status", "entities", "add", "remove"}
)
# List API entity types eligible for type+name auto-resolution on add/remove.
_RESOLVABLE_TYPES = frozenset(
    {"IpAddress", "InternetDomainName", "Hash", "CyberVulnerability"}
)
_IOC_TYPES = frozenset({"ip", "domain", "hash", "url", "vulnerability"})
_DETECTION_TYPES = frozenset({"detection_rule", "correlation", "playbook", "sandbox"})
_DOMAIN_TYPES = frozenset({"Email", "Authorization"})
# Password hash algorithms accepted by the Identity password lookup.
_HASH_ALGORITHMS = frozenset(
    {"SHA1", "SHA256", "MD5", "NTLM", "BCRYPT"}
)


def register(
    api: str, headers: dict[str, str], executor: CallableToolExecutor
) -> list[ToolDefinition]:
    """Register list / collective-insights / identity tools and return defs."""

    # ── consolidated list management ──────────────────────────────────────
    def _entity_payload(
        entity_id: str, entity_type: str, entity_name: str
    ) -> dict[str, Any] | str:
        if entity_id.strip():
            return {"id": entity_id.strip()}
        if entity_type.strip() and entity_name.strip():
            if entity_type.strip() not in _RESOLVABLE_TYPES:
                return (
                    "Error: type+name auto-resolution only works for "
                    f"{', '.join(sorted(_RESOLVABLE_TYPES))}. For other types, "
                    "resolve the name with resolve_rf_entity and pass entity_id."
                )
            return {"type": entity_type.strip(), "name": entity_name.strip()}
        return "Error: provide entity_id, or both entity_type and entity_name."

    async def manage_rf_list(
        action: str,
        list_id: str = "",
        name: str = "",
        list_type: str = "entity",
        entity_id: str = "",
        entity_type: str = "",
        entity_name: str = "",
        context_note: str = "",
        limit: int = 25,
    ) -> str:
        """Manage Recorded Future lists (IOC watchlists / block lists).

        action='create': create a list (needs name; list_type one of entity, ip,
          domain, hash, vulnerability, company, attacker, executive, source,
          text) and returns its id.
        action='search': find lists (optional name / list_type filter).
        action='info' / 'status' / 'entities': read a list (needs list_id).
        action='add' / 'remove': add or remove an entity (needs list_id and
          either entity_id, or entity_type + entity_name where entity_type is an
          RF canonical type - IpAddress, InternetDomainName, Hash, or
          CyberVulnerability). context_note (add only) is stored as the
          UI-visible annotation.
        """
        action = (action or "").strip().lower()
        if action not in _LIST_ACTIONS:
            return f"Error: action must be one of {', '.join(sorted(_LIST_ACTIONS))}."

        async with client() as c:
            if action == "create":
                if not name.strip():
                    return "Error: name is required to create a list."
                resp, err = await safe_request(
                    c,
                    "post",
                    f"{api}/list/create",
                    _PROVIDER,
                    "create list",
                    headers=headers,
                    json={"name": name.strip(), "type": list_type.strip() or "entity"},
                )
                if err:
                    return err
                created = resp.json() or {}
                return (
                    f"Created list '{created.get('name', name)}' "
                    f"(id {created.get('id', '?')}, type {created.get('type', '?')})."
                )

            if action == "search":
                body: dict[str, Any] = {"limit": clamp(limit, 25, hi=100)}
                if name.strip():
                    body["name"] = name.strip()
                if list_type.strip():
                    body["type"] = list_type.strip()
                resp, err = await safe_request(
                    c,
                    "post",
                    f"{api}/list/search",
                    _PROVIDER,
                    "search lists",
                    headers=headers,
                    json=body,
                )
                if err:
                    return err
                payload = resp.json() or []
                lists = payload if isinstance(payload, list) else payload.get("data", [])

                def _fmt(li: dict[str, Any]) -> str:
                    return (
                        f"  {li.get('name', '?')} - id: {li.get('id', '?')} "
                        f"(type {li.get('type', '?')}, updated {dt(li.get('updated'))})"
                    )

                return format_entity_list(
                    lists, _fmt, noun="list", empty_msg="No lists found."
                )

            if action in ("info", "status"):
                if not list_id.strip():
                    return f"Error: list_id is required for action='{action}'."
                resp, err = await safe_request(
                    c,
                    "get",
                    f"{api}/list/{list_id.strip()}/{action}",
                    _PROVIDER,
                    f"get list {action}",
                    headers=headers,
                )
                if err:
                    return err
                data = resp.json() or {}
                if action == "info":
                    return (
                        f"List '{data.get('name', '?')}' (id {data.get('id', list_id)}): "
                        f"type {data.get('type', '?')}, owner {data.get('owner_name', '?')}, "
                        f"created {dt(data.get('created'))}, updated {dt(data.get('updated'))}."
                    )
                return (
                    f"List {list_id} status: {data.get('status', '?')} | "
                    f"entities: {data.get('size', data.get('entity_count', '?'))}"
                )

            if action == "entities":
                if not list_id.strip():
                    return "Error: list_id is required for action='entities'."
                resp, err = await safe_request(
                    c,
                    "get",
                    f"{api}/list/{list_id.strip()}/entities",
                    _PROVIDER,
                    "get list entities",
                    headers=headers,
                )
                if err:
                    return err
                payload = resp.json() or []
                items = payload if isinstance(payload, list) else payload.get("entities", [])
                shown = items[: clamp(limit, 25, hi=200)]

                def _fmt(it: dict[str, Any]) -> str:
                    ent = it.get("entity") or it
                    return f"  {ent.get('name', ent.get('id', '?'))} ({ent.get('type', '?')})"

                return format_entity_list(
                    shown, _fmt, noun="list entity", total=len(items), empty_msg="List is empty."
                )

            # add / remove
            if not list_id.strip():
                return f"Error: list_id is required for action='{action}'."
            entity = _entity_payload(entity_id, entity_type, entity_name)
            if isinstance(entity, str):
                return entity  # validation error
            body = {"entity": entity}
            if action == "add" and context_note.strip():
                body["context"] = {"annotation": context_note.strip()}
            resp, err = await safe_request(
                c,
                "post",
                f"{api}/list/{list_id.strip()}/entity/{action}",
                _PROVIDER,
                f"{action} list entity",
                headers=headers,
                json=body,
            )
            if err:
                return err
            result = (resp.json() or {}).get("result", "ok")
            target = entity.get("id") or entity.get("name")
            verb = "added to" if action == "add" else "removed from"
            return f"Entity {target} {verb} list {list_id} (result: {result})."

    # ── collective insights ───────────────────────────────────────────────
    async def submit_rf_collective_insights(
        ioc_type: str,
        ioc_value: str,
        detection_name: str,
        detection_type: str = "correlation",
        rule_sub_type: str = "",
        mitre_codes: str = "",
        malwares: str = "",
        debug: bool = False,
    ) -> str:
        """Submit a detection from your security stack to Recorded Future Collective Insights.

        Shares that you detected an IOC (ip / domain / hash / url / vulnerability)
        so it enriches your enterprise threat intelligence. ``detection_type`` is
        one of detection_rule / correlation / playbook / sandbox (when
        detection_rule, set ``rule_sub_type`` to sigma/yara/snort). Optionally
        attach comma-separated MITRE ATT&CK codes and malware names. Set
        ``debug=true`` to validate without persisting.
        """
        ioc_type = (ioc_type or "").strip().lower()
        if ioc_type not in _IOC_TYPES:
            return f"Error: ioc_type must be one of {', '.join(sorted(_IOC_TYPES))}."
        if not ioc_value.strip():
            return "Error: ioc_value is required."
        detection_type = (detection_type or "correlation").strip().lower()
        if detection_type not in _DETECTION_TYPES:
            return f"Error: detection_type must be one of {', '.join(sorted(_DETECTION_TYPES))}."
        detection: dict[str, Any] = {
            "type": detection_type,
            "name": detection_name.strip() or "XTM One detection",
        }
        if detection_type == "detection_rule" and rule_sub_type.strip().lower() in {
            "sigma",
            "yara",
            "snort",
        }:
            detection["sub_type"] = rule_sub_type.strip().lower()
        record: dict[str, Any] = {
            "ioc": {"type": ioc_type, "value": ioc_value.strip()},
            "detection": detection,
        }
        mitre = csv_list(mitre_codes)
        if mitre:
            record["mitre_codes"] = mitre
        mal = csv_list(malwares)
        if mal:
            record["malwares"] = mal
        body = {"data": [record], "options": {"debug": bool(debug), "summary": True}}
        async with client() as c:
            resp, err = await safe_request(
                c,
                "post",
                f"{api}/collective-insights/detections",
                _PROVIDER,
                "submit collective insights detection",
                headers=headers,
                json=body,
            )
            if err:
                return err
            payload = (resp.json() or {}).get("result", {})
        processed = (payload.get("summary") or {}).get("processed") or {}
        mode = " (debug - not persisted)" if debug else ""
        return (
            f"Submitted detection for {ioc_type} {ioc_value.strip()} to Collective "
            f"Insights{mode}. Status: {payload.get('status', 'OK')}; "
            f"processed: {processed or 'n/a'}."
        )

    # ── identity intelligence ─────────────────────────────────────────────
    async def search_rf_leaked_credentials(
        domains: str, domain_types: str = "Email", limit: int = 50
    ) -> str:
        """Find compromised identities for your domains (Identity Intelligence).

        Enumerates exposed identities across breach data and stealer-malware logs
        for one or more ``domains`` (comma-separated). ``domain_types`` is a
        comma-separated subset of Email (addresses at the domain) and
        Authorization (logins for portals on the domain). Requires an Identity
        Intelligence subscription.
        """
        doms = csv_list(domains)
        if not doms:
            return "Error: provide at least one domain in domains."
        # Match case-insensitively but send RF's canonical casing. Reject a
        # non-empty domain_types whose values are all invalid instead of silently
        # querying Email - a typo like "email" should fail loudly, not return
        # unintended results. An empty/blank domain_types still defaults to Email.
        _canon = {t.lower(): t for t in _DOMAIN_TYPES}
        raw_types = csv_list(domain_types)
        dtypes = list(dict.fromkeys(_canon[t.lower()] for t in raw_types if t.lower() in _canon))
        if raw_types and not dtypes:
            return (
                "Error: domain_types must be a comma-separated subset of "
                f"{', '.join(sorted(_DOMAIN_TYPES))}."
            )
        dtypes = dtypes or ["Email"]
        body = {
            "domains": doms[:100],
            "domain_types": dtypes,
            "limit": clamp(limit, 50, hi=1000),
        }
        async with client(timeout=60) as c:
            resp, err = await safe_request(
                c,
                "post",
                f"{api}/identity/credentials/search",
                _PROVIDER,
                "search leaked credentials",
                headers=headers,
                json=body,
            )
            if err:
                return err
            payload = resp.json() or {}
        identities = payload.get("identities") or []
        count = payload.get("count", len(identities))

        def _fmt(item: Any) -> str:
            if isinstance(item, str):
                return f"  {item}"
            login = item.get("login", "?")
            domain = item.get("domain", "")
            return f"  {login}" + (f" @ {domain}" if domain else "")

        out = format_entity_list(
            identities,
            _fmt,
            noun="exposed identity",
            total=count,
            empty_msg="No compromised identities found for these domains.",
        )
        if payload.get("next_offset"):
            out += "\n[More results available; narrow the domains or raise limit.]"
        return out

    async def search_rf_collective_insights(
        detection_type: str = "",
        malware_ids: str = "",
        threat_actor_ids: str = "",
        mitre_codes: str = "",
        days: int = 30,
        limit: int = 25,
    ) -> str:
        """Search your organisation's Collective Insights detection history.

        Queries enriched detection events across all sources (your own
        submissions, Recorded Future integrations, sandbox). Filter by
        ``detection_type`` (detection_rule / correlation / playbook / sandbox),
        associated ``malware_ids`` / ``threat_actor_ids`` (RF entity ids from
        resolve_rf_entity), ``mitre_codes`` (e.g. 'T1059'), and a lookback
        window in ``days``.
        """
        detection_type = detection_type.strip().lower()
        if detection_type and detection_type not in _DETECTION_TYPES:
            return f"Error: detection_type must be one of {', '.join(sorted(_DETECTION_TYPES))}."
        filters: dict[str, Any] = {}
        if detection_type:
            filters["detection_type"] = [detection_type]
        threats: dict[str, Any] = {}
        mal = csv_list(malware_ids)
        if mal:
            threats["malware"] = {"id": mal}
        actors = csv_list(threat_actor_ids)
        if actors:
            threats["threat_actor"] = {"id": actors}
        mitre = csv_list(mitre_codes)
        if mitre:
            threats["mitre_code"] = {
                "id": [
                    c if c.lower().startswith("mitre:") else f"mitre:{c}"
                    for c in mitre
                ]
            }
        if threats:
            filters["associated_threats"] = threats
        try:
            lookback = max(1, min(int(days), 365))
        except (TypeError, ValueError):
            lookback = 30
        filters["detection_time"] = {"from": f"-{lookback}d"}
        body: dict[str, Any] = {
            "filters": filters,
            "limit": clamp(limit, 25, hi=100),
        }
        async with client(timeout=45) as c:
            resp, err = await safe_request(
                c,
                "post",
                f"{api}/collective-insights/search",
                _PROVIDER,
                "search collective insights",
                headers=headers,
                json=body,
            )
            if err:
                return err
            payload = resp.json() or {}
        events = payload.get("data") or []
        if isinstance(events, dict):
            events = events.get("events", []) or events.get("results", [])
        total = (payload.get("counts") or {}).get("total", len(events))

        def _fmt(ev: dict[str, Any]) -> str:
            ioc = ev.get("ioc") or {}
            detection = ev.get("detection") or {}
            risk = ev.get("risk") or {}
            score = (risk.get("at_detection") or {}).get("score") if isinstance(
                risk.get("at_detection"), dict
            ) else risk.get("score")
            score_part = f" | risk at detection: {score}" if score is not None else ""
            return (
                f"  {ioc.get('type', '?')} {ioc.get('value', '?')} - "
                f"{detection.get('type', ev.get('detection_type', '?'))}"
                f" '{trunc(detection.get('name', ''), 60)}'"
                f" @ {dt(ev.get('detection_time'))}{score_part}"
            )

        out = format_entity_list(
            events,
            _fmt,
            noun="detection event",
            total=total,
            empty_msg="No Collective Insights detection events matched the filters.",
        )
        if payload.get("next_offset"):
            out += "\n[More events available; narrow the filters or raise limit.]"
        return out

    # ── identity intelligence: specific-identity lookup ───────────────────
    async def lookup_rf_identity_exposures(subjects: str, limit: int = 20) -> str:
        """Get the full exposure history for specific email addresses.

        ``subjects`` is a comma-separated list of email addresses (max 50).
        Returns each identity's compromised credential records: breach dump /
        stealer-malware source, password hash algorithm, the authorization
        service targeted, and the responsible malware family where known. Use
        search_rf_leaked_credentials for domain-wide sweeps. Requires the
        Identity Intelligence module.
        """
        emails = csv_list(subjects)
        if not emails:
            return "Error: provide at least one email address in subjects."
        body = {
            "subjects": emails[:50],
            "limit": clamp(limit, 20, hi=100),
        }
        async with client(timeout=60) as c:
            resp, err = await safe_request(
                c,
                "post",
                f"{api}/identity/credentials/lookup",
                _PROVIDER,
                "lookup identity exposures",
                headers=headers,
                json=body,
            )
            if err:
                return err
            payload = resp.json() or {}
        identities = payload.get("identities") or []
        if not identities:
            return "No exposures found for these identities."
        lines: list[str] = []
        for ident in identities:
            if not isinstance(ident, dict):
                continue
            subject = ident.get("identity") or {}
            label = (
                subject.get("subjects") if isinstance(subject, dict) else None
            ) or ident.get("subject") or "?"
            if isinstance(label, list):
                label = ", ".join(str(x) for x in label[:3])
            creds = ident.get("credentials") or []
            lines.append(f"{label} - {len(creds)} exposed credential(s):")
            for cred in creds[:8]:
                if not isinstance(cred, dict):
                    continue
                dumps = cred.get("dumps") or []
                dump_names = ", ".join(
                    d.get("name", "?") for d in dumps[:3] if isinstance(d, dict)
                )
                secret = cred.get("exposed_secret") or {}
                details = secret.get("details") or {}
                stype = details.get("type") or secret.get("type") or ""
                svc = cred.get("authorization_service") or {}
                svc_part = f" @ {svc.get('domain')}" if svc.get("domain") else ""
                malware = cred.get("malware_family") or {}
                mal_part = (
                    f" [stealer: {malware.get('name')}]" if malware.get("name") else ""
                )
                lines.append(
                    f"  - {stype or 'credential'}{svc_part}"
                    + (f" | dumps: {dump_names}" if dump_names else "")
                    + mal_part
                )
            if len(creds) > 8:
                lines.append(f"  ... and {len(creds) - 8} more")
        if payload.get("next_offset"):
            lines.append("[More results available; raise limit.]")
        return "\n".join(lines)

    # ── identity intelligence: password exposure check ────────────────────
    async def check_rf_password_exposure(
        hashes: str, algorithm: str = "SHA256"
    ) -> str:
        """Check whether password hashes have appeared in breaches or stealer logs.

        ``hashes`` is a comma-separated list of password hashes (max 100).
        Pass full hashes, or 5-character hash prefixes for a privacy-preserving
        k-anonymity check. ``algorithm`` is the hash algorithm (SHA1 / SHA256 /
        MD5 / NTLM / BCRYPT). Returns each password's exposure status (Common /
        ExposedMultipleTimes / ExposedOnce / NeverExposed). Never send
        plaintext passwords. Requires the Identity Intelligence module.
        """
        values = csv_list(hashes)
        if not values:
            return "Error: provide at least one password hash in hashes."
        algorithm = (algorithm or "SHA256").strip().upper()
        if algorithm not in _HASH_ALGORITHMS:
            return f"Error: algorithm must be one of {', '.join(sorted(_HASH_ALGORITHMS))}."
        passwords = []
        for value in values[:100]:
            # 5-char values are k-anonymity prefixes; longer values are full hashes.
            key = "hash_prefix" if len(value) == 5 else "hash"
            passwords.append({"algorithm": algorithm, key: value})
        async with client(timeout=45) as c:
            resp, err = await safe_request(
                c,
                "post",
                f"{api}/identity/password/lookup",
                _PROVIDER,
                "check password exposure",
                headers=headers,
                json={"passwords": passwords},
            )
            if err:
                return err
            payload = resp.json() or {}
        results = payload.get("results") or []

        def _fmt(item: dict[str, Any]) -> str:
            pw = item.get("password") or {}
            ref = pw.get("hash") or pw.get("hash_prefix") or "?"
            return f"  {ref} ({pw.get('algorithm', algorithm)}): {item.get('exposure_status', '?')}"

        return format_entity_list(
            results,
            _fmt,
            noun="password check",
            total=len(results),
            empty_msg="No results returned for the submitted hashes.",
        )

    executor.register("manage_rf_list", manage_rf_list)
    executor.register("submit_rf_collective_insights", submit_rf_collective_insights)
    executor.register("search_rf_collective_insights", search_rf_collective_insights)
    executor.register("search_rf_leaked_credentials", search_rf_leaked_credentials)
    executor.register("lookup_rf_identity_exposures", lookup_rf_identity_exposures)
    executor.register("check_rf_password_exposure", check_rf_password_exposure)

    return [
        ToolDefinition(
            name="manage_rf_list",
            description=(
                "Manage Recorded Future lists (IOC watchlists / block lists). "
                "action='create' (needs name, list_type) returns a list id; "
                "'search' finds lists; 'info'/'status'/'entities' read a list "
                "(need list_id); 'add'/'remove' change membership (need list_id "
                "and entity_id, or entity_type+entity_name where entity_type is "
                "IpAddress/InternetDomainName/Hash/CyberVulnerability). "
                "context_note is stored as the UI annotation on add."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": sorted(_LIST_ACTIONS),
                    },
                    "list_id": {"type": "string", "description": "List id (report:...)"},
                    "name": {"type": "string", "description": "List name (create/search)"},
                    "list_type": {
                        "type": "string",
                        "default": "entity",
                        "description": "List type, e.g. entity, ip, domain, hash, vulnerability",
                    },
                    "entity_id": {"type": "string", "description": "RF entity id (add/remove)"},
                    "entity_type": {
                        "type": "string",
                        "description": "RF type for add/remove by name (IpAddress/InternetDomainName/Hash/CyberVulnerability)",
                    },
                    "entity_name": {"type": "string", "description": "Entity value for add/remove by name"},
                    "context_note": {
                        "type": "string",
                        "description": "Annotation stored with the entity (add only)",
                    },
                    "limit": {"type": "integer", "default": 25},
                },
                "required": ["action"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="submit_rf_collective_insights",
            description=(
                "Submit a detection from your security stack to Recorded Future "
                "Collective Insights, sharing that you detected an IOC (ip / "
                "domain / hash / url / vulnerability) with detection context. "
                "Optionally attach MITRE ATT&CK codes and malware names. Use "
                "debug=true to validate without persisting."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "ioc_type": {"type": "string", "enum": sorted(_IOC_TYPES)},
                    "ioc_value": {"type": "string", "description": "The detected IOC value"},
                    "detection_name": {
                        "type": "string",
                        "description": "Name of the detection rule / use case that fired",
                    },
                    "detection_type": {
                        "type": "string",
                        "enum": sorted(_DETECTION_TYPES),
                        "default": "correlation",
                    },
                    "rule_sub_type": {
                        "type": "string",
                        "description": "sigma/yara/snort (only when detection_type=detection_rule)",
                    },
                    "mitre_codes": {
                        "type": "string",
                        "description": "Comma-separated MITRE ATT&CK technique codes",
                    },
                    "malwares": {
                        "type": "string",
                        "description": "Comma-separated related malware family names",
                    },
                    "debug": {
                        "type": "boolean",
                        "default": False,
                        "description": "Validate without persisting",
                    },
                },
                "required": ["ioc_type", "ioc_value", "detection_name"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="search_rf_collective_insights",
            description=(
                "Search your organisation's Recorded Future Collective "
                "Insights detection history - enriched IOC detection events "
                "from your own submissions, RF integrations, and sandbox. "
                "Filter by detection_type, associated malware / threat actor "
                "entity ids, MITRE ATT&CK codes, and a lookback window. Use "
                "submit_rf_collective_insights to add new detections."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "detection_type": {
                        "type": "string",
                        "enum": sorted(_DETECTION_TYPES),
                        "description": "Optional detection method filter",
                    },
                    "malware_ids": {
                        "type": "string",
                        "description": "Comma-separated RF malware entity ids",
                    },
                    "threat_actor_ids": {
                        "type": "string",
                        "description": "Comma-separated RF threat actor entity ids",
                    },
                    "mitre_codes": {
                        "type": "string",
                        "description": "Comma-separated MITRE ATT&CK codes, e.g. T1059",
                    },
                    "days": {
                        "type": "integer",
                        "default": 30,
                        "description": "Lookback window in days (max 365)",
                    },
                    "limit": {"type": "integer", "default": 25},
                },
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="search_rf_leaked_credentials",
            description=(
                "Find compromised identities for your domains via Recorded Future "
                "Identity Intelligence (breach data + stealer-malware logs). "
                "domains is comma-separated; domain_types is a subset of Email and "
                "Authorization. Requires an Identity Intelligence subscription."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "domains": {
                        "type": "string",
                        "description": "Comma-separated domains to check",
                    },
                    "domain_types": {
                        "type": "string",
                        "default": "Email",
                        "description": "Comma-separated: Email and/or Authorization",
                    },
                    "limit": {"type": "integer", "default": 50},
                },
                "required": ["domains"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="lookup_rf_identity_exposures",
            description=(
                "Get the full Recorded Future Identity Intelligence exposure "
                "history for SPECIFIC email addresses: breach dumps, stealer-"
                "malware logs, targeted authorization services, and password "
                "hash metadata per identity. Use search_rf_leaked_credentials "
                "for domain-wide sweeps. Requires the Identity Intelligence "
                "module."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "subjects": {
                        "type": "string",
                        "description": "Comma-separated email addresses (max 50)",
                    },
                    "limit": {"type": "integer", "default": 20},
                },
                "required": ["subjects"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="check_rf_password_exposure",
            description=(
                "Check whether password HASHES have appeared in breach data or "
                "stealer logs (Recorded Future Identity Intelligence). Submit "
                "full hashes or 5-character prefixes (privacy-preserving "
                "k-anonymity). Returns Common / ExposedMultipleTimes / "
                "ExposedOnce / NeverExposed per hash. Never send plaintext "
                "passwords. Requires the Identity Intelligence module."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "hashes": {
                        "type": "string",
                        "description": (
                            "Comma-separated password hashes, or 5-char hash "
                            "prefixes for k-anonymity lookups (max 100)"
                        ),
                    },
                    "algorithm": {
                        "type": "string",
                        "enum": sorted(_HASH_ALGORITHMS),
                        "default": "SHA256",
                    },
                },
                "required": ["hashes"],
            },
            source=_SOURCE,
            priority=0,
        ),
    ]
