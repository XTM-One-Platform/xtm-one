"""The website lab: FiligranHQ/website (Payload CMS + Next.js) built from the checkout under review, seeded, signed in.

``[service.lab-website]`` in ``.xtm-one/checks.toml`` runs this script (standard library only) on the ``python-dev``
image, so a pull request of the public website is reviewed rendered (pages in EN, /fr and /ja, and the /admin panel)
against its own migrations and its day-1 seed, with no access to the staging or production database or blob store:

1. The source is a nested checkout under this checkout, ``website/`` by default (``LAB_WEBSITE_CHECKOUT``):
   materialize_repository(FiligranHQ/website, ref=<the PR head>, sub_paths=SUB_PATHS, path="<this checkout>/website").
   ``src/migrations`` holds Payload schema snapshots (``*.json``, several hundred MB) the lab never reads: grow
   ``src`` folder by folder with add_paths (every ``src/migrations/*.ts`` but no ``*.json``), and
   ``src/endpoints/seed/assets`` one folder at a time. The start exits 1 naming a missing path.
2. Node 24 (``engines.node``) is the lab's pinned download (``lab.NODE``); pnpm is the pinned standalone binary of the
   release ``packageManager`` names (``PNPM_EXE``).
3. PostgreSQL is the lab's own (``.xtm-one/stores.py``) with a database ``filigran_website``; ``pnpm migrate`` then
   ``pnpm seed`` run on that EMPTY database (no ``SEED_CONFIRM``: the seed refuses a database with pages). Media stay
   local (no ``BLOB_READ_WRITE_TOKEN``), mail is logged (no ``RESEND_API_KEY``), AI is off (no ``ANTHROPIC_API_KEY``).
4. Sign-in: the admin enforces payload-totp. The lab seeds ``lab@filigran.io`` (Site Admin) with a known TOTP secret
   through the Local API, the repository's own e2e pattern (``tests/helpers/seedUser.ts``); no production code path
   changes. ``/lab/sign-in`` posts ``/api/users/login`` then ``/api/verify-totp`` with a code computed here and hands
   both cookies to the browser, then opens ``/admin``.
5. This script listens on ``PORT``, the preview port: ``/lab/*`` here, everything else to ``next dev`` with the
   browser's Host kept; an ``Origin`` header is presented as ``SERVER_URL``, the one origin Payload's csrf list holds.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import http.server
import io
import json
import os
import re
import struct
import sys
import tarfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lab  # noqa: E402 - .xtm-one/lab/lab.py: the pinned downloads, the stores and the process helpers
import opencti_ask_ariane as proxy  # noqa: E402 - the preview Front: websocket tunnel

PORT = 4200
NEXT_PORT = 4201
DATABASE = "filigran_website"
SERVER_URL = f"http://localhost:{PORT}"
#: Never a person's: the lab's database lives and dies with the lab container.
LAB_ADMIN_EMAIL = "lab@filigran.io"
LAB_ADMIN_PASSWORD = "Lab-Admin-1234!"
#: The repository's own e2e secret (tests/helpers/seedUser.ts), base32.
LAB_TOTP_SECRET = "JBSWY3DPEHPK3PXP"
LANDING = "/admin"
#: The pnpm release package.json pins (packageManager pnpm@12.21.0), its standalone linux-x64 binary.
PNPM_EXE = (
    "https://registry.npmjs.org/@pnpm/exe.linux-x64/-/exe.linux-x64-12.21.0.tgz",
    "sha512",
    "8eufCjHXspKbNS/kh8q4Q0gqJoOuvC4Q5pgos6XS8By/nAMtP+jf8F1Cw9er840kfAX+PJ/ts+A9qnLE91rvXQ==",
)
SUB_PATHS = (
    "package.json",
    "pnpm-lock.yaml",
    "pnpm-workspace.yaml",
    "patches",
    "next.config.ts",
    "redirects.ts",
    "tsconfig.json",
    "tailwind.config.mjs",
    "postcss.config.js",
    "components.json",
    "scripts",
    "public",
    "src/payload.config.ts",
    "src/app",
    "src/migrations/index.ts",
    "src/endpoints/seed/index.ts",
    "src/endpoints/seed/assets/shared",
)
READY: dict[str, bool] = {}

#: The lab reviewer, created once through the Local API (overrideAccess), the way tests/helpers/seedUser.ts does.
SEED_USER_TS = """
import { getPayload } from 'payload'
import config from '../src/payload.config'

