"""OpenCTI live writes, label tools and draft lifecycle tools (#11842).

* The integration's opt-in Live writes setting (``live_writes``) skips the
  draft gate exactly as ``direct_mutations`` does; off, the gate is unchanged.
* ``list_opencti_labels`` reads labels; ``create_opencti_label`` writes one
  outside any draft (platform configuration) and is registered only with Live
  writes on; the create is idempotent on OpenCTI's normalised value.
* With the setting off, no tool that writes outside a draft is registered and
  no registered tool description names live writes (Sam, 14 Nov 2026: "when the
  option is gone, it's gone in the tools").
* A ``Label`` type in ``search_opencti`` / ``list_opencti_entities`` is
  answered by the labels query, never by other object types.
* ``delete_opencti_draft`` / ``apply_opencti_draft`` name the draft and
  confirm its final state; validate exists only with Live writes on, and
  delete needs a draft of this run or Live writes.

These drive the real ``_client`` / ``_graphql`` path with
``httpx.AsyncClient.request`` patched, like ``test_opencti_draft_context_errors.py``.
"""

from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest

from app.integrations.providers.opencti import build_tools
from app.services.tool_classification import infer_tool_type
from app.tools.base import CallableToolExecutor, ToolCall


def _build(*, live_writes: bool = False, autonomous: bool = False):
    executor = CallableToolExecutor()
    definitions = build_tools(
        "https://octi.test||static-token",
        executor,
        autonomous=autonomous,
        live_writes=live_writes,
    )
    return executor, {d.name for d in definitions}


async def _drive(executor, calls, answer):
    """Run the tool calls; return (draft header, query, variables) per request."""
    sent: list[tuple[str | None, str, dict]] = []

    async def _fake(self, method, url, **kwargs):
        headers = kwargs.get("headers") or {}
        body = kwargs.get("json") or {}
        query = body.get("query", "")
        variables = body.get("variables") or {}
        sent.append((headers.get("opencti-draft-id"), query, variables))
        payload, status = answer(headers, query, variables)
        return httpx.Response(status, json=payload, request=httpx.Request(method, url))

    results = []
    with patch.object(httpx.AsyncClient, "request", _fake):
        for name, arguments in calls:
            result = await executor.execute_tool(ToolCall(id=name, name=name, arguments=arguments))
            results.append(result.content)
    return sent, results


# ---------------------------------------------------------------------------
# The draft gate and the Live writes setting
# ---------------------------------------------------------------------------


def _graph(headers, query, variables):
    if "draftWorkspaceAdd" in query:
        return {"data": {"draftWorkspaceAdd": {"id": "draft-auto"}}}, 200
    if "stixDomainObjectEdit" in query or "fieldPatch" in query:
        return {"data": {"stixDomainObjectEdit": {"fieldPatch": {"id": "x"}}}}, 200
    return {"data": {}}, 200


@pytest.mark.asyncio
async def test_live_writes_off_keeps_the_interactive_draft_gate():
    executor, _ = _build(live_writes=False)
    sent, results = await _drive(
        executor,
        [("delete_opencti_entity", {"entity_id": "x"})],
        _graph,
    )
    assert "draft" in results[0].lower()
    assert sent == [], "the gate answers before any request"


@pytest.mark.asyncio
async def test_live_writes_off_keeps_the_autonomous_auto_draft():
    executor, _ = _build(live_writes=False, autonomous=True)
    sent, _results = await _drive(
        executor,
        [("delete_opencti_entity", {"entity_id": "x"})],
        _graph,
    )
    assert any("draftWorkspaceAdd" in q for _d, q, _v in sent)
    assert sent[-1][0] == "draft-auto", "the change itself is staged in the new draft"


@pytest.mark.asyncio
async def test_live_writes_on_writes_to_the_live_graph_without_a_draft():
    executor, _ = _build(live_writes=True)
    sent, _results = await _drive(
        executor,
        [("delete_opencti_entity", {"entity_id": "x"})],
        _graph,
    )
    assert sent, "the mutation is sent"
    assert not any("draftWorkspaceAdd" in q for _d, q, _v in sent), "no draft is created"
    assert all(not d for d, _q, _v in sent), "no request carries a draft"


