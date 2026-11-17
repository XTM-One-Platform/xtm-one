"""The OpenAEV payload persona and skill show ONE valid output item.

Both say to return exactly the JSON they show, so the example is what a model
copies: a delegated ``create`` carrying the fork-only ``fork_source_id``, the
consult-only ``spec`` and prose inside ``injector_contract_id`` teaches values
no consumer can use. The other modes are the rules under the example.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

_SEED = Path(__file__).resolve().parents[1] / "app/agents/seed_data"


@pytest.mark.parametrize(
    "rel", ["agent_persona/openaev-payload-creator.md", "skill_content/openaev-payload-builder.md"]
)
def test_the_output_example_is_one_valid_delegated_create(rel):
    text = (_SEED / rel).read_text(encoding="utf-8")
    block = re.search(r'```json\n(\{"response".*?)\n```', text, re.S)[1]
    (item,) = json.loads(block)["response"]["items"]
    assert item["decision"] == "create"
    assert "fork_source_id" not in item
    assert "spec" not in item
    assert re.fullmatch(r"[A-Z][A-Z0-9_]*", item["injector_contract_id"]), item["injector_contract_id"]
