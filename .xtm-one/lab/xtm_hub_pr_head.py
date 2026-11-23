"""The XTM Hub lab: XTM-Hub/xtm-hub built from the checkout under review, seeded, signed in.

``[service.lab-xtm-hub]`` in ``.xtm-one/checks.toml`` runs this script (standard library only) on the ``python-dev``
image, so a UI pull request of XTM Hub is reviewed rendered against its own backend, migrations and seeds, not read
as code (an XTM Hub UI round ended BLOCKED for want of a lab before this one):

1. The source is a nested checkout under this checkout, ``xtm-hub/`` by default (``LAB_XTM_HUB_CHECKOUT``):
   materialize_repository(XTM-Hub/xtm-hub, ref=<the PR head>, sub_paths=SUB_PATHS, path="<this checkout>/xtm-hub").
   The start exits 1 naming a missing one.
2. Node 24 (``.nvmrc``) and the lab's yarn (the repository's ``packageManager``) are pinned downloads (``lab.py``);
   ``yarn install`` installs the workspaces.
3. PostgreSQL and S3 are the lab's own (``.xtm-one/stores.py``), with a database ``xtm_hub``. XTM Hub refuses
   OpenSearch (``engine-detection.ts``), so Elasticsearch 8.18.3, the version of ``xtm-hub-dev/docker-compose.yml``,
   runs here from the official image's layers, pulled from Docker Hub by digest (no image key of the lab carries it)
   and started on loopback with security off.
4. The backend (``apps/backend``, tsx) migrates PostgreSQL and Elasticsearch, runs the development seeds (labels,
   documents, the demo organizations Winterfell and Casterly Rock) and creates the platform administrator
   ``lab@filigran.io`` (``ADMIN_*``). The lab then signs it in through the ``login`` mutation and seeds personal API
   tokens in every state (active, last used, expired, revoked) when the API serves them, and registers three
   platforms (OpenCTI EE, OpenAEV CE, XTM One EE) for its organization so the home page's registered-platform
   cards render filled (logged, and read back with ``registeredPlatforms``).
5. The front (``apps/frontend``) runs ``relay-compiler`` once, then ``next dev --turbopack``, the bundler XTM Hub's
   own ``dev`` script uses (webpack refuses the ``export *`` of the design system's client components).
   ``next/font/google`` cannot reach Google Fonts from a lab: ``NEXT_FONT_GOOGLE_MOCKED_RESPONSES`` (the Next.js hook
   its own tests use) names a module whose default export maps each stylesheet URL ``app/font.ts`` makes Turbopack ask
   for (built as Turbopack's ``get_stylesheet_url`` builds it, the variable axes read from Next's ``font-data.json``)
   to ``@font-face`` rules on the real Geologica and IBM Plex Sans files, taken from the Fontsource packages on npm and
   served on loopback (``FONT_PORT``) since Turbopack downloads every ``url()`` of the stylesheet.
6. This script listens on ``PORT``, the preview port: ``/lab/*`` here, the API paths (``/graphql-api``,
   ``/graphql-sse``, ``/auth/``, ``/document/``, ``/user/picture/``) to the backend, everything else to next dev with
   the browser's own Host kept. ``/lab/sign-in`` signs in as the administrator and opens ``/app/profile``;
   ``/lab/sign-in/empty`` signs in as Bran Stark (Winterfell member, no token) for the empty states. Theme:
   ``/lab/theme/dark`` or ``/lab/theme/light`` (next-themes, localStorage); language: ``/lab/locale/en``, ``fr`` or
   ``ja`` (the ``NEXT_LOCALE`` cookie). Each switch opens ``?next=<path>``, ``/app/profile`` by default.
"""

from __future__ import annotations

import functools
import hashlib
import http.client
import http.cookies
import http.server
import io
import json
import os
import re
import secrets
import select
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import filigran_licenses_pr_head as licenses  # noqa: E402 - the PostgreSQL helpers and the uuid-ossp stub
import lab  # noqa: E402 - .xtm-one/lab/lab.py: the pinned downloads, the stores and the process helpers
import opencti_ask_ariane as proxy  # noqa: E402 - the preview Front: websocket tunnel

