# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""Built-in template lookup tools.

Gated behind the ``template_search`` built-in tool id, these let an agent find a
reusable design template by **structured metadata** and read back its layouts,
palette, supported deck themes and asset references:

* ``search_templates`` — filter by name / kind / tag / colour scheme / aspect
  ratio and get the matching containers' metadata plus their asset references.
* ``get_template`` — fetch one container by id with its full layout, theme
  config and asset list; or, with ``role`` / ``query``, only the assets that
  match (the brand deck carries ~180 predefined assets, the logo sets
  included, and an agent building a logo wall wants the 31 integration marks,
  not the whole inventory).

This is emphatically **not** RAG. There is no embedding, no chunking and no
vector similarity in this path: ``search_knowledge`` searches a vectorized
``KnowledgeBase``, while these tools run exact/prefix SQL filters over
``template_containers`` (see ``app.services.template_containers``). An agent
that wants "the 16:9 dark deck" gets a deterministic answer, and a template's
content never enters the RAG corpus.
"""

from __future__ import annotations

import json
from collections import Counter
from typing import TYPE_CHECKING

from app.core.utils import format_exception_message, safe_uuid
from app.models.template_container import ASSET_ROLES
from app.services.presentation_chrome import describe_template_chrome, template_chrome
from app.services.presentation_spec import DEFAULT_SLIDE_CAP, TEMPLATE_HEROES
from app.services.presentations import template_heroes, template_slide_cap
from app.services.template_containers import PRESENTATION_KIND, TEMPLATE_KINDS, VIDEO_KIND
from app.tools.base import CallableToolExecutor, ToolDefinition

from ._shared import logger

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

#: Default number of containers returned by ``search_templates`` when the caller
#: does not ask for more. Kept small so a broad search doesn't crowd out the
#: rest of the agent's context; the service caps the ceiling.
_DEFAULT_LIMIT = 10

#: Layout / theme / extra-metadata payloads are consumer-defined and can be
#: large. ``search_templates`` shows only layout *labels* (the agent is still
#: choosing); ``get_template`` serves the full values, truncated at these
#: budgets so one oversized template can't blow the context window.
_THEME_CONFIG_BUDGET = 4000
_LAYOUTS_BUDGET = 8000
_EXTRA_METADATA_BUDGET = 2000

#: ``extra_metadata.guidance`` is PROSE the composing agent reads - how a deck
#: in this brand opens, where the numbers go, how a keynote is told - not a
#: machine field, so ``get_template`` serves it on a line of its own
#: instead of inside the metadata JSON, whose budget cut the brand deck's
#: guidance mid-sentence once its palettes grew. Its own ceiling is a hard stop
#: for an admin's runaway text, generous enough for every seeded guidance
#: (``template_generation.MAX_GUIDANCE_CHARS`` bounds a generated one lower).
_GUIDANCE_BUDGET = 4000
_GUIDANCE_KEY = "guidance"

#: Above this many assets a ``search_templates`` result shows per-role COUNTS
#: instead of one line per asset. The brand deck ships ~180 predefined assets
#: (the partner / integration / customer logos included);
#: listing every one, with its description, in a search that returns several
#: containers would be tens of thousands of characters the agent has to scroll
#: past while it is still choosing a template. ``get_template`` lists them,
#: bounded by ``_DETAIL_ASSET_LINES``, or only the role / query the agent asks for.
_SUMMARY_ASSET_LINES = 12

#: Ceiling on the asset lines an UNFILTERED ``get_template`` lists. The same
#: 180 assets, one line each with its caption, were ~35,000 characters for an
#: agent that wanted the palette and one diagram. Above the ceiling the largest
#: roles are collapsed - biggest first, until what remains fits - to their count
#: in the header and one pointer naming the ``role=`` call that lists them. The
#: roles an agent picks single assets from (marks, diagrams, screenshots) are
#: the small ones, so they stay listed in full; the logo walls are placed by
#: role in a ``logo_grid`` anyway. A filtered call is never collapsed.
_DETAIL_ASSET_LINES = 60

#: Order assets are listed in: the catalog order of ``ASSET_ROLES`` (brand
#: marks first, pictures next, ``other`` last), unlabelled at the very end.
_ROLE_ORDER: dict[str, int] = {role: index for index, role in enumerate(ASSET_ROLES)}


def _truncate_json(value: object, budget: int) -> str:
    """Compact-JSON a value, truncating past *budget* chars with a marker."""
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if len(text) <= budget:
        return text
    return text[:budget] + f"… (truncated, {len(text)} chars total)"


def _asset_role(asset: dict) -> str:
    return asset.get("asset_role") or "unlabelled"


def _sorted_by_role(assets: list[dict]) -> list[dict]:
    """Group a listing by role (catalog order), then by filename.

    Sixty assets in upload order interleave a diagram, a badge and a photo; in
    role order the agent looking for "a diagram" reads one contiguous run.
    """
    return sorted(
        assets,
        key=lambda a: (
            _ROLE_ORDER.get(_asset_role(a), len(_ROLE_ORDER)),
            str(a.get("filename") or ""),
        ),
    )


def _format_assets(assets: list[dict], *, indent: str = "  ") -> list[str]:
    """One line per asset: role, file, id — and what it DEPICTS.

    The description is the part an agent chooses by: a brand container
    holds dozens of predefined assets, and ``[illustration] xtm-platform.svg``
    alone does not say that it is the four-product suite diagram or which colour
    scheme it is drawn for. It is one line by construction
    (``normalize_asset_description``), so it cannot break this listing.
    """
    lines = []
    for asset in _sorted_by_role(assets):
        role = _asset_role(asset)
        size = asset.get("size") or 0
        line = (
            f"{indent}- [{role}] {asset.get('filename')} "
            f"(id: {asset.get('id')}, {asset.get('content_type')}, {size:,} bytes)"
        )
        description = asset.get("description")
        if description:
            line += f" — {description}"
        lines.append(line)
    return lines


def _format_asset_counts(assets: list[dict]) -> str:
    """``12 diagram, 9 screenshot, 6 product_logo`` — role counts, catalog order."""
    counts = Counter(_asset_role(asset) for asset in assets)
    ordered = sorted(counts.items(), key=lambda kv: _ROLE_ORDER.get(kv[0], len(_ROLE_ORDER)))
    return ", ".join(f"{count} {role}" for role, count in ordered)


def _format_header(container: dict) -> list[str]:
    """What identifies a template and what it dresses: name, id, kind, description,
    the deck themes it supports, aspect ratio, tags and its layout labels -
    everything but the assets, which each surface lists on its own budget."""
    lines = [
        f"Template **{container['name']}** (id: `{container['id']}`, "
        f"kind: {container['kind']})"
    ]
    if container.get("kind") == VIDEO_KIND:
        lines.append(
            "  - a video template: pass its id as template_id of create_video / edit_video, "
            "never as a deck's template_id — its assets are still referenced by file id from any deck"
        )
        lines.extend(_describe_video_rules(container.get("extra_metadata")))
    elif container.get("kind") != PRESENTATION_KIND:
        # A report template, or a legacy kind an older install still carries:
        # its assets are usable by file id, its id is not a deck's template.
        lines.append(
            f"  - not a presentation template (kind: {container['kind']}): it dresses "
            "no deck, never pass its id as template_id — its assets are still "
            "referenced by file id from any deck"
        )
    if container.get("description"):
        lines.append(f"  - description: {container['description']}")
    # The colour schemes a presentation template can dress a deck in:
    # the constraint on the deck's ``theme`` once this template is linked. The
    # legacy free-text ``theme`` label is deliberately NOT echoed - it renders
    # nothing, and quoting it next to the deck themes made the two read alike.
    schemes = container.get("supported_schemes")
    if container.get("kind") in (PRESENTATION_KIND, VIDEO_KIND) and isinstance(schemes, (list, tuple)) and schemes:
        only = " only" if len(schemes) == 1 else ""
        surface = "deck" if container.get("kind") == PRESENTATION_KIND else "video"
        lines.append(f"  - {surface} themes supported: {' / '.join(str(s) for s in schemes)}{only}")
    # The footer (and header) every deck it dresses draws on every slide,
    # and which deck setting fills each placeholder - so the agent
    # sets chrome.event_label / chrome.fields once instead of writing a footer
    # into slide content.
    if container.get("kind") == PRESENTATION_KIND:
        # The covers of its own: the only template whose decks may
        # wear them, so the agent offers them on this brand and no other.
        own_covers = [hero for hero in template_heroes(container.get("extra_metadata")) if hero in TEMPLATE_HEROES]
        if own_covers:
            lines.append(
                f"  - covers of its own (title / closing hero, on top of plain / gradient / split / "
                f"spotlight): {' / '.join(own_covers)}"
            )
        lines.extend(describe_template_chrome(template_chrome(container.get("extra_metadata"))))
        # How long a deck of this template may run, when the template
        # says so itself: a cap the platform default already states is noise.
        cap = template_slide_cap(container.get("extra_metadata"))
        if cap != DEFAULT_SLIDE_CAP:
            lines.append(
                f"  - slides per deck: up to {cap} (extra_metadata.max_slides; "
                f"the platform default is {DEFAULT_SLIDE_CAP})"
            )
    if container.get("aspect_ratio"):
        lines.append(f"  - aspect ratio: {container['aspect_ratio']}")
    if container.get("tags"):
        lines.append(f"  - tags: {', '.join(container['tags'])}")
    layouts = container.get("layouts") or []
    if layouts:
        # Surface layout ids/names rather than the whole definition — that is
        # what an agent needs to pick a layout, and the full spec is one
        # get_template call away.
        labels = [
            str(item.get("id") or item.get("name") or f"#{idx}")
            if isinstance(item, dict)
            else str(item)
            for idx, item in enumerate(layouts)
        ]
        lines.append(f"  - layouts ({len(labels)}): {', '.join(labels[:20])}")
    return lines


def _describe_video_rules(extra_metadata: object) -> list[str]:
    """What a video template presets, adds and locks, one line each."""
    from app.services.video_studio.templates import VIDEO_STYLE_KEY, parse_style

    style = parse_style(extra_metadata.get(VIDEO_STYLE_KEY) if isinstance(extra_metadata, dict) else None)
    if style is None:
        return ["  - video rules: none (its palette, fonts and logo only)"]
    lines = [f"  - video formats: {', '.join(style.aspect_ratios)}"]
    added = [name for name, bookend in (("an intro card", style.intro), ("an outro card", style.outro)) if bookend and bookend.required]
    if added:
        lines.append(f"  - adds {' and '.join(added)} when the video has none")
    if style.locked:
        lines.append(f"  - locked (the template's value always wins): {', '.join(style.locked)}")
    return lines


def _format_summary(container: dict) -> str:
    """One-container block for ``search_templates`` results: the header, then
    every asset up to ``_SUMMARY_ASSET_LINES`` of them, or per-role counts and a
    pointer to ``get_template`` above that."""
    lines = _format_header(container)
    assets = container.get("assets") or []
    if not assets:
        lines.append("  - assets: none")
    elif len(assets) <= _SUMMARY_ASSET_LINES:
        lines.append(f"  - assets ({len(assets)}):")
        lines.extend(_format_assets(assets, indent="    "))
    else:
        lines.append(
            f"  - assets ({len(assets)}): {_format_asset_counts(assets)} — "
            f"get_template(template_id=\"{container['id']}\") lists them; role= or "
            "query= narrows to one role or name"
        )
    return "\n".join(lines)


def _format_detail_assets(container: dict) -> list[str]:
    """The asset block of an unfiltered ``get_template``, bounded.

    Every asset while there are at most ``_DETAIL_ASSET_LINES``. Above that the
    largest roles are collapsed, biggest first, until what remains fits: they
    keep their count in the header and share one pointer line naming the
    ``role=`` call that lists them, while every other role is listed in full.
    Two groups of the same size collapse in a fixed order too - a catalog role
    before an unlabelled or custom one, and the role later in the catalog
    first - so the brand marks an agent picks single assets from are the last
    to go, and a group that only ``query=`` can reach stays listed longest.

    The pointer only ever advertises a call that runs AND returns the group:
    ``role=`` takes a catalog role (``ASSET_ROLES``), so an unlabelled group -
    or a custom label the REST API stored - is pointed at ``query="<label>"``
    instead, never at a ``role="unlabelled"`` the tool would refuse; and the
    label is part of what ``query=`` searches (:func:`_asset_matches`), so
    that call lists the group whatever its files are called.
    """
    assets = container.get("assets") or []
    if not assets:
        return ["  - assets: none"]
    if len(assets) <= _DETAIL_ASSET_LINES:
        return [f"  - assets ({len(assets)}):", *_format_assets(assets, indent="    ")]
    by_role: dict[str, list[dict]] = {}
    for asset in _sorted_by_role(assets):
        by_role.setdefault(_asset_role(asset), []).append(asset)
    listed = dict(by_role)

    def _collapses_first(role: str) -> tuple[int, bool, int]:
        # Biggest group first; on a tie a catalog role (one ``role=`` call
        # away) before a group only ``query=`` reaches, then the later catalog
        # role, so the brand marks at the head of the catalog stay listed.
        return (len(listed[role]), role in ASSET_ROLES, _ROLE_ORDER.get(role, -1))

    while listed and sum(len(group) for group in listed.values()) > _DETAIL_ASSET_LINES:
        del listed[max(listed, key=_collapses_first)]
    collapsed = [role for role in by_role if role not in listed]
    lines = [f"  - assets ({len(assets)}): {_format_asset_counts(assets)}"]
    for group in listed.values():
        lines.extend(_format_assets(group, indent="    "))
    counts = ", ".join(f"{len(by_role[role])} {role}" for role in collapsed)
    named = [role for role in collapsed if role in ASSET_ROLES]
    unnamed = [role for role in collapsed if role not in ASSET_ROLES]
    call = f"get_template(template_id=\"{container['id']}\""
    by_label = " / ".join(f'query="{role}"' for role in unnamed)
    if named:
        pointer = f"{call}, role=\"{named[0]}\") lists one role, query=\"WORD\" one name"
        if unnamed:
            pointer += f" ({by_label} the rest: role= takes catalog roles)"
    else:
        pointer = f"{call}, {by_label}) lists them by label (role= takes catalog roles only)"
    lines.append(f"    - {counts} not listed: {pointer}")
    return lines


def _asset_matches(asset: dict, *, role: str | None, query: str | None) -> bool:
    if role and _asset_role(asset) != role:
        return False
    if query:
        # The role label is searched too - a custom one the REST API stored,
        # or ``unlabelled`` - so the ``query="<label>"`` call the collapsed
        # listing points at returns the group even when no filename or
        # description carries the word (``[mockup] slide-1.png``).
        haystack = (
            f"{_asset_role(asset)} {asset.get('filename') or ''} "
            f"{asset.get('description') or ''}"
        ).casefold()
        return query.casefold() in haystack
    return True


def _format_filtered_assets(container: dict, *, role: str | None, query: str | None) -> str:
    """``get_template`` narrowed to the assets that match *role* / *query*.

    The header keeps what identifies the template (name, id, kind, the deck
    themes it supports); the palette, layouts and extra metadata are left out -
    an agent filtering assets is picking logos for a ``logo_grid`` or the one
    diagram a slide needs, not restyling the deck.
    """
    assets = container.get("assets") or []
    matching = [a for a in assets if _asset_matches(a, role=role, query=query)]
    applied = ", ".join(
        f"{label}={value!r}" for label, value in (("role", role), ("query", query)) if value
    )
    lines = [
        f"Template **{container['name']}** (id: `{container['id']}`, kind: {container['kind']})",
    ]
    schemes = container.get("supported_schemes")
    if container.get("kind") in (PRESENTATION_KIND, VIDEO_KIND) and isinstance(schemes, (list, tuple)) and schemes:
        surface = "deck" if container.get("kind") == PRESENTATION_KIND else "video"
        lines.append(f"  - {surface} themes supported: {' / '.join(str(s) for s in schemes)}")
    if not matching:
        counts = _format_asset_counts(assets) if assets else "none"
        lines.append(
            f"  - assets matching {applied}: none of {len(assets)} "
            f"(this template holds: {counts}). Widen the query or drop the role filter."
        )
        return "\n".join(lines)
    lines.append(f"  - assets matching {applied} ({len(matching)} of {len(assets)}):")
    lines.extend(_format_assets(matching, indent="    "))
    return "\n".join(lines)


def _split_guidance(extra_metadata: object) -> tuple[str | None, object]:
    """The guidance prose of *extra_metadata*, and the metadata without it.

    A non-dict (a legacy row holding a list or a string) has no guidance and is
    served as it is; a guidance that is not a non-empty string is left in the
    JSON for the agent to see as stored.
    """
    if not isinstance(extra_metadata, dict):
        return None, extra_metadata
    guidance = extra_metadata.get(_GUIDANCE_KEY)
    if not isinstance(guidance, str) or not guidance.strip():
        return None, extra_metadata
    rest = {key: value for key, value in extra_metadata.items() if key != _GUIDANCE_KEY}
    return " ".join(guidance.split()), rest


def _format_detail(container: dict) -> str:
    """Full block for an unfiltered ``get_template``: header, bounded asset
    list, the composition guidance, then the design (palette, layout
    definitions, the rest of the extra metadata)."""
    lines = [*_format_header(container), *_format_detail_assets(container)]
    guidance, extra_metadata = _split_guidance(container.get("extra_metadata"))
    if guidance:
        if len(guidance) > _GUIDANCE_BUDGET:
            guidance = guidance[:_GUIDANCE_BUDGET] + f"… (truncated, {len(guidance)} chars total)"
        lines.append(f"  - guidance: {guidance}")
    if container.get("theme_config"):
        lines.append(
            "  - theme config: "
            f"{_truncate_json(container['theme_config'], _THEME_CONFIG_BUDGET)}"
        )
    if container.get("layouts"):
        lines.append(
            "  - layout definitions: "
            f"{_truncate_json(container['layouts'], _LAYOUTS_BUDGET)}"
        )
    if extra_metadata:
        lines.append(
            "  - extra metadata: "
            f"{_truncate_json(extra_metadata, _EXTRA_METADATA_BUDGET)}"
        )
    return "\n".join(lines)


def build_template_search_tools(
    db: AsyncSession,
    user_id: str,
) -> tuple[list[ToolDefinition], CallableToolExecutor]:
    """Build the structured template lookup tools for an agent.

    *user_id* scopes visibility to the standard three tiers (own + group-shared
    + company-managed). ``"anonymous"`` (an external-channel user with no linked
    account) collapses to the company-managed templates ``caller_visible_filter``
    gives a caller with no account - the ones not narrowed by
    ``access_restricted`` - so an anonymous caller never reaches another user's
    private templates.

    There is deliberately **no admin-wide bypass** here, unlike the REST
    surface: an admin browsing the API is acting for themselves, whereas an
    agent answers on someone's behalf in a conversation. Honouring
    ``is_admin`` would let an admin's agent surface other users' private
    templates in a chat reply, which nothing asks for.
    """
    del db  # each operation opens its own session (mirrors shared_state/work_items)
    acting_user_id = user_id if user_id and user_id != "anonymous" else None

    async def _search_templates(
        name: str = "",
        kind: str = "",
        tags: str = "",
        theme: str = "",
        aspect_ratio: str = "",
        match_all_tags: bool = False,
        limit: int = _DEFAULT_LIMIT,
    ) -> str:
        from app.api.v1.helpers import get_user_group_ids
        from app.core.database import async_session
        from app.services.template_containers import (
            load_assets,
            record_lookups,
            search_containers,
            serialize_container,
        )

        tag_list = [t.strip() for t in (tags or "").split(",") if t.strip()]
        try:
            async with async_session() as db_session:
                user_uuid = safe_uuid(acting_user_id) if acting_user_id else None
                group_ids = (
                    await get_user_group_ids(db_session, user_uuid)
                    if user_uuid
                    else None
                )
                containers = await search_containers(
                    db_session,
                    user_id=user_uuid,
                    user_group_ids=group_ids,
                    name=name or None,
                    kind=kind or None,
                    tags=tag_list or None,
                    theme=theme or None,
                    aspect_ratio=aspect_ratio or None,
                    match_all_tags=bool(match_all_tags),
                    limit=int(limit or _DEFAULT_LIMIT),
                )
                payloads = [
                    serialize_container(
                        c, assets=await load_assets(db_session, c.id)
                    )
                    for c in containers
                ]
                await record_lookups(db_session, containers)
                await db_session.commit()
        except Exception as exc:
            logger.error(
                "search_templates tool failed: %s",
                format_exception_message(exc),
                exc_info=True,
            )
            return "Error: failed to search templates. Please try again."

        if not payloads:
            applied = {
                "name": name,
                "kind": kind,
                "tags": ", ".join(tag_list),
                "theme": theme,
                "aspect_ratio": aspect_ratio,
            }
            described = ", ".join(f"{k}={v!r}" for k, v in applied.items() if v)
            # Tool-result grounding: kind and aspect_ratio are EXACT, a theme of
            # dark / light only ever keeps presentation templates dressing that
            # scheme and any other theme must equal a legacy label, so
            # the usual cause of an empty result is an over-specified filter,
            # not a missing template. Say so instead of letting the agent
            # conclude no template exists.
            return (
                f"No templates matched ({described or 'no filters'}). "
                "kind and aspect_ratio are matched exactly; theme is 'dark' / "
                "'light' (presentation templates dressing that scheme) or an exact "
                "legacy label — retry with fewer filters (or none) to see what "
                "exists before concluding there is no suitable template."
            )

        header = (
            f"{len(payloads)} template{'s' if len(payloads) != 1 else ''} matched "
            "(structured metadata search — no semantic similarity involved):"
        )
        return "\n".join([header, *[_format_summary(p) for p in payloads]])

    async def _get_template(template_id: str = "", role: str = "", query: str = "") -> str:
        from app.api.v1.helpers import caller_visible_filter, get_user_group_ids
        from app.core.database import async_session
        from app.models.template_container import TemplateContainer
        from app.services.presentations import resolve_default_template_id
        from app.services.template_containers import (
            load_assets,
            record_lookups,
            serialize_container,
        )

        requested = (template_id or "").strip()
        target_id = safe_uuid(requested) if requested else None
        if requested and target_id is None:
            return (
                "Error: template_id must be a valid UUID (use search_templates to "
                "find one), or be omitted for the platform's default brand template."
            )
        role_filter = (role or "").strip().lower() or None
        if role_filter is not None and role_filter not in ASSET_ROLES:
            return f"Error: role must be one of {', '.join(ASSET_ROLES)} (got {role_filter!r})."
        query_filter = " ".join((query or "").split()) or None

        try:
            from sqlalchemy import select

            async with async_session() as db_session:
                if target_id is None:
                    # No id = the template a deck is styled with when the agent
                    # names none. generate_presentation applies it
                    # silently, so this is the ONLY way an agent learns which
                    # predefined assets — product logos, platform diagrams — that
                    # default brings along, and their file ids. Always a
                    # company-managed container, hence visible to every caller.
                    target_id = await resolve_default_template_id(db_session)
                    if target_id is None:
                        return (
                            "This platform has no default brand template: decks "
                            "render with the built-in theme and there are no "
                            "predefined assets. Use search_templates to list the "
                            "templates you can use."
                        )
                user_uuid = safe_uuid(acting_user_id) if acting_user_id else None
                group_ids = (
                    await get_user_group_ids(db_session, user_uuid)
                    if user_uuid is not None
                    else []
                )
                query = select(TemplateContainer).where(
                    caller_visible_filter(TemplateContainer, user_uuid, group_ids)
                )
                result = await db_session.execute(
                    query.where(TemplateContainer.id == target_id)
                )
                container = result.scalar_one_or_none()
                if container is None:
                    return (
                        f"No template found with id `{target_id}` that is visible "
                        "to you. Use search_templates to list the ones you can use."
                    )
                payload = serialize_container(
                    container, assets=await load_assets(db_session, container.id)
                )
                await record_lookups(db_session, [container])
                await db_session.commit()
        except Exception as exc:
            logger.error(
                "get_template tool failed: %s",
                format_exception_message(exc),
                exc_info=True,
            )
            return "Error: failed to read the template. Please try again."

        if role_filter or query_filter:
            return _format_filtered_assets(payload, role=role_filter, query=query_filter)
        return _format_detail(payload)

    executor = CallableToolExecutor()
    executor.register("search_templates", _search_templates)
    executor.register("get_template", _get_template)

    roles_hint = ", ".join(ASSET_ROLES)
    definitions = [
        ToolDefinition(
            name="search_templates",
            description=(
                "Find a reusable design template (a presentation deck / page "
                "design, or a report design) by its STRUCTURED metadata and get "
                "back its layouts, palette, the deck themes it supports and its "
                "asset references. This is an exact metadata filter, NOT a "
                "semantic/RAG search — it is a different tool from "
                "search_knowledge and never touches a knowledge base. Call it "
                "with no filters first to see what exists, then narrow down. "
                "`name` is a case-insensitive substring; `kind` and "
                "`aspect_ratio` must match EXACTLY; `theme` is 'dark', 'light' "
                "(the colour scheme a deck template can dress a deck in) or a "
                "legacy label; `tags` is a comma-separated list matched as any-of "
                "by default. A template's assets (logos of partners, integrations "
                "and customers included) are read with get_template. Asset roles you "
                f"may see: {roles_hint}."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Case-insensitive substring of the template name",
                    },
                    "kind": {
                        "type": "string",
                        "enum": list(TEMPLATE_KINDS),
                        "description": (
                            "What the template dresses: 'presentation' (slide "
                            "decks and pages), 'report' or 'video' (Video Studio)"
                        ),
                    },
                    "tags": {
                        "type": "string",
                        "description": (
                            "Comma-separated tags, e.g. 'quarterly,executive'. "
                            "Any-of by default."
                        ),
                    },
                    # A plain string, not an enum of the two schemes: the legacy
                    # free-text label is a legal value too, and an enum
                    # would make that form unreachable from the tool.
                    "theme": {
                        "type": "string",
                        "description": (
                            "'dark' or 'light' keeps the presentation templates able "
                            "to dress a deck in that scheme (their 'deck themes "
                            "supported'); any other value must equal a template's "
                            "legacy search label."
                        ),
                    },
                    "aspect_ratio": {
                        "type": "string",
                        "description": "Exact aspect ratio, e.g. '16:9', 'A4-portrait'",
                    },
                    "match_all_tags": {
                        "type": "boolean",
                        "description": "Require every tag instead of any (default false)",
                    },
                    "limit": {
                        "type": "integer",
                        "description": f"Max templates to return (default {_DEFAULT_LIMIT})",
                    },
                },
                "required": [],
            },
            source="builtin:design",
        ),
        ToolDefinition(
            name="get_template",
            description=(
                "Read ONE template with its full layout definitions, theme "
                "configuration and asset list — each asset with its role, file id "
                "and a description of what it depicts (architecture and product-"
                "flow diagrams, product screenshots, illustrations, badges, "
                "product logos, photos), grouped by role. Call it with NO "
                "template_id to read the platform's default brand template: that "
                "is the one generate_presentation styles a deck with when none is "
                "named, so this is how you learn which predefined assets you can "
                "place on a slide (`image` block, file_id; several logos at once: "
                "`logo_grid`). A brand template holds well over a hundred assets: "
                "pass `role` (e.g. 'integration_logo', 'customer_logo', "
                "'partner_logo', 'diagram') and/or `query` (a word of the filename "
                "or description, e.g. a vendor name) to list only the matching "
                "assets. Asset ids are stable across a binary replacement, so "
                "they are safe to reference."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "template_id": {
                        "type": "string",
                        "description": (
                            "UUID of the template container (from search_templates). "
                            "Omit to read the platform's default brand template."
                        ),
                    },
                    "role": {
                        "type": "string",
                        "enum": list(ASSET_ROLES),
                        "description": (
                            "List only the assets with this role (e.g. "
                            "'integration_logo' for the tools the products integrate "
                            "with, 'customer_logo' for a 'trusted by' wall, "
                            "'partner_logo', 'diagram', 'screenshot')."
                        ),
                    },
                    "query": {
                        "type": "string",
                        "description": (
                            "List only the assets whose filename or description "
                            "contains this text (case-insensitive), e.g. a vendor "
                            "or product name."
                        ),
                    },
                },
                "required": [],
            },
            source="builtin:design",
        ),
    ]

    return definitions, executor
