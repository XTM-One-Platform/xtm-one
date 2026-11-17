"""Tests for the Action Register parser.

Pure-parsing coverage: the markdown "Action Register" table is turned into
trackable work-item rows with normalized status/priority, the heading is
required (no false positives), and already-done rows are still parsed (the
materializer, not the parser, drops them).
"""

import pytest

from app.services.action_register import (
    materialize_action_register,
    parse_action_register,
)
from app.services.company_managed_writes import agent_writer


def test_parses_basic_action_register_table():
    text = (
        "Some preamble.\n\n"
        "## Action Register\n"
        "| Action | Owner | Status | Due |\n"
        "|--------|-------|--------|-----|\n"
        "| Patch CVE-2026-1234 | Incident Response | in progress | 2026-06-20 |\n"
        "| Notify stakeholders | Threat Intel | open | |\n"
    )
    rows = parse_action_register(text)
    assert len(rows) == 2
    assert rows[0]["title"] == "Patch CVE-2026-1234"
    assert rows[0]["owner"] == "Incident Response"
    assert rows[0]["status"] == "in_progress"  # normalized
    assert rows[0]["due"] == "2026-06-20"
    assert rows[1]["title"] == "Notify stakeholders"
    assert rows[1]["status"] == "open"
    assert rows[1]["due"] is None


def test_emoji_heading_and_priority_column():
    text = (
        "🎯 Action Register\n"
        "| Task | Assignee | State | Priority |\n"
        "| --- | --- | --- | --- |\n"
        "| Block C2 domain | Hunting | blocked | critical |\n"
    )
    rows = parse_action_register(text)
    assert len(rows) == 1
    assert rows[0]["title"] == "Block C2 domain"
    assert rows[0]["owner"] == "Hunting"
    assert rows[0]["status"] == "blocked"
    assert rows[0]["priority"] == "urgent"  # critical -> urgent


def test_status_synonyms_normalize():
    text = (
        "Action Register\n"
        "| Action | Status |\n"
        "| --- | --- |\n"
        "| A | TODO |\n"
        "| B | WIP |\n"
        "| C | Done |\n"
        "| D | on hold |\n"
        "| E | wontfix |\n"
    )
    rows = parse_action_register(text)
    statuses = {r["title"]: r["status"] for r in rows}
    assert statuses == {
        "A": "open",
        "B": "in_progress",
        "C": "done",
        "D": "blocked",
        "E": "cancelled",
    }


def test_strips_markdown_emphasis_and_links_in_cells():
    text = (
        "## Action Register\n"
        "| Action | Owner |\n"
        "| --- | --- |\n"
        "| **Rotate keys** | [IR Team](https://x) |\n"
    )
    rows = parse_action_register(text)
    assert rows[0]["title"] == "Rotate keys"
    assert rows[0]["owner"] == "IR Team"


def test_header_mapping_tolerates_surrounding_punctuation():
    # Headers decorated with trailing/surrounding punctuation still map to their
    # canonical field, while internal hyphens in multi-word aliases ("follow-up")
    # are preserved (only the ends are stripped).
    text = (
        "## Action Register\n"
        "| Follow-up: | (Owner) | Status. | _Due_ |\n"
        "| --- | --- | --- | --- |\n"
        "| Rotate creds | IR | open | 2026-07-01 |\n"
    )
    rows = parse_action_register(text)
    assert len(rows) == 1
    assert rows[0]["title"] == "Rotate creds"
    assert rows[0]["owner"] == "IR"
    assert rows[0]["status"] == "open"
    assert rows[0]["due"] == "2026-07-01"


def test_no_heading_returns_empty():
    text = (
        "| Action | Owner | Status |\n"
        "| --- | --- | --- |\n"
        "| Do thing | IR | open |\n"
    )
    # No "Action Register" heading -> not parsed (avoids false positives).
    assert parse_action_register(text) == []


