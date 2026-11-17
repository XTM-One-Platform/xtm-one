"""The Filigran licensing lab: XTM-Internals/filigran-licenses built from the checkout under review, seeded, signed in.

``[service.lab-filigran-licenses]`` in ``.xtm-one/checks.toml`` runs this script (standard library only) on the
``python-dev`` image, so a UI pull request of the licensing application is reviewed rendered against its own
migrations and a seeded customer, not read as code (xtm-one #12370):

1. The source is a nested checkout under this checkout, ``filigran-licenses/`` by default
   (``LAB_FILIGRAN_LICENSES_CHECKOUT``): materialize_repository(XTM-Internals/filigran-licenses, ref=<the PR head>,
   sub_paths=SUB_PATHS, path="<this checkout>/filigran-licenses"). The start exits 1 naming a missing one.
2. Node 24 (``engines.node``) and the lab's yarn are pinned downloads (``lab.py``); ``yarn install`` installs it.
3. PostgreSQL is the lab's own (``.xtm-one/stores.py``) with a database ``licensing``; the application's knex
   migrations run (``scripts/migrations.ts``), then the lab seeds one customer and the "Filigran License" product
   (claim ``filigran xtm``, a throwaway RSA key and CA made here) unless they exist.
4. Sign-in: the application's own development provider (``src/auth.ts``: ``NODE_ENV=development`` and
   ``LOCAL_ADMIN_*``), which every ``next build`` compiles out; nothing here touches Microsoft Entra ID, the app or
   its chart. ``/lab/sign-in`` posts that form server-side and hands the session cookie to the browser, then opens
   the seeded customer.
5. This script listens on ``PORT``, the preview port: ``/lab/*`` here, everything else to ``next dev`` with the
   browser's own Host kept (Auth.js and server actions compare it with the Origin). Theme: ``/lab/theme/dark`` or
   ``/lab/theme/light`` (next-themes keeps it in localStorage), then the seeded customer.
"""

from __future__ import annotations

import http.client
import http.server
import io
import json
import os
import subprocess
import sys
import tarfile
import threading
import time
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lab  # noqa: E402 - .xtm-one/lab/lab.py: the pinned downloads, the stores and the process helpers
import opencti_ask_ariane as proxy  # noqa: E402 - the preview Front: websocket tunnel

PORT = 3800
NEXT_PORT = 3801
DATABASE = "licensing"
#: Never a person's: the lab's database lives and dies with the lab container.
LAB_ADMIN_EMAIL = "lab@filigran.io"
LAB_ADMIN_PASSWORD = "Lab-Admin-1234!"
#: Fixed, so a brief can name /customer/<id> before the lab starts.
CUSTOMER_ID = "0f1a2b3c-4d5e-4f60-8a7b-9c0d1e2f3a4b"
CUSTOMER_NAME = "Lab customer"
PRODUCT_ID = "1e2d3c4b-5a69-4788-9a6b-5c4d3e2f1a0b"
PRODUCT_NAME = "Filigran License"
PRODUCT_CLAIM = "filigran xtm"
SUB_PATHS = (
    "package.json",
    "yarn.lock",
    ".yarnrc.yml",
    "knexfile.ts",
    "next.config.ts",
    "tsconfig.json",
    "postcss.config.js",
    "tailwind.config.ts",
    "public",
    "scripts",
    "src",
    "styles",
)
READY: dict[str, bool] = {}

#: The keys and the CA of the seeded product, made by the checkout's own node-forge (the app's generateCaCert shape).
KEYS_JS = r"""
const { createRequire } = require('module');
const crypto = require('crypto');
const forge = createRequire(process.argv[1] + '/package.json')('node-forge');
const { privateKey } = crypto.generateKeyPairSync('rsa', { modulusLength: 2048 });
const key = forge.pki.privateKeyFromPem(privateKey.export({ type: 'pkcs1', format: 'pem' }));
const pub = forge.pki.rsa.setPublicKey(key.n, key.e);
const cert = forge.pki.createCertificate();
cert.publicKey = pub;
cert.serialNumber = '01' + crypto.randomBytes(15).toString('hex');
cert.validity.notAfter = new Date();
cert.validity.notAfter.setFullYear(cert.validity.notAfter.getFullYear() + 10);
const attrs = [{ name: 'commonName', value: 'Filigran CA CERT (lab)' }, { name: 'organizationName', value: 'Filigran' }];
cert.setSubject(attrs);
cert.setIssuer(attrs);
cert.setExtensions([{ name: 'basicConstraints', cA: true }, { name: 'subjectKeyIdentifier' }]);
cert.sign(key, forge.md.sha256.create());
process.stdout.write(JSON.stringify({
  private_key_pem: forge.pki.privateKeyToPem(key),
  public_key_pem: forge.pki.publicKeyToPem(pub),
  ca_cert_pem: forge.pki.certificateToPem(cert),
}));
"""


