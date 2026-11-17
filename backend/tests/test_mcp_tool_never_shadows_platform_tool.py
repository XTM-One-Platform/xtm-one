"""An MCP server tool never replaces a platform tool of the same name (#46).

A bound ``/mcp/xtm`` server of another XTM One instance exposes the builder
tools (``get_agent``, ``list_agents``, ``load_skill``): replacing ours routed the
agent's own reads to that instance.
"""

from app.tools.base import CompositeToolExecutor, ToolDefinition


class _Executor:
    def __init__(self, label: str) -> None:
        self.label = label


def _tool(name: str, source: str) -> ToolDefinition:
    return ToolDefinition(name=name, description=f"{name} ({source})", source=source)


def test_a_server_tool_does_not_replace_a_builtin_of_its_name():
    composite = CompositeToolExecutor()
    builtin = _Executor("builtin")
    server = _Executor("server")
    composite.add_tools([_tool("get_agent", "builtin")], builtin)

    composite.add_tools([_tool("get_agent", "mcp:Other instance"), _tool("remote_only", "mcp:Other instance")], server)

    assert composite.get_tool_source("get_agent") == "builtin"
    assert composite._executors["get_agent"] is builtin
    assert [d.name for d in composite.all_tools].count("get_agent") == 1
    assert composite._executors["remote_only"] is server, "the server's other tools still load"


def test_a_server_tool_does_not_replace_an_integration_tool():
    composite = CompositeToolExecutor()
    integration = _Executor("integration")
    composite.add_tools([_tool("list_github_repos", "integration:github")], integration)

    composite.add_tools([_tool("list_github_repos", "mcp:GitHub")], _Executor("server"))

    assert composite.get_tool_source("list_github_repos") == "integration:github"
    assert composite._executors["list_github_repos"] is integration


def test_platform_tools_still_replace_each_other_and_servers_replace_servers():
    composite = CompositeToolExecutor()
    composite.add_tools([_tool("search", "mcp:A")], _Executor("a"))
    composite.add_tools([_tool("search", "mcp:B")], _Executor("b"))
    assert composite.get_tool_source("search") == "mcp:B"

    composite.add_tools([_tool("search", "builtin")], _Executor("platform"))
    assert composite.get_tool_source("search") == "builtin", "a platform tool takes a server tool's name"


def test_the_platform_resource_reader_still_replaces_a_server_tool():
    composite = CompositeToolExecutor()
    composite.add_tools([_tool("read_mcp_resource", "mcp:Wiki")], _Executor("server"))
    composite.add_tools([_tool("read_mcp_resource", "mcp:resources")], _Executor("platform"))
    assert composite.get_tool_source("read_mcp_resource") == "mcp:resources"
