"""The routes the evaluation creation screen reads before the evaluation exists.

The creation screen offers the overall metric selection and each agent's output
extraction while the evaluation is being written, so it has a family and no
evaluation id: ``GET /evaluations/metric-catalog?family=`` and ``POST
/evaluations/extractors/preview`` answer from the family alone. Both are read-only
and touch no table: the database dependency fails the test if either reaches it.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import deps
from app.api.v1 import evaluations as evaluations_api
from app.core.config import settings
from app.core.database import get_db
from app.services.evaluation.metric_package import metric_catalog

_FENCE = "`" * 3
#: The labels of nested JSON answers, found in prose.
_LABELS_EXTRACTOR = {
    "source": {"kind": "agent_output"},
    "operations": [
        {"op": "find_json"},
        {"op": "parse_json"},
        {"op": "object_values"},
        {"op": "flatten"},
        {"op": "collect_keys", "path": ["predictions"]},
        {"op": "dedupe_sort"},
    ],
    "output_kind": "label_set",
}


def _client(*, is_admin: bool = True) -> TestClient:
    app = FastAPI()
    app.include_router(evaluations_api.router, prefix="/api/v1/evaluations")
    app.dependency_overrides[deps.get_current_user] = lambda: SimpleNamespace(
        id=uuid.uuid4(), is_admin=is_admin, full_name="Author"
    )

    async def _no_database():
        raise AssertionError("a creation-time route read the database")
        yield  # pragma: no cover - makes this an async generator dependency

    app.dependency_overrides[get_db] = _no_database
    return TestClient(app)


# --- GET /evaluations/metric-catalog?family= -----------------------------------------


@pytest.mark.parametrize("family", ["CLASSIFICATION", "STRUCTURED_JSON", "STIX_BUNDLE", "PROSE"])
def test_the_family_catalog_is_the_catalog_a_saved_evaluation_of_that_family_gets(family) -> None:
    """Served from the family the author picked, the same entries as
    ``/{evaluation_id}/metric-catalog`` serves once the evaluation is saved; and not
    shadowed by ``/{evaluation_id}``, which would read ``metric-catalog`` as an id."""
    response = _client().get("/api/v1/evaluations/metric-catalog", params={"family": family})

    assert response.status_code == 200
    assert response.json() == metric_catalog(family)
    assert {entry["kind"] for entry in response.json()} == {"quality", "performance"}


def test_the_family_catalog_refuses_an_unknown_family() -> None:
    response = _client().get("/api/v1/evaluations/metric-catalog", params={"family": "NOT_A_FAMILY"})

    assert response.status_code == 400
    assert "Unknown evaluation family" in response.json()["detail"]


# --- GET /evaluations/families ----------------------------------------------------------


def test_each_family_names_the_output_kinds_an_extractor_may_produce() -> None:
    """The extraction card offers and seeds these, by the rule a save applies: a
    CLASSIFICATION extractor produces a label set, a STRUCTURED_JSON one an object or an
    array, and the other families take none in this version."""
    response = _client(is_admin=False).get("/api/v1/evaluations/families")

    assert response.status_code == 200
    kinds = {family["id"]: family["extractor_output_kinds"] for family in response.json()}
    assert kinds["CLASSIFICATION"] == ["label_set"]
    assert kinds["STRUCTURED_JSON"] == ["array", "object"]
    assert kinds["PROSE"] == []
    assert kinds["STIX_BUNDLE"] == []


# --- POST /evaluations/extractors/preview ----------------------------------------------


def test_a_pasted_sample_previews_before_the_evaluation_exists() -> None:
    """The extraction the run path applies, on a sample, with only the family."""
    nested = '{"email-1": {"predictions": {"phishing": 0.9}}, "email-2": {"predictions": {"malware": 0.4}}}'
    answer = f"Here you go:\n{_FENCE}json\n{nested}\n{_FENCE}"

    response = _client().post(
        "/api/v1/evaluations/extractors/preview",
        json={"family": "CLASSIFICATION", "extractor": _LABELS_EXTRACTOR, "sample": {"agent_output": answer}},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "extracted"
    assert body["output_kind"] == "label_set"
    assert body["item_count"] == 2
    assert json.loads(body["prediction"]) == ["malware", "phishing"]


def test_a_sample_the_extractor_cannot_read_reports_why() -> None:
    response = _client().post(
        "/api/v1/evaluations/extractors/preview",
        json={"family": "CLASSIFICATION", "extractor": _LABELS_EXTRACTOR, "sample": {"agent_output": "no json"}},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "failed"
    assert body["prediction"] is None
    assert body["failure"]["reason"]


def test_the_preview_refuses_an_extractor_the_family_cannot_score() -> None:
    """The family check a save runs: CLASSIFICATION scores a label set, not an object."""
    response = _client().post(
        "/api/v1/evaluations/extractors/preview",
        json={
            "family": "CLASSIFICATION",
            "extractor": {**_LABELS_EXTRACTOR, "output_kind": "object"},
            "sample": {"agent_output": "{}"},
        },
    )

    assert response.status_code == 422


@pytest.mark.parametrize("family", ["PROSE", "STIX_BUNDLE"])
def test_the_preview_refuses_any_extractor_for_a_family_that_takes_none(family) -> None:
    """The rule the card follows when it disables the switch: these families take no
    output extraction in this version, whatever the kind."""
    response = _client().post(
        "/api/v1/evaluations/extractors/preview",
        json={
            "family": family,
            "extractor": {
                "source": {"kind": "agent_output"},
                "operations": [{"op": "parse_json"}],
                "output_kind": "object",
            },
            "sample": {"agent_output": "{}"},
        },
    )

    assert response.status_code == 422
    assert "not supported in this version" in response.json()["detail"]


@pytest.mark.parametrize("kind", ["tool_output", "tool_input"])
def test_a_pasted_tool_call_previews_before_the_evaluation_exists(kind) -> None:
    """A tool source reads the sample's ``traces``: the one successful call the card
    builds from the pasted text, as the per-evaluation preview reads it."""
    call = {"type": "tool", "tool_name": "search_cases", "outcome": "success", kind: '{"cases": [1]}'}

    response = _client().post(
        "/api/v1/evaluations/extractors/preview",
        json={
            "family": "STRUCTURED_JSON",
            "extractor": {
                "source": {"kind": kind, "tool_name": "search_cases"},
                "operations": [{"op": "parse_json"}],
                "output_kind": "object",
            },
            "sample": {"traces": [call]},
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "extracted"
    assert json.loads(body["prediction"]) == {"cases": [1]}


@pytest.mark.parametrize(
    ("body", "status"),
    [
        pytest.param({"family": "NOT_A_FAMILY"}, 400, id="unknown-family"),
        pytest.param({"family": "CLASSIFICATION", "agent_id": str(uuid.UUID(int=1))}, 422, id="extra-field"),
    ],
)
def test_the_preview_refuses_what_its_contract_does_not_take(body, status) -> None:
    response = _client().post(
        "/api/v1/evaluations/extractors/preview",
        json={"extractor": _LABELS_EXTRACTOR, "sample": {"agent_output": "{}"}, **body},
    )

    assert response.status_code == status


@pytest.mark.parametrize(
    "sample",
    [
        pytest.param({"agent_output": "x" * 600}, id="answer"),
        # A tool source reads the traces: they are weighed too, or they would escape it.
        pytest.param(
            {"traces": [{"type": "tool", "tool_name": "search", "outcome": "success", "tool_output": "x" * 600}]},
            id="traces",
        ),
    ],
)
def test_the_preview_bounds_the_pasted_sample(monkeypatch, sample) -> None:
    monkeypatch.setattr(settings, "extraction_max_sample_bytes", 512)

    response = _client().post(
        "/api/v1/evaluations/extractors/preview",
        json={"family": "CLASSIFICATION", "extractor": _LABELS_EXTRACTOR, "sample": sample},
    )

    assert response.status_code == 422
    assert "exceeds 512 bytes" in response.json()["detail"]


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        pytest.param("get", "/api/v1/evaluations/metric-catalog?family=CLASSIFICATION", None, id="catalog"),
        pytest.param(
            "post",
            "/api/v1/evaluations/extractors/preview",
            {"family": "CLASSIFICATION", "extractor": _LABELS_EXTRACTOR, "sample": {"agent_output": "{}"}},
            id="preview",
        ),
    ],
)
def test_both_creation_routes_are_for_admins_only(method, path, payload) -> None:
    """Creating an evaluation is admin-only, and so is what its screen reads."""
    client = _client(is_admin=False)
    response = client.get(path) if method == "get" else client.post(path, json=payload)

    assert response.status_code == 403