def test_live_writes_on_drops_the_gate_wording_from_the_descriptions():
    executor_off = CallableToolExecutor()
    off = {d.name: d.description for d in build_tools("https://octi.test||t", executor_off)}
    executor_on = CallableToolExecutor()
    on = {
        d.name: d.description
        for d in build_tools("https://octi.test||t", executor_on, live_writes=True)
    }
    assert "draft" in off["delete_opencti_entity"].lower()
    assert len(on["delete_opencti_entity"]) < len(off["delete_opencti_entity"])


# ---------------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------------


def _labels(existing: list[dict]):
    store = existing  # shared, so a wrapper and the test read the same labels

    def answer(headers, query, variables):
        if "LabelByName" in query:
            wanted = str(variables.get("id", "")).lower().strip()
            hit = next((label for label in store if label["value"].lower() == wanted), None)
            return {"data": {"label": hit}}, 200
        if "FindLabel" in query:
            return {"data": {"labels": {"edges": []}}}, 200
        if "ListLabels" in query:
            search = str(variables.get("search") or "").lower()
            nodes = [label for label in store if search in label["value"].lower()]
            return {
                "data": {
                    "labels": {
                        "edges": [{"node": n} for n in nodes],
                        "pageInfo": {"endCursor": None, "hasNextPage": False, "globalCount": len(nodes)},
                    }
                }
            }, 200
        if "labelAdd" in query:
            data = variables["input"]
            created = {"id": f"label-{len(store) + 1}", "value": data["value"], "color": data.get("color")}
            store.append(created)
            return {"data": {"labelAdd": created}}, 200
        if "PatchLabel" in query:
            hit = next(label for label in store if label["id"] == variables["id"])
            for edit in variables["input"]:
                hit[edit["key"]] = edit["value"][0]
            return {"data": {"labelEdit": {"fieldPatch": dict(hit)}}}, 200
        if "DeleteLabel" in query:
            store[:] = [label for label in store if label["id"] != variables["id"]]
            return {"data": {"labelEdit": {"delete": variables["id"]}}}, 200
        if "stixCoreObjects" in query or "stixDomainObjects" in query:
            node = {"id": "vuln-1", "entity_type": "Vulnerability", "name": "CVE-2024-0001"}
            key = "stixCoreObjects" if "stixCoreObjects" in query else "stixDomainObjects"
            return {"data": {key: {"edges": [{"node": node}]}}}, 200
        return {"data": {}}, 200

    return answer


@pytest.mark.asyncio
async def test_create_then_list_a_label_in_the_live_graph():
    executor, names = _build(live_writes=True)
    assert {"list_opencti_labels", "create_opencti_label"} <= names
    sent, results = await _drive(
        executor,
        [
            ("create_opencti_label", {"value": "region:test-a", "color": "#ff8800"}),
            ("list_opencti_labels", {"search": "region:test"}),
        ],
        _labels([]),
    )
    assert results[0].startswith("✅ Label 'region:test-a' created"), results[0]
    assert "region:test-a" in results[1]
    assert "#ff8800" in results[1]
    assert not any("draftWorkspaceAdd" in q for _d, q, _v in sent), "a label needs no draft"
    assert all(not d for d, _q, _v in sent), "labels are read and written live"


@pytest.mark.asyncio
async def test_create_label_is_idempotent_on_the_normalised_value():
    executor, _ = _build(live_writes=True)
    sent, results = await _drive(
        executor,
        [("create_opencti_label", {"value": "  Region:Test-A "})],
        _labels([{"id": "label-9", "value": "region:test-a", "color": "#000000"}]),
    )
    assert "already exists" in results[0] and "label-9" in results[0]
    assert not any("labelAdd" in q for _d, q, _v in sent)


@pytest.mark.asyncio
async def test_create_label_refuses_a_color_that_is_not_hex():
    executor, _ = _build(live_writes=True)
    sent, results = await _drive(
        executor, [("create_opencti_label", {"value": "a", "color": "orange"})], _labels([])
    )
    assert results[0].startswith("Error: color 'orange'")
    assert sent == []


@pytest.mark.asyncio
async def test_create_label_runs_live_even_in_a_ui_draft():
    executor = CallableToolExecutor()
    build_tools(
        "https://octi.test||static-token",
        executor,
        scope_headers={"opencti-draft-id": "draft-1"},
        live_writes=True,
    )
    sent, results = await _drive(
        executor, [("create_opencti_label", {"value": "cfg"})], _labels([])
    )
    assert results[0].startswith("✅ Label 'cfg' created"), results[0]
    assert all(not d for d, _q, _v in sent)


