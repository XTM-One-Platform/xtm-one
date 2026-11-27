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
        {"kind": "playbook", "id": "p1"},
        {"kind": "notifier", "id": "n2"},
        {"kind": "playbook", "id": "p2"},
    ]


def test_list_of_trigger_ids_and_single_trigger_id() -> None:
    assert provisioned_objects({"trigger_id": "t0", "trigger_ids": ["t1", ""]}) == [
        {"kind": "trigger", "id": "t0"},
        {"kind": "trigger", "id": "t1"},
    ]


def test_nothing_held() -> None:
    assert provisioned_objects(None) == []
    assert provisioned_objects({"status": "idle"}) == []
