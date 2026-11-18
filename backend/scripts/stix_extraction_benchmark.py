"""Benchmark the grounded STIX extraction on span-annotated reports, scored two ways.

Runs ``app.services.stix_extraction`` on every document of a doccano JSONL ground truth
(the research datasets: MAR, VOSC...) with a real provider, then scores the same
predictions with:

* the research pipeline's **span metrics**, for continuity with its published numbers
  (NER strict / type / within-exact on character spans after overlap resolution; RE
  strict-strict on mention spans; RE set strict-strict on raw surface text), and
* the platform's **STIX_BUNDLE metrics** (``run_for_family``), what an XTM One evaluation
  reports: exact canonical-id F1 and the aligned, soft and relationship-aligned F1.

Per-document results, the pooled (micro) and per-document (macro) means, and a document
bootstrap 95% interval when there are at least two documents, go to ``--out``.

Input: a doccano JSONL ground truth (span metrics too), or a JSONL of ``{"id", "text",
"reference"}`` STIX references (``stix_benchmark_azerg.py``). ``--system harvester`` runs the
built-in harvester's method instead of the pipeline, ``--profile coarse`` scores a dataset
whose guideline is coarser than STIX (see ``coarse``).

Credentials come only from the environment (``OPENAI_API_KEY`` /
``ANTHROPIC_API_KEY``); no ``.env`` is read and no key is printed.

Example (from ``backend/``)::

    OPENAI_API_KEY=... uv run --no-sync python scripts/stix_extraction_benchmark.py \\
        --ground-truth ../data/ground_truth.jsonl --provider openai --model gpt-4.1 --out /tmp/bench
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import os
import random
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from stix_benchmark_doccano import doccano_to_stix, label_kind

from app.services.evaluation.metric_package.quality_adapter import (
    build_quality_config,
    run_quality,
)
from app.services.stix_extraction import (
    ExtractionOptions,
    Usage,
    extract_stix,
    provider_structured_call,
)
from app.services.stix_extraction.ontology import RELATION_TYPES

_STIX_KEYS = (
    "stix.entity_prf",
    "stix.entity_prf.aligned",
    "stix.entity_prf.soft",
    "stix.relationship_prf",
    "stix.relationship_prf.aligned",
)


def _provider(name: str):
    if name == "openai":
        from app.llm.openai_provider import OpenAIProvider

        return OpenAIProvider(os.environ["OPENAI_API_KEY"])
    if name == "anthropic":
        from app.llm.anthropic_provider import AnthropicProvider

        return AnthropicProvider(os.environ["ANTHROPIC_API_KEY"])
    raise SystemExit(f"unknown provider {name}")


# --- span metrics (the research pipeline's, for continuity) ----------------------------


def _class(kind: str | None) -> str:
    """Span metrics compare labels after the dataset's own merge: a threat actor is an
    intrusion set."""
    if kind is None:
        return "?"
    return "intrusion-set" if kind == "threat-actor" else kind


def _resolve_overlaps(spans: list[dict]) -> list[dict]:
    """Keep one span per overlapping group: most relations, then longest, then earliest."""
    spans = sorted(spans, key=lambda s: (-s["relations"], -(s["end"] - s["start"]), s["start"]))
    kept: list[dict] = []
    for span in spans:
        if any(span["start"] < k["end"] and k["start"] < span["end"] for k in kept):
            continue
        kept.append(span)
    return sorted(kept, key=lambda s: s["start"])


def _prf(tp: int, fp: int, fn: int) -> dict:
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    f = 2 * p * r / (p + r) if p + r else 0.0
    return {"precision": p, "recall": r, "f1": f, "tp": tp, "fp": fp, "fn": fn}


def _gt_spans(document: dict) -> tuple[list[dict], list[tuple]]:
    entities = {e["id"]: e for e in document["entities"]}
    rel_count: dict = defaultdict(int)
    for relation in document["relations"]:
        rel_count[relation["from_id"]] += 1
        rel_count[relation["to_id"]] += 1
    spans = [
        {"start": e["start_offset"], "end": e["end_offset"], "label": _class(label_kind(e["label"])),
         "relations": rel_count[e["id"]], "id": e["id"]}
        for e in document["entities"] if label_kind(e["label"])
    ]
    kept = {s["id"] for s in _resolve_overlaps(spans)}
    relations = []
    for relation in document["relations"]:
        s, t = entities.get(relation["from_id"]), entities.get(relation["to_id"])
        if not s or not t or s["id"] not in kept or t["id"] not in kept:
            continue
        relations.append(((s["start_offset"], s["end_offset"], _class(label_kind(s["label"]))),
                          relation["type"].lower(),
                          (t["start_offset"], t["end_offset"], _class(label_kind(t["label"])))))
    return [s for s in spans if s["id"] in kept], relations


def _pred_spans(result) -> tuple[list[dict], list[tuple]]:
    rel_count: dict = defaultdict(int)
    for relation in result.relations:
        rel_count[relation.source] += 1
        rel_count[relation.target] += 1
    spans = [
        {"start": s, "end": e, "label": _class(obj.kind), "relations": rel_count[obj.ref], "ref": obj.ref}
        for obj in result.objects for s, e in obj.spans
    ]
    kept = _resolve_overlaps(spans)
    kept_by_ref: dict = defaultdict(list)
    for span in kept:
        kept_by_ref[span["ref"]].append(span)
    relations = []
    for relation in result.relations:
        sources, targets = kept_by_ref.get(relation.source, []), kept_by_ref.get(relation.target, [])
        if not sources or not targets:
            continue
        # A span metric needs one mention pair: the closest, as the research pipeline chose.
        s, t = min(((s, t) for s in sources for t in targets),
                   key=lambda p: min(abs(p[0]["end"] - p[1]["start"]), abs(p[0]["start"] - p[1]["end"])))
        relations.append(((s["start"], s["end"], s["label"]), relation.type, (t["start"], t["end"], t["label"])))
    return kept, relations


def span_metrics(document: dict, result) -> dict:
    text = document["text"]
    gt, gt_rel = _gt_spans(document)
    pred, pred_rel = _pred_spans(result)
    gt_set = {(s["start"], s["end"], s["label"]) for s in gt}
    pred_set = {(s["start"], s["end"], s["label"]) for s in pred}
    strict = len(gt_set & pred_set)
    # Type match: an overlapping span with the same label, paired one-to-one greedily.
    used: set = set()
    type_tp = 0
    for g in gt:
        for i, p in enumerate(pred):
            if i not in used and p["label"] == g["label"] and p["start"] < g["end"] and g["start"] < p["end"]:
                used.add(i)
                type_tp += 1
                break
    within = sum(1 for g in gt if any(p["start"] <= g["start"] and g["end"] <= p["end"] for p in pred))
    within_p = sum(1 for p in pred if any(p["start"] <= g["start"] and g["end"] <= p["end"] for g in gt))
    gt_rel_set, pred_rel_set = set(gt_rel), set(pred_rel)

    def surface(rel: tuple) -> tuple:
        (s0, s1, sl), kind, (t0, t1, tl) = rel
        return (text[s0:s1], sl, kind, text[t0:t1], tl)

    gt_surface, pred_surface = {surface(r) for r in gt_rel}, {surface(r) for r in pred_rel}
    return {
        "ner_strict": _prf(strict, len(pred_set) - strict, len(gt_set) - strict),
        "ner_type": _prf(type_tp, len(pred) - type_tp, len(gt) - type_tp),
        "ner_within_exact": {"precision": within_p / len(pred) if pred else 0.0,
                             "recall": within / len(gt) if gt else 0.0},
        "re_strict_strict": _prf(len(gt_rel_set & pred_rel_set), len(pred_rel_set - gt_rel_set),
                                 len(gt_rel_set - pred_rel_set)),
        "re_set_strict_strict": _prf(len(gt_surface & pred_surface), len(pred_surface - gt_surface),
                                     len(gt_surface - pred_surface)),
    }


# --- STIX metrics (the platform's) ----------------------------------------------------


def stix_metrics(reference: dict, prediction: dict) -> dict:
    """The STIX_BUNDLE metrics through the platform's own seam (``run_quality``), exactly
    as an XTM One evaluation run computes them."""
    config = build_quality_config("STIX_BUNDLE", None, [])
    outcomes = run_quality(
        family="STIX_BUNDLE",
        reference=reference,
        prediction=json.dumps(prediction),
        output_schema=None,
        config=config,
    )
    out = {}
    for outcome in outcomes:
        if outcome.key not in (*_STIX_KEYS, "stix.semantic_validity"):
            continue
        detail = outcome.serialized.get("detail") or {}
        out[outcome.key] = {
            "value": outcome.value,
            **{k: detail.get(k) for k in ("precision", "recall", "f1", "tp", "fp", "fn", "numerator", "denominator")},
            **{k: list(detail.get(k) or []) for k in ("missing", "spurious", "fragments", "partial") if k in detail},
        }
    return out


def stage_metrics(reference: dict, result, args: argparse.Namespace | None = None, bundles: dict | None = None) -> dict:
    """Entity and relationship scores after each step, as the research board showed them
    (detection 96 recall / 56 precision, then validation buying precision back): which
    step loses what."""
    from app.services.stix_extraction.bundle import build_bundle

    stages = result.stages
    patterns = stages.get("patterns", [])
    out = {}
    for stage, objects, relations in (
        ("detection", [*stages.get("detected", []), *patterns], []),
        ("resolution", [*stages.get("resolved", []), *patterns], []),
        ("relations_raw", stages.get("final", []), stages.get("relations_raw", [])),
        ("final", stages.get("final", []), stages.get("relations_final", [])),
    ):
        bundle = build_bundle(objects, relations, document_name="", include_report=False, include_evidence=False)
        if bundles is not None:
            bundles[stage] = bundle
        # Under the run's own profile, so each stage reads like the headline numbers.
        scored_reference, scored_bundle = scored(reference, bundle, args) if args is not None else (reference, bundle)
        scores = stix_metrics(scored_reference, scored_bundle)
        keep = ("stix.entity_prf.aligned",) if not relations else (
            "stix.entity_prf.aligned", "stix.relationship_prf.aligned")
        out[stage] = {
            key: {k: scores[key].get(k) for k in ("precision", "recall", "f1", "tp", "fp", "fn")} for key in keep
        }
    return out


# --- aggregation ------------------------------------------------------------------------


def _micro(docs: list[dict], section: str, key: str) -> dict:
    docs = [d for d in docs if d[section][key].get("tp") is not None]
    tp = sum(d[section][key]["tp"] for d in docs)
    fp = sum(d[section][key]["fp"] for d in docs)
    fn = sum(d[section][key]["fn"] for d in docs)
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    return {"precision": p, "recall": r, "f1": 2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else 1.0}


def _bootstrap(values: list[float], iterations: int = 10_000, seed: int = 7) -> tuple[float, float] | None:
    if len(values) < 2:
        return None
    rng = random.Random(seed)  # noqa: S311 - a statistical resample, not a secret
    means = sorted(sum(rng.choice(values) for _ in values) / len(values) for _ in range(iterations))
    return means[int(0.025 * iterations)], means[int(0.975 * iterations) - 1]


def summarize(docs: list[dict]) -> dict:
    summary: dict = {"documents": len(docs)}
    sections = [("stix", _STIX_KEYS)]
    if all("span" in d for d in docs):
        sections.insert(0, ("span", ("ner_strict", "ner_type", "re_strict_strict", "re_set_strict_strict")))
    for section, keys in sections:
        for key in keys:
            # A metric the suite skipped for a document (too many objects to pair) is left
            # out of the averages and counted.
            f1s = [
                d[section][key]["f1"] if section == "span" else d[section][key]["f1"] / 100.0
                for d in docs if d[section][key].get("f1") is not None
            ]
            if section == "stix":
                micro = _micro([{"stix": {key: d["stix"][key]}} for d in docs], "stix", key)
            else:
                micro = _micro(docs, section, key)
            summary[f"{section}.{key}"] = {
                "unscored_documents": len(docs) - len(f1s),
                "micro": micro,
                "macro_f1": sum(f1s) / len(f1s),
                "macro_f1_ci95": _bootstrap(f1s),
            }
    summary["cost"] = {
        "calls": sum(d["usage"]["calls"] for d in docs),
        "input_tokens": sum(d["usage"]["input_tokens"] for d in docs),
        "output_tokens": sum(d["usage"]["output_tokens"] for d in docs),
        "seconds": sum(d["seconds"] for d in docs),
        "characters": sum(d["characters"] for d in docs),
    }
    return summary


# --- evaluation profile ---------------------------------------------------------------

#: The relationship types the first benchmark scored (``--relations core``); ``all`` scores
#: every type the extraction writes, observables' and indicators' included.
_CORE_RELATIONS = frozenset({"uses", "targets", "exploits", "located-at", "originates-from", "attributed-to"})
_ALL_RELATIONS = frozenset({*RELATION_TYPES, "authored-by", "based-on"})


def split_aliases(bundle: dict) -> dict:
    """For a dataset that annotates each alias as its own entity (AZERG: Crambus, OilRig,
    APT34): a predicted object with aliases becomes one object per name, each carrying the
    object's relationships. STIX merges them; this only reads the prediction the dataset's way."""
    objects = list(bundle.get("objects", []))
    clones: dict[str, list[str]] = {}
    extra = []
    for obj in objects:
        names = [a for a in (obj.get("aliases") or obj.get("x_opencti_aliases") or []) if isinstance(a, str)]
        if obj.get("type") == "relationship" or not names:
            continue
        clones[obj["id"]] = []
        for index, name in enumerate(names):
            clone = {k: v for k, v in obj.items() if k not in ("aliases", "x_opencti_aliases")}
            clone["id"] = f"{obj['type']}--alias-{index}-{obj['id'].split('--', 1)[1]}"
            clone["name"] = name
            clones[obj["id"]].append(clone["id"])
            extra.append(clone)
    for rel in [o for o in objects if o.get("type") == "relationship"]:
        sources = [rel["source_ref"], *clones.get(rel["source_ref"], [])]
        targets = [rel["target_ref"], *clones.get(rel["target_ref"], [])]
        for i, source in enumerate(sources):
            for j, target in enumerate(targets):
                if i or j:
                    extra.append({**rel, "id": f"{rel['id']}-alias-{i}-{j}", "source_ref": source, "target_ref": target})
    return {**bundle, "objects": objects + extra}


def without_sectors(bundle: dict) -> dict:
    """For a dataset that annotates no sectors (AZERG): sector identities, and the
    relationships that reach them, left out of both sides."""
    sectors = {
        o["id"] for o in bundle.get("objects", []) if o.get("type") == "identity" and o.get("identity_class") == "class"
    }
    return {**bundle, "objects": [
        o for o in bundle.get("objects", [])
        if o.get("id") not in sectors and o.get("source_ref") not in sectors and o.get("target_ref") not in sectors
    ]}


def _signature(objects: dict[str, dict], relationship: dict) -> tuple[str, str, str]:
    return (
        str(objects.get(relationship.get("source_ref"), {}).get("type")),
        str(relationship.get("relationship_type", "")).lower(),
        str(objects.get(relationship.get("target_ref"), {}).get("type")),
    )


def closed_world(references: list[dict]) -> tuple[set[str], set[tuple[str, str, str]]]:
    """The object types and relationship signatures a dataset annotates at least once:
    what its guideline covers (Text2KGBench's "locally closed" evaluation). A prediction
    outside them cannot be checked against this dataset, right or wrong."""
    types: set[str] = set()
    signatures: set[tuple[str, str, str]] = set()
    for reference in references:
        objects = {o["id"]: o for o in reference.get("objects", [])}
        for obj in objects.values():
            if obj.get("type") == "relationship":
                signatures.add(_signature(objects, obj))
            else:
                types.add(str(obj.get("type")))
    return types, signatures


def within(bundle: dict, world: tuple[set[str], set[tuple[str, str, str]]]) -> dict:
    """*bundle* restricted to the dataset's closed world."""
    types, signatures = world
    objects = {o["id"]: o for o in bundle.get("objects", [])}
    kept = []
    for obj in objects.values():
        if obj.get("type") == "relationship":
            if _signature(objects, obj) in signatures:
                kept.append(obj)
        elif obj.get("type") in types:
            kept.append(obj)
    return {**bundle, "objects": kept}


def profiled(reference: dict, prediction: dict, args: argparse.Namespace) -> tuple[dict, dict]:
    """Both sides under the run's guideline profile, alike (and, for the prediction, a
    relationship confidence floor when ``--min-confidence``). A prediction entry that is not a
    STIX object (a model that wrote a bare string into ``objects``) is not knowledge: dropped."""
    prediction = {**prediction, "objects": [o for o in prediction.get("objects", []) if isinstance(o, dict)]}
    floor = getattr(args, "min_confidence", 0)
    if floor:
        prediction = {**prediction, "objects": [
            o for o in prediction.get("objects", [])
            if o.get("type") != "relationship" or int(o.get("confidence", 100)) >= floor
        ]}
    if args.no_sectors:
        reference, prediction = without_sectors(reference), without_sectors(prediction)
    if getattr(args, "alias_split", False):
        prediction = split_aliases(prediction)
    if args.profile == "coarse":
        scored = _ALL_RELATIONS if getattr(args, "relations", "core") == "all" else _CORE_RELATIONS
        reference, prediction = coarse(reference, scored), coarse(prediction, scored)
    return reference, prediction


_WORLD: dict[str, tuple[set[str], set[tuple[str, str, str]]]] = {}


def scored(reference: dict, prediction: dict, args: argparse.Namespace) -> tuple[dict, dict]:
    """Both sides under the profile, then the prediction restricted to the dataset's closed
    world when ``--closed-world`` (computed once over every reference of the run)."""
    reference, prediction = profiled(reference, prediction, args)
    if args.closed_world and "world" in _WORLD:
        prediction = within(prediction, _WORLD["world"])
    return reference, prediction


def prepare_world(references: list[dict], args: argparse.Namespace) -> None:
    if args.closed_world:
        _WORLD["world"] = closed_world([profiled(ref, ref, args)[0] for ref in references])


def coarse(bundle: dict, relations: frozenset[str] = _CORE_RELATIONS) -> dict:
    """The ``coarse`` profile, applied to both sides alike, for a dataset whose guideline is
    coarser than STIX's: one identity class (a sector is not told from an organization), one
    location class (a country from a region), a channel is a tool, an ``indicator`` is the
    observable its pattern names, and only ``relations`` are scored (``--relations``, read from
    the run's arguments so a caller importing this module scores what it asks for)."""
    from app.services.stix_extraction.iocs import find_pattern_entities

    # An indicator based on an observable of the bundle is scored as that observable.
    based_on = {
        o.get("source_ref") for o in bundle.get("objects", [])
        if o.get("type") == "relationship" and o.get("relationship_type") == "based-on"
    }
    objects = []
    for obj in bundle.get("objects", []):
        obj = dict(obj)
        kind = obj.get("type")
        if kind in ("report", "course-of-action", "marking-definition", "note", "grouping"):
            continue
        if kind == "relationship" and str(obj.get("relationship_type", "")).lower() not in relations and not (
            obj.get("relationship_type") == "based-on" and str(obj.get("source_ref", "")).startswith("indicator--")
        ):
            # An indicator's based-on is kept whatever is scored: the metric reads it to fold
            # the indicator into its observable and never scores it as a relationship.
            continue
        if kind == "identity":
            obj.pop("identity_class", None)
        elif kind == "location":
            for field in ("x_opencti_location_type", "country", "region", "city"):
                obj.pop(field, None)
        elif kind == "channel":
            obj["type"] = "tool"
        elif kind == "indicator" and obj["id"] not in based_on:
            values = re.findall(r"=\s*'([^']+)'", str(obj.get("pattern", "")))
            found = find_pattern_entities(values[0]) if values else []
            if not found:
                continue
            match = found[0]
            obj = {"type": "file", "id": obj["id"], "hashes": {"SHA-256": match.value}} if match.kind == "sha256" else (
                {"type": match.kind if match.kind != "file-name" else "file", "id": obj["id"],
                 **({"name": match.value} if match.kind == "file-name" else {"value": match.value})}
            )
        objects.append(obj)
    return {**bundle, "objects": objects}


# --- systems ---------------------------------------------------------------------------


async def run_pipeline(document: dict, llm: object, args: argparse.Namespace, options: ExtractionOptions):
    usage = Usage()
    call = provider_structured_call(llm, args.model, usage, max_concurrency=args.concurrency)
    if getattr(args, "call_cache", ""):
        call = replayed(call, Path(args.call_cache), args.model)
    routes = dict(r.split("=", 1) for r in getattr(args, "step_model", "").split(",") if "=" in r)
    if routes:
        call = routed(call, routes, llm, usage, args)
    knowledge = None
    if args.knowledge:
        from app.services.stix_extraction.knowledge import attack_knowledge

        knowledge = attack_knowledge
    techniques = None
    if args.techniques or args.technique_linking:
        from app.services.stix_extraction.knowledge import attack_catalogue

        techniques = attack_catalogue
    embed = None
    if getattr(args, "technique_retrieval_k", 0):
        embed = _openai_embed(args.embedding_model)
    result = await extract_stix(document["text"], call, usage=usage, document_name=f"doc {document.get('id')}",
                                options=options, knowledge=knowledge, techniques=techniques, embed=embed)
    return result.bundle, usage.as_dict(), result


_EMBEDDERS: dict[str, object] = {}


def _openai_embed(model: str):
    """An embedding function on OpenAI's API (one per model, so the catalogue's vectors are
    computed once a run): texts -> vectors, in batches."""
    if model not in _EMBEDDERS:
        import openai

        client = openai.AsyncOpenAI()

        async def embed(texts: list[str]) -> list[list[float]] | None:
            vectors: list[list[float]] = []
            for start in range(0, len(texts), 256):
                answer = await client.embeddings.create(model=model, input=[t[:8000] or " " for t in texts[start:start + 256]])
                vectors += [d.embedding for d in answer.data]
            return vectors

        _EMBEDDERS[model] = embed
    return _EMBEDDERS[model]


def routed(call, routes: dict[str, str], llm: object, usage: Usage, args: argparse.Namespace):
    """*call*, with the steps *routes* names sent to another model of the same provider (a
    stronger model on the steps that decide precision), each recorded like the others."""
    by_model = {}
    for model in set(routes.values()):
        other = provider_structured_call(llm, model, usage, max_concurrency=args.concurrency)
        by_model[model] = replayed(other, Path(args.call_cache), model) if getattr(args, "call_cache", "") else other

    async def dispatch(*, step: str, system: str, user: str, schema: dict) -> dict | None:
        # A step's extra samples ("detection_2") go where the step goes.
        route = routes.get(step) or routes.get(re.sub(r"_\d+$", "", step), "")
        target = by_model.get(route, call)
        return await target(step=step, system=system, user=user, schema=schema)

    return dispatch


def replayed(call, folder: Path, model: str):
    """*call* recording each answer under *folder*, keyed by everything the call sends, and
    answering from the record when the same call comes again. Two variants of the pipeline
    run against one record read the same answers up to the first step they change, so their
    difference is the change, not the model's sampling. A failed call is not recorded."""
    import hashlib

    folder.mkdir(parents=True, exist_ok=True)

    async def cached(*, step: str, system: str, user: str, schema: dict) -> dict | None:
        key = hashlib.sha256(json.dumps([model, step, system, user, schema], sort_keys=True).encode()).hexdigest()
        path = folder / f"{key}.json"
        if path.exists():
            return json.loads(path.read_text())
        with (folder / "misses.log").open("a") as log:
            log.write(f"{step}\t{key}\n")
        if os.environ.get("XTM_CALL_CACHE_DEBUG"):
            (folder / "misses").mkdir(exist_ok=True)
            (folder / "misses" / f"{key}.json").write_text(json.dumps({"step": step, "system": system, "user": user}))
        answer = await call(step=step, system=system, user=user, schema=schema)
        if answer is None:
            return None
        # Runs in parallel may ask one call at once: each writes its own file, the first
        # recorded answer stays, and every run goes on with that one.
        tmp = path.with_suffix(f".{os.getpid()}.{random.getrandbits(32):08x}.tmp")
        tmp.write_text(json.dumps(answer))
        try:
            os.link(tmp, path)
        except FileExistsError:
            answer = json.loads(path.read_text())
        except OSError:
            # A filesystem without hard links (EPERM, ENOTSUP): the last answer written
            # stays, as before the links.
            tmp.replace(path)
        finally:
            tmp.unlink(missing_ok=True)
        return answer

    return cached


async def run_harvester(document: dict, llm: object, args: argparse.Namespace):
    """The built-in harvester's method in one call: its persona and its bound skill as the
    system prompt, the report as the message, JSON out (its agent loop then validates)."""
    from app.llm.base import ChatMessage
    from app.services.structured_output import validate_and_parse

    if not document["text"].strip():  # a passage with no text (a source that could not be fetched): nothing to send
        return {"type": "bundle", "objects": []}, {"calls": 0, "input_tokens": 0, "output_tokens": 0}, None
    seed = Path(__file__).resolve().parents[1] / "app" / "agents" / "seed_data"
    system = (seed / "agent_persona" / "cti-stix-harvester.md").read_text() + "\n\n" + (
        seed / "skill_content" / "opencti-stix-bundle-construction.md").read_text()
    # JSON mode is OpenAI's; other providers answer JSON from the skill's instructions and
    # the answer is parsed the same way.
    hints = {"structured_output": {"type": "json_object"}} if args.provider == "openai" else {}
    response = await llm.chat(
        [ChatMessage(role="system", content=system), ChatMessage(role="user", content=document["text"])],
        model=args.model, temperature=0.0, max_tokens=32_000, **hints,
    )
    bundle, _ = validate_and_parse(response.content or "", {"type": "object"})
    usage = response.usage or {}
    return (bundle if isinstance(bundle, dict) else {"type": "bundle", "objects": []}), {
        "calls": 1,
        "input_tokens": int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0),
        "output_tokens": int(usage.get("completion_tokens") or usage.get("output_tokens") or 0),
    }, None