const payload = await getPayload({ config })
const found = await payload.find({ collection: 'users', where: { email: { equals: process.env.LAB_EMAIL } }, limit: 1 })
if (found.totalDocs === 0) {
  await payload.create({
    collection: 'users',
    data: {
      name: 'Lab reviewer',
      email: process.env.LAB_EMAIL,
      password: process.env.LAB_PASSWORD,
      accessRoles: ['site-admin'],
      totpSecret: process.env.LAB_TOTP,
    } as never,
  })
}
process.exit(0)
"""


def missing_sub_paths(where: Path) -> list[str]:
    return [sub_path for sub_path in SUB_PATHS if not (where / sub_path).exists()]


def website_checkout() -> Path:
    where = lab.CHECKOUT / os.environ.get("LAB_WEBSITE_CHECKOUT", "website")
    missing = missing_sub_paths(where)
    if missing:
        raise SystemExit(
            f"website: the checkout at {where} lacks {', '.join(missing)}: materialize FiligranHQ/website at the PR "
            f"head under this checkout's {where.name}/ directory (sub_paths {list(SUB_PATHS)}, src grown folder by "
            "folder without src/migrations/*.json), then start again"
        )
    return where


def app_env() -> dict[str, str]:
    """The site's environment: the lab's PostgreSQL, local media, no mail, no AI, no Vercel."""
    return {
        "DATABASE_URL": f"postgresql://copilot:lab@127.0.0.1:{lab.stores.PG_PORT}/{DATABASE}",
        # Lab-only values: the container and its database are thrown away with the lab.
        "PAYLOAD_SECRET": "lab-payload-secret-not-for-production-0123456789",
        "PREVIEW_SECRET": "lab-preview-secret",
        "CRON_SECRET": "lab-cron-secret",
        "NEXT_PUBLIC_SERVER_URL": SERVER_URL,
        "NEXT_TELEMETRY_DISABLED": "1",
        "NODE_OPTIONS": "--no-deprecation",
        "CI": "1",
    }


def stores_dest() -> Path:
    return lab.stores.STORES_DIR if lab.stores.installed(lab.stores.STORES_DIR) else lab.CACHE / "stores"


def totp(secret: str, at: float | None = None, digits: int = 6, period: int = 30) -> str:
    """RFC 6238 (SHA1, 6 digits, 30 s), payload-totp's defaults."""
    key = base64.b32decode(secret.upper() + "=" * (-len(secret) % 8))
    counter = int((time.time() if at is None else at) // period)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(code).zfill(digits)


def toolchain() -> tuple[Path, Path, dict[str, str]]:
    lab.ROOT.mkdir(parents=True, exist_ok=True)
    lab.CACHE.mkdir(exist_ok=True)
    node = lab.CACHE / "node" / "package" / "bin" / "node"
    pnpm = lab.CACHE / "pnpm-exe" / "package" / "pnpm"
    for spec, target, done in ((lab.NODE, "node", node), (PNPM_EXE, "pnpm-exe", pnpm)):
        if not done.is_file():
            with tarfile.open(fileobj=io.BytesIO(lab.fetch(spec)), mode="r:gz") as archive:
                archive.extractall(lab.CACHE / target, filter="data")
    node.chmod(0o755)
    pnpm.chmod(0o755)
    env = {
        "PATH": f"{node.parent}:{pnpm.parent}:{os.environ.get('PATH', '')}",
        "npm_config_store_dir": str(lab.CACHE / "pnpm-store-website"),
    }
    return node, pnpm, env


#: An element payload's upload check (uploads/validateSvg.js) refuses in an SVG, with its content.
REFUSED_SVG_ELEMENT = re.compile(r"<(foreignObject|script|iframe|embed|object)\b[^>]*?(/>|>.*?</\1\s*>)", re.I | re.S)


def strip_refused_svg(text: str) -> tuple[str, int]:
    """*text* without the elements Payload refuses on upload, and how many were removed."""
    return REFUSED_SVG_ELEMENT.subn("", text)


def sanitize_seed_svgs(root: Path) -> list[str]:
    """The lab's throwaway copy of the seed's SVG assets made uploadable: Payload refuses an SVG holding a
    foreignObject (or a script, iframe, embed, object), so the seed of a fresh database stops at the first one.
    Each file changed is logged by name; the checkout under review is never committed from here."""
    changed = []
    for svg in sorted((root / "src" / "endpoints" / "seed" / "assets").rglob("*.svg")):
        text = svg.read_text(encoding="utf-8", errors="replace")
        cleaned, removed = strip_refused_svg(text)
        if removed:
            svg.write_text(cleaned, encoding="utf-8")
            changed.append(f"{svg.relative_to(root)} ({removed} element(s))")
    return changed


def seed_user(root: Path, run_env: dict[str, str]) -> None:
    script = root / ".lab" / "seed-user.ts"
    script.parent.mkdir(exist_ok=True)
    script.write_text(SEED_USER_TS, encoding="utf-8")
    lab.run("seed-user", [str(root / "node_modules" / ".bin" / "tsx"), str(script)], cwd=root, env={
        **run_env, "LAB_EMAIL": LAB_ADMIN_EMAIL, "LAB_PASSWORD": LAB_ADMIN_PASSWORD, "LAB_TOTP": LAB_TOTP_SECRET})


def upstream_headers(headers, payload: bytes = b"") -> dict[str, str]:
    """The browser's headers for next dev, its Host kept; an Origin is presented as the site's own (csrf list)."""
    out = {k: v for k, v in headers.items() if k.lower() not in ("connection", "content-length", "origin")}
    if any(k.lower() == "origin" for k in headers.keys()):
        out["Origin"] = SERVER_URL
    if payload:
        out["Content-Length"] = str(len(payload))
    return out


def cookie_pairs(cookies: list[str]) -> str:
    return "; ".join(cookie.split(";", 1)[0] for cookie in cookies)


def _post(path: str, body: dict, cookies: list[str]) -> tuple[int, list[str], bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", NEXT_PORT, timeout=600)
    headers = {"Content-Type": "application/json"}
    if cookies:
        headers["Cookie"] = cookie_pairs(cookies)
    connection.request("POST", path, body=json.dumps(body).encode(), headers=headers)
    response = connection.getresponse()
    data = response.read()
    set_cookies = [value for key, value in response.getheaders() if key.lower() == "set-cookie"]
    connection.close()
    return response.status, set_cookies, data


def sign_in() -> list[str]:
    """Password then the TOTP code: the cookies of a full session (payload-token and the totp cookie)."""
    status, cookies, data = _post("/api/users/login", {"email": LAB_ADMIN_EMAIL, "password": LAB_ADMIN_PASSWORD}, [])
    if status != 200 or not cookies:
        raise RuntimeError(f"login answered {status}: {data[:300]!r}")
    status, more, data = _post("/api/verify-totp", {"token": totp(LAB_TOTP_SECRET)}, cookies)
    if status != 200 or not json.loads(data or b"{}").get("ok"):
        raise RuntimeError(f"verify-totp answered {status}: {data[:300]!r}")
    return cookies + more


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

    def _sign_in(self) -> None:
        self._body()
        try:
            cookies = sign_in()
        except (OSError, RuntimeError, ValueError) as error:
            lab.log(f"lab sign-in: {error}")
            self._send(502, b"lab sign-in failed: read the service log", "text/plain")
            return
        self._send(302, b"", "text/plain", cookies, LANDING)

    def _forward(self) -> None:
        if "upgrade" in (self.headers.get("Connection") or "").lower():
            self._tunnel()
            return
        payload = self._body()
        connection = http.client.HTTPConnection("127.0.0.1", NEXT_PORT, timeout=600)
        connection.request(self.command, self.path, body=payload or None,
                           headers=upstream_headers(self.headers, payload))
        lab.relay_response(self, connection.getresponse())
        connection.close()

    def _handle(self) -> None:
        path = self.path.split("?", 1)[0]
        try:
            if path == "/lab/ready":
                body = json.dumps({"ready": bool(READY)}).encode()
                self._send(200 if READY else 503, body, "application/json")
            elif path == "/lab/sign-in":
                self._sign_in()
            else:
                self._forward()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_HEAD = _handle


def main() -> None:
    root = website_checkout()
    node, pnpm, env = toolchain()
    lab.start_stores()
    lab.stores.ensure_database(stores_dest(), DATABASE)
    run_env = {**env, **app_env()}
    lab.run("pnpm-install", [str(pnpm), "install", "--frozen-lockfile"], cwd=root, env=run_env)
    lab.run("migrate", [str(pnpm), "run", "migrate"], cwd=root, env=run_env)
    for changed in sanitize_seed_svgs(root):
        lab.log(f"seed asset made uploadable for the lab (Payload refuses it as is): {changed}")
    lab.run("seed", [str(pnpm), "run", "seed"], cwd=root, env=run_env)
    seed_user(root, run_env)
    lab.log(f"seeded the day-1 content and {LAB_ADMIN_EMAIL} (Site Admin, TOTP enrolled)")
    lab.spawn("website-next", [str(node), str(root / "node_modules/next/dist/bin/next"), "dev",
                               "-p", str(NEXT_PORT), "-H", "127.0.0.1"], cwd=root, env=run_env)
    lab.wait_http(f"http://127.0.0.1:{NEXT_PORT}/api/users/me", 900, "website-next")
    proxy.VITE_PORT = NEXT_PORT  # the websocket tunnel (next dev's HMR)
    server = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), Front)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    READY["up"] = True
    lab.log(f"seeded signed-in session for {LAB_ADMIN_EMAIL} through /lab/sign-in (password + TOTP)")
    lab.log(
        f"lab up: http://localhost:{PORT}/ (EN), /fr, /ja; /lab/sign-in signs in as {LAB_ADMIN_EMAIL} and opens "
        f"{LANDING} (built from {root.name}/)"
    )
    while True:
        for child in lab.CHILDREN:
            if child.poll() is not None:
                raise SystemExit(f"website: {child.args[0]} exited with {child.returncode}")
        time.sleep(2)


if __name__ == "__main__":
    try:
        main()
    finally:
        for child in lab.CHILDREN:
            if child.poll() is None:
                child.terminate()
