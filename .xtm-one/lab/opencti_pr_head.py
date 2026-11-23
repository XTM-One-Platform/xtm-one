"""The OpenCTI PR-head lab: OpenCTI's backend AND front built from a nested checkout, seeded, signed in (#11862).

``[service.lab-opencti-pr-head]`` in ``.xtm-one/checks.toml`` runs this script (standard library only) on the
``python-dev`` image. The release-tag lab (``lab-opencti`` + ``lab-opencti-ask-ariane``) renders a master PR head
blank: its front queries fields the tag's backend lacks. Here both halves come from the same checkout:

1. The OpenCTI source is a nested checkout under this checkout, ``opencti/`` by default (``LAB_OPENCTI_CHECKOUT``):
   materialize_repository(OpenCTI-Platform/opencti, ref=<the PR head branch or SHA>, sub_paths=SUB_PATHS,
   path="<this checkout>/opencti"). Re-materialize it at another ref and restart the service to compare.
2. Node and yarn are the lab's pinned downloads (``lab.NODE``, ``lab.YARN``); ``yarn workspaces focus`` installs the
   GraphQL and front workspaces; the backend's Python requirements (STIX pattern checks) are pip-installed.
3. The backend: the connectors manifest (fetched, else an empty catalog is written), ``graphql-codegen``, the esbuild
   development build, then ``build/back.mjs`` on ``BACKEND_PORT`` against the lab's OpenSearch, RabbitMQ, Redis and S3
   services (``lab-opencti-search``, ``-broker``, ``-redis``, ``-s3``), with its own index, queue, namespace and bucket
   prefix so the release-tag lab can share them. Migrations run at its boot.
4. The front: ``relay-compiler`` on the checkout's schema, then Vite on ``VITE_PORT`` proxied to that backend.
5. This script listens on ``PORT``, the preview port: every request goes to Vite with the lab admin's bearer token,
   so every page renders signed in (no sign-in form). ``/lab/theme/dark`` and ``/lab/theme/light`` set the admin's
   theme and redirect to the page named by ``?next=`` (default the dashboard).
6. Seed (``SEED``): one label, one vocabulary entry, then one FILLED and one EMPTY entity per main type
   (Organization, Individual, Sector, System, Report, Indicator, Malware). Each page is logged
   ``seeded opencti page <label>: <url>`` (the service's ``session_log``: the start answer lists them).
   A PR adds its own seed in ``opencti-lab-seed.json`` at this checkout's root (``LAB_OPENCTI_SEED`` names
   another file): ``[{"label": ..., "query": "mutation ...", "variables": {...}, "path": "/dashboard/.../{id}"}]``,
   ``{id}`` being the ``id`` of the mutation's first field.
7. Playbook (``PLAYBOOK``): the 11-step, 14-link "Report qualification pipeline" built node by node, so a
   playbook editor round opens a built canvas with no script. A seed file entry may carry a playbook instead of a
   query: ``{"label": ..., "playbook": {"name", "description", "nodes": [{"key", "component_id", "name",
   "parent": [key, port] | null, "configuration": {...}}], "links": [[from key, port, to key]]}}``. A configuration
   string ``{seeded:<kind>}`` is the id of a seeded entity (``{seeded:organization-filled}``). A playbook already
   holding its nodes is kept: the seed is idempotent across restarts.
8. Seed script: a seed file entry may carry ``{"label", "script": "<path from this checkout's root>.mjs",
   "path"?: "/dashboard/..."}`` instead of a query: the PR's own Node seed (a demo dataset, a .mjs or .js file) runs
   with the lab's pinned node once the backend is healthy, with ``OPENCTI_URL`` (the backend), ``OPENCTI_TOKEN``
   (the lab admin's token) and ``LAB_PREVIEW_URL`` in its environment, so no node is needed in another image and
   no seed is hand-ported to GraphQL. A non-zero exit is logged and skipped; ``path`` names the page it fills.
"""

from __future__ import annotations

import base64
import http.client
import http.server
import io
import json
import os
import secrets
import select
import socket
import subprocess
import sys
import tarfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lab  # noqa: E402 - .xtm-one/lab/lab.py: the pinned toolchain and the process helpers

PORT = 3500
VITE_PORT = 3501
#: Not 4000: the release-tag lab-opencti holds it when both labs run.
BACKEND_PORT = 4100
BACKEND_URL = f"http://127.0.0.1:{BACKEND_PORT}"
HEALTH_KEY = "lab-only-opencti-pr-head-health"
HEALTH_URL = f"{BACKEND_URL}/health?health_access_key={HEALTH_KEY}"
ADMIN_EMAIL = "lab@filigran.io"
ADMIN_PASSWORD = "lab-only-password"
#: The lab admin's API token: the proxy sends it on every request.
ADMIN_TOKEN = "00000000-0000-4000-8000-000000000002"
#: The prefix this lab's stores carry, apart from the release-tag lab's.
PREFIX = "prhead"
SESSION_PREFIX = "seeded opencti page"
SEED_ENV = "LAB_OPENCTI_SEED"
SEED_FILE = "opencti-lab-seed.json"
GRAPHQL_DIR = "opencti-platform/opencti-graphql"
FRONT_DIR = "opencti-platform/opencti-front"
#: What the nested OpenCTI checkout must hold: both workspaces, their siblings' manifests and the lock.
SUB_PATHS = (
    "package.json",
    "yarn.lock",
    ".yarnrc.yml",
    "patch",
    "opencti-platform/opencti-front",
    "opencti-platform/opencti-graphql",
    "opencti-platform/eslint-plugin-custom-rules",
)
THEMES = ("dark", "light")
#: A PR seed script's extensions and its run budget, in seconds.
SCRIPT_SUFFIXES = (".mjs", ".js", ".cjs")
SCRIPT_TIMEOUT = 600
#: Answered by this script once the backend is healthy (the preview server starts after it).
READY_PATH = "/lab/ready"


