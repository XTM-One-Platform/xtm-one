"""Draft of the organization profile with a model (#9484, S-a; dev-docs/ctem-fleets.md section 6.1).

The gates of S-a: no model call without the request, a filled field is a
conflict and never overwritten, no fetch without a URL and no document
read without an upload, an unsupported field is named, the deterministic
draft unchanged. The model is a stub; nothing touches a network.
"""

from __future__ import annotations

import ast
import json
import pathlib
import uuid
from types import SimpleNamespace

import pytest

from app.services import organization_context as oc
from app.services import organization_context_ai_draft as ai

BACKEND = pathlib.Path(__file__).resolve().parents[1]


class _Llm:
    def __init__(self, answer: dict | str) -> None:
        self.answer = answer if isinstance(answer, str) else json.dumps(answer)
        self.calls: list[list] = []

    async def chat(self, messages, **kwargs):
        self.calls.append(messages)
        return SimpleNamespace(
            content=self.answer, stop_reason="end_turn", usage={"input_tokens": 100, "output_tokens": 20}
        )


def _evidence(**texts: str) -> ai.Evidence:
    return ai.Evidence(texts=dict(texts) or {"description": "retail bank"})


def test_a_proposal_is_validated_typed_and_cites_a_given_source():
    answer = {
        "proposals": [
            {
                "field": "regions",
                "value": ["fr", "DE", "fr"],
                "source": "description",
                "confidence": "high",
                "basis": "FR/DE",
            },
            {
                "field": "size",
                "value": "1001-5000",
                "source": "description",
                "confidence": "medium",
                "basis": "4,000 people",
            },
            {"field": "currency", "value": "EURO", "source": "description", "confidence": "high", "basis": "x"},
            {"field": "sector", "value": "Finance", "source": "website:https://x.example", "confidence": "high"},
            {"field": "favourite_colour", "value": "blue", "source": "description"},
        ]
    }
    proposals, _ns, _sp, notes = ai.parse_proposals(json.dumps(answer), oc.OrganizationProfile(), _evidence())
    assert [(p.field, p.value) for p in proposals] == [("regions", ["FR", "DE"]), ("size", "1001-5000")]
    assert all(p.source == "description" and not p.conflict for p in proposals)
    assert any("currency" in n for n in notes)  # not ISO 4217: dropped, named
    assert any("source 'website:https://x.example' was not given" in n for n in notes)
    assert any("favourite_colour" in n for n in notes)


def test_a_filled_field_is_a_conflict_and_an_identical_value_is_kept_unchanged():
    current = oc.OrganizationProfile(sector="Banking", regions=["FR"])
    answer = {
        "proposals": [
            {"field": "sector", "value": "Retail banking", "source": "description", "confidence": "high"},
            {"field": "regions", "value": ["FR"], "source": "description", "confidence": "high"},
        ]
    }
    proposals, not_supported, *_ = ai.parse_proposals(json.dumps(answer), current, _evidence())
    by_field = {p.field: p for p in proposals}
    assert (by_field["sector"].conflict, by_field["sector"].current) == (True, "Banking")
    # Equal to the current value: answered for "Unchanged (n)", never a conflict (ctem-fleets.md 6.1).
    assert (by_field["regions"].value, by_field["regions"].current, by_field["regions"].conflict) == (
        ["FR"],
        ["FR"],
        False,
    )
    assert "regions" not in not_supported


def test_an_unsupported_missing_field_is_named_never_guessed():
    answer = {"proposals": [{"field": "sector", "value": "Energy", "source": "description", "confidence": "high"}]}
    _p, not_supported, _s, _n = ai.parse_proposals(json.dumps(answer), oc.OrganizationProfile(), _evidence())
    assert "sector" not in not_supported
    assert "software" in not_supported and "loaded_hourly_rate" in not_supported


def test_an_inferred_amount_is_flagged_check_this():
    answer = {
        "proposals": [
            {
                "field": "loaded_hourly_rate",
                "value": 95,
                "source": "description",
                "confidence": "medium",
                "basis": "sector average",
            },
            {
                "field": "risk_appetite.annual_loss_threshold",
                "value": 2000000,
                "source": "description",
                "confidence": "high",
                "basis": "stated: 2M",
            },
        ]
    }
    proposals, *_ = ai.parse_proposals(json.dumps(answer), oc.OrganizationProfile(), _evidence())
    assert {p.field: p.check for p in proposals} == {
        "loaded_hourly_rate": True,
        "risk_appetite.annual_loss_threshold": False,
    }


