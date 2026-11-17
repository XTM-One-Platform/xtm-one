"""Intel 471 alert + watcher-management tools (the write surface).

Watchers are Intel 471's standing monitors: a watcher group holds one or more
watchers, each a ``search`` (free-text or typed pattern + filters over chosen
data sets) or a ``thread`` monitor (a specific forum thread). Matching activity
lands in the alert feed. This module lets an agent set up and maintain
autonomous underground monitoring and consume the resulting alerts:

* ``list_intel471_alerts`` - the watcher-driven alert feed (UID-based paging,
  optional mark-as-read, watcher-group filter).
* ``list_intel471_watcher_groups`` / ``get_intel471_watcher_group`` - the
  configured monitors (group + its watchers).
* ``create_intel471_watcher_group`` - create a new group (write).
* ``create_intel471_watcher`` - add a watcher to a group (write).
* ``manage_intel471_watchers`` - the consolidated long tail: update_group,
  delete_group (destructive), get_watcher, update_watcher, delete_watcher
  (destructive).

Reads are priority 0, creates priority 1, and the destructive actions inside
``manage_intel471_watchers`` make it priority 2.
"""

from __future__ import annotations

from typing import Any

from app.integrations.http import format_entity_list
from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import (
    _SOURCE,
    clamp_count,
    extract_items,
    ms_to_dt,
    safe_seg,
    titan_get,
    titan_request,
    trunc,
)

# Consolidated long-tail actions -> (HTTP method, whether it is destructive).
_WATCHER_ACTIONS: dict[str, tuple[str, bool]] = {
    "update_group": ("put", False),
    "delete_group": ("delete", True),
    "get_watcher": ("get", False),
    "update_watcher": ("put", False),
    "delete_watcher": ("delete", True),
}

_WATCHER_TYPES = ("search", "thread")

# Valid values for the watcher-group listing + watcher notification enums
# (mirrors the ToolDefinition schemas so a hallucinated value gets a clear
# error instead of an opaque Titan 4xx).
_GROUP_SECTIONS = ("myGroups", "sharedGroups")
_NOTIFICATION_CHANNELS = ("website", "email")
_NOTIFICATION_FREQUENCIES = ("immediate", "daily")


