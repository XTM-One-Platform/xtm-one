"""A worker that reads itself outgoing leaves resumable work to the incoming release for a bounded time only (#2040).

Before: ``leaving`` set the claimable slots to 0, for as long as a worker of another, newer-started release stayed
present. A pod of another build that stayed up (a rollout stuck half way) kept every worker of the current release
from claiming assignment runs: scheduled fires waited for a worker for hours and expired before start while chat,
served outside the reconciler, went on. Now the outgoing worker claims what the incoming release left due for
``OUTGOING_LEAVE_SECONDS`` (``ripe_seconds``), and lends no idle slot meanwhile.
"""

from __future__ import annotations

import inspect

from app.services import orchestration_reconciler


def test_the_leave_to_the_incoming_release_is_bounded():
    assert 0 < orchestration_reconciler.OUTGOING_LEAVE_SECONDS <= 300


def test_an_outgoing_worker_still_claims_work_left_due_past_the_bound():
    source = inspect.getsource(orchestration_reconciler)
    assert "available = 0 if draining or leaving" not in source, "leaving must not zero the claimable slots"
    assert (
        "available = 0 if draining or not claims_here(adapter) else _available_slots_for(adapter, adapters)" in source
    )
    bound = source.index("ripe = max(ripe, OUTGOING_LEAVE_SECONDS)")
    assert source.index("if leaving:", bound - 200) < bound < source.index("engine.claim_due(", bound)
    assert "if not claimed_ids and not leaving:" in source, "an outgoing worker lends no idle slot"
