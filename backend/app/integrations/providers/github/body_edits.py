"""In-place edits of an issue or pull request body.

``update_github_issue`` and ``update_github_pr`` take ``edits`` instead of a
whole ``body``: ordered find/replace edits applied to the body as GitHub stores
it at write time, with the contract of ``sandbox_edit_file`` and
``create_github_commit`` edits (:mod:`app.services.sandbox_workspace.editing`):
each ``find`` exists and is unique unless ``replace_all``, nothing is written
unless every edit applies, and the unified diff is the read-back. A long body
edited by many writers then changes one line without entering the context.

``expect_updated_at`` (the ``updated_at`` the caller read) refuses the write
when the issue or pull request changed since: a concurrent edit is never
overwritten by a body computed from an older read.
"""

from __future__ import annotations

from typing import Any

from app.services.sandbox_workspace import editing

from . import _common
from ._common import _PROVIDER

#: The schema of the ``edits`` argument, shared by both tools.
EDITS_SCHEMA: dict[str, Any] = {
    "type": "array",
    "description": "Instead of body: find/replace edits on the body as stored (each find unique unless replace_all); all or nothing, diff returned",
    "items": {
        "type": "object",
        "properties": {
            "find": {"type": "string"},
            "replace": {"type": "string"},
            "replace_all": {"type": "boolean"},
        },
        "required": ["find", "replace"],
    },
}

#: The schema of ``expect_updated_at``, shared by both tools.
EXPECT_UPDATED_AT_SCHEMA: dict[str, Any] = {
    "type": "string",
    "description": "updated_at you read: refused if it changed since",
}


async def edited_body(
    client: Any,
    url: str,
    headers: dict[str, str],
    raw_edits: Any,
    expect_updated_at: str,
    what: str,
) -> tuple[str, str, str | None]:
    """``(new body, diff, error)``: *raw_edits* applied to the body GitHub serves at *url*.

    *what* names the object in the answers (``issue #5 in o/r``). Without
    edits only the ``expect_updated_at`` guard runs (body and diff empty).
    Nothing is written here; an error means the caller writes nothing either.
    """
    edits: list[editing.Edit] = []
    if raw_edits:
        try:
            edits = editing.parse_edits(raw_edits)
        except editing.EditError as exc:
            return "", "", f"Error: the edits of {what} are refused: {exc}. Nothing was written."
    resp, err = await _common.safe_request(client, "get", url, _PROVIDER, f"read {what}", headers=headers)
    if err:
        return "", "", f"{err}\nNothing was written."
    data = resp.json() if resp is not None else {}
    if not isinstance(data, dict):
        return "", "", f"Error: GitHub answered no object for {what}. Nothing was written."
    stored_at = str(data.get("updated_at") or "")
    expected = (expect_updated_at or "").strip()
    if expected and expected != stored_at:
        return (
            "",
            "",
            (
                f"Error: {what} changed since your read (updated_at {stored_at}, you read {expected}). "
                "Nothing was written: read it again and redo the edits."
            ),
        )
    if not edits:
        return "", "", None
    before = data.get("body") or ""
    try:
        applied = editing.apply_edits(before, edits, read_with="get_github_issue or get_github_pr (include_body)")
    except editing.EditError as exc:
        return "", "", f"Error: the body of {what}: {exc}"
    diff = editing.unified_diff("body", before, applied.text) or "(no change in the body)\n"
    return applied.text, diff, None
