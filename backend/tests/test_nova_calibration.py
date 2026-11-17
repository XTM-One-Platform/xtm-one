"""The Nova semantic-threshold calibration.

Every shipped constant is a measurement on the labelled set; these tests hold
the constants to the set they were measured on, the mapping to its meaning,
and every surface that reports an uncalibrated model.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.api.v1 import security_audit
from app.api.v1.nova_rules import _to_response, nova_engine_status
from app.models.embedding import EMBEDDING_DIMENSIONS
from app.services import nova_calibration, nova_engine
from app.services.nova_calibration import (
    CALIBRATIONS,
    REFERENCE,
    REFERENCE_BASELINE,
    REFERENCE_MODEL,
    REFERENCE_SPAN,
    calibrate_score,
    calibration_set_version,
    calibration_status,
    derive_calibration,
    is_calibrated,
    load_calibration_set,
)
from app.services.nova_guard import NovaRuleError, has_semantic_patterns, parse_rule

#: The labelled set's content, pinned to its version: a change to the texts is
#: a new version, and every shipped entry must then be measured again.
_SET_DIGESTS = {"2026.09": "fea11230bc55b433976cfdda09fba401fce7af1a571942aa6279dded95f9baf7"}


def _set_digest() -> str:
    entries = load_calibration_set()
    return hashlib.sha256(json.dumps(entries, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def _pair_count() -> int:
    return sum(len(e["matching"]) + len(e["non_matching"]) for e in load_calibration_set())


# ---------------------------------------------------------------------------
# The labelled set and the shipped entries
# ---------------------------------------------------------------------------


def test_the_set_content_is_the_one_its_version_was_measured_on():
    assert _SET_DIGESTS.get(calibration_set_version()) == _set_digest()


def test_the_set_has_several_patterns_per_category_each_with_both_kinds_of_text():
    entries = load_calibration_set()
    per_category = Counter(e["category"] for e in entries)
    assert per_category and min(per_category.values()) >= 2
    for entry in entries:
        assert entry["pattern"].strip()
        assert len(entry["matching"]) >= 3 and len(entry["non_matching"]) >= 3
        assert not set(entry["matching"]) & set(entry["non_matching"])
    patterns = [e["pattern"] for e in entries]
    assert len(patterns) == len(set(patterns))


@pytest.mark.parametrize("calibration", [REFERENCE, *CALIBRATIONS.values()], ids=lambda c: c.model)
def test_every_shipped_entry_was_measured_on_the_current_set(calibration):
    assert calibration.set_version == calibration_set_version()
    assert calibration.samples == _pair_count()
    assert 0 <= calibration.baseline < 1 and 0 < calibration.span < 1
    assert calibration.dimensions > 0


def test_entries_are_keyed_by_their_own_model():
    assert all(name == calibration.model for name, calibration in CALIBRATIONS.items())
    assert REFERENCE_MODEL not in CALIBRATIONS


# ---------------------------------------------------------------------------
# The mapping
# ---------------------------------------------------------------------------


def test_the_reference_model_is_scored_unchanged():
    for score in (0.0, 0.1, REFERENCE_BASELINE, 0.73, 1.0):
        assert calibrate_score(score, REFERENCE_MODEL) == score
        assert calibrate_score(score, f"sentence-transformers/{REFERENCE_MODEL}") == score


def test_a_model_with_no_measurement_is_scored_unchanged_and_reported():
    assert calibrate_score(0.42, "embed-1") == 0.42
    assert calibrate_score(0.42, None) == 0.42
    assert calibration_status("embed-1") == {
        "calibrated_for": REFERENCE_MODEL,
        "configured_model": "embed-1",
        "calibrated": False,
    }
    assert calibration_status(None)["calibrated"] is False


@pytest.mark.parametrize("name", sorted(CALIBRATIONS))
def test_a_calibrated_model_lands_its_medians_on_the_references(name):
    calibration = CALIBRATIONS[name]
    assert calibrate_score(calibration.baseline, name) == pytest.approx(REFERENCE_BASELINE)
    assert calibrate_score(calibration.baseline + calibration.span, name) == pytest.approx(
        REFERENCE_BASELINE + REFERENCE_SPAN
    )
    assert is_calibrated(name) and is_calibrated(name.upper())
    assert is_calibrated(name, calibration.dimensions)


@pytest.mark.parametrize("name", sorted(CALIBRATIONS))
def test_a_calibrated_model_at_another_vector_length_is_scored_unchanged_and_reported(name):
    other = CALIBRATIONS[name].dimensions // 2
    assert calibrate_score(0.42, name, other) == 0.42
    assert not is_calibrated(name, other)
    assert calibration_status(name, other)["calibrated"] is False


def test_the_reference_model_is_the_reference_at_its_own_length_only():
    assert is_calibrated(REFERENCE_MODEL, REFERENCE.dimensions)
    assert not is_calibrated(REFERENCE_MODEL, REFERENCE.dimensions * 2)


@pytest.mark.parametrize(
    ("configured", "measured"),
    [
        ("nomic-embed-text:latest", "nomic-embed-text"),
        ("nomic-embed-text:v1.5", "nomic-embed-text"),
        ("all-minilm", REFERENCE_MODEL),
        ("all-minilm:l6-v2", REFERENCE_MODEL),
    ],
)
def test_an_ollama_tag_of_the_same_weights_reads_the_measured_entry(configured, measured):
    assert is_calibrated(configured)
    assert calibrate_score(0.5, configured) == calibrate_score(0.5, measured)


def test_another_ollama_tag_is_not_taken_for_the_measured_one():
    assert not is_calibrated("nomic-embed-text:v2-moe")


def test_derive_takes_medians_and_refuses_what_does_not_separate():
    calibration = derive_calibration("m", [0.1, 0.2, 0.9], [0.5, 0.6, 0.0], set_version="v", dimensions=8)
    assert (calibration.baseline, calibration.span, calibration.samples, calibration.dimensions) == (
        0.2,
        0.3,
        6,
        8,
    )
    with pytest.raises(ValueError, match="does not separate"):
        derive_calibration("m", [0.5, 0.6], [0.4, 0.5], set_version="v", dimensions=8)
    with pytest.raises(ValueError, match="both sample sets"):
        derive_calibration("m", [], [0.4], set_version="v", dimensions=8)


def test_derive_refuses_a_gap_that_rounds_to_nothing():
    with pytest.raises(ValueError, match="does not separate"):
        derive_calibration("m", [0.20001, 0.20002], [0.20003, 0.20004], set_version="v", dimensions=8)


def test_an_entry_with_no_span_is_never_applied(monkeypatch):
    monkeypatch.setitem(
        CALIBRATIONS,
        "flat",
        nova_calibration.SemanticCalibration(
            model="flat", baseline=0.3, span=0.0, samples=2, set_version="v", dimensions=8
        ),
    )
    assert calibrate_score(0.8, "flat") == 0.8
    assert not is_calibrated("flat")


# ---------------------------------------------------------------------------
# The engine scores on the reference scale
# ---------------------------------------------------------------------------


class _Pattern:
    def __init__(self, text: str) -> None:
        self.pattern = text
        self.threshold = 0.5


def _vector(head: list[float], length: int) -> list[float]:
    return head + [0.0] * (length - len(head))


class _Provider:
    """Embeds the prompt and the pattern at a cosine of 0.6, in vectors of *length*."""

    base_url = "https://embeddings.example.test/v1"
    embedding_dimensions = None

    def __init__(self, length: int = 2) -> None:
        self.length = length

    async def embed(self, text, model=None):
        return _vector([1.0, 0.0] if text == "prompt" else [0.6, 0.8], self.length)


def _measured_length(model: str) -> int:
    calibration = CALIBRATIONS.get(model) or (REFERENCE if model == REFERENCE_MODEL else None)
    return calibration.dimensions if calibration else 16


@pytest.fixture
def _fresh_cache():
    nova_engine.clear_pattern_embedding_cache()
    yield
    nova_engine.clear_pattern_embedding_cache()


@pytest.fixture(autouse=True)
def _unusable_marker(monkeypatch):
    """The Redis marker of an override that returned no embedding, in memory."""
    store: dict[str, str] = {}

    async def _marked(provider_id, override):
        return store.get("marker") == nova_engine._unusable_marker(provider_id, override)

    async def _mark(provider_id, override):
        store["marker"] = nova_engine._unusable_marker(provider_id, override)

    async def _clear():
        store.pop("marker", None)

    monkeypatch.setattr(nova_engine, "_override_marked_unusable", _marked)
    monkeypatch.setattr(nova_engine, "_mark_override_unusable", _mark)
    monkeypatch.setattr(nova_engine, "clear_override_unusable_marker", _clear)
    return store


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["nomic-embed-text", "embed-1", REFERENCE_MODEL])
async def test_the_engine_scores_on_the_reference_scale(monkeypatch, _fresh_cache, model):
    length = _measured_length(model)

    async def _get(_db):
        return _Provider(length), model, "cfg-1"

    monkeypatch.setattr("app.llm.router.get_embedding_provider", _get)
    rule = SimpleNamespace(semantics={"$s": _Pattern("pattern")})

    scores = await nova_engine._precompute_semantics(None, "prompt", [rule], None)

    assert scores == {"pattern": pytest.approx(calibrate_score(0.6, model))}


@pytest.mark.asyncio
async def test_the_engine_leaves_a_score_at_an_unmeasured_length_raw(monkeypatch, _fresh_cache):
    async def _get(_db):
        return _Provider(1536), "text-embedding-3-large", "cfg-1"

    monkeypatch.setattr("app.llm.router.get_embedding_provider", _get)
    rule = SimpleNamespace(semantics={"$s": _Pattern("pattern")})

    scores = await nova_engine._precompute_semantics(None, "prompt", [rule], None)

    assert scores == {"pattern": pytest.approx(0.6)}


@pytest.mark.asyncio
async def test_the_nova_override_is_the_model_calibrated_for(monkeypatch, _fresh_cache):
    async def _get(_db):
        return _Provider(CALIBRATIONS["text-embedding-3-large"].dimensions), "embed-1", "cfg-1"

    monkeypatch.setattr("app.llm.router.get_embedding_provider", _get)
    rule = SimpleNamespace(semantics={"$s": _Pattern("pattern")})

    scores = await nova_engine._precompute_semantics(None, "prompt", [rule], "text-embedding-3-large")

    assert scores == {"pattern": pytest.approx(calibrate_score(0.6, "text-embedding-3-large"))}
    assert scores["pattern"] != pytest.approx(0.6)


class _Scalar:
    def __init__(self, value) -> None:
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _ProviderRowSession:
    """Answers the lookup of the embedding provider's configuration row."""

    def __init__(self, provider: str) -> None:
        self.provider = provider

    async def execute(self, _statement):
        return _Scalar(self.provider)


