"""Development and spent data only: are the relationships the metric scores as errors wrong, or true facts the gold does not
record? A judge of another model family (GPT-6.1 Sol, max effort) reads each report's text and a shuffled list
of triples, blind to their origin: a sample of the run's spurious triples (aligned metric), gold triples (a
positive control: should be supported) and gold triples with one endpoint swapped for another entity of the
same type that the gold never links that way (a negative control: should not be). Each triple is answered
stated / implied / not_supported. Writes <out>/<zip stem>-<position>.json (resumes) and prints the rates.
SAMPLE=all judges a sample of every predicted triple instead, each with its STIX confidence (precision by
confidence band); SAMPLE=implied every implied one (confidence below 85), each with the step that read it
(``x_xtm_origin``: precision by origin); ORIGIN=<origin> keeps, in either, the triples of that origin only.
Answer folders as scripts/stix_score_variants.py reads them (<run dir>/<zip stem>/<position>.json, {"answer"}).
usage (backend/, OPENAI_API_KEY): uv run --no-sync python scripts/stix_support_judge.py <run dir> <out dir> <dataset zip> [...]"""
import asyncio
import json
import os
import random
import sys
import zipfile
from pathlib import Path

import openai

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.llm.base import FileRef
from app.llm.openai_compat import normalize_openai_reasoning_effort
from app.services.evaluation.extraction.pipeline import quality_with_extraction
from app.services.evaluation.metric_package.quality_adapter import build_quality_config
from app.tools.base import FileContext

SPURIOUS, CONTROLS, MAX_TEXT = 25, 5, 120_000
SYSTEM = """You check knowledge-graph relationships against the cyber threat intelligence report they were
extracted from. For each TRIPLE (source, relationship type, target), decide from the REPORT alone:
- stated: the report says it (in any words): the same source and target entities, the relationship's
  meaning and direction;
- implied: the report does not say it in one place but a careful analyst would record it from what the
  report says (e.g. the group's malware runs the command, so the group uses the technique);
- not_supported: the report does not support it, the endpoints are wrong or reversed, or the relationship
  type misstates what the report says.
Relationship types follow STIX 2.1: uses, targets, attributed-to, indicates, based-on, located-at,
exploits, has, part-of, related-to, delivers, drops, communicates-with, originates-from, variant-of,
authored-by, compromises, hosts, owns, impersonates. Judge the fact, not its usefulness. Answer JSON only:
{"verdicts": [{"triple": "T1", "verdict": "stated" | "implied" | "not_supported"}, ...]} with one entry
per triple."""


def display(o: dict) -> str:
    return str(o.get("name") or o.get("value") or o.get("pattern") or o.get("id"))


def gold_triples(bundle: dict) -> tuple[list[tuple[str, str, str]], dict[str, list[str]], set]:
    by_id = {o["id"]: o for o in bundle.get("objects", [])}
    out, by_type = [], {}
    for o in by_id.values():
        if o["type"] not in ("relationship", "report"):
            by_type.setdefault(o["type"], []).append(display(o))
    for r in by_id.values():
        if r["type"] == "relationship" and r.get("source_ref") in by_id and r.get("target_ref") in by_id:
            s, t = by_id[r["source_ref"]], by_id[r["target_ref"]]
            out.append((display(s), r["relationship_type"], display(t), t["type"]))
    return out, by_type, {(a, k, b) for a, k, b, _ in out}


