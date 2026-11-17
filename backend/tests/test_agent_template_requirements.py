"""The template setup-requirement schema rejects entries the wizard cannot act on.

The wizard gates its Next button on ``must_be_defined`` entries, so a typoed
``kind`` would silently do nothing and a blank ``target`` would render a
requirement that can never be satisfied. The schema turns both into a 422 at
write time (see ``TemplateSetupRequirement``).
"""

import pytest
from pydantic import ValidationError

from app.api.v1.schemas.agent import CreateTemplateRequest, TemplateSetupRequirement


def _template(**must_be_defined_entry) -> dict:
    return {
        "slug": "t",
        "name": "T",
        "must_be_defined": [must_be_defined_entry],
    }


def test_a_valid_requirement_round_trips():
    req = CreateTemplateRequest(
        **_template(kind="integration", target="email", instruction="Bind one.")
    )
    assert req.must_be_defined == [
        TemplateSetupRequirement(
            kind="integration", target="email", instruction="Bind one."
        )
    ]


@pytest.mark.parametrize(
    "entry",
    [
        {"kind": "integratoin", "target": "email", "instruction": "typo kind"},
        {"kind": "integration", "target": "", "instruction": "blank target"},
        {"kind": "integration", "target": "email", "instruction": ""},
        {"kind": "knowledge_bse", "target": "kb", "instruction": "typo kind"},
    ],
)
def test_malformed_requirements_are_rejected_at_write_time(entry):
    with pytest.raises(ValidationError):
        CreateTemplateRequest(**_template(**entry))
