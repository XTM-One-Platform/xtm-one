"""Synced rows of the organization profile are read-only by hand (#1367, point 3).

Pure: the stored and the incoming profiles are compared in memory.
"""

from __future__ import annotations

import copy
from typing import Any

from app.services import organization_context as oc
from app.services.organization_context_locks import synced_edit_problems


def _profile() -> dict[str, Any]:
    return oc.parse_profile(
        {
            "name": "Acme",
            "assets_exposure": [
                {
                    "name": "LAPTOP-ALICE",
                    "category": "host",
                    "criticality": "unknown",
                    "source": "intune",
                    "external_id": "d-1",
                    "sync_id": "s-1",
                },
                {"name": "build-01", "category": "host", "criticality": "low"},
            ],
            "ai_systems": [
                {
                    "name": "Copilot",
                    "kind": "assistant",
                    "exposure": "internal",
                    "source": "openaev",
                    "external_id": "a-1",
                }
            ],
        }
    ).model_dump(mode="json")


def test_no_stored_profile_and_an_unchanged_save_pass():
    stored = _profile()
    assert synced_edit_problems(None, stored) == []
    assert synced_edit_problems(stored, copy.deepcopy(stored)) == []


def test_a_synced_field_changed_by_hand_is_refused_with_the_row_and_field():
    stored = _profile()
    incoming = copy.deepcopy(stored)
    incoming["assets_exposure"][0]["criticality"] = "high"
    (problem,) = synced_edit_problems(stored, incoming)
    assert "LAPTOP-ALICE" in problem and "intune" in problem and "criticality" in problem


def test_a_synced_ai_system_renamed_by_hand_is_refused():
    # #1501: the profile AI system is the third asset (category ai_target), locked like any synced asset.
    stored = _profile()
    assert stored["assets_exposure"][2]["category"] == "ai_target" and "ai_systems" not in stored
    incoming = copy.deepcopy(stored)
    incoming["assets_exposure"][2]["name"] = "Renamed"
    assert synced_edit_problems(stored, incoming)


def test_local_fields_of_a_synced_row_and_every_field_of_a_hand_row_stay_editable():
    stored = _profile()
    incoming = copy.deepcopy(stored)
    incoming["assets_exposure"][0]["owner"] = "Alice"
    incoming["assets_exposure"][0]["environment"] = "production"
    incoming["assets_exposure"][1]["criticality"] = "very_high"
    incoming["assets_exposure"][2]["owner"] = "IT"
    assert synced_edit_problems(stored, incoming) == []


def test_a_removed_synced_row_is_left_to_the_sync_exclusion_path():
    stored = _profile()
    incoming = copy.deepcopy(stored)
    incoming["assets_exposure"] = incoming["assets_exposure"][1:]
    assert synced_edit_problems(stored, incoming) == []