PORT = 3900
NEXT_PORT = 3901
API_PORT = 3902
ES_PORT = 9204
FONT_PORT = 3903
ES_TRANSPORT_PORT = 9304
DATABASE = "xtm_hub"
#: Never a person's: the lab's database lives and dies with the lab container.
LAB_ADMIN_EMAIL = "lab@filigran.io"
LAB_ADMIN_PASSWORD = "lab-only-password"
#: A member of the seeded Winterfell organization (seed 12-development-demo-organizations.js), given a known password
#: here: the empty states (no API token) render signed in as him.
EMPTY_USER_EMAIL = "Bran.Stark@winterfell.example"
EMPTY_USER_PASSWORD = "lab-only-password-empty"
LANDING = "/app/profile"
LOCALES = ("en", "fr", "ja")
#: The paths the backend serves (apps/backend/src/index.ts and src/server/endpoints); the rest is next dev's.
API_PREFIXES = ("/graphql-api", "/graphql-sse", "/auth/", "/document/", "/user/picture/")
SUB_PATHS = (
    "package.json",
    "yarn.lock",
    ".yarnrc.yml",
    "tsconfig.json",
    "apps/backend",
    "apps/frontend",
)
#: Elasticsearch 8.18.3 (xtm-hub-dev/docker-compose.yml), the linux/amd64 manifest of docker.io/library/elasticsearch.
#: Each layer is checked against the digest the manifest names, the manifest against this one.
ES_REPOSITORY = "library/elasticsearch"
ES_MANIFEST = "sha256:1bb0607912f0abc1bfaf53a479aa89f6f7df491b5b0ad5c6ca22d85873cda50a"
ES_PREFIX = "usr/share/elasticsearch"
#: The faces next/font asks Google Fonts for (apps/frontend/app/font.ts), from Fontsource.
GEOLOGICA = (
    "https://registry.npmjs.org/@fontsource-variable/geologica/-/geologica-5.3.0.tgz",
    "sha512",
    "N4pGIVYOXkyXDMooL/HeNYWbx7aMxRP+xABgkjZwi6bVeM/GM/cqHdO8UNFQiR5QR52yLI1RrzgKxm0DKWO2wQ==",
)
IBM_PLEX_SANS = (
    "https://registry.npmjs.org/@fontsource/ibm-plex-sans/-/ibm-plex-sans-5.3.0.tgz",
    "sha512",
    "CbE4CbbEEZJX860XyUiRpsksXIQR8Rp2XDva2VO53NJox9tVNtusrysd2x5YkUEY3ErQ66W1IiiQL8/wihhw5w==",
)
PLEX_WEIGHTS = ("100", "200", "300", "400", "500", "600", "700")
#: The latin subset of Google Fonts' css2 answers, the one subset font.ts asks for.
LATIN_RANGE = (
    "U+0000-00FF, U+0131, U+0152-0153, U+02BB-02BC, U+02C6, U+02DA, U+02DC, U+0304, U+0308, U+0329, U+2000-206F, "
    "U+20AC, U+2122, U+2191, U+2193, U+2212, U+2215, U+FEFF, U+FFFD"
)
#: Personal API tokens seeded for the administrator: (name, scope, days to expiry, then what the lab does to it).
TOKENS = (
    ("CI pipeline", "read_write", 90, "used"),
    ("Grafana dashboard", "read", 30, ""),
    ("Nightly export", "admin", 365, ""),
    ("Old laptop", "read", 7, "revoked"),
    ("Partner sync", "read", 7, "expired"),
)
#: Platforms registered for the administrator's organization, so the home page renders its registered-platform cards
#: filled: (identifier, platform id, title, contract, version). Fixed ids: a restart refreshes them, never doubles them.
#: The OpenAEV version is under 2.4.0, the first one XTM Hub registers per tenant (required-platform-version.ts).
PLATFORMS = (
    ("opencti", "5ab0c0de-0000-4000-8000-000000000001", "Lab OpenCTI", "EE", "6.9.0"),
    ("openaev", "5ab0c0de-0000-4000-8000-000000000002", "Lab OpenAEV", "CE", "2.3.0"),
    ("xtmone", "5ab0c0de-0000-4000-8000-000000000003", "Lab XTM One", "EE", "1.0.0"),
)
READY: dict[str, bool] = {}

FONT_MOCK_JS = """\
// The lab's stand-in for Google Fonts (NEXT_FONT_GOOGLE_MOCKED_RESPONSES): each css2 URL app/font.ts makes
// Turbopack ask for, answered with the latin faces the lab serves on loopback.
module.exports = __RESPONSES__;
"""
GOOGLE_FONTS_CSS2 = "https://fonts.googleapis.com/css2"


def missing_sub_paths(where: Path) -> list[str]:
    return [sub_path for sub_path in SUB_PATHS if not (where / sub_path).exists()]


def hub_checkout() -> Path:
    where = lab.CHECKOUT / os.environ.get("LAB_XTM_HUB_CHECKOUT", "xtm-hub")
    missing = missing_sub_paths(where)
    if missing:
        raise SystemExit(
            f"xtm-hub: the checkout at {where} lacks {', '.join(missing)}: materialize XTM-Hub/xtm-hub at the PR head "
            f"with sub_paths {list(SUB_PATHS)} under this checkout's {where.name}/ directory, then start again"
        )
    return where


