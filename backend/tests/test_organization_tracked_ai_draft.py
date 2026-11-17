"""Tracked organizations drafted with a model, single and bulk (#9484, S-c; dev-docs/ctem-fleets.md 6.1).

The gates of S-c: names read from a list or the first column of a CSV or
XLSX file, at most 50 per run, one model call per batch of 10, a name
already tracked never drafted nor created twice, a failed batch kept with
its error while the others go on, every value validated, nothing created
before an administrator accepts, every accepted organization created with
monitoring off. The model is a stub; nothing touches a network.
"""

from __future__ import annotations

import ast
import io
import json
import pathlib
import uuid
from types import SimpleNamespace

import pytest

from app.services import organization_context_proposals as proposals
from app.services import organization_tracked_ai_draft as tracked_ai

BACKEND = pathlib.Path(__file__).resolve().parents[1]


def test_names_come_from_lines_and_a_csv_first_column_deduplicated_header_skipped():
    csv_bytes = b"Supplier;Country\nAcme Cloud;US\nGlobex;DE\nacme cloud;US\n"
    names = tracked_ai.read_names("Initech\n\n  Globex  \n", "suppliers.csv", csv_bytes)
    assert names == ["Initech", "Globex", "Acme Cloud"]


def test_names_come_from_an_xlsx_first_column():
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    for row in (("Name", "Spend"), ("Umbrella", 10), ("Hooli", 20)):
        sheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    assert tracked_ai.read_names("", "customers.xlsx", buffer.getvalue()) == ["Umbrella", "Hooli"]


@pytest.mark.parametrize(
    ("file_name", "content", "message"),
    [
        ("list.pdf", b"x", "CSV or XLSX"),
        ("big.csv", b"x" * (tracked_ai.FILE_MAX_BYTES + 1), "limited to 2 MB"),
    ],
)
def test_a_bad_file_is_refused_with_its_reason(file_name, content, message):
    with pytest.raises(ValueError, match=message):
        tracked_ai.read_names("", file_name, content)


def test_a_batch_answer_is_validated_and_a_skipped_name_stays_empty():
    answer = {
        "organizations": [
            {
                "name": "Acme Cloud",
                "sector": "Technology",
                "regions": ["us", "IE"],
                "website": "https://acme.example",
                "relationship": "provider",
                "criticality": "high",
                "confidence": "high",
                "basis": "public cloud",
            },
            {
                "name": "Globex",
                "regions": ["Europe"],
                "website": "javascript:alert(1)",
                "relationship": "overlord",
                "criticality": "extreme",
                "confidence": "certain",
            },
        ]
    }
    rows = tracked_ai.parse_batch(json.dumps(answer), ["Acme Cloud", "Globex", "Initech"], default_relationship="customer")
    acme, globex, initech = rows
    assert (acme.regions, acme.website, acme.relationship, acme.criticality) == (
        ["US", "IE"],
        "https://acme.example",
        "provider",
        "high",
    )
    # Every bad value is left at its default and named, never stored.
    assert (globex.regions, globex.website, globex.relationship, globex.criticality, globex.confidence) == (
        [],
        "",
        "customer",
        "medium",
        "low",
    )
    assert any("regions" in n for n in globex.notes) and any("website" in n for n in globex.notes)
    assert (initech.sector, initech.confidence) == ("", "low")
    assert initech.notes == ["The model proposed nothing for this name."]


def test_an_unusable_answer_is_an_error():
    with pytest.raises(tracked_ai.TrackedDraftError):
        tracked_ai.parse_batch("I cannot help", ["Acme"], default_relationship="provider")


async def test_one_call_per_batch_of_ten_at_most_fifty_names_and_tracked_names_cost_nothing():
    names = [f"Org {i}" for i in range(60)]
    calls: list[list[str]] = []

    async def call_batch(batch):
        calls.append(batch)
        return [tracked_ai.Row(name=n, relationship="provider", confidence="high") for n in batch]

    rows = await tracked_ai.draft_rows(names, call_batch=call_batch, tracked={"org 3": "id-3"})
    assert len(rows) == 50
    assert [len(c) for c in calls] == [10, 10, 10, 10, 9]
    conflict = rows[3]
    assert (conflict.status, conflict.already_tracked_id) == ("conflict", "id-3")
    assert all("Org 3" not in c for c in calls)


async def test_a_failed_batch_keeps_its_rows_with_the_error_and_the_others_go_on():
    names = [f"Org {i}" for i in range(15)]

    async def call_batch(batch):
        if batch[0] == "Org 0":
            raise tracked_ai.TrackedDraftError("The model's answer was cut for this batch. Retry it.")
        return [tracked_ai.Row(name=n, relationship="partner") for n in batch]

    rows = await tracked_ai.draft_rows(names, call_batch=call_batch, tracked={})
    assert {r.status for r in rows[:10]} == {"error"}
    assert rows[0].error.startswith("The model's answer was cut")
    assert {r.status for r in rows[10:]} == {"pending"}