_LABEL_UUID = "5a0b7a4e-1c2d-4e5f-8a9b-0c1d2e3f4a5b"


def _labels_by_id(existing: list[dict]):
    """``_labels`` with ``label(id:)`` also answering an id, as OpenCTI does."""
    base = _labels(existing)
    store = existing

    def answer(headers, query, variables):
        if "LabelByName" in query:
            hit = next((label for label in store if label["id"] == variables.get("id")), None)
            if hit:
                return {"data": {"label": hit}}, 200
        return base(headers, query, variables)

    return answer


@pytest.mark.asyncio
async def test_create_update_then_delete_a_label():
    """The acceptance path: create, update value and color, read back, delete, read back."""
    executor, names = _build(live_writes=True)
    assert "manage_opencti_label" in names
    store: list[dict] = []
    sent, results = await _drive(
        executor,
        [
            ("create_opencti_label", {"value": "test-x"}),
            (
                "manage_opencti_label",
                {"action": "update", "label": "test-x", "value": "test-y", "color": "#00aa11"},
            ),
            ("list_opencti_labels", {"search": "test-y"}),
            ("manage_opencti_label", {"action": "delete", "label": "label-1"}),
            ("list_opencti_labels", {"search": "test-y"}),
        ],
        _labels_by_id(store),
    )
    assert "label-1" in results[0], results[0]
    assert results[1].startswith("✅ Label 'test-x' (id: `label-1`) updated"), results[1]
    assert "test-y" in results[2] and "#00aa11" in results[2], results[2]
    assert results[3].startswith("✅ Label 'test-y' (id: `label-1`) deleted"), results[3]
    assert results[4] == "No label matching 'test-y' in OpenCTI."
    assert all(not d for d, _q, _v in sent), "labels are never written into a draft"


@pytest.mark.asyncio
async def test_manage_label_takes_an_id():
    executor, _ = _build(live_writes=True)
    store = [{"id": _LABEL_UUID, "value": "old", "color": "#000000"}]
    _sent, results = await _drive(
        executor,
        [("manage_opencti_label", {"action": "update", "label": _LABEL_UUID, "color": "#ffffff"})],
        _labels_by_id(store),
    )
    assert results[0].startswith(f"✅ Label 'old' (id: `{_LABEL_UUID}`) updated"), results[0]
    assert store[0]["color"] == "#ffffff" and store[0]["value"] == "old"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "start"),
    [
        ({"action": "rename", "label": "a"}, "Error: `action` is `update` or `delete`"),
        ({"action": "update", "label": "a"}, "Error: `update` needs a new `value`"),
        ({"action": "update", "label": "a", "color": "red"}, "Error: color 'red'"),
        ({"action": "delete", "label": " "}, "Error: `label`"),
    ],
)
async def test_manage_label_refuses_bad_arguments_before_any_request(arguments, start):
    executor, _ = _build(live_writes=True)
    sent, results = await _drive(executor, [("manage_opencti_label", arguments)], _labels([]))
    assert results[0].startswith(start), results[0]
    assert sent == []


@pytest.mark.asyncio
async def test_manage_label_names_a_missing_label_and_a_value_clash():
    executor, _ = _build(live_writes=True)
    store = [
        {"id": "label-1", "value": "a", "color": None},
        {"id": "label-2", "value": "b", "color": None},
    ]
    sent, results = await _drive(
        executor,
        [
            ("manage_opencti_label", {"action": "delete", "label": "nope"}),
            ("manage_opencti_label", {"action": "update", "label": "a", "value": "B"}),
        ],
        _labels(store),
    )
    assert results[0].startswith("Error: OpenCTI holds no label 'nope'"), results[0]
    assert "already holds the value 'B'" in results[1], results[1]
    assert not any("PatchLabel" in q or "DeleteLabel" in q for _d, q, _v in sent)
    assert [label["value"] for label in store] == ["a", "b"]