def api_env() -> dict[str, str]:
    """The backend's environment (config/custom-environment-variables.json): the lab's stores, no external service."""
    return {
        "NODE_ENV": "development",
        "LOCAL_DEV": "true",
        "APP_PORT": str(API_PORT),
        "ADMIN_EMAIL": LAB_ADMIN_EMAIL,
        "ADMIN_PASSWORD": LAB_ADMIN_PASSWORD,
        "DATABASE_HOST": "127.0.0.1",
        "DATABASE_PORT": str(lab.stores.PG_PORT),
        "DATABASE_USER": "copilot",
        "DATABASE_PASSWORD": "lab",
        "DATABASE_BASE": DATABASE,
        "MINIO_ENDPOINT": "127.0.0.1",
        "MINIO_PORT": str(lab.stores.S3_PORT),
        "MINIO_ACCESS_KEY": lab.stores.S3_USER,
        "MINIO_SECRET_KEY": lab.stores.S3_PASSWORD,
        "ELASTIC_PROTOCOL": "http",
        "ELASTIC_HOST": "127.0.0.1",
        "ELASTIC_PORT": str(ES_PORT),
        # Not https://dev.hub.staging.filigran.io: index.ts runs the development seeds on any other front URL.
        "BASE_URL_FRONT": f"http://localhost:{PORT}",
        # Lab-only values: the container and its database are thrown away with the lab.
        "PORTAL_COOKIE_NAME": "xtm-hub-lab",
        "PORTAL_COOKIE_SECRET": "lab-only-cookie-secret-never-used-outside-the-lab",
    }


def front_env(mock: Path) -> dict[str, str]:
    """next dev's environment: the browser calls the API through the preview port, the server calls it directly."""
    return {
        "NODE_ENV": "development",
        "NEXT_PUBLIC_CLIENT_HTTP_API": "/",
        "SERVER_HTTP_API": f"http://127.0.0.1:{API_PORT}/",
        "NEXT_FONT_GOOGLE_MOCKED_RESPONSES": str(mock),
        "NEXT_TELEMETRY_DISABLED": "1",
    }


def stores_dest() -> Path:
    return lab.stores.STORES_DIR if lab.stores.installed(lab.stores.STORES_DIR) else lab.CACHE / "stores"


def psql(sql: str, **params: str) -> str:
    """Run *sql* on the lab's ``xtm_hub`` database; each value of *params* is a psql variable, never spliced in."""
    argv = [str(lab.stores.pg_bindir(stores_dest()) / "psql"), "-h", "127.0.0.1", "-p", str(lab.stores.PG_PORT),
            "-U", "copilot", "-d", DATABASE, "-v", "ON_ERROR_STOP=1", "-qAt"]
    for name, value in params.items():
        argv += ["-v", f"{name}={value}"]
    return subprocess.run(argv, input=sql, check=True, capture_output=True, text=True).stdout.strip()


def password_row(password: str, salt: str) -> tuple[str, str]:
    """The (salt, hash) pair apps/backend/src/utils/hash-password.util.ts stores: PBKDF2-SHA512, 1000 rounds, 64 bytes."""
    return salt, hashlib.pbkdf2_hmac("sha512", password.encode(), salt.encode(), 1000, 64).hex()


# region Elasticsearch from the official image's layers


