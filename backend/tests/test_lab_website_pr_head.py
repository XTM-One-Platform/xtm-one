"""The website PR-head lab: [service.lab-website].

FiligranHQ/website (Payload CMS + Next.js) from a nested checkout at the PR head, migrated and day-1 seeded on the
lab's PostgreSQL, the reviewer signed in past payload-totp by the lab. These pin the declaration, the ports, the
environment the site boots with (no staging or production store), the TOTP code and the headers the preview forwards.
"""

from __future__ import annotations

import base64
import importlib.util

import pytest

from tests.test_lab_seed_declarations import CHECKS, LAB, _services

SCRIPT = LAB.parent / "website_pr_head.py"

pytestmark = pytest.mark.skipif(not CHECKS.is_file() or not SCRIPT.is_file(), reason="no .xtm-one next to the backend")


def _module():
    spec = importlib.util.spec_from_file_location("lab_website_pr_head", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_service_runs_the_script_with_the_stores():
    service = _services()["lab-website"]
    module = _module()
    assert service["argv"] == ["python", ".xtm-one/lab/website_pr_head.py"]
    assert service["image"] == "python-dev" and not service.get("depends_on")
    assert ".xtm-one/stores.py" in service["needs"]
    assert service["port"] == module.PORT and service["ready_path"] == "/lab/ready"
    assert service["session_log"] and service["session_log"] in "seeded signed-in session for lab@filigran.io"


def test_the_ports_never_meet_another_lab():
    module = _module()
    ours = {module.PORT, module.NEXT_PORT}
    assert len(ours) == 2
    others = {spec["port"] for name, spec in _services().items() if name != "lab-website"}
    assert not ours & others


def test_the_site_uses_the_lab_postgres_and_no_remote_store():
    module = _module()
    env = module.app_env()
    assert env["DATABASE_URL"].startswith(f"postgresql://copilot:lab@127.0.0.1:{module.lab.stores.PG_PORT}/")
    assert env["DATABASE_URL"].endswith("/" + module.DATABASE)
    for remote in ("BLOB_READ_WRITE_TOKEN", "USE_VERCEL_BLOB", "RESEND_API_KEY", "ANTHROPIC_API_KEY",
                   "PAYLOAD_DATABASE_URL", "SEED_CONFIRM", "VERCEL"):
        assert remote not in env, remote
    assert env["NEXT_PUBLIC_SERVER_URL"] == module.SERVER_URL


def test_the_totp_code_is_rfc_6238():
    """RFC 6238 appendix B, SHA1, truncated to 6 digits (payload-totp's defaults)."""
    module = _module()
    rfc_key = base64.b32encode(b"12345678901234567890").decode()  # the RFC's published test key
    assert module.totp(rfc_key, at=59) == "287082"
    assert module.totp(rfc_key, at=1111111109) == "081804"
    assert len(module.totp(module.LAB_TOTP_SECRET)) == 6


def test_the_forward_uses_next_host_and_presents_the_site_origin():
    module = _module()
    headers = module.upstream_headers(
        {"Host": "preview.example", "Origin": "http://preview.example", "Connection": "keep-alive",
         "Content-Length": "9"}, b"ab")
    assert headers["Host"] == f"localhost:{module.NEXT_PORT}" and headers["Origin"] == module.SERVER_URL
    assert "Connection" not in headers and headers["Content-Length"] == "2"
    # A page load on the plain-http preview carries no Origin: the site's own is presented anyway, else Payload
    # ignores the payload-token cookie and /admin renders signed out.
    assert module.upstream_headers({"Host": "preview.example"})["Origin"] == module.SERVER_URL


def test_next_dev_is_not_bound_to_an_address_that_turns_the_en_rewrite_external():
    source = SCRIPT.read_text()
    assert '"-H", "127.0.0.1"' not in source


def test_a_redirect_next_builds_goes_to_the_browser_origin():
    module = _module()
    browser = "http://preview.example:4200"
    assert module.browser_location("Location", f"{module.NEXT_ORIGIN}/fr?x=1", browser) == f"{browser}/fr?x=1"
    assert module.browser_location("Location", "https://filigran.io/", browser) == "https://filigran.io/"
    assert module.browser_location("Content-Type", module.NEXT_ORIGIN, browser) == module.NEXT_ORIGIN


def test_a_seed_svg_loses_only_what_payload_refuses(tmp_path):
    module = _module()
    assets = tmp_path / "src" / "endpoints" / "seed" / "assets" / "shared"
    assets.mkdir(parents=True)
    (assets / "blur.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg"><foreignObject x="1"><div xmlns="http://www.w3.org/1999/xhtml">'
        '</div></foreignObject><path d="M0 0"/></svg>', encoding="utf-8")
    (assets / "plain.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"><path d="M0 0"/></svg>',
                                      encoding="utf-8")
    changed = module.sanitize_seed_svgs(tmp_path)
    assert changed == ["src/endpoints/seed/assets/shared/blur.svg (1 element(s))"]
    assert (assets / "blur.svg").read_text(encoding="utf-8") == '<svg xmlns="http://www.w3.org/2000/svg"><path d="M0 0"/></svg>'


def test_plain_tldr_and_faq_questions_become_rich_text_only():
    module = _module()
    rich = {"root": {"children": []}}
    guide = {"tldr": [{"text": "a"}, {"text": rich}],
             "layout": [{"blockType": "faq", "items": [{"question": "q", "answer": rich}]},
                        {"blockType": "content", "text": "kept"}]}
    assert module.lexicalize({"guides": [{"locales": {"en": guide}}]}) == 2
    assert guide["tldr"][0]["text"]["root"]["children"][0]["children"][0]["text"] == "a"
    assert guide["tldr"][1]["text"] is rich and guide["layout"][0]["items"][0]["answer"] is rich
    assert guide["layout"][0]["items"][0]["question"]["root"]["type"] == "root"
    assert guide["layout"][1]["text"] == "kept"


def test_the_start_refuses_a_checkout_missing_a_sub_path(monkeypatch, tmp_path):
    module = _module()
    where = tmp_path / "website"
    for sub_path in module.SUB_PATHS:
        target = where / sub_path
        target.parent.mkdir(parents=True, exist_ok=True)
        if "." in sub_path.rsplit("/", 1)[-1]:
            target.write_text("{}", encoding="utf-8")
        else:
            target.mkdir()
    monkeypatch.setattr(module.lab, "CHECKOUT", tmp_path)
    monkeypatch.delenv("LAB_WEBSITE_CHECKOUT", raising=False)
    assert module.website_checkout() == where
    (where / "src" / "app").rmdir()
    with pytest.raises(SystemExit) as refused:
        module.website_checkout()
    assert "lacks src/app" in str(refused.value)
