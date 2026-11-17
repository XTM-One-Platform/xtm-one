# SPDX-License-Identifier: Apache-2.0

"""Shared constants used across backend services and API routes.

These constants centralise commonly-used magic numbers — especially string
truncation limits — so that intent is clear and tuning is done in one place.
"""

# ---------------------------------------------------------------------------
# String truncation limits — used by services for LLM context, database
# fields, logging, and activity feeds.
# ---------------------------------------------------------------------------

TRUNCATE_LABEL = 200
"""Short labels: conversation titles, tool descriptions, error previews."""

TRUNCATE_PREVIEW = 300
"""Medium previews: message previews, bio snippets, summaries."""

TRUNCATE_SNIPPET = 500
"""Error response bodies, content previews, user message previews."""

TRUNCATE_QUESTION = 1000
"""User questions, HITL descriptions, task descriptions."""

TRUNCATE_ERROR = 2000
"""Full error messages, notification summaries."""

TRUNCATE_RESULT = 5000
"""Tool results, consulted/transferred text, large API responses."""

TRUNCATE_CONTENT = 10_000
"""Full content blocks (rarely used)."""

ADVISORY_CONSULT_RETURN_CAP = 6000
"""Advise-only consult (WS10 / D3) return cap injected into the parent loop.

A consulted specialist ADVISES: it returns a decision + a compact spec, never a
payload. Capping the text handed back to the orchestrator's live context (the
full answer is still persisted on the child AssignmentRun) is a belt on top of
the ~160K live-tool-result budget so 3-5 parallel advisory consults can never
bloat the orchestrator's context."""
