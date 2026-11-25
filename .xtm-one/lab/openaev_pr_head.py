"""The OpenAEV PR-head lab: OpenAEV's backend and front built from the checkout under review, seeded, signed in.

``[service.lab-openaev]`` in ``.xtm-one/checks.toml`` runs this script (standard library only) on the
``toolchain-jvm`` image (Temurin JDK 21 and Maven 3.9), so a UI pull request of OpenAEV is reviewed rendered
against its own backend, its own Flyway migrations and seeded data, not a stub (xtm-one #11799, item 5):

1. The OpenAEV source is a nested checkout under this checkout, ``openaev/`` by default (``LAB_OPENAEV_CHECKOUT``):
   materialize_repository(OpenAEV-Platform/openaev, ref=<the PR head>, sub_paths=OPENAEV_SUB_PATHS,
   path="<this checkout>/openaev"). The start exits 1 naming a missing one.
2. The backend: ``mvn install -pl openaev-api -am -DskipTests`` (the local repository in the lab cache), then
   ``java -jar openaev-api/target/openaev-api.jar`` on ``BACKEND_PORT``, 8080, the port openaev-front's
   ``vite.config.ts`` proxies ``/api`` to. Its stores: PostgreSQL and S3 from ``.xtm-one/stores.py`` (database
   ``openaev``; pg_trgm and hstore are in the contrib set), OpenSearch and RabbitMQ from the OpenCTI lab's store
   services (``lab-opencti-search`` on 9200, ``lab-opencti-broker`` on 5672), as OpenAEV's CI runs them.
3. The administrator is the platform's own (``OPENAEV_ADMIN_*``); the seed goes through the public API with its
   token on the default tenant: assets, an asset group, a scenario and a simulation of it; then, through psql, one
   finished simulation of that scenario per past week with scored expectations (``seed_history``).
4. ``/lab/sign-in`` sets the ``openaev_token`` cookie to the admin token (``TokenAuthenticationFilter`` reads
   it), then opens ``/``. Theme: ``/lab/theme/dark`` or ``/lab/theme/light`` (``PUT /api/me/profile``).
5. This script listens on ``PORT``, the preview port: ``/lab/*`` here, everything else (websockets included) to
   Vite on ``VITE_PORT``, which proxies ``/api`` to the backend. Node and yarn are the lab's pinned downloads.
6. The XTM counterpart (gap lab-xtm-counterpart-stub): the default tenant is connected to an OpenCTI stub this
   script serves on ``OPENCTI_STUB_PORT`` (``openaev.xtm.opencti.*``, the legacy keys the default tenant reads),
   so the backend's own counterpart lookup answers and every pivot renders its found and not-found states with no
   browser stub: ``POST /graphql`` answers ``stixCoreObjects`` from ``OPENCTI_COUNTERPARTS`` (two of the seeded
   assets, an attack pattern, a vulnerability; ``lab-mac-01`` stays not found) and every other query empty;
   ``/dashboard/id/<id>`` is the page a found pivot opens. ``LAB_OPENAEV_OPENCTI=off`` leaves OpenCTI not
   connected, the teaser state.
"""

from __future__ import annotations

import http.server
import io
import json
import os
import subprocess
import sys
import tarfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lab  # noqa: E402 - .xtm-one/lab/lab.py: the pinned downloads, the stores and the process helpers
import opencti_ask_ariane as proxy  # noqa: E402 - the preview Front: forward and websocket tunnel to Vite

