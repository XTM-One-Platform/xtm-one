"""The OpenAEV Ask Ariane lab: OpenAEV's own front, signed in, with the Ask Ariane panel answering.

``[service.lab-openaev-ask-ariane]`` in ``.xtm-one/checks.toml`` runs this script (standard library only) on
the ``python-dev`` image. It renders the Ask Ariane panel (``openaev-front/src/admin/components/ariane``) of an
OpenAEV checkout the agent chooses, so a ``@filigran/chatbot`` bump is reviewed in the product that embeds it.
Unlike the OpenCTI lab, no OpenAEV backend runs (it needs Java and four stores no lab image key carries):

1. The OpenAEV source is a nested checkout under this checkout, ``openaev/`` by default
   (``LAB_OPENAEV_CHECKOUT``): materialize_repository(OpenAEV-Platform/openaev, ref=<branch or main>,
   sub_paths=OPENAEV_SUB_PATHS, path="<this checkout>/openaev"). Re-materialize it at another ref and restart
   the service to compare two branches.
2. Node and yarn are the lab's pinned downloads (``lab.NODE``, ``lab.YARN``: yarn 4.18.1 is openaev-front's
   ``packageManager``); ``yarn install`` installs the front, then Vite serves it on ``VITE_PORT``.
3. This script listens on ``PORT``, the preview port. ``/api/xtmone/chat/*`` (the product's Ask Ariane proxy) is
   answered from the OpenCTI lab's fixtures (``opencti_ask_ariane``: agents, three seeded conversations, a long
   Markdown answer streamed in chunks; ``slow`` holds the working state, ``error`` ends on an error frame,
   ``full`` reports a context over 95% of the window). The thinking and done frames carry the context usage, so
   the composer gauge renders with no ``window.fetch`` override. The session calls the shell makes
   (``/api/me``, ``/api/me/tenants``, ``/api/settings``, ``/api/settings/public``, ``/api/tenant-settings``)
   answer a signed-in lab admin on an Enterprise Edition platform connected to XTM One, AI terms accepted;
   every other ``/api`` call answers an empty list (GET), an empty page (POST ``.../search``) or ``{}``, and is
   logged once (``openaev-ask-ariane: unstubbed ...``). Everything else, websockets included, goes to Vite.
4. Theme: open ``/lab/theme/dark`` or ``/lab/theme/light`` (a cookie the lab's ``/api/me`` reads as the user's
   theme), then the page it redirects to renders in that theme.

Pins: an ``openaev-pins.json`` file at this checkout's root (``{"@filigran/chatbot": "3.27.2"}``) or
``LAB_OPENAEV_PINS=@filigran/chatbot@3.27.2``, which wins over the file, rewrites the front's pins before the
install (``openaev-ask-ariane: front pins overridden: ...``); the ``lab up`` line names the chatbot version served.
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
from http.cookies import SimpleCookie
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lab  # noqa: E402 - .xtm-one/lab/lab.py: the pinned toolchain and the process helpers
import opencti_ask_ariane as fixtures  # noqa: E402 - the Ask Ariane fixtures, stream and Vite tunnel

PORT = 3300
VITE_PORT = 3301
#: What the nested OpenAEV checkout must hold: the front alone (its own yarn project and lock).
OPENAEV_SUB_PATHS = ("openaev-front",)
CHAT_PREFIX = "/api/xtmone/chat"
PINS_ENV = "LAB_OPENAEV_PINS"
PINS_FILE = "openaev-pins.json"
#: openaev-front/src/utils/url-helper.ts DEFAULT_TENANT_UUID: the tenant the lab user opens.
TENANT_ID = "2cffad3a-0001-4078-b0e2-ef74274022c3"
USER_ID = "6f7a1b2c-0d3e-4f50-8a61-7b8c9d0e1f20"
THEME_COOKIE = "lab_theme"
#: The unstubbed routes already logged, so a polling page logs each one once.
UNSTUBBED: set[str] = set()


def requested_pins(checkout: Path, env_name: str = PINS_ENV, file_name: str = PINS_FILE) -> dict[str, str]:
    """The front pins to override: ``LAB_OPENAEV_PINS`` (``name@version`` pairs), else ``openaev-pins.json``."""
    text = os.environ.get(env_name, "").strip()
    if text:
        pins = {}
        for pair in filter(None, (part.strip() for part in text.split(","))):
            name, at, version = pair.rpartition("@")
            if at and name:
                pins[name] = version
            else:
                lab.log(f"ask-ariane: {env_name} entry {pair!r} ignored: write name@version")
        return pins
    path = checkout / file_name
    if not path.is_file():
        return {}
    pins = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(pins, dict) or not all(isinstance(v, str) for v in pins.values()):
        raise SystemExit(f"ask-ariane: {path} must be an object of package name to version")
    return pins


def toolchain() -> tuple[Path, Path, dict[str, str]]:
    """The lab's pinned node and yarn, unpacked in the lab cache, and the environment a yarn install runs with."""
    lab.ROOT.mkdir(parents=True, exist_ok=True)
    lab.CACHE.mkdir(exist_ok=True)
    node, yarn = lab.CACHE / "node" / "package" / "bin" / "node", lab.CACHE / "yarn" / "package" / "bin" / "yarn.js"
    for spec, target in ((lab.NODE, "node"), (lab.YARN, "yarn")):
        with tarfile.open(fileobj=io.BytesIO(lab.fetch(spec)), mode="r:gz") as archive:
            archive.extractall(lab.CACHE / target, filter="data")
    node.chmod(0o755)
    env = {
        "YARN_ENABLE_TELEMETRY": "0",
        "YARN_ENABLE_PROGRESS_BARS": "0",
        "YARN_ENABLE_GLOBAL_CACHE": "false",
        "YARN_CACHE_FOLDER": str(lab.CACHE / "yarn-cache"),
        "PATH": f"{node.parent}:{os.environ.get('PATH', '')}",
    }
    return node, yarn, env


