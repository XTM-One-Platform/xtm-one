"""The flow Classify (CLM) node - catalog, validator and executor, hermetic.

The walk end to end (branch taken, journal, error path, the example flow) is
in ``test_flow_classifier_postgres.py``; provider resolution against real
config rows too.
"""

import uuid
from types import SimpleNamespace

import pytest

from app.llm.decisions import MAX_CHOICES, ChoiceDecision
from app.services import flow_classifier
from app.services.flow_catalog import (
    NODE_KINDS_BY_ID,
    PER_CASE_SOCKET_KINDS,
    get_port,
)
from app.services.flow_classifier import MAX_STATE_CHARS, ClassifierError
from app.services.flow_graph import normalize_graph, validate_graph
from app.services.flow_runner import _confidence_floor, _Renderer

# ── Catalog ──────────────────────────────────────────────────────────────────


def test_the_classify_node_is_a_condition_with_label_uncertain_and_error_outputs():
    nk = NODE_KINDS_BY_ID["condition.classify"]
    assert nk.category == "conditions"
    assert nk.requires_ai is True
    assert [p.id for p in nk.outputs] == ["case", "default", "error"]
    assert {f.name for f in nk.config_fields if f.required} == {
        "model",
        "input",
        "question",
        "cases",
    }
    assert {v.name for v in nk.output_variables} == {
        "label",
        "confidence",
        "probabilities",
    }


def test_a_label_socket_resolves_to_the_case_port():
    assert "condition.classify" in PER_CASE_SOCKET_KINDS
    port = get_port(
        NODE_KINDS_BY_ID["condition.classify"], "case:investigate", output=True
    )
    assert port is not None
    assert port.label == "investigate"
    assert port.types == ("branch",)


# ── Validator ────────────────────────────────────────────────────────────────


def _graph(cases, edges_from_classify):
    return normalize_graph(
        {
            "nodes": [
                {"id": "t1", "kind": "trigger.manual", "config": {}},
                {
                    "id": "c1",
                    "kind": "condition.classify",
                    "config": {
                        "model": "typesafe/jev-1.13",
                        "input": "{{t1.prompt}}",
                        "question": "Relevant?",
                        "cases": cases,
                    },
                },
                {"id": "s1", "kind": "util.stop", "config": {"result": "x"}},
            ],
            "edges": [
                {
                    "id": "e0",
                    "source": "t1",
                    "source_handle": "out",
                    "target": "c1",
                    "target_handle": "in",
                    "kind": "data",
                },
                *[
                    {
                        "id": f"e{i + 1}",
                        "source": "c1",
                        "source_handle": handle,
                        "target": "s1",
                        "target_handle": "in",
                        "kind": "error" if handle == "error" else "condition_branch",
                    }
                    for i, handle in enumerate(edges_from_classify)
                ],
            ],
        }
    )


def test_every_output_of_the_classify_node_is_wirable():
    result = validate_graph(
        _graph({"yes": "", "no": ""}, ["case:yes", "case:no", "default", "error"])
    )
    assert result.is_valid
    assert [i.code for i in result.issues] == []


def test_a_label_edge_that_outlived_its_label_is_flagged():
    result = validate_graph(_graph({"yes": ""}, ["case:yes", "case:gone"]))
    stale = [i for i in result.issues if i.code == "switch.stale_case"]
    assert len(stale) == 1
    assert stale[0].edge_id == "e2"


def test_more_labels_than_a_decision_model_takes_is_flagged():
    cases = {f"l{i}": "" for i in range(MAX_CHOICES + 1)}
    result = validate_graph(_graph(cases, ["default"]))
    assert [i.code for i in result.issues] == ["classify.too_many_labels"]


# ── Executor ─────────────────────────────────────────────────────────────────


def _walker(outputs=None, untrusted=()):
    """A ``FlowWalker`` with only what the Classify executor reads."""
    from app.services.flow_runner import FlowWalker

    walker = object.__new__(FlowWalker)
    walker.db = object()
    walker.user = SimpleNamespace(id=uuid.UUID(int=7))
    walker._renderer = _Renderer(
        node_outputs=outputs or {"t1": {"prompt": "alert body"}},
        untrusted_nodes=set(untrusted),
        trigger_payload={},
        trigger_untrusted=False,
        variables={},
        trigger_source="webhook trigger",
    )
    return walker


def _config(**overrides):
    config = {
        "model": "typesafe/jev-1.13",
        "input": "{{t1.prompt}}",
        "question": "Relevant?",
        "cases": {"yes": "Needs an analyst", "no": "Noise"},
        "min_confidence": 0.6,
    }
    config.update(overrides)
    return config


