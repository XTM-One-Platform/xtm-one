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

"""``extract_stix_from_document``: the grounded STIX extraction pipeline as a built-in tool.

The agent decides to extract and from what (an attached file or text); the tool runs the
pipeline (``app.services.stix_extraction``) on the provider and model of the agent's own
turn, so an evaluation that compares models compares the whole pipeline on each, and
returns ``{"valid", "bundle", "stats", "errors", "warnings"}``. Several attached reports
give one ``report`` each in one bundle. An agent whose
``config.finalize_on_builtin_tool`` names this tool returns the bundle as its answer
without another model call, as ``stix_bundle_validator`` does for the single-pass
harvester.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Sequence
from typing import TYPE_CHECKING

from app.services.builtin_tools._shared import list_available_files, resolve_file_ref
from app.tools.base import (
    CallableToolExecutor,
    FileContext,
    ToolDefinition,
    ToolResult,
    tool_call_timeout,
    tool_result_char_cap,
)

if TYPE_CHECKING:
    from app.services.stix_extraction.pipeline import PlatformNames

logger = logging.getLogger(__name__)

TOOL_NAME = "extract_stix_from_document"
#: The tool's own execution timeout (``ToolDefinition.execution_timeout``).
EXECUTION_TIMEOUT_SECONDS = 1800
#: Kept back from the timeout to answer with the reports extracted so far.
_ANSWER_MARGIN_SECONDS = 60


def _answer(result: dict[str, object]) -> ToolResult:
    """The answer: *result* whole on ``structured`` (the finalize fast path makes a valid
    bundle the run's answer from there, as it stands, when the call is its round's only
    one), the model's view as text: the bundle only when it fits the run's tool-result
    limit, else its size and what it is. A bundle cut at the limit, which the model would
    have to retype, is never shown."""
    text = json.dumps(result, ensure_ascii=False)
    cap = tool_result_char_cap()
    if cap is None or len(text) <= cap:
        return ToolResult(tool_call_id="", content=text, structured=result)
    bundle_chars = len(json.dumps(result.get("bundle"), ensure_ascii=False))
    shown = {key: value for key, value in result.items() if key != "bundle"}
    shown["bundle_chars"] = bundle_chars
    shown["bundle_note"] = (
        f"The bundle ({bundle_chars} characters) is too large to show here. It is the run's answer as "
        "it stands when this was the round's only tool call; extract every report in one call, "
        "never in parallel calls."
    )
    return ToolResult(tool_call_id="", content=json.dumps(shown, ensure_ascii=False), structured=result)


def _failure(*errors: str) -> str:
    return json.dumps({"valid": False, "errors": list(errors)}, ensure_ascii=False)


async def _sources(
    file_ids: object, file_id: object, text: object, document_name: object, file_context: FileContext | None
) -> tuple[list[tuple[str, str]], list[str]] | str:
    """The (name, text) of each report to extract and the attached files skipped, or what
    is missing. With none named, the files :func:`_unnamed_reports` gives (one report per
    file), leaving out one with no text (an image beside the report); a file named
    explicitly must have text. A text without a name is named by
    :func:`~app.services.stix_extraction.bundle.default_report_name`, a file by
    :func:`~app.services.stix_extraction.bundle.file_report_name`."""
    from app.services.stix_extraction.bundle import file_report_name

    name = document_name if isinstance(document_name, str) else ""
    if isinstance(text, str) and text.strip():
        return [(name, text)], []
    wanted = [i for i in (file_ids if isinstance(file_ids, list) else []) if isinstance(i, str) and i]
    if isinstance(file_id, str) and file_id:
        wanted.append(file_id)
    if file_context is None:
        return "Give the report as 'file_ids' (attached files) or as 'text'. " + list_available_files(None)
    if wanted:
        refs = [resolve_file_ref(file_context, i) for i in wanted]
    else:
        refs = _unnamed_reports(file_context)
        if isinstance(refs, str):
            return refs
    if not refs or any(ref is None for ref in refs):
        return "Give the report as 'file_ids' (attached files) or as 'text'. " + list_available_files(file_context)
    sources: list[tuple[str, str]] = []
    skipped: list[str] = []
    for ref in refs:
        content = await file_context.read_text(ref.file_id) or ""
        if not content.strip():
            if wanted:
                return f"No text could be read from '{ref.filename}'."
            skipped.append(ref.filename)
            continue
        sources.append((file_report_name(ref.filename, content), content))
    if not sources:
        return "No text could be read from the attached files: " + ", ".join(f"'{f}'" for f in skipped) + "."
    if len(sources) == 1 and name:
        sources = [(name, sources[0][1])]
    return sources, skipped


def _unnamed_reports(file_context: FileContext) -> list | str:
    """The files a call naming none reads: the turn's own attachments in a chat; with none
    attached this turn, the conversation's only file, or a request to name them (an earlier
    turn's reports were extracted then); every file where the context holds only the run's
    own (an assignment, a delegated task)."""
    files = file_context.list_files()
    turn = file_context.current_turn_ids
    if turn is None:
        return files
    attached = [ref for ref in files if ref.file_id.removeprefix("file-") in turn]
    if attached:
        return attached
    if len(files) <= 1:
        return files
    return ("No report is attached to this message: give the reports to extract as 'file_ids'. "
            + list_available_files(file_context))


def _incomplete(stats: dict) -> list[str]:
    """Errors: a step none of whose calls answered leaves the bundle without its
    entities or its relationships, which must not become a final answer."""
    errors = []
    for step in ("detection", "relation"):
        if stats.get(f"{step}_chunks") and stats.get(f"failed_{step}_chunks") == stats.get(f"{step}_chunks"):
            errors.append(f"every {step} call failed")
    return errors


def _refused(usage: dict) -> list[str]:
    """Errors: a call the run's budget refused, or the model declined, leaves the bundle incomplete."""
    errors = []
    if refused := usage.get("refused_calls") or 0:
        errors.append(f"the run's budget could not pay for {refused} extraction calls: the bundle is incomplete")
    if declined := usage.get("declined_calls") or 0:
        errors.append(f"the model declined {declined} extraction calls: the bundle is incomplete")
    return errors