def backend_env(encryption_key: str) -> dict[str, str]:
    """The environment of the PR-head backend: the lab's stores under this lab's own prefixes."""
    return {
        "NODE_ENV": "development",
        "NODE_OPTIONS": "--max-old-space-size=4096",
        "APP__PORT": str(BACKEND_PORT),
        "APP__BASE_URL": f"http://localhost:{PORT}",
        "APP__HEALTH_ACCESS_KEY": HEALTH_KEY,
        "APP__ADMIN__EMAIL": ADMIN_EMAIL,
        "APP__ADMIN__PASSWORD": ADMIN_PASSWORD,
        "APP__ADMIN__TOKEN": ADMIN_TOKEN,
        "APP__ENCRYPTION_KEY": encryption_key,
        "APP__ENABLED_DEV_FEATURES": '["*"]',
        "APP__BOOTSTRAP_WIZARD__ENABLED": "false",
        "APP__TELEMETRY__FILIGRAN__ENABLED": "false",
        "ELASTICSEARCH__URL": "http://localhost:9200",
        "ELASTICSEARCH__INDEX_PREFIX": PREFIX,
        "RABBITMQ__HOSTNAME": "localhost",
        "RABBITMQ__PORT": "5672",
        "RABBITMQ__USERNAME": "guest",
        "RABBITMQ__PASSWORD": "guest",
        "RABBITMQ__QUEUE_PREFIX": PREFIX,
        "REDIS__HOSTNAME": "localhost",
        "REDIS__PORT": "6380",
        "REDIS__NAMESPACE": PREFIX,
        "MINIO__ENDPOINT": "localhost",
        "MINIO__PORT": "9002",
        "MINIO__USE_SSL": "false",
        "MINIO__ACCESS_KEY": "opencti",
        "MINIO__SECRET_KEY": "lab-only-opencti-s3",
        "MINIO__BUCKET_NAME": f"{PREFIX}-bucket",
    }


# --- Seed -----------------------------------------------------------------------------------------------------------

LABEL = {"value": "lab-seeded", "color": "#0fbcff"}
VOCABULARY = {"name": "lab-vocabulary-entry", "category": "report_types_ov", "description": "Seeded by the PR-head lab"}
FILLED_DESCRIPTION = (
    "Seeded by the OpenCTI PR-head lab. This description is long enough to wrap on a wide screen, so the "
    "overview's text block, its truncation and its expand control render as they would with real data."
)
INDICATOR_PATTERN = "[ipv4-addr:value = '198.51.100.7']"

#: (label, mutation field, input type, route of its page, filled input, empty input). Order matters: the report
#: links the filled organization and malware seeded before it.
TYPES: tuple[tuple[str, str, str, str, dict, dict], ...] = (
    ("Organization", "organizationAdd", "OrganizationAddInput", "/dashboard/entities/organizations/{id}",
     {"description": FILLED_DESCRIPTION, "x_opencti_organization_type": "vendor", "x_opencti_score": 80}, {}),
    ("Individual", "individualAdd", "IndividualAddInput", "/dashboard/entities/individuals/{id}",
     {"description": FILLED_DESCRIPTION}, {}),
    ("Sector", "sectorAdd", "SectorAddInput", "/dashboard/entities/sectors/{id}",
     {"description": FILLED_DESCRIPTION}, {}),
    ("System", "systemAdd", "SystemAddInput", "/dashboard/entities/systems/{id}",
     {"description": FILLED_DESCRIPTION}, {}),
    ("Malware", "malwareAdd", "MalwareAddInput", "/dashboard/arsenal/malwares/{id}",
     {"description": FILLED_DESCRIPTION, "malware_types": ["ransomware"], "is_family": True,
      "aliases": ["LabLocker", "LL-26"]}, {}),
    ("Indicator", "indicatorAdd", "IndicatorAddInput", "/dashboard/observations/indicators/{id}",
     {"description": FILLED_DESCRIPTION, "pattern_type": "stix", "pattern": INDICATOR_PATTERN,
      "x_opencti_main_observable_type": "IPv4-Addr", "x_opencti_score": 75, "indicator_types": ["malicious-activity"],
      "valid_from": "2026-11-01T00:00:00.000Z"},
     {"pattern_type": "stix", "pattern": "[domain-name:value = 'empty.lab.example']",
      "x_opencti_main_observable_type": "Domain-Name"}),
    ("Report", "reportAdd", "ReportAddInput", "/dashboard/analyses/reports/{id}",
     {"description": FILLED_DESCRIPTION, "report_types": ["threat-report"], "published": "2026-11-10T09:00:00.000Z",
      "confidence": 80},
     {"published": "2026-11-10T09:00:00.000Z"}),
)


def mutation(field: str, input_type: str) -> str:
    return f"mutation LabSeed($input: {input_type}!) {{ {field}(input: $input) {{ id }} }}"


def seed_plan(label_id: str | None) -> list[dict]:
    """The seed of every main type, filled then empty: ``{label, query, variables, path, kind}`` each.

    The filled entities carry the seeded label; the filled report contains the filled organization and malware
    (``objects`` is filled in by :func:`run_seed` once their ids are known: ``kind`` names them).
    """
    plan: list[dict] = []
    for label, field, input_type, path, filled, empty in TYPES:
        for state, extra in (("filled", filled), ("empty", empty)):
            data = {"name": f"Lab {label} ({state})", **extra}
            if state == "filled" and label_id:
                data["objectLabel"] = [label_id]
            plan.append({"label": f"{label} {state}", "query": mutation(field, input_type), "variables": {"input": data},
                         "path": path, "kind": f"{label.lower()}-{state}"})
    return plan