async def main() -> None:
    run, out = Path(sys.argv[1]), Path(sys.argv[2])
    out.mkdir(parents=True, exist_ok=True)
    model = os.environ.get("JUDGE_MODEL", "gpt-6.1-sol")
    effort = normalize_openai_reasoning_effort("max", model) or "high"
    client, gate = openai.AsyncOpenAI(), asyncio.Semaphore(4)

    async def one(zp: Path, z: zipfile.ZipFile, files: dict, cfg, e: dict) -> None:
        path = out / f"{zp.stem}-{e['position']}.json"
        if path.exists():
            return
        f = Path(run) / zp.stem / f"{e['position']}.json"
        answer = json.loads(f.read_text()).get("answer") or "" if f.exists() else ""
        res = quality_with_extraction(extractor=None, family="STIX_BUNDLE", reference=e["expected_answer"],
                                      agent_output=answer, execution_details=None, output_schema=None,
                                      config=cfg).metric_results.get("stix.relationship_prf.aligned") or {}
        spurious = list((res.get("detail") or {}).get("spurious") or [])
        confidence: dict[str, object] = {}
        origin: dict[str, object] = {}
        mode = os.environ.get("SAMPLE", "")
        if mode in ("all", "implied") and answer:
            objs = json.loads(answer).get("objects", [])
            by = {o.get("id"): o for o in objs}
            spurious = []
            for r in objs:
                if r.get("type") == "relationship" and r.get("source_ref") in by and r.get("target_ref") in by:
                    label = f"{display(by[r['source_ref']])} {r['relationship_type']} {display(by[r['target_ref']])}"
                    wanted = os.environ.get("ORIGIN")
                    if wanted and r.get("x_xtm_origin") != wanted:
                        continue
                    if r["relationship_type"] != "based-on" and (mode == "all" or (r.get("confidence") or 100) < 85):
                        spurious.append(label)
                        confidence[label] = r.get("confidence")
                        origin[label] = r.get("x_xtm_origin")
        gold, by_type, gold_set = gold_triples(json.loads(e["expected_answer"]))
        rng = random.Random(f"{zp.stem}-{e['position']}")  # noqa: S311 - a reproducible sample, not a secret
        who = "predicted" if mode in ("all", "implied") else "spurious"
        listed = [(who, s) for s in rng.sample(spurious, min(SPURIOUS if mode != "implied" else 60, len(spurious)))]
        for a, k, b, _ in rng.sample(gold, min(CONTROLS, len(gold))):
            listed.append(("gold", f"{a} {k} {b}"))
        for a, k, b, ttype in rng.sample(gold, len(gold)):
            others = [x for x in by_type.get(ttype, []) if x != b and (a, k, x) not in gold_set]
            if others and sum(1 for w, _ in listed if w == "swapped") < CONTROLS:
                listed.append(("swapped", f"{a} {k} {rng.choice(others)}"))
        if not listed:
            path.write_text(json.dumps({"rows": []}))
            return
        rng.shuffle(listed)
        ctx = FileContext()
        fr = files[e["files"][0]]
        ref = FileRef.from_bytes(z.read(fr["path"]), filename=fr["filename"], content_type=fr["content_type"])
        ctx.add(ref)
        text = (await ctx.read_text(ref.file_id) or "")[:MAX_TEXT]
        lines = "\n".join(f"T{i + 1}: {t}" for i, (_, t) in enumerate(listed))
        async with gate:
            response = await client.responses.create(model=model, reasoning={"effort": effort}, instructions=SYSTEM,
                                                     input=f"REPORT:\n{text}\n\nTRIPLES:\n{lines}")
        raw = response.output_text or "{}"
        verdicts = {v.get("triple"): v.get("verdict") for v in json.loads(raw[raw.find("{"):raw.rfind("}") + 1]).get("verdicts", [])}
        rows = [{"who": w, "triple": t, "verdict": verdicts.get(f"T{i + 1}"), "confidence": confidence.get(t),
                 "origin": origin.get(t)}
                for i, (w, t) in enumerate(listed)]
        path.write_text(json.dumps({"rows": rows}))
        print(zp.stem, e["position"], len(rows), flush=True)

    jobs = []
    for zarg in sys.argv[3:]:
        zp = Path(zarg)
        z = zipfile.ZipFile(zp)
        m = json.loads(z.read("manifest.json"))
        cfg = build_quality_config("STIX_BUNDLE", None, [e["expected_answer"] for e in m["entries"]])
        files = {fr["id"]: fr for fr in m["files"]}
        jobs += [one(zp, z, files, cfg, e) for e in m["entries"]]
    await asyncio.gather(*jobs)
    tally: dict[str, dict[str, int]] = {}
    for p in out.glob("*.json"):
        for r in json.loads(p.read_text()).get("rows", []):
            tally.setdefault(r["who"], {}).setdefault(str(r["verdict"]), 0)
            tally[r["who"]][str(r["verdict"])] += 1
    for who, c in sorted(tally.items()):
        n = sum(c.values())
        print(f"{who:9s} n={n:4d} " + " ".join(f"{k} {100 * v / n:5.1f}%" for k, v in sorted(c.items())))

asyncio.run(main())