class _DropAuthOnRedirect(urllib.request.HTTPRedirectHandler):
    """Docker Hub answers a blob with a redirect to its CDN, which refuses the registry's bearer token."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None:
            new.remove_header("Authorization")
        return new


def _registry_get(path: str, token: str, accept: str = "") -> urllib.request.Request:
    request = urllib.request.Request(f"https://registry-1.docker.io/v2/{ES_REPOSITORY}/{path}")
    request.add_header("Authorization", f"Bearer {token}")
    if accept:
        request.add_header("Accept", accept)
    return request


def keep_elasticsearch(member: tarfile.TarInfo, dest: str) -> tarfile.TarInfo | None:
    """Only the Elasticsearch home of a layer, never a whiteout, never a member the data filter refuses."""
    name = member.name.lstrip("./")
    if not name.startswith(f"{ES_PREFIX}/") and name != ES_PREFIX:
        return None
    if "/.wh." in f"/{name}":
        return None
    try:
        return tarfile.data_filter(member, dest)
    except tarfile.FilterError:
        return None


def elasticsearch_home() -> Path:
    """The Elasticsearch home of the pinned image, pulled once per lab container."""
    root = lab.CACHE / "elasticsearch"
    home = root / ES_PREFIX
    if (home / "bin" / "elasticsearch").is_file():
        return home
    lab.log(f"elasticsearch: pulling {ES_REPOSITORY}@{ES_MANIFEST[:19]} from Docker Hub (about 690 MB, once per lab)")
    opener = urllib.request.build_opener(_DropAuthOnRedirect)
    scope = urllib.parse.quote(f"repository:{ES_REPOSITORY}:pull", safe="")
    with opener.open(f"https://auth.docker.io/token?service=registry.docker.io&scope={scope}", timeout=60) as answer:
        token = json.load(answer)["token"]
    accept = "application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json"
    with opener.open(_registry_get(f"manifests/{ES_MANIFEST}", token, accept), timeout=60) as answer:
        manifest = answer.read()
    if f"sha256:{hashlib.sha256(manifest).hexdigest()}" != ES_MANIFEST:
        raise SystemExit(f"xtm-hub: the Elasticsearch manifest does not match {ES_MANIFEST}")
    root.mkdir(parents=True, exist_ok=True)
    for layer in json.loads(manifest)["layers"]:
        digest = layer["digest"]
        if not layer.get("mediaType", "").endswith("gzip"):
            raise SystemExit(f"xtm-hub: Elasticsearch layer {digest} is {layer.get('mediaType')}, not gzip")
        sha = hashlib.sha256()
        with tempfile.TemporaryFile(dir=lab.CACHE) as blob:
            with opener.open(_registry_get(f"blobs/{digest}", token), timeout=600) as answer:
                while chunk := answer.read(1 << 20):
                    sha.update(chunk)
                    blob.write(chunk)
            if f"sha256:{sha.hexdigest()}" != digest:
                raise SystemExit(f"xtm-hub: Elasticsearch layer {digest} does not match its digest")
            blob.seek(0)
            with tarfile.open(fileobj=blob, mode="r:gz") as archive:
                archive.extractall(root, filter=keep_elasticsearch)
        lab.log(f"elasticsearch: layer {digest[:19]} ({layer.get('size', 0) // (1 << 20)} MB) unpacked")
    if not (home / "bin" / "elasticsearch").is_file():
        raise SystemExit(f"xtm-hub: no {ES_PREFIX}/bin/elasticsearch in the image {ES_MANIFEST}")
    return home


def elasticsearch_settings(data: Path) -> str:
    """Single node on loopback, no security, no ML, no GeoIP download: a lab store, never reached from outside."""
    return "\n".join((
        "cluster.name: xtm-hub-lab",
        "node.name: xtm-hub-lab",
        "network.host: 127.0.0.1",
        f"http.port: {ES_PORT}",
        f"transport.port: {ES_TRANSPORT_PORT}",
        "discovery.type: single-node",
        f"path.data: {data}",
        f"path.logs: {data.parent / 'es-logs'}",
        "xpack.security.enabled: false",
        "xpack.ml.enabled: false",
        "xpack.watcher.enabled: false",
        "ingest.geoip.downloader.enabled: false",
        "cluster.routing.allocation.disk.threshold_enabled: false",
        "node.store.allow_mmap: false",
        "",
    ))


def start_elasticsearch() -> None:
    home = elasticsearch_home()
    data = lab.ROOT / "es-data"
    data.mkdir(parents=True, exist_ok=True)
    (home / "config" / "elasticsearch.yml").write_text(elasticsearch_settings(data), encoding="utf-8")
    for tool in ("elasticsearch", "elasticsearch-env", "elasticsearch-cli"):
        if (home / "bin" / tool).is_file():
            (home / "bin" / tool).chmod(0o755)
    lab.spawn("xtm-hub-elasticsearch", [str(home / "bin" / "elasticsearch")], cwd=home,
              env={"ES_JAVA_OPTS": "-Xms512m -Xmx512m", "ES_PATH_CONF": str(home / "config")})
    lab.wait_http(f"http://127.0.0.1:{ES_PORT}/_cluster/health?wait_for_status=yellow&timeout=5s", 600,
                  "xtm-hub-elasticsearch")
    release_migration_lock()


def release_migration_lock() -> None:
    """Drop the backend's migration lock index: an API stopped mid-migration (a lab restart) leaves it set and the next
    start fails with 'Migration already on going'. The lab's Elasticsearch is its own; nothing else migrates it."""
    request = urllib.request.Request(f"http://127.0.0.1:{ES_PORT}/migrations_lock", method="DELETE")
    try:
        urllib.request.urlopen(request, timeout=30).close()
        lab.log("xtm-hub: released a stale Elasticsearch migration lock")
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise


# endregion


# region Fonts