def _scoring_env(monkeypatch, provider, *, provider_id=None, owner=None, default="embed-1"):
    async def _get(_db):
        return provider, default, provider_id

    async def _owner(_db, _model):
        return owner

    monkeypatch.setattr("app.llm.router.get_embedding_provider", _get)
    monkeypatch.setattr("app.llm.router.find_provider_for_model", _owner)


@pytest.mark.asyncio
async def test_the_scoring_model_is_the_override_else_the_platform_model(monkeypatch):
    async def _missing(_db):
        raise RuntimeError("no embedding provider")

    _scoring_env(monkeypatch, _Provider())
    override = nova_engine.NovaModels(semantic_model="m-2")
    assert await nova_engine.semantic_scoring(None, override) == nova_engine.SemanticScoring("m-2")
    assert await nova_engine.semantic_scoring(None, nova_engine.NovaModels()) == nova_engine.SemanticScoring("embed-1")
    monkeypatch.setattr("app.llm.router.get_embedding_provider", _missing)
    assert (await nova_engine.semantic_scoring(None, nova_engine.NovaModels())).model is None
    # An override with no provider to run it scores nothing, so it claims no calibration.
    assert (await nova_engine.semantic_scoring(None, override)).model is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("owner", "scored", "ignored"), [("ollama", "embed-1", "m-2"), ("openai", "m-2", None), (None, "m-2", None)]
)
async def test_an_override_owned_by_another_provider_is_reported_and_scores_on_the_platform_model(
    monkeypatch, owner, scored, ignored
):
    _scoring_env(monkeypatch, _Provider(), provider_id="cfg-1", owner=owner)
    models = nova_engine.NovaModels(semantic_model="m-2")
    scoring = await nova_engine.semantic_scoring(_ProviderRowSession("openai"), models)
    assert (scoring.model, scoring.override_ignored) == (scored, ignored)
    if ignored:
        assert scoring.override_reason == nova_engine.OVERRIDE_OF_ANOTHER_PROVIDER