def missing_sub_paths(where: Path) -> list[str]:
    return [sub_path for sub_path in SUB_PATHS if not (where / sub_path).exists()]


def licenses_checkout() -> Path:
    where = lab.CHECKOUT / os.environ.get("LAB_FILIGRAN_LICENSES_CHECKOUT", "filigran-licenses")
    missing = missing_sub_paths(where)
    if missing:
        raise SystemExit(
            f"filigran-licenses: the checkout at {where} lacks {', '.join(missing)}: materialize "
            f"XTM-Internals/filigran-licenses at the PR head with sub_paths {list(SUB_PATHS)} under this "
            f"checkout's {where.name}/ directory, then start again"
        )
    return where


def app_env() -> dict[str, str]:
    """The application's environment: the lab's PostgreSQL, the development sign-in, no Entra ID."""
    return {
        "NODE_ENV": "development",
        "DATABASE_HOST": "127.0.0.1",
        "DATABASE_PORT": str(lab.stores.PG_PORT),
        "DATABASE_USER": "copilot",
        "DATABASE_PASSWORD": "lab",
        "DATABASE_ID": DATABASE,
        # Lab-only values: the container and its database are thrown away with the lab.
        "AUTH_SECRET": "lab-auth-secret-not-for-production-0123456789",
        "AUTH_TRUST_HOST": "true",
        "LOCAL_ADMIN_EMAIL": LAB_ADMIN_EMAIL,
        "LOCAL_ADMIN_PASSWORD": LAB_ADMIN_PASSWORD,
        "TRIAL_AUTH_TOKEN": "lab-trial-token",
        "NEXT_TELEMETRY_DISABLED": "1",
    }


def stores_dest() -> Path:
    return lab.stores.STORES_DIR if lab.stores.installed(lab.stores.STORES_DIR) else lab.CACHE / "stores"


def psql(sql: str, **params: str) -> str:
    """Run *sql* through psql; each value of *params* is bound as a psql variable (``:'name'``), never spliced in."""
    argv = [str(lab.stores.pg_bindir(stores_dest()) / "psql"), "-h", "127.0.0.1", "-p", str(lab.stores.PG_PORT),
            "-U", "copilot", "-d", DATABASE, "-v", "ON_ERROR_STOP=1", "-qAt"]
    for name, value in params.items():
        argv += ["-v", f"{name}={value}"]
    return subprocess.run(argv, input=sql, check=True, capture_output=True, text=True).stdout.strip()


def stub_uuid_ossp() -> None:
    """The init migration runs CREATE EXTENSION IF NOT EXISTS "uuid-ossp"; the lab's PostgreSQL has no such module.

    The application never calls its functions (ids come from the uuid package). CREATE EXTENSION IF NOT EXISTS reads
    the catalog before any control file, so a pg_extension row of that name in the lab database (the superuser
    writes it; the image's /opt/stores is read-only) lets the migration run unchanged.
    """
    psql(
        "INSERT INTO pg_extension (oid, extname, extowner, extnamespace, extrelocatable, extversion) "
        "SELECT (SELECT max(oid)::bigint FROM pg_extension) + 100000, 'uuid-ossp', "
        "(SELECT oid FROM pg_roles WHERE rolname = current_user), 'public'::regnamespace, true, 'lab-stub' "
        "WHERE NOT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'uuid-ossp')"
    )
    lab.log("uuid-ossp: catalog stub in the lab database (no function; the app calls none)")