def rescore(args: argparse.Namespace) -> None:
    """Score a finished run's saved bundles again (another metric revision or profile),
    without a model call; the run's cost and time are kept."""
    folder = Path(args.rescore)
    previous = json.loads((folder / "results.json").read_text())
    # Another reference of the same passages (a STIX JSONL, ``--ground-truth``): the run's
    # bundles scored against a richer annotation without running it again.
    others = {}
    if args.ground_truth:
        others = {
            str(d["id"]): d["reference"]
            for d in (json.loads(line) for line in Path(args.ground_truth).read_text().splitlines() if line.strip())
        }

    def reference_of(doc_id: str) -> dict:
        return others[doc_id] if others else json.loads((folder / f"doc-{doc_id}.reference.json").read_text())

    prepare_world([reference_of(r["id"]) for r in previous["documents"]], args)
    per_doc = []
    for record in previous["documents"]:
        doc_id = record["id"]
        reference = reference_of(doc_id)
        prediction = json.loads((folder / f"doc-{doc_id}.prediction.json").read_text())
        scored_reference, scored_prediction = scored(reference, prediction, args)
        per_doc.append({**{k: record[k] for k in ("id", "characters", "seconds", "usage")},
                        "stix": stix_metrics(scored_reference, scored_prediction)})
    report = {**{k: previous.get(k) for k in ("system", "model", "provider", "options")},
              "profile": args.profile, "no_sectors": args.no_sectors, "closed_world": args.closed_world,
              "summary": summarize(per_doc),
              "documents": per_doc}
    suffix = (
        f"{args.profile}{'-nosectors' if args.no_sectors else ''}{'-closed' if args.closed_world else ''}"
        f"{f'-min{args.min_confidence}' if args.min_confidence else ''}{'-aliassplit' if args.alias_split else ''}"
        f"{'-allrel' if args.relations == 'all' else ''}{f'-{Path(args.ground_truth).stem}' if others else ''}"
    )
    (folder / f"rescored-{suffix}.json").write_text(
        json.dumps(report, indent=1, default=str))
    print(json.dumps(report["summary"], default=str))