@pytest.mark.asyncio
@pytest.mark.parametrize(("saved_for", "scored"), [("cfg-1", "m-2"), ("cfg-before", "embed-1")])
async def test_an_override_is_used_for_the_provider_it_was_checked_against_only(monkeypatch, saved_for, scored):
    """A recorded provider decides, whatever the owner lookup says."""
    _scoring_env(monkeypatch, _Provider(), provider_id="cfg-1", owner="ollama")
    models = nova_engine.NovaModels(semantic_model="m-2", semantic_provider_id=saved_for)
    assert (await nova_engine.semantic_scoring(_ProviderRowSession("openai"), models)).model == scored


class _PickyProvider(_Provider):
    """Embeds only the models it serves; records what it was asked for."""

    def __init__(self, serves: set[str], length: int = 2) -> None:
        super().__init__(length)
        self.serves = serves
        self.asked: list[str | None] = []

    async def embed(self, text, model=None):
        self.asked.append(model)
        return await super().embed(text, model) if model in self.serves else []


@pytest.mark.asyncio
async def test_an_override_that_returns_no_embedding_falls_back_and_is_reported(monkeypatch, _fresh_cache):
    """An override the owner lookup cannot place, served by
    no configured provider, used to leave every semantics: condition unmatched
    (block rules included) while nothing reported it."""
    length = CALIBRATIONS["text-embedding-3-large"].dimensions
    provider = _PickyProvider({"text-embedding-3-large"}, length)
    _scoring_env(monkeypatch, provider, provider_id="cfg-1", owner=None, default="text-embedding-3-large")
    db = _ProviderRowSession("openai")
    rule = SimpleNamespace(semantics={"$s": _Pattern("pattern")})

    scores = await nova_engine._precompute_semantics(db, "prompt", [rule], "not-a-served-model")

    assert scores == {"pattern": pytest.approx(calibrate_score(0.6, "text-embedding-3-large"))}
    assert provider.asked[0] == "not-a-served-model"
    assert set(provider.asked[1:]) == {"text-embedding-3-large"}
    scoring = await nova_engine.semantic_scoring(db, nova_engine.NovaModels(semantic_model="not-a-served-model"))
    assert scoring == nova_engine.SemanticScoring(
        "text-embedding-3-large",
        override_ignored="not-a-served-model",
        override_reason=nova_engine.OVERRIDE_RETURNS_NO_EMBEDDING,
    )

    provider.asked.clear()
    await nova_engine._precompute_semantics(db, "prompt", [rule], "not-a-served-model")
    assert set(provider.asked) == {"text-embedding-3-large"}, "the recorded fallback is not asked again"