#: Failed steps whose count the warnings name, beyond detection and relationships.
_FAILED_STEPS = {
    "failed_pair_chunks": "candidate pair",
    "failed_behaviour_chunks": "behaviour",
    "failed_technique_checks": "technique check",
    "failed_technique_link_batches": "technique linking",
    "failed_relevance_batches": "relevance review",
    "failed_verification_batches": "relationship verification",
    "failed_cross_chunk_batches": "cross-passage relationship",
    "failed_detection_samples": "extra detection reading",
    "failed_technique_select": "technique selection",
    "failed_technique_report": "technique report",
    "failed_activity_hub": "activity",
}


def _warnings(stats: dict) -> list[str]:
    """Everything that left the bundle less complete than its options ask: every failed
    step, and ATT&CK when it could not be loaded."""
    warnings = []
    for step in ("detection", "relation"):
        failed = stats.get(f"failed_{step}_chunks") or 0
        if failed and failed != stats.get(f"{step}_chunks"):
            warnings.append(f"{failed} of {stats.get(f'{step}_chunks')} {step} calls failed: the bundle may be incomplete")
    if stats.get("failed_resolution_batches"):
        warnings.append(f"{stats['failed_resolution_batches']} resolution calls failed: those entities kept their detected kind")
    for key, label in _FAILED_STEPS.items():
        if stats.get(key):
            warnings.append(f"{stats[key]} {label} call(s) failed: the bundle may be incomplete")
    if stats.get("technique_catalogue_unavailable") or stats.get("knowledge_unavailable"):
        warnings.append("ATT&CK could not be loaded: techniques are not linked to ATT&CK, and none were read from behaviour")
    if stats.get("platform_names_not_asked"):
        warnings.append(f"{stats['platform_names_not_asked']} names were past the platform lookup's bound and keep the text's spelling")
    if stats.get("platform_names_unavailable"):
        warnings.append("the platform's names could not be read: the objects keep the text's names")
    if stats.get("truncated_at_chars"):
        warnings.append(f"the text was cut at {stats['truncated_at_chars']} characters")
    return warnings