def font_faces() -> dict[str, list[dict[str, str]]]:
    """The local woff2 faces per family, unpacked from the pinned Fontsource packages."""
    root = lab.CACHE / "fonts"
    for spec, target in ((GEOLOGICA, "geologica"), (IBM_PLEX_SANS, "ibm-plex-sans")):
        if not (root / target / "package").is_dir():
            with tarfile.open(fileobj=io.BytesIO(lab.fetch(spec)), mode="r:gz") as archive:
                archive.extractall(root / target, filter="data")
    geologica = root / "geologica" / "package" / "files" / "geologica-latin-wght-normal.woff2"
    plex = root / "ibm-plex-sans" / "package" / "files"
    return {
        "Geologica": [{"weight": "100 900", "file": str(geologica)}],
        "IBM Plex Sans": [{"weight": w, "file": str(plex / f"ibm-plex-sans-latin-{w}-normal.woff2")} for w in PLEX_WEIGHTS],
    }


def font_url(file: str) -> str:
    """Where Turbopack downloads a face from: the lab's loopback font server (serve_fonts)."""
    return f"http://127.0.0.1:{FONT_PORT}/{Path(file).relative_to(lab.CACHE / 'fonts').as_posix()}"


def font_calls(font_ts: str) -> list[dict]:
    """The next/font/google calls of app/font.ts: family, fixed weights (none: the variable font) and display."""
    calls = []
    for name, body in re.findall(r"\b([A-Z][A-Za-z0-9_]*)\(\s*\{(.*?)\}\s*\)", font_ts, re.S):
        listed = re.search(r"weight:\s*\[([^\]]*)\]", body)
        single = re.search(r"weight:\s*['\"](\d+)['\"]", body)
        weights = re.findall(r"\d+", listed.group(1)) if listed else [single.group(1)] if single else []
        display = re.search(r"display:\s*['\"](\w+)['\"]", body)
        calls.append({"family": name.replace("_", " "), "weights": sorted(set(weights), key=int),
                      "display": display.group(1) if display else "swap"})
    return calls


