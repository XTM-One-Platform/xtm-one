"""Opt-in live LLM smoke suite - see ``README.md``.

Every test module here carries ``live_marks(<provider>)``: skipped unless
``LIVE_LLM_SMOKE=1`` and that provider's key are set. What this conftest adds
is the output guard - whatever a live test logs, captures or fails with goes
through the key scrubber before pytest prints it.
"""

from __future__ import annotations

import logging

import pytest

from tests.live.live_llm_support import scrub_report, scrubbing_record_factory


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    report = yield
    scrub_report(report)
    return report


@pytest.fixture(autouse=True)
def _scrub_secrets_from_log_records():
    previous = logging.getLogRecordFactory()
    logging.setLogRecordFactory(scrubbing_record_factory(previous))
    yield
    logging.setLogRecordFactory(previous)
