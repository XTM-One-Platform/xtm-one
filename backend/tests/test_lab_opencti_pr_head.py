"""The OpenCTI PR-head lab: [service.lab-opencti-pr-head] (xtm-one #11862).

OpenCTI's backend and front built from one nested checkout at a PR head, on the lab's OpenCTI stores, seeded and
signed in. These pin the declaration, the seed plan, the PR seed file and the preview routes.
"""

from __future__ import annotations

import importlib.util
import json

import pytest

from app.services.sandbox_workspace import checks
from tests.test_lab_seed_declarations import CHECKS, LAB, _services

SCRIPT = LAB.parent / "opencti_pr_head.py"
STORE_KEYS = {"opencti-search", "opencti-broker", "opencti-redis", "opencti-s3"}

pytestmark = pytest.mark.skipif(not CHECKS.is_file() or not SCRIPT.is_file(), reason="no .xtm-one next to the backend")


def _module():
    spec = importlib.util.spec_from_file_location("lab_opencti_pr_head", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_service_runs_the_script_on_the_opencti_stores_without_the_tag_backend():
    service = _services()["lab-opencti-pr-head"]
    module = _module()
    assert service["argv"] == ["python", ".xtm-one/lab/opencti_pr_head.py"]
    # The toolchain image: node-gyp compiles OpenCTI's native module with make and a C++ compiler.
    assert service["image"] == "toolchain-node" and service["port"] == module.PORT
    assert service["ready_path"] == module.READY_PATH and service["session_log"] == module.SESSION_PREFIX
    assert "lab-opencti" not in service["depends_on"]
    specs = checks.load_services(CHECKS.read_text(encoding="utf-8"))
    chain = checks.service_chain(specs, "lab-opencti-pr-head", own_images=STORE_KEYS)
    ports = [spec.port for spec in chain]
    assert len(set(ports)) == len(ports)
    # Its own backend and Vite ports collide with no service of the chain, nor with the release-tag OpenCTI.
    assert {module.VITE_PORT, module.BACKEND_PORT}.isdisjoint({*ports, _services()["lab-opencti"]["port"]})


def test_native_modules_build_against_the_pinned_node_headers_offline(tmp_path):
    module = _module()
    node = tmp_path / "node" / "package" / "bin" / "node"
    env = module.toolchain_env(node, {"PATH": "/usr/bin", "HOME": "/root"})
    # node-gyp reads the headers the pinned package ships instead of downloading them from nodejs.org.
    assert env["npm_config_nodedir"] == str(tmp_path / "node" / "package")
    assert env["PATH"].split(":")[0] == str(node.parent)
    assert env["HOME"] == "/root" and env["YARN_ENABLE_GLOBAL_CACHE"] == "false"


def test_the_backend_reaches_the_declared_stores_under_its_own_prefixes():
    module = _module()
    env = module.backend_env("k" * 44)
    tag = _services()["lab-opencti"]["env"]
    for key in (
        "ELASTICSEARCH__URL",
        "REDIS__PORT",
        "MINIO__PORT",
        "MINIO__ACCESS_KEY",
        "MINIO__SECRET_KEY",
        "RABBITMQ__PORT",
    ):
        assert env[key] == tag[key], key
    assert env["APP__PORT"] == str(module.BACKEND_PORT) and env["APP__ADMIN__TOKEN"] == module.ADMIN_TOKEN
    assert env["APP__ADMIN__TOKEN"] != tag["APP__ADMIN__TOKEN"]
    for key in ("ELASTICSEARCH__INDEX_PREFIX", "REDIS__NAMESPACE", "RABBITMQ__QUEUE_PREFIX"):
        assert env[key] == module.PREFIX
    assert module.HEALTH_URL.endswith(env["APP__HEALTH_ACCESS_KEY"])


def test_the_seed_holds_a_filled_and_an_empty_entity_of_every_main_type():
    module = _module()
    plan = module.seed_plan("label-1")
    labels = [entry["label"] for entry in plan]
    for kind in ("Organization", "Individual", "Sector", "System", "Report", "Indicator", "Malware"):
        assert f"{kind} filled" in labels and f"{kind} empty" in labels
    for entry in plan:
        data = entry["variables"]["input"]
        assert data["name"] and "{id}" in entry["path"] and entry["query"].startswith("mutation")
        if entry["label"].endswith("filled"):
            assert data["objectLabel"] == ["label-1"] and data.get("description")
        else:
            assert "description" not in data and "objectLabel" not in data
    # The report, filled with objects at run time, comes after the entities it contains.
    kinds = [entry["kind"] for entry in plan]
    assert kinds.index("report-filled") > max(kinds.index("organization-filled"), kinds.index("malware-filled"))


def test_the_run_links_the_report_to_the_seeded_entities_and_logs_each_page(monkeypatch):
    module = _module()
    sent: list[dict] = []
    logged: list[str] = []

    def fake(query, variables=None):
        sent.append(variables)
        if "System" in variables["input"]["name"] and "empty" in variables["input"]["name"]:
            return {"errors": [{"message": "refused"}]}
        return {"data": {"x": {"id": f"id-{len(sent)}"}}}

    monkeypatch.setattr(module, "graphql", fake)
    monkeypatch.setattr(module.lab, "log", logged.append)
    pages = module.run_seed(module.seed_plan(None), "http://localhost:3500")
    report = next(v for v in sent if v["input"]["name"] == "Lab Report (filled)")
    assert len(report["input"]["objects"]) == 2
    assert len(pages) == len(module.TYPES) * 2 - 1
    assert any("System empty failed" in line for line in logged)
    assert all(url.startswith("http://localhost:3500/dashboard/") for _label, url in pages)
    assert sum(line.startswith(module.SESSION_PREFIX) for line in logged) == len(pages)


def test_a_pr_adds_its_own_seed_and_a_malformed_file_stops_the_start(tmp_path):
    module = _module()
    assert module.extra_seed(tmp_path, {}) == []
    entry = {
        "label": "Case",
        "query": 'mutation { caseIncidentAdd(input: {name: "x"}) { id } }',
        "path": "/dashboard/cases/incidents/{id}",
    }
    (tmp_path / module.SEED_FILE).write_text(json.dumps([entry]), encoding="utf-8")
    [extra] = module.extra_seed(tmp_path, {})
    assert extra["label"] == "Case" and extra["variables"] == {} and extra["kind"] == "extra"
    (tmp_path / "other.json").write_text('[{"label": 1}]', encoding="utf-8")
    with pytest.raises(SystemExit):
        module.extra_seed(tmp_path, {module.SEED_ENV: "other.json"})
    (tmp_path / "bad.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(SystemExit):
        module.extra_seed(tmp_path, {module.SEED_ENV: "bad.json"})


def test_a_pr_seed_script_runs_with_the_pinned_node_against_the_pr_head_backend(tmp_path, monkeypatch):
    module = _module()
    (tmp_path / "demo").mkdir()
    (tmp_path / "demo" / "seed.mjs").write_text("process.exit(0)", encoding="utf-8")
    (tmp_path / "outside.mjs").write_text("", encoding="utf-8")
    entries = [{"label": "Demo", "script": "demo/seed.mjs", "path": "/dashboard/analyses/reports"}]
    (tmp_path / module.SEED_FILE).write_text(json.dumps(entries), encoding="utf-8")
    [extra] = module.extra_seed(tmp_path, {})
    assert extra["script"] == str((tmp_path / "demo" / "seed.mjs").resolve())
    # A script outside the checkout, missing, or not a Node file stops the start.
    demo = tmp_path / "demo"
    for bad in ("../outside.mjs", "missing.mjs", "seed.py"):
        (demo / "bad.json").write_text(json.dumps([{"label": "x", "script": bad}]), encoding="utf-8")
        with pytest.raises(SystemExit):
            module.extra_seed(demo, {module.SEED_ENV: "bad.json"})

    calls: list[dict] = []
    logged: list[str] = []

    class _Process:
        def __init__(self, code):
            self.code = code

        def wait(self, timeout=None):
            return self.code

    def fake_spawn(name, argv, *, cwd=None, env=None, daemon=True):
        calls.append({"argv": argv, "cwd": cwd, "env": env, "daemon": daemon})
        return _Process(len(calls) - 1)

    monkeypatch.setattr(module.lab, "spawn", fake_spawn)
    monkeypatch.setattr(module.lab, "log", logged.append)
    monkeypatch.setattr(module, "graphql", lambda *a, **k: pytest.fail("a script entry sends no GraphQL"))
    pages = module.run_seed([extra, {**extra, "label": "Broken"}], "http://localhost:3500")
    assert pages == [("Demo", "http://localhost:3500/dashboard/analyses/reports")]
    first = calls[0]
    assert first["argv"] == [str(module.node_binary()), extra["script"]] and first["daemon"] is False
    assert first["env"]["OPENCTI_URL"] == module.BACKEND_URL and first["env"]["OPENCTI_TOKEN"] == module.ADMIN_TOKEN
    assert first["env"]["LAB_PREVIEW_URL"] == "http://localhost:3500"
    assert any("seed script Broken failed: exit 1" in line for line in logged)


def test_the_playbook_fixture_is_the_twelve_step_fifteen_link_report_pipeline():
    module = _module()
    playbook = module.PLAYBOOK
    assert playbook["name"] == "Report qualification pipeline"
    assert module.playbook_counts(playbook) == (12, 15)
    assert module.valid_playbook(playbook)
    nodes = playbook["nodes"]
    assert nodes[0]["parent"] is None and all(n["parent"] for n in nodes[1:])
    assert len({n["key"] for n in nodes}) == len(nodes)
    # The built-in entry comes after the entities a configuration may reference.
    entry = module.playbook_entry()
    assert entry["playbook"] is playbook and entry["path"].endswith("/automation/{id}")


def test_every_fixture_step_uses_a_component_the_lab_exposes():
    module = _module()
    # A step whose component the backend hides (the AI agent ones without XTM One) blanks the editor's canvas.
    assert module.missing_components(module.PLAYBOOK, module.LAB_COMPONENTS) == []
    assert not {"PLAYBOOK_AI_AGENT_TRANSFORM_COMPONENT", "PLAYBOOK_AI_AGENT_SEND_COMPONENT"} & module.LAB_COMPONENTS
    # The Sharing step is configured with the seeded organization, so its form opens filled.
    [share] = [n for n in module.PLAYBOOK["nodes"] if n["component_id"] == module.SHARING_COMPONENT]
    assert module.node_configuration(share, {"organization-filled": "org-1"}) == {
        "organizations": ["org-1"],
        "applyToElements": "only-main",
    }


def test_every_fixture_step_carries_the_configuration_its_component_requires():
    module = _module()
    # playbookAddNode refuses a node missing a required key: the seed stopped at Qualify & translate (2 of 11).
    assert module.missing_configuration(module.PLAYBOOK) == []
    [qualify] = [n for n in module.PLAYBOOK["nodes"] if n["key"] == "qualify"]
    action = qualify["configuration"]["actions"][0]
    assert qualify["configuration"]["applyToElements"] == "only-main"
    assert set(action["value"][0]) == {"label", "value", "patch_value"}
    [status] = [n for n in module.PLAYBOOK["nodes"] if n["key"] == "status"]
    assert json.loads(status["configuration"]["filters"])["mode"] == "and"


def test_a_fixture_missing_a_required_configuration_is_not_seeded(monkeypatch):
    module = _module()
    calls, fake = _playbook_backend(components=set(module.LAB_COMPONENTS))
    monkeypatch.setattr(module, "graphql", fake)
    logged: list[str] = []
    monkeypatch.setattr(module.lab, "log", logged.append)
    playbook = {
        "name": "Bare",
        "nodes": [
            {"key": "a", "component_id": "PLAYBOOK_INTERNAL_MANUAL_TRIGGER", "name": "Start", "parent": None},
            {"key": "b", "component_id": "PLAYBOOK_UPDATE_KNOWLEDGE_COMPONENT", "name": "Edit", "parent": ["a", "out"]},
        ],
    }
    assert module.seed_playbook(playbook, "http://localhost:3500", {}) is None
    assert any("required configuration missing: Edit: actions, applyToElements" in line for line in logged)
    assert not any("playbookAdd" in query for query, _ in calls)


def test_a_fixture_with_a_hidden_component_is_not_seeded(monkeypatch):
    module = _module()
    calls, fake = _playbook_backend(components={"PLAYBOOK_INTERNAL_DATA_STREAM"})
    logged: list[str] = []
    monkeypatch.setattr(module, "graphql", fake)
    monkeypatch.setattr(module.lab, "log", logged.append)
    assert module.seed_playbook(module.PLAYBOOK, "http://localhost:3500", {}) is None
    assert not any("playbookAdd" in q for q, _v in calls)
    assert any("components not available" in line and "PLAYBOOK_CONNECTOR_COMPONENT" in line for line in logged)


ENRICHMENT = {"id": "conn-1", "name": "Enricher", "connector_type": "INTERNAL_ENRICHMENT", "playbook_compatible": True}


def _playbook_backend(
    existing_nodes: int = 0, playbook_exists: bool = False, components=None, connectors=None, register_error: str = ""
):
    calls: list[tuple[str, dict]] = []
    listed = _module().LAB_COMPONENTS if components is None else components
    registered = [ENRICHMENT] if connectors is None else connectors

    def fake(query, variables=None):
        calls.append((query, variables or {}))
        if "LabConnectors" in query:
            return {"data": {"connectors": registered}}
        if "registerConnector" in query:
            if register_error:
                return {"errors": [{"message": register_error}]}
            return {"data": {"registerConnector": {"id": variables["input"]["id"]}}}
        if "LabPlaybookComponents" in query:
            return {"data": {"playbookComponents": [{"id": c} for c in sorted(listed)]}}
        if "LabPlaybooks" in query:
            edges = []
            if playbook_exists:
                definition = {"nodes": [{"id": f"n{i}"} for i in range(existing_nodes)], "links": []}
                edges = [{"node": {"id": "pb-1", "name": "Report qualification pipeline",
                                   "playbook_definition": json.dumps(definition)}}]
            return {"data": {"playbooks": {"edges": edges}}}
        if "playbookDelete" in query:
            return {"data": {"playbookDelete": variables["id"]}}
        if "playbookAdd(" in query:
            return {"data": {"playbookAdd": {"id": "pb-1"}}}
        if "playbookAddNode" in query:
            return {"data": {"playbookAddNode": f"node-{len(calls)}"}}
        if "playbookAddLink" in query:
            return {"data": {"playbookAddLink": f"link-{len(calls)}"}}
        return {"errors": [{"message": "unexpected"}]}

    return calls, fake


def test_a_fresh_lab_builds_the_pipeline_and_logs_its_page_with_the_counts(monkeypatch):
    module = _module()
    calls, fake = _playbook_backend()
    logged: list[str] = []
    monkeypatch.setattr(module, "graphql", fake)
    monkeypatch.setattr(module.lab, "log", logged.append)
    page = module.seed_playbook(module.PLAYBOOK, "http://localhost:3500", {})
    assert page == ("Playbook Report qualification pipeline", "http://localhost:3500/dashboard/data/processing/automation/pb-1")
    nodes = [v["input"] for q, v in calls if "playbookAddNode" in q]
    links = [v["input"] for q, v in calls if "playbookAddLink" in q]
    assert len(nodes) == 12 and len(links) == 4
    # Every step but the entry point is created with its parent link in the same write: 11 + 4 = 15 links.
    assert sum(1 for n in nodes if n.get("parentNodeId")) == 11 and "parentNodeId" not in nodes[0]
    assert all(n["parentNodeId"].startswith("node-") for n in nodes[1:])
    assert json.loads(nodes[0]["configuration"]) == {"create": True, "update": False, "delete": False}
    # The Enrich-through-connector step names the backend's playbook-compatible enrichment connector.
    assert json.loads(nodes[1]["configuration"]) == {"connector": "conn-1"}
    assert not any("registerConnector" in q for q, _v in calls)
    [line] = [entry for entry in logged if entry.startswith(module.SESSION_PREFIX)]
    assert line.startswith("seeded opencti page Playbook Report qualification pipeline (12 nodes, 15 links, built)")
    assert not any("partial" in entry for entry in logged)


def test_a_backend_without_an_enrichment_connector_gets_the_lab_one_registered(monkeypatch):
    module = _module()
    other = {"id": "imp-1", "name": "Importer", "connector_type": "EXTERNAL_IMPORT", "playbook_compatible": True}
    off = {**ENRICHMENT, "id": "conn-off", "playbook_compatible": False}
    calls, fake = _playbook_backend(connectors=[other, off])
    monkeypatch.setattr(module, "graphql", fake)
    monkeypatch.setattr(module.lab, "log", lambda _line: None)
    assert module.seed_playbook(module.PLAYBOOK, "http://localhost:3500", {})
    [registered] = [v["input"] for q, v in calls if "registerConnector" in q]
    assert registered["type"] == "INTERNAL_ENRICHMENT" and registered["playbook_compatible"] is True
    nodes = [v["input"] for q, v in calls if "playbookAddNode" in q]
    assert json.loads(nodes[1]["configuration"]) == {"connector": module.LAB_CONNECTOR["id"]}


def test_no_enrichment_connector_is_named_before_any_playbook_write(monkeypatch):
    module = _module()
    calls, fake = _playbook_backend(connectors=[], register_error="Access denied")
    logged: list[str] = []
    monkeypatch.setattr(module, "graphql", fake)
    monkeypatch.setattr(module.lab, "log", logged.append)
    assert module.seed_playbook(module.PLAYBOOK, "http://localhost:3500", {}) is None
    assert not any("playbookAdd" in q for q, _v in calls)
    assert any("no enrichment connector for the PLAYBOOK_CONNECTOR_COMPONENT step" in line for line in logged)
    assert any("Lab enrichment (seed)" in line and "Access denied" in line for line in logged)
    assert not any("partial" in line for line in logged)


def test_an_older_head_without_playbook_compatible_still_resolves_its_connector(monkeypatch):
    module = _module()
    calls: list[str] = []

    def fake(query, variables=None):
        calls.append(query)
        if "playbook_compatible" in query:
            return {"errors": [{"message": 'Cannot query field "playbook_compatible" on type "Connector".'}]}
        return {"data": {"connectors": [{"id": "old-1", "name": "Old", "connector_type": "INTERNAL_ENRICHMENT"}]}}

    monkeypatch.setattr(module, "graphql", fake)
    assert module.enrichment_connector() == "old-1"
    assert len(calls) == 2


def test_the_seed_is_idempotent_across_restarts(monkeypatch):
    module = _module()
    calls, fake = _playbook_backend(existing_nodes=12, playbook_exists=True)
    logged: list[str] = []
    monkeypatch.setattr(module, "graphql", fake)
    monkeypatch.setattr(module.lab, "log", logged.append)
    page = module.seed_playbook(module.PLAYBOOK, "http://localhost:3500", {})
    assert page and page[1].endswith("/automation/pb-1")
    assert not any("playbookAdd" in q for q, _v in calls)
    assert any("(12 nodes, 15 links, kept)" in line for line in logged)
    # An empty playbook left by an earlier start is built in place, never created twice.
    calls, fake = _playbook_backend(existing_nodes=0, playbook_exists=True)
    monkeypatch.setattr(module, "graphql", fake)
    module.seed_playbook(module.PLAYBOOK, "http://localhost:3500", {})
    assert not any("playbookAdd(" in q for q, _v in calls)
    assert sum("playbookAddNode" in q for q, _v in calls) == 12
    # The 11-step fixture of an earlier lab start is deleted and the current one built in its place.
    calls, fake = _playbook_backend(existing_nodes=11, playbook_exists=True)
    monkeypatch.setattr(module, "graphql", fake)
    logged.clear()
    module.seed_playbook(module.PLAYBOOK, "http://localhost:3500", {})
    assert any("playbookDelete" in q for q, _v in calls)
    assert sum("playbookAddNode" in q for q, _v in calls) == 12
    assert any("(12 nodes, 15 links, built)" in line for line in logged)


def test_a_release_without_node_parents_gets_the_node_then_its_link(monkeypatch):
    module = _module()
    calls: list[dict] = []

    def fake(query, variables=None):
        calls.append(variables)
        if "playbookAddNode" in query and "parentNodeId" in variables["input"]:
            return {"errors": [{"message": 'Field "parentNodeId" is not defined by type "PlaybookAddNodeInput".'}]}
        if "playbookAddNode" in query:
            return {"data": {"playbookAddNode": "node-2"}}
        return {"data": {"playbookAddLink": "link-1"}}

    monkeypatch.setattr(module, "graphql", fake)
    node = {"key": "b", "component_id": "PLAYBOOK_LOGGER_COMPONENT", "name": "Log", "parent": ["a", "out"]}
    assert module.add_node("pb-1", node, {"a": "node-1"}, {}) == "node-2"
    assert calls[-1]["input"] == {"from_node": "node-1", "from_port": "out", "to_node": "node-2"}


def test_a_sharing_step_always_carries_organizations():
    module = _module()
    sharing = {"key": "s", "component_id": module.SHARING_COMPONENT, "name": "Share", "parent": ["a", "out"]}
    # Created without organizations, a Sharing step crashes playbook_definition: the seeded organization is set.
    configuration = module.node_configuration(sharing, {"organization-filled": "org-1"})
    assert configuration == {"organizations": ["org-1"], "applyToElements": "only-main"}
    explicit = {**sharing, "configuration": {"organizations": ["{seeded:organization-empty}"], "applyToElements": "all-elements"}}
    assert module.node_configuration(explicit, {"organization-empty": "org-2"}) == {
        "organizations": ["org-2"],
        "applyToElements": "all-elements",
    }
    # Without a seeded organization the list is still present (empty), never missing.
    assert module.node_configuration(sharing, {}) == {"organizations": [], "applyToElements": "only-main"}


def test_a_pr_seed_file_may_carry_a_playbook(tmp_path):
    module = _module()
    playbook = {
        "name": "PR playbook",
        "nodes": [
            {"key": "a", "component_id": "PLAYBOOK_INTERNAL_MANUAL_TRIGGER", "name": "Start", "parent": None},
            {"key": "b", "component_id": "PLAYBOOK_LOGGER_COMPONENT", "name": "Log", "parent": ["a", "out"]},
        ],
        "links": [],
    }
    (tmp_path / module.SEED_FILE).write_text(json.dumps([{"label": "Mine", "playbook": playbook}]), encoding="utf-8")
    [entry] = module.extra_seed(tmp_path, {})
    assert entry["playbook"] == playbook and entry["query"] == ""
    broken = {**playbook, "links": [["a", "out", "missing"]]}
    (tmp_path / "bad.json").write_text(json.dumps([{"label": "Mine", "playbook": broken}]), encoding="utf-8")
    with pytest.raises(SystemExit):
        module.extra_seed(tmp_path, {module.SEED_ENV: "bad.json"})


def test_the_theme_route_keeps_next_on_the_preview_origin():
    module = _module()
    assert module.lab_route("/lab/theme/dark") == ("dark", "/dashboard")
    assert module.lab_route("/lab/theme/light?next=/dashboard/analyses/reports/x") == (
        "light",
        "/dashboard/analyses/reports/x",
    )
    assert module.lab_route("/lab/theme/dark?next=https://example.com/") == ("dark", "/dashboard")
    assert module.lab_route("/lab/theme/dark?next=//example.com") == ("dark", "/dashboard")
    assert module.lab_route("/lab/theme/blue") is None and module.lab_route("/dashboard") is None


def test_the_theme_is_written_as_the_built_in_theme_id_when_the_platform_has_themes():
    module = _module()
    answer = {
        "data": {
            "themes": {
                "edges": [
                    {"node": {"id": "t-dark", "name": "Filigran Dark"}},
                    {"node": {"id": "t-legacy-light", "name": "Light"}},
                    {"node": {"id": "t-light", "name": "Filigran Light"}},
                ]
            }
        }
    }
    assert module.theme_value("light", answer) == "t-light"
    assert module.theme_value("dark", answer) == "t-dark"
    legacy = {"data": {"themes": {"edges": [{"node": {"id": "t-old", "name": "Light"}}]}}}
    assert module.theme_value("light", legacy) == "t-old"
    # An older release without the theme module: the literal stays.
    assert module.theme_value("light", {"errors": [{"message": "Cannot query field themes"}]}) == "light"


def test_every_forwarded_request_carries_the_lab_admin_token_only():
    module = _module()
    headers = module.forwarded_headers(
        [("Host", "preview"), ("Authorization", "Bearer someone-else"), ("Accept", "*/*")]
    )
    assert headers["Authorization"] == f"Bearer {module.ADMIN_TOKEN}"
    assert headers["Host"] == f"127.0.0.1:{module.VITE_PORT}" and headers["Accept"] == "*/*"


def test_the_checkout_check_names_the_sub_paths_to_materialize(tmp_path, monkeypatch):
    module = _module()
    monkeypatch.setattr(module.lab, "CHECKOUT", tmp_path)
    with pytest.raises(SystemExit) as stopped:
        module.opencti_checkout()
    assert "opencti-platform/opencti-graphql" in str(stopped.value)
    for directory in (module.GRAPHQL_DIR, module.FRONT_DIR):
        (tmp_path / "opencti" / directory).mkdir(parents=True)
        (tmp_path / "opencti" / directory / "package.json").write_text("{}", encoding="utf-8")
    (tmp_path / "opencti" / "yarn.lock").write_text("", encoding="utf-8")
    assert module.opencti_checkout() == tmp_path / "opencti"