def _axis(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


def stylesheet_url(call: dict, font_data: dict) -> str:
    """The css2 URL Turbopack asks for (next-core next_font/google/util.rs get_stylesheet_url, normal style only)."""
    family, display = call["family"].replace(" ", "+"), call["display"]
    if call["weights"]:
        return f"{GOOGLE_FONTS_CSS2}?family={family}:wght@{';'.join(call['weights'])}&display={display}"
    axes = (font_data.get(call["family"]) or {}).get("axes") or []
    wght = next((axis for axis in axes if axis.get("tag") == "wght"), None)
    if wght is None:
        return f"{GOOGLE_FONTS_CSS2}?family={family}&display={display}"
    return f"{GOOGLE_FONTS_CSS2}?family={family}:wght@{_axis(wght['min'])}..{_axis(wght['max'])}&display={display}"


def face_css(family: str, faces: list[dict[str, str]], display: str) -> str:
    return "".join(
        f"/* latin */\n@font-face {{\n  font-family: '{family}';\n  font-style: normal;\n  font-weight: {face['weight']};\n"
        f"  font-display: {display};\n  src: url({font_url(face['file'])}) format('woff2');\n"
        f"  unicode-range: {LATIN_RANGE};\n}}\n"
        for face in faces
    )


def next_font_data(root: Path) -> dict:
    """Next's own Google Fonts catalogue (axes per family), {} when the install does not carry it."""
    for base in (root / "node_modules", root / "apps" / "frontend" / "node_modules"):
        data = base / "next" / "dist" / "compiled" / "@next" / "font" / "dist" / "google" / "font-data.json"
        if data.is_file():
            return json.loads(data.read_text(encoding="utf-8"))
    return {}


def write_font_mock(faces: dict[str, list[dict[str, str]]], calls: list[dict], font_data: dict) -> Path:
    missing = [face["file"] for family in faces.values() for face in family if not Path(face["file"]).is_file()]
    if missing:
        raise SystemExit(f"xtm-hub: the font packages lack {', '.join(missing)}")
    responses = {
        stylesheet_url(call, font_data): face_css(
            call["family"],
            [face for face in faces[call["family"]] if not call["weights"] or face["weight"] in call["weights"]],
            call["display"],
        )
        for call in calls
        if call["family"] in faces
    }
    mock = lab.CACHE / "fonts" / "google-fonts-mock.js"
    mock.parent.mkdir(parents=True, exist_ok=True)
    mock.write_text(FONT_MOCK_JS.replace("__RESPONSES__", json.dumps(responses, indent=2)), encoding="utf-8")
    lab.log(f"font stand-in for {', '.join(responses) or 'no stylesheet'}")
    return mock


class QuietFiles(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A002 - the base signature
        pass


def serve_fonts() -> None:
    """The faces on loopback: Turbopack downloads each url() of a Google Fonts stylesheet."""
    handler = functools.partial(QuietFiles, directory=str(lab.CACHE / "fonts"))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", FONT_PORT), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()


# endregion


# region Seed


def graphql(query: str, variables: dict | None = None, cookie: str = "") -> tuple[dict, list[str]]:
    """One GraphQL call to the backend: (the answer, the Set-Cookie headers)."""
    connection = http.client.HTTPConnection("127.0.0.1", API_PORT, timeout=120)
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if cookie:
        headers["Cookie"] = cookie
    connection.request("POST", "/graphql-api", body=json.dumps({"query": query, "variables": variables or {}}),
                       headers=headers)
    response = connection.getresponse()
    body = json.loads(response.read() or b"{}")
    cookies = [value for key, value in response.getheaders() if key.lower() == "set-cookie"]
    connection.close()
    return body, cookies


LOGIN = "mutation LabLogin($email: String!, $password: String) { login(email: $email, password: $password) { id } }"


def sign_in(email: str, password: str) -> list[str]:
    """The session cookies of *email*, through the login mutation the sign-in form sends."""
    body, cookies = graphql(LOGIN, {"email": email, "password": password})
    if not (body.get("data") or {}).get("login") or not cookies:
        raise RuntimeError(f"login of {email} refused: {json.dumps(body.get('errors'))[:300]}")
    return cookies


def cookie_pairs(cookies: list[str]) -> str:
    return "; ".join(cookie.split(";", 1)[0] for cookie in cookies)


def seed_empty_user() -> None:
    """A known password on the seeded member who holds no token, for the empty states."""
    salt, digest = password_row(EMPTY_USER_PASSWORD, secrets.token_hex(16))
    psql('UPDATE "User" SET salt = :\'salt\', password = :\'hash\' WHERE lower(email) = lower(:\'email\')',
         salt=salt, hash=digest, email=EMPTY_USER_EMAIL)


def seed_tokens() -> list[str]:
    """The administrator's API tokens in every state; skipped (and said) when the API has no apiTokens yet."""
    cookie = cookie_pairs(sign_in(LAB_ADMIN_EMAIL, LAB_ADMIN_PASSWORD))
    body, _ = graphql("query { apiTokens { name } me { organizations { id name personal_space } } }", cookie=cookie)
    if body.get("errors"):
        lab.log(f"xtm-hub: no API token seeded ({json.dumps(body['errors'])[:200]})")
        return []
    existing = {token["name"] for token in body["data"]["apiTokens"]}
    organizations = sorted(body["data"]["me"]["organizations"] or [], key=lambda org: org["personal_space"])
    if not organizations:
        lab.log("xtm-hub: no API token seeded (the administrator belongs to no organization)")
        return []
    seeded = []
    for index, (name, scope, days, then) in enumerate(TOKENS):
        if name in existing:
            continue
        expires = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + days * 86400))
        organization = organizations[index % len(organizations)]
        created, _ = graphql(
            "mutation LabToken($input: CreateApiTokenInput!) { createApiToken(input: $input) { api_token { id } } }",
            {"input": {"name": name, "scope": scope, "organization_id": organization["id"], "expires_at": expires}},
            cookie=cookie,
        )
        if created.get("errors"):
            lab.log(f"xtm-hub: token {name} refused ({json.dumps(created['errors'])[:200]})")
            continue
        if then == "revoked":
            # Set in the database like the other states, so the lab never depends on the revoke path under review.
            psql("UPDATE \"ApiToken\" SET revoked_at = now() - interval '1 day' WHERE name = :'name'", name=name)
        elif then == "expired":
            psql("UPDATE \"ApiToken\" SET expires_at = now() - interval '3 days' WHERE name = :'name'", name=name)
        elif then == "used":
            psql("UPDATE \"ApiToken\" SET last_used_at = now() - interval '2 hours' WHERE name = :'name'", name=name)
        seeded.append(f"{name} ({scope}, {then or 'active'}, {organization['name']})")
    return seeded


REGISTER = (
    "mutation LabRegister($input: RegisterPlatformInput!) { registerPlatform(input: $input) { token } }"
)


