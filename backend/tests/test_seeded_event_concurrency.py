"""Seeded event assignments run one at a time unless their seed names a cap (#1427, item 5)."""

from app.agents import seed
from app.agents.builtin_agents import BUILTIN_ASSIGNMENTS
from app.services.assignment_dispatch import (
    MAX_CONCURRENT_RUNS_KEY,
    SEEDED_EVENT_CONCURRENCY,
    SEEDED_EVENT_TRIGGER_TYPES,
    assignment_concurrency_cap,
    seeded_trigger_config,
)


def test_an_event_trigger_is_capped_at_one_run() -> None:
    config = {"event": "run.failed"}
    written = seeded_trigger_config("platform_event", config)
    assert written == {"event": "run.failed", MAX_CONCURRENT_RUNS_KEY: SEEDED_EVENT_CONCURRENCY}
    assert config == {"event": "run.failed"}
    assert assignment_concurrency_cap(written) == 1
    assert seeded_trigger_config("integration_event", None)[MAX_CONCURRENT_RUNS_KEY] == 1


def test_a_cap_the_seed_names_is_kept() -> None:
    assert seeded_trigger_config("integration_event", {MAX_CONCURRENT_RUNS_KEY: 3})[MAX_CONCURRENT_RUNS_KEY] == 3


def test_other_triggers_are_unchanged() -> None:
    for trigger_type, config in (("schedule", {"cron": "0 8 * * 1"}), ("work_item", {}), ("manual", {})):
        assert seeded_trigger_config(trigger_type, config) == config


def test_every_seeded_event_assignment_runs_one_at_a_time_unless_capped_by_its_seed() -> None:
    events = [a for a in BUILTIN_ASSIGNMENTS if a["trigger_type"] in SEEDED_EVENT_TRIGGER_TYPES]
    assert events
    for data in events:
        written = seeded_trigger_config(data["trigger_type"], data.get("trigger_config"))
        declared = (data.get("trigger_config") or {}).get(MAX_CONCURRENT_RUNS_KEY)
        assert written[MAX_CONCURRENT_RUNS_KEY] == (declared or SEEDED_EVENT_CONCURRENCY), data["name"]


def test_the_seed_writes_through_the_rule_on_create_and_update() -> None:
    import inspect

    source = inspect.getsource(seed.seed_builtin_assignments)
    assert source.count('seeded_trigger_config(data["trigger_type"], data.get("trigger_config"))') == 2
