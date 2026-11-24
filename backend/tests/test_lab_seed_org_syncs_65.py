"""The organization syncs and import preview seed of the lab (#65)."""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LAB = ROOT / ".xtm-one" / "lab" / "lab.py"
ASGI = LAB.parent / "lab_asgi.py"

pytestmark = pytest.mark.skipif(not LAB.is_file() or not ASGI.is_file(), reason="no .xtm-one next to the backend")


def _asgi(monkeypatch, env: dict[str, str]):
    from app.services import organization_context_import as imp

    async def backend(scope, receive, send):  # pragma: no cover - never called here
        return None

    for key in ("LAB_ORG_IMPORT", "LAB_OPENCTI_IDENTITIES"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(imp, "read_source", imp.read_source)
    monkeypatch.setattr(imp, "source_statuses", imp.source_statuses)
    monkeypatch.setitem(sys.modules, "app.main", types.SimpleNamespace(application=backend))
    monkeypatch.syspath_prepend(str(LAB.parent))
    spec = importlib.util.spec_from_file_location("lab_asgi_org_import_under_test", ASGI)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, imp


def _source(name: str) -> str:
    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    return ast.unparse(next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name))


@pytest.mark.parametrize(
    ("section", "source"),
    [("assets", "opencti"), ("assets", "openaev"), ("asset_groups", "openaev"), ("software", "openaev")],
)
def test_the_fixture_reads_rows_through_the_real_mappers(monkeypatch, section, source):
    module, imp = _asgi(monkeypatch, {})
    read = module.lab_org_import_read(section, source)
    assert read is not None and read.error is None and read.rows and read.total == len(read.rows)
    from app.services import organization_context as org_context

    plan = imp.plan_import(org_context.parse_profile({}), section, source, read)
    assert plan.error is None and len(plan.new) == len(read.rows)


def test_no_fixture_for_a_pair_the_lab_does_not_cover(monkeypatch):
    module, _ = _asgi(monkeypatch, {})
    assert module.lab_org_import_read("primary_assets", "opencrq") is None


def test_the_stub_replaces_only_a_not_connected_read(monkeypatch):
    module, imp = _asgi(monkeypatch, {})
    answers = {
        "opencti": imp.ReadResult(error="OpenCTI: not connected, or no token for this administrator"),
        "openaev": imp.ReadResult(error="OpenAEV: upstream 502"),
    }

    async def live(db, *, user_id, section, source, filters, batch_type=""):
        return answers[source]

    monkeypatch.setattr(imp, "read_source", live)
    assert module.stub_org_import({"LAB_ORG_IMPORT": "1"}) is True
    kwargs = {"user_id": "u", "section": "assets", "filters": {}}
    replaced = asyncio.run(imp.read_source(None, source="opencti", **kwargs))
    assert replaced.error is None and replaced.rows
    kept = asyncio.run(imp.read_source(None, source="openaev", **kwargs))
    assert kept is answers["openaev"]


def test_the_stub_marks_only_an_unavailable_platform_available(monkeypatch):
    module, imp = _asgi(monkeypatch, {})

    async def live(db, *, user_id):
        return {
            "opencti": imp.SourceStatus("opencti", False, "not connected"),
            "openaev": imp.SourceStatus("openaev", True),
            "opencrq": imp.SourceStatus("opencrq", False, "not_connected"),
        }

    monkeypatch.setattr(imp, "source_statuses", live)
    module.stub_org_import({"LAB_ORG_IMPORT": "1"})
    statuses = asyncio.run(imp.source_statuses(None, user_id="u"))
    assert statuses["opencti"].available and statuses["openaev"].available
    assert not statuses["opencrq"].available


def test_the_switch_off_leaves_the_real_path(monkeypatch):
    module, imp = _asgi(monkeypatch, {})
    before = imp.read_source
    assert module.stub_org_import({}) is False
    assert imp.read_source is before


def test_every_lab_sets_the_switch_unless_a_declaration_says_otherwise():
    assert '"LAB_ORG_IMPORT": os.environ.get("LAB_ORG_IMPORT", "1")' in LAB.read_text(encoding="utf-8")


def test_the_seed_saves_both_syncs_pauses_one_reads_back_and_logs_it():
    helper = _source("seed_org_syncs")
    for part in (
        "'/organization-context/syncs'",
        "ORG_SYNC_OWNER_EMAIL",
        "'role': 'admin'",
        "/role",
        "{'role': 'user'}",
        "/run",
        "paused_reason",
        "/organization-context/import/preview",
        "seeded organization syncs:",
        "/settings/organization",
    ):
        assert part in helper, part
    seeded = _source("seed_entities")
    call = "seed_org_syncs(token, base)"
    assert call in seeded and "skipped['organization syncs']" in seeded
    assert seeded.index(call) < seeded.index("seeded.json")
