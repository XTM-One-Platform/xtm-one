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
   token on the default tenant: assets, an asset group, a scenario and a simulation of it.
4. ``/lab/sign-in`` sets the ``openaev_token`` cookie to the admin token (``TokenAuthenticationFilter`` reads
   it), then opens ``/``. Theme: ``/lab/theme/dark`` or ``/lab/theme/light`` (``PUT /api/me/profile``).
5. This script listens on ``PORT``, the preview port: ``/lab/*`` here, everything else (websockets included) to
   Vite on ``VITE_PORT``, which proxies ``/api`` to the backend. Node and yarn are the lab's pinned downloads.
"""

from __future__ import annotations

import http.server
import io
import json
import os
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


def seed_steps() -> list[tuple[str, str, object, str | None, str | None]]:
    """The seed in order: (step name, tenant resource, body, id field of the answer, STATE key of that id)."""
    steps: list[tuple[str, str, object, str | None, str | None]] = [
        (f"asset {asset['asset_name']}", "endpoints/agentless", asset, "asset_id", None) for asset in SEED_ASSETS
    ]
    steps.append(("asset group Lab servers", "asset_groups",
                  {"asset_group_name": "Lab servers", "asset_group_description": "Seeded by the lab"}, None, None))
    steps.append((f"scenario {SEED_SCENARIO['scenario_name']}", "scenarios", SEED_SCENARIO, "scenario_id", "scenario"))
    steps.append((f"simulation {SEED_SIMULATION['exercise_name']}", "exercises", SEED_SIMULATION, "exercise_id",
                  "simulation"))
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
            created = seed_call("POST", tenant_path(resource), body)
            created_id = str(created[id_field]) if id_field else ""  # type: ignore[index]
        except (SeedError, KeyError, TypeError) as exc:
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