def seed_platforms() -> list[str]:
    """The registered platforms of the administrator's personal space, through the registerPlatform mutation.

    The mutation mails the organization's administrators after it commits: a refused mail (no SMTP in the lab) answers
    an error over a registration that exists, so the lab reads registeredPlatforms back instead of trusting the answer.
    """
    cookie = cookie_pairs(sign_in(LAB_ADMIN_EMAIL, LAB_ADMIN_PASSWORD))
    body, _ = graphql("query { me { organizations { id name personal_space } } }", cookie=cookie)
    # The personal space first: the administrator holds ManagePlatformRegistration there whatever the demo seeds say.
    organizations = sorted(((body.get("data") or {}).get("me") or {}).get("organizations") or [],
                           key=lambda org: not org["personal_space"])
    if not organizations:
        lab.log("xtm-hub: no platform registered (the administrator belongs to no organization)")
        return []
    organization = organizations[0]
    for identifier, platform_id, title, contract, version in PLATFORMS:
        answer, _ = graphql(REGISTER, {"input": {
            "organizationId": organization["id"],
            "identifier": identifier,
            "platform": {"id": platform_id, "url": f"http://{identifier}.lab.invalid", "title": title,
                         "contract": contract, "version": version},
        }}, cookie=cookie)
        if answer.get("errors"):
            lab.log(f"xtm-hub: registration of {title} answered {json.dumps(answer['errors'])[:200]}")
    listed, _ = graphql("query { registeredPlatforms(input: {onlyActive: true}) { title contract } }", cookie=cookie)
    if listed.get("errors"):
        lab.log(f"xtm-hub: registered platforms unreadable ({json.dumps(listed['errors'])[:200]})")
        return []
    return [f"{row['title']} ({row['contract']}, {organization['name']})"
            for row in listed["data"]["registeredPlatforms"]]


# endregion


def toolchain() -> tuple[Path, Path, dict[str, str]]:
    node, yarn, env = licenses.toolchain()
    env["YARN_CACHE_FOLDER"] = str(lab.CACHE / "yarn-cache-xtm-hub")
    return node, yarn, env


def upstream_headers(headers, payload: bytes = b"") -> dict[str, str]:
    """The browser's headers, its Host kept: next dev serves its dev resources to the same origin only."""
    out = {k: v for k, v in headers.items() if k.lower() not in ("connection", "content-length")}
    if payload:
        out["Content-Length"] = str(len(payload))
    return out


def is_api_path(path: str) -> bool:
    return path.split("?", 1)[0].startswith(API_PREFIXES)


def next_path(query: str) -> str:
    """The ?next= of a switch, a local path only (never an open redirect), the profile by default."""
    target = urllib.parse.parse_qs(query).get("next", [LANDING])[0]
    return target if target.startswith("/") and not target.startswith("//") else LANDING


def locale_cookie(locale: str) -> str:
    morsel = http.cookies.SimpleCookie()
    morsel["NEXT_LOCALE"] = locale
    morsel["NEXT_LOCALE"]["path"] = "/"
    morsel["NEXT_LOCALE"]["samesite"] = "Lax"
    return morsel["NEXT_LOCALE"].OutputString()


def preview_csp(policy: str) -> str:
    """The app's policy without upgrade-insecure-requests (next.config.mjs sets it outside production): the preview
    is plain http, and the browser would rewrite every chunk, stylesheet and font to an https URL nothing answers."""
    kept = [part.strip() for part in policy.split(";") if part.strip().lower() != "upgrade-insecure-requests"]
    return "; ".join(part for part in kept if part)


def preview_header(key: str, value: str) -> str:
    """An upstream header as the preview relays it: the content security policy through ``preview_csp``."""
    return preview_csp(value) if key.lower() == "content-security-policy" else value


def tunnel_head(command: str, path: str, version: str, headers) -> bytes:
    """The upgrade request next dev gets: Host and Origin on loopback, since next dev refuses its HMR socket to an
    origin outside allowedDevOrigins (the preview's address is not one, and the product's config is not ours)."""
    lines = [f"{command} {path} {version}"]
    lines += [f"{k}: {v}" for k, v in headers.items() if k.lower() not in ("host", "origin")]
    lines += [f"Host: localhost:{NEXT_PORT}", f"Origin: http://localhost:{NEXT_PORT}"]
    return ("\r\n".join(lines) + "\r\n\r\n").encode()