def test_the_unusable_marker_names_one_provider_and_model_pair():
    assert nova_engine._unusable_marker("a|b", "c") != nova_engine._unusable_marker("a", "b|c")


@pytest.mark.asyncio
async def test_an_override_saved_for_another_provider_is_not_sent_to_this_one(monkeypatch, _fresh_cache):
    provider = _PickyProvider({"embed-1", "mistral-embed"})
    _scoring_env(monkeypatch, provider, provider_id="cfg-now")
    rule = SimpleNamespace(semantics={"$s": _Pattern("pattern")})

    await nova_engine._precompute_semantics(
        None, "prompt", [rule], "mistral-embed", semantic_provider_id="cfg-before"
    )

    assert set(provider.asked) == {"embed-1"}


@pytest.mark.asyncio
async def test_saving_an_override_checks_the_platform_provider_embeds_with_it(monkeypatch):
    from fastapi import HTTPException

    from app.api.v1.nova_rules import _semantic_override_provider

    _scoring_env(monkeypatch, _PickyProvider({"text-embedding-3-large"}), provider_id="cfg-9")
    assert await _semantic_override_provider(None, "text-embedding-3-large") == "cfg-9"
    with pytest.raises(HTTPException) as refused:
        await _semantic_override_provider(None, "mistral-embed")
    assert refused.value.status_code == 422
    assert "returned no embedding for `mistral-embed`" in refused.value.detail

    from app.llm.router import EmbeddingConfigurationError

    async def _missing(_db):
        raise EmbeddingConfigurationError("no embedding provider")

    monkeypatch.setattr("app.llm.router.get_embedding_provider", _missing)
    with pytest.raises(HTTPException) as none:
        await _semantic_override_provider(None, "text-embedding-3-large")
    assert none.value.status_code == 422 and "No embedding provider" in none.value.detail

    async def _broken(_db):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr("app.llm.router.get_embedding_provider", _broken)
    with pytest.raises(RuntimeError):
        await _semantic_override_provider(None, "text-embedding-3-large")