def test_no_table_returns_empty():
    assert parse_action_register("## Action Register\n\nNothing tabular here.") == []
    assert parse_action_register("") == []
    assert parse_action_register("just prose") == []


def test_requires_a_title_column():
    text = (
        "## Action Register\n"
        "| Owner | Status |\n"
        "| --- | --- |\n"
        "| IR | open |\n"
    )
    # Without a recognizable title/action column the table is skipped.
    assert parse_action_register(text) == []


@pytest.mark.asyncio
async def test_materialize_creates_skips_existing_and_dedups_in_batch(monkeypatch):
    """materialize_action_register: resolves owners, skips correlation keys that
    already exist (batch idempotency, one query), collapses in-batch duplicate
    titles, drops terminal rows, and creates the rest exactly once with
    source=action_register."""
    text = (
        "## Action Register\n\n"
        "| Action | Owner | Status |\n"
        "| --- | --- | --- |\n"
        "| Investigate incident 8942 | Triage | open |\n"
        "| Patch CVE-2026-0001 | Patcher | in progress |\n"
        "| Investigate incident 8942 | Triage | open |\n"  # in-batch duplicate
        "| Already handled | Triage | done |\n"  # terminal -> dropped
    )

    async def _names(_uid):
        return {"triage": "11111111-1111-1111-1111-111111111111",
                "patcher": "22222222-2222-2222-2222-222222222222"}

    # Pretend the Patch-CVE key already exists from a prior run -> must be skipped.
    async def _existing(*, user_id, keys):
        return {k for k in keys if "patch-cve" in k}

    created: list[dict] = []

    async def _create(**kwargs):
        created.append(kwargs)
        return {"id": "x"}

    monkeypatch.setattr(
        "app.services.action_register._resolve_agent_names", _names
    )
    monkeypatch.setattr(
        "app.services.work_item.existing_correlation_keys", _existing
    )
    monkeypatch.setattr("app.services.work_item.create_work_item", _create)

    n = await materialize_action_register(
        text=text,
        user_id="33333333-3333-3333-3333-333333333333",
        is_company_managed=False,
        correlation_prefix="run:abc",
        created_by_agent_id=None,
        writer=agent_writer(is_company_managed=False),
    )

    # Only the (deduped, non-terminal, not-already-existing) Investigate row.
    assert n == 1
    assert len(created) == 1
    assert created[0]["title"] == "Investigate incident 8942"
    assert created[0]["owner_agent_id"] == "11111111-1111-1111-1111-111111111111"
    assert created[0]["source"] == "action_register"
    assert "auto_trigger" not in created[0]
    assert created[0]["correlation_key"].startswith("run:abc:")


@pytest.mark.asyncio
async def test_materialize_skips_unowned_and_unresolvable_rows(monkeypatch):
    """A row only materialises when its owner resolves to a KNOWN agent.

    Blank owners, human approvers, and any other unresolvable owner are skipped
    so they are never minted as orphan, unassigned work items (the exact bug:
    Action Register rows turning into items nobody owns). Owned rows still create.
    """
    text = (
        "## Action Register\n\n"
        "| Action | Owner | Status |\n"
        "| --- | --- | --- |\n"
        "| Investigate incident 8942 | Triage | open |\n"  # resolves -> created
        "| Weekly policy lifecycle sweep |  | open |\n"  # blank owner -> skipped
        "| Travel & Expenses approval chase | Jane Doe | open |\n"  # human -> skipped
        "| Next review checkpoint | Policy & Governance | open |\n"  # unknown -> skipped
    )

    async def _names(_uid):
        return {"triage": "11111111-1111-1111-1111-111111111111"}

    async def _existing(*, user_id, keys):
        return set()

    created: list[dict] = []

    async def _create(**kwargs):
        created.append(kwargs)
        return {"id": "x"}

    monkeypatch.setattr("app.services.action_register._resolve_agent_names", _names)
    monkeypatch.setattr("app.services.work_item.existing_correlation_keys", _existing)
    monkeypatch.setattr("app.services.work_item.create_work_item", _create)

    n = await materialize_action_register(
        text=text,
        user_id="33333333-3333-3333-3333-333333333333",
        is_company_managed=True,
        correlation_prefix="actionreg:run1",
        created_by_agent_id=None,
        writer=agent_writer(is_company_managed=True),
    )

    # Only the row with a resolvable agent owner is materialised.
    assert n == 1
    assert len(created) == 1
    assert created[0]["title"] == "Investigate incident 8942"
    # Crucially, NO orphan (owner_agent_id=None) was ever created.
    assert all(c["owner_agent_id"] for c in created)