@pytest.fixture
def classify_spy(monkeypatch):
    """Replace the CLM call; ``.decision`` / ``.error`` decide its outcome."""
    spy = SimpleNamespace(calls=[], decision=None, error=None)

    async def fake_classify(db, **kwargs):
        spy.calls.append(kwargs)
        if spy.error is not None:
            raise spy.error
        return spy.decision

    monkeypatch.setattr(flow_classifier, "classify", fake_classify)
    return spy


@pytest.mark.asyncio
async def test_a_confident_label_takes_its_own_socket(classify_spy):
    classify_spy.decision = ChoiceDecision(
        choice="yes", confidence=0.9, probabilities={"yes": 0.9, "no": 0.1}
    )
    provider_id = uuid.uuid4()
    result = await _walker()._exec_condition_classify(
        _config(provider=str(provider_id))
    )
    assert result.error is None
    assert result.next_handles == ["case:yes", "case"]
    assert result.switch_case == "yes"
    assert result.outputs == {
        "label": "yes",
        "confidence": 0.9,
        "probabilities": {"yes": 0.9, "no": 0.1},
        "_tainted": False,
    }
    assert classify_spy.calls == [
        {
            "provider_id": provider_id,
            "model": "typesafe/jev-1.13",
            "state": "alert body",
            "instructions": "Relevant?",
            "choices": {"yes": "Needs an analyst", "no": "Noise"},
            "user_id": str(uuid.UUID(int=7)),
        }
    ]


@pytest.mark.asyncio
async def test_a_label_under_the_confidence_floor_takes_uncertain(classify_spy):
    classify_spy.decision = ChoiceDecision(choice="no", confidence=0.55)
    result = await _walker()._exec_condition_classify(_config())
    assert result.next_handles == ["default"]
    assert result.switch_case is None
    assert result.outputs["label"] == "no"


@pytest.mark.asyncio
async def test_no_decision_takes_the_error_path(classify_spy):
    classify_spy.error = ClassifierError("The decision model call failed (HTTP 503).")
    result = await _walker()._exec_condition_classify(_config())
    assert result.error == "The decision model call failed (HTTP 503)."


@pytest.mark.asyncio
async def test_untrusted_content_taints_the_decision(classify_spy):
    classify_spy.decision = ChoiceDecision(choice="yes", confidence=1.0)
    result = await _walker(untrusted=["t1"])._exec_condition_classify(_config())
    assert result.outputs["_tainted"] is True


@pytest.mark.asyncio
async def test_oversized_content_is_cut_and_says_so(classify_spy):
    classify_spy.decision = ChoiceDecision(choice="yes", confidence=1.0)
    walker = _walker({"t1": {"prompt": "x" * (MAX_STATE_CHARS + 10)}})
    result = await walker._exec_condition_classify(_config())
    assert len(classify_spy.calls[0]["state"]) == MAX_STATE_CHARS
    assert result.warning is not None and str(MAX_STATE_CHARS) in result.warning


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"model": "  "}, "Pick the decision model this node asks."),
        ({"cases": {}}, "Declare at least one label to classify into."),
        ({"cases": ["yes", "no"]}, "Declare at least one label to classify into."),
        (
            {"cases": {f"l{i}": "" for i in range(MAX_CHOICES + 1)}},
            f"A decision model takes at most {MAX_CHOICES} labels.",
        ),
        ({"input": ""}, "The content to classify is empty."),
        ({"question": " "}, "The question to ask about the content is empty."),
        (
            {"provider": "not-a-uuid"},
            "The LLM provider picked on this node is not a valid id.",
        ),
    ],
    ids=[
        "blank-model",
        "no-labels",
        "labels-not-a-map",
        "too-many-labels",
        "empty-content",
        "blank-question",
        "bad-provider-id",
    ],
)
async def test_an_unusable_config_fails_without_calling_the_model(
    classify_spy, overrides, error
):
    classify_spy.decision = ChoiceDecision(choice="yes", confidence=1.0)
    result = await _walker()._exec_condition_classify(_config(**overrides))
    assert result.error == error
    assert classify_spy.calls == []


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, 0.0),
        ("", 0.0),
        ("0.7", 0.7),
        (0.25, 0.25),
        (-1, 0.0),
        (3, 1.0),
        (True, 0.0),
        (float("nan"), 0.0),
        ("high", 0.0),
    ],
)
def test_confidence_floor_is_clamped_and_never_raises(value, expected):
    assert _confidence_floor(value) == expected