def seed(root: Path, node: Path) -> None:
    """One customer and the Filigran License product, unless they exist."""
    if psql("SELECT count(*) FROM \"Product\" WHERE name = :'name'", name=PRODUCT_NAME) == "0":
        keys = json.loads(subprocess.run([str(node), "-e", KEYS_JS, str(root)], check=True, capture_output=True,
                                         text=True).stdout)
        psql(
            "INSERT INTO \"Product\" (id, name, description, private_key_pem, public_key_pem, ca_cert_pem, "
            "has_quota, has_multi_platform, certificate_product, is_deprecated) VALUES (:'id'::uuid, :'name', "
            ":'description', :'private', :'public', :'ca', false, true, :'claim', false)",
            id=PRODUCT_ID, name=PRODUCT_NAME, description="Lab product (throwaway key)", private=keys["private_key_pem"],
            public=keys["public_key_pem"], ca=keys["ca_cert_pem"], claim=PRODUCT_CLAIM,
        )
    psql(
        "INSERT INTO \"Customer\" (id, name, description, license_name) VALUES (:'id'::uuid, :'name', :'description', "
        ":'license') ON CONFLICT DO NOTHING",
        id=CUSTOMER_ID, name=CUSTOMER_NAME, description="Seeded by the lab", license="Lab Customer",
    )


def toolchain() -> tuple[Path, Path, dict[str, str]]:
    lab.ROOT.mkdir(parents=True, exist_ok=True)
    lab.CACHE.mkdir(exist_ok=True)
    node = lab.CACHE / "node" / "package" / "bin" / "node"
    yarn = lab.CACHE / "yarn" / "package" / "bin" / "yarn.js"
    for spec, target, done in ((lab.NODE, "node", node), (lab.YARN, "yarn", yarn)):
        if not done.is_file():
            with tarfile.open(fileobj=io.BytesIO(lab.fetch(spec)), mode="r:gz") as archive:
                archive.extractall(lab.CACHE / target, filter="data")
    node.chmod(0o755)
    env = {
        **lab.YARN_ENV,
        "YARN_CACHE_FOLDER": str(lab.CACHE / "yarn-cache-licenses"),
        # A throwaway build of a branch head: its lockfile may lag the lab's yarn.
        "YARN_ENABLE_IMMUTABLE_INSTALLS": "false",
        "PATH": f"{node.parent}:{os.environ.get('PATH', '')}",
    }
    return node, yarn, env


def upstream_headers(headers, payload: bytes = b"") -> dict[str, str]:
    """The browser's headers for next dev, its Host kept: Auth.js and server actions compare Host and Origin."""
    out = {k: v for k, v in headers.items() if k.lower() not in ("connection", "content-length")}
    if payload:
        out["Content-Length"] = str(len(payload))
    return out


def set_cookies(response: http.client.HTTPResponse) -> list[str]:
    return [value for key, value in response.getheaders() if key.lower() == "set-cookie"]


def cookie_pairs(cookies: list[str]) -> str:
    return "; ".join(cookie.split(";", 1)[0] for cookie in cookies)