async def run(args: argparse.Namespace) -> None:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    documents = [json.loads(line) for line in Path(args.ground_truth).read_text().splitlines() if line.strip()]
    documents = documents[: args.limit] if args.limit else documents
    prepare_world([d["reference"] if "reference" in d else doccano_to_stix(d) for d in documents], args)
    llm = _provider(args.provider)
    options = ExtractionOptions(chunk_chars=args.chunk_chars, relation_chunk_chars=args.relation_chunk_chars,
                                include_implied=not args.stated_only, verify_relations=args.verify,
                                verify_score_only=args.verify_score_only, relation_gleaning=args.glean,
                                relation_pairs=not args.no_pairs, relatedness=args.relatedness,
                                cross_chunk_relations=args.cross_chunk_relations,
                                behaviour_techniques=args.techniques, technique_threshold=args.technique_threshold,
                                knowledge_focus=args.knowledge_focus, technique_mode=args.technique_mode,
                                stated_links=args.stated_links, relevance_review=args.relevance,
                                relevance_web_addresses=args.relevance_web_addresses,
                                activity_hub=args.activity_hub, technique_linking=args.technique_linking,
                                technique_retrieval_k=args.technique_retrieval_k,
                                technique_choice=args.technique_choice,
                                technique_check_views=tuple(args.technique_check_views),
                                structural_techniques=args.structural_techniques,
                                detection_samples=args.detection_samples)
    gate = asyncio.Semaphore(args.documents_in_parallel)

    async def one(document: dict) -> dict:
        # Resume: a passage whose record is saved is not run again.
        saved = out / f"doc-{document.get('id')}.record.json"
        if saved.exists():
            return json.loads(saved.read_text())
        predicted = out / f"doc-{document.get('id')}.prediction.json"
        if predicted.exists():  # a run interrupted before records were saved: score its bundle
            reference = document["reference"] if "reference" in document else doccano_to_stix(document)
            scored_reference, scored_bundle = scored(reference, json.loads(predicted.read_text()), args)
            record = {"id": document.get("id"), "characters": len(document["text"]), "seconds": 0, "resumed": True,
                      "usage": {"calls": 0, "failed_calls": 0, "input_tokens": 0, "output_tokens": 0},
                      "stix": stix_metrics(scored_reference, scored_bundle)}
            saved.write_text(json.dumps(record, default=str))
            return record
        async with gate:
            started = time.monotonic()
            for attempt in range(2):
                try:
                    # A provider connection can hang without a timeout (a machine that slept):
                    # a passage that takes this long is run again, once.
                    if args.system == "harvester":
                        bundle, usage, result = await asyncio.wait_for(run_harvester(document, llm, args), args.document_timeout)
                    else:
                        bundle, usage, result = await asyncio.wait_for(run_pipeline(document, llm, args, options), args.document_timeout)
                    break
                except TimeoutError:
                    if attempt:
                        raise
                    print(f"doc {document.get('id')}: timed out, run again", flush=True)
            seconds = time.monotonic() - started
        reference = document["reference"] if "reference" in document else doccano_to_stix(document)
        scored_reference, scored_bundle = scored(reference, bundle, args)
        record = {
            "id": document.get("id"),
            "characters": len(document["text"]),
            "seconds": round(seconds, 1),
            "usage": usage,
            "stix": stix_metrics(scored_reference, scored_bundle),
        }
        if result is not None:
            record["pipeline_stats"] = result.stats
            # Which step loses what: scores after detection, resolution, the relationships
            # the model proposed, and the final bundle (each stage's bundle is saved).
            traced: dict = {}
            record["stages"] = stage_metrics(reference, result, args, traced)
            (out / f"doc-{document.get('id')}.stages.json").write_text(json.dumps(
                {"bundles": traced, "off_ontology": result.stages.get("relations_off_ontology", []),
                 "technique_scores": result.stages.get("technique_scores", [])}, default=str))
        if "entities" in document and result is not None:
            record["span"] = span_metrics(document, result)
        (out / f"doc-{document.get('id')}.prediction.json").write_text(json.dumps(bundle, indent=1))
        (out / f"doc-{document.get('id')}.reference.json").write_text(json.dumps(reference, indent=1))
        saved.write_text(json.dumps(record, default=str))
        def f1(key: str) -> str:  # undefined (no reference and no prediction of that kind): "n/a"
            value = record["stix"][key]["f1"]
            return "n/a" if value is None else f"{value:.1f}"

        print(f"doc {document.get('id')}: {seconds:.0f}s, {usage['calls']} calls, "
              f"entity aligned F1 {f1('stix.entity_prf.aligned')}, "
              f"relationship aligned F1 {f1('stix.relationship_prf.aligned')}", flush=True)
        for stage, scores in record.get("stages", {}).items():
            line = "  ".join(
                f"{key.split('.')[1]} P {v['precision']:.0f} R {v['recall']:.0f} F1 {v['f1']:.0f}"
                for key, v in scores.items() if v.get("f1") is not None
            )
            print(f"    {stage:14s} {line}", flush=True)
        return record

    per_doc = list(await asyncio.gather(*(one(document) for document in documents)))
    report = {"system": args.system, "model": args.model, "step_model": args.step_model, "provider": args.provider, "profile": args.profile,
              "options": dataclasses.asdict(options), "summary": summarize(per_doc), "documents": per_doc}
    (out / "results.json").write_text(json.dumps(report, indent=1, default=str))
    print(json.dumps(report["summary"], indent=1, default=str))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--ground-truth", default="")
    parser.add_argument("--provider", default="openai", choices=("openai", "anthropic"))
    parser.add_argument("--model", default="gpt-4.1")
    parser.add_argument("--out", default="")
    parser.add_argument("--rescore", default="", help="Score a finished run folder again, no model call.")
    parser.add_argument("--no-sectors", action="store_true", help="The dataset annotates no sectors.")
    parser.add_argument("--closed-world", action="store_true",
                        help="Score predictions only within the object types and relationship signatures the dataset annotates.")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--chunk-chars", type=int, default=2_000)
    parser.add_argument("--detection-samples", type=int, default=1,
                        help="Detection calls per chunk, their entities united before resolution.")
    parser.add_argument("--relation-chunk-chars", type=int, default=3_000)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--documents-in-parallel", type=int, default=1)
    parser.add_argument("--system", default="pipeline", choices=("pipeline", "harvester"))
    parser.add_argument("--profile", default="full", choices=("full", "coarse"))
    parser.add_argument("--pairs", action="store_true", help="Kept for older commands: the pair pass is on by default.")
    parser.add_argument("--no-pairs", action="store_true", help="Leave out the candidate-pair recall pass.")
    parser.add_argument("--relatedness", action="store_true", help="The relatedness-oriented pair pass (relatedness option).")
    parser.add_argument("--cross-chunk-relations", action="store_true",
                        help="Relationships between entities named in different relationship chunks (cross_chunk_relations option).")
    parser.add_argument("--structural-techniques", action="store_true",
                        help="Tie the techniques of a mapping table or list to its group or software (layout).")
    parser.add_argument("--techniques", action="store_true",
                        help="ATT&CK techniques from described behaviour (behaviour_techniques option).")
    parser.add_argument("--technique-threshold", type=float, default=0.7)
    parser.add_argument("--technique-mode", default="recall", choices=("recall", "balanced", "precision"),
                        help="Which of the technique stage's techniques to keep (section 8.8).")
    parser.add_argument("--technique-check-views", type=int, nargs="+", default=[700],
                        help="The technique check's readings, each a definition length (technique_check_views).")
    parser.add_argument("--technique-choice", default="each", choices=("each", "family", "best"),
                        help="Which of one behaviour's checked techniques to keep (technique_choice).")
    parser.add_argument("--knowledge-focus", action="store_true",
                        help="Leave out objects no relationship touches, IOCs excepted (section 8.8).")
    parser.add_argument("--relevance", action="store_true",
                        help="Review each object's role in the report and leave out the names that only frame it (section 8.8).")
    parser.add_argument("--technique-linking", action="store_true",
                        help="Link techniques named in words ATT&CK does not use to their ATT&CK id (technique_linking option).")
    parser.add_argument("--activity-hub", action="store_true",
                        help="The report's unnamed main activity as an incident or a campaign (activity_hub option).")
    parser.add_argument("--stated-links", action="store_true",
                        help="Keep quoted links no verb expresses as related-to, and merge aliases the report declares (section 8.8).")
    parser.add_argument("--document-timeout", type=float, default=900,
                        help="Seconds one document may take before it is run again (reasoning models are slower).")
    parser.add_argument("--relations", default="core", choices=("core", "all"),
                        help="coarse profile: the relationship types scored (core: the first six).")
    parser.add_argument("--stated-only", action="store_true", help="Leave implied relationships out.")
    parser.add_argument("--knowledge", action="store_true", help="Link names to ATT&CK, as the tool does.")
    parser.add_argument("--verify", action="store_true", help="Verify relationships against their evidence.")
    parser.add_argument("--verify-score-only", action="store_true",
                        help="Verify without dropping: thresholds are then swept with --min-confidence.")
    parser.add_argument("--glean", action="store_true", help="A relationship recall pass per chunk.")
    parser.add_argument("--alias-split", action="store_true",
                        help="Read each predicted alias as its own entity (datasets annotating aliases apart).")
    parser.add_argument("--call-cache", default="",
                        help="Record each model answer in this folder and replay it when the same call comes again.")
    parser.add_argument("--relevance-web-addresses", action="store_true",
                        help="The relevance review also reads the domains and URLs the patterns found.")
    parser.add_argument("--technique-retrieval-k", type=int, default=0,
                        help="Techniques from behaviour also take the k ATT&CK techniques nearest each quote by embedding.")
    parser.add_argument("--embedding-model", default="text-embedding-3-large")
    parser.add_argument("--step-model", default="",
                        help="step=model,... : those steps on another model of the provider (e.g. relevance=claude-opus-5-5).")
    parser.add_argument("--min-confidence", type=int, default=0,
                        help="Rescore: leave out predicted relationships whose STIX confidence is below this.")
    args = parser.parse_args()
    if args.rescore:
        rescore(args)
    else:
        asyncio.run(run(args))


if __name__ == "__main__":
    main()
