# SPDX-License-Identifier: Apache-2.0
"""The webhook receivers load ``Integration.auth_type`` with the integration row.

``_shown_platform_url`` reads ``integration.auth_type`` after the session that
loaded the row is closed. A ``load_only`` that leaves it out defers it, and the
read then raises ``DetachedInstanceError``: every delivery of a registered
(``platform_managed``) OpenCTI integration with a ``base_url`` answered 500 and
started no run (found in the CTEM-08 lab proof).
"""

from __future__ import annotations

import inspect
import re

from app.api.v1 import webhooks


def _load_only_blocks(func) -> list[str]:
    source = inspect.getsource(func)
    return re.findall(r"load_only\((.*?)\)", source, flags=re.S)


def test_the_assignment_receiver_loads_auth_type_with_the_resolved_integration():
    blocks = [b for b in _load_only_blocks(webhooks.receive_assignment_webhook) if "Integration.id" in b]
    assert blocks, "the receiver resolves the integration with load_only"
    assert all("Integration.auth_type" in b for b in blocks)


def test_the_integration_receiver_loads_auth_type_with_the_resolved_integration():
    blocks = [b for b in _load_only_blocks(webhooks.receive_integration_webhook) if "Integration.id" in b]
    assert blocks, "the receiver resolves the integration with load_only"
    assert all("Integration.auth_type" in b for b in blocks)