class Front(proxy.Front):
    """The preview port: ``/lab/*`` here, the rest to next dev."""

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

    def _next(self, method: str, path: str, payload: bytes = b"", extra: dict[str, str] | None = None, *,
              own_cookies: bool = True):
        connection = http.client.HTTPConnection("127.0.0.1", NEXT_PORT, timeout=600)
        headers = upstream_headers(self.headers, payload)
        if not own_cookies:
            # A fresh CSRF pair: a browser CSRF cookie would be reused by Auth.js with no Set-Cookie to forward.
            headers = {k: v for k, v in headers.items() if k.lower() != "cookie"}
        headers.update(extra or {})
        connection.request(method, path, body=payload or None, headers=headers)
        return connection, connection.getresponse()

    def _sign_in(self) -> None:
        """The development provider's form, posted here; its session cookie goes to the browser."""
        self._body()
        connection, response = self._next("GET", "/api/auth/csrf", extra={"Accept": "application/json"},
                                          own_cookies=False)
        csrf = json.loads(response.read() or b"{}").get("csrfToken", "")
        cookies = set_cookies(response)
        connection.close()
        form = urllib.parse.urlencode({"csrfToken": csrf, "email": LAB_ADMIN_EMAIL, "password": LAB_ADMIN_PASSWORD,
                                       "callbackUrl": f"/customer/{CUSTOMER_ID}"}).encode()
        connection, response = self._next("POST", "/api/auth/callback/credentials", form, {
            "Content-Type": "application/x-www-form-urlencoded", "Cookie": cookie_pairs(cookies)}, own_cookies=False)
        response.read()
        signed = set_cookies(response)
        connection.close()
        if not any("session-token=" in cookie for cookie in signed):
            lab.log(f"lab sign-in: no session cookie (status {response.status})")
            self._send(502, b"lab sign-in failed: read the service log", "text/plain")
            return
        self._send(302, b"", "text/plain", cookies + signed, f"/customer/{CUSTOMER_ID}")

    def _theme(self, theme: str) -> None:
        self._body()
        page = (f"<!doctype html><script>localStorage.setItem('theme', {json.dumps(theme)});"
                f"location.replace('/customer/{CUSTOMER_ID}');</script>").encode()
        self._send(200, page, "text/html; charset=utf-8")

    def _forward(self) -> None:
        if "upgrade" in (self.headers.get("Connection") or "").lower():
            self._tunnel()
            return
        payload = self._body()
        connection, response = self._next(self.command, self.path, payload)
        self.send_response(response.status, response.reason)
        for key, value in response.getheaders():
            if key.lower() not in ("transfer-encoding", "connection", "content-length"):
                self.send_header(key, value)
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        while True:
            chunk = response.read1(65536)
            if not chunk:
                break
            self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
            self.wfile.flush()
        self.wfile.write(b"0\r\n\r\n")
        connection.close()

    def _handle(self) -> None:
        path = self.path.split("?", 1)[0]
        try:
            if path == "/lab/ready":
                body = json.dumps({"ready": bool(READY)}).encode()
                self._send(200 if READY else 503, body, "application/json")
            elif path == "/lab/sign-in":
                self._sign_in()
            elif path in ("/lab/theme/dark", "/lab/theme/light"):
                self._theme(path.rsplit("/", 1)[-1])
            else:
                self._forward()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_HEAD = _handle


def main() -> None:
    root = licenses_checkout()
    node, yarn, env = toolchain()
    lab.start_stores()
    lab.stores.ensure_database(stores_dest(), DATABASE)
    stub_uuid_ossp()
    run_env = {**env, **app_env()}
    lab.run("yarn", [str(node), str(yarn), "install"], cwd=root, env=run_env)
    lab.run("migrations", [str(node), str(root / "node_modules/tsx/dist/cli.mjs"), "scripts/migrations.ts"], cwd=root,
            env=run_env)
    seed(root, node)
    lab.log(f"seeded customer {CUSTOMER_NAME} ({CUSTOMER_ID}) and product {PRODUCT_NAME} (claim {PRODUCT_CLAIM})")
    lab.spawn("licenses-next", [str(node), str(root / "node_modules/next/dist/bin/next"), "dev", "--turbopack",
                                "-p", str(NEXT_PORT), "-H", "127.0.0.1"], cwd=root, env=run_env)
    lab.wait_http(f"http://127.0.0.1:{NEXT_PORT}/api/health", 600, "licenses-next")
    proxy.VITE_PORT = NEXT_PORT  # the websocket tunnel (next dev's HMR)
    server = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), Front)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    READY["up"] = True
    lab.log(f"seeded signed-in session for {LAB_ADMIN_EMAIL} through /lab/sign-in (development provider)")
    lab.log(
        f"lab up: http://localhost:{PORT}/lab/sign-in signs in as {LAB_ADMIN_EMAIL} and opens /customer/{CUSTOMER_ID} "
        f"(built from {root.name}/); theme at /lab/theme/dark or /lab/theme/light"
    )
    while True:
        for child in lab.CHILDREN:
            if child.poll() is not None:
                raise SystemExit(f"filigran-licenses: {child.args[0]} exited with {child.returncode}")
        time.sleep(2)


if __name__ == "__main__":
    try:
        main()
    finally:
        for child in lab.CHILDREN:
            if child.poll() is None:
                child.terminate()
