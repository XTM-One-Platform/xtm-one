"""The XTM Hub PR-head lab: [service.lab-xtm-hub].

XTM Hub's backend and front from a nested checkout at the PR head, on the lab's PostgreSQL and S3 with an Elasticsearch
of its own, development-seeded, the administrator signed in through the login mutation. These pin the declaration,
the ports, the environment both apps boot with, the paths the preview sends to the API, the font stand-in and the
pinned downloads.
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import tarfile

import pytest

from tests.test_lab_seed_declarations import CHECKS, LAB, _services

SCRIPT = LAB.parent / "xtm_hub_pr_head.py"

pytestmark = pytest.mark.skipif(not CHECKS.is_file() or not SCRIPT.is_file(), reason="no .xtm-one next to the backend")


def _module():
    spec = importlib.util.spec_from_file_location("lab_xtm_hub_pr_head", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_service_runs_the_script_with_the_stores():
    service = _services()["lab-xtm-hub"]
    module = _module()
    assert service["argv"] == ["python", ".xtm-one/lab/xtm_hub_pr_head.py"]
    assert service["image"] == "python-dev" and not service.get("depends_on")
    assert ".xtm-one/stores.py" in service["needs"] and ".xtm-one/lab" in service["needs"]
    assert service["port"] == module.PORT and service["ready_path"] == "/lab/ready"
    assert service["session_log"] and service["session_log"] in "seeded signed-in session for lab@filigran.io"


def test_the_ports_never_meet_another_lab_or_a_store():
    module = _module()
    ours = {module.PORT, module.NEXT_PORT, module.API_PORT, module.FONT_PORT, module.ES_PORT, module.ES_TRANSPORT_PORT}
    assert len(ours) == 6
    others = {spec["port"] for name, spec in _services().items() if name != "lab-xtm-hub"}
    stores = {module.lab.stores.PG_PORT, module.lab.stores.REDIS_PORT, module.lab.stores.S3_PORT}
    assert not ours & others and not ours & stores


def test_the_api_runs_on_the_lab_stores_with_the_development_seeds():
    module = _module()
    env = module.api_env()
    assert env["NODE_ENV"] == "development" and env["APP_PORT"] == str(module.API_PORT)
    assert env["DATABASE_PORT"] == str(module.lab.stores.PG_PORT) and env["DATABASE_BASE"] == module.DATABASE
    assert env["MINIO_PORT"] == str(module.lab.stores.S3_PORT) and env["MINIO_ACCESS_KEY"] == module.lab.stores.S3_USER
    assert env["ELASTIC_PORT"] == str(module.ES_PORT) and env["ELASTIC_PROTOCOL"] == "http"
    # index.ts seeds only when the front URL is not the staging one.
    assert "dev.hub.staging.filigran.io" not in env["BASE_URL_FRONT"]
    # index.ts refuses to start on a missing or changeMe cookie name or secret.
    assert env["PORTAL_COOKIE_NAME"] not in ("", "changeMe") and env["PORTAL_COOKIE_SECRET"] not in ("", "changeMe")
    assert env["ADMIN_EMAIL"] == module.LAB_ADMIN_EMAIL


def test_the_front_calls_the_api_through_the_preview_and_never_google_fonts(tmp_path):
    module = _module()
    env = module.front_env(tmp_path / "mock.js")
    assert env["NEXT_PUBLIC_CLIENT_HTTP_API"] == "/"
    assert env["SERVER_HTTP_API"] == f"http://127.0.0.1:{module.API_PORT}/"
    assert env["NEXT_FONT_GOOGLE_MOCKED_RESPONSES"] == str(tmp_path / "mock.js")


@pytest.mark.parametrize(
    ("path", "api"),
    [
        ("/graphql-api", True),
        ("/graphql-sse?x=1", True),
        ("/auth/oidc/callback", True),
        ("/document/visualize/a/b.pdf", True),
        ("/user/picture/1", True),
        ("/app/profile", False),
        ("/redirect/news-feed/1", False),
        ("/_next/static/chunk.js", False),
    ],
)
def test_the_preview_sends_the_api_paths_to_the_backend_only(path, api):
    assert _module().is_api_path(path) is api


@pytest.mark.parametrize(
    ("query", "target"),
    [
        ("", "/app/profile"),
        ("next=/app/admin", "/app/admin"),
        ("next=https://evil.example", "/app/profile"),
        ("next=//evil.example", "/app/profile"),
    ],
)
def test_a_switch_opens_a_local_path_only(query, target):
    assert _module().next_path(query) == target


def test_the_preview_keeps_the_csp_but_never_upgrades_to_https():
    policy = "default-src 'self'; img-src 'self' data:; upgrade-insecure-requests; frame-ancestors 'none'"
    assert _module().preview_csp(policy) == "default-src 'self'; img-src 'self' data:; frame-ancestors 'none'"


def test_the_hmr_socket_is_opened_from_loopback():
    module = _module()
    head = module.tunnel_head(
        "GET",
        "/_next/hmr?id=x",
        "HTTP/1.1",
        {"Host": "10.0.0.1:3900", "Origin": "http://10.0.0.1:3900", "Upgrade": "websocket"},
    ).decode()
    assert head.startswith("GET /_next/hmr?id=x HTTP/1.1\r\n") and "Upgrade: websocket" in head
    assert f"Host: localhost:{module.NEXT_PORT}" in head and f"Origin: http://localhost:{module.NEXT_PORT}" in head
    assert "10.0.0.1" not in head and head.endswith("\r\n\r\n")


def test_the_locale_cookie_is_the_one_next_intl_reads():
    cookie = _module().locale_cookie("fr")
    assert cookie.startswith("NEXT_LOCALE=fr") and "Path=/" in cookie


def test_the_password_row_is_the_hub_scheme():
    """apps/backend/src/utils/hash-password.util.ts: pbkdf2Sync(password, salt, 1000, 64, 'sha512') as hex."""
    salt, digest = _module().password_row("secret", "00ff")
    assert salt == "00ff"
    assert digest == hashlib.pbkdf2_hmac("sha512", b"secret", b"00ff", 1000, 64).hex() and len(digest) == 128


def test_the_platforms_register_on_the_personal_space_and_are_read_back(monkeypatch):
    """One registerPlatform per seeded platform, then registeredPlatforms: a mail refusal never hides a registration."""
    module = _module()
    calls: list[tuple[str, dict]] = []

    def graphql(query, variables=None, cookie=""):
        calls.append((query, variables or {}))
        if "organizations" in query:
            orgs = [
                {"id": "o-demo", "name": "Winterfell", "personal_space": False},
                {"id": "o-own", "name": "lab", "personal_space": True},
            ]
            return {"data": {"me": {"organizations": orgs}}}, []
        if "registerPlatform" in query:
            return {"errors": [{"message": "smtp refused"}]}, []
        return {"data": {"registeredPlatforms": [{"title": "Lab OpenCTI", "contract": "EE"}]}}, []

    monkeypatch.setattr(module, "sign_in", lambda email, password: ["s=1; Path=/"])
    monkeypatch.setattr(module, "graphql", graphql)
    monkeypatch.setattr(module.lab, "log", lambda message: None)
    assert module.seed_platforms() == ["Lab OpenCTI (EE, lab)"]
    registered = [variables["input"] for query, variables in calls if "registerPlatform" in query]
    assert [entry["identifier"] for entry in registered] == ["opencti", "openaev", "xtmone"]
    assert {entry["organizationId"] for entry in registered} == {"o-own"}
    assert len({entry["platform"]["id"] for entry in registered}) == len(module.PLATFORMS)
    # OpenAEV 2.4.0 and later registers per tenant: the seed stays under it, with no tenant.
    openaev = next(entry["platform"] for entry in registered if entry["identifier"] == "openaev")
    assert tuple(int(part) for part in openaev["version"].split(".")) < (2, 4, 0) and "tenantId" not in openaev
    assert "registeredPlatforms" in calls[-1][0]


FONT_TS = """import { Geologica, IBM_Plex_Sans } from 'next/font/google';

