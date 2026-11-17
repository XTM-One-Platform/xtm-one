"""Tests for the periodic Notion page-quarantine recovery probe.

The per-page 404 quarantine (``skipped_stale_page_ids``)
used to be permanent: a quarantined page was never probed again and never removed
from the skip list, so a page restored in Notion could only recover via a
manual force re-sync.

Now every ``_NOTION_PAGE_RECHECK_EVERY``-th run with a non-empty skip list
bypasses the quarantine (``_is_notion_page_recheck_run``), and
``_handle_notion_stale_pages(rechecked=True)`` removes recovered pages from
the skip list while keeping still-404 pages quarantined.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.services.kb_sync_scheduler import (
    _NOTION_CFG_NOT_FOUND_COUNTS,
    _NOTION_CFG_PAGE_RECHECK_COUNTER,
    _NOTION_CFG_SKIPPED_PAGE_IDS,
    _NOTION_PAGE_RECHECK_EVERY,
    _handle_notion_stale_pages,
    _is_notion_page_recheck_run,
    _reset_notion_stale_page_config,
)

_PAGE_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_PAGE_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


def _make_source(config: dict) -> SimpleNamespace:
    return SimpleNamespace(config=config)


# ---------------------------------------------------------------------------
# _is_notion_page_recheck_run
# ---------------------------------------------------------------------------


def test_recheck_predicate_false_without_skip_list():
    assert _is_notion_page_recheck_run({}) is False
    assert _is_notion_page_recheck_run({_NOTION_CFG_SKIPPED_PAGE_IDS: []}) is False


def test_recheck_predicate_false_on_fresh_quarantine():
    """Counter absent (fresh quarantine) -> not a recheck run."""
    config = {_NOTION_CFG_SKIPPED_PAGE_IDS: [_PAGE_A]}
    assert _is_notion_page_recheck_run(config) is False


def test_recheck_predicate_true_when_counter_at_boundary():
    config = {
        _NOTION_CFG_SKIPPED_PAGE_IDS: [_PAGE_A],
        _NOTION_CFG_PAGE_RECHECK_COUNTER: _NOTION_PAGE_RECHECK_EVERY - 1,
    }
    assert _is_notion_page_recheck_run(config) is True


def test_recheck_predicate_tolerates_corrupt_counter():
    """A corrupted (non-int / bool) counter is treated as 0 — no crash."""
    for corrupt in ("seven", True, None, 3.5):
        config = {
            _NOTION_CFG_SKIPPED_PAGE_IDS: [_PAGE_A],
            _NOTION_CFG_PAGE_RECHECK_COUNTER: corrupt,
        }
        assert _is_notion_page_recheck_run(config) is False


def test_recheck_predicate_ignores_corrupt_skip_list():
    config = {_NOTION_CFG_SKIPPED_PAGE_IDS: "corrupt"}
    assert _is_notion_page_recheck_run(config) is False
    config = {_NOTION_CFG_SKIPPED_PAGE_IDS: [123, None]}
    assert _is_notion_page_recheck_run(config) is False


def test_recheck_cadence_over_successive_runs():
    """Simulate successive runs: the counter advances each run and the
    predicate fires exactly once per _NOTION_PAGE_RECHECK_EVERY runs."""
    config = {_NOTION_CFG_SKIPPED_PAGE_IDS: [_PAGE_A]}
    recheck_runs = []
    for run in range(2 * _NOTION_PAGE_RECHECK_EVERY):
        if _is_notion_page_recheck_run(config):
            recheck_runs.append(run)
        # Mirror the counter advancement _handle_notion_stale_pages performs
        # (page still 404s, stays quarantined).
        source = _make_source(dict(config))
        _handle_notion_stale_pages(
            source,
            [_PAGE_A],
            base_config=config,
            kb_id="kb",
            source_id="src",
            rechecked=_is_notion_page_recheck_run(config),
        )
        config = source.config
    assert len(recheck_runs) == 2, (
        f"Expected exactly 2 recheck runs in {2 * _NOTION_PAGE_RECHECK_EVERY} "
        f"runs, got {recheck_runs}"
    )


# ---------------------------------------------------------------------------
# _handle_notion_stale_pages — rechecked runs
# ---------------------------------------------------------------------------


def test_recheck_removes_recovered_page_from_quarantine():
    """A quarantined page that did NOT 404 on the recheck run is recovered."""
    config = {
        _NOTION_CFG_SKIPPED_PAGE_IDS: [_PAGE_A, _PAGE_B],
        _NOTION_CFG_PAGE_RECHECK_COUNTER: _NOTION_PAGE_RECHECK_EVERY - 1,
    }
    source = _make_source(dict(config))

    # Page A still 404s; page B recovered.
    _handle_notion_stale_pages(
        source,
        [_PAGE_A],
        base_config=config,
        kb_id="kb",
        source_id="src",
        rechecked=True,
    )

    skipped = source.config.get(_NOTION_CFG_SKIPPED_PAGE_IDS) or []
    assert _PAGE_A in skipped, "A still-404 page must stay quarantined"
    assert _PAGE_B not in skipped, "A recovered page must leave the quarantine"


def test_recheck_removes_all_pages_and_drops_tracking_keys():
    """When every quarantined page recovered, all tracking keys are dropped."""
    config = {
        _NOTION_CFG_SKIPPED_PAGE_IDS: [_PAGE_A, _PAGE_B],
        _NOTION_CFG_NOT_FOUND_COUNTS: {_PAGE_A: 3, _PAGE_B: 3},
        _NOTION_CFG_PAGE_RECHECK_COUNTER: _NOTION_PAGE_RECHECK_EVERY - 1,
    }
    source = _make_source(dict(config))

    _handle_notion_stale_pages(
        source,
        [],
        base_config=config,
        kb_id="kb",
        source_id="src",
        rechecked=True,
    )

    assert _NOTION_CFG_SKIPPED_PAGE_IDS not in source.config
    assert _NOTION_CFG_NOT_FOUND_COUNTS not in source.config
    assert _NOTION_CFG_PAGE_RECHECK_COUNTER not in source.config


def test_non_recheck_run_never_unquarantines():
    """Outside recheck runs, pages absent from the 404 list stay quarantined
    (they were skipped, not probed — absence proves nothing)."""
    config = {
        _NOTION_CFG_SKIPPED_PAGE_IDS: [_PAGE_A],
        _NOTION_CFG_PAGE_RECHECK_COUNTER: 1,
    }
    source = _make_source(dict(config))

    _handle_notion_stale_pages(
        source,
        [],
        base_config=config,
        kb_id="kb",
        source_id="src",
        rechecked=False,
    )

    skipped = source.config.get(_NOTION_CFG_SKIPPED_PAGE_IDS) or []
    assert _PAGE_A in skipped


def test_counter_advances_on_quarantined_runs():
    config = {
        _NOTION_CFG_SKIPPED_PAGE_IDS: [_PAGE_A],
        _NOTION_CFG_PAGE_RECHECK_COUNTER: 2,
    }
    source = _make_source(dict(config))

    _handle_notion_stale_pages(
        source, [], base_config=config, kb_id="kb", source_id="src"
    )

    assert source.config[_NOTION_CFG_PAGE_RECHECK_COUNTER] == 3


def test_counter_not_created_when_quarantine_starts_this_run():
    """The cadence starts counting from the run AFTER the quarantine is
    created — the creating run itself does not write a counter."""
    from app.services.kb_sync_scheduler import _NOTION_PAGE_STALE_THRESHOLD

    config = {
        _NOTION_CFG_NOT_FOUND_COUNTS: {_PAGE_A: _NOTION_PAGE_STALE_THRESHOLD - 1},
    }
    source = _make_source(dict(config))

    newly = _handle_notion_stale_pages(
        source, [_PAGE_A], base_config=config, kb_id="kb", source_id="src"
    )

    assert newly == [_PAGE_A]
    assert _PAGE_A in source.config[_NOTION_CFG_SKIPPED_PAGE_IDS]
    assert _NOTION_CFG_PAGE_RECHECK_COUNTER not in source.config


def test_force_resync_clears_recheck_counter():
    source = _make_source(
        {
            _NOTION_CFG_SKIPPED_PAGE_IDS: [_PAGE_A],
            _NOTION_CFG_PAGE_RECHECK_COUNTER: 3,
        }
    )
    _reset_notion_stale_page_config(source)
    assert _NOTION_CFG_PAGE_RECHECK_COUNTER not in source.config
    assert _NOTION_CFG_SKIPPED_PAGE_IDS not in source.config
