"""The OpenCTI playbook graph tools read the graph back and refuse what OpenCTI
would get wrong.

* ``manage_opencti_playbook`` action ``get`` lists every node (id, name,
  component, position, configuration) and every link (id, from node and port,
  to node), bounded with a note on a very large graph.
* ``update_positions`` sends the ``[{id, position: {x, y}}]`` array
  ``playbookUpdatePositions`` reads (it calls ``.filter`` on it: ``{}`` was an
  INTERNAL_SERVER_ERROR) and converts the friendly ``{node_id: {x, y}}`` map.
* Graph edits on one playbook are serialized: OpenCTI rewrites the whole
  definition on every mutation, so two concurrent edits lost a node.
* An edit naming a node or link the playbook does not hold is refused, where
  OpenCTI answered a delete of an unknown id with a success.

No network: ``_graphql`` is replaced by an in-memory OpenCTI that does the same
read-modify-write as ``playbook-domain.ts``.
"""

import asyncio
import json

import app.integrations.providers.opencti.playbooks as playbooks_mod
from app.tools.base import CallableToolExecutor, ToolCall, is_error_shaped_result

ENTRY = "PLAYBOOK_INTERNAL_DATA_STREAM"


class _FakeOpenCTI:
    """One playbook, stored as OpenCTI stores it: a JSON definition string.

    Each mutation reads the definition, yields (a real request takes time),
    then writes what it read plus its change: concurrent mutations race the
    way they do on the platform.
    """

    def __init__(self, nodes=None, links=None):
        self.definition = json.dumps({"nodes": nodes or [], "links": links or []})
        self.calls: list[tuple[str, dict | None]] = []
        self.counter = 0

    def _new_id(self, prefix):
        self.counter += 1
        return f"{prefix}-{self.counter}"

    async def __call__(self, client, base_url, headers, query, variables=None):
        self.calls.append((query, variables))
        variables = variables or {}
        if "playbookComponents" in query:
            return {"playbookComponents": []}, None
        if "query Playbook" in query or "PlaybookGraph" in query:
            return {
                "playbook": {
                    "id": "pb-1",
                    "name": "Triage",
                    "playbook_running": False,
                    "playbook_definition": self.definition,
                    "queue_messages": 0,
                    "last_executions": [],
                }
            }, None
        read = json.loads(self.definition)
        await asyncio.sleep(0)
        if "playbookAddNode" in query:
            node_id = self._new_id("node")
            node = dict(variables["input"], id=node_id)
            read["nodes"].append(node)
            result = {"playbookAddNode": node_id}
        elif "playbookAddLink" in query:
            known = {n["id"] for n in read["nodes"]}
            inp = variables["input"]
            if inp["from_node"] not in known or inp["to_node"] not in known:
                return {}, "GraphQL error in 'AddLink': Playbook link node from not found"
            if inp["from_port"] not in ("out", "unmatch"):
                return {}, "GraphQL error in 'AddLink': Playbook link invalid from configuration"
            link_id = self._new_id("link")
            read["links"].append(
                {
                    "id": link_id,
                    "from": {"id": inp["from_node"], "port": inp["from_port"]},
                    "to": {"id": inp["to_node"]},
                }
            )
            result = {"playbookAddLink": link_id}
        elif "playbookDeleteNode" in query:
            read["nodes"] = [n for n in read["nodes"] if n["id"] != variables["nodeId"]]
            result = {"playbookDeleteNode": {"id": "pb-1"}}
        elif "playbookDeleteLink" in query:
            read["links"] = [link for link in read["links"] if link["id"] != variables["linkId"]]
            result = {"playbookDeleteLink": {"id": "pb-1"}}
        elif "playbookUpdatePositions" in query:
            positions = json.loads(variables["positions"])
            if not isinstance(positions, list):
                return {}, (
                    "GraphQL error in 'UpdatePositions': nodesPositions.filter is not a "
                    "function [INTERNAL_SERVER_ERROR]"
                )
            moved = {p["id"]: p["position"] for p in positions}
            for node in read["nodes"]:
                if node["id"] in moved:
                    node["position"] = moved[node["id"]]
            result = {"playbookUpdatePositions": "pb-1"}
        else:
            return {}, None
        self.definition = json.dumps(read)
        return result, None

    def mutations(self, name):
        return [v for q, v in self.calls if name in q]

    def graph(self):
        return json.loads(self.definition)


