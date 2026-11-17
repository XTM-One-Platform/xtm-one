"""The About page's short labels in the structured documentation (#10123).

``serves`` (who the fleet serves) and ``members`` (one line per member) are
structured fields: validated field by field, cut to their limit, kept from the
previous version on a regenerate that does not give them, and asked for by the
generator's guide with their word limits.
"""

from app.services.fleet_doc_generation import KEPT_BLOCKS, STRUCTURED_GUIDE
from app.services.fleet_docs import BLOCK_NAMES, STRUCTURED_FIELDS, StructuredDoc, clean_structured_fields, field_limit


def test_serves_and_members_are_structured_fields_with_limits():
    assert {"serves", "members"} <= set(STRUCTURED_FIELDS)
    assert field_limit("serves") == 6
    assert field_limit("members") == 60
    doc = StructuredDoc.model_validate({"serves": ["CEO"], "members": [{"name": "Desk Lead", "role": "Routes each alert"}]})
    assert doc.members[0].role == "Routes each alert"


def test_a_wrong_member_row_costs_that_field_only():
    notes: list[dict] = []
    cleaned = clean_structured_fields({"purpose_line": "Triage alerts.", "members": [{"role": "no name"}]}, notes)
    assert cleaned is not None and cleaned.get("purpose_line") == "Triage alerts."
    assert "members" not in cleaned or not cleaned["members"]


def test_the_guide_asks_for_short_labels_and_the_blocks_are_kept_and_named():
    assert "at most 25 words" in STRUCTURED_GUIDE
    assert "at most 12 words" in STRUCTURED_GUIDE
    assert {"serves", "members"} <= set(KEPT_BLOCKS)
    assert BLOCK_NAMES["members"] == "member roles"
