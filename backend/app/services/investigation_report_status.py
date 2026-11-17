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

"""Whether a run's cited report is being written.

The report is written after the run ends, by the chat turn that started it
(`deep_investigation_chat._write_report`), and a run may end with none at
all: no source a report could stand on, no model, a draft that kept citing
nothing. The block used to guess "writing" for three minutes after every
completed run. The writer now says so instead: a Redis marker set as soon as
the run's row exists (`DeepInvestigationOrchestrator.run(on_created=...)`) and
cleared when the writer returns, whatever it wrote. The TTL only bounds a
writer that died without clearing it.

Fail-open toward saying nothing: a marker that could not be read is
``unknown`` (the block keeps its old wait), never a claimed activity.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from typing import Any

from app.core.utils import format_exception_message

logger = logging.getLogger(__name__)

#: A run's budget plus its report, with room: only a crashed writer waits it out.
REPORT_WRITING_TTL_SECONDS = 30 * 60

#: One Redis round trip, bounded: a read endpoint never waits on Redis.
_REDIS_TIMEOUT_SECONDS = 2.0

REPORT_STATUSES = ("written", "writing", "none", "unknown")


async def _redis():
    """The shared client; a seam the unit suite makes fail open."""
    from app.core.redis import redis_client

    return await redis_client()


def report_writing_key(investigation_id: Any) -> str:
    return f"investigation:report_writing:{investigation_id}"


async def mark_report_writing(investigation_id: Any) -> None:
    """Say a report is coming for this run. Never raises."""
    try:
        client = await _redis()
        await asyncio.wait_for(
            client.set(report_writing_key(investigation_id), "1", ex=REPORT_WRITING_TTL_SECONDS),
            _REDIS_TIMEOUT_SECONDS,
        )
    except Exception as exc:  # a status hint must not fail a run
        logger.warning("investigation %s: report marker not set: %s", investigation_id, format_exception_message(exc))


async def clear_report_writing(investigation_id: Any) -> None:
    """The writer is done, whatever it wrote. Never raises."""
    try:
        client = await _redis()
        await asyncio.wait_for(client.delete(report_writing_key(investigation_id)), _REDIS_TIMEOUT_SECONDS)
    except Exception as exc:
        logger.warning(
            "investigation %s: report marker not cleared (expires in %ss): %s",
            investigation_id,
            REPORT_WRITING_TTL_SECONDS,
            format_exception_message(exc),
        )


async def reports_being_written(investigation_ids: Iterable[Any]) -> set[str] | None:
    """The ids among these whose report is being written, or None when Redis did not answer."""
    ids = [str(i) for i in investigation_ids]
    if not ids:
        return set()
    try:
        client = await _redis()
        values = await asyncio.wait_for(
            client.mget([report_writing_key(i) for i in ids]), _REDIS_TIMEOUT_SECONDS
        )
    except Exception as exc:
        logger.warning("report markers not read: %s", format_exception_message(exc))
        return None
    return {i for i, value in zip(ids, values, strict=True) if value is not None}


def report_status(investigation: Any, writing: set[str] | None) -> str:
    """``written`` | ``writing`` | ``none`` | ``unknown`` (the markers could not be read)."""
    if investigation.report:
        return "written"
    if writing is None:
        return "unknown"
    return "writing" if str(investigation.id) in writing else "none"
