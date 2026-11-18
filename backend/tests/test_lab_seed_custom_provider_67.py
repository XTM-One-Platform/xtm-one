"""The custom integration provider seed of the lab (#67)."""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LAB = ROOT / ".xtm-one" / "lab" / "lab.py"

pytestmark = pytest.mark.skipif(not LAB.is_file(), reason="no .xtm-one next to the backend")


def _lab(monkeypatch):
    monkeypatch.syspath_prepend(str(LAB.parent))
    monkeypatch.syspath_prepend(str(LAB.parents[1]))
    spec = importlib.util.spec_from_file_location("lab_custom_provider_under_test", LAB)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


def _source(name: str) -> str:
    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    return ast.unparse(next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name))


def test_the_bundle_passes_the_real_import_parser(monkeypatch):
    from app.services.integration_bundle_manager import (
        normalize_custom_provider_slug,
        parse_integration_bundle_zip,
    )

    lab = _lab(monkeypatch)
    parsed = parse_integration_bundle_zip(lab.lab_custom_provider_bundle())
    assert normalize_custom_provider_slug(parsed.metadata.provider) == lab.LAB_CUSTOM_PROVIDER
    assert parsed.metadata.auth_type == "api_key"
    assert {s.type for s in parsed.metadata.settings} == {"secret", "select"}
    assert parsed.integration_instance is None


def test_the_provider_is_not_a_native_one(monkeypatch):
    from app.api.v1.integration_catalog import PROVIDER_CATALOG

    lab = _lab(monkeypatch)
    assert lab.LAB_CUSTOM_PROVIDER not in PROVIDER_CATALOG
    assert lab.LAB_CUSTOM_PROVIDER_METADATA["provider"] not in PROVIDER_CATALOG


def test_the_seed_imports_once_reads_back_and_logs_it():
    helper = _source("seed_custom_integration_provider")
    for part in (
        "'/integrations/catalog'",
        "/api/v1/integrations/admin/import",
        'name="bundle"',
        "is_custom",
        "seeded custom integration provider:",
    ):
        assert part in helper, part


def test_every_lab_runs_the_seed():
    assert "seed_custom_integration_provider(token, base)" in LAB.read_text(encoding="utf-8")