def _build(monkeypatch, fake):
    monkeypatch.setattr(playbooks_mod, "_graphql", fake)
    executor = CallableToolExecutor()
    playbooks_mod.register("https://demo.opencti.io", {}, executor)
    return executor


async def _edit(executor, **arguments):
    result = await executor.execute_tool(
        ToolCall(id="e", name="edit_opencti_playbook_graph", arguments={"playbook_id": "pb-1", **arguments})
    )
    return result.content


def _seeded():
    return _FakeOpenCTI(
        nodes=[
            {
                "id": "entry",
                "name": "Listen",
                "component_id": ENTRY,
                "position": {"x": 0, "y": 0},
                "configuration": json.dumps({"create": True, "filters": '{"mode":"and"}'}),
            },
            {"id": "log", "name": "Log", "component_id": "PLAYBOOK_LOGGING_COMPONENT", "position": {"x": 0, "y": 150}},
        ],
        links=[{"id": "l-1", "from": {"id": "entry", "port": "out"}, "to": {"id": "log"}}],
    )


# ── get: the whole graph ───────────────────────────────────────────────────


async def test_get_lists_every_node_and_link_with_ids_and_configuration(monkeypatch):
    executor = _build(monkeypatch, _seeded())
    result = await executor.execute_tool(
        ToolCall(id="g", name="manage_opencti_playbook", arguments={"action": "get", "playbook_id": "pb-1"})
    )
    out = result.content
    assert "graph: 2 nodes, 1 links" in out
    assert f"- entry 'Listen' [{ENTRY}] at (0, 0)" in out
    assert 'configuration: {"create": true' in out
    assert "- log 'Log' [PLAYBOOK_LOGGING_COMPONENT] at (0, 150)" in out
    assert "- l-1: entry (port out) -> log" in out
    # Node names and configurations are user-written text: quarantined.
    assert "UNTRUSTED" in out


async def test_get_bounds_a_very_large_graph_with_a_note(monkeypatch):
    nodes = [{"id": f"n{i}", "name": "x", "component_id": "C"} for i in range(250)]
    executor = _build(monkeypatch, _FakeOpenCTI(nodes=nodes))
    result = await executor.execute_tool(
        ToolCall(id="g", name="manage_opencti_playbook", arguments={"action": "get", "playbook_id": "pb-1"})
    )
    assert "graph: 250 nodes, 0 links" in result.content
    assert "(50 more nodes not shown)" in result.content
    assert "- n199 " in result.content
    assert "- n200 " not in result.content


# ── update_positions: the shape OpenCTI reads ──────────────────────────────


async def test_update_positions_converts_a_map_and_moves_the_nodes(monkeypatch):
    fake = _seeded()
    executor = _build(monkeypatch, fake)
    out = await _edit(executor, action="update_positions", positions=json.dumps({"log": {"x": 300, "y": 40}}))
    assert out == "Updated the positions of 1 node(s) of playbook pb-1."
    sent = json.loads(fake.mutations("playbookUpdatePositions")[-1]["positions"])
    assert sent == [{"id": "log", "position": {"x": 300, "y": 40}}]
    assert {n["id"]: n.get("position") for n in fake.graph()["nodes"]}["log"] == {"x": 300, "y": 40}


async def test_update_positions_accepts_the_array_opencti_stores(monkeypatch):
    fake = _seeded()
    executor = _build(monkeypatch, fake)
    out = await _edit(
        executor,
        action="update_positions",
        positions=json.dumps([{"id": "entry", "position": {"x": 5, "y": 6}}]),
    )
    assert out.startswith("Updated the positions of 1 node(s)")


async def test_update_positions_refuses_an_empty_or_wrong_shape_naming_the_expected_one(monkeypatch):
    fake = _seeded()
    executor = _build(monkeypatch, fake)
    for bad in ("{}", "[]", '{"log": [1, 2]}', '[{"id": "log"}]', '"x"', "{not json"):
        out = await _edit(executor, action="update_positions", positions=bad)
        assert is_error_shaped_result(out), (bad, out)
        assert '{"NODE_ID": {"x": 100, "y": 200}}' in out, (bad, out)
        assert "INTERNAL_SERVER_ERROR" not in out
    assert fake.mutations("playbookUpdatePositions") == []


