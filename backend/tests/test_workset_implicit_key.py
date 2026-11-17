# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""The worksets the runtime opens on its own are told apart from the named ones.

An agent chat corpus and an assignment corpus are never sealed by design, so
Operations > Worksets must not read "Still collecting" on them forever; the
API says which they are, the UI never guesses from the key.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

from app.services.builtin_tools.workset import chat_workset_key, is_implicit_workset_key
from app.services.workset_read import Progress

AGENT = uuid.UUID("11111111-2222-3333-4444-555555555555")
WORKSPACE = uuid.UUID("66666666-7777-8888-9999-000000000000")
CONVERSATION = uuid.UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")


def test_every_chat_key_the_runtime_builds_is_implicit():
    assert is_implicit_workset_key(chat_workset_key(AGENT))
    assert is_implicit_workset_key(chat_workset_key(AGENT, WORKSPACE))
    assert is_implicit_workset_key(chat_workset_key(AGENT, WORKSPACE, conversation_id=CONVERSATION))
    assert is_implicit_workset_key(chat_workset_key(AGENT, conversation_id=CONVERSATION))


def test_the_assignment_key_is_implicit():
    assert is_implicit_workset_key(f"assignment-{uuid.uuid4()}")


def test_a_named_corpus_is_not_implicit():
    for key in ("reports", "agent-weekly", "assignment-report", "flow-run-1", "", None, f"x-agent-{AGENT}"):
        assert not is_implicit_workset_key(key), key


def test_the_api_row_carries_the_flag():
    from app.api.v1.worksets import _serialize

    now = datetime.now(UTC)

    def row(key: str) -> SimpleNamespace:
        return SimpleNamespace(
            id=uuid.uuid4(),
            key=key,
            label=None,
            scope="global",
            scope_ref=None,
            status="active",
            processing="auto",
            workers=1,
            item_count=0,
            evicted_count=0,
            content_chars=0,
            max_items=1,
            max_content_chars=1,
            max_auto_calls=1,
            auto_calls_used=0,
            declared_total=None,
            declared_total_source=None,
            sealed_at=None,
            watermark_at=None,
            last_progress_at=None,
            expires_at=None,
            resolved_at=None,
            is_company_managed=False,
            owner_agent_id=None,
            created_at=now,
            updated_at=now,
        )

    assert _serialize(row(chat_workset_key(AGENT)))["implicit"] is True
    assert _serialize(row("reports"))["implicit"] is False


def test_a_single_bucket_needs_no_breakdown():
    only_pending = Progress(added=3, pending=3)
    assert "Of the" not in only_pending.describe()
    mixed = Progress(added=5, ready=2, evicted=3)
    assert "Of the 5 added: 2 summarised, 3 evicted" in mixed.describe()