def _label_not_core(store: list[dict]):
    base = _labels_by_id(store)

    def answer(headers, query, variables):
        if "DeleteEntity" in query:
            message = "Cannot delete the object, Stix-Core-Object cannot be found."
            return {"errors": [{"message": message, "extensions": {"code": "FUNCTIONAL_ERROR"}}]}, 200
        return base(headers, query, variables)

    return answer


@pytest.mark.asyncio
@pytest.mark.parametrize("live_writes", [True, False])
async def test_delete_entity_on_a_label_names_the_label_tool(live_writes):
    executor = CallableToolExecutor()
    # Without Live writes the delete runs in the active draft.
    build_tools(
        "https://octi.test||static-token",
        executor,
        scope_headers=None if live_writes else {"opencti-draft-id": "draft-1"},
        live_writes=live_writes,
    )
    store = [{"id": _LABEL_UUID, "value": "test-y", "color": None}]
    _sent, results = await _drive(
        executor, [("delete_opencti_entity", {"entity_id": _LABEL_UUID})], _label_not_core(store)
    )
    text = results[-1]
    assert "is the label 'test-y'" in text, text
    assert "Stix-Core-Object cannot be found" not in text
    if live_writes:
        assert "manage_opencti_label" in text
    else:
        assert "manage_opencti_label" not in text and "live" not in text.lower()
        assert "Settings > Taxonomies > Labels" in text


@pytest.mark.asyncio
async def test_delete_entity_keeps_the_error_for_an_unknown_id():
    executor, _ = _build(live_writes=True)
    _sent, results = await _drive(
        executor, [("delete_opencti_entity", {"entity_id": _LABEL_UUID})], _label_not_core([])
    )
    assert "cannot be found" in results[0], results[0]
    assert "manage_opencti_label" not in results[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["list_opencti_entities", "search_opencti"])
async def test_a_label_type_returns_labels_never_other_objects(tool):
    executor, _ = _build()
    _sent, results = await _drive(
        executor,
        [(tool, {"types": ["Label"], "search": "region"})],
        _labels([{"id": "label-1", "value": "region:emea", "color": None}]),
    )
    assert "region:emea" in results[0]
    assert "CVE-2024-0001" not in results[0]
    assert "Vulnerability" not in results[0]


@pytest.mark.asyncio
async def test_a_label_type_mixed_with_others_is_refused():
    executor, _ = _build()
    sent, results = await _drive(
        executor,
        [("list_opencti_entities", {"types": ["Label", "Malware"]})],
        _labels([]),
    )
    assert results[0].startswith("Error: `Label` cannot be listed with other types")
    assert sent == []


# ---------------------------------------------------------------------------
# Draft lifecycle
# ---------------------------------------------------------------------------


def _drafts():
    store = {"d-old": {"id": "d-old", "name": "Old one", "draft_status": "open"}}

    def answer(headers, query, variables):
        if "draftWorkspaceAdd" in query:
            did = f"d-{len(store) + 1}"
            store[did] = {"id": did, "name": variables["input"]["name"], "draft_status": "open"}
            return {"data": {"draftWorkspaceAdd": {"id": did}}}, 200
        if "draftWorkspaceDelete" in query:
            store.pop(variables["id"], None)
            return {"data": {"draftWorkspaceDelete": variables["id"]}}, 200
        if "draftWorkspaceValidate" in query:
            store[variables["id"]]["draft_status"] = "validated"
            return {"data": {"draftWorkspaceValidate": {"id": "work-1"}}}, 200
        if "draftWorkspaces" in query:
            nodes = [d for d in store.values() if d["draft_status"] == "open"]
            return {"data": {"draftWorkspaces": {"edges": [{"node": n} for n in nodes]}}}, 200
        if "draftWorkspace(" in query:
            return {"data": {"draftWorkspace": store.get(variables.get("id"))}}, 200
        return {"data": {}}, 200

    return answer


@pytest.mark.asyncio
async def test_create_then_delete_a_draft_of_this_run():
    executor, names = _build()
    assert "delete_opencti_draft" in names
    assert "apply_opencti_draft" not in names, "validate needs Live writes"
    sent, results = await _drive(
        executor,
        [
            ("create_opencti_draft", {"name": "Config"}),
            ("delete_opencti_draft", {"draft_id": "d-2"}),
            ("list_opencti_drafts", {}),
        ],
        _drafts(),
    )
    assert results[1] == "✅ Draft 'Config' (`d-2`) deleted: it no longer exists in OpenCTI."
    assert "d-2" not in results[2]
    deletes = [d for d, q, _v in sent if "draftWorkspaceDelete" in q]
    assert deletes == [""], "the deletion carries an empty draft header: the live graph"


