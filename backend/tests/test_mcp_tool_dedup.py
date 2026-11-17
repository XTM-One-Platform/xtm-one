"""Tests for MCP tool deduplication of namespaced variants."""

from app.services.builtin_tools import _deduplicate_namespaced_tools
from app.tools.base import ToolDefinition


def _make_tool(name: str, description: str = "") -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=description or f"Tool: {name}",
        input_schema={"type": "object", "properties": {}},
        source="mcp:test-server",
    )


def test_dedup_removes_namespaced_variant_when_base_exists():
    """When both 'search_deals' and 'search_deals___ns__server__rw' exist, keep only the base."""
    tools = [
        _make_tool("search_deals"),
        _make_tool("search_deals___filigran__hubspot__read-write"),
    ]
    result = _deduplicate_namespaced_tools(tools)
    assert len(result) == 1
    assert result[0].name == "search_deals"


def test_dedup_keeps_namespaced_tool_when_no_base():
    """When only the namespaced variant exists (no base), keep it."""
    tools = [
        _make_tool("search_deals___filigran__hubspot__read-write"),
    ]
    result = _deduplicate_namespaced_tools(tools)
    assert len(result) == 1
    assert result[0].name == "search_deals___filigran__hubspot__read-write"


def test_dedup_keeps_all_when_no_namespace_separator():
    """Tools without '___' are never removed."""
    tools = [
        _make_tool("search_deals"),
        _make_tool("list_contacts"),
        _make_tool("get_company"),
    ]
    result = _deduplicate_namespaced_tools(tools)
    assert len(result) == 3


def test_dedup_handles_multiple_base_tools():
    """Multiple base+namespaced pairs are each deduped independently."""
    tools = [
        _make_tool("search_deals"),
        _make_tool("search_deals___filigran__hubspot__read-write"),
        _make_tool("list_contacts"),
        _make_tool("list_contacts___filigran__hubspot__read-write"),
        _make_tool("get_company"),
    ]
    result = _deduplicate_namespaced_tools(tools)
    assert len(result) == 3
    names = [t.name for t in result]
    assert "search_deals" in names
    assert "list_contacts" in names
    assert "get_company" in names


def test_dedup_empty_list():
    """Empty input returns empty output."""
    assert _deduplicate_namespaced_tools([]) == []


def test_dedup_preserves_order():
    """Base tools retain their original order."""
    tools = [
        _make_tool("list_contacts"),
        _make_tool("search_deals"),
        _make_tool("search_deals___filigran__hubspot__read-write"),
        _make_tool("get_company"),
        _make_tool("list_contacts___filigran__hubspot__read-write"),
    ]
    result = _deduplicate_namespaced_tools(tools)
    names = [t.name for t in result]
    assert names == ["list_contacts", "search_deals", "get_company"]


def test_dedup_does_not_remove_similar_prefix_without_separator():
    """A tool named 'search_deals_advanced' should NOT be deduped even if 'search_deals' exists."""
    tools = [
        _make_tool("search_deals"),
        _make_tool("search_deals_advanced"),
    ]
    result = _deduplicate_namespaced_tools(tools)
    assert len(result) == 2