def register(
    headers: dict[str, str], executor: CallableToolExecutor
) -> list[ToolDefinition]:
    """Register the alert + watcher tools and return their definitions."""

    # --- Alerts ----------------------------------------------------------
    async def list_intel471_alerts(
        watcher_group: str = "",
        show_read: bool = True,
        mark_as_read: bool = False,
        from_time: str = "",
        until_time: str = "",
        offset: str = "",
        count: int = 10,
        sort: str = "latest",
    ) -> str:
        """List the watcher-driven alert feed (hits from your standing monitors).

        Each alert is a report/post/message/etc. that matched a watcher. Filter
        to one ``watcher_group`` uid, hide already-read alerts with
        ``show_read=False``, and page with ``offset`` = the UID of the last alert
        you already processed (NOT a numeric offset). Set ``mark_as_read=True``
        to mark the returned alerts read (a lightweight write). Set up watchers
        with create_intel471_watcher_group / create_intel471_watcher first.
        """
        params: dict[str, Any] = {
            "count": clamp_count(count),
            "showRead": "true" if show_read else "false",
            "sort": sort if sort in ("latest", "earliest") else "latest",
        }
        if mark_as_read:
            params["markAsRead"] = "true"
        if watcher_group.strip():
            params["watcherGroup"] = watcher_group.strip()
        if offset.strip():
            params["offset"] = offset.strip()
        if from_time.strip():
            params["from"] = from_time.strip()
        if until_time.strip():
            params["until"] = until_time.strip()
        payload, err = await titan_get(headers, "/alerts", "list alerts", params=params)
        if err:
            return err
        items = extract_items(payload, "alerts")

        def _fmt(obj: dict[str, Any]) -> str:
            report = obj.get("report") if isinstance(obj.get("report"), dict) else {}
            subject = report.get("subject") or report.get("title") or obj.get("subject") or "(alert)"
            uid = obj.get("uid") or obj.get("id") or "?"
            when = ms_to_dt(obj.get("foundTime") or obj.get("date") or report.get("created"))
            group = obj.get("watcherGroupUid") or ""
            meta = " | ".join(p for p in (when, f"group: {group}" if group else "") if p)
            tail = f"\n    {meta}" if meta else ""
            return f"  [{uid}] {trunc(subject, 140)}{tail}"

        out = format_entity_list(
            items, _fmt, noun="alert", total=len(items),
            empty_msg="No alerts (no watcher hits for the current filters).",
        )
        if items:
            last_uid = items[-1].get("uid") or items[-1].get("id")
            if last_uid:
                out += f'\n[For the next page pass offset="{last_uid}" (the last alert UID).]'
        return out

    # --- Watcher groups (read) ------------------------------------------
    async def list_intel471_watcher_groups(section: str = "myGroups") -> str:
        """List the watcher groups (standing monitors) on the account.

        ``section`` chooses which groups: 'myGroups' (groups you own, the
        default) or 'sharedGroups'. Returns each group's uid + name; feed a uid
        to get_intel471_watcher_group for its watchers.
        """
        section = section.strip() or "myGroups"
        if section not in _GROUP_SECTIONS:
            return f"Error: section must be one of {', '.join(_GROUP_SECTIONS)}."
        params = {"section": section}
        payload, err = await titan_get(
            headers, "/watcherGroups", "list watcher groups", params=params
        )
        if err:
            return err
        items = payload if isinstance(payload, list) else extract_items(payload, "watcherGroups")
        items = [i for i in items if isinstance(i, dict)]

        def _fmt(obj: dict[str, Any]) -> str:
            name = obj.get("name") or "?"
            wtype = obj.get("groupType") or obj.get("type") or ""
            tail = f" ({wtype})" if wtype else ""
            return f"  {name}{tail} (uid: {obj.get('uid', '?')})"

        return format_entity_list(
            items, _fmt, noun="watcher group", total=len(items),
            empty_msg="No watcher groups found.",
        )

    async def get_intel471_watcher_group(group_uid: str) -> str:
        """Get a watcher group and its watchers by uid (from list_intel471_watcher_groups)."""
        if not safe_seg(group_uid):
            return "Error: 'group_uid' must be a plain group uid (no path separators)."
        group_uid = group_uid.strip()
        payload, err = await titan_get(
            headers, f"/watcherGroups/{group_uid}", f"get watcher group {group_uid}",
            expected_statuses={404},
        )
        if err:
            return f"No watcher group found with uid '{group_uid}'." if _is_404(payload) else err
        group = payload if isinstance(payload, dict) else {}
        name = group.get("name") or "?"
        lines = [f"Watcher group: {name} (uid: {group_uid})"]
        if group.get("description"):
            lines.append(f"  {trunc(group.get('description'), 200)}")
        # Fetch the watchers in the group.
        wpayload, werr = await titan_get(
            headers, f"/watcherGroups/{group_uid}/watchers",
            f"list watchers in {group_uid}", expected_statuses={404},
        )
        if not werr:
            watchers = wpayload if isinstance(wpayload, list) else extract_items(wpayload, "watchers")
            watchers = [w for w in watchers if isinstance(w, dict)]
            if watchers:
                lines.append(f"Watchers ({len(watchers)}):")
                for w in watchers[:25]:
                    lines.append(f"  - {_watcher_summary(w)}")
                if len(watchers) > 25:
                    lines.append(f"  ... and {len(watchers) - 25} more watchers not shown.")
            else:
                lines.append("Watchers: none")
        return "\n".join(lines)

    # --- Watcher groups + watchers (write) ------------------------------
    async def create_intel471_watcher_group(name: str, description: str = "") -> str:
        """Create a new watcher group (a container for standing monitors) (WRITE).

        ``name`` is required. After creating the group, add monitors to it with
        create_intel471_watcher. Returns the new group's uid.
        """
        name = (name or "").strip()
        if not name:
            return "Error: 'name' is required to create a watcher group."
        body: dict[str, Any] = {"name": name}
        if description.strip():
            body["description"] = description.strip()
        payload, err = await titan_request(
            headers, "post", "/watcherGroups", "create watcher group", json=body
        )
        if err:
            return err
        group = payload if isinstance(payload, dict) else {}
        return (
            f"Created watcher group '{name}' (uid: {group.get('uid', '?')}). "
            "Add monitors with create_intel471_watcher."
        )

    async def create_intel471_watcher(
        group_uid: str,
        watcher_type: str = "search",
        free_text_pattern: str = "",
        data_sets: str = "",
        thread_uid: str = "",
        thread_type: str = "",
        description: str = "",
        notification_channel: str = "website",
        notification_frequency: str = "",
    ) -> str:
        """Add a watcher (standing monitor) to a group (WRITE).

        ``watcher_type`` is 'search' (query the collections with
        ``free_text_pattern`` across optional comma-separated ``data_sets`` -
        e.g. 'malware,cve,credentials') or 'thread' (monitor a specific forum
        ``thread_uid``, with ``thread_type``). ``notification_channel`` is
        'website' (alerts via API/UI only) or 'email' (with
        ``notification_frequency`` 'immediate' or 'daily'). Hits appear in
        list_intel471_alerts.
        """
        if not safe_seg(group_uid):
            return "Error: 'group_uid' must be a plain group uid (no path separators)."
        wtype = (watcher_type or "search").strip().lower()
        if wtype not in _WATCHER_TYPES:
            return f"Error: watcher_type must be one of {', '.join(_WATCHER_TYPES)}."
        channel = (notification_channel or "website").strip().lower() or "website"
        if channel not in _NOTIFICATION_CHANNELS:
            return (
                "Error: notification_channel must be one of "
                f"{', '.join(_NOTIFICATION_CHANNELS)}."
            )
        frequency = (notification_frequency or "").strip().lower()
        if channel == "email":
            if not frequency:
                return (
                    "Error: notification_frequency ('immediate' or 'daily') is "
                    "required when notification_channel is 'email'."
                )
            if frequency not in _NOTIFICATION_FREQUENCIES:
                return (
                    "Error: notification_frequency must be one of "
                    f"{', '.join(_NOTIFICATION_FREQUENCIES)}."
                )
        body: dict[str, Any] = {"type": wtype, "notificationChannel": channel}
        if description.strip():
            body["description"] = description.strip()
        # Frequency only makes sense for email delivery; never send it for the
        # website channel (Titan rejects the combination).
        if channel == "email":
            body["notificationFrequency"] = frequency
        if wtype == "search":
            if not free_text_pattern.strip():
                return "Error: 'free_text_pattern' is required for a 'search' watcher."
            body["freeTextPattern"] = free_text_pattern.strip()
            sets = [s.strip() for s in (data_sets or "").split(",") if s.strip()]
            if sets:
                body["dataSets"] = sets
        else:  # thread
            if not thread_uid.strip():
                return "Error: 'thread_uid' is required for a 'thread' watcher."
            body["threadUid"] = thread_uid.strip()
            if thread_type.strip():
                body["threadType"] = thread_type.strip()
        payload, err = await titan_request(
            headers, "post", f"/watcherGroups/{group_uid.strip()}/watchers",
            "create watcher", json=body,
        )
        if err:
            return err
        watcher = payload if isinstance(payload, dict) else {}
        return (
            f"Created {wtype} watcher (uid: {watcher.get('uid', '?')}) in group "
            f"{group_uid.strip()}. Hits will appear in list_intel471_alerts."
        )

    async def manage_intel471_watchers(
        action: str,
        group_uid: str,
        watcher_uid: str = "",
        name: str = "",
        description: str = "",
        watcher_body: dict[str, Any] | None = None,
    ) -> str:
        """Consolidated long-tail watcher management (some actions are DESTRUCTIVE).

        ``action`` is one of:
        - 'update_group' (rename/redescribe a group; needs name and/or description)
        - 'delete_group' (DESTRUCTIVE - permanently deletes the group and its
          watchers; only groups you own can be deleted; cannot be undone)
        - 'get_watcher' (fetch a single watcher; needs watcher_uid)
        - 'update_watcher' (replace a watcher's config; needs watcher_uid and the
          full watcher_body object - fetch it with get_watcher first)
        - 'delete_watcher' (DESTRUCTIVE - permanently removes one watcher; needs
          watcher_uid; cannot be undone)
        ``group_uid`` is always required.
        """
        action = (action or "").strip().lower()
        if action not in _WATCHER_ACTIONS:
            return (
                f"Error: unknown action '{action}'. Supported: "
                f"{', '.join(sorted(_WATCHER_ACTIONS))}."
            )
        if not safe_seg(group_uid):
            return "Error: 'group_uid' must be a plain group uid (no path separators)."
        group_uid = group_uid.strip()

        if action == "update_group":
            if not (name.strip() or description.strip()):
                return "Error: update_group needs a new 'name' and/or 'description'."
            body: dict[str, Any] = {}
            if name.strip():
                body["name"] = name.strip()
            if description.strip():
                body["description"] = description.strip()
            payload, err = await titan_request(
                headers, "put", f"/watcherGroups/{group_uid}",
                "update watcher group", json=body,
            )
            return err or f"Updated watcher group {group_uid}."

        if action == "delete_group":
            _payload, err = await titan_request(
                headers, "delete", f"/watcherGroups/{group_uid}",
                "delete watcher group", expected_statuses={404},
            )
            if err and not _is_404(_payload):
                return err
            if err:
                return f"No watcher group found with uid '{group_uid}'."
            return f"Deleted watcher group {group_uid} (and its watchers)."

        # Remaining actions operate on a specific watcher.
        if not safe_seg(watcher_uid):
            return f"Error: action '{action}' requires a plain 'watcher_uid'."
        watcher_uid = watcher_uid.strip()
        path = f"/watcherGroups/{group_uid}/watchers/{watcher_uid}"

        if action == "get_watcher":
            payload, err = await titan_get(
                headers, path, f"get watcher {watcher_uid}", expected_statuses={404}
            )
            if err:
                return f"No watcher found with uid '{watcher_uid}'." if _is_404(payload) else err
            return _watcher_summary(payload if isinstance(payload, dict) else {}, verbose=True)

        if action == "update_watcher":
            if not isinstance(watcher_body, dict) or not watcher_body:
                return (
                    "Error: update_watcher needs the full 'watcher_body' object "
                    "(fetch the current watcher with action='get_watcher' first, "
                    "then send the modified body)."
                )
            _payload, err = await titan_request(
                headers, "put", path, "update watcher", json=watcher_body
            )
            return err or f"Updated watcher {watcher_uid}."

        # delete_watcher
        _payload, err = await titan_request(
            headers, "delete", path, "delete watcher", expected_statuses={404}
        )
        if err and not _is_404(_payload):
            return err
        if err:
            return f"No watcher found with uid '{watcher_uid}'."
        return f"Deleted watcher {watcher_uid} from group {group_uid}."

    handlers = {
        "list_intel471_alerts": list_intel471_alerts,
        "list_intel471_watcher_groups": list_intel471_watcher_groups,
        "get_intel471_watcher_group": get_intel471_watcher_group,
        "create_intel471_watcher_group": create_intel471_watcher_group,
        "create_intel471_watcher": create_intel471_watcher,
        "manage_intel471_watchers": manage_intel471_watchers,
    }
    for name, fn in handlers.items():
        executor.register(name, fn)

    return _definitions()