PORT = 3700
VITE_PORT = 3701
#: openaev-front/vite.config.ts proxies /api to http://localhost:8080, fixed in the product.
BACKEND_PORT = 8080
DATABASE = "openaev"
BUCKET = "openaev"
#: openaev-model Tenant.DEFAULT_TENANT_UUID: the tenant every seeded row belongs to.
TENANT_ID = "2cffad3a-0001-4078-b0e2-ef74274022c3"
#: Never a person's: the lab's database lives and dies with the lab container.
ADMIN_EMAIL = "lab@filigran.io"
ADMIN_PASSWORD = "Lab-Admin-1234!"
#: Drawn at each start: the backend's admin token, the API seed's bearer and the sign-in cookie.
ADMIN_TOKEN = str(uuid.uuid4())
COOKIE_NAME = "openaev_token"
#: HealthCheckApi requires health_access_key equal to openaev.healthcheck.key (400 without it).
HEALTH_KEY = "lab-only-health-key"
HEALTH_PATH = f"/api/health?health_access_key={HEALTH_KEY}"
#: The Maven reactor the API builds from, and the front.
OPENAEV_SUB_PATHS = (
    "pom.xml",
    "openaev-api",
    "openaev-framework",
    "openaev-model",
    "openaev-build-services",
    "openaev-maven-plugin",
    "openaev-es8-client",
    "openaev-es9-client",
    "openaev-opensearch-client",
    "openaev-ocsf",
    "openaev-front",
)
#: The seed, created in order through the API (tenant routes, so the write scope names one tenant).
SEED_ASSETS = (
    {"asset_name": "lab-web-01", "asset_hostname": "lab-web-01.lab.local", "asset_ips": ["10.0.0.11"],
     "endpoint_platform": "Linux", "endpoint_arch": "x86_64"},
    {"asset_name": "lab-dc-01", "asset_hostname": "lab-dc-01.lab.local", "asset_ips": ["10.0.0.2"],
     "endpoint_platform": "Windows", "endpoint_arch": "x86_64"},
    {"asset_name": "lab-mac-01", "asset_hostname": "lab-mac-01.lab.local", "asset_ips": ["10.0.0.42"],
     "endpoint_platform": "MacOS", "endpoint_arch": "arm64"},
)
SEED_SCENARIO = {
    "scenario_name": "Lab ransomware readiness",
    "scenario_description": "Seeded by the xtm-one OpenAEV lab: initial access, discovery and impact on the lab assets.",
    "scenario_category": "attack-scenario",
    "scenario_severity": "high",
}
SEED_SIMULATION = {
    "exercise_name": "Lab ransomware readiness, simulation 1",
    "exercise_description": "Seeded by the xtm-one OpenAEV lab.",
    "exercise_category": "attack-scenario",
    "exercise_severity": "high",
}
#: The agent the atomic testing targets, registered as an OpenAEV agent would (``/endpoints/register``).
SEED_AGENT = {
    "asset_name": "lab-agent-01",
    "asset_external_reference": "lab-agent-01-ref",
    "asset_hostname": "lab-agent-01.lab.local",
    "asset_ips": ["10.0.0.21"],
    "asset_mac_addresses": ["02:00:00:00:00:21"],
    "endpoint_platform": "Linux",
    "endpoint_arch": "x86_64",
    "agent_is_service": True,
    "agent_is_elevated": True,
    "agent_executed_by_user": "root",
    "agent_installation_mode": "service",
    "agent_installation_directory": "/opt/openaev-agent",
    "agent_service_name": "openaev-agent",
}
#: A manual command payload: creating it creates its injector contract, the one the atomic testing runs.
SEED_PAYLOAD = {
    "payload_type": "Command",
    "payload_name": "Lab whoami",
    "payload_description": "Seeded by the xtm-one OpenAEV lab.",
    "payload_source": "MANUAL",
    "payload_status": "VERIFIED",
    "payload_platforms": ["Linux"],
    "command_executor": "sh",
    "command_content": "whoami",
    "payload_domains": [],
}
SEED_ATOMIC_TESTING = {
    "inject_title": "Lab whoami on lab-agent-01",
    "inject_description": "Seeded by the xtm-one OpenAEV lab; launched, no implant answers, so it stays running.",
    "inject_content": {},
}
#: The history the dashboards and trends read: finished simulations of the seeded scenario, one per past week, each
#: with prevention, detection and vulnerability expectations on the seeded assets and asset groups, some failed (gap
#: lab-openaev-no-history-seed: run 043c787d wrote them by hand through lab-sql). The API cannot back-date a
#: simulation, so this step writes the rows with psql; every row's updated_at is now, so the indexer picks them up.
HISTORY_WEEKS = 10
HISTORY_STEP = "history of finished simulations"
HISTORY_SQL = """
SELECT set_config('lab.scenario', :'scenario', false), set_config('lab.weeks', :'weeks', false);
DO $$
DECLARE
  sc text; w int; ex text; inj text; g record; a record; k int := 0;
  weeks int := current_setting('lab.weeks')::int;
BEGIN
  SELECT scenario_id INTO sc FROM scenarios WHERE scenario_name = current_setting('lab.scenario') LIMIT 1;
  IF sc IS NULL THEN
    RAISE EXCEPTION 'no scenario named %', current_setting('lab.scenario');
  END IF;
  FOR w IN 1..weeks LOOP
    ex := gen_random_uuid()::text;
    INSERT INTO exercises(exercise_id, exercise_name, exercise_start_date, exercise_end_date, exercise_status,
                          exercise_category, exercise_created_at, exercise_updated_at)
    VALUES (ex, current_setting('lab.scenario') || ', week ' || (weeks + 1 - w), now() - w * interval '7 days',
            now() - w * interval '7 days' + interval '2 hours', 'FINISHED', 'attack-scenario',
            now() - w * interval '7 days', now());
    INSERT INTO scenarios_exercises(scenario_id, exercise_id) VALUES (sc, ex);
    inj := gen_random_uuid()::text;
    INSERT INTO injects(inject_id, inject_title, inject_all_teams, inject_enabled, inject_depends_duration,
                        inject_exercise, inject_created_at, inject_updated_at)
    VALUES (inj, 'Execute validation payloads', false, true, 0, ex, now() - w * interval '7 days', now());
    FOR g IN SELECT asset_group_id FROM asset_groups LOOP
      k := k + 1;
      INSERT INTO injects_expectations(inject_expectation_id, inject_expectation_created_at,
        inject_expectation_updated_at, exercise_id, inject_id, inject_expectation_type, inject_expectation_score,
        inject_expectation_expected_score, inject_expectation_name, inject_expectation_group, asset_group_id,
        inject_expiration_time, inject_expectation_signatures_initialized, inject_expectation_collector_missing_at_init)
      VALUES (gen_random_uuid()::text, now() - w * interval '7 days', now(), ex, inj,
              CASE WHEN k % 2 = 0 THEN 'PREVENTION' ELSE 'DETECTION' END,
              CASE WHEN (k * 7 + w) % 5 < 2 THEN 0 ELSE 100 END, 100,
              CASE WHEN k % 2 = 0 THEN 'Prevention' ELSE 'Detection' END, true, g.asset_group_id, 21600, true, false);
    END LOOP;
    FOR a IN SELECT asset_id FROM assets WHERE asset_type = 'Endpoint' LOOP
      k := k + 1;
      INSERT INTO injects_expectations(inject_expectation_id, inject_expectation_created_at,
        inject_expectation_updated_at, exercise_id, inject_id, inject_expectation_type, inject_expectation_score,
        inject_expectation_expected_score, inject_expectation_name, inject_expectation_group, asset_id,
        inject_expiration_time, inject_expectation_signatures_initialized, inject_expectation_collector_missing_at_init)
      VALUES (gen_random_uuid()::text, now() - w * interval '7 days', now(), ex, inj,
              CASE k % 3 WHEN 0 THEN 'PREVENTION' WHEN 1 THEN 'DETECTION' ELSE 'VULNERABILITY' END,
              CASE WHEN (k * 3 + w) % 4 = 0 THEN 0 ELSE 100 END, 100,
              CASE k % 3 WHEN 0 THEN 'Prevention' WHEN 1 THEN 'Detection' ELSE 'Vulnerability' END,
              false, a.asset_id, 21600, true, false);
    END LOOP;
  END LOOP;
END $$;
SELECT count(*) FROM exercises WHERE exercise_status = 'FINISHED';
"""
STATE: dict[str, str] = {}
#: The seed steps the database holds (step name: created id), written after each step: a kept database seeds only
#: the steps this record lacks, so a step that failed or was cut on the first start lands on the next one.
SEEDED = lab.CACHE / "openaev-seeded.json"
#: A seed call that meets a backend still warming up (refused, 5xx) is tried again; a 4xx answer is final.
SEED_ATTEMPTS = 5
SEED_RETRY_SECONDS = 6.0
#: Spring Boot serves /api/health once Tomcat is up, before its CommandLineRunners ran: InitAdminCommandLineRunner
#: writes the admin token after that, so a seed sent on the health answer alone was refused 401 (gap
#: lab-openaev-seed-401). The seed waits until the token authenticates GET /api/me.
ADMIN_TOKEN_WAIT_SECONDS = 600.0
ADMIN_TOKEN_POLL_SECONDS = 3.0


