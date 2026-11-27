# SPDX-License-Identifier: Apache-2.0

"""The owner of each object XTM One provisions in a module, and the triggers two owners share (#2640)."""

from app.services.ctem_platform_sources import (
    OBJECT_FLEET,
    fleet_objects,
    object_owner,
    owned_objects,
    with_shared_events,
)

SIGNALS_STATE = {
    "name": "XTM One - Signals - OpenAEV listener",
    "notifier_id": "n1",
    "trigger_ids": {"simulation_finished": "t1", "finding_updated": "t2"},
    "integration_id": "i1",
}


def _desk(integration_id: str = "i1") -> dict:
    return {
        "provider": "openaev",
        "openaev_provisioning": {
            "status": "provisioned",
            "name": "XTM One - Detection validation desk - Finished simulations",
            "notifier_id": "n9",
            "trigger_ids": {"simulation_finished": "t9"},
            "integration_id": integration_id,
        },
    }


def test_owner_read_from_the_object_name() -> None:
    assert object_owner("XTM One - PIR intelligence desk - PIR matches") == "PIR intelligence desk"
    assert object_owner("XTM One - Signals - OpenCTI live listener - Trigger") == "Signals"
    assert object_owner("Someone else - x") == ""
    assert object_owner(None) == ""


def test_a_source_lists_its_own_objects_with_owner_and_event() -> None:
    objects = owned_objects(SIGNALS_STATE, OBJECT_FLEET)
    assert [o["owner"] for o in objects] == ["Signals", "Signals", "Signals"]
    assert {o["id"]: o.get("event") for o in objects} == {
        "n1": None,
        "t1": "simulation_finished",
        "t2": "finding_updated",
    }


def test_fleet_objects_are_listed_apart_per_integration() -> None:
    configs = [_desk(), _desk("other"), {"provider": "opencti"}, None]
    objects = fleet_objects(configs, "openaev", "i1")
    assert objects == [
        {
            "kind": "notifier",
            "id": "n9",
            "name": "XTM One - Detection validation desk - Finished simulations - Notifier",
            "owner": "Detection validation desk",
        },
        {
            "kind": "trigger",
            "id": "t9",
            "name": "XTM One - Detection validation desk - Finished simulations - Trigger",
            "owner": "Detection validation desk",
            "event": "simulation_finished",
        },
    ]
    # The source row counts only its own: no fleet object leaks into it.
    own = owned_objects(SIGNALS_STATE, OBJECT_FLEET)
    assert {o["id"] for o in own}.isdisjoint({o["id"] for o in objects})


def test_a_trigger_two_owners_hold_on_one_event_says_so() -> None:
    own = with_shared_events(owned_objects(SIGNALS_STATE, OBJECT_FLEET), fleet_objects([_desk()], "openaev", "i1"))
    by_id = {o["id"]: o for o in own}
    assert by_id["t1"]["shared_with"] == ["Detection validation desk"]
    assert "shared_with" not in by_id["t2"]
    assert "shared_with" not in by_id["n1"]


def test_nothing_held() -> None:
    assert fleet_objects([], "openaev", "i1") == []
    assert fleet_objects([_desk()], "aikido", "i1") == []
    assert with_shared_events([], []) == []