def _is_404(resp: Any) -> bool:
    return getattr(resp, "status_code", None) == 404


def _watcher_summary(w: dict[str, Any], *, verbose: bool = False) -> str:
    """One-line (or verbose) summary of a watcher record."""
    wtype = w.get("type") or "?"
    uid = w.get("uid") or "?"
    patterns = w.get("patterns")
    pattern_str = ""
    if isinstance(patterns, list) and patterns:
        first = patterns[0]
        if isinstance(first, dict):
            pattern_str = str(first.get("pattern") or first.get("value") or "")
    label = w.get("freeTextPattern") or pattern_str or w.get("threadUid") or w.get("description") or ""
    base = f"[{wtype}] {trunc(label, 120)} (uid: {uid})"
    if not verbose:
        return base
    lines = [f"Watcher {base}"]
    sets = w.get("dataSets")
    if isinstance(sets, list) and sets:
        lines.append(f"  data sets: {', '.join(str(s) for s in sets)}")
    if w.get("notificationChannel"):
        lines.append(f"  notify: {w.get('notificationChannel')} {w.get('notificationFrequency') or ''}".rstrip())
    return "\n".join(lines)


def _definitions() -> list[ToolDefinition]:
    """Static ToolDefinition metadata for the alert + watcher tools."""

    def _td(name, description, properties, required, priority):
        return ToolDefinition(
            name=name,
            description=description,
            input_schema={"type": "object", "properties": properties, "required": required},
            source=_SOURCE,
            priority=priority,
        )

    return [
        _td(
            "list_intel471_alerts",
            "List the Intel 471 watcher-driven alert feed - reports/posts/messages "
            "that matched your standing watchers. Filter to one watcher_group uid, "
            "hide read alerts with show_read=false, and page with offset = the UID "
            "of the last alert you processed (NOT a number). mark_as_read=true "
            "marks the returned alerts read. Requires watchers to be configured.",
            {
                "watcher_group": {"type": "string", "description": "Only alerts from this watcher-group uid."},
                "show_read": {"type": "boolean", "default": True, "description": "Include already-read alerts."},
                "mark_as_read": {"type": "boolean", "default": False, "description": "Mark returned alerts as read."},
                "from_time": {"type": "string", "description": "Creation-time lower bound (unix millis or relative)."},
                "until_time": {"type": "string", "description": "Creation-time upper bound (unix millis or relative)."},
                "offset": {"type": "string", "description": "UID of the last already-acquired alert (for paging)."},
                "count": {"type": "integer", "default": 10, "description": "Records to return (1-100)."},
                "sort": {"type": "string", "enum": ["latest", "earliest"], "default": "latest"},
            },
            [],
            0,
        ),
        _td(
            "list_intel471_watcher_groups",
            "List the Intel 471 watcher groups (standing monitors) on the account. "
            "section = 'myGroups' (owned, default) or 'sharedGroups'. Feed a uid to "
            "get_intel471_watcher_group.",
            {
                "section": {
                    "type": "string",
                    "enum": ["myGroups", "sharedGroups"],
                    "default": "myGroups",
                    "description": "Which watcher groups to list.",
                }
            },
            [],
            0,
        ),
        _td(
            "get_intel471_watcher_group",
            "Get an Intel 471 watcher group and the watchers it contains by uid "
            "(from list_intel471_watcher_groups).",
            {"group_uid": {"type": "string", "description": "Watcher-group uid."}},
            ["group_uid"],
            0,
        ),
        _td(
            "create_intel471_watcher_group",
            "Create a new Intel 471 watcher group (a container for standing "
            "monitors). 'name' is required. Add monitors afterwards with "
            "create_intel471_watcher.",
            {
                "name": {"type": "string", "description": "Group name (required)."},
                "description": {"type": "string", "description": "Optional group description."},
            },
            ["name"],
            1,
        ),
        _td(
            "create_intel471_watcher",
            "Add a watcher (standing monitor) to an Intel 471 watcher group. "
            "watcher_type 'search' queries the collections with free_text_pattern "
            "over optional data_sets (comma-separated, e.g. 'malware,cve,"
            "credentials'); 'thread' monitors a forum thread_uid. Choose "
            "notification_channel 'website' or 'email' (with notification_frequency "
            "'immediate'/'daily'). Hits appear in list_intel471_alerts.",
            {
                "group_uid": {"type": "string", "description": "Target watcher-group uid (required)."},
                "watcher_type": {
                    "type": "string",
                    "enum": list(_WATCHER_TYPES),
                    "default": "search",
                    "description": "'search' (pattern query) or 'thread' (forum thread monitor).",
                },
                "free_text_pattern": {"type": "string", "description": "Search pattern (required for 'search')."},
                "data_sets": {
                    "type": "string",
                    "description": "Comma-separated data sets to limit a search watcher (e.g. 'malware,cve').",
                },
                "thread_uid": {"type": "string", "description": "Forum thread uid (required for 'thread')."},
                "thread_type": {"type": "string", "description": "Thread type (for 'thread' watchers)."},
                "description": {"type": "string", "description": "Optional watcher description."},
                "notification_channel": {
                    "type": "string",
                    "enum": ["website", "email"],
                    "default": "website",
                    "description": "Where alerts are delivered ('website' = API/UI only).",
                },
                "notification_frequency": {
                    "type": "string",
                    "enum": ["immediate", "daily"],
                    "description": "Email cadence (email channel only).",
                },
            },
            ["group_uid"],
            1,
        ),
        _td(
            "manage_intel471_watchers",
            "Consolidated Intel 471 watcher management. action = update_group "
            "(rename/redescribe), delete_group (DESTRUCTIVE - permanently deletes "
            "the group and its watchers, cannot be undone), get_watcher, "
            "update_watcher (send the full watcher_body from get_watcher), or "
            "delete_watcher (DESTRUCTIVE - permanently removes one watcher, cannot "
            "be undone). group_uid is always required.",
            {
                "action": {
                    "type": "string",
                    "enum": sorted(_WATCHER_ACTIONS),
                    "description": "The management operation to perform.",
                },
                "group_uid": {"type": "string", "description": "Watcher-group uid (required)."},
                "watcher_uid": {"type": "string", "description": "Watcher uid (for the *_watcher actions)."},
                "name": {"type": "string", "description": "New group name (update_group)."},
                "description": {"type": "string", "description": "New group description (update_group)."},
                "watcher_body": {
                    "type": "object",
                    "description": "Full watcher object for update_watcher (fetch with get_watcher first).",
                },
            },
            ["action", "group_uid"],
            2,
        ),
    ]