class SeedError(Exception):
    """A seed step that did not land, with the reason the backend gave."""


def missing_sub_paths(where: Path) -> list[str]:
    return [sub_path for sub_path in OPENAEV_SUB_PATHS if not (where / sub_path).exists()]


def openaev_checkout() -> Path:
    where = lab.CHECKOUT / os.environ.get("LAB_OPENAEV_CHECKOUT", "openaev")
    missing = missing_sub_paths(where)
    if missing:
        raise SystemExit(
            f"openaev: the OpenAEV checkout at {where} lacks {', '.join(missing)}: materialize "
            f"OpenAEV-Platform/openaev at the PR head with sub_paths {list(OPENAEV_SUB_PATHS)} under this "
            f"checkout's {where.name}/ directory, then start again"
        )
    return where


#: OpenAEV's ``enabled_dev_features`` in the lab: ``*`` turns every ``PreviewFeature`` on.
PREVIEW_FEATURES = "*"

#: The OpenCTI the lab connects the default tenant to: a stub on this port, answering the counterpart lookup.
OPENCTI_STUB_PORT = 3702
OPENCTI_STUB_URL = f"http://localhost:{OPENCTI_STUB_PORT}/"
OPENCTI_STUB_TOKEN = "lab-only-opencti-token-never-used-outside-the-lab"
#: The OpenCTI objects the stub holds, keyed by what OpenAEV looks them up by (an asset's name or its
#: hostname, an attack pattern's MITRE id, a vulnerability's name); lab-mac-01 is absent on purpose, the
#: not-found state. ``hostname`` is a lookup key only (the seeded ``asset_hostname`` of SEED_ASSETS), never
#: a field of the answered node.
OPENCTI_COUNTERPARTS = (
    {"id": "lab-octi-system-web-01", "entity_type": "System", "name": "lab-web-01",
     "hostname": "lab-web-01.lab.local", "confidence": 80, "reports": 3, "markings": ("TLP:AMBER",)},
    {"id": "lab-octi-ipv4-dc-01", "entity_type": "IPv4-Addr", "observable_value": "lab-dc-01",
     "hostname": "lab-dc-01.lab.local", "reports": 1, "markings": ("TLP:GREEN",)},
    {"id": "lab-octi-attack-pattern-t1486", "entity_type": "Attack-Pattern", "name": "Data Encrypted for Impact",
     "x_mitre_id": "T1486", "confidence": 100, "reports": 12, "markings": ("TLP:CLEAR",)},
    {"id": "lab-octi-vulnerability-cve-2024-3400", "entity_type": "Vulnerability", "name": "CVE-2024-3400",
     "confidence": 90, "reports": 7, "markings": ("TLP:CLEAR",)},
)
OPENCTI_MODIFIED = "2026-11-20T09:00:00.000Z"


