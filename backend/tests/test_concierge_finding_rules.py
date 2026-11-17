"""The pure rules of Concierge findings and variants (#8259): fingerprint, keys,
evidence merge, the #1044 length rule and template placeholders."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.services.concierge.findings import (
    MAX_EVIDENCE_IDS,
    Evidence,
    FindingError,
    clip_text,
    fingerprint,
    merge_evidence,
    normalize_pattern_key,
)
from app.services.concierge.variants import MAX_VARIANT_CHARS, VariantError, check_variant, placeholders

AGENT = uuid.UUID("11111111-1111-1111-1111-111111111111")


class TestPatternKey:
    @pytest.mark.parametrize(
        ("kind", "key", "expected"),
        [
            ("behaviour", "  Answers In The Wrong Language!  ", "answers-in-the-wrong-language"),
            ("behaviour", "skips__the_summary", "skips__the_summary"),
            ("failure_cause", " Tool_Timeout ", "tool_timeout"),
            ("tool_errors", "search_knowledge", "search_knowledge"),
            ("cost_drift", "ignored", ""),
            ("latency", None, ""),
            ("negative_feedback", "", ""),
            (
                "low_criterion",
                "AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA",
                "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            ),
        ],
    )
    def test_normalized(self, kind: str, key: str | None, expected: str) -> None:
        assert normalize_pattern_key(kind, key) == expected

    @pytest.mark.parametrize(
        ("kind", "key"),
        [
            ("unknown_kind", "x"),
            ("behaviour", "!!!"),
            ("low_criterion", "not-a-uuid"),
            ("evaluation_weakness", ""),
            ("negative_feedback", "not-a-uuid"),
            ("tool_errors", ""),
            ("tool_errors", "x" * 129),
            ("failure_cause", "\x00\x01"),
        ],
    )
    def test_refused(self, kind: str, key: str) -> None:
        with pytest.raises(FindingError):
            normalize_pattern_key(kind, key)

    def test_slug_is_bounded(self) -> None:
        assert len(normalize_pattern_key("behaviour", "a" * 500)) == 64


class TestFingerprint:
    def test_same_pattern_same_fingerprint(self) -> None:
        first = fingerprint("agent", AGENT, "behaviour", normalize_pattern_key("behaviour", "Wrong Language"))
        again = fingerprint("agent", AGENT, "behaviour", normalize_pattern_key("behaviour", "wrong-language"))
        assert first == again
        assert len(first) == 64

    @pytest.mark.parametrize(
        "other",
        [
            ("assignment", AGENT, "behaviour", "wrong-language"),
            ("agent", uuid.UUID(int=2), "behaviour", "wrong-language"),
            ("agent", AGENT, "tool_errors", "wrong-language"),
            ("agent", AGENT, "behaviour", "wrong-tone"),
        ],
    )
    def test_any_part_changes_it(self, other: tuple) -> None:
        assert fingerprint(*other) != fingerprint("agent", AGENT, "behaviour", "wrong-language")


class TestEvidenceMerge:
    def test_fresh_first_no_repeat_capped(self) -> None:
        stored = {"trace_ids": [f"t{i}" for i in range(MAX_EVIDENCE_IDS)], "counts": {"runs": 4}}
        merged = merge_evidence(stored, Evidence(trace_ids=("new", "t0"), counts={"runs": 9}))
        assert merged["trace_ids"][:3] == ["new", "t0", "t1"]
        assert len(merged["trace_ids"]) == MAX_EVIDENCE_IDS
        assert merged["counts"] == {"runs": 9}
        assert merged["rating_ids"] == []
        assert merged["run_ids"] == []

    def test_counts_kept_when_fresh_has_none(self) -> None:
        merged = merge_evidence({"counts": {"runs": 4}}, Evidence(rating_ids=("r1",)))
        assert merged == {"trace_ids": [], "rating_ids": ["r1"], "run_ids": [], "counts": {"runs": 4}}


def test_clip_text_drops_control_characters_keeps_lines() -> None:
    assert clip_text("a\x00b\r\nc\td\x1b") == "ab\nc\td"
    assert clip_text("x" * 30, limit=10) == "x" * 10 + " [...]"
    assert clip_text(None) == ""


class TestVariant:
    def test_accepted(self) -> None:
        variant = check_variant("persona", "You help {{user.name}}.", "You help {{user.name}}, briefly.", " why ")
        assert variant.text == "You help {{user.name}}, briefly."
        assert variant.summary == "why"

    @pytest.mark.parametrize(
        ("change_type", "base", "text", "summary"),
        [
            ("flow_repair", "a", "b", "s"),
            ("persona", "same", " same ", "s"),
            ("persona", "base", "", "s"),
            ("persona", "base", "changed", ""),
            ("assignment_prompt", "Read {{trigger.body}}", "Read the body", "s"),
            ("skill", "Plain", "Plain {{trigger.body}}", "s"),
            ("persona", "x" * 100, "y" * 2_101, "s"),
            ("skill", "x" * 10_000, "y" * 15_001, "s"),
            ("persona", "x" * (MAX_VARIANT_CHARS + 1), "y", "s"),
        ],
    )
    def test_refused(self, change_type: str, base: str, text: str, summary: str) -> None:
        with pytest.raises(VariantError):
            check_variant(change_type, base, text, summary)

    def test_growth_limit_is_inclusive(self) -> None:
        assert len(check_variant("skill", "x" * 10_000, "y" * 15_000, "s").text) == 15_000
        assert len(check_variant("persona", "x" * 100, "y" * 2_100, "s").text) == 2_100

    def test_placeholders_trimmed(self) -> None:
        assert placeholders("{{ trigger.body }} and {{var_id.abc}}") == frozenset({"trigger.body", "var_id.abc"})


class TestTargetAccess:
    @pytest.mark.asyncio
    async def test_a_registered_rule_reads_a_target_that_is_no_agent_s(self, monkeypatch) -> None:
        from app.services.concierge import access

        flow_id = uuid.UUID(int=7)
        granted = access.AgentAccess(agent=None, content=False)
        seen: list[uuid.UUID] = []

        async def _rule(_db, _reader, target_id):
            seen.append(target_id)
            return granted

        monkeypatch.setattr(access, "_TARGET_RULES", {})
        assert await access.target_access(None, None, "flow", flow_id) is None
        access.register_target_access("flow", _rule)
        assert await access.target_access(None, None, "flow", flow_id) is granted
        assert seen == [flow_id]


def test_evidence_serializes_every_ref_kind() -> None:
    evidence = Evidence(trace_ids=("t",), rating_ids=("r",), run_ids=("u",), counts={"n": 2})
    assert evidence.as_json() == {"trace_ids": ["t"], "rating_ids": ["r"], "run_ids": ["u"], "counts": {"n": 2}}


class TestReadRules:
    def test_a_content_finding_is_redacted_without_content_access(self) -> None:
        from app.services.concierge.access import redact_finding

        payload = {
            "hypothesis": "h",
            "suggested_experiment": "e",
            "content_evidence": True,
            "evidence": {"trace_ids": ["t"], "rating_ids": ["r"], "run_ids": ["u"], "counts": {"n": 1}},
        }
        assert redact_finding(payload, content=True) is payload
        redacted = redact_finding(payload, content=False)
        assert redacted == {
            "hypothesis": "",
            "suggested_experiment": "",
            "content_evidence": True,
            "evidence": {"trace_ids": [], "rating_ids": [], "run_ids": ["u"], "counts": {"n": 1}},
            "redacted": True,
        }
        counts_only = {**payload, "content_evidence": False}
        assert redact_finding(counts_only, content=False) is counts_only

    @pytest.mark.parametrize(
        ("principal", "role", "refused"),
        [
            (None, "quality_analyst", True),
            (SimpleNamespace(agent_id=AGENT, role="experimenter"), "quality_analyst", True),
            (SimpleNamespace(agent_id=AGENT, role="quality_analyst"), "quality_analyst", False),
        ],
    )
    def test_writes_are_held_by_one_role_of_the_concierge(self, principal, role, refused) -> None:
        from app.services.concierge.access import WRITE_REFUSED, Reader

        scope = SimpleNamespace(opted_out_user_ids=frozenset())
        reader = Reader(user=None, principal=principal, scope=scope)
        assert reader.write_refusal(role) == (WRITE_REFUSED if refused else None)
        assert reader.is_concierge is (principal is not None)


@pytest.mark.asyncio
async def test_nobody_to_read_for_reads_nothing(monkeypatch) -> None:
    from app.services.concierge import access

    monkeypatch.setattr(access, "_current_principal", lambda _agent_id: None)
    assert await access.load_reader(None, None) is None
    assert await access.load_reader(None, "anonymous") is None
