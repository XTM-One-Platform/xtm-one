# SPDX-License-Identifier: Apache-2.0

"""The objects a platform source holds, listed for the Open in links (#2583 point 2)."""

from app.services.ctem_platform_sources import provisioned_objects


def test_lists_notifier_triggers_and_playbook_once_in_order() -> None:
    state = {
        "notifier_id": "n1",
        "trigger_ids": {"create": "t1", "update": "t2", "dup": "t1"},
        "paths": {
            "b": {"notifier_id": "n2", "playbook_id": "p2"},
            "a": {"notifier_id": "n1", "playbook_id": "p1"},
        },
    }
    assert provisioned_objects(state) == [
        {"kind": "notifier", "id": "n1"},
        {"kind": "trigger", "id": "t1"},
        {"kind": "trigger", "id": "t2"},
        {"kind": "playbook", "id": "p1", "path": "a", "path_label": "a"},
        {"kind": "notifier", "id": "n2", "path": "b", "path_label": "b"},
        {"kind": "playbook", "id": "p2", "path": "b", "path_label": "b"},
    ]


def test_per_path_objects_carry_their_path_label() -> None:
    """#2627: the two PIR playbooks of one row read apart by the path each serves."""
    state = {"paths": {"relevant_threat": {"playbook_id": "p2"}, "new_vulnerability": {"playbook_id": "p1"}}}
    labels = {o["id"]: o["path_label"] for o in provisioned_objects(state)}
    assert labels == {
        "p1": "New vulnerability on a running version",
        "p2": "Relevant new threat or actor",
    }


def test_list_of_trigger_ids_and_single_trigger_id() -> None:
    assert provisioned_objects({"trigger_id": "t0", "trigger_ids": ["t1", ""]}) == [
        {"kind": "trigger", "id": "t0"},
        {"kind": "trigger", "id": "t1"},
    ]


def test_nothing_held() -> None:
    assert provisioned_objects(None) == []
    assert provisioned_objects({"status": "idle"}) == []