def extra_seed(checkout: Path, environ: dict[str, str] | None = None) -> list[dict]:
    """The PR's own seed (``opencti-lab-seed.json`` or ``LAB_OPENCTI_SEED``); a malformed file stops the start."""
    environ = os.environ if environ is None else environ
    path = checkout / environ.get(SEED_ENV, SEED_FILE)
    if not path.is_file():
        return []
    try:
        entries = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise SystemExit(f"opencti-pr-head: {path.name} is not JSON: {exc}") from None
    if not isinstance(entries, list) or not all(
        isinstance(e, dict) and isinstance(e.get("label"), str)
        and (isinstance(e.get("query"), str) or valid_playbook(e.get("playbook"))
             or seed_script(checkout, e.get("script")) is not None)
        for e in entries
    ):
        raise SystemExit(
            f"opencti-pr-head: {path.name} must be a list of {{label, query, variables?, path?}}, {{label, playbook}} "
            "or {label, script, path?} objects (a playbook: name, nodes with key, component_id, name, parent; links as "
            f"[from, port, to]; a script: a {'/'.join(SCRIPT_SUFFIXES)} file inside this checkout)"
        )
    seeds = []
    for e in entries:
        seed = {"label": e["label"], "query": e.get("query") or "", "variables": e.get("variables") or {},
                "path": e.get("path") or "", "kind": "extra"}
        if e.get("playbook"):
            seed["playbook"] = e["playbook"]
        script = seed_script(checkout, e.get("script"))
        if script is not None and not seed["query"] and not e.get("playbook"):
            seed["script"] = str(script)
        seeds.append(seed)
    return seeds


def seed_script(checkout: Path, script: object) -> Path | None:
    """The PR seed script *script* names, a Node file inside *checkout*; None when it is not one."""
    if not isinstance(script, str) or not script.endswith(SCRIPT_SUFFIXES):
        return None
    root = checkout.resolve()
    path = (root / script).resolve()
    return path if path.is_relative_to(root) and path.is_file() else None


def node_binary() -> Path:
    """The lab's pinned node, unpacked by :func:`main` before any seed runs."""
    return lab.CACHE / "node" / "package" / "bin" / "node"


def run_script(entry: dict, base: str) -> tuple[str, str] | None:
    """Run a PR seed script with the pinned node against the PR-head backend; its page, None on a failure (logged)."""
    node = node_binary()
    env = {**toolchain_env(node, dict(os.environ)), "OPENCTI_URL": BACKEND_URL, "OPENCTI_TOKEN": ADMIN_TOKEN,
           "LAB_PREVIEW_URL": base}
    script = Path(entry["script"])
    process = lab.spawn(f"seed-script {entry['label']}", [str(node), str(script)], cwd=script.parent, env=env, daemon=False)
    try:
        code = process.wait(timeout=SCRIPT_TIMEOUT)
    except subprocess.TimeoutExpired:
        process.kill()
        lab.log(f"opencti-pr-head: seed script {entry['label']} failed: still running after {SCRIPT_TIMEOUT} s, stopped")
        return None
    if code != 0:
        lab.log(f"opencti-pr-head: seed script {entry['label']} failed: exit {code}")
        return None
    if not entry.get("path"):
        lab.log(f"opencti-pr-head: seed script {entry['label']} ran")
        return None
    url = f"{base}{entry['path']}"
    lab.log(f"{SESSION_PREFIX} {entry['label']}: {url}")
    return entry["label"], url


def valid_playbook(playbook: object) -> bool:
    """Whether *playbook* is a seedable definition: one entry point first, every parent and link on a known key."""
    if not isinstance(playbook, dict) or not isinstance(playbook.get("name"), str):
        return False
    nodes, links = playbook.get("nodes"), playbook.get("links") or []
    if not isinstance(nodes, list) or not nodes or not isinstance(links, list):
        return False
    seen: set[str] = set()
    for index, node in enumerate(nodes):
        if not isinstance(node, dict) or not all(isinstance(node.get(k), str) for k in ("key", "component_id", "name")):
            return False
        parent = node.get("parent")
        if index == 0 and parent:
            return False
        if index > 0 and not (isinstance(parent, list) and len(parent) == 2 and parent[0] in seen):
            return False
        seen.add(node["key"])
    return all(isinstance(link, list) and len(link) == 3 and link[0] in seen and link[2] in seen for link in links)


def created_id(answer: dict) -> str:
    """The ``id`` of the first field of a mutation's answer, empty on an error."""
    data = answer.get("data") if isinstance(answer, dict) else None
    if not isinstance(data, dict) or answer.get("errors"):
        return ""
    first = next(iter(data.values()), None)
    return str(first.get("id") or "") if isinstance(first, dict) else ""


def page_url(base: str, path: str, entity_id: str) -> str:
    return f"{base}{path.replace('{id}', entity_id)}" if path and entity_id else ""


# --- GraphQL --------------------------------------------------------------------------------------------------------