@pytest.mark.asyncio
async def test_a_provider_failure_during_the_check_is_not_called_an_invalid_model(monkeypatch):
    from fastapi import HTTPException

    from app.api.v1.nova_rules import _semantic_override_provider

    class _Down(_Provider):
        async def embed(self, text, model=None):
            raise TimeoutError("upstream timed out")

    class _Zero(_Provider):
        async def embed(self, text, model=None):
            return [0.0, 0.0]

    _scoring_env(monkeypatch, _Down(), provider_id="cfg-9")
    with pytest.raises(HTTPException) as down:
        await _semantic_override_provider(None, "text-embedding-3-large")
    assert down.value.status_code == 502 and "try again" in down.value.detail

    _scoring_env(monkeypatch, _Zero(), provider_id="cfg-9")
    with pytest.raises(HTTPException) as zero:
        await _semantic_override_provider(None, "text-embedding-3-large")
    assert zero.value.status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "configured", "expected"),
    [
        ("openai", 1536, 1536),
        ("openai", None, None),
        ("custom_openai", 1536, 1536),
        ("custom_openai", None, EMBEDDING_DIMENSIONS),
        ("other", 1536, None),
    ],
)
async def test_the_scoring_carries_the_length_the_provider_asks_for(monkeypatch, kind, configured, expected):
    from app.llm.custom_openai_provider import CustomOpenAIProvider
    from app.llm.openai_provider import OpenAIProvider

    cls = {"openai": OpenAIProvider, "custom_openai": CustomOpenAIProvider, "other": _Provider}[kind]
    provider = cls.__new__(cls)
    provider.embedding_dimensions = configured
    _scoring_env(monkeypatch, provider)
    scoring = await nova_engine.semantic_scoring(None, nova_engine.NovaModels())
    assert scoring == nova_engine.SemanticScoring("embed-1", expected)


# ---------------------------------------------------------------------------
# The rules tab: engine status and the per-rule flag
# ---------------------------------------------------------------------------


_SEMANTIC_RULE = (
    'rule Semantic { meta: category = "Test"\n'
    '  semantics: $s = "a pattern" (0.4)\n'
    "  condition: semantics.$s }"
)
_KEYWORD_RULE = 'rule Keyword { meta: category = "Test"\n  keywords: $k = "placeholder"\n  condition: keywords.$k }'