@pytest.mark.asyncio
async def test_delete_refuses_a_draft_this_run_did_not_create_without_live_writes():
    executor, _ = _build()
    sent, results = await _drive(
        executor, [("delete_opencti_draft", {"draft_id": "d-old"})], _drafts()
    )
    assert results[0].startswith("Error: draft `d-old` was not created in this run")
    assert "live" not in results[0].lower(), "the refusal never names the setting"
    assert sent == []


# ---------------------------------------------------------------------------
# Setting off: the option is gone from the tools
# ---------------------------------------------------------------------------

_LIVE_WRITE_TOOLS = {"apply_opencti_draft", "create_opencti_label", "manage_opencti_label"}


@pytest.mark.parametrize("autonomous", [False, True])
def test_setting_off_registers_no_live_write_tool_and_no_live_wording(autonomous):
    executor = CallableToolExecutor()
    definitions = build_tools(
        "https://octi.test||t", executor, autonomous=autonomous, live_writes=False
    )
    names = {d.name for d in definitions}
    assert not names & _LIVE_WRITE_TOOLS
    assert not set(executor._handlers) & _LIVE_WRITE_TOOLS, "no handler either"
    assert "list_opencti_labels" in names and "delete_opencti_draft" in names
    for d in definitions:
        text = (d.description + str(d.input_schema)).lower()
        assert "live write" not in text, d.name
        assert "live graph" not in text, d.name
        assert "live_writes" not in text, d.name
        assert "live data" not in text, d.name
        assert "live" not in (d.input_schema.get("properties") or {}), d.name


def test_setting_on_registers_the_live_write_tools():
    _executor, names = _build(live_writes=True)
    assert names >= _LIVE_WRITE_TOOLS


def test_no_tool_takes_a_per_call_live_argument():
    for live_writes in (False, True):
        executor = CallableToolExecutor()
        for d in build_tools("https://octi.test||t", executor, live_writes=live_writes):
            assert "live" not in (d.input_schema.get("properties") or {}), d.name


@pytest.mark.asyncio
async def test_setting_off_draft_gate_message_has_no_live_hint():
    executor, _ = _build(live_writes=False)
    _sent, results = await _drive(
        executor, [("delete_opencti_entity", {"entity_id": "x"})], _graph
    )
    assert "draft" in results[0].lower()
    assert "live" not in results[0].lower()


@pytest.mark.asyncio
async def test_live_writes_deletes_any_draft_and_validates_one():
    executor, names = _build(live_writes=True)
    assert "apply_opencti_draft" in names
    _sent, results = await _drive(
        executor,
        [
            ("create_opencti_draft", {"name": "Staged"}),
            ("apply_opencti_draft", {"draft_id": "d-2"}),
            ("apply_opencti_draft", {"draft_id": "d-2"}),
            ("delete_opencti_draft", {"draft_id": "d-old"}),
        ],
        _drafts(),
    )
    assert results[1].startswith("✅ Draft 'Staged' (`d-2`) validated: status `validated`"), results[1]
    assert "work-1" in results[1]
    assert "cannot be validated again" in results[2]
    assert results[3].startswith("✅ Draft 'Old one' (`d-old`) deleted")


def test_validating_a_draft_is_a_write_that_asks_for_approval():
    assert infer_tool_type("apply_opencti_draft") == "write"
    assert infer_tool_type("delete_opencti_draft") == "write"
    assert infer_tool_type("create_opencti_label") == "write"
    assert infer_tool_type("manage_opencti_label") == "write"


@pytest.mark.asyncio
async def test_deleting_the_active_draft_clears_it_from_the_run():
    executor, _ = _build()
    sent, _results = await _drive(
        executor,
        [
            ("create_opencti_draft", {"name": "Tmp"}),
            ("delete_opencti_draft", {"draft_id": "d-2"}),
            ("get_opencti_draft_info", {}),
        ],
        _drafts(),
    )
    assert len(sent) == 4, "get_opencti_draft_info has no active draft left to read"
