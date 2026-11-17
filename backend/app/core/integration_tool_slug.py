# SPDX-License-Identifier: Apache-2.0

"""The tool-name suffix of an integration instance.

When an agent binds several integrations of one provider, every tool of each
instance is exposed as ``<tool>__<slug>`` (``integration_tools._suffix_instance_tools``).
The slug is stored on the integration (``Integration.tool_slug``): it is
derived ONCE from the display name when the row is created and never follows a
rename, so a persona, a skill or an assignment gate citing a suffixed tool name
keeps working when someone renames the integration. Its owner may change it
explicitly (the integrations API validates it with :func:`tool_slug_error`).

Pure module: no database, no service import, so the model's insert default and
the migration backfill compute exactly what the tool build computed before.
"""

from __future__ import annotations

import re

#: What a stored slug may hold.
TOOL_SLUG_PATTERN = re.compile(r"^[a-z0-9_]+$")

#: The longest stored slug. The tool build caps an instance slug at 24
#: characters and keeps 3 of them for a ``_N`` uniqueness counter, so a
#: stored slug longer than 21 would be cut at build time anyway.
MAX_TOOL_SLUG_LEN = 21


def derive_tool_slug(name: str | None, provider: str, integration_id: object = "") -> str:
    """The slug the display *name* yields, as the tool build derived it before the slug was stored.

    Lowercased, every run of non-alphanumerics collapsed to ``_``, trimmed to
    :data:`MAX_TOOL_SLUG_LEN`. An empty name falls back to the provider plus a
    short id prefix (a custom provider key's ``:`` is sanitized too).
    """
    base = re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")
    if not base:
        base = re.sub(
            r"[^a-z0-9]+",
            "_",
            f"{provider}_{str(integration_id or '').replace('-', '')[:8]}".lower(),
        ).strip("_")
    return base[:MAX_TOOL_SLUG_LEN].rstrip("_") or "integration"


def tool_slug_error(value: object) -> str | None:
    """Why *value* cannot be stored as a tool slug, ``None`` when it can."""
    if not isinstance(value, str) or not value:
        return "tool_slug must be a non-empty string."
    if len(value) > MAX_TOOL_SLUG_LEN:
        return f"tool_slug holds at most {MAX_TOOL_SLUG_LEN} characters."
    if not TOOL_SLUG_PATTERN.match(value):
        return "tool_slug holds lowercase letters, digits and underscores only."
    if value.strip("_") != value:
        return "tool_slug cannot start or end with an underscore."
    return None