def _merge(bundles: list[dict]) -> dict:
    """One bundle of several reports' bundles: an object two reports share (same
    standard id) appears once, each report keeps its own references."""
    if len(bundles) == 1:
        return bundles[0]
    seen: set[str] = set()
    objects = []
    for bundle in bundles:
        for obj in bundle["objects"]:
            if obj["id"] not in seen:
                seen.add(obj["id"])
                objects.append(obj)
    return {"type": "bundle", "id": bundles[0]["id"], "objects": objects}


def _validation_errors(bundle: dict) -> list[str]:
    try:
        import stix2

        stix2.parse(bundle, allow_custom=True, version="2.1")
    except Exception as exc:  # stix2 raises several types on an invalid object
        return [f"stix2 validation error: {exc}"]
    return []


def shipped_options() -> dict[str, object]:
    """The extraction options the tool runs with (benchmarks run these too).

    What the XTM One convention keeps (dev-docs/stix-grounded-extraction.md §8.9): the
    report's knowledge, not what only frames it (the relevance review, domains and URLs
    included); ATT&CK's techniques, those the report describes without naming them
    included (verified against their definition, FROZEN-K2); products and ransomware
    brands named as analysts and OpenCTI name them.
    """
    return {
        "relevance_review": True,
        "relevance_web_addresses": True,
        "behaviour_techniques": True,
        "technique_mode": "balanced",
        "technique_linking": True,
        "attack_techniques_only": True,
        "product_names": True,
        "ransomware_twins": True,
        "galaxy_aliases": True,
        "sector_names": True,
        "victim_countries": True,
        "graded_confidence": True,
    }