def test_only_existing_pirs_are_proposed_and_primary_assets_link_to_a_single_matching_asset():
    evidence = _evidence(opencti="{}")
    evidence.known_pir_ids = ["pir-1"]
    evidence.opencrq_assets = {"core banking": ["a1"]}
    answer = {
        "proposals": [
            {"field": "pir_ids", "value": ["pir-1", "pir-9"], "source": "opencti", "confidence": "high"},
            {
                "field": "primary_assets",
                "value": [{"name": "Core banking", "criticality": "critical"}],
                "source": "opencti",
                "confidence": "high",
            },
        ],
        "suggested_pirs": ["Ransomware on retail banks"],
    }
    proposals, _ns, suggested, notes = ai.parse_proposals(json.dumps(answer), oc.OrganizationProfile(), evidence)
    by = {p.field: p.value for p in proposals}
    assert by["pir_ids"] == ["pir-1"]
    assert by["primary_assets"][0]["opencrq_asset_id"] == "a1"
    assert suggested == ["Ransomware on retail banks"]
    assert any("PIR id(s) left out" in n for n in notes)


def test_an_unusable_answer_is_an_error():
    with pytest.raises(ai.AiDraftError):
        ai.parse_proposals("I cannot help", oc.OrganizationProfile(), _evidence())


@pytest.mark.parametrize(
    ("inputs", "message"),
    [
        (ai.AiDraftInputs(use_platforms=False), "Give a description"),
        (ai.AiDraftInputs(description="x" * 4001), "4,000"),
        (ai.AiDraftInputs(website="ftp://x.example"), r"http\(s\) URL"),
        (ai.AiDraftInputs(documents=[ai.DraftDocument("d.txt", b"x")] * 6), "At most 5"),
    ],
)
def test_inputs_are_bounded_before_anything_is_read(inputs, message):
    with pytest.raises(ValueError, match=message):
        inputs.validate()


async def test_no_fetch_without_a_url_and_no_read_without_an_upload(monkeypatch):
    async def boom(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("read an input that was not given")

    monkeypatch.setattr(ai, "read_website", boom)
    monkeypatch.setattr(ai, "read_documents", boom)
    monkeypatch.setattr(ai, "read_platforms", boom)
    evidence = await ai.collect_evidence(
        SimpleNamespace(), user_id=uuid.uuid4(), inputs=ai.AiDraftInputs(description="retail bank", use_platforms=False)
    )
    assert evidence.texts == {"description": "retail bank"}


async def test_one_model_call_over_the_inputs_and_nothing_saved(monkeypatch):
    async def no_save(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("the AI draft saved a version")

    monkeypatch.setattr(oc, "save_version", no_save)
    llm = _Llm({"proposals": [{"field": "sector", "value": "Retail", "source": "description", "confidence": "high"}]})
    draft = await ai.draft_with_ai(
        SimpleNamespace(),
        user_id=uuid.uuid4(),
        inputs=ai.AiDraftInputs(description="retail chain", use_platforms=False),
        current=oc.OrganizationProfile(),
        based_on_version=None,
        llm=llm,
        model="m",
    )
    assert len(llm.calls) == 1
    assert "retail chain" in llm.calls[0][1].content
    assert [p.field for p in draft.proposals] == ["sector"]
    assert draft.usage == {"input_tokens": 100, "output_tokens": 20}


def test_same_site_links_stay_on_the_host_and_are_bounded():
    html = "".join(
        f'<a href="{h}">x</a>'
        for h in ("/about", "https://other.example/x", "/about#team", "/products", "/careers", "/contact", "/news", "/")
    )
    assert ai.same_site_links(html, "https://acme.example/") == [
        "https://acme.example/about",
        "https://acme.example/products",
        "https://acme.example/careers",
        "https://acme.example/contact",
    ]


def test_accepted_sources_keep_contract_fields_only():
    assert oc.accepted_sources({"sector": {"source": "description", "basis": "b"}, "x": {"source": "y"}}) == {
        "sector": {"source": "description", "basis": "b"}
    }
    assert oc.accepted_sources(None) is None


def test_no_page_load_or_scheduler_imports_the_ai_draft():
    """Only the ai-draft endpoint imports the service, inside its handler (no model at load or in the background)."""
    importers = []
    for path in (BACKEND / "app").rglob("*.py"):
        if path.name == "organization_context_ai_draft.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.ImportFrom) and node.module and "organization_context_ai_draft" in node.module) or (isinstance(node, ast.ImportFrom) and any(
                a.name == "organization_context_ai_draft" for a in node.names
            )):
                importers.append(path.relative_to(BACKEND).as_posix())
    assert importers == ["app/api/v1/organization_context.py"]
