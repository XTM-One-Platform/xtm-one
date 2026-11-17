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

"""What a knowledge base sync error says, and to whom.

A failed sync keeps the full text of what went wrong - the exception message of
the failed run, which may carry a driver message or a request URL - because the
people who fix the source need it. That text lives in two places
served to every reader of the knowledge base: ``KBSyncSource.last_sync_error``
and the sync entries of its activity feed. So what is served is decided per
caller, by :func:`served_sync_text`:

* a caller who can manage the knowledge base (``helpers.can_manage``) is served
  exactly what they were always served;
* any other reader is served a fixed sentence wherever the stored text may
  carry a sync error. The sentence is chosen by the platform's own wording of
  the failure (a timeout, a stall, a quarantine, a configuration error), never
  by the exception text: a failure is classified by its exception type,
  where it happens, and once a sync error is stored all there is left to
  classify is the text meant for managers.

The activity search runs the same choice in SQL
(``knowledge_bases._activity_search_filter``, built from the rules below), so a
reader's search and its count match only the sentences a reader is served.
Nothing stored changes, and the server log keeps the full error with the
knowledge base and source ids.

Imports nothing but the stdlib and ``kb_document_errors``: the sync scheduler,
which writes these messages, and the knowledge base API, which serves them,
both import it.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.services.kb_document_errors import public_activity_details

# The scheduler's wording of a failure, which the reader rules match on. Every
# writer builds its message from these, so the two cannot drift apart.

#: ``last_sync_error`` of a source a watchdog quarantined: the stale-reset one
#: and the error-count one. Force re-sync and re-enabling match
#: on it to re-enable an auto-disabled source, never a manually disabled one.
QUARANTINE_ERROR_PREFIX = "Source quarantined after"
#: ``last_sync_error`` of a source disabled by a permanent configuration error
#: (e.g. a sharing link used as a site_id), matched the same way.
CONFIG_ERROR_PREFIX = "Source disabled after configuration error:"
#: ``last_sync_error`` of a source disabled after consecutive permanent (404 /
#: 410) errors, matched the same way.
PERM_FAIL_ERROR_PREFIX = "Source needs reconfiguration after"
SYNC_TIMEOUT_PREFIX = "Sync timed out after "
SYNC_STALLED_PREFIX = "Sync stalled (no progress for >"
REPEATED_FAILURES_PREFIX = "Sync has failed "
STALE_PAGES_PREFIX = "Stale Notion page reference(s) quarantined after "
PARTIAL_FAILURES_PREFIX = "Sync source has had page-level failures in "

#: Where a sync source's ``last_sync_error`` is served (every other place is
#: the event of an activity entry).
LAST_SYNC_ERROR = "last_sync_error"
SYNC_FAILED_EVENT = "sync_failed"
SYNC_ALERT_EVENT = "sync_alert"
SYNC_QUARANTINED_EVENT = "sync_quarantined"
SYNC_CONFIG_ERROR_EVENT = "sync_config_error"
#: The sync family of activity events. One the rules do not name is served to a
#: reader as a fixed sentence too, unless it is listed in
#: ``READER_VISIBLE_SYNC_EVENTS``: a new sync event is hidden from readers until
#: someone decides otherwise.
SYNC_EVENT_PREFIX = "sync_"
#: Sync events whose details are only ever the platform's own words (the source
#: type, a document count): a reader is served them as written.
READER_VISIBLE_SYNC_EVENTS: frozenset[str] = frozenset({"sync_started", "sync_completed"})


class SyncErrorCode(StrEnum):
    SYNC_FAILED = "sync_failed"
    TIMEOUT = "timeout"
    STALLED = "stalled"
    REPEATED_FAILURES = "repeated_failures"
    QUARANTINED = "quarantined"
    NEEDS_RECONFIGURATION = "needs_reconfiguration"
    CONFIGURATION_ERROR = "configuration_error"
    STALE_PAGES = "stale_pages"
    PARTIAL_FAILURES = "partial_failures"
    ALERT = "alert"
    SYNC_EVENT = "sync_event"


_MANAGERS_SEE_DETAILS = "Only people who can manage this knowledge base can see the details."

#: What a reader is served in place of the stored text.
READER_SENTENCES: dict[SyncErrorCode, str] = {
    SyncErrorCode.SYNC_FAILED: f"The sync failed. {_MANAGERS_SEE_DETAILS}",
    SyncErrorCode.TIMEOUT: f"The sync timed out. {_MANAGERS_SEE_DETAILS}",
    SyncErrorCode.STALLED: f"The sync stopped making progress and was reset. {_MANAGERS_SEE_DETAILS}",
    SyncErrorCode.REPEATED_FAILURES: f"The sync has failed several times in a row. {_MANAGERS_SEE_DETAILS}",
    SyncErrorCode.QUARANTINED: f"The source was disabled after repeated failed syncs. {_MANAGERS_SEE_DETAILS}",
    SyncErrorCode.NEEDS_RECONFIGURATION: (
        f"The source was disabled because its configured path or resource no longer exists. {_MANAGERS_SEE_DETAILS}"
    ),
    SyncErrorCode.CONFIGURATION_ERROR: (
        f"The source was disabled because of a configuration error. {_MANAGERS_SEE_DETAILS}"
    ),
    SyncErrorCode.STALE_PAGES: (
        f"Some Notion pages this source references no longer exist and are skipped. {_MANAGERS_SEE_DETAILS}"
    ),
    SyncErrorCode.PARTIAL_FAILURES: f"Some pages failed to sync in several runs in a row. {_MANAGERS_SEE_DETAILS}",
    SyncErrorCode.ALERT: f"A sync source needs attention. {_MANAGERS_SEE_DETAILS}",
    SyncErrorCode.SYNC_EVENT: "Only people who can manage this knowledge base can see the details of this sync event.",
}


@dataclass(frozen=True, slots=True)
class ReaderRule:
    """How a reader is served the stored text of one place.

    The first prefix the stored text starts with picks the sentence, else
    *default* does. A prefix only ever selects a sentence: what follows it is
    never served.
    """

    prefixes: tuple[tuple[str, SyncErrorCode], ...]
    default: SyncErrorCode

    def code_for(self, stored: str) -> SyncErrorCode:
        for prefix, code in self.prefixes:
            if stored.startswith(prefix):
                return code
        return self.default


LAST_SYNC_ERROR_RULE = ReaderRule(
    prefixes=(
        (SYNC_TIMEOUT_PREFIX, SyncErrorCode.TIMEOUT),
        (SYNC_STALLED_PREFIX, SyncErrorCode.STALLED),
        (QUARANTINE_ERROR_PREFIX, SyncErrorCode.QUARANTINED),
        (PERM_FAIL_ERROR_PREFIX, SyncErrorCode.NEEDS_RECONFIGURATION),
        (CONFIG_ERROR_PREFIX, SyncErrorCode.CONFIGURATION_ERROR),
    ),
    default=SyncErrorCode.SYNC_FAILED,
)

#: The sync events whose details may carry the error text, by event.
ACTIVITY_READER_RULES: dict[str, ReaderRule] = {
    SYNC_FAILED_EVENT: ReaderRule(
        prefixes=((SYNC_TIMEOUT_PREFIX, SyncErrorCode.TIMEOUT),),
        default=SyncErrorCode.SYNC_FAILED,
    ),
    SYNC_ALERT_EVENT: ReaderRule(
        prefixes=(
            (REPEATED_FAILURES_PREFIX, SyncErrorCode.REPEATED_FAILURES),
            (STALE_PAGES_PREFIX, SyncErrorCode.STALE_PAGES),
            (PARTIAL_FAILURES_PREFIX, SyncErrorCode.PARTIAL_FAILURES),
        ),
        default=SyncErrorCode.ALERT,
    ),
    SYNC_QUARANTINED_EVENT: ReaderRule(
        prefixes=((PERM_FAIL_ERROR_PREFIX, SyncErrorCode.NEEDS_RECONFIGURATION),),
        default=SyncErrorCode.QUARANTINED,
    ),
    SYNC_CONFIG_ERROR_EVENT: ReaderRule(prefixes=(), default=SyncErrorCode.CONFIGURATION_ERROR),
}

#: Every other event of the sync family but ``READER_VISIBLE_SYNC_EVENTS``.
UNLISTED_SYNC_EVENT_RULE = ReaderRule(prefixes=(), default=SyncErrorCode.SYNC_EVENT)


def reader_rule(where: str) -> ReaderRule | None:
    """The rule a reader is served the text of *where* by, ``None`` if served as is."""
    if where == LAST_SYNC_ERROR:
        return LAST_SYNC_ERROR_RULE
    rule = ACTIVITY_READER_RULES.get(where)
    if rule is not None:
        return rule
    if where.startswith(SYNC_EVENT_PREFIX) and where not in READER_VISIBLE_SYNC_EVENTS:
        return UNLISTED_SYNC_EVENT_RULE
    return None


def served_sync_text(where: str, stored: str | None, *, can_manage: bool) -> str | None:
    """The text a caller is served for a knowledge base's sync error surfaces.

    *where* is ``LAST_SYNC_ERROR`` for a sync source's ``last_sync_error``, else
    the event of the activity entry whose details *stored* is. A caller who can
    manage the knowledge base is served what they were always served: the stored
    ``last_sync_error``, and an entry's details as ``public_activity_details``
    cleans them. Any other caller is also served that, except where the
    stored text may carry a sync error: there, a fixed sentence.
    """
    served = stored if where == LAST_SYNC_ERROR else public_activity_details(where, stored)
    if can_manage or not stored:
        return served
    rule = reader_rule(where)
    if rule is None:
        return served
    return READER_SENTENCES[rule.code_for(stored)]