async def test_update_positions_refuses_an_unknown_node(monkeypatch):
    fake = _seeded()
    executor = _build(monkeypatch, fake)
    out = await _edit(executor, action="update_positions", positions=json.dumps({"ghost": {"x": 1, "y": 2}}))
    assert out.startswith("Error: unknown node id 'ghost' in playbook pb-1")
    assert fake.mutations("playbookUpdatePositions") == []


# ── Unknown ids are refused before any write ───────────────────────────────


async def test_delete_node_with_an_unknown_id_is_an_error(monkeypatch):
    fake = _seeded()
    executor = _build(monkeypatch, fake)
    out = await _edit(executor, action="delete_node", node_id="probe-unknown")
    assert is_error_shaped_result(out)
    assert "unknown node id 'probe-unknown'" in out
    assert "entry, log" in out
    assert fake.mutations("playbookDeleteNode") == []


async def test_delete_link_with_an_unknown_id_is_an_error(monkeypatch):
    fake = _seeded()
    executor = _build(monkeypatch, fake)
    out = await _edit(executor, action="delete_link", link_id="probe-unknown")
    assert out.startswith("Error: unknown link id 'probe-unknown'")
    assert fake.mutations("playbookDeleteLink") == []


async def test_replace_node_and_add_link_name_the_unknown_ids(monkeypatch):
    fake = _seeded()
    executor = _build(monkeypatch, fake)
    replaced = await _edit(
        executor, action="replace_node", node_id="ghost", name="X", component_id="PLAYBOOK_LOGGING_COMPONENT"
    )
    linked = await _edit(executor, action="add_link", from_node="entry", from_port="out", to_node="nowhere")
    assert "unknown node id 'ghost'" in replaced
    assert "unknown node id 'nowhere'" in linked
    assert fake.mutations("playbookReplaceNode") == []
    assert fake.mutations("playbookAddLink") == []


async def test_a_known_id_is_deleted(monkeypatch):
    fake = _seeded()
    executor = _build(monkeypatch, fake)
    assert await _edit(executor, action="delete_link", link_id="l-1") == "Deleted link l-1."
    assert fake.graph()["links"] == []


# ── Concurrent edits on one playbook are serialized ────────────────────────


async def test_concurrent_add_node_and_add_link_keep_every_node(monkeypatch):
    """Two add_node and one add_link in one turn: OpenCTI's read-modify-write
    lost nodes when they raced. Serialized, every node is kept and the link
    lands."""
    fake = _seeded()
    executor = _build(monkeypatch, fake)
    results = await asyncio.gather(
        _edit(executor, action="add_node", name="A", component_id="PLAYBOOK_LOGGING_COMPONENT"),
        _edit(executor, action="add_node", name="B", component_id="PLAYBOOK_LOGGING_COMPONENT"),
        _edit(executor, action="add_link", from_node="log", from_port="out", to_node="entry"),
    )
    assert all(not is_error_shaped_result(r) for r in results), results
    graph = fake.graph()
    assert len(graph["nodes"]) == 4
    assert len(graph["links"]) == 2


async def test_the_race_is_real_without_the_lock(monkeypatch):
    """The fake does lose a write when two mutations interleave: the test
    above proves the lock, not a fake that cannot race."""
    fake = _seeded()
    node_input = {"name": "A", "component_id": "C", "position": {"x": 0, "y": 0}}
    query = "mutation AddNode { playbookAddNode }"
    await asyncio.gather(
        fake(None, "", {}, query, {"id": "pb-1", "input": node_input}),
        fake(None, "", {}, query, {"id": "pb-1", "input": node_input}),
    )
    assert len(fake.graph()["nodes"]) == 3


