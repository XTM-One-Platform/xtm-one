# SPDX-License-Identifier: Apache-2.0

"""Preload heavyweight modules off the event loop.

Importing a large SDK chain (e.g. ``app.llm.router`` → ``AnthropicProvider``
→ ``anthropic`` → ``httpx`` → ``click``) inline inside async code blocks the
event-loop thread for seconds on first use, stalling every pending worker
coroutine.  This module is the single shared offload
helper for every async hot path that lazily imports such a chain — do NOT
copy the pattern into individual services.
"""

import asyncio
import importlib

# Modules whose first import has already been offloaded to a worker thread.
# Plain set (no lock): concurrent first calls may both offload, but
# ``importlib.import_module`` is idempotent and thread-safe, so the worst
# case is a redundant no-op thread hop during warm-up.  Process-wide by
# design: a module warmed up by one hot path (e.g. the assignment engine)
# short-circuits for every other one (e.g. the document processor).
_hot_path_modules_loaded: set[str] = set()


def _import_modules_sync(names: tuple[str, ...]) -> None:
    """Import modules synchronously (runs inside ``asyncio.to_thread``)."""
    for name in names:
        importlib.import_module(name)


async def ensure_hot_path_imports(*names: str) -> None:
    """Preload heavyweight modules without blocking the event loop.

    Offloads the first-load import I/O to a thread via ``asyncio.to_thread``;
    once a module has been loaded THROUGH THIS HELPER the call short-circuits
    to a set lookup (no thread hop).  A module first imported elsewhere still
    pays one redundant hop here — that hop is a cheap ``sys.modules`` lookup
    inside the thread.  Deliberately do NOT short-circuit on ``sys.modules``
    membership: a module appears there at the START of its import, so a
    concurrent warm-up call that skipped the offload would block the event
    loop on the import lock — exactly the stall this helper exists to avoid.

    Callers must still bind attributes at call time with a normal
    ``from module import name`` AFTER awaiting this helper — the import is
    then a cheap ``sys.modules`` lookup.  Never cache the function objects
    themselves: a frozen reference breaks call-time rebinding (e.g. test
    fixtures monkeypatching ``app.llm.router.get_llm_for_user``).
    """
    pending = tuple(n for n in names if n not in _hot_path_modules_loaded)
    if not pending:
        return
    await asyncio.to_thread(_import_modules_sync, pending)
    _hot_path_modules_loaded.update(pending)
