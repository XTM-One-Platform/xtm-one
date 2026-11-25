"""#1495 last slice: the stored profile field ``tech_stack`` is named ``software`` (migration s1495swfield01)."""

import importlib.util
from pathlib import Path

from app.services import organization_context as oc

_PATH = Path(__file__).resolve().parents[1] / "alembic/versions/s1495swfield01_profile_tech_stack_key_to_software.py"
_SPEC = importlib.util.spec_from_file_location("s1495swfield01", _PATH)
assert _SPEC and _SPEC.loader
mig = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(mig)


def test_profile_key_is_renamed_rows_kept() -> None:
    stored = {
        "name": "Acme",
        "tech_stack": [{"product": "nginx", "vendor": "F5", "cpe": ""}],
        "assets_exposure": [{"name": "web-1", "tech_stack": ["nginx"]}],
        "onboarding_skipped": ["tech_stack", "pir_ids"],
    }
    out = mig.rename_profile(stored)
    assert out is not None
    assert "tech_stack" not in out
    assert out["software"] == [{"product": "nginx", "vendor": "F5", "cpe": ""}]
    # The per-asset product list is not the profile field.
    assert out["assets_exposure"] == [{"name": "web-1", "tech_stack": ["nginx"]}]
    assert out["onboarding_skipped"] == ["software", "pir_ids"]
    # s1495products01 renames the per-asset list later; the model reads it as ``products``.
    parsed = {**out, "assets_exposure": [{"name": "web-1", "products": ["nginx"]}]}
    assert oc.parse_profile(parsed).software[0].product == "nginx"
    assert mig.rename_profile(out) is None  # re-run: no-op


def test_downgrade_names_it_back() -> None:
    out = mig.rename_profile({"software": [{"product": "nginx"}]}, "software", "tech_stack")
    assert out == {"tech_stack": [{"product": "nginx"}]}


def test_both_keys_keep_software() -> None:
    out = mig.rename_profile({"tech_stack": [{"product": "old"}], "software": [{"product": "new"}]})
    assert out == {"software": [{"product": "new"}]}


def test_provenance_and_proposals_paths_renamed() -> None:
    sources = {"tech_stack": {"source": "ai_draft"}, "tech_stack[0].cpe": {"source": "user"}, "sector": {"source": "x"}}
    assert mig.rename_sources(sources) == {
        "software": {"source": "ai_draft"},
        "software[0].cpe": {"source": "user"},
        "sector": {"source": "x"},
    }
    assert mig.rename_sources({"sector": {}}) is None
    proposals = [{"field": "tech_stack", "value": []}, {"field": "sector", "value": "x"}]
    assert mig.rename_proposals(proposals) == [{"field": "software", "value": []}, {"field": "sector", "value": "x"}]
    assert mig.rename_proposals([{"field": "sector"}]) is None


def test_profile_model_has_no_tech_stack_field() -> None:
    assert "tech_stack" not in oc.OrganizationProfile.model_fields
    assert ("software", "software") in oc.contract_fields()