def _row(text: str) -> SimpleNamespace:
    now = datetime.now(UTC)
    return SimpleNamespace(
        id="r1",
        user_id=None,
        name="R",
        description="",
        category="Jailbreak",
        action="log",
        rule_text=text,
        enabled=True,
        tags=[],
        is_company_managed=True,
        shared_group_ids=None,
        shared_group_permissions=None,
        created_at=now,
        updated_at=now,
    )


def test_a_rule_says_whether_it_has_semantics():
    assert _to_response(_row(_SEMANTIC_RULE)).has_semantics is True
    assert _to_response(_row(_KEYWORD_RULE)).has_semantics is False
    assert has_semantic_patterns("not a rule") is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model", "calibrated", "ignored", "reason"),
    [
        ("embed-1", False, None, None),
        ("text-embedding-3-large", True, None, None),
        ("text-embedding-3-large", True, "m-2", "no_embedding"),
        (None, False, None, None),
    ],
)
async def test_the_engine_status_reports_the_scoring_models_calibration(monkeypatch, model, calibrated, ignored, reason):
    async def _models(_db):
        return nova_engine.NovaModels()

    async def _scoring(_db, _models=None):
        return nova_engine.SemanticScoring(model, override_ignored=ignored, override_reason=reason)

    monkeypatch.setattr(nova_engine, "resolve_nova_models", _models)
    monkeypatch.setattr(nova_engine, "semantic_scoring", _scoring)

    status = await nova_engine_status(db=None, _user=None, _agentic_gate=None)

    assert status["semantic_calibration"] == {
        "calibrated_for": REFERENCE_MODEL,
        "configured_model": model,
        "calibrated": calibrated,
        "override_ignored": ignored,
        "override_reason": reason,
    }


# ---------------------------------------------------------------------------
# Rule validation: what the number after a pattern means
# ---------------------------------------------------------------------------


def _with(tier: str, value: str) -> str:
    return f'rule V {{ meta: category = "Test"\n  {tier}: $p = "a pattern" ({value})\n  condition: {tier}.$p }}'


@pytest.mark.parametrize("tier", ["semantics", "llm"])
@pytest.mark.parametrize("value", ["0", "0.35", "1", "1.0"])
def test_a_value_between_0_and_1_is_accepted(tier, value):
    assert parse_rule(_with(tier, value)).name == "V"


@pytest.mark.parametrize(
    ("tier", "meaning"),
    [
        ("semantics", "the minimum similarity between the message and the pattern"),
        ("llm", "the temperature of the model's yes/no call, not a confidence threshold"),
    ],
)
@pytest.mark.parametrize("value", ["1.5", "2", "0.4.1", "-1", "-0.2", "2e0", "nan", "inf", ""])
def test_a_value_outside_0_and_1_is_refused_with_its_meaning(tier, meaning, value):
    with pytest.raises(NovaRuleError) as refused:
        parse_rule(_with(tier, value))
    assert f"{tier} $p: `{value}` is not a number between 0 and 1" in str(refused.value)
    assert meaning in str(refused.value)


def test_a_pattern_with_no_value_keeps_the_engine_default():
    rule = 'rule D { meta: category = "Test"\n  semantics: $p = "a pattern"\n  condition: semantics.$p }'
    assert parse_rule(rule).name == "D"


# ---------------------------------------------------------------------------
# The Security Audit finding
# ---------------------------------------------------------------------------


class _Rows:
    def __init__(self, rows) -> None:
        self._rows = rows

    def all(self):
        return self._rows


class _AuditSession:
    def __init__(self, rows) -> None:
        self.rows = rows

    async def execute(self, _statement):
        return _Rows(self.rows)


def _audit_env(
    monkeypatch, *, model="embed-1", dimensions=None, engine=True, connected=True, ignored=None, reason=None
):
    async def _connected(_db, _provider):
        return connected

    async def _scoring(_db, _models=None):
        return nova_engine.SemanticScoring(model, dimensions, override_ignored=ignored, override_reason=reason)

    monkeypatch.setattr(nova_engine, "NOVA_HUNTING_AVAILABLE", engine)
    monkeypatch.setattr("app.services.integration_state.any_provider_connected", _connected)
    monkeypatch.setattr(nova_engine, "semantic_scoring", _scoring)