async def test_opencti_is_matched_by_search_and_preset_only_on_a_single_exact_match():
    async def call_batch(batch):
        return [tracked_ai.Row(name=n, relationship="provider") for n in batch]

    nodes = {
        "Acme": [{"id": "o1", "name": "ACME"}, {"id": "o2", "name": "Acme Holdings"}],
        "Globex": [{"id": "g1", "name": "Globex"}, {"id": "g2", "name": "Globex Corp", "x_opencti_aliases": ["globex"]}],
        "Initech": [],
    }

    async def search(name):
        return nodes[name]

    acme, globex, initech = await tracked_ai.draft_rows(
        ["Acme", "Globex", "Initech"], call_batch=call_batch, tracked={}, search=search
    )
    assert acme.opencti_organization_id == "o1"
    assert [m["id"] for m in acme.opencti_matches] == ["o1", "o2"]
    assert globex.opencti_organization_id == ""  # two exact matches: the administrator picks
    assert initech.opencti_matches == []


class _Db:
    def __init__(self) -> None:
        self.added: list = []

    def add(self, row):
        self.added.append(row)

    async def flush(self):
        return None


async def test_the_draft_is_stored_as_one_pending_proposal_and_nothing_is_created(monkeypatch):
    async def no_create(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("the draft created an organization")

    from app.services import organizations as orgs

    monkeypatch.setattr(orgs, "create_tracked", no_create)
    db = _Db()
    row = tracked_ai.Row(name="Acme", relationship="provider", criticality="high")
    stored = await proposals.store_tracked_draft(db, author_user_id=None, rows=[row.as_dict()])  # type: ignore[arg-type]
    assert (stored.kind, stored.organization_id, len(db.added)) == ("new_tracked", None, 1)
    assert stored.proposals[0]["name"] == "Acme"


async def test_accepting_rows_creates_them_monitoring_off_with_the_picked_opencti_and_records_each_row(monkeypatch):
    from app.services import organization_context as oc
    from app.services import organizations as orgs

    created: list[dict] = []
    saved: list[dict] = []

    async def create_tracked(db, **kwargs):
        created.append(kwargs)
        return SimpleNamespace(id=uuid.uuid4(), monitoring="off", **kwargs)

    async def save_version(db, profile, **kwargs):
        saved.append({"profile": profile.model_dump(mode="json"), **kwargs})
        return SimpleNamespace(version=1)

    monkeypatch.setattr(orgs, "create_tracked", create_tracked)
    monkeypatch.setattr(oc, "save_version", save_version)
    rows = [
        tracked_ai.Row(
            name="Acme", relationship="provider", criticality="high", sector="Technology", regions=["US"], basis="b"
        ).as_dict(),
        tracked_ai.Row(name="Globex", relationship="partner", opencti_organization_id="g1").as_dict(),
        tracked_ai.Row(name="Initech", relationship="customer").as_dict(),
    ]
    proposal = SimpleNamespace(status="pending", kind="new_tracked", proposals=rows)
    outcome = await proposals.decide(
        _Db(),  # type: ignore[arg-type]
        proposal,  # type: ignore[arg-type]
        admin_user_id=uuid.uuid4(),
        accept=["Acme", "Globex"],
        opencti_ids={"Acme": "o9"},
    )
    assert outcome["status"] == "accepted" and len(outcome["created"]) == 2
    assert [(c["name"], c["opencti_organization_id"]) for c in created] == [("Acme", "o9"), ("Globex", "g1")]
    # The drafted sector and regions become the first version, with their basis.
    assert len(saved) == 1
    assert saved[0]["source"] == "ai_draft"
    assert saved[0]["profile"]["sector"] == "Technology" and saved[0]["profile"]["regions"] == ["US"]
    assert [r["status"] for r in proposal.proposals] == ["accepted", "accepted", "rejected"]


@pytest.mark.parametrize(
    ("row", "message"),
    [
        ({"status": "conflict", "already_tracked_id": "x"}, "Already tracked"),
        ({"status": "error", "error": "cut"}, "batch failed"),
    ],
)
async def test_a_conflict_or_failed_row_is_never_created(row, message):
    entry = {**tracked_ai.Row(name="Acme", relationship="provider").as_dict(), **row}
    proposal = SimpleNamespace(status="pending", kind="new_tracked", proposals=[entry])
    with pytest.raises(proposals.ProposalError, match=message):
        await proposals.decide(_Db(), proposal, admin_user_id=uuid.uuid4(), accept=["Acme"])  # type: ignore[arg-type]
    assert proposal.status == "pending"


def test_only_the_draft_endpoint_imports_the_service():
    """No page load or scheduler reaches the model: the endpoint imports it inside its handler."""
    importers = []
    for path in (BACKEND / "app").rglob("*.py"):
        if path.name == "organization_tracked_ai_draft.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (
                "organization_tracked_ai_draft" in (node.module or "")
                or any(a.name == "organization_tracked_ai_draft" for a in node.names)
            ):
                importers.append(path.relative_to(BACKEND).as_posix())
    assert importers == ["app/api/v1/organizations.py"]
