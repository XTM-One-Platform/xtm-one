"""edit_opencti_playbook_graph re-points a link and moves a node in one write.

OpenCTI ships ``playbookRelink`` and ``playbookMoveNode`` (opencti #19969);
the tool exposes them as ``relink`` and ``move_node`` (xtm-one #1801), so an
agent no longer deletes and re-adds links, which could leave a node without a
parent. Unknown ids are refused before anything is sent, as for the other edits.
"""

import json

import app.integrations.providers.opencti.playbooks as playbooks_mod
from app.tools.base import CallableToolExecutor, ToolCall
from tests.test_opencti_playbook_graph import _FakeOpenCTI


class _Fake(_FakeOpenCTI):
    """The graph fake, with the two single-write mutations of playbook-domain.ts."""

    async def __call__(self, client, base_url, headers, query, variables=None):
        if "playbookRelink" not in query and "playbookMoveNode" not in query:
            return await super().__call__(client, base_url, headers, query, variables)
        self.calls.append((query, variables))
        read = json.loads(self.definition)
        if "playbookRelink" in query:
            link = next(link for link in read["links"] if link["id"] == variables["linkId"])
            link["to"] = {"id": variables["toNodeId"]}
            if variables.get("fromNodeId"):
                link["from"] = {"id": variables["fromNodeId"], "port": variables["fromPortId"]}
            result = {"playbookRelink": link["id"]}
        else:
            read["links"] = [link for link in read["links"] if link["to"]["id"] != variables["nodeId"]]
            link_id = self._new_id("link")
            read["links"].append(
                {
                    "id": link_id,
                    "from": {"id": variables["parentNodeId"], "port": variables["parentPortId"]},
                    "to": {"id": variables["nodeId"]},
                }
            )
            result = {"playbookMoveNode": link_id}
        self.definition = json.dumps(read)
        return result, None


def _seeded() -> _Fake:
    return _Fake(
        nodes=[
            {
                "id": "entry",
                "name": "Listen",
                "component_id": "PLAYBOOK_INTERNAL_DATA_STREAM",
                "position": {"x": 0, "y": 0},
            },
            {
                "id": "filter",
                "name": "Filter",
                "component_id": "PLAYBOOK_FILTERING_COMPONENT",
                "position": {"x": 0, "y": 150},
            },
            {"id": "log", "name": "Log", "component_id": "PLAYBOOK_LOGGING_COMPONENT", "position": {"x": 0, "y": 300}},
        ],
        links=[
            {"id": "l-1", "from": {"id": "entry", "port": "out"}, "to": {"id": "filter"}},
            {"id": "l-2", "from": {"id": "filter", "port": "out"}, "to": {"id": "log"}},
        ],
    )


async def _edit(monkeypatch, fake, **arguments) -> str:
    monkeypatch.setattr(playbooks_mod, "_graphql", fake)
    executor = CallableToolExecutor()
    playbooks_mod.register("https://demo.opencti.io", {}, executor)
    result = await executor.execute_tool(
        ToolCall(id="e", name="edit_opencti_playbook_graph", arguments={"playbook_id": "pb-1", **arguments})
    )
    return result.content


async def test_relink_points_the_link_at_a_new_target(monkeypatch):
    fake = _seeded()
    out = await _edit(monkeypatch, fake, action="relink", link_id="l-1", to_node="log")
    assert out.startswith("Relinked link l-1 to node log"), out
    (sent,) = fake.mutations("playbookRelink")
    assert sent["fromNodeId"] is None and sent["fromPortId"] is None
    assert next(link for link in fake.graph()["links"] if link["id"] == "l-1")["to"] == {"id": "log"}


async def test_relink_with_a_new_source_sends_node_and_port(monkeypatch):
    fake = _seeded()
    out = await _edit(
        monkeypatch, fake, action="relink", link_id="l-2", to_node="log", from_node="entry", from_port="out"
    )
    assert out.startswith("Relinked link l-2"), out
    (sent,) = fake.mutations("playbookRelink")
    assert (sent["fromNodeId"], sent["fromPortId"]) == ("entry", "out")


async def test_relink_refuses_a_source_node_without_its_port(monkeypatch):
    fake = _seeded()
    out = await _edit(monkeypatch, fake, action="relink", link_id="l-1", to_node="log", from_node="entry")
    assert "from_node and from_port go together" in out
    assert fake.mutations("playbookRelink") == []


async def test_relink_refuses_an_unknown_link(monkeypatch):
    fake = _seeded()
    out = await _edit(monkeypatch, fake, action="relink", link_id="ghost", to_node="log")
    assert "unknown link id 'ghost'" in out
    assert fake.mutations("playbookRelink") == []


async def test_move_node_moves_the_branch_in_one_write(monkeypatch):
    fake = _seeded()
    out = await _edit(
        monkeypatch, fake, action="move_node", node_id="log", parent_node_id="entry", parent_port_id="out"
    )
    assert out.startswith("Moved node log and its branch under node entry"), out
    assert len(fake.mutations("playbookMoveNode")) == 1
    assert fake.mutations("playbookDeleteLink") == [] and fake.mutations("playbookAddLink") == []
    into_log = [link for link in fake.graph()["links"] if link["to"]["id"] == "log"]
    assert [link["from"]["id"] for link in into_log] == ["entry"]


async def test_move_node_refuses_an_unknown_node(monkeypatch):
    fake = _seeded()
    out = await _edit(
        monkeypatch, fake, action="move_node", node_id="ghost", parent_node_id="entry", parent_port_id="out"
    )
    assert "unknown node id 'ghost'" in out
    assert fake.mutations("playbookMoveNode") == []


async def test_move_node_names_its_required_fields(monkeypatch):
    fake = _seeded()
    out = await _edit(monkeypatch, fake, action="move_node", node_id="log")
    assert "node_id, parent_node_id and parent_port_id are required" in out