def graphql(query: str, variables: dict | None = None) -> dict:
    """*query* on the PR-head backend as the lab admin."""
    request = urllib.request.Request(
        f"{BACKEND_URL}/graphql",
        data=json.dumps({"query": query, "variables": variables or {}}).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {ADMIN_TOKEN}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return {"errors": [{"message": f"HTTP {exc.code}: {exc.read()[:300].decode('utf-8', 'replace')}"}]}


def run_seed(plan: list[dict], base: str, ids: dict[str, str] | None = None) -> list[tuple[str, str]]:
    """Run *plan*, log each seeded page and return ``(label, url)``; a failed entry is logged and skipped.

    *ids* receives the id of each seeded ``kind`` (a playbook entry reads them for ``{seeded:<kind>}``).
    """
    ids = {} if ids is None else ids
    pages: list[tuple[str, str]] = []
    for entry in plan:
        if entry.get("playbook"):
            page = seed_playbook(entry["playbook"], base, ids, entry["label"])
            if page:
                pages.append(page)
            continue
        if entry.get("script"):
            page = run_script(entry, base)
            if page:
                pages.append(page)
            continue
        variables = json.loads(json.dumps(entry["variables"]))
        if entry["kind"] == "report-filled":
            objects = [ids[k] for k in ("organization-filled", "malware-filled") if ids.get(k)]
            if objects:
                variables["input"]["objects"] = objects
        answer = graphql(entry["query"], variables)
        entity_id = created_id(answer)
        if not entity_id:
            lab.log(f"opencti-pr-head: seed {entry['label']} failed: {str(answer.get('errors'))[:400]}")
            continue
        ids[entry["kind"]] = entity_id
        url = page_url(base, entry["path"], entity_id)
        if url:
            pages.append((entry["label"], url))
            lab.log(f"{SESSION_PREFIX} {entry['label']}: {url}")
    return pages


# --- Playbook -----------------------------------------------------------------------------------------------------

PLAYBOOK_PATH = "/dashboard/data/processing/automation/{id}"
SEEDED_REF = "{seeded:"
#: An Enrich-through-connector step names its connector: OpenCTI validates ``required: ['connector']`` at
#: ``playbookAddNode`` on current heads, so the seed resolves a playbook-compatible enrichment connector first
#: (:func:`enrichment_connector`) and passes it as ``{seeded:connector-enrichment}``.
CONNECTOR_COMPONENT = "PLAYBOOK_CONNECTOR_COMPONENT"
CONNECTOR_KIND = "connector-enrichment"
#: The enrichment connector the lab registers when the backend has none: a fixed id, so a restart finds it again.
LAB_CONNECTOR: dict = {
    "id": "5e1ab0c0-7a1b-4c0d-8e5e-0000000c0de1",
    "name": "Lab enrichment (seed)",
    "type": "INTERNAL_ENRICHMENT",
    "scope": ["Report"],
    "auto": False,
    "only_contextual": False,
    "playbook_compatible": True,
}
#: A filter group matching every Report (OpenCTI's FilterGroup, sent as the JSON string its schema types).
REPORT_FILTERS = json.dumps({
    "mode": "and",
    "filters": [{"key": ["entity_type"], "values": ["Report"], "operator": "eq", "mode": "or"}],
    "filterGroups": [],
})


def manipulate(op: str, attribute: str, value: str) -> dict:
    """A Manipulate knowledge configuration: one action on *attribute*, applied to the main element only."""
    return {
        "actions": [{"op": op, "attribute": attribute, "value": [{"label": value, "value": value, "patch_value": value}]}],
        "applyToElements": "only-main",
    }


#: The ``required`` keys of each component's ``configuration_schema`` (opencti-graphql playbook-components.ts and
#: manipulate-knowledge-component.ts): ``playbookAddNode`` refuses a node missing one with 'Playbook node
#: configuration misses required fields', which stops the seed and leaves a partial canvas.
REQUIRED_CONFIGURATION: dict[str, tuple[str, ...]] = {
    "PLAYBOOK_INTERNAL_DATA_STREAM": ("create", "update", "delete"),
    "PLAYBOOK_INTERNAL_DATA_CRON": ("period", "triggerTime", "onlyLast", "filters"),
    "PLAYBOOK_LOGGER_COMPONENT": ("level",),
    "PLAYBOOK_FILTERING_COMPONENT": ("filters",),
    "PLAYBOOK_REDUCING_COMPONENT": ("filters",),
    "PLAYBOOK_CONNECTOR_COMPONENT": ("connector",),
    "PLAYBOOK_UPDATE_KNOWLEDGE_COMPONENT": ("actions", "applyToElements"),
    "PLAYBOOK_RULE_COMPONENT": ("rule", "inferences"),
}
#: The report qualification pipeline the playbook editor rounds review: 12 steps, 15 links (11 parent links and
#: 4 ``unmodified`` links joining a branch back). ``parent`` is ``[node key, out port]``; the entry point has none.
#: Every step uses a component the lab's backend exposes (:data:`LAB_COMPONENTS`): an unexposed one leaves the
#: canvas node without its component and the editor's error boundary blanks the page. Every step carries the
#: configuration its component requires (:data:`REQUIRED_CONFIGURATION`).
PLAYBOOK: dict = {
    "name": "Report qualification pipeline",
    "description": "Seeded by the OpenCTI PR-head lab: the playbook editor's reference fixture.",
    "nodes": [
        {"key": "listen", "component_id": "PLAYBOOK_INTERNAL_DATA_STREAM", "name": "Listen new reports",
         "parent": None, "configuration": {"create": True, "update": False, "delete": False}},
        {"key": "scrape", "component_id": CONNECTOR_COMPONENT, "name": "Scrape full article",
         "parent": ["listen", "out"], "configuration": {"connector": "{seeded:connector-enrichment}"}},
        {"key": "qualify", "component_id": "PLAYBOOK_UPDATE_KNOWLEDGE_COMPONENT", "name": "Qualify & translate",
         "parent": ["scrape", "out"], "configuration": manipulate("replace", "confidence", "75")},
        {"key": "status", "component_id": "PLAYBOOK_FILTERING_COMPONENT", "name": "Status is QUALIFIED",
         "parent": ["qualify", "out"], "configuration": {"filters": REPORT_FILTERS}},
        {"key": "logerr", "component_id": "PLAYBOOK_LOGGER_COMPONENT", "name": "Qualifier failed: log error",
         "parent": ["qualify", "unmodified"], "configuration": {"level": "error"}},
        {"key": "extract", "component_id": "PLAYBOOK_UPDATE_KNOWLEDGE_COMPONENT",
         "name": "Extract entities & relationships", "parent": ["status", "out"],
         "configuration": manipulate("replace", "x_opencti_score", "50")},
        {"key": "rejected", "component_id": "PLAYBOOK_INGESTION_COMPONENT", "name": "Ingest rejected (status + note)",
         "parent": ["status", "no-match"]},
        {"key": "processed", "component_id": "PLAYBOOK_UPDATE_KNOWLEDGE_COMPONENT", "name": "Change status to PROCESSED",
         "parent": ["extract", "out"], "configuration": manipulate("replace", "confidence", "90")},
        {"key": "categorize", "component_id": "PLAYBOOK_UPDATE_KNOWLEDGE_COMPONENT", "name": "Categorize feed flavours",
         "parent": ["processed", "out"], "configuration": manipulate("replace", "report_types", "threat-report")},
        {"key": "ready", "component_id": "PLAYBOOK_UPDATE_KNOWLEDGE_COMPONENT", "name": "Change status to READY",
         "parent": ["categorize", "out"], "configuration": manipulate("replace", "confidence", "100")},
        {"key": "share", "component_id": "PLAYBOOK_SHARING_COMPONENT", "name": "Share with the lab organization",
         "parent": ["ready", "out"], "configuration": {"organizations": ["{seeded:organization-filled}"]}},
        {"key": "ingest", "component_id": "PLAYBOOK_INGESTION_COMPONENT", "name": "Ingest published report",
         "parent": ["share", "out"]},
    ],
    "links": [
        ["extract", "unmodified", "processed"],
        ["processed", "unmodified", "categorize"],
        ["categorize", "unmodified", "ready"],
        ["ready", "unmodified", "ingest"],
    ],
}
#: A Sharing step needs its organizations configured: one created without them crashes ``playbook_definition``
#: (playbook-domain.ts) and blanks the canvas. Its organization is the seeded filled organization.
SHARING_COMPONENT = "PLAYBOOK_SHARING_COMPONENT"
#: The components OpenCTI's ``availableComponents`` (playbook-domain.ts) lists on the lab's backend, which configures
#: no XTM One: ``PLAYBOOK_AI_AGENT_TRANSFORM_COMPONENT`` and ``PLAYBOOK_AI_AGENT_SEND_COMPONENT`` are hidden there.
LAB_COMPONENTS = frozenset({
    "PLAYBOOK_INTERNAL_MANUAL_TRIGGER", "PLAYBOOK_INTERNAL_DATA_STREAM", "PLAYBOOK_DATA_STREAM_PIR",
    "PLAYBOOK_INTERNAL_DATA_CRON", "PLAYBOOK_LOGGER_COMPONENT", "PLAYBOOK_INGESTION_COMPONENT",
    "PLAYBOOK_FILTERING_COMPONENT", "PLAYBOOK_CONNECTOR_COMPONENT", "PLAYBOOK_UPDATE_KNOWLEDGE_COMPONENT",
    "PLAYBOOK_CONTAINER_WRAPPER_COMPONENT", "PLAYBOOK_SECURITY_COVERAGE_COMPONENT", SHARING_COMPONENT,
    "PLAYBOOK_UNSHARING_COMPONENT", "PLAYBOOK_ACCESS_RESTRICTIONS_COMPONENT", "PLAYBOOK_REMOVE_ACCESS_RESTRICTIONS_COMPONENT",
    "PLAYBOOK_RULE_COMPONENT", "PLAYBOOK_NOTIFIER_COMPONENT", "PLAYBOOK_CREATE_INDICATOR_COMPONENT",
    "PLAYBOOK_REDUCING_COMPONENT", "PLAYBOOK_CREATE_OBSERVABLE_COMPONENT", "PLAYBOOK_SEND_EMAIL_TEMPLATE_COMPONENT",
})


def missing_components(playbook: dict, available: set[str] | frozenset[str]) -> list[str]:
    """The component ids *playbook* uses that *available* does not list, in first-use order."""
    missing: list[str] = []
    for node in playbook.get("nodes") or []:
        component_id = node.get("component_id")
        if component_id not in available and component_id not in missing:
            missing.append(component_id)
    return missing


def available_components() -> set[str] | None:
    """The component ids the backend lists (``playbookComponents``), ``None`` when it cannot be read."""
    answer = graphql("query LabPlaybookComponents { playbookComponents { id } }")
    rows = (answer.get("data") or {}).get("playbookComponents") if isinstance(answer, dict) else None
    if not isinstance(rows, list) or answer.get("errors"):
        return None
    return {str(row.get("id")) for row in rows if isinstance(row, dict) and row.get("id")}


def missing_configuration(playbook: dict) -> list[str]:
    """``<step name>: <key>, ...`` for every step missing a key its component requires, in node order."""
    missing: list[str] = []
    for node in playbook.get("nodes") or []:
        configuration = node_configuration(node, {}) or {}
        keys = [k for k in REQUIRED_CONFIGURATION.get(node.get("component_id"), ()) if k not in configuration]
        if keys:
            missing.append(f"{node.get('name')}: {', '.join(keys)}")
    return missing


def playbook_counts(playbook: dict) -> tuple[int, int]:
    """``(nodes, links)`` the definition ends with: one link per parented node plus the extra links."""
    nodes = playbook.get("nodes") or []
    return len(nodes), sum(1 for n in nodes if n.get("parent")) + len(playbook.get("links") or [])


def uses_component(playbook: dict, component_id: str) -> bool:
    return any(node.get("component_id") == component_id for node in playbook.get("nodes") or [])


def enrichment_connector() -> str:
    """The id of a playbook-compatible enrichment connector, the lab's own registered when none exists; empty (logged).

    A release whose ``Connector`` or ``RegisterConnectorInput`` has no ``playbook_compatible`` (older heads) is read
    and registered without it.
    """
    answer = graphql("query LabConnectors { connectors { id name connector_type playbook_compatible } }")
    if answer.get("errors") and "playbook_compatible" in str(answer.get("errors")):
        answer = graphql("query LabConnectors { connectors { id name connector_type } }")
    rows = (answer.get("data") or {}).get("connectors") if isinstance(answer, dict) else None
    for row in rows if isinstance(rows, list) else []:
        if (isinstance(row, dict) and row.get("id") and row.get("connector_type") == "INTERNAL_ENRICHMENT"
                and row.get("playbook_compatible", True) is not False):
            return str(row["id"])
    register = "mutation LabConnector($input: RegisterConnectorInput) { registerConnector(input: $input) { id } }"
    answer = graphql(register, {"input": LAB_CONNECTOR})
    if answer.get("errors") and "playbook_compatible" in str(answer.get("errors")):
        answer = graphql(register, {"input": {k: v for k, v in LAB_CONNECTOR.items() if k != "playbook_compatible"}})
    connector_id = created_id(answer)
    if not connector_id:
        lab.log(f"opencti-pr-head: no playbook-compatible enrichment connector, and registering "
                f"{LAB_CONNECTOR['name']} failed: {str(answer.get('errors'))[:300]}")
    return connector_id


def resolve_seeded(value: object, ids: dict[str, str]) -> object:
    """*value* with every ``{seeded:<kind>}`` string replaced by the id seeded for that kind (kept when unknown)."""
    if isinstance(value, str) and value.startswith(SEEDED_REF) and value.endswith("}"):
        return ids.get(value[len(SEEDED_REF):-1], value)
    if isinstance(value, list):
        return [resolve_seeded(v, ids) for v in value]
    if isinstance(value, dict):
        return {k: resolve_seeded(v, ids) for k, v in value.items()}
    return value


def node_configuration(node: dict, ids: dict[str, str]) -> dict | None:
    """The configuration a node is created with; a Sharing step always gets organizations."""
    configuration = resolve_seeded(node.get("configuration"), ids)
    if node["component_id"] != SHARING_COMPONENT:
        return configuration if isinstance(configuration, dict) else None
    configuration = dict(configuration) if isinstance(configuration, dict) else {}
    organizations = [
        o for o in configuration.get("organizations") or [] if isinstance(o, str) and not o.startswith(SEEDED_REF)
    ]
    if not organizations and ids.get("organization-filled"):
        organizations = [ids["organization-filled"]]
    configuration["organizations"] = organizations
    configuration.setdefault("applyToElements", "only-main")
    return configuration


def existing_playbook(name: str) -> tuple[str, int]:
    """``(id, node count)`` of the playbook named *name*, ``("", 0)`` when none."""
    answer = graphql(
        "query LabPlaybooks($search: String) { playbooks(first: 50, search: $search) "
        "{ edges { node { id name playbook_definition } } } }",
        {"search": name},
    )
    edges = (((answer.get("data") or {}).get("playbooks") or {}).get("edges") or []) if isinstance(answer, dict) else []
    for edge in edges:
        node = (edge or {}).get("node") or {}
        if node.get("name") == name:
            try:
                definition = json.loads(node.get("playbook_definition") or "{}")
            except ValueError:
                definition = {}
            return str(node.get("id") or ""), len(definition.get("nodes") or [])
    return "", 0


ADD_NODE = "mutation LabPlaybookNode($id: ID!, $input: PlaybookAddNodeInput!) { playbookAddNode(id: $id, input: $input) }"
ADD_LINK = "mutation LabPlaybookLink($id: ID!, $input: PlaybookAddLinkInput!) { playbookAddLink(id: $id, input: $input) }"


def add_node(playbook_id: str, node: dict, keys: dict[str, str], ids: dict[str, str]) -> str:
    """Create *node* under its parent; the node's id, empty on an error (logged).

    A release whose ``PlaybookAddNodeInput`` has no ``parentNodeId`` (before opencti #19949) is answered by a node
    created alone, then its parent link.
    """
    data: dict = {"component_id": node["component_id"], "name": node["name"], "position": {"x": 0, "y": 0}}
    configuration = node_configuration(node, ids)
    if configuration is not None:
        data["configuration"] = json.dumps(configuration)
    parent = node.get("parent")
    if parent:
        data["parentNodeId"], data["parentPortId"] = keys.get(parent[0], ""), parent[1]
    answer = graphql(ADD_NODE, {"id": playbook_id, "input": data})
    if parent and "parentNodeId" in str(answer.get("errors") or ""):
        data.pop("parentNodeId"), data.pop("parentPortId")
        answer = graphql(ADD_NODE, {"id": playbook_id, "input": data})
        node_id = str((answer.get("data") or {}).get("playbookAddNode") or "") if not answer.get("errors") else ""
        if node_id and not add_link(playbook_id, keys.get(parent[0], ""), parent[1], node_id):
            return ""
        return node_id
    if answer.get("errors"):
        lab.log(f"opencti-pr-head: playbook node {node['name']} failed: {str(answer.get('errors'))[:300]}")
        return ""
    return str((answer.get("data") or {}).get("playbookAddNode") or "")


def add_link(playbook_id: str, from_node: str, from_port: str, to_node: str) -> bool:
    answer = graphql(ADD_LINK, {"id": playbook_id, "input": {"from_node": from_node, "from_port": from_port, "to_node": to_node}})
    if answer.get("errors"):
        lab.log(f"opencti-pr-head: playbook link {from_port} failed: {str(answer.get('errors'))[:300]}")
        return False
    return True


def playbook_entry() -> dict:
    """The plan entry of the built-in playbook fixture, after the entities it may reference."""
    return {"label": "Playbook", "query": "", "variables": {}, "path": PLAYBOOK_PATH, "kind": "playbook",
            "playbook": PLAYBOOK}


def seed_playbook(playbook: dict, base: str, ids: dict[str, str], label: str = "Playbook") -> tuple[str, str] | None:
    """Build *playbook* node by node and log its page; a playbook already built is kept (idempotent).

    The page is logged ``seeded opencti page <label> <name> (<nodes> nodes, <links> links): <url>``.
    """
    nodes_count, links_count = playbook_counts(playbook)
    available = available_components()
    missing = missing_components(playbook, available) if available is not None else []
    if missing:
        # A node whose component the backend does not list renders no card and blanks the editor's canvas.
        lab.log(f"opencti-pr-head: seed {label} skipped: components not available on this backend: {', '.join(missing)}")
        return None
    unconfigured = missing_configuration(playbook)
    if unconfigured:
        # Named before any write: the backend refuses such a node and the seed would stop on a partial canvas.
        lab.log(f"opencti-pr-head: seed {label} skipped: required configuration missing: {'; '.join(unconfigured)}")
        return None
    playbook_id, existing = existing_playbook(playbook["name"])
    if (not existing or existing != nodes_count) and uses_component(playbook, CONNECTOR_COMPONENT) \
            and not ids.get(CONNECTOR_KIND):
        ids[CONNECTOR_KIND] = enrichment_connector()
        if not ids[CONNECTOR_KIND]:
            # Named before any write: a connector step without its connector is refused and leaves a partial canvas.
            lab.log(f"opencti-pr-head: seed {label} skipped: no enrichment connector for the {CONNECTOR_COMPONENT} step")
            return None
    if playbook_id and existing and existing != nodes_count:
        # An earlier fixture (another step count) is replaced, so a kept canvas is always the current fixture.
        answer = graphql("mutation LabPlaybookDelete($id: ID!) { playbookDelete(id: $id) }", {"id": playbook_id})
        if answer.get("errors"):
            lab.log(f"opencti-pr-head: seed {label} could not replace the earlier playbook: {str(answer.get('errors'))[:300]}")
            return None
        playbook_id, existing = "", 0
    if not playbook_id:
        answer = graphql(
            "mutation LabPlaybook($input: PlaybookAddInput!) { playbookAdd(input: $input) { id } }",
            {"input": {"name": playbook["name"], "description": playbook.get("description") or ""}},
        )
        playbook_id = created_id(answer)
        if not playbook_id:
            lab.log(f"opencti-pr-head: seed {label} failed: {str(answer.get('errors'))[:300]}")
            return None
    if not existing:
        keys: dict[str, str] = {}
        for node in playbook.get("nodes") or []:
            node_id = add_node(playbook_id, node, keys, ids)
            if not node_id:
                lab.log(f"opencti-pr-head: seed {label} stopped at {node['name']}: the playbook is partial")
                return None
            keys[node["key"]] = node_id
        for from_key, port, to_key in playbook.get("links") or []:
            if not add_link(playbook_id, keys.get(from_key, ""), port, keys.get(to_key, "")):
                return None
    url = page_url(base, PLAYBOOK_PATH, playbook_id)
    state = "kept" if existing else "built"
    lab.log(f"{SESSION_PREFIX} {label} {playbook['name']} ({nodes_count} nodes, {links_count} links, {state}): {url}")
    return f"{label} {playbook['name']}", url


def seed_label() -> str:
    answer = graphql("mutation LabLabel($input: LabelAddInput!) { labelAdd(input: $input) { id } }", {"input": LABEL})
    if not created_id(answer):
        lab.log(f"opencti-pr-head: seed label failed: {str(answer.get('errors'))[:300]}")
    return created_id(answer)


def seed_vocabulary() -> None:
    answer = graphql(
        "mutation LabVocabulary($input: VocabularyAddInput!) { vocabularyAdd(input: $input) { id } }", {"input": VOCABULARY}
    )
    if created_id(answer):
        lab.log(f"opencti-pr-head: seeded vocabulary {VOCABULARY['category']}: {VOCABULARY['name']}")
    else:
        lab.log(f"opencti-pr-head: seed vocabulary failed: {str(answer.get('errors'))[:300]}")


#: The names OpenCTI reads as each theme (utils/themeName.ts of the front: built-in first, then legacy).
THEME_NAMES = {"dark": ("Filigran Dark", "Dark"), "light": ("Filigran Light", "Light")}


def theme_value(theme: str, answer: dict) -> str:
    """The value of the user's ``theme`` field for *theme*, from a ``themes`` query *answer*.

    Since the theme module, ``me.theme`` is a theme id (the front falls back to the platform theme on any other
    value); an older release reads the literal ``dark`` / ``light``, kept when the query answers no match.
    """
    data = answer.get("data") if isinstance(answer, dict) else None
    edges = ((data or {}).get("themes") or {}).get("edges") or []
    by_name = {str(e.get("node", {}).get("name")): str(e.get("node", {}).get("id") or "") for e in edges if isinstance(e, dict)}
    for name in THEME_NAMES[theme]:
        if by_name.get(name):
            return by_name[name]
    return theme


def set_theme(theme: str) -> bool:
    value = theme_value(theme, graphql("query LabThemes { themes(first: 100) { edges { node { id name } } } }"))
    answer = graphql(
        "mutation LabTheme($input: [EditInput]!) { meEdit(input: $input) { id } }",
        {"input": [{"key": "theme", "value": [value]}]},
    )
    return bool(created_id(answer))


# --- Preview --------------------------------------------------------------------------------------------------------


def lab_route(path: str) -> tuple[str, str] | None:
    """``(theme, next path)`` for ``/lab/theme/<dark|light>[?next=...]``, else ``None``.

    ``next`` is kept only as a path of this origin (a full URL or ``//host`` falls back to the dashboard).
    """
    parts = urllib.parse.urlsplit(path)
    if not parts.path.startswith("/lab/theme/"):
        return None
    theme = parts.path.removeprefix("/lab/theme/").strip("/")
    if theme not in THEMES:
        return None
    wanted = urllib.parse.parse_qs(parts.query).get("next", ["/dashboard"])[0]
    if not wanted.startswith("/") or wanted.startswith("//"):
        wanted = "/dashboard"
    return theme, wanted


def forwarded_headers(headers: list[tuple[str, str]]) -> dict[str, str]:
    """The request's headers for Vite: the Host rewritten, the admin's bearer token set (no cookie session needed)."""
    kept = {k: v for k, v in headers if k.lower() not in ("host", "connection", "content-length", "authorization")}
    kept["Host"] = f"127.0.0.1:{VITE_PORT}"
    kept["Authorization"] = f"Bearer {ADMIN_TOKEN}"
    return kept


class Preview(http.server.BaseHTTPRequestHandler):
    """The preview port: ``/lab/theme/*`` here, the rest (websockets included) to Vite as the lab admin."""

    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - the base class's name
        return

    def _theme(self, theme: str, wanted: str) -> None:
        done = set_theme(theme)
        lab.log(f"opencti-pr-head: theme {theme} {'set' if done else 'NOT set'} for the lab admin")
        self.send_response(302)
        self.send_header("Location", wanted)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _tunnel(self) -> None:
        upstream = socket.create_connection(("127.0.0.1", VITE_PORT), timeout=10)
        lines = [f"{self.command} {self.path} {self.request_version}"]
        lines += [f"{k}: {v}" for k, v in forwarded_headers(list(self.headers.items())).items()]
        upstream.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        self.close_connection = True
        sockets = [self.connection, upstream]
        upstream.settimeout(None)
        self.connection.settimeout(None)
        try:
            while True:
                readable, _, _ = select.select(sockets, [], [], 300)
                if not readable:
                    return
                for sock in readable:
                    data = sock.recv(65536)
                    if not data:
                        return
                    (upstream if sock is self.connection else self.connection).sendall(data)
        finally:
            upstream.close()

    def _forward(self) -> None:
        if "upgrade" in (self.headers.get("Connection") or "").lower():
            self._tunnel()
            return
        length = int(self.headers.get("Content-Length") or 0)
        payload = self.rfile.read(length) if length else b""
        upstream = http.client.HTTPConnection("127.0.0.1", VITE_PORT, timeout=600)
        headers = forwarded_headers(list(self.headers.items()))
        if payload:
            headers["Content-Length"] = str(len(payload))
        upstream.request(self.command, self.path, body=payload or None, headers=headers)
        lab.relay_response(self, upstream.getresponse())
        upstream.close()

    def _ready(self) -> None:
        data = json.dumps({"ready": True, "backend": BACKEND_URL}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _handle(self) -> None:
        try:
            route = lab_route(self.path)
            if self.path.split("?", 1)[0] == READY_PATH:
                self._ready()
            elif route:
                self._theme(*route)
            else:
                self._forward()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_HEAD = _handle


# --- Start ----------------------------------------------------------------------------------------------------------


def opencti_checkout() -> Path:
    where = lab.CHECKOUT / os.environ.get("LAB_OPENCTI_CHECKOUT", "opencti")
    if not (where / "yarn.lock").is_file() or not all((where / d / "package.json").is_file() for d in (GRAPHQL_DIR, FRONT_DIR)):
        raise SystemExit(
            f"opencti-pr-head: no OpenCTI checkout at {where}: materialize OpenCTI-Platform/opencti at the PR head with "
            f"sub_paths {list(SUB_PATHS)} under this checkout's {where.name}/ directory, then start again"
        )
    return where


def connectors_manifest(graphql_dir: Path, node: Path, env: dict[str, str]) -> None:
    """The connectors manifest the catalog imports: fetched as the build does, else an empty catalog (logged)."""
    process = lab.spawn("manifest", [str(node), "script/get-connectors-manifest.ts"], cwd=graphql_dir, env=env, daemon=False)
    if process.wait() == 0:
        return
    target = graphql_dir / "src" / "__generated__" / "opencti-manifest.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"id": "lab-empty-catalog", "name": "Lab empty catalog", "contracts": []}), encoding="utf-8")
    lab.log("opencti-pr-head: WARNING the connectors manifest could not be fetched: an empty catalog was written")


def toolchain_env(node: Path, environ: dict[str, str]) -> dict[str, str]:
    """The yarn environment: the pinned node first on PATH, and node-gyp pointed at its own headers.

    The pinned node package ships ``include/node``; without ``npm_config_nodedir`` node-gyp downloads the headers
    from nodejs.org, which the sandbox network refuses, and a native module (node-calls-python) fails the install.
    """
    return {**environ, "YARN_ENABLE_TELEMETRY": "0", "YARN_ENABLE_PROGRESS_BARS": "0",
            "YARN_ENABLE_GLOBAL_CACHE": "false", "YARN_CACHE_FOLDER": str(lab.CACHE / "yarn-cache"),
            "npm_config_nodedir": str(node.parent.parent),
            "PATH": f"{node.parent}:{environ.get('PATH', '')}"}


def main() -> None:
    root = opencti_checkout()
    graphql_dir, front = root / GRAPHQL_DIR, root / FRONT_DIR
    lab.ROOT.mkdir(parents=True, exist_ok=True)
    lab.CACHE.mkdir(exist_ok=True)
    node, yarn = node_binary(), lab.CACHE / "yarn" / "package" / "bin" / "yarn.js"
    for spec, target in ((lab.NODE, "node"), (lab.YARN, "yarn")):
        with tarfile.open(fileobj=io.BytesIO(lab.fetch(spec)), mode="r:gz") as archive:
            archive.extractall(lab.CACHE / target, filter="data")
    node.chmod(0o755)
    env = toolchain_env(node, dict(os.environ))
    lab.run("yarn", [str(node), str(yarn), "workspaces", "focus", "opencti-graphql", "opencti-front"], cwd=root, env=env)
    requirements = graphql_dir / "src" / "python" / "requirements.txt"
    if requirements.is_file():
        pip = lab.spawn("pip", [sys.executable, "-m", "pip", "install", "-q", "-r", str(requirements)], daemon=False)
        if pip.wait() != 0:
            lab.log("opencti-pr-head: WARNING the backend's Python requirements did not install: STIX pattern checks fail")
    connectors_manifest(graphql_dir, node, env)
    lab.run("codegen", [str(node), str(yarn), "build:types"], cwd=graphql_dir, env=env)
    lab.run("backend-build", [str(node), "builder/builder.ts", "--development"], cwd=graphql_dir, env=env)
    encryption_key = base64.b64encode(secrets.token_bytes(32)).decode()
    lab.spawn("opencti-backend", [str(node), "--enable-source-maps", "build/back.mjs"], cwd=graphql_dir,
              env={**env, **backend_env(encryption_key)})
    lab.run("relay", [str(node), str(yarn), "relay"], cwd=front, env=env)
    lab.spawn(
        "opencti-front",
        [str(node), "node_modules/vite/bin/vite.js", "--host", "127.0.0.1", "--port", str(VITE_PORT), "--strictPort"],
        cwd=front,
        env={**env, "BACK_END_URL": BACKEND_URL, "FRONT_END_PORT": str(VITE_PORT)},
    )
    lab.wait_http(f"http://127.0.0.1:{VITE_PORT}/", 300, "opencti-front")
    lab.wait_http(HEALTH_URL, 1200, "opencti-backend (migrations at boot)")
    server = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), Preview)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://localhost:{PORT}"
    seed_vocabulary()
    plan = [*seed_plan(seed_label()), playbook_entry(), *extra_seed(lab.CHECKOUT)]
    pages = run_seed(plan, base)
    version = json.loads((front / "package.json").read_text(encoding="utf-8")).get("version", "")
    lab.log(f"lab up: {base}/dashboard, OpenCTI {version} backend and front from {root.name}/, signed in as the lab admin "
            f"on every page; {len(pages)} seeded pages; theme: {base}/lab/theme/dark or /lab/theme/light (?next=<path>)")
    while True:
        for child in lab.CHILDREN:
            if child.poll() is not None:
                raise SystemExit(f"opencti-pr-head: {child.args[0]} exited with {child.returncode}")
        time.sleep(2)


if __name__ == "__main__":
    try:
        main()
    finally:
        for child in lab.CHILDREN:
            if child.poll() is None:
                child.terminate()