async def test_a_node_lost_to_another_writer_is_reported(monkeypatch):
    """A writer outside this process (the OpenCTI UI) rewrote the definition
    between the write and its read-back: the call fails loudly."""
    fake = _seeded()
    original = fake.__call__

    async def _clobber(client, base_url, headers, query, variables=None):
        result = await original(client, base_url, headers, query, variables)
        if "playbookAddNode" in query:
            graph = fake.graph()
            graph["nodes"] = [n for n in graph["nodes"] if n["id"] in ("entry", "log")]
            fake.definition = json.dumps(graph)
        return result

    monkeypatch.setattr(playbooks_mod, "_graphql", _clobber)
    executor = CallableToolExecutor()
    playbooks_mod.register("https://demo.opencti.io", {}, executor)
    out = await _edit(executor, action="add_node", name="A", component_id="PLAYBOOK_LOGGING_COMPONENT")
    assert is_error_shaped_result(out)
    assert "node node-1 is missing from playbook pb-1 when read back" in out


# ── add_node and the editor's one-root layout ──────────────────────────────


async def test_add_node_without_a_parent_warns_it_has_none(monkeypatch):
    """The OpenCTI editor refuses a playbook with two parentless nodes
    ("multiple roots"): the node is added and the answer names it."""
    fake = _seeded()
    executor = _build(monkeypatch, fake)
    out = await _edit(executor, action="add_node", name="A", component_id="PLAYBOOK_LOGGING_COMPONENT")
    assert out.startswith("Added node (node_id: node-1).")
    assert "WARNING: nodes without a parent link: entry, node-1." in out
    assert not is_error_shaped_result(out)
    result = await executor.execute_tool(
        ToolCall(id="g", name="manage_opencti_playbook", arguments={"action": "get", "playbook_id": "pb-1"})
    )
    assert "- node-1 'A'" in result.content


async def test_add_node_with_a_parent_creates_the_node_and_its_link(monkeypatch):
    fake = _seeded()
    executor = _build(monkeypatch, fake)
    out = await _edit(
        executor,
        action="add_node",
        name="A",
        component_id="PLAYBOOK_LOGGING_COMPONENT",
        parent_node_id="log",
        parent_port_id="out",
    )
    assert out == "Added node (node_id: node-1) under node log (port out, link_id: link-2)."
    graph = fake.graph()
    assert "node-1" in {n["id"] for n in graph["nodes"]}
    assert {"id": "link-2", "from": {"id": "log", "port": "out"}, "to": {"id": "node-1"}} in graph["links"]


async def test_add_node_with_an_invalid_port_leaves_no_orphan(monkeypatch):
    fake = _seeded()
    executor = _build(monkeypatch, fake)
    out = await _edit(
        executor,
        action="add_node",
        name="A",
        component_id="PLAYBOOK_LOGGING_COMPONENT",
        parent_node_id="log",
        parent_port_id="nope",
    )
    assert is_error_shaped_result(out)
    assert "invalid from configuration" in out
    assert "The new node was removed again" in out
    assert {n["id"] for n in fake.graph()["nodes"]} == {"entry", "log"}
    assert len(fake.graph()["links"]) == 1


async def test_add_node_with_an_unknown_parent_or_half_a_parent_writes_nothing(monkeypatch):
    fake = _seeded()
    executor = _build(monkeypatch, fake)
    unknown = await _edit(
        executor, action="add_node", name="A", component_id="C", parent_node_id="ghost", parent_port_id="out"
    )
    half = await _edit(executor, action="add_node", name="A", component_id="C", parent_node_id="log")
    assert "unknown node id 'ghost'" in unknown
    assert half.startswith("Error: parent_node_id and parent_port_id go together")
    assert fake.mutations("playbookAddNode") == []


def test_parentless_warning_is_silent_on_a_single_tree():
    nodes = [{"id": "a"}, {"id": "b"}]
    links = [{"id": "l", "from": {"id": "a", "port": "out"}, "to": {"id": "b"}}]
    assert playbooks_mod._parentless_warning((nodes, links)) == ""
    assert playbooks_mod._parentless_warning(None) == ""
    assert "a, c" in playbooks_mod._parentless_warning(([*nodes, {"id": "c"}], links))


async def test_edits_on_different_playbooks_are_not_serialized_together():
    assert playbooks_mod._graph_lock("u", "pb-1") is playbooks_mod._graph_lock("u", "pb-1")
    assert playbooks_mod._graph_lock("u", "pb-1") is not playbooks_mod._graph_lock("u", "pb-2")
    assert playbooks_mod._graph_lock("u", "pb-1") is not playbooks_mod._graph_lock("v", "pb-1")
