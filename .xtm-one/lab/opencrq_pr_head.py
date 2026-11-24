"""The OpenCRQ PR-head lab: OpenCRQ's back and front built from the checkout under review, seeded, signed in.

``[service.lab-opencrq]`` in ``.xtm-one/checks.toml`` runs this script (standard library only) on the
``python-dev`` image, so a UI pull request of OpenCRQ is reviewed rendered against its own backend, its own
migrations and seeded data, not a stub (xtm-one #11799, item 5):

1. The OpenCRQ source is a nested checkout under this checkout, ``opencrq/`` by default
   (``LAB_OPENCRQ_CHECKOUT``): materialize_repository(OpenCRQ-Platform/opencrq, ref=<the PR head>,
   sub_paths=OPENCRQ_SUB_PATHS, path="<this checkout>/opencrq"). The start exits 1 naming a missing one.
2. Node 26 (the root ``engines.node``) and yarn 4.18.1 are pinned downloads; ``yarn install`` installs the
   workspaces, ``tsx esbuild.ts --development`` builds the backend.
3. PostgreSQL is the lab's own (``.xtm-one/stores.py``, from the image's ``/opt/stores`` when it has them) with
   a database ``opencrq``. ``back/config/development.local.json`` is written for it: the backend runs its
   migrations, then the product's platform seed (``platform_seed``: tenants, users, assets, threats, risk
   scenarios, CRQ simulations with their loss estimates, treatment plans), as the product's own local stack does.
4. OpenCRQ signs people in through XTM One only; the lab inserts a session for the seeded administrator
   (``LAB_ADMIN_EMAIL``) as the product's integration tests do (``integration-tests/src/helpers/sign-in.ts``) and
   ``/lab/sign-in`` sets its cookie (named after the backend port in development, ``trpc.ts``), then opens ``/``.
5. This script listens on ``PORT``, the preview port: ``/lab/*`` here, everything else (websockets included) to
   Vite, which proxies ``/trpc`` and ``/api`` to the backend. Theme: ``/lab/theme/dark`` or
   ``/lab/theme/light`` sets the admin's theme in the database. Language: ``/lab/locale/en`` or ``/lab/locale/fr``
   sets the admin's ``users.locale`` (what ``getUserLocale`` answers and the front applies on load);
   ``/lab/locale/default`` clears it (the tenant's language, then the browser's). Every ``/lab/`` switch then opens
   ``?next=<path>`` (a local path), else ``/``.
6. Without XTM One the backend runs in Community Edition, where a user reaches one tenant: the first of their
   memberships by id (``visibleTenants``, ``back/src/features/app-router/tenant-edition-utils.ts``), Acme Bank
   for the seeded administrator. ``/lab/tenant/<name>`` (case-insensitive, e.g. ``Tarnwick Industries``, the
   seeded tenant with no asset groups, or ``Fresh Start``, born as a tenant created from the platform administration:
   its ``Enterprise`` risk scope and its Get started wizard, nothing else) makes that tenant the one shown
   (:func:`select_tenant`), then opens ``?next=<path>``; an unknown name answers 404 with the seeded names.
7. Feature flags (``feature_flags``, the closed registry of ``utils/src/feature-flags.ts``): every flag on by
   default (``*``), so a screen behind a development flag renders; ``LAB_OPENCRQ_FEATURE_FLAGS`` narrows them
   (comma-separated names, ``none`` for none). The lab-up line names them.
8. OpenCTI: off by default (no ``connectors.opencti``: every OpenCTI surface reads unavailable). ``LAB_OPENCRQ_OPENCTI=live``
   (``[service.lab-opencrq-opencti]``, which starts ``lab-opencti`` first) connects the Fresh Start tenant to the
   lab's OpenCTI (:func:`opencti_connectors`): its Get started Threats step and Integrations > OpenCTI then talk to
   a running OpenCTI.
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
import urllib.parse
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lab  # noqa: E402 - .xtm-one/lab/lab.py: the pinned downloads, the stores and the process helpers
import opencti_ask_ariane as proxy  # noqa: E402 - the preview Front: forward and websocket tunnel to Vite

PORT = 3600
VITE_PORT = 3601
BACKEND_PORT = 3602
HEALTH_PORT = 3603
DATABASE = "opencrq"
LAB_ADMIN_EMAIL = "lab@filigran.io"
#: Never a person's: the lab's database lives and dies with the lab container.
LAB_ADMIN_PASSWORD = "Lab-Admin-1234!"
SESSION_DAYS = 7
#: ``module/src/modules/users/validation.ts`` supportedLocaleSchema; ``default`` clears the user's choice.
LOCALES = ("en", "fr")
#: ``FEATURE_FLAG_ALL`` of ``utils/src/feature-flags.ts``: every flag of the registry at once.
ALL_FEATURE_FLAGS = "*"
#: The root yarn project, the root TypeScript configs and every workspace the backend and front build with.
OPENCRQ_SUB_PATHS = (
    "package.json",
    "yarn.lock",
    ".yarnrc.yml",
    "tsconfig.base.json",
    "tsconfig.json",
    "front",
    "back",
    "module",
    "utils",
    "eslint-rules",
    "integration-tests/package.json",
)
#: OpenCRQ's root ``engines.node`` is ``>=26.0.0``; the lab's shared Node is 24.
NODE26 = (
    "https://registry.npmjs.org/node-linux-x64/-/node-linux-x64-26.0.0.tgz",
    "sha512",
    "shErYZlvlAWQ1bOJEJyCPnF035NiPBnItTbUQZnn87dO0NKZ/o3tQ7ALrlRDdfUsfIozu8BD5l2fk+ma4ho4NA==",
)
SESSION: dict[str, str] = {}
#: ``Tenant_FreshStart`` of ``module/src/platform-seed/data/ids.ts``: the seeded tenant with the Get started wizard.
FRESH_START_TENANT = "c3e1f5a7-2d84-4b96-9f1c-6a0e8d2b4f57"


def missing_sub_paths(where: Path) -> list[str]:
    return [sub_path for sub_path in OPENCRQ_SUB_PATHS if not (where / sub_path).exists()]


def opencrq_checkout() -> Path:
    where = lab.CHECKOUT / os.environ.get("LAB_OPENCRQ_CHECKOUT", "opencrq")
    missing = missing_sub_paths(where)
    if missing:
        raise SystemExit(
            f"opencrq: the OpenCRQ checkout at {where} lacks {', '.join(missing)}: materialize "
            f"OpenCRQ-Platform/opencrq at the PR head with sub_paths {list(OPENCRQ_SUB_PATHS)} under this "
            f"checkout's {where.name}/ directory, then start again"
        )
    return where


def session_cookie_name(port: int = BACKEND_PORT) -> str:
    """``back/src/trpc.ts``: outside production the session cookie carries the backend port."""
    return f"app::auth_session::{port}"


def feature_flags(value: str | None = None) -> list[str]:
    """The ``feature_flags`` the backend boots with: ``LAB_OPENCRQ_FEATURE_FLAGS`` or every flag.

    The backend refuses an unknown name at boot (``config-schema.ts``), so a typo shows in its log at once.
    """
    raw = os.environ.get("LAB_OPENCRQ_FEATURE_FLAGS", ALL_FEATURE_FLAGS) if value is None else value
    if raw.strip().lower() == "none":
        return []
    return [name.strip() for name in raw.split(",") if name.strip()] or [ALL_FEATURE_FLAGS]


def next_path(query: str) -> str:
    """The ``next`` of a ``/lab/`` switch: a local path only (never ``//host``), else ``/``."""
    wanted = urllib.parse.parse_qs(query).get("next", ["/"])[0]
    return wanted if wanted.startswith("/") and not wanted.startswith("//") and "\\" not in wanted else "/"


def opencti_connectors(value: str | None = None) -> dict:
    """The OpenCTI part of the config: empty unless ``LAB_OPENCRQ_OPENCTI`` is ``live``.

    ``live`` points the Fresh Start tenant at the lab's OpenCTI (``[service.lab-opencti]``, its lab-only admin
    token), per tenant as ``config-schema.ts`` keys it, and names it in the product switcher (``xtm.opencti_url``).
    """
    raw = os.environ.get("LAB_OPENCRQ_OPENCTI", "") if value is None else value
    if raw.strip().lower() != "live":
        return {}
    return {
        "connectors": {"opencti": {FRESH_START_TENANT: {"url": lab.LIVE_OPENCTI_URL, "token": lab.CTEM_OPENCTI_TOKEN}}},
        "xtm": {"opencti_url": lab.LIVE_OPENCTI_URL},
    }


def local_config(admin_email: str = LAB_ADMIN_EMAIL) -> dict:
    """``back/config/development.local.json`` for the lab's PostgreSQL and ports, the platform seed on."""
    return {
        "postgres": {
            "database": DATABASE,
            "user": "copilot",
            "host": "127.0.0.1",
            "port": lab.stores.PG_PORT,
            "password": "lab",
        },
        # development.json points at its own MinIO; the lab's S3 (stores.py) with a bucket of its own instead.
        "file_storage": {
            "endpoint": f"http://127.0.0.1:{lab.stores.S3_PORT}",
            "access_key": lab.stores.S3_USER,
            "secret_key": lab.stores.S3_PASSWORD,
            "bucket": "opencrq",
            "region": "us-east-1",
        },
        "http_server": {"host": "127.0.0.1", "port": BACKEND_PORT},
        "healthcheck_http_server": {"port": HEALTH_PORT},
        "dev_server": {"port": VITE_PORT},
        "platform_seed": {"enabled": True, "admin": {"email": admin_email, "password": LAB_ADMIN_PASSWORD}},
        "feature_flags": feature_flags(),
        # Required by the config parse; nothing answers there, so the Ask Ariane launcher reads unavailable.
        "xtm_one": {"url": "http://127.0.0.1:9", "token": "lab", "platform_url": f"http://localhost:{PORT}"},
        **opencti_connectors(),
    }


def toolchain() -> tuple[Path, Path, dict[str, str]]:
    lab.ROOT.mkdir(parents=True, exist_ok=True)
    lab.CACHE.mkdir(exist_ok=True)
    node = lab.CACHE / "node26" / "package" / "bin" / "node"
    yarn = lab.CACHE / "yarn" / "package" / "bin" / "yarn.js"
    for spec, target, done in ((NODE26, "node26", node), (lab.YARN, "yarn", yarn)):
        if not done.is_file():
            with tarfile.open(fileobj=io.BytesIO(lab.fetch(spec)), mode="r:gz") as archive:
                archive.extractall(lab.CACHE / target, filter="data")
    node.chmod(0o755)
    env = {
        **lab.YARN_ENV,
        "YARN_CACHE_FOLDER": str(lab.CACHE / "yarn-cache"),
        "PATH": f"{node.parent}:{os.environ.get('PATH', '')}",
    }
    return node, yarn, env


def psql(sql: str, database: str = DATABASE, **params: str) -> str:
    """Run *sql* through psql; each value of *params* is bound as a psql variable (``:'name'``), never spliced in.

    The statement goes on stdin: psql interpolates variables in a script, not in a ``-c`` command.
    """
    dest = lab.stores.STORES_DIR if lab.stores.installed(lab.stores.STORES_DIR) else lab.CACHE / "stores"
    argv = [str(lab.stores.pg_bindir(dest) / "psql"), "-h", "127.0.0.1", "-p", str(lab.stores.PG_PORT),
            "-U", "copilot", "-d", database, "-v", "ON_ERROR_STOP=1", "-qAt"]
    for name, value in params.items():
        argv += ["-v", f"{name}={value}"]
    return subprocess.run(argv, input=sql, check=True, capture_output=True, text=True).stdout.strip()


def seed_session(admin_email: str = LAB_ADMIN_EMAIL) -> str:
    """A session of the seeded administrator, as ``integration-tests/src/helpers/sign-in.ts`` creates one."""
    deadline = time.monotonic() + 600
    while True:
        user = psql("SELECT id FROM users WHERE email = :'email'", email=admin_email) if seeded() else ""
        if user:
            break
        if time.monotonic() > deadline:
            raise SystemExit(f"opencrq: no seeded user {admin_email} within 600s: read the backend log for the seed")
        time.sleep(3)
    session = str(uuid.uuid4())
    psql(
        "INSERT INTO sessions (id, user_id, expires_at) VALUES (:'session'::uuid, :'user'::uuid, now() + make_interval(days => :'days'::int))",
        session=session, user=user, days=str(SESSION_DAYS),
    )
    SESSION.update(id=session, user=user)
    return session


def select_tenant(name: str, user: str) -> str | None:
    """Make the tenant named *name* the one Community Edition shows *user*; ``None`` when no tenant has that name.

    Community Edition keeps a user's first membership by tenant id (UUID order is the string order
    ``communityEditionTenantId`` sorts by), so the user joins the tenant and leaves every tenant whose id sorts
    before it. Their group memberships stay, so going back with the tenant's own name restores the same rights.
    """
    tenant = psql("SELECT id FROM tenants WHERE lower(name) = lower(:'name') ORDER BY id LIMIT 1", name=name)
    if not tenant:
        return None
    psql(
        "INSERT INTO tenant_users (tenant_id, user_id) VALUES (:'tenant'::uuid, :'user'::uuid) ON CONFLICT DO NOTHING;\n"
        "DELETE FROM tenant_users WHERE user_id = :'user'::uuid AND tenant_id < :'tenant'::uuid",
        tenant=tenant, user=user,
    )
    return tenant


def set_locale(locale: str, user: str) -> bool:
    """Write *user*'s language (``en``, ``fr``; ``default`` clears it); ``False`` for any other value."""
    if locale == "default":
        psql("UPDATE users SET locale = NULL WHERE id = :'user'::uuid", user=user)
        return True
    if locale not in LOCALES:
        return False
    psql("UPDATE users SET locale = :'locale' WHERE id = :'user'::uuid", locale=locale, user=user)
    return True


def tenant_names() -> str:
    return psql("SELECT coalesce(string_agg(name, ', ' ORDER BY id), '') FROM tenants")


def seeded() -> bool:
    try:
        return psql("SELECT to_regclass('public.sessions') IS NOT NULL") == "t"
    except subprocess.CalledProcessError:
        return False


class Front(proxy.Front):
    """The preview port: ``/lab/*`` here, the rest to Vite."""

    def _redirect(self, cookie: str | None = None) -> None:
        self._body()
        self.send_response(302)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Location", next_path(urllib.parse.urlsplit(self.path).query))
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _not_found(self, message: str) -> None:
        self._body()
        data = message.encode()
        self.send_response(404)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _handle(self) -> None:
        path = self.path.split("?", 1)[0]
        try:
            if path == "/lab/ready":
                data = json.dumps({"ready": bool(SESSION)}).encode()
                self.send_response(200 if SESSION else 503)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            elif path == "/lab/sign-in":
                self._redirect(f"{session_cookie_name()}={SESSION.get('id', '')}; Path=/; HttpOnly; SameSite=Lax")
            elif path in ("/lab/theme/dark", "/lab/theme/light"):
                psql("UPDATE users SET theme = :'theme' WHERE id = :'user'::uuid", theme=path.rsplit("/", 1)[-1], user=SESSION.get("user", ""))
                self._redirect()
            elif path.startswith("/lab/locale/"):
                locale = path[len("/lab/locale/"):].strip("/").lower()
                if set_locale(locale, SESSION.get("user", "")):
                    self._redirect()
                else:
                    self._not_found(f"no language {locale!r}; use one of {', '.join(LOCALES)} or default\n")
            elif path.startswith("/lab/tenant/"):
                name = urllib.parse.unquote(path[len("/lab/tenant/"):]).strip()
                if select_tenant(name, SESSION.get("user", "")) is None:
                    self._not_found(f"no seeded tenant named {name!r}; seeded tenants: {tenant_names()}\n")
                else:
                    self._redirect()
            else:
                self._forward()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_HEAD = _handle


def main() -> None:
    root = opencrq_checkout()
    back, front = root / "back", root / "front"
    node, yarn, env = toolchain()
    lab.start_stores()
    # A restart in place (sync with restart, #12198) finds the database of the previous run: kept.
    dest = lab.stores.STORES_DIR if lab.stores.installed(lab.stores.STORES_DIR) else lab.CACHE / "stores"
    lab.stores.ensure_database(dest, DATABASE)
    (back / "config" / "development.local.json").write_text(json.dumps(local_config(), indent=2) + "\n", encoding="utf-8")
    run_env = {**env, "NODE_ENV": "development"}
    lab.run("yarn", [str(node), str(yarn), "install"], cwd=root, env=run_env)
    lab.run("back-build", [str(node), str(yarn), "tsx", "esbuild.ts", "--development"], cwd=back, env=run_env)
    lab.spawn("opencrq-back", [str(node), "--enable-source-maps", "dist/index-pm.mjs"], cwd=back,
              env={**run_env, "INIT_CWD": str(back)})
    seed_session()
    lab.log(f"seeded signed-in session for {LAB_ADMIN_EMAIL} (platform seed: risk scenarios, CRQ simulations)")
    proxy.VITE_PORT = VITE_PORT
    vite = next((p for p in (front / "node_modules/vite/bin/vite.js", root / "node_modules/vite/bin/vite.js") if p.is_file()), None)
    if vite is None:
        raise SystemExit("opencrq: vite is not installed under front/ or the root node_modules")
    lab.spawn("opencrq-front", [str(node), str(vite), "--host", "127.0.0.1", "--port", str(VITE_PORT), "--strictPort"],
              cwd=front, env={**run_env, "PROXY_TARGET": f"http://127.0.0.1:{BACKEND_PORT}"})
    lab.wait_http(f"http://127.0.0.1:{VITE_PORT}/", 300, "opencrq-front")
    server = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), Front)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    lab.log(
        f"lab up: http://localhost:{PORT}/lab/sign-in signs in as {LAB_ADMIN_EMAIL} (OpenCRQ built from {root.name}/, "
        f"seeded); theme at /lab/theme/dark or /lab/theme/light; language at /lab/locale/<{'|'.join(LOCALES)}|default>; "
        f"tenant at /lab/tenant/<name> ({tenant_names()}); ?next=<path> after a switch; "
        f"feature flags: {', '.join(feature_flags()) or 'none'}; "
        f"opencti: {'lab OpenCTI on the Fresh Start tenant' if opencti_connectors() else 'off'}"
    )
    while True:
        for child in lab.CHILDREN:
            if child.poll() is not None:
                raise SystemExit(f"opencrq: {child.args[0]} exited with {child.returncode}")
        time.sleep(2)


if __name__ == "__main__":
    try:
        main()
    finally:
        for child in lab.CHILDREN:
            if child.poll() is None:
                child.terminate()
