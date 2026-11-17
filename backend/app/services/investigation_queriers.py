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

"""Queriers: how one source is actually interrogated.

A querier turns ``(source, subject, url)`` into a ``SourceOutcome`` — facts
about what came back, never a step status.  The orchestrator derives the status
from those facts, which is what makes "a failed source can never be stored as a
success" structural rather than a convention each querier has to remember.

This module holds the ``web_fetch`` querier, the only one wired in this slice.
Integration-backed packs (VirusTotal, OpenCTI, Recorded Future) become further
queriers over the same loop.

Mapping transport failures onto the error taxonomy
--------------------------------------------------
``investigation_state.STEP_ERROR_TYPES`` is the vocabulary the block, the
report and the ``investigation.source_error`` metrics all share, so every
failure has to land on exactly one of its six values.  The mapping below is
opinionated where the HTTP semantics are ambiguous:

* **5xx maps to ``unreachable``, not ``invalid_response``.**  From the
  analyst's point of view a source that returns 500 could not answer at all;
  ``invalid_response`` is reserved for a source that *did* answer (2xx) with
  something unusable, which is a different remediation.
* **An empty 2xx is not an error.**  A page that fetched cleanly and yielded no
  text means the source answered and had nothing — that is ``empty``, reached
  by reporting zero findings, and the distinction from ``error`` is exactly
  what the block is required to show.
* **Truncation is ``partial``, not success.**  A page cut off at the character
  cap was only partly read, so the step is ``degraded``: the analyst should
  know the source was not fully consumed before treating its absence of
  findings as meaningful.

Three of these failures carry NO ``detail_code`` on purpose: their message is
lifted from a third-party exception (``str(httpx.ConnectError)``) and cannot be
translated. They fall back to the free-text ``status_detail``, which is what
the block rendered for everything before codes existed — so the untranslatable
case degrades to the previous behaviour rather than losing the diagnostic.

No confidence is ever produced here — see the orchestrator's module docstring.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote as url_quote

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.redaction import redact_secrets
from app.core.utils import format_exception_message, safe_uuid
from app.services.investigation_opencti import SEED_IP_SEPARATOR, known_label
from app.services.investigation_orchestrator import (
    Evidence,
    SourceOutcome,
    SourceQuerier,
)
from app.services.investigation_packs import (
    ACTION_MAP_INFRASTRUCTURE,
    ACTION_PIVOT_NEIGHBOURS,
    FINGERPRINT_SETS,
    HOSTNAME_KEYWORD_SETS,
    ONE_SUBJECT_TYPES,
    OPENCTI_PROVIDER,
    PASSIVE_DNS_MAX_PAGES,
    SHARED_IP_MIN_SEEDS,
    VIRUSTOTAL_PROVIDER,
    FingerprintSignal,
    SourceDefinition,
    ascii_host,
    classify_subject,
    display_host,
    host_spellings,
    neighbour_leads,
    pivot_cap,
    pivot_leads,
    seed_ip_leads,
    site_of,
)
from app.services.web_fetch import extract_from_html, fetch_and_extract, is_html_media_type

logger = logging.getLogger(__name__)

#: Markdown that would change how a label RENDERS, or be read as a
#: citation marker, stripped from any text a third party chose.
_MARKUP = re.compile(r"[\[\]*_`~|\r\n]+")

#: Passages fetched from a knowledge base per source. A cap rather than
#: everything: the block shows the DOCUMENTS a step cited, and a run that
#: pulled two hundred passages from one corpus would render as a wall of
#: citations that says less than five does. Hitting it marks the step
#: `degraded`, so a capped read never reads as a complete one.
KB_TOP_K = 8

#: Passages a search with a relevance floor examines before it cuts to
#: `KB_TOP_K`: the floor filters what the fused ranking returned, so a narrow
#: pool would make a strict floor read as "nothing relevant" when only the
#: first few passages were looked at.
KB_FLOOR_POOL = KB_TOP_K * 3
#: How much of a matched passage is retained as the citation's quote.
#:
#: Long enough that a reader can recognise the claim in it, short enough that
#: a dozen citations do not become the corpus. Kept because a report that must
#: cite cannot be written from a list of filenames — a synthesis given only
#: "this document matched" has nothing to ground a sentence in, and would
#: invent the sentence.
MAX_PASSAGE_CHARS = 600

#: Characters of an MCP tool's textual result kept for the step detail. The
#: result itself is not stored — the step records that the tool answered and
#: with how much, not the payload.
MCP_DETAIL_CHARS = 300

#: Per-source fetch timeout. Deliberately shorter than ``web_fetch``'s own
#: 30 s default: a run queries several sources in sequence under an iteration
#: budget, and one slow source must not eat the whole run's wall clock.
DEFAULT_SOURCE_TIMEOUT = 15

#: Wall-clock budget for one knowledge-base or MCP source, connect included.
#:
#: Same rationale as `DEFAULT_SOURCE_TIMEOUT`, and the same number: a run
#: queries several sources in sequence under an ITERATION budget, which counts
#: iterations and not seconds, so nothing else in the loop bounds a source that
#: simply never answers. Without this, one hung server holds the whole run — and
#: for MCP that is worse than for HTTP, because connecting to a stdio transport
#: spawns a subprocess that stays parented to the worker for as long as the run
#: is stuck.
#:
#: `asyncio.TimeoutError` IS `TimeoutError` on 3.11+, so the existing
#: `except TimeoutError` arms below catch these and map them to the `timeout`
#: error type with no extra branch. Before this they were unreachable on the
#: connect/execute path.
SOURCE_CALL_TIMEOUT = DEFAULT_SOURCE_TIMEOUT

#: Character cap per source. Small on purpose — this slice counts a source as
#: having findings or not; it does not summarise the page, so pulling 80 000
#: characters would cost bandwidth and buy nothing.
DEFAULT_MAX_CHARS = 20_000

#: Below this many characters of extracted text, a 2xx HTML response is
#: treated as having produced nothing. Cookie walls, "no results" pages and JS
#: shells all return a short boilerplate body, and counting those as a finding
#: would make `empty` unreachable in practice — which would put the block right
#: back into the prior art's "everything looks fine" failure mode.
MIN_USEFUL_CHARS = 200

#: The same threshold for a structured response, an order of magnitude lower.
#: A JSON body has no boilerplate: a compact answer IS the answer. Shodan's
#: InternetDB returns a valid, useful 109-character document for a clean IP,
#: and judging it by the HTML threshold would report a working source as
#: `empty` — the exact silent wrongness this taxonomy exists to prevent.
MIN_USEFUL_JSON_CHARS = 40


def _passage(text: str) -> str:
    """Fit third-party text into a citable passage, secrets first.

    The same argument as `_third_party_detail`, and it does not depend on the
    call having FAILED: an HTTP-backed MCP tool that echoes its request on a
    2xx — debug wrappers, reflectors, verbose gateways — puts an
    `Authorization` header straight into a quote. Ingested corpus documents
    routinely carry pasted credentials for the same reason.

    A secret here travels much further than one in `status_detail`. That sits
    in a column; this is persisted to `investigations.report_sources`,
    interpolated verbatim into the synthesis prompt sent to a third-party
    model provider, and rendered into the chat transcript. Redaction before
    truncation, because the cap bounds volume and not sensitivity.
    """
    # Redacted once and held: the patterns are regex-heavy and `text` here is a
    # whole page body or corpus chunk, one per evidence item on every step.
    cleaned = redact_secrets(text)
    fitted = cleaned[:MAX_PASSAGE_CHARS]
    if len(fitted) < len(cleaned):
        # Cut back to a boundary. A passage ending "the campaign began in
        # Febr" is offered to the model as a complete excerpt, and the model
        # has no way to know it was cut — so it can build a claim on half a
        # fact and cite it properly. Sentence end first, word boundary as the
        # fallback; the raw cut stands only if neither exists, which means the
        # passage has no whitespace at all.
        for end in (
            max(fitted.rfind(". "), fitted.rfind("! "), fitted.rfind("? ")),
            fitted.rfind(" "),
        ):
            if end > MAX_PASSAGE_CHARS // 2:
                return fitted[: end + 1].strip()
    return fitted


def _third_party_detail(text: str, *, limit: int = MCP_DETAIL_CHARS) -> str | None:
    """Fit third-party text into ``status_detail``, secrets first.

    Redaction BEFORE truncation, and truncation is not a substitute for it: a
    bearer token is comfortably inside the cap. Everything this is applied to —
    an exception message from a third-party client, an MCP server's own error
    string — is text we did not write, reaching a column that is persisted on
    the step and rendered in the block, and the common leak is an error that
    echoes the request that produced it.

    Applied to the tool's own error string as well as to exception messages.
    They are the same exposure by different routes, and treating only one of
    them is how the gap got there.
    """
    return redact_secrets(text)[:limit].strip() or None


def _is_structured(content_type: str) -> bool:
    """Whether the body is structured data rather than a rendered page.

    Matches ``application/json`` and its suffixed variants (RDAP answers
    ``application/rdap+json``), plus XML, which has the same property: no
    presentational boilerplate to filter out.
    """
    ct = (content_type or "").split(";")[0].strip().lower()
    return ct.endswith(("json", "xml"))


def _status_error_type(status_code: int) -> str:
    if status_code == 429:
        return "rate_limited"
    if status_code in (404, 410):
        return "not_found"
    if status_code in (401, 403):
        return "auth_error"
    if status_code >= 500:
        return "unreachable"
    return "invalid_response"


async def _guarded_fetch(
    url: str, *, max_chars: int, timeout: int, **fetch_options: Any
) -> tuple[Any, SourceOutcome | None, bool]:
    """Fetch ``url``: ``(result, failure, answered)``.

    One mapping of transport failures onto the error taxonomy, shared by every
    querier that fetches a page, so a page that 404s reads the same whichever
    kind of source asked for it. ``answered`` says whether the server replied
    at all, a status included: the taxonomy files a 5xx under `unreachable`
    with a transport failure, and a caller retrying elsewhere must tell them
    apart by this, not by a detail code.
    """
    try:
        result = await fetch_and_extract(
            url, max_chars=max_chars, timeout=timeout, **fetch_options
        )
    except httpx.HTTPStatusError as exc:
        return None, SourceOutcome(
            error_type=_status_error_type(exc.response.status_code),
            detail=f"HTTP {exc.response.status_code}",
            detail_code="source.http_status",
            detail_params={"status": exc.response.status_code},
        ), True
    except (httpx.TimeoutException, TimeoutError):
        return None, SourceOutcome(
            error_type="timeout",
            detail=f"no response in {timeout}s",
            detail_code="source.timed_out",
            detail_params={"seconds": timeout},
        ), False
    except httpx.ConnectError as exc:
        return None, SourceOutcome(error_type="unreachable", detail=str(exc)), False
    except httpx.HTTPError as exc:
        # Any other transport-level httpx failure (read error, protocol
        # error, too many redirects surfaced as an httpx error).
        return None, SourceOutcome(error_type="unreachable", detail=str(exc)), False
    except ValueError as exc:
        # `fetch_and_extract` raises ValueError for an SSRF-refused URL and
        # for the redirect cap. Both mean this source cannot be queried as
        # configured — a pack problem, not a transient one.
        return None, SourceOutcome(error_type="invalid_response", detail=str(exc)), False
    return result, None, True


def make_web_fetch_querier(
    *,
    timeout: int = DEFAULT_SOURCE_TIMEOUT,
    max_chars: int = DEFAULT_MAX_CHARS,
    min_useful_chars: int = MIN_USEFUL_CHARS,
    min_useful_json_chars: int = MIN_USEFUL_JSON_CHARS,
) -> SourceQuerier:
    """Build a querier that fetches a source's URL and reports what it got.

    Returns no leads: extracting further indicators from free text needs a
    model pass that is not part of this slice (the orchestrator keeps the
    multi-hop machinery regardless).
    """

    async def _query(source: SourceDefinition, _subject_value: str, url: str) -> SourceOutcome:
        if source.pinned:
            return await _pinned_query(
                source,
                url,
                timeout=timeout,
                max_chars=max_chars,
                min_useful_chars=min_useful_chars,
                min_useful_json_chars=min_useful_json_chars,
            )
        result, failure, _answered = await _guarded_fetch(url, max_chars=max_chars, timeout=timeout)
        if failure is not None:
            return failure

        text = (result.text or "").strip()
        threshold = min_useful_json_chars if _is_structured(result.content_type) else min_useful_chars
        if len(text) < threshold:
            # Zero findings → the orchestrator derives `empty`. Not an error:
            # the source answered.
            return SourceOutcome(
                findings_count=0,
                evidence=(Evidence(kind="url", label=result.url, href=result.url),),
                detail=(None if not text else f"page returned only {len(text)} characters of text"),
                detail_code=None if not text else "source.thin_response",
                detail_params=None if not text else {"chars": len(text)},
            )

        return SourceOutcome(
            findings_count=1,
            evidence=(Evidence(kind="url", label=result.url, href=result.url),),
            partial=result.truncated,
            detail=(f"content truncated at {max_chars} characters" if result.truncated else None),
            detail_code="source.truncated" if result.truncated else None,
            detail_params={"max_chars": max_chars} if result.truncated else None,
        )

    return _query


async def _pinned_query(
    source: SourceDefinition,
    url: str,
    *,
    timeout: int,
    max_chars: int,
    min_useful_chars: int = MIN_USEFUL_CHARS,
    min_useful_json_chars: int = MIN_USEFUL_JSON_CHARS,
) -> SourceOutcome:
    """A free HTTP source (a URL a built pack's author typed), fetched pinned.

    What the step says is generic: the precise reason (which name resolved
    where, which policy refused it) goes to the logs only, so a pack's author
    cannot use the step's detail to map the network the platform sees.

    What it answered is judged like a shared fetch's page: an HTML page is
    read through the shared extractor, so its markup is never the text, and a
    thin answer (a cookie wall, a "Not found" page, a JS shell) is `empty`
    with `source.thin_response`, by the same thresholds. Its quote goes
    through `_passage`, secrets first, like every other querier's: it is
    stored on the step and returned by the investigation's REST read.
    """
    from app.services.pinned_fetch import PinnedFetchRefusedError, fetch_pinned

    try:
        result = await fetch_pinned(url, timeout=timeout)
    except PinnedFetchRefusedError as refused:
        logger.info("free HTTP source %r refused: %r", source.name[:80], refused.reason[:300])
        if refused.kind == "timeout":
            return SourceOutcome(
                error_type="timeout",
                detail=f"no response in {timeout}s",
                detail_code="source.timed_out",
                detail_params={"seconds": timeout},
            )
        if refused.kind == "status" and refused.status is not None:
            return SourceOutcome(
                error_type=_status_error_type(refused.status),
                detail=f"HTTP {refused.status}",
                detail_code="source.http_status",
                detail_params={"status": refused.status},
            )
        return SourceOutcome(
            error_type="invalid_response",
            detail="this address could not be read",
            detail_code="source.free_http_refused",
            detail_params={},
        )
    page = result.text
    if is_html_media_type(result.content_type):
        from app.core.parse_offload import run_parse

        page, _title = await run_parse(lambda: extract_from_html(result.text, url=result.url))
    text = page.strip()[:max_chars]
    threshold = min_useful_json_chars if _is_structured(result.content_type) else min_useful_chars
    if len(text) < threshold:
        return SourceOutcome(
            findings_count=0,
            evidence=(Evidence(kind="url", label=url, href=url),),
            detail=(None if not text else f"page returned only {len(text)} characters of text"),
            detail_code=None if not text else "source.thin_response",
            detail_params=None if not text else {"chars": len(text)},
        )
    return SourceOutcome(
        findings_count=1,
        evidence=(Evidence(kind="url", label=url, href=url, quote=_passage(" ".join(text.split()))),),
        partial=result.capped,
        detail=("only the first part of the response was read" if result.capped else None),
        detail_code="source.free_http_capped" if result.capped else None,
        detail_params={} if result.capped else None,
    )


# ── Knowledge-base sources ──────────────────────────────────────────


def make_knowledge_base_querier(db: AsyncSession, *, user_id=None) -> SourceQuerier:
    """Query a pack's knowledge bases for the subject.

    The citation is the point. A hit is a DOCUMENT, so the evidence names the
    document rather than inventing a URL for it — a knowledge base may be
    shared with the agent and not with the reader, and a citation that renders
    as a link they cannot open is worse than one that renders as a name.

    Distinct documents are counted, not chunks: three passages from one report
    are one source of evidence, and reporting "3 findings" for them would
    inflate the coverage the block shows.

    ``user_id`` re-applies the caller's visibility at QUERY time. Authorization
    already happens at selection — `investigation_pack_resolver` builds KB packs
    from visible corpora only — but that is one layer, in one place, and this is
    a public factory that will answer for any `SourceDefinition` it is handed.
    Two ways that bites: a pack persisted on a run and re-resolved after a
    corpus was unshared still names it, and the first caller to build a source
    from a request body inherits the gap with nothing behind it. The re-check
    is the knowledge bases' own three-tier visibility; an embedding's
    ``user_id`` is its knowledge base's OWNER, so filtering on it would hide
    every corpus shared with the caller.

    Every read runs on a short session of its own, never the orchestrator's:
    a failed or timed-out search must fail this step, not abort the
    transaction the run records its next step on.
    """
    del db

    async def _query(
        source: SourceDefinition, subject_value: str, query: str
    ) -> SourceOutcome:
        from app.core.database import async_session

        async with async_session() as session:
            return await _query_on(session, source, subject_value)

    async def _query_on(
        db: AsyncSession, source: SourceDefinition, subject_value: str
    ) -> SourceOutcome:
        from sqlalchemy import select

        from app.api.v1.helpers import get_user_group_ids, user_visible_filter
        from app.llm.router import get_embedding_provider
        from app.models.knowledge_base import KnowledgeBase
        from app.services.hybrid_search import hybrid_search_embeddings

        knowledge_base_ids = list(source.knowledge_base_ids)
        # What the corpus is asked: the subject, or the pack's question about it.
        question = source.knowledge_base_query(subject_value)
        # `str()`: callers hand a `UUID` as often as a string.
        uid = safe_uuid(str(user_id)) if user_id else None
        if uid is not None:
            group_ids = await get_user_group_ids(db, uid)
            # As strings: the database hands back UUID objects, and the
            # search parses each id with `uuid.UUID(...)`, which accepts a
            # string only — every caller-checked corpus search failed on it.
            knowledge_base_ids = [
                str(kb_id)
                for kb_id in (
                    await db.execute(
                        select(KnowledgeBase.id).where(
                            KnowledgeBase.id.in_(knowledge_base_ids),
                            user_visible_filter(KnowledgeBase, uid, group_ids),
                        )
                    )
                ).scalars()
            ]
            if not knowledge_base_ids:
                return SourceOutcome(
                    error_type="not_found",
                    detail="none of these knowledge bases is available to you",
                    detail_code="source.kb_unavailable",
                    detail_params={},
                )

        try:
            provider, model, _ = await get_embedding_provider(db)
            query_embedding = await asyncio.wait_for(
                provider.embed(question, model=model),
                timeout=SOURCE_CALL_TIMEOUT,
            )
        except TimeoutError:
            return SourceOutcome(
                error_type="timeout",
                detail=(
                    f"the embedding provider did not answer within "
                    f"{SOURCE_CALL_TIMEOUT}s"
                ),
                detail_code="source.timed_out",
                detail_params={"seconds": SOURCE_CALL_TIMEOUT},
            )
        except Exception as exc:
            # No provider, or it refused. `unreachable` rather than
            # `invalid_response`: nothing about the corpus is wrong, the thing
            # that turns a question into a vector is simply not answering.
            return SourceOutcome(
                error_type="unreachable",
                detail=_third_party_detail(
                    f"the embedding provider could not be reached: "
                    f"{format_exception_message(exc)}"
                ),
                detail_code="source.kb_provider_unreachable",
                detail_params={},
            )

        try:
            results = await asyncio.wait_for(
                hybrid_search_embeddings(
                    db,
                    query_text=question,
                    query_embedding=query_embedding,
                    knowledge_base_ids=knowledge_base_ids,
                    # One more than the cap, so "there is more" is OBSERVED
                    # rather than inferred from a full page. A corpus holding
                    # exactly `KB_TOP_K` chunks — or one document chunked into
                    # that many passages — was read completely, and marking it
                    # `degraded` with "the corpus may hold more" is a false
                    # degraded. Same class of harm as a false success.
                    top_k=(KB_FLOOR_POOL if source.min_similarity is not None else KB_TOP_K) + 1,
                    expected_embedding_dimensions=len(query_embedding),
                    expected_embedding_model=model,
                ),
                timeout=SOURCE_CALL_TIMEOUT,
            )
        except TimeoutError:
            return SourceOutcome(
                error_type="timeout",
                detail=(
                    f"the knowledge-base search did not finish within "
                    f"{SOURCE_CALL_TIMEOUT}s"
                ),
                detail_code="source.timed_out",
                detail_params={"seconds": SOURCE_CALL_TIMEOUT},
            )
        except Exception as exc:
            return SourceOutcome(
                error_type="invalid_response",
                detail=_third_party_detail(
                    f"the knowledge-base search failed: "
                    f"{format_exception_message(exc)}"
                ),
                detail_code="source.kb_search_failed",
                detail_params={},
            )

        floor = source.min_similarity
        if floor is not None:
            # The search always returns the nearest passages; the floor is what
            # makes "nothing relevant here" reachable. A passage that matched
            # every word of a pack's question is kept whatever its similarity:
            # the text search found it on its own terms. Not for the bare
            # subject: one word can sit in any passage, so there every passage
            # meets the floor.
            words_decide = bool(source.query_template)
            more_ranked = len(results) > KB_FLOOR_POOL
            examined = results[:KB_FLOOR_POOL]
            relevant = [
                r
                for r in examined
                if r.vector_score >= floor or (words_decide and r.text_rank is not None)
            ]
            if examined and not relevant:
                return SourceOutcome(
                    findings_count=0,
                    detail=(
                        f"none of the {len(examined)} best-ranked passages reached "
                        f"the relevance floor ({floor:g})"
                    ),
                    detail_code="source.kb_below_floor",
                    detail_params={"floor": floor, "examined": len(examined)},
                )
            # The corpus holds more relevant passages when more than are shown
            # passed, or when every one examined passed and the ranking went on:
            # relevance had not run out inside the pool.
            capped = len(relevant) > KB_TOP_K or (more_ranked and len(relevant) == len(examined))
            results = relevant
        else:
            capped = len(results) > KB_TOP_K
        results = results[:KB_TOP_K]

        if not results:
            # The corpus answered and holds nothing on this subject. An
            # `empty` step, not an error — the same distinction the web
            # querier draws for a page that loaded and said nothing.
            return SourceOutcome(findings_count=0)

        evidence = await _document_evidence(db, results)
        return SourceOutcome(
            findings_count=len(evidence),
            evidence=tuple(evidence),
            # Truncated at the fetch cap: the corpus holds more — observed,
            # because one extra passage was asked for and came back.
            partial=capped,
            detail=(
                f"showing the top {KB_TOP_K} passages; the corpus holds more"
                if capped
                else None
            ),
            detail_code="source.kb_capped" if capped else None,
            detail_params={"top_k": KB_TOP_K} if capped else None,
        )

    return _query


async def _document_evidence(
    db: AsyncSession, results: Sequence[Any]
) -> list[Evidence]:
    """One citation per distinct document, in the order first seen.

    Ordering is by relevance because that is the order the search returned;
    de-duplication keeps the FIRST occurrence, so the passage that ranked
    highest is what named the document.

    The names are RESOLVED rather than read off the hit: `HybridResult` carries
    a `source_id` and no filename, so citing it directly would render every
    knowledge-base citation as a bare UUID — technically provenance, and
    useless to the analyst it is for. One query resolves the whole page.
    """
    import uuid as _uuid

    from sqlalchemy import select

    from app.models.knowledge_base import KnowledgeDocument

    ordered_ids: list[str] = []
    passages: dict[str, str] = {}
    seen: set[str] = set()
    for hit in results:
        doc_id = str(getattr(hit, "source_id", "") or "")
        if not doc_id or doc_id in seen:
            continue
        seen.add(doc_id)
        ordered_ids.append(doc_id)
        # The passage that ranked highest is the one kept, for the same reason
        # it is the one that names the document: it is what the search
        # actually matched on.
        text = str(getattr(hit, "content", "") or "").strip()
        if text:
            passages[doc_id] = _passage(text)
    if not ordered_ids:
        return []

    valid: dict[str, _uuid.UUID] = {}
    for raw in ordered_ids:
        try:
            valid[raw] = _uuid.UUID(raw)
        except (ValueError, AttributeError, TypeError):
            continue

    names: dict[str, str] = {}
    if valid:
        rows = (
            await db.execute(
                select(KnowledgeDocument.id, KnowledgeDocument.filename).where(
                    KnowledgeDocument.id.in_(list(valid.values()))
                )
            )
        ).all()
        names = {str(row[0]): row[1] for row in rows}

    # A document the search matched but the resolve missed (deleted between
    # the two queries) still gets a citation: the passage genuinely came from
    # somewhere, and dropping it would understate what the step read.
    return [
        Evidence(
            kind="document",
            label=str(names.get(doc_id) or "deleted document")[:200],
            # No span: a corpus passage backs a claim without backing a
            # position in prose nobody wrote. The quote is what a report
            # cites; there is no answer to index into.
            quote=passages.get(doc_id) or None,
        )
        for doc_id in ordered_ids
    ]


# ── MCP tool sources ────────────────────────────────────────────────


def _subject_argument(input_schema: dict | None) -> str | None:
    """The single text argument a subject can be passed as, or None.

    A designation names a tool, not a parameter, because an operator reading a
    tool list knows which tool looks things up and rarely knows its argument
    names. Those are in the tool's own JSON Schema, so they are read from
    there at call time rather than typed in.

    Deliberately narrow, in two directions. The subject must have exactly one
    place to go: a tool requiring both a repository and a query is one this
    cannot drive, and picking either would ask a well-formed question that is
    not the one the analyst asked. And nothing else may be left unfilled: a
    tool requiring a text query AND a numeric board id would fail on the
    missing id anyway, so it is refused here, where the message can say why.
    Only when the tool requires nothing at all are optional parameters
    considered, under the same uniqueness rule.

    Returning None where the answer is ambiguous is the entire point. The
    alternative — take the first, or match on a name like "query" — puts the
    subject into a parameter chosen by coincidence, on a tool that may do
    anything.
    """
    if not isinstance(input_schema, dict):
        return None
    properties = input_schema.get("properties")
    if not isinstance(properties, dict):
        return None

    def _is_text(name: str) -> bool:
        spec = properties.get(name)
        if not isinstance(spec, dict):
            return False
        declared = spec.get("type")
        # An absent type is any type, which includes text; a union counts when
        # text is among its members.
        if declared is None:
            return True
        if isinstance(declared, list):
            return "string" in declared
        return declared == "string"

    required = input_schema.get("required")
    required = [n for n in required if isinstance(n, str)] if isinstance(required, list) else []
    if required:
        # Exactly one required parameter, and it takes text. Any other shape —
        # two required strings, or a string beside a required number this has
        # no value for — leaves the call either ambiguous or incomplete.
        if len(required) == 1 and _is_text(required[0]):
            return required[0]
        return None

    optional = [n for n in properties if _is_text(n)]
    return optional[0] if len(optional) == 1 else None


def make_mcp_querier(db: AsyncSession, *, user_id: str | None = None) -> SourceQuerier:
    """Call one tool on one MCP server and record what it answered.

    Re-authorised at call time, not only when the pack was built: a share or
    a designation withdrawn while the run is queued must stop the call. The
    server must still be visible to ``user_id`` (a run with no user skips
    that check, as it does at selection) and the tool must still be the one
    designated for investigations.

    The citation names the SERVER and the TOOL, not a URL: an MCP result has
    no address, and the useful provenance is which tool on which server
    produced it. A reader chasing a surprising finding needs to know that, and
    the step's `query` already carries the arguments.

    The tool's payload is deliberately NOT stored. A step records that a source
    answered and with how much substance; the findings prose belongs to the
    report, and copying tool output into every step would make the block a log.

    Connections are opened and closed per source. That is more expensive than
    a shared manager, and it is the right trade here: an investigation queries
    a handful of sources once, and a manager living across the whole run would
    hold a subprocess or socket open for every server in every pack for the
    run's duration.
    """

    async def _query(
        source: SourceDefinition, subject_value: str, query: str
    ) -> SourceOutcome:
        import uuid as _uuid

        from app.api.v1.helpers import build_mcp_runtime
        from app.models.mcp_server import MCPServer
        from app.tools.base import ToolCall
        from app.tools.mcp_client import MCPAuthorizationRequiredError, MCPManager

        try:
            server_uuid = _uuid.UUID(str(source.mcp_server_id))
        except (ValueError, AttributeError, TypeError):
            return SourceOutcome(
                error_type="invalid_response",
                detail=f"{source.mcp_server_id!r} is not a valid MCP server id",
                detail_code="source.mcp_bad_server_id",
                detail_params={"server_id": str(source.mcp_server_id)},
            )

        # Re-read, never served from the session's identity map: the pack
        # resolver loaded this row earlier in the run, and a server disabled
        # or re-designated since must be seen as it is now.
        server = await db.get(MCPServer, server_uuid, populate_existing=True)
        uid = safe_uuid(str(user_id)) if user_id else None
        if server is not None and uid is not None:
            from sqlalchemy import select

            from app.api.v1.helpers import get_user_group_ids, user_visible_filter

            group_ids = await get_user_group_ids(db, uid)
            visible = (
                await db.execute(
                    select(MCPServer.id).where(
                        MCPServer.id == server_uuid,
                        user_visible_filter(MCPServer, uid, group_ids),
                    )
                )
            ).scalar_one_or_none()
            if visible is None:
                # Answered like a missing server: an unshared one is unknown.
                server = None
        if server is None:
            # A pack naming a server that no longer exists is a pack problem,
            # and `not_found` says exactly that rather than blaming the network.
            return SourceOutcome(
                error_type="not_found",
                detail=f"MCP server {source.mcp_server_id} no longer exists",
                detail_code="source.mcp_server_missing",
                detail_params={"server_id": str(source.mcp_server_id)},
            )
        if not server.enabled:
            return SourceOutcome(
                error_type="unreachable",
                detail=f"MCP server {server.name!r} is disabled",
                detail_code="source.mcp_server_disabled",
                detail_params={"server": server.name},
            )
        if server.investigation_tool_name != source.mcp_tool_name:
            # The designation was withdrawn or moved to another tool: this one
            # is no longer offered for investigations, whatever the server has.
            return SourceOutcome(
                error_type="not_found",
                detail=(
                    f"MCP server {server.name!r} no longer designates "
                    f"{source.mcp_tool_name!r} for investigations"
                ),
                detail_code="source.mcp_tool_missing",
                detail_params={"server": server.name, "tool": str(source.mcp_tool_name)},
            )

        manager = MCPManager()
        try:
            # Bounded, connect included: a stdio transport spawns a
            # subprocess here and can hang before any call is made. In THIS
            # task (`asyncio.timeout`, not `wait_for`'s own task): the
            # transport's cancel scopes are closed by `disconnect_all` below,
            # and a scope closed from another task fails and leaks the process.
            async with asyncio.timeout(SOURCE_CALL_TIMEOUT):
                tools = await manager.connect_server(await build_mcp_runtime(server))
            # Keyed on the server's RAW tool names, which is what
            # `MCPManager.execute_tool` dispatches on too (`has_tool`
            # compares `t.name` against the call's name, unsanitised). The
            # sanitising done elsewhere in the tool subsystem is for LLM-facing
            # names; it does not apply on this path, so a server exposing
            # `lookup.indicator` works end to end.
            offered = {t.name: t for t in tools}
            if source.mcp_tool_name not in offered:
                # Connected, but the tool is gone. Distinguished from a
                # connection failure because the fix is different: one is a
                # pack to update, the other a server to bring back.
                return SourceOutcome(
                    error_type="not_found",
                    detail=(
                        f"{server.name!r} does not offer a tool named "
                        f"{source.mcp_tool_name!r}"
                    ),
                    detail_code="source.mcp_tool_missing",
                    detail_params={
                        "server": server.name,
                        "tool": str(source.mcp_tool_name),
                    },
                )
            schema = getattr(offered[source.mcp_tool_name], "input_schema", None)
            argument = source.mcp_argument or _subject_argument(schema)
            required = set((schema or {}).get("required") or []) if isinstance(schema, dict) else set()
            if argument is not None and required - {argument}:
                # A pinned argument still leaves other required inputs unset:
                # the call would only fail the tool's own validation.
                argument = None
            if argument is None:
                # The tool is there and the connection is fine; what is
                # missing is the one thing nobody may guess — where the
                # subject goes. Sending it to an arbitrary parameter of a
                # tool whose other parameters are unknown is how an
                # investigation ends up doing something other than looking
                # up. `invalid_response` because the tool listing is what
                # came back unusable; the detail names the fix.
                return SourceOutcome(
                    error_type="invalid_response",
                    detail=(
                        f"{source.mcp_tool_name!r} does not take exactly one "
                        "text argument, so the subject cannot be placed. Set "
                        "the argument name on the server."
                    ),
                    detail_code="source.mcp_argument_ambiguous",
                    detail_params={"tool": str(source.mcp_tool_name)},
                )
            async with asyncio.timeout(SOURCE_CALL_TIMEOUT):
                result = await manager.execute_tool(
                    ToolCall(
                        id=str(_uuid.uuid4()),
                        name=str(source.mcp_tool_name),
                        arguments={argument: subject_value},
                    )
                )
        except MCPAuthorizationRequiredError as exc:
            return SourceOutcome(
                error_type="auth_error",
                detail=_third_party_detail(format_exception_message(exc)),
                detail_code="source.querier_error",
                detail_params={},
            )
        except TimeoutError:
            # Reachable now. Before the `wait_for` calls above, nothing on the
            # connect/execute path could raise this.
            return SourceOutcome(
                error_type="timeout",
                detail=f"{server.name} did not answer within {SOURCE_CALL_TIMEOUT}s",
                detail_code="source.timed_out",
                detail_params={"seconds": SOURCE_CALL_TIMEOUT},
            )
        except ConnectionError as exc:
            return SourceOutcome(
                error_type="unreachable",
                detail=_third_party_detail(format_exception_message(exc)),
                detail_code="source.querier_error",
                detail_params={},
            )
        except Exception as exc:
            return SourceOutcome(
                error_type="invalid_response",
                detail=_third_party_detail(format_exception_message(exc)),
                detail_code="source.querier_error",
                detail_params={},
            )
        finally:
            # Best-effort: a server that will not close must not fail a step
            # whose tool already answered.
            try:
                await manager.disconnect_all()
            except Exception:  # a failed close is not a failed step
                logger.debug("MCP disconnect failed after an investigation step", exc_info=True)

        citation = Evidence(
            kind="tool_result",
            label=f"{server.name} · {source.mcp_tool_name}",
        )

        if getattr(result, "is_error", False):
            # The tool ran and refused. Its own message is the diagnostic, and
            # it is far more useful than "invalid_response" alone.
            return SourceOutcome(
                error_type="invalid_response",
                # Through the same pass as every exception message on this
                # path. `result.content` is an arbitrary string from a
                # third-party server, and error strings from HTTP-backed tools
                # routinely echo the failing request — an Authorization header
                # included. It lands in `status_detail`, which is persisted and
                # rendered, so truncation alone does nothing: a bearer token is
                # well inside the cap.
                detail=_third_party_detail(str(result.content or "")),
                detail_code="source.mcp_tool_error",
                detail_params={},
                evidence=(citation,),
            )

        text = str(result.content or "").strip()
        if text:
            # Replaced, not appended: `citation` was built before the result
            # was read, and the quote is what a report can cite.
            citation = Evidence(
                kind=citation.kind,
                label=citation.label,
                href=citation.href,
                quote=_passage(text),
            )
        if not text:
            # Answered with nothing. `empty`, exactly like a page that loaded
            # and said nothing — the tool is working, the subject is not there.
            return SourceOutcome(findings_count=0, evidence=(citation,))

        return SourceOutcome(findings_count=1, evidence=(citation,))

    return _query


# ── Site fingerprints (disinformation infrastructure) ───────────────────────

#: Bytes of a page tested against a fingerprint set. The signals sit in the
#: page head and its first scripts; a larger page is tested on its first part
#: and the step is `degraded`, so a signal missed past the cut never reads as
#: a signal the site does not have.
FINGERPRINT_MAX_BYTES = 2_000_000

#: Wall-clock budget of one fingerprint, the HTTPS read and its HTTP fallback
#: together. The fetch's own ``timeout`` bounds each connect, read and
#: write, not the response: the investigated site's operator decides the
#: response, and one that sends a byte every few seconds trips neither that
#: timeout nor `FINGERPRINT_MAX_BYTES` and would hold a worker for hours. The
#: same budget as a knowledge-base or MCP source (`SOURCE_CALL_TIMEOUT`).
FINGERPRINT_STEP_TIMEOUT = SOURCE_CALL_TIMEOUT

#: Characters of page source kept on each side of a matched signal: enough to
#: recognise the tag it sits in, short enough that the quote stays a sentence
#: about the page rather than a dump of it.
FINGERPRINT_CONTEXT_CHARS = 40

#: Characters of a header value kept in a quote.
FINGERPRINT_HEADER_CHARS = 120

_WHITESPACE = re.compile(r"\s+")


def _excerpt(text: str, start: int, end: int) -> str:
    left = max(0, start - FINGERPRINT_CONTEXT_CHARS)
    right = min(len(text), end + FINGERPRINT_CONTEXT_CHARS)
    snippet = _WHITESPACE.sub(" ", text[left:right]).strip()
    return f"{'…' if left else ''}{snippet}{'…' if right < len(text) else ''}"


def _observed_on() -> str:
    return datetime.now(UTC).date().isoformat()


def fingerprint_quote(
    url: str, status: int, observed_on: str, signal: FingerprintSignal, excerpt: str
) -> str:
    """The citable record of one matched signal.

    Written by the platform, not lifted from the page, because what a report
    cites here is an OBSERVATION — this page, on this day, carried this trait —
    and the page source alone is markup that cannot carry a claim
    (`investigation_report.is_quotable`). Every value in it is copied from the
    response, and the matched source is quoted verbatim, so the sentence stays
    checkable against the page.
    """
    where = (
        "the page source"
        if signal.location == "body"
        else f"the {signal.header} response header"
    )
    return _passage(
        f"The home page {url} answered HTTP {status} on {observed_on} and shows "
        f"the signal {signal.label}: {where} contains “{excerpt}”."
    )


def _not_reached(failure: SourceOutcome | None, answered: bool) -> bool:
    """No answer at all: a connection that failed, not a status (a 5xx is an
    answer, and final). A timeout is not retried over another scheme either:
    a port that drops packets (a filtered 443) would cost the run a second
    full timeout for a site that is very likely down both ways."""
    return failure is not None and not answered and failure.error_type == "unreachable"


def make_site_fingerprint_querier(
    *, timeout: int = DEFAULT_SOURCE_TIMEOUT, deadline: float = FINGERPRINT_STEP_TIMEOUT
) -> SourceQuerier:
    """Fetch a site's home page and report which signals of its set it shows.

    One finding per matched signal, each with its own citation, and no verdict:
    a theme, a plugin or a host is common on its own, and what an analyst
    weighs is the combination. A page that answered with none of the signals is
    `empty`, not an error — the site is up and simply not built that way.

    The request goes to the investigated site itself, which is why the pack
    carrying this source is never auto-selected. ``timeout`` bounds each
    network operation, ``deadline`` the whole read, both schemes together.
    """

    async def _read(url: str, subject_value: str, fetch_options: dict) -> tuple[Any, SourceOutcome | None]:
        """The page over HTTPS, or over HTTP when a bare host is not reached."""
        result, failure, answered = await _guarded_fetch(url, **fetch_options)
        if _not_reached(failure, answered) and "://" not in subject_value:
            # A bare host unreachable over HTTPS may still serve plain HTTP;
            # an answer of any status (a 503 included) means the site is up,
            # and is final.
            fallback = "http://" + url.removeprefix("https://")
            result, http_failure, http_answered = await _guarded_fetch(fallback, **fetch_options)
            if _not_reached(http_failure, http_answered):
                return None, SourceOutcome(
                    error_type="unreachable",
                    detail=f"over HTTPS: {failure.detail}; over HTTP: {http_failure.detail}",
                )
            failure = http_failure
        return result, failure

    async def _query(
        source: SourceDefinition, subject_value: str, _asked: str
    ) -> SourceOutcome:
        signals = FINGERPRINT_SETS.get(str(source.fingerprint_set))
        if signals is None:
            # Unreachable through the constructor; a hand-built source naming a
            # set this build does not know is a pack problem, shown as one.
            return SourceOutcome(
                error_type="invalid_response",
                detail=f"unknown fingerprint set {source.fingerprint_set!r}",
            )
        url = source.fingerprint_url(subject_value)
        fetch_options = {
            "max_chars": DEFAULT_MAX_CHARS,
            "timeout": timeout,
            # The extracted text is not read; the cheaper extractor will do.
            "use_trafilatura": False,
            # The investigated site decides the response size: read no more
            # than is tested.
            "max_bytes": FINGERPRINT_MAX_BYTES,
        }
        try:
            # In this task (`asyncio.timeout`): the deadline cancels the read
            # where it stands, a trickled body included, and closes its
            # connection on the way out.
            async with asyncio.timeout(deadline):
                result, failure = await _read(url, subject_value, fetch_options)
        except TimeoutError:
            return SourceOutcome(
                error_type="timeout",
                detail=f"the site was not read within {deadline}s",
                detail_code="source.timed_out",
                detail_params={"seconds": deadline},
            )
        if failure is not None:
            return failure

        raw = result.body or b""
        capped = result.body_capped or len(raw) > FINGERPRINT_MAX_BYTES
        body = raw[:FINGERPRINT_MAX_BYTES].decode("utf-8", errors="replace")
        headers = result.headers or {}
        final_url = result.url
        page = _MARKUP.sub("", final_url)[:200]
        observed_on = _observed_on()

        evidence: list[Evidence] = []
        for signal in signals.signals:
            match = signal.search(body, headers)
            if match is None:
                continue
            if signal.location == "body":
                excerpt = _excerpt(body, match.start(), match.end())
            else:
                value = headers.get(str(signal.header).lower(), "")
                excerpt = f"{signal.header}: {value[:FINGERPRINT_HEADER_CHARS]}"
            evidence.append(
                Evidence(
                    kind="url",
                    label=f"{signal.label} — {page}",
                    href=final_url,
                    quote=fingerprint_quote(
                        final_url, result.status_code, observed_on, signal, excerpt
                    ),
                )
            )

        tested = len(signals.signals)
        if capped:
            detail = f"only the first {FINGERPRINT_MAX_BYTES} bytes of the page were tested"
            detail_code = "source.fingerprint_capped"
            detail_params: dict = {"max_bytes": FINGERPRINT_MAX_BYTES}
        else:
            detail = f"{len(evidence)} of {tested} signals present"
            detail_code = "source.fingerprint_matched"
            detail_params = {"matched": len(evidence), "tested": tested}
        return SourceOutcome(
            findings_count=len(evidence),
            # A page with none of the signals still names what was read.
            evidence=tuple(evidence)
            or (Evidence(kind="url", label=page, href=final_url),),
            partial=capped,
            detail=detail,
            detail_code=detail_code,
            detail_params=detail_params,
        )

    return _query


# ── Passive DNS (VirusTotal) ────────────────────────────────────────────────

#: Resolutions read per passive-DNS step: one VirusTotal page at the API's
#: maximum. One request per step, because a free key allows four a minute and
#: a run queries its sources back to back. A subject with more resolutions is
#: `degraded` and says so — the newest ones were read, not all of them.
PASSIVE_DNS_PAGE_SIZE = 40

#: Pages read when host names are filtered: 1,000 resolutions, the budget the
#: OpenCTI VirusTotal connector gives the same pivot (`ip_resolutions_max_pages`
#: 25). On a shared-hosting IP the newest resolutions are other people's sites
#: and five pages reached back two months, short of a network registered the
#: year before. Each page is one VirusTotal request; a page that fails after
#: the first (a free key's rate limit) keeps what was read and the step is
#: `degraded`.
#:
#: The 1,000 resolutions are a premium key's ceiling. A free key allows four
#: requests a minute and 500 a day: its read ends on a 429 after about four
#: pages (160 resolutions), `degraded`, and one full read would spend 5 % of
#: its day. A 429 naming a short `Retry-After` (`SEED_RETRY_AFTER_MAX`) is
#: waited out once per page; a minute's quota is not.
PASSIVE_DNS_FILTERED_MAX_PAGES = PASSIVE_DNS_MAX_PAGES

#: Wall-clock budget of one passive-DNS read, every page together.
#: Each request has its own timeout, which bounds an operation and not a
#: response or a run of pages; this bounds the step. Longer than
#: `SOURCE_CALL_TIMEOUT` because a filtered read is up to
#: `PASSIVE_DNS_FILTERED_MAX_PAGES` requests. A read the deadline cuts after
#: its first page keeps what was read and is `degraded`, like one a failed
#: page cuts.
PASSIVE_DNS_STEP_TIMEOUT = 60

#: Resolutions a passive-DNS step lists as evidence, the most recent first.
#: Each is a citation persisted on the step and in the run's report
#: sources and rendered one per line: a busy shared-hosting IP would make it
#: hundreds. The count of matches is still the step's finding count, and a
#: step that lists fewer than it kept is `degraded` and says how many it shows.
PASSIVE_DNS_MAX_EVIDENCE = 100

#: Seeds of a list resolved at once: one request each, bounded so a long list
#: does not burst a VirusTotal quota.
SEED_CONCURRENCY = 6
#: Longest `Retry-After` a list's seeds wait out once, on a 429; a longer one
#: (or a second 429) stops the read: the key's quota is spent for now.
SEED_RETRY_AFTER_MAX = 5


def _public_ip(value: str) -> str | None:
    """*value* as an IP, if it is a globally routable one. A parked or
    sinkholed domain resolves to `0.0.0.0`, `127.0.0.1` or a private range:
    pivoting on that maps nobody's infrastructure and spends a pivot slot."""
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return None
    return str(address) if address.is_global else None


def _is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


def _retry_after(response: httpx.Response) -> float | None:
    """A 429's `Retry-After` in seconds, when it is given as a number."""
    try:
        return max(0.0, float(response.headers.get("retry-after", "")))
    except ValueError:
        return None

#: ``(integration_id, user_id) -> api key or None``.
ApiKeyLoader = Callable[[str | None, Any], Awaitable[str | None]]


async def load_virustotal_route(integration_id: str | None):
    """The egress route of the VirusTotal integration a run reads (``None``:
    direct). An admin may route an integration through a relay group; its
    requests then leave from that group, never from the platform. Read on a
    short session of its own, like the key; a lookup that fails fails the step
    rather than falling back to a direct connection.
    """
    from app.core.database import async_session
    from app.services.relays import integration_egress_route_by_id

    async with async_session() as session:
        return await integration_egress_route_by_id(session, integration_id)


async def load_virustotal_api_key(integration_id: str | None, user_id) -> str | None:
    """The VirusTotal key a run may use, or None.

    Re-checked at QUERY time against the caller's visibility, for the reason
    the knowledge-base querier gives: the pack was built from what the caller
    could see when they chose it, and a stored ref outlives that. The same
    three-tier rule the picker used (`user_visible_filter`, no admin bypass),
    on a connected VirusTotal integration holding a key. No caller, no key: a
    person's quota is never spent on behalf of nobody.

    On its own short session, so a failed read fails this step and never the
    transaction the run records its next step on.
    """
    from sqlalchemy import select

    from app.api.v1.helpers import get_user_group_ids, user_visible_filter
    from app.core.database import async_session
    from app.models.integration import Integration
    from app.services.vault import SecretUnavailableError, reveal_field

    integration_uuid = safe_uuid(str(integration_id or ""))
    uid = safe_uuid(str(user_id)) if user_id else None
    if integration_uuid is None or uid is None:
        return None
    async with async_session() as session:
        group_ids = await get_user_group_ids(session, uid)
        integration = (
            await session.execute(
                select(Integration).where(
                    Integration.id == integration_uuid,
                    Integration.provider == VIRUSTOTAL_PROVIDER,
                    Integration.status == "connected",
                    user_visible_filter(Integration, uid, group_ids),
                )
            )
        ).scalar_one_or_none()
    if integration is None or not integration.api_key_enc:
        return None
    try:
        # A vault reference is resolved; the vault's failure is recorded on the integration.
        api_key = await reveal_field(integration, "api_key_enc")
    except SecretUnavailableError:
        return None
    except Exception:
        logger.warning("could not decrypt the key of VirusTotal integration %s", integration_uuid)
        return None
    return (api_key or "").strip() or None


def _resolution_date(value: Any) -> str | None:
    try:
        return datetime.fromtimestamp(int(value), UTC).date().isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _next_cursor(payload: dict) -> str | None:
    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    cursor = meta.get("cursor")
    if cursor:
        return str(cursor)
    links = payload.get("links") if isinstance(payload.get("links"), dict) else {}
    nxt = links.get("next")
    if isinstance(nxt, str) and nxt:
        from urllib.parse import parse_qs, urlsplit

        found = parse_qs(urlsplit(nxt).query).get("cursor")
        return found[0] if found else None
    return None


def passive_dns_quote(host: str, ip: str, last_seen: str | None, resolver: str | None) -> str:
    """The citable record of one resolution, every value copied from it."""
    site = site_of(host)
    readable = display_host(site)
    named = f"{readable} ({site})" if readable != site else site
    if site != host:
        named = f"{host}, a host name of the site {named},"
    sentence = f"VirusTotal passive DNS records that {named} resolved to {ip}"
    if last_seen:
        sentence += f", last observed on {last_seen}"
    if resolver:
        sentence += f", as seen by the resolver {resolver}"
    return _passage(sentence + ".")


def make_passive_dns_querier(
    *,
    user_id=None,
    timeout: int = DEFAULT_SOURCE_TIMEOUT,
    deadline: float = PASSIVE_DNS_STEP_TIMEOUT,
    key_loader: ApiKeyLoader | None = None,
    client_factory: Callable[[], httpx.AsyncClient] | None = None,
) -> SourceQuerier:
    """Read what a subject resolved to, from VirusTotal's passive DNS.

    For a domain the findings are the IPs it resolved to; for an IP, the host
    names that resolved to it — one finding and one citation per distinct
    resolution, linked to the VirusTotal page of the other end, which is the
    next subject an analyst investigates.

    With ``hostname_keywords`` only the host names that match count. The rest
    are not hidden: the step says how many of how many were kept, and the
    pattern is in its query.

    ``key_loader`` and ``client_factory`` are injected for the reason the
    orchestrator's querier is: the error mapping is the part most likely to be
    wrong, and it is testable without a network or a database. ``timeout``
    bounds each request, ``deadline`` the read of a subject's pages.
    """
    load_key = key_loader or load_virustotal_api_key

    async def _client(integration_id: str | None) -> httpx.AsyncClient:
        if client_factory is not None:
            return client_factory()
        from app.core.egress import egress_client

        # Pinned to the integration's route: its requests follow the relay
        # group an admin set on it, whatever context the run's task carries.
        return egress_client(timeout=timeout, route=await load_virustotal_route(integration_id))

    async def _seeds_query(source: SourceDefinition, api_key: str) -> SourceOutcome:
        """A list subject: the newest public IP each seed resolved to, one page
        each, then the hops: the IPs at least `SHARED_IP_MIN_SEEDS` seeds share,
        or every seed's IP (`pivot_scope`), most-shared first. A seed resolving
        only to non-public addresses is set aside and counted. The first 429
        stops the read (one short `Retry-After` is waited out): the seeds not
        read yet are counted, not sent."""
        from app.integrations.providers.virustotal import VT_API, build_vt_headers
        from app.integrations.providers.virustotal._common import gui_link

        gate = asyncio.Semaphore(SEED_CONCURRENCY)
        quota_spent = asyncio.Event()

        async def _newest(client: httpx.AsyncClient, seed: str) -> tuple[str, Any]:
            async with gate:
                for attempt in (1, 2):
                    if quota_spent.is_set():
                        return seed, "not_read"
                    try:
                        resp = await client.get(
                            f"{VT_API}/domains/{url_quote(ascii_host(seed).lower(), safe='')}/resolutions",
                            headers=build_vt_headers(api_key),
                            params={"limit": PASSIVE_DNS_PAGE_SIZE},
                            timeout=timeout,
                        )
                    except (httpx.HTTPError, TimeoutError):
                        return seed, "failed"
                    if resp.status_code != 429:
                        break
                    wait = _retry_after(resp)
                    if attempt == 2 or wait is None or wait > SEED_RETRY_AFTER_MAX:
                        quota_spent.set()
                        return seed, "rate_limited"
                    await asyncio.sleep(wait)
            if resp.status_code == 404:
                return seed, None  # VirusTotal holds no resolution for it
            if resp.status_code >= 400:
                return seed, "failed"
            try:
                payload = resp.json()
            except ValueError:
                return seed, "failed"
            only_private = False
            for record in (payload.get("data") if isinstance(payload, dict) else None) or []:
                attributes = record.get("attributes") if isinstance(record, dict) else None
                if not isinstance(attributes, dict):
                    continue
                raw = _MARKUP.sub("", str(attributes.get("ip_address") or "")).strip()[:64]
                ip = _public_ip(raw)
                if ip:
                    resolver = _MARKUP.sub("", str(attributes.get("resolver") or "")).strip()[:80]
                    return seed, (ip, _resolution_date(attributes.get("date")), resolver or None)
                # An address, just not a public one (a value that is no
                # address at all is no resolution).
                only_private = only_private or _is_ip(raw)
            return seed, ("set_aside" if only_private else None)

        async with await _client(source.integration_id) as client:
            answers = await asyncio.gather(*(_newest(client, seed) for seed in source.seed_values))
        failed = [seed for seed, answer in answers if answer == "failed"]
        # Refused by the key's rate limit, and not sent once it was hit.
        limited = [seed for seed, answer in answers if answer in ("rate_limited", "not_read")]
        set_aside = [seed for seed, answer in answers if answer == "set_aside"]
        resolved = [(seed, answer) for seed, answer in answers if isinstance(answer, tuple)]
        if not resolved and limited and not failed:
            return SourceOutcome(
                error_type=_status_error_type(429),
                detail=f"VirusTotal's rate limit (HTTP 429): {len(limited)} of {len(source.seed_values)} seeds not read",
                detail_code="source.http_status",
                detail_params={"status": 429},
            )
        if not resolved and (failed or limited):
            return SourceOutcome(
                error_type="unreachable",
                detail=f"VirusTotal answered for none of the {len(failed) + len(limited)} seeds",
            )
        by_ip: dict[str, list[str]] = {}
        for seed, (ip, _seen, _resolver) in resolved:
            by_ip.setdefault(ip, []).append(seed)
        # Most seeds first, then the order the seeds were given in.
        ranked = [ip for ip, _seeds in sorted(by_ip.items(), key=lambda kv: -len(kv[1]))]
        shared = [ip for ip in ranked if len(by_ip[ip]) >= SHARED_IP_MIN_SEEDS]
        pivots = (ranked if source.pivot_scope == "all" else shared)[: pivot_cap(source.pivot_scope)]
        # No step to pivot into (a built pack's pivot step switched off): the
        # seeds are mapped, no IP is hopped to. Same rule as `_goal_hops`.
        if not (source.hop_action or source.action == ACTION_MAP_INFRASTRUCTURE):
            pivots = []
        evidence = [
            Evidence(
                kind="url",
                label=f"{display_host(seed)}{SEED_IP_SEPARATOR}{ip}",
                href=gui_link("ip_addresses", ip),
                quote=passive_dns_quote(ascii_host(seed).lower(), ip, last_seen, resolver),
            )
            for seed, (ip, last_seen, resolver) in resolved
        ]
        detail = (
            f"{len(resolved)} of {len(source.seed_values)} seeds resolved to {len(by_ip)} IP(s), "
            f"{len(shared)} shared by {SHARED_IP_MIN_SEEDS} seeds or more; pivoting on {len(pivots)}"
        )
        if failed:
            detail += f"; VirusTotal did not answer for {len(failed)}"
        if limited:
            detail += f"; {len(limited)} not read: VirusTotal's rate limit (HTTP 429)"
        if set_aside:
            detail += f"; {len(set_aside)} resolved only to a non-public address, set aside"
        params: dict[str, int] = {
            "resolved": len(resolved),
            "seeds": len(source.seed_values),
            "ips": len(by_ip),
            "shared": len(shared),
            "pivoted": len(pivots),
        }
        # Said only when they happened, each by its own clause in the block
        # (`DETAIL_CODE_OPTIONAL_PARAMS`): the cause of a gap is shown, not
        # only its size.
        for name, seeds in (("set_aside", set_aside), ("not_read", limited), ("failed", failed)):
            if seeds:
                params[name] = len(seeds)
        return SourceOutcome(
            findings_count=len(resolved),
            evidence=tuple(evidence),
            # A seed VirusTotal did not answer for, or was not asked about,
            # is a gap in the map.
            partial=bool(failed or limited),
            detail=detail,
            detail_code="source.passive_dns_seeds",
            detail_params=params,
            leads=seed_ip_leads(source, pivots),
        )

    async def _query(
        source: SourceDefinition, subject_value: str, _asked: str
    ) -> SourceOutcome:
        from app.integrations.providers.virustotal import VT_API, build_vt_headers
        from app.integrations.providers.virustotal._common import gui_link

        api_key = await load_key(source.integration_id, user_id)
        if api_key is None:
            return SourceOutcome(
                error_type="not_found",
                detail="no connected VirusTotal integration is available to you",
                detail_code="source.vt_unavailable",
                detail_params={},
            )

        if source.seed_values:
            return await _seeds_query(source, api_key)
        if "domain_list" in source.subject_types and source.subject is None:
            # A list subject's resolutions are its seeds': without them there
            # is nothing to ask, and the summary is not a host name.
            return SourceOutcome(
                error_type="invalid_response",
                detail="a list subject reached passive DNS without its domains",
            )

        value = source.query_value(subject_value).strip().lower()
        subject_is_ip = classify_subject(value) == "ip"
        collection = "ip_addresses" if subject_is_ip else "domains"
        max_pages = source.max_pages or (PASSIVE_DNS_FILTERED_MAX_PAGES if source.hostname_keywords else 1)

        records: list = []
        cursor: str | None = None
        # Whether VirusTotal said more exists after the last page read, even
        # through a `next` link this code cannot follow.
        more_listed = False
        pages = 0
        # A page that fails after the first one keeps what was already read:
        # the run reports `degraded` instead of throwing away good pages.
        cut_short = False

        async def _page(client: httpx.AsyncClient, params: dict) -> tuple[dict | None, SourceOutcome | None]:
            """One page: ``(payload, None)`` or ``(None, failure)``. A 429
            naming a short `Retry-After` is waited out once."""
            for attempt in (1, 2):
                try:
                    resp = await client.get(
                        f"{VT_API}/{collection}/{url_quote(value, safe='')}/resolutions",
                        headers=build_vt_headers(api_key),
                        params=params,
                        timeout=timeout,
                    )
                except (httpx.TimeoutException, TimeoutError):
                    return None, SourceOutcome(
                        error_type="timeout",
                        detail=f"no response in {timeout}s",
                        detail_code="source.timed_out",
                        detail_params={"seconds": timeout},
                    )
                except httpx.HTTPError as exc:
                    return None, SourceOutcome(
                        error_type="unreachable",
                        detail=_third_party_detail(format_exception_message(exc)),
                    )
                wait = _retry_after(resp) if resp.status_code == 429 else None
                if attempt == 1 and wait is not None and wait <= SEED_RETRY_AFTER_MAX:
                    await asyncio.sleep(wait)
                    continue
                break
            if resp.status_code >= 400:
                return None, SourceOutcome(
                    error_type=_status_error_type(resp.status_code),
                    detail=f"HTTP {resp.status_code}",
                    detail_code="source.http_status",
                    detail_params={"status": resp.status_code},
                )
            try:
                payload = resp.json()
            except ValueError:
                payload = None
            if not isinstance(payload, dict):
                return None, SourceOutcome(
                    error_type="invalid_response",
                    detail="VirusTotal answered with something that is not a JSON document",
                )
            return payload, None

        async with await _client(source.integration_id) as client:
            try:
                # Every page together, in this task: the deadline cancels the
                # request in flight and keeps the pages already read.
                async with asyncio.timeout(deadline):
                    while pages < max_pages:
                        params: dict = {"limit": PASSIVE_DNS_PAGE_SIZE}
                        if cursor:
                            params["cursor"] = cursor
                        payload, failure = await _page(client, params)
                        if failure is not None:
                            if pages == 0:
                                return failure
                            cut_short = True
                            break
                        page_records = payload.get("data")
                        records += page_records if isinstance(page_records, list) else []
                        pages += 1
                        cursor = _next_cursor(payload)
                        links = payload.get("links") if isinstance(payload.get("links"), dict) else {}
                        more_listed = bool(cursor or links.get("next"))
                        if not cursor:
                            break
            except TimeoutError:
                if pages == 0:
                    return SourceOutcome(
                        error_type="timeout",
                        detail=f"VirusTotal was not read within {deadline}s",
                        detail_code="source.timed_out",
                        detail_params={"seconds": deadline},
                    )
                cut_short = True
        has_more = more_listed or cut_short

        resolutions: list[tuple[str, str, str | None, str | None]] = []
        seen: set[tuple[str, str]] = set()
        for record in records:
            attributes = record.get("attributes") if isinstance(record, dict) else None
            if not isinstance(attributes, dict):
                continue
            host = _MARKUP.sub("", str(attributes.get("host_name") or "")).strip().lower()[:253]
            ip = _MARKUP.sub("", str(attributes.get("ip_address") or "")).strip()[:64]
            # One site, one finding: its panel's host names are the same site
            # seen through the hosting, and the newest record (VirusTotal lists
            # newest first) is the one kept.
            if not host or not ip or (site_of(host), ip) in seen:
                continue
            seen.add((site_of(host), ip))
            resolver = _MARKUP.sub("", str(attributes.get("resolver") or "")).strip()[:80]
            resolutions.append((host, ip, _resolution_date(attributes.get("date")), resolver or None))

        keywords = (
            HOSTNAME_KEYWORD_SETS.get(source.hostname_keywords)
            if source.hostname_keywords
            else None
        )
        kept = [r for r in resolutions if keywords is None or keywords.matches(r[0])]
        # The most recent ones (VirusTotal lists newest first) are cited; the
        # count of matches stays the finding count.
        listed = kept[:PASSIVE_DNS_MAX_EVIDENCE]
        capped = len(listed) < len(kept)

        evidence = tuple(
            Evidence(
                kind="url",
                # The other end of the resolution: the next subject.
                label=display_host(site_of(host)) if subject_is_ip else ip,
                href=gui_link("domains", site_of(host)) if subject_is_ip else gui_link("ip_addresses", ip),
                quote=passive_dns_quote(host, ip, last_seen, resolver),
            )
            for host, ip, last_seen, resolver in listed
        )
        if keywords is not None:
            # Both facts when both hold: a filtered read that stopped before
            # VirusTotal's last page must not read as the whole shared IP.
            detail = f"{len(kept)} of {len(resolutions)} resolved sites match the keywords"
            detail_code = "source.passive_dns_filtered"
            if has_more:
                detail += "; VirusTotal holds more that were not read"
                detail_code = "source.passive_dns_filtered_more"
            detail_params: dict | None = {"kept": len(kept), "fetched": len(resolutions)}
            if capped:
                # Its own clause (`DETAIL_CODE_OPTIONAL_PARAMS`): a list cut
                # here never reads as every match.
                detail += f"; only the {len(listed)} most recent are shown"
                detail_params["shown"] = len(listed)
        elif has_more or capped:
            # Up to: malformed and repeated records are dropped from a page.
            # Either VirusTotal listed more than was read, or more was read
            # than is shown: in both cases it holds more than the list.
            limit = min(pages * PASSIVE_DNS_PAGE_SIZE, PASSIVE_DNS_MAX_EVIDENCE)
            detail = f"showing up to the {limit} most recent resolutions; VirusTotal holds more"
            detail_code = "source.passive_dns_more"
            detail_params = {"limit": limit}
        else:
            detail, detail_code, detail_params = None, None, None
        return SourceOutcome(
            findings_count=len(kept),
            evidence=evidence,
            partial=has_more or capped,
            detail=detail,
            detail_code=detail_code,
            detail_params=detail_params,
            # Hops are taken from what is cited, newest first.
            leads=_goal_hops(source, subject_is_ip, listed),
        )

    return _query


def _goal_hops(
    source: SourceDefinition,
    subject_is_ip: bool,
    kept: Sequence[tuple[str, str, str | None, str | None]],
) -> tuple[SourceDefinition, ...]:
    """The next hop of a goal plan, from what this resolution read found.

    Only for a source serving a goal-plan action, and newest first, because
    VirusTotal lists newest first and the network being mapped is the one in
    use now: the IP a domain last resolved to, then the sites that IP served
    most recently.
    """
    if not kept:
        return ()
    # A built pack names the action its hops serve (`hop_action`); the
    # built-in plan hops by its own action names.
    if not subject_is_ip and (source.hop_action or source.action == ACTION_MAP_INFRASTRUCTURE):
        return pivot_leads(source, kept[0][1])
    if subject_is_ip and (source.hop_action or source.action == ACTION_PIVOT_NEIGHBOURS):
        sites = list(dict.fromkeys(site_of(host) for host, _ip, _seen, _resolver in kept))
        return neighbour_leads(
            sites, opencti=source.hop_opencti, fingerprint=source.hop_fingerprint, action=source.hop_action
        )
    return ()


# ── OpenCTI ─────────────────────────────────────────────────────────────────
#
# What the platform already knows, read as the caller: the registered
# OpenCTI's company-managed integration holds no secret, a per-user token is
# minted for each step, so a run reads exactly what its user may read there.


@dataclass(frozen=True)
class OpenCTIAccess:
    """Where to ask the caller's OpenCTI, and where a person opens what it holds."""

    base_url: str
    public_url: str
    headers: dict[str, str]


OpenCTIAccessLoader = Callable[[Any], Awaitable["OpenCTIAccess | None"]]
OpenCTIRunner = Callable[["OpenCTIAccess", str, dict], Awaitable[tuple[dict, "str | None"]]]


async def load_opencti_access(user_id) -> OpenCTIAccess | None:
    """The caller's OpenCTI access, or None (no caller, none registered, none visible).

    Re-checked at QUERY time through the query the pack was built with
    (`only_registered_opencti`: the registered, ``platform_managed``
    integration only), and on its own short session so a failed read fails
    this step only. `_get_token` mints the caller's own JWT for it: that row
    stores no token, so nothing anyone else holds is ever lent.
    """
    from app.api.v1.helpers import get_user_group_ids, user_visible_query
    from app.core.database import async_session
    from app.models.integration import Integration
    from app.services.integration_tools import _get_token, get_integration_public_url
    from app.services.investigation_pack_resolver import only_registered_opencti

    uid = safe_uuid(str(user_id)) if user_id else None
    if uid is None:
        return None
    async with async_session() as session:
        group_ids = await get_user_group_ids(session, uid)
        query = only_registered_opencti(user_visible_query(Integration, uid, group_ids))
        integ = (await session.execute(query)).scalars().first()
        if integ is None:
            return None
        try:
            token = await _get_token(session, str(integ.id), OPENCTI_PROVIDER, user_id=str(uid), integ=integ)
        except Exception as exc:
            logger.warning("OpenCTI token for the investigation failed: %s", format_exception_message(exc))
            return None
        public = get_integration_public_url(integ)
    if not token or "||" not in token:
        return None
    base_url, _, api_token = token.partition("||")
    base_url = base_url.rstrip("/")
    if not base_url or not api_token:
        return None
    return OpenCTIAccess(
        base_url=base_url,
        public_url=(public or base_url).rstrip("/"),
        headers={"Authorization": f"Bearer {api_token}", "Content-Type": "application/json"},
    )


async def run_opencti_query(access: OpenCTIAccess, query: str, variables: dict) -> tuple[dict, str | None]:
    """One GraphQL call through the provider's client (token re-mints included)."""
    from app.integrations.providers.opencti._common import _client, _graphql

    return await _graphql(_client(access.headers), access.base_url, access.headers, query, variables)


#: Observables by value, with the containers that hold them and the campaigns /
#: intrusion sets they are tied to: the three things "is it known?" means.
OPENCTI_OBSERVABLES_QUERY = """
query InvestigationObservables($values: [Any!]!, $first: Int!) {
  stixCyberObservables(first: $first, filters: {mode: and, filters: [{key: "value", values: $values}], filterGroups: []}) {
    edges { node {
      id entity_type observable_value x_opencti_score
      objectLabel { value }
      containers(first: 5) { edges { node {
        id entity_type
        ... on Report { name published objects(first: 3, types: ["Intrusion-Set"]) { edges { node { ... on IntrusionSet { id name } } } } }
        ... on Grouping { name }
        ... on CaseIncident { name }
      } } }
    } }
  }
}
"""

#: The campaigns and intrusion sets an observable is tied to.
OPENCTI_THREATS_QUERY = """
query InvestigationThreats($ids: [String!]!) {
  stixCoreRelationships(first: 100, fromId: $ids, toTypes: ["Campaign", "Intrusion-Set", "Threat-Actor-Group"]) {
    edges { node {
      relationship_type
      from { ... on StixCyberObservable { id } }
      to { ... on Campaign { id name entity_type } ... on IntrusionSet { id name entity_type } ... on ThreatActorGroup { id name entity_type } }
    } }
  }
}
"""

#: A name (a campaign, an actor): the threats OpenCTI holds under it.
OPENCTI_THREAT_BY_NAME_QUERY = """
query InvestigationThreatByName($search: String!) {
  stixDomainObjects(first: 5, search: $search, types: ["Intrusion-Set", "Campaign", "Threat-Actor-Group"]) {
    edges { node {
      id entity_type
      ... on IntrusionSet { name description }
      ... on Campaign { name description }
      ... on ThreatActorGroup { name description }
      reports(first: 3) { edges { node { id name published } } }
    } }
  }
}
"""

_OBSERVABLE_SUBJECTS = frozenset({"domain", "url", "ip"})


def _opencti_link(access: OpenCTIAccess, entity_id: str) -> str:
    return f"{access.public_url}/dashboard/id/{entity_id}"


def opencti_observable_quote(node: dict, reports: Sequence[dict], threats: Sequence[dict]) -> str:
    """The citable record of one observable OpenCTI holds, every value copied from it."""
    value = str(node.get("observable_value") or "")
    sentence = f"OpenCTI already holds the {node.get('entity_type') or 'observable'} {value}"
    facts = []
    if isinstance(node.get("x_opencti_score"), int):
        facts.append(f"score {node['x_opencti_score']}")
    labels = [str(lab.get("value")) for lab in node.get("objectLabel") or [] if isinstance(lab, dict) and lab.get("value")]
    if labels:
        facts.append("labels " + ", ".join(labels))
    if facts:
        sentence += " (" + "; ".join(facts) + ")"
    if reports:
        named = []
        for report in reports:
            item = f'"{report["name"]}"'
            if report.get("published"):
                item += f" (published {str(report['published'])[:10]})"
            named.append(item)
        sentence += ", in " + ", ".join(named)
    if threats:
        sentence += ", tied to " + ", ".join(f'the {t["type"]} "{t["name"]}"' for t in threats)
    return _passage(sentence + ".")


def make_opencti_querier(
    *,
    user_id=None,
    access_loader: OpenCTIAccessLoader | None = None,
    runner: OpenCTIRunner | None = None,
) -> SourceQuerier:
    """What the caller's OpenCTI already knows about a subject.

    For an indicator (a domain, a URL's host, an IP) or a list of them (a
    pivot's co-hosted sites): one finding per value OpenCTI holds, cited with
    the containers holding it and the campaigns or intrusion sets it is tied
    to, linked to the entity in OpenCTI; the values it does not hold are
    listed too, because "new to the platform" is the finding an analyst acts
    on. For a name: the threats OpenCTI holds under it. Never a verdict.

    ``access_loader`` and ``runner`` are injected so the mapping is testable
    without a database or an OpenCTI.
    """
    load_access = access_loader or load_opencti_access
    run = runner or run_opencti_query

    async def _query(source: SourceDefinition, subject_value: str, _asked: str) -> SourceOutcome:
        access = await load_access(user_id)
        if access is None:
            return SourceOutcome(
                error_type="not_found",
                detail="no OpenCTI registered with this platform is available to you",
                detail_code="source.opencti_unavailable",
                detail_params={},
            )
        values = [v.strip().lower() for v in source.opencti_lookup_values(subject_value) if v.strip()]
        if not source.opencti_values and classify_subject(values[0] if values else "") not in _OBSERVABLE_SUBJECTS:
            return await _query_by_name(access, subject_value.strip())

        # A name may be held under either of its spellings (an Armenian-script
        # site as typed or as its A-labels): every one is asked, and a site is
        # known when any of them is.
        asked = list(dict.fromkeys(form for value in values for form in host_spellings(value)))
        data, err = await run(access, OPENCTI_OBSERVABLES_QUERY, {"values": asked, "first": max(len(asked) * 2, 10)})
        if err:
            return SourceOutcome(error_type="invalid_response", detail=_third_party_detail(err))
        nodes = [
            edge["node"]
            for edge in ((data.get("stixCyberObservables") or {}).get("edges") or [])
            if isinstance(edge, dict) and isinstance(edge.get("node"), dict)
        ]
        threats_by_observable: dict[str, list[dict]] = {}
        if nodes:
            rel_data, rel_err = await run(access, OPENCTI_THREATS_QUERY, {"ids": [n["id"] for n in nodes]})
            if not rel_err:
                for edge in (rel_data.get("stixCoreRelationships") or {}).get("edges") or []:
                    node = (edge or {}).get("node") or {}
                    origin = ((node.get("from") or {}).get("id")) or ""
                    target = node.get("to") or {}
                    if origin and target.get("name"):
                        kind = "campaign" if target.get("entity_type") == "Campaign" else "intrusion set"
                        if target.get("entity_type") == "Threat-Actor-Group":
                            kind = "threat actor"
                        entry = {"id": target.get("id"), "name": target["name"], "type": kind}
                        if entry not in threats_by_observable.setdefault(origin, []):
                            threats_by_observable[origin].append(entry)

        evidence: list[Evidence] = []
        known: list[str] = []
        known_sites: set[str] = set()
        seen_reports: dict[str, dict] = {}
        for node in nodes:
            value = str(node.get("observable_value") or "").lower()
            if not value or ascii_host(value).lower() in known_sites:
                continue
            known.append(value)
            known_sites.add(ascii_host(value).lower())
            reports = []
            threats = list(threats_by_observable.get(node["id"], []))
            for c_edge in (node.get("containers") or {}).get("edges") or []:
                container = (c_edge or {}).get("node") or {}
                if not container.get("name"):
                    continue
                reports.append(container)
                seen_reports.setdefault(container["id"], container)
                for o_edge in (container.get("objects") or {}).get("edges") or []:
                    target = (o_edge or {}).get("node") or {}
                    entry = {"id": target.get("id"), "name": target.get("name"), "type": "intrusion set"}
                    if entry["name"] and entry not in threats:
                        threats.append(entry)
            evidence.append(
                Evidence(
                    kind="url",
                    label=known_label(value),
                    href=_opencti_link(access, node["id"]),
                    quote=opencti_observable_quote(node, reports, threats),
                )
            )
        for report in seen_reports.values():
            evidence.append(
                Evidence(kind="url", label=f"OpenCTI {report.get('entity_type', 'container')}: {report['name']}", href=_opencti_link(access, report["id"]))
            )
        unknown = [v for v in values if ascii_host(v).lower() not in known_sites]
        if unknown:
            shown = ", ".join(unknown[:12]) + (f" and {len(unknown) - 12} more" if len(unknown) > 12 else "")
            evidence.append(
                Evidence(
                    kind="tool_result",
                    label=f"Not in OpenCTI yet: {shown}",
                    quote=_passage(f"OpenCTI holds nothing for {', '.join(unknown)}."),
                )
            )
        if not known:
            return SourceOutcome(
                evidence=tuple(evidence),
                detail_code="source.opencti_none_known",
                detail_params={"checked": len(values)},
                detail=f"none of the {len(values)} value(s) is in OpenCTI yet",
            )
        return SourceOutcome(
            findings_count=len(known),
            evidence=tuple(evidence),
            detail_code="source.opencti_known",
            detail_params={"known": len(known), "checked": len(values)},
            detail=f"{len(known)} of {len(values)} value(s) already in OpenCTI",
        )

    async def _query_by_name(access: OpenCTIAccess, name: str) -> SourceOutcome:
        data, err = await run(access, OPENCTI_THREAT_BY_NAME_QUERY, {"search": name})
        if err:
            return SourceOutcome(error_type="invalid_response", detail=_third_party_detail(err))
        evidence: list[Evidence] = []
        for edge in (data.get("stixDomainObjects") or {}).get("edges") or []:
            node = (edge or {}).get("node") or {}
            if not node.get("name"):
                continue
            reports = [(r or {}).get("node") or {} for r in (node.get("reports") or {}).get("edges") or []]
            sentence = f'OpenCTI holds the {node.get("entity_type", "entity")} "{node["name"]}"'
            named = [f'"{r["name"]}"' for r in reports if r.get("name")]
            if named:
                sentence += ", described in " + ", ".join(named)
            evidence.append(
                Evidence(kind="url", label=f"{node['name']} - {node.get('entity_type')} in OpenCTI", href=_opencti_link(access, node["id"]), quote=_passage(sentence + "."))
            )
        if not evidence:
            return SourceOutcome(
                detail_code="source.opencti_none_known",
                detail_params={"checked": 1},
                detail="nothing by that name is in OpenCTI yet",
            )
        return SourceOutcome(
            findings_count=len(evidence),
            evidence=tuple(evidence),
            detail_code="source.opencti_known",
            detail_params={"known": len(evidence), "checked": 1},
            detail=f"{len(evidence)} matching threat(s) in OpenCTI",
        )

    return _query


# ── Routing ─────────────────────────────────────────────────────────────────


def make_dispatching_querier(
    db: AsyncSession,
    *,
    http_querier: SourceQuerier | None = None,
    # Untyped on purpose: callers pass a `UUID` or a string, and both the
    # web-search credential lookup and the corpus filter below normalise it
    # themselves.
    user_id=None,
) -> SourceQuerier:
    """One querier that routes by source kind.

    The orchestrator takes a single `SourceQuerier` on purpose — it should not
    know that sources come in kinds — so the routing lives here. A kind with no
    querier returns an `invalid_response` step rather than raising: a pack
    naming a kind this build cannot answer is a configuration problem, and it
    should show as one failed source rather than a dead run.
    """
    http = http_querier or make_web_fetch_querier()
    # The caller's identity reaches the corpus reader, the MCP caller and the
    # passive-DNS reader, the kinds a visibility rule decides (see their
    # docstrings); an HTTP source or a site fingerprint is addressed by the
    # pack.
    knowledge_base = make_knowledge_base_querier(db, user_id=user_id)
    mcp = make_mcp_querier(db, user_id=user_id)
    # The search key is one the caller may spend.
    web_search = make_web_search_querier(db, user_id=user_id)
    by_kind: dict[str, SourceQuerier] = {
        "http": http,
        "knowledge_base": knowledge_base,
        "mcp_tool": mcp,
        "web_search": web_search,
        "site_fingerprint": make_site_fingerprint_querier(),
        # A person's VirusTotal key, re-checked against their visibility.
        "passive_dns": make_passive_dns_querier(user_id=user_id),
        # The registered OpenCTI, read as the caller.
        "opencti": make_opencti_querier(user_id=user_id),
    }

    async def _query(
        source: SourceDefinition, subject_value: str, query: str
    ) -> SourceOutcome:
        querier = by_kind.get(source.kind)
        if querier is None:
            return SourceOutcome(
                error_type="invalid_response",
                detail=f"no querier for source kind {source.kind!r}",
            )
        return await querier(source, subject_value, query)

    return _query


# ── Web search ──────────────────────────────────────────────────────────────


#: How much of a search answer is kept. The provider is capped at
#: `MAX_ANSWER_TOKENS`, so this is a guard against a runaway response rather
#: than a routine trim.
MAX_ANSWER_CHARS = 20_000

#: Cited pages promoted to second-hop steps.
#:
#: The provider searched and SUMMARISED; the pages it cited were never read by
#: this platform. Fetching the most-cited ones is what turns a single query
#: into an investigation: the run gets the primary source's own text, and the
#: report can quote it rather than the provider's paraphrase of it.
#:
#: Small on purpose. Every lead spends an iteration that a planned source
#: would otherwise have had, and the pages beyond the first few are the ones
#: the provider leaned on least.
MAX_SEARCH_LEADS = 3


async def resolve_search_credentials(
    db: AsyncSession, *, user_id: object = None
) -> tuple[str, str, str | None] | None:
    """``(provider, api_key, model)`` for a search-capable provider *user_id* may use.

    Read from the platform's own LLM configuration rather than from the
    integrations catalogue: web search here is not a third-party service
    somebody connected, it is the model provider the install already uses,
    and asking an operator to configure the same key twice to get a cited
    answer would be a configuration trap.

    Never filtered on ``LLMProviderConfig.user_id``: that column is an audit
    trail (created by), not ownership, and reading it as ownership once left
    `web_search` dead for everyone but the admin who typed the key in. Who may
    spend a provider's key is its access list: the run's caller gets
    the providers granted to them and the unrestricted ones, a run with no
    caller the unrestricted ones only.

    Preference follows ``SEARCH_PROVIDERS``; within a vendor the caller's
    team provider, then the admin-marked default. Returns None when nothing
    capable is usable, which the querier reports as a source that cannot
    answer — not as a source that found nothing.
    """
    from app.llm.provider_access import usable_provider_configs
    from app.services.vault import SecretUnavailableError, reveal_field
    from app.services.web_search import SEARCH_PROVIDERS

    configs = await usable_provider_configs(db, user_id, SEARCH_PROVIDERS)
    unavailable: SecretUnavailableError | None = None
    for config in sorted(configs, key=lambda c: SEARCH_PROVIDERS.index(c.provider)):
        try:
            api_key = await reveal_field(config, "api_key_enc")
        except SecretUnavailableError as exc:
            # Its vault did not answer: the next capable provider may.
            unavailable = unavailable or exc
            continue
        if (api_key or "").strip():
            # The provider's configured model, when it has one: a key may be
            # limited to it, and the built-in model id is only the fallback.
            return config.provider, api_key, (config.default_model or None)
    if unavailable is not None:
        raise unavailable
    return None


def make_web_search_querier(db: AsyncSession, *, user_id=None) -> SourceQuerier:
    """Ask a provider to search the web and answer, keeping its citations.

    This is the source kind that makes an OPEN question answerable. Every
    other source in this module is a lookup: a URL template filled with a
    subject, a corpus searched for a string, a tool called with an argument.
    None of them can find a page nobody named in advance, which is why an
    unclassifiable subject came back with almost nothing to read.

    The provider both searches and writes. That is deliberate: its answer
    arrives with sources attached to spans of its own prose, and no amount of
    post-processing over a list of links could reconstruct which sentence a
    source supports.
    """
    del db

    async def _query(
        source: SourceDefinition, subject_value: str, query: str
    ) -> SourceOutcome:
        from app.core.database import async_session
        from app.services.web_search import SearchError, fit_answer, search

        # A session of its own, closed before the search: on the run's
        # session the read would leave a transaction open on the provider
        # table for the whole provider call, which every chat turn reads.
        async with async_session() as session:
            credentials = await resolve_search_credentials(session, user_id=user_id)
        if credentials is None:
            return SourceOutcome(
                error_type="auth_error",
                detail=(
                    "no provider with web search is configured or available to "
                    "you; add an Anthropic or OpenAI key in LLM settings"
                ),
            )
        provider, api_key, model = credentials

        try:
            answer = await search(provider, api_key, query, model=model)
        except SearchError as exc:
            return SourceOutcome(error_type=exc.error_type, detail=exc.detail[:MCP_DETAIL_CHARS])

        # Truncated and re-anchored together, never separately: cutting the
        # text while passing the offsets through is what let a span point past
        # the stored answer. `_anchor` already stripped, so the strip here
        # would be a no-op AND a second chance to shift every offset — see
        # `fit_answer`.
        text, citations = fit_answer(answer.text or "", answer.citations, MAX_ANSWER_CHARS)
        evidence = tuple(
            Evidence(
                kind="url",
                label=citation.title or citation.url,
                href=citation.url,
                quote=citation.quote or None,
                start=citation.start,
                end=citation.end,
            )
            for citation in citations
        )

        if not text:
            # The provider ran and wrote nothing. `empty`, exactly like a page
            # that loaded and said nothing.
            return SourceOutcome(findings_count=0, answer=None, evidence=evidence)

        if not evidence:
            # Prose with no source behind it. Kept, because the reader is
            # better served seeing it than not — but `partial`, because an
            # uncited answer is precisely what this feature exists to avoid
            # presenting as a finding.
            return SourceOutcome(
                findings_count=0,
                answer=text,
                partial=True,
                detail="the search answered without citing a source",
            )

        # One finding per distinct source, not per citation: a provider often
        # cites the same page for several sentences, and counting those
        # separately would inflate how much was actually found.
        distinct = len({c.href for c in evidence if c.href})
        return SourceOutcome(
            findings_count=distinct or 1,
            answer=text,
            evidence=evidence,
            leads=_pages_worth_reading(answer),
        )

    return _query


def _lead_name(title: str) -> str:
    """A page title, made safe to print in a report section.

    The title comes from the page, so it is attacker-influenceable, and
    `render_sources_queried` prints it as `- **{name}** — …` with no escaping.
    A title carrying `[7]` plants a citation marker in the one section this
    feature designs to be incapable of lying — and `reconcile_citations` will
    validate it happily if the number is in range. One carrying `**` breaks
    the line's own emphasis.

    Stripped rather than escaped: the name is a label, not prose, and a label
    with backslashes in it reads worse than one with the markup removed.
    """
    return _MARKUP.sub(" ", title or "").strip()[:200]


def _pages_worth_reading(answer) -> tuple[SourceDefinition, ...]:
    """Second-hop steps for the pages a search leaned on most.

    A lead in this loop is another SOURCE for the same subject, not another
    subject — so the useful ones here are the cited pages themselves. The
    provider read them and gave back a sentence each; fetching them gives the
    run their own text, which is what lets the report quote a primary source
    instead of a paraphrase of it.

    Ranked by how many times the answer cited a page, because that is the
    provider's own signal about which ones carried the answer. Ties keep first
    appearance, so the ranking is stable for the same response.
    """
    from collections import Counter

    cited = Counter(c.url for c in answer.citations if c.url)
    if not cited:
        return ()

    first_seen = {}
    titles = {}
    for index, citation in enumerate(answer.citations):
        if citation.url and citation.url not in first_seen:
            first_seen[citation.url] = index
            titles[citation.url] = citation.title or citation.url

    ranked = sorted(cited, key=lambda url: (-cited[url], first_seen[url]))

    leads: list[SourceDefinition] = []
    for url in ranked[:MAX_SEARCH_LEADS]:
        lead = SourceDefinition(
            name=_lead_name(titles[url]) or url[:200],
            subject_types=ONE_SUBJECT_TYPES,
            # Braces doubled: `build_url` runs `str.format` over the template,
            # and a real URL containing `{` or `}` — which tracking parameters
            # do carry — would raise there or, worse, silently swallow the
            # segment as a field name.
            url_template=url.replace("{", "{{").replace("}", "}}"),
        )
        try:
            # Probed, because `SourceDefinition` validates nothing at
            # construction: the `except ValueError` that used to sit around
            # the constructor could never fire, so the documented "skipped
            # rather than failing the step" was a comment describing a guard
            # that did not exist. Building the URL once here makes it true.
            lead.build_url("probe")
        except (ValueError, IndexError, KeyError):
            logger.debug("skipping unusable search lead %r", url)
            continue
        leads.append(lead)
    return tuple(leads)
