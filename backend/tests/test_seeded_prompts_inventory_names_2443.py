"""Seeded prompts query the inventory, never a removed category or tool (#2443, S3).

Every built-in persona, assignment prompt, skill and knowledge seed under
``app/agents`` is scanned: a prompt that still sends an agent to the
``assets_exposure`` or ``software`` category of ``get_organization_context``,
or to ``list_ctem_assets``, fails the build. The inventory is answered by the
query functions of ``builtin_tools/inventory_query.py``, and every one of them
a seeded prompt names exists.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.services.builtin_tools.inventory_query import INVENTORY_QUERY_TOOLS

_AGENTS = Path(__file__).resolve().parents[1] / "app" / "agents"

#: What a seeded prompt may no longer name, with the query that replaces it.
_REMOVED: dict[str, re.Pattern[str]] = {
    "the assets_exposure category (search_assets, get_asset)": re.compile(r"assets_exposure"),
    "list_ctem_assets (search_assets)": re.compile(r"list_ctem_assets"),
    "a get_organization_context category read (search_software, find_assets_running)": re.compile(
        r"get_organization_context`?[^\n.]{0,40}\bcategor"
    ),
}


def _seeded_texts() -> list[tuple[str, str]]:
    files = [p for p in _AGENTS.rglob("*") if p.is_file() and p.suffix in {".md", ".py", ".json"}]
    files = [p for p in files if "model_prices" not in p.name]
    return [(str(p.relative_to(_AGENTS)), p.read_text(encoding="utf-8")) for p in sorted(files)]


def test_no_seeded_prompt_names_a_removed_category_or_tool() -> None:
    found = [
        f"{name}: {what}"
        for name, text in _seeded_texts()
        for what, pattern in _REMOVED.items()
        if pattern.search(text)
    ]
    assert found == []


def test_every_inventory_function_a_seeded_prompt_names_exists() -> None:
    named = {match for _, text in _seeded_texts() for match in re.findall(r"`([a-z]+_[a-z_]+)`", text)}
    used = {"search_software", "find_assets_running", "get_asset", "search_assets", "list_primary_assets"}
    assert used <= named
    assert used <= set(INVENTORY_QUERY_TOOLS)