class Front(proxy.Front):
    """The preview port: ``/lab/*`` here, the API paths to the backend, the rest to next dev."""

    def _tunnel(self) -> None:
        """next dev's HMR websocket: the bytes, both ways, the upgrade request presented from loopback."""
        upstream = socket.create_connection(("127.0.0.1", NEXT_PORT), timeout=10)
        upstream.sendall(tunnel_head(self.command, self.path, self.request_version, self.headers))
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

    def _send(self, status: int, body: bytes, ctype: str, cookies: list[str] = (), location: str = "") -> None:
        self.send_response(status)
        for cookie in cookies:
            self.send_header("Set-Cookie", cookie)
        if location:
            self.send_header("Location", location)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _sign_in(self, email: str, password: str) -> None:
        self._body()
        try:
            cookies = sign_in(email, password)
        except (OSError, RuntimeError, ValueError) as error:
            lab.log(f"lab sign-in: {error}")
            self._send(502, b"lab sign-in failed: read the service log", "text/plain")
            return
        self._send(302, b"", "text/plain", cookies, next_path(self.path.partition("?")[2]))

    def _theme(self, theme: str) -> None:
        self._body()
        target = next_path(self.path.partition("?")[2])
        page = (f"<!doctype html><script>localStorage.setItem('theme', {json.dumps(theme)});"
                f"location.replace({json.dumps(target)});</script>").encode()
        self._send(200, page, "text/html; charset=utf-8")

    def _locale(self, locale: str) -> None:
        self._body()
        self._send(302, b"", "text/plain", [locale_cookie(locale)], next_path(self.path.partition("?")[2]))

    def _forward(self, port: int) -> None:
        if "upgrade" in (self.headers.get("Connection") or "").lower():
            self._tunnel()
            return
        payload = self._body()
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=600)
        connection.request(self.command, self.path, body=payload or None, headers=upstream_headers(self.headers,
                                                                                                    payload))
        lab.relay_response(self, connection.getresponse(), preview_header)
        connection.close()

    def _handle(self) -> None:
        path = self.path.split("?", 1)[0]
        try:
            if path == "/lab/ready":
                body = json.dumps({"ready": bool(READY)}).encode()
                self._send(200 if READY else 503, body, "application/json")
            elif path == "/lab/sign-in":
                self._sign_in(LAB_ADMIN_EMAIL, LAB_ADMIN_PASSWORD)
            elif path == "/lab/sign-in/empty":
                self._sign_in(EMPTY_USER_EMAIL, EMPTY_USER_PASSWORD)
            elif path in ("/lab/theme/dark", "/lab/theme/light"):
                self._theme(path.rsplit("/", 1)[-1])
            elif path.startswith("/lab/locale/") and path.rsplit("/", 1)[-1] in LOCALES:
                self._locale(path.rsplit("/", 1)[-1])
            elif is_api_path(path):
                self._forward(API_PORT)
            else:
                self._forward(NEXT_PORT)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_HEAD = _handle


def main() -> None:
    root = hub_checkout()
    node, yarn, env = toolchain()
    lab.start_stores()
    lab.stores.ensure_database(stores_dest(), DATABASE)
    licenses.DATABASE = DATABASE  # the stub writes the catalog row of this lab's database
    licenses.stub_uuid_ossp()
    start_elasticsearch()
    lab.run("yarn", [str(node), str(yarn), "install"], cwd=root, env=env)
    backend, frontend = root / "apps" / "backend", root / "apps" / "frontend"
    lab.spawn("xtm-hub-api", [str(node), str(yarn), "tsx", "src/index.ts"], cwd=backend, env={**env, **api_env()})
    lab.run("relay", [str(node), str(yarn), "relay"], cwd=frontend, env=env)
    mock = write_font_mock(font_faces(), font_calls((frontend / "app" / "font.ts").read_text(encoding="utf-8")),
                           next_font_data(root))
    serve_fonts()
    lab.wait_http(f"http://127.0.0.1:{API_PORT}/health", 900, "xtm-hub-api")
    seed_empty_user()
    seeded = seed_tokens()
    lab.log(f"seeded API tokens for {LAB_ADMIN_EMAIL}: {', '.join(seeded) or 'none'}")
    platforms = seed_platforms()
    lab.log(f"seeded registered platforms for {LAB_ADMIN_EMAIL}: {', '.join(platforms) or 'none'}")
    lab.spawn("xtm-hub-next", [str(node), str(yarn), "next", "dev", "--turbopack", "-p", str(NEXT_PORT), "-H",
                               "127.0.0.1"], cwd=frontend, env={**env, **front_env(mock)})
    lab.wait_http(f"http://127.0.0.1:{NEXT_PORT}/", 900, "xtm-hub-next")
    proxy.VITE_PORT = NEXT_PORT  # the websocket tunnel (next dev's HMR)
    server = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), Front)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    READY["up"] = True
    lab.log(f"seeded signed-in session for {LAB_ADMIN_EMAIL} through /lab/sign-in (login mutation)")
    lab.log(
        f"lab up: http://localhost:{PORT}/lab/sign-in signs in as {LAB_ADMIN_EMAIL} and opens {LANDING} (built from "
        f"{root.name}/); /lab/sign-in/empty as {EMPTY_USER_EMAIL} (no token); theme at /lab/theme/dark or "
        f"/lab/theme/light, language at /lab/locale/en, fr or ja, each with ?next=<path>"
    )
    while True:
        for child in lab.CHILDREN:
            if child.poll() is not None:
                raise SystemExit(f"xtm-hub: {child.args[0]} exited with {child.returncode}")
        time.sleep(2)


if __name__ == "__main__":
    try:
        main()
    finally:
        for child in lab.CHILDREN:
            if child.poll() is None:
                child.terminate()
