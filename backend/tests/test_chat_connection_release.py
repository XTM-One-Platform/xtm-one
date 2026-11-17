"""Chat paths must release the DB connection before the long agentic loop.

Mirrors ``test_assignment_engine_connection_release.py`` for the chat side:
``run_agent_chat`` and ``run_general_chat`` can hold an open transaction for
the entire agentic loop (minutes of LLM + tool calls), pinning a pool
connection per concurrent chat and starving the default 10+10 pool. The
shared :func:`app.core.database.release_db_connection_before_loop` helper
commits any open transaction right before the loop so the connection goes
back to the pool; SQLAlchemy transparently re-acquires one on the session's
next use (post-loop persistence keeps working unchanged).
"""

import inspect

import pytest

from app.core.database import release_db_connection_before_loop
from app.services import agent_chat as agent_chat_module
from app.services import assignment_engine as assignment_engine_module
from app.services import general_chat as general_chat_module
from app.services.agent_chat import run_agent_chat
from app.services.general_chat import run_general_chat


class FakeSession:
    def __init__(self, *, in_transaction: bool, fail_commit: bool = False):
        self._in_transaction = in_transaction
        self._fail_commit = fail_commit
        self.commits = 0
        self.rollbacks = 0

    def in_transaction(self) -> bool:
        return self._in_transaction

    async def commit(self) -> None:
        if self._fail_commit:
            raise RuntimeError("boom commit")
        self.commits += 1
        self._in_transaction = False

    async def rollback(self) -> None:
        self.rollbacks += 1
        self._in_transaction = False


@pytest.mark.asyncio
async def test_shared_helper_commits_open_transaction():
    db = FakeSession(in_transaction=True)

    await release_db_connection_before_loop(db)

    assert db.commits == 1
    assert db.rollbacks == 0
    assert not db.in_transaction()


@pytest.mark.asyncio
async def test_shared_helper_commit_error_triggers_rollback(
    caplog: pytest.LogCaptureFixture,
):
    """A failed commit must roll back (best effort) and never raise, so the
    chat request keeps working with a held connection instead of crashing."""
    db = FakeSession(in_transaction=True, fail_commit=True)

    with caplog.at_level("WARNING"):
        await release_db_connection_before_loop(db)

    assert db.commits == 0
    assert db.rollbacks == 1
    assert not db.in_transaction()
    assert "Failed to release DB connection before loop" in caplog.text


def _release_call_position(source: str, func_name: str) -> int:
    # Match the call prefix only (not the full argument list) so harmless
    # refactors of the call site don't break the ordering contract below.
    needle = "await release_db_connection_before_loop("
    pos = source.find(needle)
    assert pos != -1, (
        f"{func_name} must call release_db_connection_before_loop() before "
        "invoking the agentic loop"
    )
    return pos


def test_run_agent_chat_releases_connection_before_loop():
    """The release call must precede the agentic loop, which is now the
    single execution path for BOTH tool-enabled and no-tools conversations
    (the duplicated no-tools direct streaming fork was removed by CORE-11 /
    ``test_no_tools_unification.py`` pins that the fork stays gone)."""
    source = inspect.getsource(run_agent_chat)

    release_pos = _release_call_position(source, "run_agent_chat")
    loop_pos = source.find("run_agent_loop(")

    assert loop_pos != -1, "run_agent_chat must invoke run_agent_loop"
    assert release_pos < loop_pos, (
        "release_db_connection_before_loop must be called BEFORE run_agent_loop"
    )


def test_run_general_chat_releases_connection_before_loop():
    """Same contract for the general assistant chat path."""
    source = inspect.getsource(run_general_chat)

    release_pos = _release_call_position(source, "run_general_chat")
    loop_pos = source.find("run_agent_loop(")

    assert loop_pos != -1, "run_general_chat must invoke run_agent_loop"
    assert release_pos < loop_pos, (
        "release_db_connection_before_loop must be called BEFORE run_agent_loop"
    )


def test_run_public_agent_chat_releases_connection_before_loop():
    """The public chat widget is anonymous / embeddable and spikes during
    traffic bursts; it must release the pooled connection before the agentic
    loop like the authenticated chat paths. It was the only direct
    ``run_agent_loop`` caller that otherwise held its session across the whole
    generation, pinning one pooled connection per concurrent visitor.
    The turn runs in ``_run_public_agent_turn``, behind the quota gate of
    ``_run_public_agent_chat``."""
    from app.api.v1.public_chat import _run_public_agent_turn

    source = inspect.getsource(_run_public_agent_turn)

    release_pos = _release_call_position(source, "_run_public_agent_turn")
    loop_pos = source.find("run_agent_loop(")

    assert loop_pos != -1, "_run_public_agent_turn must invoke run_agent_loop"
    assert release_pos < loop_pos, (
        "release_db_connection_before_loop must be called BEFORE run_agent_loop"
    )


def test_all_loop_runners_use_shared_helper():
    """All three loop-running services must use the shared helper from
    app.core.database (not a local copy), so the release semantics can never
    drift between the assignment engine and the chat paths."""
    assert (
        agent_chat_module.release_db_connection_before_loop
        is release_db_connection_before_loop
    )
    assert (
        general_chat_module.release_db_connection_before_loop
        is release_db_connection_before_loop
    )
    assert (
        assignment_engine_module.release_db_connection_before_loop
        is release_db_connection_before_loop
    )