def lab_user(theme: str) -> dict:
    """The signed-in lab admin, in *theme*."""
    return {
        "user_id": USER_ID,
        "user_email": "lab@filigran.io",
        "user_firstname": "Lab",
        "user_lastname": "User",
        "user_lang": "en",
        "user_theme": theme,
        "user_admin": True,
        "user_is_only_player": False,
        "user_is_planner": True,
        "user_is_observer": True,
        "user_is_manager": True,
        "user_is_player": True,
        "user_capabilities": [],
        "user_grants": {},
        "user_tenants": [TENANT_ID],
    }


def platform_settings(theme: str, version: str) -> dict:
    """An Enterprise Edition platform connected to XTM One, AI terms accepted (xtmOneAvailability.ts)."""
    return {
        "platform_id": "lab",
        "platform_name": "OpenAEV Lab",
        "platform_theme": theme,
        "platform_lang": "en",
        "platform_version": version,
        "platform_base_url": f"http://localhost:{PORT}",
        "auth_local_enable": True,
        "platform_license": {"license_is_validated": True, "license_is_enterprise": True, "license_type": "lab"},
        "filigran_chatbot_ai_cgu_status": "enabled",
        "platform_xtm_one_configured": True,
        "platform_xtm_one_url": f"http://localhost:{PORT}",
        "default_tenant_id": TENANT_ID,
        "enabled_dev_features": [],
        "platform_dark_theme": {},
        "platform_light_theme": {},
        "platform_banner_by_level": {},
        "expectation_article_expiration_time": 0,
        "expectation_challenge_expiration_time": 0,
        "expectation_detection_expiration_time": 0,
        "expectation_logging_expiration_time": 0,
        "expectation_manual_default_score_value": 0,
        "expectation_manual_expiration_time": 0,
        "expectation_prevention_expiration_time": 0,
        "expectation_vulnerability_expiration_time": 0,
    }


def session_route(method: str, path: str, theme: str, version: str) -> tuple[int, object]:
    """The answer of a non-chat ``/api`` *path*: (status, JSON value)."""
    route = path.split("?", 1)[0].rstrip("/")
    if route.startswith(f"/api/tenants/{TENANT_ID}"):
        route = "/api" + route.removeprefix(f"/api/tenants/{TENANT_ID}")
    if route == "/api/me":
        return 200, lab_user(theme)
    if route == "/api/me/tenants":
        return 200, [{"tenant_id": TENANT_ID, "tenant_name": "Lab"}]
    if route in ("/api/settings", "/api/settings/public"):
        return 200, platform_settings(theme, version)
    if route == "/api/tenant-settings":
        return 200, {"platform_name": "OpenAEV Lab", "platform_lang": "en", "platform_theme": theme}
    if f"{method} {route}" not in UNSTUBBED:
        UNSTUBBED.add(f"{method} {route}")
        lab.log(f"openaev-ask-ariane: unstubbed {method} {route}")
    if method == "GET":
        return 200, []
    if route.endswith("/search"):
        return 200, {"content": [], "totalElements": 0, "numberOfElements": 0, "totalPages": 0, "size": 20, "number": 0}
    return 200, {}