@pytest.mark.asyncio
async def test_materialize_resolves_owner_by_slug_and_uuid(monkeypatch):
    """Owner cells that use the agent slug or a literal agent UUID still resolve
    (and the row is materialised), not just the exact display name."""
    aid = "11111111-1111-1111-1111-111111111111"
    text = (
        "## Action Register\n\n"
        "| Action | Owner | Status |\n"
        "| --- | --- | --- |\n"
        "| Triage alert A | triage-bot | open |\n"  # by slug
        f"| Triage alert B | {aid} | open |\n"  # by literal agent id
    )

    async def _names(_uid):
        # _resolve_agent_names indexes BOTH name and slug -> id.
        return {"triage": aid, "triage-bot": aid}

    async def _existing(*, user_id, keys):
        return set()

    created: list[dict] = []

    async def _create(**kwargs):
        created.append(kwargs)
        return {"id": "x"}

    monkeypatch.setattr("app.services.action_register._resolve_agent_names", _names)
    monkeypatch.setattr("app.services.work_item.existing_correlation_keys", _existing)
    monkeypatch.setattr("app.services.work_item.create_work_item", _create)

    n = await materialize_action_register(
        text=text,
        user_id="33333333-3333-3333-3333-333333333333",
        is_company_managed=True,
        correlation_prefix="actionreg:run2",
        created_by_agent_id=None,
        writer=agent_writer(is_company_managed=True),
    )

    assert n == 2
    assert {c["title"] for c in created} == {"Triage alert A", "Triage alert B"}
    assert all(c["owner_agent_id"] == aid for c in created)


@pytest.mark.asyncio
async def test_materialize_sets_provenance(monkeypatch):
    """Materialised items carry provenance: a real description (never a
    bare title) plus the source run id and register owner in the payload, so a
    surfaced item is always explainable and the UI can link back to the run."""
    aid = "11111111-1111-1111-1111-111111111111"
    text = (
        "## Action Register\n\n"
        "| Action | Owner | Status |\n"
        "| --- | --- | --- |\n"
        "| Investigate incident 8942 | Triage | open |\n"
    )

    async def _names(_uid):
        return {"triage": aid}

    async def _existing(*, user_id, keys):
        return set()

    created: list[dict] = []

    async def _create(**kwargs):
        created.append(kwargs)
        return {"id": "x"}

    monkeypatch.setattr("app.services.action_register._resolve_agent_names", _names)
    monkeypatch.setattr("app.services.work_item.existing_correlation_keys", _existing)
    monkeypatch.setattr("app.services.work_item.create_work_item", _create)

    await materialize_action_register(
        text=text,
        user_id="33333333-3333-3333-3333-333333333333",
        is_company_managed=True,
        correlation_prefix="actionreg:run-xyz",
        created_by_agent_id=aid,
        source_run_id="run-xyz",
        writer=agent_writer(is_company_managed=True),
    )

    assert len(created) == 1
    row = created[0]
    # A real description (not None) so the human can read what the item is.
    assert row["description"] and "Action Register" in row["description"]
    # Provenance in the payload: the originating run + the register owner cell.
    assert row["payload"]["source_run_id"] == "run-xyz"
    assert row["payload"]["register_owner"] == "Triage"
