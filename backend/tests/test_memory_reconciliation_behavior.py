"""Behavioral tests for ``reconcile_and_persist_facts`` decision handling.

The reconciliation pipeline extracts candidate facts, embeds them, finds
existing facts in the "contradiction zone" and asks an LLM curator to decide
ADD / SKIP / SUPERSEDE / MERGE per candidate.  These tests stub the curator
LLM and the embedding function with deterministic values and verify:

* ADD persists a new fact row.
* SKIP does not persist anything.
* SUPERSEDE deletes the targeted existing ids, then persists the new fact.
* MERGE replaces the originals with the merged content (source
  ``reconciliation``).
* Malformed curator JSON falls back to plain ADD for every candidate.
* The 0.92 dedup short-circuit inside ``persist_memory`` skips
  near-identical facts.
* Hallucinated superseded ids (not present in the related set) are dropped.
* The same existing id cannot be consumed by two candidates in one batch.
* Per-user vs agent-level scoping is respected end to end.

Assessment finding: OBS-8 (critical untested modules).
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.sql.dml import Delete

import app.services.persistent_memory as pm
from app.models.agent_memory_entry import AgentMemoryEntry

AGENT_ID = "00000000-0000-0000-0000-00000000aaaa"
USER_ID = "00000000-0000-0000-0000-00000000bbbb"

_VEC = [1.0, 0.0, 0.0]


class FakeDB:
    """Minimal AsyncSession stand-in recording adds and executed statements."""

    def __init__(self) -> None:
        self.added: list[AgentMemoryEntry] = []
        self.executed: list = []

    def add(self, obj) -> None:
        self.added.append(obj)

    async def execute(self, stmt):
        self.executed.append(stmt)
        return SimpleNamespace(
            scalar_one_or_none=lambda: None,
            scalars=lambda: SimpleNamespace(all=lambda: []),
            fetchall=lambda: [],
        )

    async def flush(self) -> None:
        return None

    def in_transaction(self) -> bool:
        # The commit-release points only commit when a transaction is
        # open; the fake session never opens one.
        return False

    async def commit(self) -> None:
        return None

    def deleted_ids(self) -> set[str]:
        """Flatten the bind-parameter values of every executed DELETE."""
        ids: set[str] = set()
        for stmt in self.executed:
            if not isinstance(stmt, Delete):
                continue
            for value in stmt.compile().params.values():
                if isinstance(value, (list, tuple)):
                    ids.update(str(v) for v in value)
                elif value is not None:
                    ids.add(str(value))
        return ids


def _related(entry_id: str, content: str, score: float = 0.8) -> dict:
    return {"id": entry_id, "content": content, "score": score}


def _curator_json(decisions: list[dict]) -> str:
    return json.dumps({"decisions": decisions})


async def _run_reconcile(
    *,
    facts: list[str],
    related_by_fact: list[list[dict]],
    curator_response: str | None = None,
    curator_error: Exception | None = None,
    user_id: str | None = None,
    duplicate: bool = False,
) -> tuple[dict, FakeDB, AsyncMock]:
    """Run ``reconcile_and_persist_facts`` with all external seams stubbed."""
    db = FakeDB()

    if curator_error is not None:
        chat = AsyncMock(side_effect=curator_error)
    else:
        chat = AsyncMock(
            return_value=SimpleNamespace(content=curator_response or "")
        )
    llm = SimpleNamespace(chat=chat)

    with (
        patch.object(
            pm, "_extract_candidate_facts", new=AsyncMock(return_value=facts)
        ),
        patch.object(pm, "embed_text", new=AsyncMock(return_value=_VEC)),
        # ``persist_memory`` resolves embeddings through the provenance-aware
        # ``embed_text_with_model`` seam (embedding model tracking) —
        # stub it too so the dedup short-circuit sees a real vector instead
        # of a failed embed (the fake provider tuple has no ``.embed``).
        patch.object(
            pm,
            "embed_text_with_model",
            new=AsyncMock(return_value=(_VEC, "text-embedding-3-small")),
        ),
        patch.object(
            pm, "_find_related_facts", new=AsyncMock(side_effect=related_by_fact)
        ) as find_related,
        patch.object(
            pm, "_is_duplicate_fact", new=AsyncMock(return_value=duplicate)
        ),
        patch(
            "app.llm.router.get_embedding_provider",
            new=AsyncMock(return_value=("openai", "text-embedding-3-small")),
        ),
    ):
        summary = await pm.reconcile_and_persist_facts(
            db,
            llm,  # type: ignore[arg-type]
            agent_id=AGENT_ID,
            messages_text="conversation text",
            user_id=user_id,
        )
    _run_reconcile.last_find_related = find_related  # type: ignore[attr-defined]
    return summary, db, chat


# ---------------------------------------------------------------------------
# ADD
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_persists_new_fact():
    fact = "The user wants ADS metrics computed as YTD averages."
    summary, db, chat = await _run_reconcile(
        facts=[fact],
        related_by_fact=[[]],  # no related facts -> curator skipped
    )

    assert summary == {
        "added": 1,
        "superseded": 0,
        "skipped": 0,
        "merged": 0,
        "deleted": 0,
        "curator_degraded": False,
    }
    assert len(db.added) == 1
    entry = db.added[0]
    assert entry.content == fact
    assert entry.memory_type == "fact"
    assert entry.source == "compaction"
    # No related facts anywhere -> the curator LLM must not be called.
    chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_curator_add_decision_persists():
    fact = "Reports must use the subregion field filter."
    rel = _related(str(uuid.uuid4()), "Reports use a region filter.")
    summary, db, chat = await _run_reconcile(
        facts=[fact],
        related_by_fact=[[rel]],
        curator_response=_curator_json(
            [{"new_fact": fact, "action": "ADD"}]
        ),
    )

    assert summary["added"] == 1
    assert len(db.added) == 1
    chat.assert_awaited_once()


# ---------------------------------------------------------------------------
# SKIP
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_skip_does_not_persist():
    fact = "The currency prefix is EUR."
    rel = _related(str(uuid.uuid4()), "All amounts are displayed in EUR.")
    summary, db, _ = await _run_reconcile(
        facts=[fact],
        related_by_fact=[[rel]],
        curator_response=_curator_json(
            [{"new_fact": fact, "action": "SKIP"}]
        ),
    )

    assert summary["skipped"] == 1
    assert summary["added"] == 0
    assert db.added == []
    assert db.deleted_ids() == set()


# ---------------------------------------------------------------------------
# SUPERSEDE
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_supersede_deletes_targets_then_persists():
    old_id = str(uuid.uuid4())
    fact = "Use the subregion filter, not the country filter."
    rel = _related(old_id, "Use the country filter for reports.")
    summary, db, _ = await _run_reconcile(
        facts=[fact],
        related_by_fact=[[rel]],
        curator_response=_curator_json(
            [
                {
                    "new_fact": fact,
                    "action": "SUPERSEDE",
                    "superseded_ids": [old_id],
                }
            ]
        ),
    )

    assert summary["superseded"] == 1
    assert summary["deleted"] == 1
    assert summary["added"] == 0
    # The old fact row was deleted...
    assert old_id in db.deleted_ids()
    # ...and the corrected fact was persisted in its place.
    assert len(db.added) == 1
    assert db.added[0].content == fact
    assert db.added[0].source == "compaction"


@pytest.mark.asyncio
async def test_supersede_with_hallucinated_id_is_dropped():
    """Ids not present in the candidate's related set must be ignored.

    MEM-6: when ALL proposed ids are invalid the decision is downgraded to
    a plain ADD (old facts retained) instead of being counted as superseded.
    """
    fact = "The dashboard refresh interval is 15 minutes."
    rel = _related(str(uuid.uuid4()), "Dashboard refresh is 5 minutes.")
    hallucinated = str(uuid.uuid4())
    summary, db, _ = await _run_reconcile(
        facts=[fact],
        related_by_fact=[[rel]],
        curator_response=_curator_json(
            [
                {
                    "new_fact": fact,
                    "action": "SUPERSEDE",
                    "superseded_ids": [hallucinated],
                }
            ]
        ),
    )

    # Nothing deleted (the only listed id was hallucinated)...
    assert db.deleted_ids() == set()
    assert summary["deleted"] == 0
    # ...and the decision is downgraded to ADD - the new fact is persisted
    # but NOT miscounted as superseded (no old fact was actually replaced).
    assert summary["superseded"] == 0
    assert summary["added"] == 1
    assert len(db.added) == 1


@pytest.mark.asyncio
async def test_same_superseded_id_consumed_only_once_per_batch():
    """One existing id mapped to two candidates must be deleted only once."""
    shared_id = str(uuid.uuid4())
    fact_a = "Weekly digests are sent on Monday mornings."
    fact_b = "Weekly digests must include a KPI summary table."
    rel_a = [_related(shared_id, "Digests are sent on Fridays.")]
    rel_b = [_related(shared_id, "Digests are sent on Fridays.", score=0.7)]
    summary, db, _ = await _run_reconcile(
        facts=[fact_a, fact_b],
        related_by_fact=[rel_a, rel_b],
        curator_response=_curator_json(
            [
                {
                    "new_fact": fact_a,
                    "action": "SUPERSEDE",
                    "superseded_ids": [shared_id],
                },
                {
                    "new_fact": fact_b,
                    "action": "SUPERSEDE",
                    "superseded_ids": [shared_id],
                },
            ]
        ),
    )

    # The shared id is deleted exactly once.
    delete_stmts = [s for s in db.executed if isinstance(s, Delete)]
    assert len(delete_stmts) == 1
    assert db.deleted_ids() == {shared_id}
    assert summary["deleted"] == 1
    # Both new facts are persisted, but only the first counts as SUPERSEDE -
    # the second candidate's only id was already consumed, so its decision is
    # downgraded to ADD (MEM-6: it did not actually replace anything).
    assert summary["superseded"] == 1
    assert summary["added"] == 1
    assert len(db.added) == 2


# ---------------------------------------------------------------------------
# MERGE
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_merge_replaces_originals_with_merged_content():
    old_id = str(uuid.uuid4())
    fact = "The user also wants the digest in Slack."
    merged = "The user wants the digest by email and in Slack."
    rel = _related(old_id, "The user wants the digest by email.")
    summary, db, _ = await _run_reconcile(
        facts=[fact],
        related_by_fact=[[rel]],
        curator_response=_curator_json(
            [
                {
                    "new_fact": fact,
                    "action": "MERGE",
                    "superseded_ids": [old_id],
                    "merged_content": merged,
                }
            ]
        ),
    )

    assert summary["merged"] == 1
    assert summary["deleted"] == 1
    assert old_id in db.deleted_ids()
    assert len(db.added) == 1
    # The merged statement replaces the candidate text...
    assert db.added[0].content == merged
    # ...and is attributed to the reconciliation source.
    assert db.added[0].source == "reconciliation"


@pytest.mark.asyncio
async def test_merge_without_merged_content_falls_back_to_fact_text():
    old_id = str(uuid.uuid4())
    fact = "Digest must include open incident counts."
    rel = _related(old_id, "Digest includes incident stats.")
    summary, db, _ = await _run_reconcile(
        facts=[fact],
        related_by_fact=[[rel]],
        curator_response=_curator_json(
            [
                {
                    "new_fact": fact,
                    "action": "MERGE",
                    "superseded_ids": [old_id],
                    # merged_content intentionally missing
                }
            ]
        ),
    )

    assert summary["merged"] == 1
    assert db.added[0].content == fact


@pytest.mark.asyncio
@pytest.mark.parametrize("leading", ["", "Digest includes counts per severity."])
async def test_merge_content_loses_its_leading_reasoning_before_the_delete(leading):
    """A reasoning-only merged_content falls back to the fact, never to nothing (#12356)."""
    old_id = str(uuid.uuid4())
    fact = "Digest must include open incident counts."
    rel = _related(old_id, "Digest includes incident stats.")
    _summary, db, _ = await _run_reconcile(
        facts=[fact],
        related_by_fact=[[rel]],
        curator_response=_curator_json(
            [
                {
                    "new_fact": fact,
                    "action": "MERGE",
                    "superseded_ids": [old_id],
                    "merged_content": f"<reasoning>combine both</reasoning>{leading}",
                }
            ]
        ),
    )

    assert old_id in db.deleted_ids()
    assert db.added[0].content == (leading or fact)


# ---------------------------------------------------------------------------
# Curator failure modes -> plain ADD fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_malformed_curator_json_falls_back_to_add():
    fact = "All exports must be CSV with semicolon separators."
    rel = _related(str(uuid.uuid4()), "Exports are CSV files.")
    summary, db, chat = await _run_reconcile(
        facts=[fact],
        related_by_fact=[[rel]],
        curator_response="I think you should probably keep both facts!",
    )

    # MEM-5: an unparseable first response is retried once with the terser
    # JSON-only reprompt before falling back to plain ADD.
    assert chat.await_count == 2
    assert summary["curator_degraded"] is True
    assert summary["added"] == 1
    assert summary["superseded"] == 0
    assert db.deleted_ids() == set()
    assert len(db.added) == 1


@pytest.mark.asyncio
async def test_fenced_curator_json_with_preamble_accepted_first_attempt():
    # A fenced, prose-wrapped curator reply must be
    # recovered on the FIRST attempt instead of burning the retry and
    # degrading to ADD-all.
    fact = "Use the subregion field filter, not country."
    old_id = str(uuid.uuid4())
    rel = _related(old_id, "Use the country field filter.")
    raw = (
        "Here are my decisions:\n```json\n"
        + _curator_json(
            [
                {
                    "new_fact": fact,
                    "action": "SUPERSEDE",
                    "superseded_ids": [old_id],
                }
            ]
        )
        + "\n```"
    )
    summary, db, chat = await _run_reconcile(
        facts=[fact],
        related_by_fact=[[rel]],
        curator_response=raw,
    )

    assert chat.await_count == 1
    assert summary["curator_degraded"] is False
    assert summary["superseded"] == 1
    assert db.deleted_ids() == {old_id}
    assert len(db.added) == 1


@pytest.mark.asyncio
async def test_curator_exception_falls_back_to_add():
    fact = "Notifications go to the #ops channel."
    rel = _related(str(uuid.uuid4()), "Notifications go to #general.")
    summary, db, _ = await _run_reconcile(
        facts=[fact],
        related_by_fact=[[rel]],
        curator_error=RuntimeError("LLM unavailable"),
    )

    assert summary["added"] == 1
    assert db.deleted_ids() == set()
    assert len(db.added) == 1


# ---------------------------------------------------------------------------
# Dedup short-circuit (0.92 cosine threshold inside persist_memory)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dedup_short_circuit_skips_near_identical_fact():
    fact = "The user's main repository is xtm-one."
    summary, db, _ = await _run_reconcile(
        facts=[fact],
        related_by_fact=[[]],
        duplicate=True,  # _is_duplicate_fact -> True (similarity >= 0.92)
    )

    assert summary == {
        "added": 0,
        "superseded": 0,
        "skipped": 1,
        "merged": 0,
        "deleted": 0,
        "curator_degraded": False,
    }
    assert db.added == []


# ---------------------------------------------------------------------------
# Scoping — per-user vs agent-level
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_user_scoped_fact_carries_user_id():
    fact = "This user prefers short bullet-point answers."
    summary, db, _ = await _run_reconcile(
        facts=[fact],
        related_by_fact=[[]],
        user_id=USER_ID,
    )

    assert summary["added"] == 1
    entry = db.added[0]
    assert entry.user_id == uuid.UUID(USER_ID)
    assert entry.agent_id == uuid.UUID(AGENT_ID)
    # Related-fact lookup must search within the user scope.
    find_related: AsyncMock = _run_reconcile.last_find_related  # type: ignore[attr-defined]
    assert find_related.await_args.kwargs["user_id"] == uuid.UUID(USER_ID)


@pytest.mark.asyncio
async def test_agent_level_fact_has_no_user_id():
    fact = "The deployment window is Friday 18:00 UTC."
    summary, db, _ = await _run_reconcile(
        facts=[fact],
        related_by_fact=[[]],
        user_id=None,
    )

    assert summary["added"] == 1
    entry = db.added[0]
    assert entry.user_id is None
    assert entry.agent_id == uuid.UUID(AGENT_ID)
    find_related: AsyncMock = _run_reconcile.last_find_related  # type: ignore[attr-defined]
    assert find_related.await_args.kwargs["user_id"] is None


# ---------------------------------------------------------------------------
# No candidate facts -> empty summary, no LLM / DB traffic
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_facts_returns_empty_summary():
    summary, db, chat = await _run_reconcile(facts=[], related_by_fact=[])

    assert summary == {
        "added": 0,
        "superseded": 0,
        "skipped": 0,
        "merged": 0,
        "deleted": 0,
        "curator_degraded": False,
    }
    assert db.added == []
    assert db.executed == []
    chat.assert_not_awaited()


# ---------------------------------------------------------------------------
# _parse_reconcile_response — curator output parsing
# ---------------------------------------------------------------------------


class TestParseReconcileResponse:
    def test_parses_plain_json(self):
        raw = _curator_json([{"new_fact": "f", "action": "ADD"}])
        decisions = pm._parse_reconcile_response(raw)
        assert decisions == [{"new_fact": "f", "action": "ADD"}]

    def test_strips_code_fences(self):
        raw = "```json\n" + _curator_json(
            [{"new_fact": "f", "action": "SKIP"}]
        ) + "\n```"
        decisions = pm._parse_reconcile_response(raw)
        assert decisions[0]["action"] == "SKIP"

    def test_extracts_json_object_from_surrounding_prose(self):
        raw = (
            "Here are my decisions: "
            + _curator_json([{"new_fact": "f", "action": "ADD"}])
            + " Hope this helps!"
        )
        decisions = pm._parse_reconcile_response(raw)
        assert len(decisions) == 1

    def test_parses_fenced_json_with_preamble(self):
        # Production failure mode: a preamble sentence followed by a
        # fenced JSON payload used to defeat the fence-strip + brace-slice
        # parser and force the ADD-all fallback.
        raw = (
            "Sure! Here are my reconciliation decisions:\n\n```json\n"
            + _curator_json([{"new_fact": "f", "action": "SUPERSEDE"}])
            + "\n```\nLet me know if you need anything else."
        )
        decisions = pm._parse_reconcile_response(raw)
        assert decisions == [{"new_fact": "f", "action": "SUPERSEDE"}]

    def test_repairs_single_quoted_json(self):
        raw = "{'decisions': [{'new_fact': 'f', 'action': 'ADD'}]}"
        decisions = pm._parse_reconcile_response(raw)
        assert decisions == [{"new_fact": "f", "action": "ADD"}]

    def test_repairs_trailing_comma_json(self):
        raw = '{"decisions": [{"new_fact": "f", "action": "SKIP"},]}'
        decisions = pm._parse_reconcile_response(raw)
        assert decisions == [{"new_fact": "f", "action": "SKIP"}]

    def test_non_json_returns_empty_and_warns(self, caplog):
        # The ADD-all degradation must stay observable: garbage
        # input returns [] AND emits the production warning.
        with caplog.at_level("WARNING", logger="app.services.persistent_memory"):
            assert pm._parse_reconcile_response("definitely not json") == []
        assert any("non-JSON output" in r.message for r in caplog.records)

    def test_empty_and_none_safe(self):
        assert pm._parse_reconcile_response("") == []
        assert pm._parse_reconcile_response(None) == []  # type: ignore[arg-type]

    def test_bare_top_level_array_tolerated(self):
        # Models occasionally drop the {"decisions": ...} wrapper; the bare
        # array is accepted (the caller's candidate-text validation drops
        # decisions that reference no real candidate).
        raw = '[{"new_fact": "f", "action": "ADD"}]'
        assert pm._parse_reconcile_response(raw) == [
            {"new_fact": "f", "action": "ADD"}
        ]

    def test_decisions_not_a_list_returns_empty_and_warns(self, caplog):
        with caplog.at_level("WARNING", logger="app.services.persistent_memory"):
            assert (
                pm._parse_reconcile_response('{"decisions": {"action": "ADD"}}')
                == []
            )
        assert any("unexpected shape" in r.message for r in caplog.records)

    def test_non_dict_decision_items_filtered(self):
        raw = '{"decisions": [{"new_fact": "f", "action": "ADD"}, "junk", 42]}'
        decisions = pm._parse_reconcile_response(raw)
        assert decisions == [{"new_fact": "f", "action": "ADD"}]