def opencti_connected() -> bool:
    """Whether the lab connects the default tenant to the OpenCTI stub (``LAB_OPENAEV_OPENCTI=off``: not connected)."""
    return os.environ.get("LAB_OPENAEV_OPENCTI", "on").strip().lower() not in ("off", "0", "false", "no")


def opencti_env() -> dict[str, str]:
    """``openaev.xtm.opencti.*`` (the legacy keys, read as the default tenant's connection) onto the stub.

    Through SPRING_APPLICATION_JSON, not one variable per key: XtmConfig binds ``opencti`` as a
    ``Map<String, Object>``, and a relaxed env name such as ``..._API_URL`` would bind a nested ``api`` key, read
    as a tenant. The API URL is the product's fallback, ``url`` + ``graphql``."""
    if not opencti_connected():
        return {}
    connection = {"enable": True, "url": OPENCTI_STUB_URL, "token": OPENCTI_STUB_TOKEN}
    return {"SPRING_APPLICATION_JSON": json.dumps({"openaev": {"xtm": {"opencti": connection}}})}


def _filter_values(node: object, out: set[str]) -> None:
    """Every value of every filter of a GraphQL FilterGroup but the entity_type ones, recursively."""
    if isinstance(node, dict):
        keys = node.get("key")
        values = node.get("values")
        if isinstance(values, list) and keys != ["entity_type"]:
            out.update(v for v in values if isinstance(v, str))
        for child in node.values():
            _filter_values(child, out)
    elif isinstance(node, list):
        for child in node:
            _filter_values(child, out)


def opencti_node(item: dict) -> dict:
    """One stub object in the shape OpenAEV's OpenCTICounterpartProvider reads."""
    node = {
        "id": item["id"],
        "entity_type": item["entity_type"],
        "standard_id": f"{item['entity_type'].lower()}--{item['id']}",
        "representative": {"main": item.get("name") or item.get("observable_value")},
        "modified": OPENCTI_MODIFIED,
        "reports": {"pageInfo": {"globalCount": item.get("reports", 0)}},
        "objectMarking": [{"definition": m} for m in item.get("markings", ())],
    }
    for field in ("name", "observable_value", "x_mitre_id", "confidence"):
        if field in item:
            node[field] = item[field]
    return node


#: The fields of a counterpart a lookup value matches, case-insensitively.
COUNTERPART_LOOKUP_KEYS = ("id", "name", "observable_value", "x_mitre_id", "hostname")


def opencti_answer(body: dict) -> dict:
    """The stub's GraphQL answer: the counterpart lookup from OPENCTI_COUNTERPARTS, any other query empty."""
    query = str(body.get("query") or "")
    if "stixCoreObjects" in query:
        wanted: set[str] = set()
        _filter_values((body.get("variables") or {}).get("filters"), wanted)
        wanted_lower = {w.lower() for w in wanted}
        edges = [
            {"node": opencti_node(item)}
            for item in OPENCTI_COUNTERPARTS
            if any(str(item.get(f, "")).lower() in wanted_lower
                   for f in COUNTERPART_LOOKUP_KEYS if item.get(f))
        ]
        return {"data": {"stixCoreObjects": {"edges": edges}}}
    if "externalReferences" in query:
        return {"data": {"externalReferences": {"edges": []}}}
    return {"data": {}}