export const geologica = Geologica({
  subsets: ['latin'],
  display: 'swap',
  variable: '--font-geologica',
  adjustFontFallback: false,
});

export const ibmPlexSans = IBM_Plex_Sans({
  weight: ['100', '200', '300', '400', '500', '600', '700'],
  subsets: ['latin'],
  display: 'swap',
  variable: '--font-ibm-plex-sans',
});
"""
FONT_DATA = {"Geologica": {"axes": [{"tag": "CRSV", "min": 0, "max": 1}, {"tag": "wght", "min": 100, "max": 900}]}}


def test_the_stylesheet_urls_are_the_ones_turbopack_asks_for():
    """next-core next_font/google/util.rs get_stylesheet_url: the variable wght range, else the sorted weights."""
    module = _module()
    urls = [module.stylesheet_url(call, FONT_DATA) for call in module.font_calls(FONT_TS)]
    assert urls == [
        "https://fonts.googleapis.com/css2?family=Geologica:wght@100..900&display=swap",
        "https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@100;200;300;400;500;600;700&display=swap",
    ]


def test_the_font_mock_answers_with_faces_served_on_loopback(tmp_path, monkeypatch):
    module = _module()
    monkeypatch.setattr(module.lab, "CACHE", tmp_path)
    face = tmp_path / "fonts" / "geologica" / "face.woff2"
    face.parent.mkdir(parents=True)
    face.write_bytes(b"wOF2")
    calls = module.font_calls(FONT_TS)
    mock = module.write_font_mock({"Geologica": [{"weight": "100 900", "file": str(face)}]}, calls, FONT_DATA)
    text = mock.read_text(encoding="utf-8")
    assert text.startswith("// ") and "NEXT_FONT_GOOGLE_MOCKED_RESPONSES" in text
    responses = json.loads(text.split("module.exports = ", 1)[1].rstrip().rstrip(";"))
    assert list(responses) == ["https://fonts.googleapis.com/css2?family=Geologica:wght@100..900&display=swap"]
    css = responses["https://fonts.googleapis.com/css2?family=Geologica:wght@100..900&display=swap"]
    assert f"src: url(http://127.0.0.1:{module.FONT_PORT}/geologica/face.woff2)" in css and "/* latin */" in css
    with pytest.raises(SystemExit):
        module.write_font_mock({"Geologica": [{"weight": "400", "file": str(tmp_path / "missing.woff2")}]}, calls, {})


def test_only_the_elasticsearch_home_is_unpacked(tmp_path):
    module = _module()
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name in (
            "usr/share/elasticsearch/bin/elasticsearch",
            "etc/passwd",
            "usr/share/elasticsearch/lib/.wh.old.jar",
            "usr/share/elasticsearchx/evil",
        ):
            info = tarfile.TarInfo(name)
            info.size = 1
            archive.addfile(info, io.BytesIO(b"x"))
    buffer.seek(0)
    with tarfile.open(fileobj=buffer) as archive:
        archive.extractall(tmp_path, filter=module.keep_elasticsearch)  # noqa: S202 - the filter under test
    assert (tmp_path / "usr/share/elasticsearch/bin/elasticsearch").is_file()
    assert not (tmp_path / "etc").exists() and not (tmp_path / "usr/share/elasticsearchx").exists()
    assert not (tmp_path / "usr/share/elasticsearch/lib/.wh.old.jar").exists()


def test_elasticsearch_stays_on_loopback_without_security(tmp_path):
    settings = _module().elasticsearch_settings(tmp_path / "data")
    assert "network.host: 127.0.0.1" in settings and "discovery.type: single-node" in settings
    assert "xpack.security.enabled: false" in settings and "ingest.geoip.downloader.enabled: false" in settings


def test_the_downloads_are_pinned():
    module = _module()
    assert module.ES_MANIFEST.startswith("sha256:") and len(module.ES_MANIFEST) == 71
    for url, algorithm, digest in (module.GEOLOGICA, module.IBM_PLEX_SANS):
        assert url.startswith("https://registry.npmjs.org/") and algorithm == "sha512" and digest


def test_the_start_refuses_a_checkout_missing_a_sub_path(monkeypatch, tmp_path):
    module = _module()
    where = tmp_path / "xtm-hub"
    for sub_path in module.SUB_PATHS:
        target = where / sub_path
        target.parent.mkdir(parents=True, exist_ok=True)
        if "." in sub_path.rsplit("/", 1)[-1]:
            target.write_text("{}", encoding="utf-8")
        else:
            target.mkdir()
    monkeypatch.setattr(module.lab, "CHECKOUT", tmp_path)
    monkeypatch.delenv("LAB_XTM_HUB_CHECKOUT", raising=False)
    assert module.hub_checkout() == where
    (where / "apps" / "frontend").rmdir()
    with pytest.raises(SystemExit) as refused:
        module.hub_checkout()
    assert "lacks apps/frontend" in str(refused.value)


def test_the_declaration_comment_names_every_sub_path():
    module = _module()
    comment = " ".join(CHECKS.read_text(encoding="utf-8").split("[service.lab-xtm-hub]")[0].splitlines()[-9:])
    for sub_path in module.SUB_PATHS:
        assert f'"{sub_path}"' in comment, sub_path