def build_stix_extraction_tool(
    llm: object | None,
    model: str | None,
    *,
    platform_names: Sequence[PlatformNames] | None = None,
    attack_sources: Sequence[tuple[str, str, object]] | None = None,
) -> tuple[list[ToolDefinition], CallableToolExecutor]:
    """The extraction tool, running its model calls on *llm* / *model* (the turn's own).

    *llm* ``None`` builds the definition alone (the tool catalog). *platform_names*
    collects how the run's bound platforms name the objects they hold
    (``integration_tools.PLATFORM_NAMES_CONTEXT_KEY``), filled when the run's
    integrations are built, possibly after this tool: read at each call, and used
    only when it holds exactly one (two platforms would name one object two ways).
    *attack_sources* collects the run's bound MITRE ATT&CK integrations the same way
    (``integration_tools.ATTACK_SOURCES_CONTEXT_KEY``): ATT&CK is read from the one it
    holds, through its relay route; otherwise from the deployment's configured source.
    """

    async def _extract(
        file_ids: list[str] | None = None,
        text: str = "",
        document_name: str = "",
        include_observables: bool = True,
        file_context: FileContext | None = None,
        **kwargs: object,
    ) -> str | ToolResult:
        if llm is None:
            return _failure("No language model is available to run the extraction.")
        found = await _sources(file_ids, kwargs.get("file_id"), text, document_name, file_context)
        if isinstance(found, str):
            return _failure(found)
        sources, skipped = found

        from app.services.stix_extraction import (
            ExtractionOptions,
            Usage,
            extract_stix,
            provider_structured_call,
        )
        from app.services.stix_extraction.knowledge import attack_functions

        usage = Usage()
        call = provider_structured_call(llm, model, usage)
        options = ExtractionOptions(include_observables=include_observables is not False, **shipped_options())
        bundles, errors, stats = [], [], []
        warnings = [f"'{filename}' has no text and was not extracted" for filename in skipped]
        platform = platform_names[0] if platform_names is not None and len(platform_names) == 1 else None
        bound = attack_sources[0] if attack_sources is not None and len(attack_sources) == 1 else None
        attack_knowledge, attack_catalogue = attack_functions(bound)  # type: ignore[arg-type]
        # The call's own deadline, before the tool's timeout: a report not finished in time is
        # given up, and the reports extracted so far are answered (a timeout would lose them all).
        applied = tool_call_timeout()
        budget = min(EXECUTION_TIMEOUT_SECONDS, applied) if applied else EXECUTION_TIMEOUT_SECONDS
        deadline = time.monotonic() + budget - _ANSWER_MARGIN_SECONDS
        for index, (name, source) in enumerate(sources):
            remaining = deadline - time.monotonic()
            try:
                if remaining <= 0:
                    raise TimeoutError
                # A title the caller gives names the report; otherwise the report's own title.
                # Its publisher is read either way.
                result = await asyncio.wait_for(
                    extract_stix(
                        source, call, usage=usage, document_name=name, options=options,
                        knowledge=attack_knowledge, techniques=attack_catalogue,
                        read_title=not (isinstance(document_name, str) and document_name.strip()),
                        read_publisher=True, platform_names=platform,
                    ),
                    remaining,
                )
            except TimeoutError:
                left = [n for n, _ in sources[index:]]
                warnings.append(
                    "The call's time ran out before these reports were extracted: "
                    + ", ".join(f"'{n}'" for n in left) + "; extract them in another call."
                )
                if not bundles:
                    errors.append("No report could be extracted within the call's time.")
                break
            bundles.append(result.bundle)
            stats.append({"document": result.document_name, **{k: v for k, v in result.stats.items() if k != "usage"}})
            errors.extend(f"{result.document_name}: {e}" for e in _incomplete(result.stats))
            warnings.extend(f"{result.document_name}: {w}" for w in _warnings(result.stats))
        bundle = _merge(bundles)
        errors.extend(_validation_errors(bundle))
        errors.extend(_refused(usage.as_dict()))
        spent = usage.as_dict()
        if spent["failed_calls"]:
            warnings.append(f"{spent['failed_calls']} of {spent['calls'] + spent['failed_calls']} model calls failed")
        if spent["off_schema_calls"]:
            warnings.append(f"{spent['off_schema_calls']} model answers were off their format and kept nothing")
        if usage.calls == 0:
            errors.append("The language model answered none of the extraction calls.")
        return _answer({
            "valid": not errors,
            "errors": errors,
            "warnings": warnings,
            "stats": {"documents": stats, "usage": usage.as_dict()},
            "bundle": bundle,
        })

    executor = CallableToolExecutor()
    executor.register(TOOL_NAME, _extract)
    definition = ToolDefinition(
        name=TOOL_NAME,
        description=(
            "Extract a report's cyber threat intelligence (attached file or text) as a "
            "STIX 2.1 bundle of grounded objects and relationships; one call extracts every "
            "report given. Returns {valid, bundle, stats, errors}."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "file_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "The reports to extract; omit for those attached to the message.",
                },
                "text": {"type": "string", "description": "The report's text, when it is not a file."},
                "document_name": {"type": "string", "description": "The report's title."},
                "include_observables": {
                    "type": "boolean",
                    "description": "Also extract observables (default true).",
                },
            },
        },
        source="builtin:cti",
        # One call reads whole reports: tens of model calls per document, four at a
        # time, the documents one after another. The loop's 300 s per-tool timeout
        # would cancel a long report's calls already paid for.
        execution_timeout=EXECUTION_TIMEOUT_SECONDS,
    )
    return [definition], executor


__all__ = ["TOOL_NAME", "build_stix_extraction_tool"]