class OpenCtiStub(http.server.BaseHTTPRequestHandler):
    """The OpenCTI the lab's tenant is connected to: GraphQL on /graphql, a plain page per object id."""

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - BaseHTTPRequestHandler's signature
        return

    def _send(self, status: int, data: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:  # noqa: N802 - http.server's name
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            body = {}
        self._send(200, json.dumps(opencti_answer(body if isinstance(body, dict) else {})).encode(), "application/json")

    def do_GET(self) -> None:  # noqa: N802 - http.server's name
        object_id = self.path.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
        item = next((i for i in OPENCTI_COUNTERPARTS if i["id"] == object_id), None)
        title = (item.get("name") or item.get("observable_value")) if item else "OpenCTI lab stub"
        page = f"<!doctype html><title>{title}</title><h1>OpenCTI (lab stub)</h1><p>{title}</p>".encode()
        self._send(200 if item or object_id in ("", "graphql") else 404, page, "text/html; charset=utf-8")


def start_opencti_stub() -> None:
    """Serve the OpenCTI stub in a thread, before the backend boots (its connector registers at start)."""
    if not opencti_connected():
        lab.log("openaev: OpenCTI not connected (LAB_OPENAEV_OPENCTI=off): every pivot shows its teaser")
        return
    server = http.server.ThreadingHTTPServer(("127.0.0.1", OPENCTI_STUB_PORT), OpenCtiStub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    lab.log(f"openaev: default tenant connected to the OpenCTI stub {OPENCTI_STUB_URL} "
            f"({len(OPENCTI_COUNTERPARTS)} counterparts)")


def backend_env() -> dict[str, str]:
    """Spring's relaxed binding of application.properties onto the lab's stores, as OpenAEV's CI sets them."""
    return {
        "SERVER_PORT": str(BACKEND_PORT),
        "OPENAEV_BASE_URL": f"http://localhost:{PORT}",
        "SPRING_DATASOURCE_URL": f"jdbc:postgresql://127.0.0.1:{lab.stores.PG_PORT}/{DATABASE}",
        "SPRING_DATASOURCE_USERNAME": "copilot",
        "SPRING_DATASOURCE_PASSWORD": "lab",
        "MINIO_ENDPOINT": "127.0.0.1",
        "MINIO_PORT": str(lab.stores.S3_PORT),
        "MINIO_BUCKET": BUCKET,
        "MINIO_ACCESS_KEY": lab.stores.S3_USER,
        "MINIO_ACCESS_SECRET": lab.stores.S3_PASSWORD,
        "ENGINE_URL": "http://127.0.0.1:9200",
        "OPENAEV_RABBITMQ_HOSTNAME": "127.0.0.1",
        "OPENAEV_RABBITMQ_PORT": "5672",
        "OPENAEV_RABBITMQ_USER": "guest",
        "OPENAEV_RABBITMQ_PASS": "guest",
        "OPENAEV_ADMIN_EMAIL": ADMIN_EMAIL,
        "OPENAEV_ADMIN_PASSWORD": ADMIN_PASSWORD,
        "OPENAEV_ADMIN_TOKEN": ADMIN_TOKEN,
        "OPENAEV_ADMIN_ENCRYPTION_KEY": "lab-only-encryption-key-never-used-outside",
        "OPENAEV_ADMIN_ENCRYPTION_SALT": "lab-only-salt",
        "OPENAEV_RATELIMIT_ENABLED": "false",
        "OPENAEV_HEALTHCHECK_KEY": HEALTH_KEY,
        # No egress to the Hub from a lab: its connectivity check would only log failures.
        "OPENAEV_XTM_HUB_ENABLE": "false",
        # Every preview feature on (PreviewFeature.FEATURE_FLAG_ALL, read as openaev.enabled-dev-features): a
        # pull request behind a preview flag renders its screens in the lab instead of the flag-off fallback.
        "OPENAEV_ENABLED_DEV_FEATURES": PREVIEW_FEATURES,
        **opencti_env(),
    }


#: The Maven build's inputs: a source of one of these modules newer than the jar means a rebuild.
BUILD_INPUTS = tuple(p for p in OPENAEV_SUB_PATHS if p != "openaev-front")
#: The class data sharing archive of the backend's JVM, written at its first exit and read by the next boot.
CDS_ARCHIVE = lab.CACHE / "openaev-api.jsa"
#: The lock file's digest once openaev-front's install succeeded.
FRONT_INSTALLED = lab.CACHE / "openaev-front-installed.lock.sha256"


def jar_is_current(root: Path, jar: Path) -> bool:
    """Whether *jar* exists and no file of the build's inputs (``target/`` aside) is newer than it."""
    if not jar.is_file():
        return False
    built = jar.stat().st_mtime
    for sub_path in BUILD_INPUTS:
        where = root / sub_path
        files = [where] if where.is_file() else where.rglob("*")
        for path in files:
            if "target" in path.relative_to(root).parts or "node_modules" in path.parts:
                continue
            if path.is_file() and path.stat().st_mtime > built:
                return False
    return True


def java_argv(jar: Path) -> list[str]:
    """The backend's JVM, with a class data sharing archive (JDK 19+) so a restart boots from it."""
    return ["java", "-Xmx2g", "-XX:+AutoCreateSharedArchive", f"-XX:SharedArchiveFile={CDS_ARCHIVE}",
            "-XX:TieredStopAtLevel=1", "-jar", str(jar)]


def maven_argv(root: Path) -> list[str]:
    """The API and the reactor modules it needs, tests skipped, the local repository in the lab cache."""
    return ["mvn", "-B", "-q", "-f", str(root / "pom.xml"), f"-Dmaven.repo.local={lab.CACHE / 'm2'}",
            "-DskipTests", "-Dspotless.check.skip=true", "install", "-pl", "openaev-api", "-am"]


def toolchain() -> tuple[Path, Path, dict[str, str]]:
    """The lab's pinned node (openaev-front engines: >= 24.11) and yarn, unpacked in the lab cache."""
    lab.ROOT.mkdir(parents=True, exist_ok=True)
    lab.CACHE.mkdir(exist_ok=True)
    node = lab.CACHE / "node" / "package" / "bin" / "node"
    yarn = lab.CACHE / "yarn" / "package" / "bin" / "yarn.js"
    for spec, target, done in ((lab.NODE, "node", node), (lab.YARN, "yarn", yarn)):
        if not done.is_file():
            with tarfile.open(fileobj=io.BytesIO(lab.fetch(spec)), mode="r:gz") as archive:
                archive.extractall(lab.CACHE / target, filter="data")
    node.chmod(0o755)
    env = {**lab.YARN_ENV, "YARN_CACHE_FOLDER": str(lab.CACHE / "yarn-cache"),
           "PATH": f"{node.parent}:{os.environ.get('PATH', '')}"}
    return node, yarn, env


def api(method: str, path: str, body: object | None = None) -> object:
    """One call to the backend as the admin: a bearer token and no cookie, so CSRF does not apply."""
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(f"http://127.0.0.1:{BACKEND_PORT}{path}", data=data, method=method)
    request.add_header("Authorization", f"Bearer {ADMIN_TOKEN}")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(request, timeout=60) as response:
        raw = response.read()
    return json.loads(raw) if raw else None


def wait_for_admin_token(timeout: float = ADMIN_TOKEN_WAIT_SECONDS) -> bool:
    """Whether the admin token authenticates (``GET /api/me`` answers 200) within *timeout* seconds; a 401 or a
    refused connection is the backend still running its start-up runners, anything else is final."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            api("GET", "/api/me")
            return True
        except urllib.error.HTTPError as exc:
            if exc.code != 401 and exc.code < 500:
                lab.log(f"openaev seed: GET /api/me answered HTTP {exc.code}, not waiting for the admin token")
                return False
        except urllib.error.URLError:
            pass
        if time.monotonic() >= deadline:
            lab.log(f"openaev seed: the admin token did not authenticate within {timeout:.0f} s")
            return False
        time.sleep(ADMIN_TOKEN_POLL_SECONDS)


def tenant_path(resource: str) -> str:
    return f"/api/tenants/{TENANT_ID}/{resource}"


def seed_call(method: str, path: str, body: object) -> object:
    """One seed write, tried again while the backend refuses or answers 5xx; a 4xx is final, its answer quoted."""
    for attempt in range(1, SEED_ATTEMPTS + 1):
        try:
            return api(method, path, body)
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read()[:300].decode("utf-8", "replace").strip()
            except OSError:
                detail = ""
            reason = f"HTTP {exc.code} {detail or exc.reason}"
            if exc.code < 500:
                raise SeedError(reason) from exc
        except urllib.error.URLError as exc:
            reason = str(exc.reason)
        if attempt == SEED_ATTEMPTS:
            raise SeedError(f"{reason} (after {SEED_ATTEMPTS} attempts)")
        time.sleep(SEED_RETRY_SECONDS)
    raise SeedError("no attempt")


def platform_version() -> str:
    """The backend's own version (``GET /api/settings`` platform_version): an agent registered with any other
    version gets an upgrade job, whose installer lookup answers 500 'Agent installer version ... not found' in a
    build from source (gap lab-openaev-no-running-atomic-testing-seed)."""
    settings = api("GET", "/api/settings") or {}
    version = settings.get("platform_version") if isinstance(settings, dict) else None
    if not version:
        raise SeedError("GET /api/settings answered no platform_version")
    return str(version)


def agent_body() -> dict:
    return {**SEED_AGENT, "endpoint_agent_version": platform_version()}


def atomic_testing_body() -> dict:
    return {**SEED_ATOMIC_TESTING, "inject_injector_contract": STATE["contract"], "inject_assets": [STATE["agent"]]}


def first_contract_id(answer: object) -> str:
    """The contract the payload created, from the contract search (its labels carry the payload name)."""
    rows = answer.get("content") if isinstance(answer, dict) else None
    if not rows:
        raise KeyError(f"no injector contract labelled {SEED_PAYLOAD['payload_name']!r}")
    return str(rows[0]["injector_contract_id"])


def seed_steps() -> list[tuple[str, object, object, object, str | None]]:
    """The seed in order: (step name, tenant resource, body, id field of the answer, STATE key of that id). A
    resource or body may be a callable, called when the step runs (it reads the ids the earlier steps put in STATE);
    the id field may be a callable reading the answer."""
    steps: list[tuple[str, object, object, object, str | None]] = [
        (f"asset {asset['asset_name']}", "endpoints/agentless", asset, "asset_id", None) for asset in SEED_ASSETS
    ]
    steps.append(("asset group Lab servers", "asset_groups",
                  {"asset_group_name": "Lab servers", "asset_group_description": "Seeded by the lab"}, None, None))
    steps.append((f"scenario {SEED_SCENARIO['scenario_name']}", "scenarios", SEED_SCENARIO, "scenario_id", "scenario"))
    steps.append((f"simulation {SEED_SIMULATION['exercise_name']}", "exercises", SEED_SIMULATION, "exercise_id",
                  "simulation"))
    # A registered agent and an atomic testing launched on it, so the running and pending states render with no
    # hand SQL (gap lab-openaev-no-running-atomic-testing-seed). No implant ever answers: the testing stays running.
    steps.append((f"agent {SEED_AGENT['asset_name']}", "endpoints/register", agent_body, "asset_id", "agent"))
    steps.append((f"payload {SEED_PAYLOAD['payload_name']}", "payloads", SEED_PAYLOAD, "payload_id", "payload"))
    steps.append((f"contract of {SEED_PAYLOAD['payload_name']}", "injector_contracts/search",
                  {"page": 0, "size": 1, "textSearch": SEED_PAYLOAD["payload_name"], "include_full_details": False},
                  first_contract_id, "contract"))
    steps.append((f"atomic testing {SEED_ATOMIC_TESTING['inject_title']}", "atomic-testings", atomic_testing_body,
                  "inject_id", "atomic_testing"))
    steps.append((f"launch of {SEED_ATOMIC_TESTING['inject_title']}",
                  lambda: f"atomic-testings/{STATE['atomic_testing']}/launch", None, None, None))
    return steps


def seeded_record() -> dict[str, str]:
    try:
        record = json.loads(SEEDED.read_text())
    except (OSError, ValueError):
        return {}
    return {str(k): str(v) for k, v in record.items()} if isinstance(record, dict) else {}


def seed() -> list[str]:
    """The seed through the API; each step is logged, recorded once it lands, and a failed one is named in
    ``STATE["seed_missing"]`` (read by ``/lab/ready`` and the lab-up line), never hidden."""
    record = seeded_record()
    done: list[str] = []
    missing: list[str] = []
    for name, resource, body, id_field, state_key in seed_steps():
        if name in record:
            if state_key and record[name]:
                STATE[state_key] = record[name]
            lab.log(f"openaev seed: {name} kept from the previous run")
            continue
        try:
            path = tenant_path(resource() if callable(resource) else str(resource))
            created = seed_call("POST", path, body() if callable(body) else body)
            if callable(id_field):
                created_id = id_field(created)
            else:
                created_id = str(created[id_field]) if id_field else ""  # type: ignore[index]
        except (SeedError, KeyError, TypeError, IndexError, urllib.error.URLError) as exc:
            lab.log(f"openaev seed: {name} failed: {exc}")
            missing.append(f"{name} ({exc})")
            continue
        if state_key:
            STATE[state_key] = created_id
        record[name] = created_id
        SEEDED.parent.mkdir(parents=True, exist_ok=True)
        SEEDED.write_text(json.dumps(record))
        done.append(name)
    if missing:
        STATE["seed_missing"] = "; ".join(missing)
    else:
        STATE.pop("seed_missing", None)
    return done


def psql(sql: str, **params: str) -> str:
    """Run *sql* on the lab's ``openaev`` database; each value of *params* is bound as a psql variable
    (``:'name'``), never spliced in. The statement goes on stdin: psql interpolates variables in a script only."""
    dest = lab.stores.STORES_DIR if lab.stores.installed(lab.stores.STORES_DIR) else lab.CACHE / "stores"
    argv = [str(lab.stores.pg_bindir(dest) / "psql"), "-h", "127.0.0.1", "-p", str(lab.stores.PG_PORT),
            "-U", "copilot", "-d", DATABASE, "-v", "ON_ERROR_STOP=1", "-qAt"]
    for name, value in params.items():
        argv += ["-v", f"{name}={value}"]
    return subprocess.run(argv, input=sql, check=True, capture_output=True, text=True).stdout.strip()


def seed_history() -> bool:
    """The finished simulations of the past weeks, written once per database (recorded as ``HISTORY_STEP``) after
    the API seed, so the scenario's created rows exist. A failure is named in ``STATE["seed_missing"]`` and tried
    again on the next start; True when the rows were written in this call."""
    record = seeded_record()
    if HISTORY_STEP in record:
        lab.log(f"openaev seed: {HISTORY_STEP} kept from the previous run")
        return False
    try:
        finished = psql(HISTORY_SQL, scenario=SEED_SCENARIO["scenario_name"], weeks=str(HISTORY_WEEKS))
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = (getattr(exc, "stderr", "") or str(exc)).strip()[:300]
        lab.log(f"openaev seed: {HISTORY_STEP} failed: {detail}")
        missing = [part for part in STATE.get("seed_missing", "").split("; ") if part]
        STATE["seed_missing"] = "; ".join([*missing, f"{HISTORY_STEP} ({detail})"])
        return False
    record[HISTORY_STEP] = finished.splitlines()[-1] if finished else ""
    SEEDED.parent.mkdir(parents=True, exist_ok=True)
    SEEDED.write_text(json.dumps(record))
    return True


def set_theme(theme: str) -> None:
    me = api("GET", "/api/me") or {}
    api("PUT", "/api/me/profile", {
        "user_firstname": me.get("user_firstname") or "Lab",
        "user_lastname": me.get("user_lastname") or "Admin",
        "user_organization": me.get("user_organization"),
        "user_lang": me.get("user_lang") or "auto",
        "user_theme": theme,
        "user_country": me.get("user_country"),
    })


def sign_in_cookie() -> str:
    return f"{COOKIE_NAME}={ADMIN_TOKEN}; Path=/; HttpOnly; SameSite=Lax"


def xsrf_cookie() -> str:
    """The XSRF-TOKEN cookie before the first page loads: the backend's CookieCsrfTokenRepository (plain request
    handler) accepts a header equal to it, so the first page's search POST is not refused with 403 (an empty list)."""
    return f"XSRF-TOKEN={uuid.uuid4()}; Path=/; SameSite=Lax"


class Front(proxy.Front):
    """The preview port: ``/lab/*`` here, the rest to Vite."""

    def _redirect(self, *cookies: str) -> None:
        self._body()
        self.send_response(302)
        for cookie in cookies:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Location", "/")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _handle(self) -> None:
        path = self.path.split("?", 1)[0]
        try:
            if path == "/lab/ready":
                ready = "seeded" in STATE
                data = json.dumps({"ready": ready, **STATE}).encode()
                self.send_response(200 if ready else 503)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            elif path == "/lab/sign-in":
                self._redirect(sign_in_cookie(), xsrf_cookie())
            elif path in ("/lab/theme/dark", "/lab/theme/light"):
                set_theme(path.rsplit("/", 1)[-1])
                self._redirect()
            else:
                self._forward()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_HEAD = _handle


def main() -> None:
    root = openaev_checkout()
    front = root / "openaev-front"
    node, yarn, env = toolchain()
    lab.start_stores()
    dest = lab.stores.STORES_DIR if lab.stores.installed(lab.stores.STORES_DIR) else lab.CACHE / "stores"
    # A restart in place (sync with restart, #12198) finds the database of the previous run: kept, migrations only.
    kept = not lab.stores.ensure_database(dest, DATABASE)
    if kept:
        lab.log(f"openaev database {DATABASE} kept from the previous run")
    jar = root / "openaev-api" / "target" / "openaev-api.jar"
    if jar_is_current(root, jar):
        # A restart in place with no source newer than the jar (#12236): the Maven build is skipped.
        lab.log("openaev build kept from the previous run (no source newer than the jar)")
    else:
        lab.run("openaev-build", maven_argv(root), cwd=root)
    if not jar.is_file():
        raise SystemExit(f"openaev: the build left no {jar}")
    start_opencti_stub()
    lab.spawn("openaev-back", java_argv(jar), cwd=root / "openaev-api", env={**os.environ, **backend_env()})
    digest = lab.lock_digest(front / "yarn.lock")
    if digest and (front / "node_modules").is_dir() and FRONT_INSTALLED.is_file() and FRONT_INSTALLED.read_text() == digest:
        lab.log("openaev-front install kept from the previous run (yarn.lock unchanged)")
    else:
        lab.run("yarn", [str(node), str(yarn), "install"], cwd=front, env={**os.environ, **env})
        if digest:
            FRONT_INSTALLED.write_text(digest)
    proxy.VITE_PORT = VITE_PORT
    lab.spawn("openaev-front",
              [str(node), "node_modules/vite/bin/vite.js", "--host", "127.0.0.1", "--port", str(VITE_PORT), "--strictPort"],
              cwd=front, env={**os.environ, **env})
    # /api/health is the backend's unauthenticated route (AppSecurityConfig permitAll), keyed by
    # openaev.healthcheck.key; /api/settings answers 401 without a session.
    lab.wait_http(f"http://127.0.0.1:{BACKEND_PORT}{HEALTH_PATH}", 900, "openaev-back")
    if kept and not SEEDED.is_file():
        # A database kept from a run older than the seed record: seeding again would duplicate every row.
        lab.log("openaev seed kept from the previous run (no seed record: restart without the kept database to reseed)")
    else:
        if not kept:
            # A new database holds none of the record's rows: an empty record, so even a seed whose every step
            # failed is completed by the next start on the kept database.
            SEEDED.parent.mkdir(parents=True, exist_ok=True)
            SEEDED.write_text("{}")
        wait_for_admin_token()
        for item in seed():
            lab.log(f"seeded openaev {item}")
        if seed_history():
            lab.log(f"seeded openaev {HISTORY_STEP} ({HISTORY_WEEKS} weeks)")
    STATE["seeded"] = "1"
    lab.log(f"seeded signed-in session for {ADMIN_EMAIL} (admin token cookie)")
    lab.wait_http(f"http://127.0.0.1:{VITE_PORT}/", 300, "openaev-front")
    server = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), Front)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    lab.log(
        f"lab up: http://localhost:{PORT}/lab/sign-in signs in as {ADMIN_EMAIL} (OpenAEV built from {root.name}/, "
        + (f"seed INCOMPLETE, missing: {STATE['seed_missing']}" if "seed_missing" in STATE else "seeded")
        + "); theme at /lab/theme/dark or /lab/theme/light"
    )
    while True:
        for child in lab.CHILDREN:
            if child.poll() is not None:
                raise SystemExit(f"openaev: {child.args[0]} exited with {child.returncode}")
        time.sleep(2)


if __name__ == "__main__":
    try:
        main()
    finally:
        # Waited for, not only signalled (#12198): a restart in place reruns this script in the same container, its
        # pgdata, Maven repository and build output kept, once the JVM and PostgreSQL are gone.
        lab.stores.stop(lab.CHILDREN)