async def _audit(rows) -> list:
    findings: list = []
    await security_audit._check_nova_semantic_calibration(_AuditSession(rows), findings)
    return findings


@pytest.mark.asyncio
async def test_block_rules_with_semantics_on_an_uncalibrated_model_are_a_warning(monkeypatch):
    _audit_env(monkeypatch)
    [finding] = await _audit([("Blocker", "block", _SEMANTIC_RULE), ("Logger", "log", _SEMANTIC_RULE)])
    assert (finding.id, finding.category, finding.severity) == (
        "agents.nova_semantics_uncalibrated_block_rules",
        "agents",
        "warning",
    )
    assert "Blocker" in finding.description and "Logger" not in finding.description
    assert "scripts/calibrate_nova_semantics.py" in finding.recommendation


@pytest.mark.asyncio
async def test_log_only_rules_with_semantics_on_an_uncalibrated_model_are_info(monkeypatch):
    _audit_env(monkeypatch)
    [finding] = await _audit([("Logger", "log", _SEMANTIC_RULE), ("Keyword", "block", _KEYWORD_RULE)])
    assert (finding.id, finding.severity) == ("agents.nova_semantics_uncalibrated", "info")


@pytest.mark.asyncio
async def test_a_calibrated_model_at_another_length_is_reported_uncalibrated(monkeypatch):
    _audit_env(monkeypatch, model="text-embedding-3-large", dimensions=1536)
    [finding] = await _audit([("Logger", "log", _SEMANTIC_RULE)])
    assert (finding.id, finding.severity) == ("agents.nova_semantics_uncalibrated", "info")
    assert "text-embedding-3-large at 1536 dimensions" in finding.title


@pytest.mark.asyncio
async def test_a_calibrated_model_passes(monkeypatch):
    _audit_env(monkeypatch, model="nomic-embed-text")
    [finding] = await _audit([("Blocker", "block", _SEMANTIC_RULE)])
    assert (finding.id, finding.severity) == ("agents.nova_semantics_calibrated", "pass")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reason", "said"),
    [
        (nova_engine.OVERRIDE_OF_ANOTHER_PROVIDER, "was chosen for another embedding provider"),
        (nova_engine.OVERRIDE_RETURNS_NO_EMBEDDING, "returned no embedding"),
    ],
)
async def test_an_override_that_is_not_used_is_a_warning_next_to_the_scoring_models_calibration(
    monkeypatch, reason, said
):
    """The audit used to go silent when the override was
    of another provider, while the engine left semantics: block rules unevaluated."""
    _audit_env(monkeypatch, model="nomic-embed-text", ignored="mistral-embed", reason=reason)
    ignored, calibrated = await _audit([("Blocker", "block", _SEMANTIC_RULE)])
    assert (ignored.id, ignored.category, ignored.severity) == (
        "agents.nova_semantic_override_ignored",
        "agents",
        "warning",
    )
    assert "mistral-embed" in ignored.title and said in ignored.description
    assert "nomic-embed-text" in ignored.description
    assert (calibrated.id, calibrated.severity) == ("agents.nova_semantics_calibrated", "pass")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("env", "rows"),
    [
        ({"engine": False}, [("Blocker", "block", _SEMANTIC_RULE)]),
        ({"connected": False}, [("Blocker", "block", _SEMANTIC_RULE)]),
        ({"model": None}, [("Blocker", "block", _SEMANTIC_RULE)]),
        ({}, [("Keyword", "block", _KEYWORD_RULE)]),
        ({}, []),
    ],
)
async def test_nothing_is_reported_where_the_semantics_tier_scores_nothing(monkeypatch, env, rows):
    _audit_env(monkeypatch, **env)
    assert await _audit(rows) == []
