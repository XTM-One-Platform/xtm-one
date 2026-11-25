"""to_workset keeps people's records out of the corpus: a tool that declares
``personal_records`` has each item's reference stored, never the record."""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

from app.services.builtin_tools import workset as workset_tools
from app.services.workset_split import ItemDraft, SplitResult
from app.tools.base import ToolDefinition


def test_the_property_defaults_off_and_contact_tools_declare_it() -> None:
    assert ToolDefinition(name="t", description="d").personal_records is False
    providers = Path(__file__).resolve().parents[1] / "app" / "integrations" / "providers"
    declared = set()
    for path in (providers / "hubspot.py", providers / "ses.py", providers / "drata" / "personnel.py"):
        declared |= set(re.findall(r'name="(\w+)",\n\s+personal_records=True,', path.read_text()))
    assert declared == {
        "search_hubspot_contacts",
        "get_hubspot_contact",
        "list_ses_contacts",
        "get_ses_contact",
        "list_drata_personnel",
        "get_drata_personnel",
    }


def test_a_personal_item_keeps_its_reference_not_its_record() -> None:
    draft = ItemDraft(
        title="Jane Doe",
        content='{"email": "jane@example.com", "phone": "+33 1 00 00 00 00"}',
        external_ref="contact-42",
        source_url="https://crm.example/contacts/42",
    )

    stored = workset_tools.as_personal_reference(draft, "search_hubspot_contacts")

    assert "jane@example.com" not in stored.content and "+33" not in stored.content
    assert "contact-42" in stored.content and "search_hubspot_contacts" in stored.content
    assert (stored.title, stored.external_ref, stored.source_url) == (
        "Jane Doe",
        "contact-42",
        "https://crm.example/contacts/42",
    )


def test_the_receipt_says_records_were_not_stored() -> None:
    split = SplitResult(items=[ItemDraft(title="Jane", content="x", external_ref="c1")])
    outcomes = [SimpleNamespace(ok=True, action="created", error=None)]

    receipt = workset_tools._receipt("list_contacts", split, outcomes, "1 item", personal_records=True)

    assert "people's records" in receipt
    assert "people's records" not in workset_tools._receipt("list_issues", split, outcomes, "1 item")
