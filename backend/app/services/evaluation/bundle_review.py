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

"""The STIX bundle review (#12282): one entry's expected bundle against one or more answers.

The STIX scorer compares canonical identifiers: an object counts as found only when the
answer holds the same canonical object. The review shows that comparison object by object,
and also pairs what the scorer counts as a miss plus a spurious object when both name the
same thing (another name or alias, an ATT&CK id, a defanged value, a type of the same
family), so a reader sees *why* a score is low. The pairing is presentation only: it never
changes a score.

Built on :func:`canonical_stix_bundle`, the scorer's own reading of a bundle, so the review
and the score never disagree on what was found.

Output, for the page (sources are ``"expected"`` and each answer's key):

* ``objects``: every distinct canonical object of any source, described
  (``t`` type, ``n`` name, ``c`` canonical id, ``a`` aliases, ``x`` ATT&CK id, ``h`` hashes);
* ``groups``: the group of each object (one per expected object plus what pairs with it,
  then the rest);
* ``sources``: per source, the indexes of its objects;
* ``relationships``: rows ``[source group, target group, {source: [type, source object,
  target object, duplicates]}]``, one per expected triple, another type on the same pair
  joining it as a wrong type, anything else opening its own row; each source keeps its real
  endpoint objects, so a page compares canonical triples exactly as the scorer does;
* ``unreadable``: the answers the scorer could not read as a bundle.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.evaluation import Evaluation, EvaluationResult, EvaluationRun
from app.models.evaluation_metric import EvaluationMetric
from app.services.evaluation.metric_package import (
    canonical_stix_bundle,
    is_pooled_key,
    quality_catalog,
    reference_key,
)
from app.services.evaluation.metric_package.families import STIX_BUNDLE

EXPECTED = "expected"
#: Object types the review pairs with each other when their names match: an actor modelled
#: as a threat actor or an intrusion set, a piece of software as malware, tool or software.
_TYPE_FAMILIES: tuple[frozenset[str], ...] = (
    frozenset({"threat-actor", "intrusion-set"}),
    frozenset({"malware", "tool", "software"}),
)
#: Past this many distinct objects the near-miss pairing (quadratic) is skipped: exact
#: canonical matches still line up, near-misses stay unpaired.
MAX_PAIRED_OBJECTS = 2500
_NAME_CHARS = 140
_ALIAS_CHARS = 60
_MAX_ALIASES = 8


def _refang(value: str) -> str:
    return value.replace("[.]", ".").replace("(.)", ".").replace("hxxp", "http").replace("[:]", ":")


def _key(value: object) -> str:
    return re.sub(r"[^a-z0-9]", "", _refang(str(value)).lower())


def _tokens(value: object) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", _refang(str(value)).lower()) if len(w) > 2}


def _compatible(a: str, b: str) -> bool:
    return a == b or any(a in fam and b in fam for fam in _TYPE_FAMILIES)


def describe(obj: Mapping[str, Any]) -> dict[str, Any]:
    """What the review shows of one canonical object."""
    name = obj.get("name") or obj.get("value") or obj.get("pattern")
    if not name and obj.get("type") == "location":
        name = obj.get("country") or obj.get("region") or obj.get("city")
    hashes = obj.get("hashes") if isinstance(obj.get("hashes"), Mapping) else None
    if not name and obj.get("type") == "file" and hashes:
        algo = next(iter(hashes))
        name = f"{algo} {str(hashes[algo])[:10]}…"
    ext = obj.get("x_mitre_id")
    for ref in obj.get("external_references") or []:
        if (
            isinstance(ref, Mapping)
            and str(ref.get("source_name", "")).startswith("mitre")
            and ref.get("external_id")
        ):
            ext = ref["external_id"]
    out: dict[str, Any] = {"t": obj["type"], "n": str(name or obj["type"])[:_NAME_CHARS], "c": obj["id"]}
    aliases = obj.get("aliases") or obj.get("x_opencti_aliases")
    if isinstance(aliases, list) and aliases:
        out["a"] = [str(a)[:_ALIAS_CHARS] for a in aliases[:_MAX_ALIASES]]
    if ext:
        out["x"] = str(ext)
    if obj.get("type") == "file" and hashes:
        out["h"] = {str(k): str(v) for k, v in hashes.items()}
    return out


def _close(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    """Whether two objects name the same thing although their canonical ids differ."""
    if not _compatible(a["t"], b["t"]):
        return False
    na, nb = _key(a["n"]), _key(b["n"])
    if na and na == nb:
        return True
    if a.get("x") and a.get("x") == b.get("x"):
        return True
    aliases = {_key(x) for x in a.get("a", [])} | {_key(x) for x in b.get("a", [])}
    if na in aliases or nb in aliases:
        return True
    if a["t"] == b["t"] and min(len(na), len(nb)) >= 5 and (na in nb or nb in na):
        return True
    ta, tb = _tokens(a["n"]), _tokens(b["n"])
    return a["t"] == b["t"] and bool(ta and tb) and len(ta & tb) / len(ta | tb) >= 0.6


def build_review(expected: object, answers: Sequence[tuple[str, object]]) -> dict[str, Any] | None:
    """The review of *answers* (``(key, raw answer)``) against *expected*, or ``None`` when
    the expected bundle cannot be read (or the STIX backend is unavailable)."""
    exp = canonical_stix_bundle(expected, reference=True)
    if exp is None:
        return None
    objects: list[dict[str, Any]] = []
    index: dict[str, int] = {}

    def add(obj: Mapping[str, Any]) -> int:
        cid = str(obj["id"])
        if cid not in index:
            index[cid] = len(objects)
            objects.append(describe(obj))
        return index[cid]

    sources: dict[str, list[int]] = {EXPECTED: [add(o) for o in exp.objects]}
    triples: dict[str, Sequence[tuple[str, str, str]]] = {EXPECTED: exp.triples}
    unreadable: list[str] = []
    for key, raw in answers:
        view = canonical_stix_bundle(raw)
        if view is None:
            unreadable.append(key)
            continue
        sources[key] = sorted({add(o) for o in view.objects})
        triples[key] = view.triples

    groups = _group(objects, set(sources[EXPECTED]))
    rows = _relationship_rows(objects, groups, index, triples)
    return {
        "objects": objects,
        "groups": groups,
        "sources": sources,
        "relationships": rows,
        "unreadable": unreadable,
    }


def _group(objects: list[dict[str, Any]], expected: set[int]) -> list[int]:
    """One group per expected object plus whatever is close to it, then the rest; two
    expected objects never share a group."""
    parent = list(range(len(objects)))
    has_exp = [i in expected for i in range(len(objects))]

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    if len(objects) <= MAX_PAIRED_OBJECTS:
        order = sorted(range(len(objects)), key=lambda i: (not has_exp[i], i))
        for x, i in enumerate(order):
            for j in order[x + 1 :]:
                ri, rj = find(i), find(j)
                if ri == rj or (has_exp[ri] and has_exp[rj]):
                    continue
                if _close(objects[i], objects[j]):
                    if has_exp[rj]:
                        ri, rj = rj, ri
                    parent[rj] = ri
                    has_exp[ri] = has_exp[ri] or has_exp[rj]
    roots = sorted({find(i) for i in range(len(objects))}, key=lambda r: (not has_exp[r], r))
    gid = {r: k for k, r in enumerate(roots)}
    return [gid[find(i)] for i in range(len(objects))]


def _relationship_rows(
    objects: list[dict[str, Any]],
    groups: list[int],
    index: dict[str, int],
    triples: Mapping[str, Sequence[tuple[str, str, str]]],
) -> list[list[Any]]:
    """Relationship rows (see the module doc)."""
    rows: list[list[Any]] = []
    row_of: dict[tuple[int, int, str], int] = {}
    seen: dict[tuple[int, str], set[tuple[str, str, str]]] = {}

    def exact(entry: list[Any] | None, expected: list[Any] | None) -> bool:
        return (
            entry is not None
            and expected is not None
            and expected[0] == entry[0]
            and objects[expected[1]]["c"] == objects[entry[1]]["c"]
            and objects[expected[2]]["c"] == objects[entry[2]]["c"]
        )

    def put(row: int, key: str, kind: str, source: str, target: str) -> None:
        cells = rows[row][2]
        if (source, target, kind) in (done := seen.setdefault((row, key), set())):
            return
        done.add((source, target, kind))
        new = [kind, index[source], index[target], 0]
        current = cells.get(key)
        if current is None:
            cells[key] = new
        elif exact(new, cells.get(EXPECTED)) and not exact(current, cells.get(EXPECTED)):
            cells[key] = [*new[:3], current[3] + 1]
        else:
            current[3] += 1

    def ends(source: str, target: str) -> tuple[int, int] | None:
        if source not in index or target not in index:
            return None
        return groups[index[source]], groups[index[target]]

    for source, kind, target in triples.get(EXPECTED, ()):
        pair = ends(source, target)
        if pair is None:
            continue
        k = (*pair, kind)
        if k not in row_of:
            row_of[k] = len(rows)
            rows.append([pair[0], pair[1], {}])
            put(row_of[k], EXPECTED, kind, source, target)
    for key, items in triples.items():
        if key == EXPECTED:
            continue
        for source, kind, target in items:
            pair = ends(source, target)
            if pair is None:
                continue
            if (*pair, kind) in row_of:
                put(row_of[(*pair, kind)], key, kind, source, target)
                continue
            free = next(
                (
                    i
                    for (a, b, _), i in row_of.items()
                    if (a, b) == pair and EXPECTED in rows[i][2] and key not in rows[i][2]
                ),
                None,
            )
            if free is not None:
                put(free, key, kind, source, target)
                continue
            row_of[(*pair, kind)] = len(rows)
            rows.append([pair[0], pair[1], {}])
            put(row_of[(*pair, kind)], key, kind, source, target)
    return rows


#: The most answers one review lays side by side.
MAX_REVIEWED_ANSWERS = 4
#: The report text sent for the annotated view, past which it is cut.
MAX_TEXT_CHARS = 400_000


class BundleReviewRefusedError(ValueError):
    """The results cannot be reviewed together (another family, another entry)."""


async def _entry_pages(db: AsyncSession, entry: Any) -> tuple[list[str], str | None, str | None]:
    """The text the review annotates: the entry's prompt, else its first attached file's
    text layer (one string per PDF page); with the file name and where the text came from."""
    prompt = (getattr(entry, "input_prompt", None) or "").strip()
    file_ids = getattr(entry, "file_ids", None) or []
    if not file_ids:
        return ([prompt[:MAX_TEXT_CHARS]] if prompt else []), None, "prompt" if prompt else None
    from app.core.parse_offload import run_parse
    from app.models.file_attachment import FileAttachment
    from app.services.chunking import extract_text_from_file, pdf_page_texts
    from app.services.file_storage import file_storage

    try:
        attachment = await db.get(FileAttachment, uuid.UUID(str(file_ids[0])))
    except ValueError:
        attachment = None
    if attachment is None:
        return [], None, None
    try:
        content = await file_storage.download(attachment.s3_object_name)
        if attachment.filename.lower().endswith(".pdf"):
            pages = await run_parse(pdf_page_texts, content)
        else:
            pages = [await run_parse(extract_text_from_file, content, attachment.filename)]
    except Exception:
        return [], attachment.filename, None
    budget, kept = MAX_TEXT_CHARS, []
    for page in pages:
        if budget <= 0:
            break
        kept.append(page[:budget])
        budget -= len(page)
    return kept, attachment.filename, "file"


async def load_bundle_review(db: AsyncSession, result_ids: Sequence[uuid.UUID]) -> dict[str, Any]:
    """The review of these results' answers against the entry the first was scored on.

    Raises:
        BundleReviewRefusedError: no result, too many, a result missing, one of another
            family than STIX, or one scored against another expected bundle.
    """
    from app.services.evaluation_engine import scored_entry

    if not 1 <= len(result_ids) <= MAX_REVIEWED_ANSWERS:
        raise BundleReviewRefusedError(f"Review 1 to {MAX_REVIEWED_ANSWERS} answers")
    rows = (
        await db.execute(
            select(EvaluationResult, EvaluationRun, Evaluation)
            .join(EvaluationRun, EvaluationRun.id == EvaluationResult.run_id)
            .join(Evaluation, Evaluation.id == EvaluationRun.evaluation_id)
            .where(EvaluationResult.id.in_(list(result_ids)))
        )
    ).all()
    found = {r[0].id: r for r in rows}
    if len(found) != len(set(result_ids)):
        raise LookupError("Evaluation result not found")
    ordered = [found[rid] for rid in dict.fromkeys(result_ids)]
    if any(evaluation.family != STIX_BUNDLE for _, _, evaluation in ordered):
        raise BundleReviewRefusedError("Only STIX_BUNDLE answers have a bundle review")

    entries = [await scored_entry(db, result) for result, _, _ in ordered]
    entry = entries[0][0]
    if entry is None:
        raise BundleReviewRefusedError("The entry this answer was scored against no longer exists")
    key = reference_key(STIX_BUNDLE, entry.expected_answer)
    for other, _ in entries[1:]:
        if other is None or reference_key(STIX_BUNDLE, other.expected_answer) != key:
            raise BundleReviewRefusedError("These answers were not scored against the same expected bundle")

    review = build_review(entry.expected_answer, [(str(r.id), r.agent_output) for r, _, _ in ordered])
    if review is None:
        raise BundleReviewRefusedError("The expected bundle cannot be read, or the STIX backend is unavailable")

    pooled = [m["key"] for m in quality_catalog(STIX_BUNDLE) if is_pooled_key(m["key"])]
    values: dict[uuid.UUID, dict[str, float | None]] = {r.id: {} for r, _, _ in ordered}
    for rid, metric, value in (
        await db.execute(
            select(EvaluationMetric.result_id, EvaluationMetric.key, EvaluationMetric.value).where(
                EvaluationMetric.result_id.in_(list(values)), EvaluationMetric.key.in_(pooled)
            )
        )
    ).all():
        values[rid][metric] = value

    pages, file_name, text_source = await _entry_pages(db, entry)
    return {
        "entry": {
            "label": (getattr(entry, "description", None) or "").strip() or f"Entry {getattr(entry, 'position', 0) + 1}",
            "pages": pages,
            "file_name": file_name,
            "text_source": text_source,
        },
        "answers": [
            {
                "key": str(result.id),
                "result_id": str(result.id),
                "run_id": str(run.id),
                "evaluation_id": str(evaluation.id),
                "evaluation_name": evaluation.name,
                "model_id": run.model_id,
                "subject_id": str(run.agent_id or run.flow_id) if (run.agent_id or run.flow_id) else None,
                "created_at": run.created_at.isoformat() if run.created_at else None,
                "judge_score": result.score,
                "anomaly": result.execution_anomaly_kind,
                "metrics": values[result.id],
            }
            for result, run, evaluation in ordered
        ],
        **review,
    }


__all__ = [
    "EXPECTED",
    "MAX_PAIRED_OBJECTS",
    "MAX_REVIEWED_ANSWERS",
    "BundleReviewRefusedError",
    "build_review",
    "describe",
    "load_bundle_review",
]
