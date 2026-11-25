"""#2206: a to_workset result is never dropped for a spent summarisation budget.

The store keeps the item (see test_workset_implicit_lifecycle_postgres) and the
receipt the model reads says its summary waits, until when and how to finish it.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.services.builtin_tools import workset as workset_tools
from app.services.workset_split import ItemDraft, SplitResult


def test_the_receipt_names_the_items_whose_summary_waits_for_the_budget() -> None:
    split = SplitResult(items=[ItemDraft(title="Skill", content="x", external_ref="s1")])
    warning = (
        "This workset's automatic summarisation budget is spent (200/200 calls). "
        "It replenishes at 2026-11-26T08:00+00:00. Summarise this item with workset_set_summary"
    )
    outcomes = [SimpleNamespace(ok=True, action="created", error=None, warning=warning)]

    receipt = workset_tools._receipt("load_skill", split, outcomes, "1 item")

    assert "1 stored (1 added" in receipt
    assert "NOT stored" not in receipt
    assert "1 stored item(s) wait for a summary" in receipt
    assert "replenishes at" in receipt


def test_a_receipt_without_a_warning_carries_no_summary_line() -> None:
    split = SplitResult(items=[ItemDraft(title="Skill", content="x", external_ref="s1")])
    outcomes = [SimpleNamespace(ok=True, action="created", error=None, warning=None)]

    assert "wait for a summary" not in workset_tools._receipt("load_skill", split, outcomes, "1 item")
