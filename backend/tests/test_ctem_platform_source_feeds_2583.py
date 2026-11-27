"""#2583 point 6: a source row names the paths its one switch feeds and its objects by name."""

from __future__ import annotations

from app.services import ctem_platform_sources as sources


def test_the_opencti_live_listener_feeds_every_path_it_records() -> None:
    feeds = [f["path"] for f in sources.fed_paths("opencti", "listener")]
    assert {"new_vulnerability", "relevant_threat", "credential_leak", "third_party_event"} <= set(feeds)
    labels = {f["path"]: f["label"] for f in sources.fed_paths("opencti", "listener")}
    assert labels["third_party_event"] == "Third-party or sub-processor event", "the label, never the key"


def test_a_single_path_source_feeds_one_path() -> None:
    assert [f["path"] for f in sources.fed_paths("github", "listener")] != []
    assert sources.fed_paths("nobody", "listener") == []


def test_objects_carry_their_platform_name() -> None:
    state = {
        "name": "XTM One - Signals - OpenCTI live listener",
        "notifier_id": "n1",
        "trigger_id": "t1",
        "paths": {
            "third_party_event": {
                "name": "XTM One - Signals - OpenCTI live listener - Third-party or sub-processor event",
                "notifier_id": "n2",
                "trigger_id": "t2",
            }
        },
    }
    named = {o["id"]: o["name"] for o in sources.provisioned_objects(state)}
    assert named["t1"] == "XTM One - Signals - OpenCTI live listener - Trigger"
    assert named["n2"] == "XTM One - Signals - OpenCTI live listener - Third-party or sub-processor event - Notifier"
