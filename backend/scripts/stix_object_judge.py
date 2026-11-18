"""Development and spent data only: are the objects an extraction gives the report's knowledge? A judge of
another model family (GPT-6.1 Sol, max effort) reads each report and a shuffled, unlabelled list of objects
(type and name): a sample of the run's objects, the reference's objects (a positive control) and objects of
another report of the dataset (a negative control: not in this report). Each is answered
knowledge (named in the report and part of what it establishes about the threat: an actor, its tools and
malware, its techniques, its victims and targets, its infrastructure and indicators, a vulnerability exploited),
mention (named, but only in passing: a publisher, an author, a cited source, a product or company named
without a role) or absent (the report does not name it). Writes <out>/<zip stem>-<position>.json (resumes).
Answer folders as scripts/stix_score_variants.py reads them (<run dir>/<zip stem>/<position>.json).
usage (backend/, OPENAI_API_KEY): uv run --no-sync python scripts/stix_object_judge.py <run dir> <out dir> <dataset zip> [...]"""
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
from app.tools.base import FileContext

SAMPLE, CONTROLS, MAX_TEXT = 30, 6, 120_000
SKIPPED = {"relationship", "report", "marking-definition", "indicator", "identity-publisher"}
SYSTEM = """You check the objects a knowledge graph holds for a cyber threat intelligence report. For each
OBJECT (a STIX type and a name), decide from the REPORT alone:
- knowledge: the report names it and it is part of what the report establishes about the threat: an actor,
  its malware and tools, its techniques, its victims and targets (organizations, sectors, places), its
  infrastructure, an indicator value, a vulnerability exploited;
- mention: the report names it, but only in passing, with no role in the threat activity: the publisher, an
  author, a cited source, a product, platform or company named without a role, a page artefact;
- absent: the report does not name it (in any form or alias).
Judge the object, not its type's correctness. Answer JSON only:
{"verdicts": [{"object": "O1", "verdict": "knowledge" | "mention" | "absent"}, ...]} with one entry per object."""


def objects_of(bundle: dict) -> list[tuple[str, str]]:
    out = []
    for o in bundle.get("objects", []):
        kind = o.get("type")
        name = o.get("name") or o.get("value") or (next(iter((o.get("hashes") or {}).values()), None))
        if kind and name and kind not in SKIPPED:
            out.append((kind, str(name)))
    return list(dict.fromkeys(out))


async def main() -> None:
    run, out = Path(sys.argv[1]), Path(sys.argv[2])
    out.mkdir(parents=True, exist_ok=True)
    model = os.environ.get("JUDGE_MODEL", "gpt-6.1-sol")
    effort = normalize_openai_reasoning_effort("max", model) or "high"
    client, gate = openai.AsyncOpenAI(), asyncio.Semaphore(4)

    async def one(zp: Path, z: zipfile.ZipFile, files: dict, entries: list, e: dict) -> None:
        path = out / f"{zp.stem}-{e['position']}.json"
        if path.exists():
            return
        f = run / zp.stem / f"{e['position']}.json"
        answer = json.loads(f.read_text()).get("answer") or "" if f.exists() else ""
        predicted = objects_of(json.loads(answer)) if answer else []
        gold = objects_of(json.loads(e["expected_answer"]))
        rng = random.Random(f"{zp.stem}-{e['position']}")  # noqa: S311 - a reproducible sample, not a secret
        here = {name.casefold() for _, name in gold + predicted}
        others = [o for x in entries if x is not e for o in objects_of(json.loads(x["expected_answer"]))
                  if o[1].casefold() not in here and len(o[1]) > 3]
        listed = [("predicted", o) for o in rng.sample(predicted, min(SAMPLE, len(predicted)))]
        listed += [("gold", o) for o in rng.sample(gold, min(CONTROLS, len(gold)))]
        listed += [("foreign", o) for o in rng.sample(others, min(CONTROLS, len(others)))]
        if not listed:
            path.write_text(json.dumps({"rows": []}))
            return
        rng.shuffle(listed)
        fr = files[e["files"][0]]
        ctx = FileContext()
        ref = FileRef.from_bytes(z.read(fr["path"]), filename=fr["filename"], content_type=fr["content_type"])
        ctx.add(ref)
        text = (await ctx.read_text(ref.file_id) or "")[:MAX_TEXT]
        lines = "\n".join(f"O{i + 1}: {kind} | {name}" for i, (_, (kind, name)) in enumerate(listed))
        async with gate:
            response = await client.responses.create(model=model, reasoning={"effort": effort}, instructions=SYSTEM,
                                                     input=f"REPORT:\n{text}\n\nOBJECTS:\n{lines}")
        raw = response.output_text or "{}"
        verdicts = {v.get("object"): v.get("verdict") for v in json.loads(raw[raw.find("{"):raw.rfind("}") + 1]).get("verdicts", [])}
        rows = [{"who": w, "type": kind, "name": name, "verdict": verdicts.get(f"O{i + 1}")}
                for i, (w, (kind, name)) in enumerate(listed)]
        path.write_text(json.dumps({"rows": rows}))
        print(zp.stem, e["position"], len(rows), flush=True)

    jobs = []
    for zarg in sys.argv[3:]:
        zp = Path(zarg)
        z = zipfile.ZipFile(zp)
        m = json.loads(z.read("manifest.json"))
        files = {fr["id"]: fr for fr in m["files"]}
        jobs += [one(zp, z, files, m["entries"], e) for e in m["entries"]]
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