class Front(fixtures.Front):
    """The preview port: the chat proxy and the session calls from fixtures, the rest to Vite."""

    version = ""

    def _theme(self) -> str:
        morsel = SimpleCookie(self.headers.get("Cookie") or "").get(THEME_COOKIE)
        return "light" if morsel is not None and morsel.value == "light" else "dark"

    def _json(self, status: int, value: object, extra: list[tuple[str, str]] | None = None) -> None:
        data = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Set-Cookie", "XSRF-TOKEN=lab-only-csrf; Path=/")
        for key, value_ in extra or []:
            self.send_header(key, value_)
        self.end_headers()
        self.wfile.write(data)

    def _handle(self) -> None:
        path = self.path.split("?", 1)[0]
        try:
            if path in ("/lab/theme/dark", "/lab/theme/light"):
                self._body()
                self.send_response(302)
                self.send_header("Set-Cookie", f"{THEME_COOKIE}={path.rsplit('/', 1)[-1]}; Path=/")
                self.send_header("Location", "/")
                self.send_header("Content-Length", "0")
                self.end_headers()
            elif path.startswith(CHAT_PREFIX + "/"):
                # The OpenCTI fixtures answer the same chatbot routes under its own prefix.
                self.path = "/chatbot" + self.path.removeprefix(CHAT_PREFIX)
                self._chatbot()
            elif path.startswith("/api/") or path in ("/csrf", "/logout"):
                status, value = session_route(self.command, self.path, self._theme(), self.version)
                self._body()
                self._json(status, value)
            else:
                self._forward()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_HEAD = _handle


def openaev_checkout() -> Path:
    where = lab.CHECKOUT / os.environ.get("LAB_OPENAEV_CHECKOUT", "openaev")
    front = where / "openaev-front"
    if not (front / "yarn.lock").is_file() or not (front / "package.json").is_file():
        raise SystemExit(
            f"openaev-ask-ariane: no OpenAEV checkout at {where}: materialize OpenAEV-Platform/openaev at the ref to "
            f"review with sub_paths {list(OPENAEV_SUB_PATHS)} under this checkout's {where.name}/ directory, then start again"
        )
    return front


def main() -> None:
    front = openaev_checkout()
    node, yarn, env = toolchain()
    overridden = fixtures.override_pins(front, requested_pins(lab.CHECKOUT))
    if overridden:
        env["YARN_ENABLE_IMMUTABLE_INSTALLS"] = "false"
        lab.log(f"openaev-ask-ariane: front pins overridden: {', '.join(overridden)}")
    lab.run("yarn", [str(node), str(yarn), "install"], cwd=front, env={**os.environ, **env})
    manifest = json.loads((front / "package.json").read_text(encoding="utf-8"))
    chatbot = manifest["dependencies"].get("@filigran/chatbot")
    Front.version = str(manifest.get("version") or "")
    # The fixtures' Vite tunnel and forward read the module's VITE_PORT at call time.
    fixtures.VITE_PORT = VITE_PORT
    lab.spawn(
        "openaev-front",
        [str(node), "node_modules/vite/bin/vite.js", "--host", "127.0.0.1", "--port", str(VITE_PORT), "--strictPort"],
        cwd=front,
        env=env,
    )
    lab.wait_http(f"http://127.0.0.1:{VITE_PORT}/", 300, "openaev-front")
    server = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), Front)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://localhost:{PORT}"
    for conversation in fixtures.CONVERSATIONS:
        lab.log(f"seeded Ask Ariane conversation: {base}/ (Ask Ariane, history: {conversation['title']})")
    lab.log(
        f"lab up: {base}/, OpenAEV front at @filigran/chatbot {chatbot}, signed in as lab@filigran.io; "
        f"theme at {base}/lab/theme/dark or /lab/theme/light; a message holding 'slow' holds the working state, "
        "'error' ends on an error, 'full' fills the context gauge"
    )
    while True:
        for child in lab.CHILDREN:
            if child.poll() is not None:
                raise SystemExit(f"openaev-ask-ariane: {child.args[0]} exited with {child.returncode}")
        time.sleep(2)


if __name__ == "__main__":
    try:
        main()
    finally:
        for child in lab.CHILDREN:
            if child.poll() is None:
                child.terminate()
