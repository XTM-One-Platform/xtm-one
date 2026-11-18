"""A second relationship matcher (development and spent data): a judge of another model family (GPT-6.1 Sol,
max effort) reads each report's gold relationships and, blind to which system wrote them, every predicted
triple of the evaluation's runs mixed and shuffled, plus a negative control (gold facts with one endpoint
swapped for another entity of the same type: false). For each listed triple it names the gold relationship
stating the same fact (same real-world endpoints, aliases allowed, same meaning and direction), or none.
Per run: precision = matched predicted / predicted, recall = distinct gold matched / gold, macro over the
entries with gold relationships; the control's acceptance rate is reported (a judge accepting them is not
to be trusted). Writes <out>/<position>.json (resumes).
usage (backend/, OPENAI_API_KEY): uv run --no-sync python scripts/stix_relation_judge.py <export.zip> <out dir>"""
import asyncio
import io
import json
import os
import random
import sys
import zipfile
from pathlib import Path

import openai

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.llm.openai_compat import normalize_openai_reasoning_effort

SYSTEM = """You compare knowledge-graph relationships extracted from a cyber threat intelligence report
with the relationships a human analyst recorded for the same report. For each CANDIDATE triple, decide
which GOLD relationship states the same fact: the same real-world source and target (a name, an alias, an
abbreviation or a spelling variant of the same entity counts; a different entity does not, even of the same
kind), and a relationship of the same meaning and direction (synonymous verbs count, e.g. "uses" for
"delivers" only if the gold fact is the same; reversed roles do not). If no gold relationship states that
fact, answer null. Several candidates may match the same gold relationship. Judge only from the triples;
do not reward plausibility. Answer JSON only: {"matches": [{"candidate": "C1", "gold": "G3" or null}, ...]}
with one entry per candidate."""


def triples(bundle: dict) -> list[tuple[str, str, str, str, str]]:
    by_id = {o.get("id"): o for o in bundle.get("objects", [])}
    out = []
    for r in bundle.get("objects", []):
        if r.get("type") != "relationship":
            continue
        s, t = by_id.get(r.get("source_ref")), by_id.get(r.get("target_ref"))
        if not s or not t:
            continue
        name = lambda o: str(o.get("name") or o.get("value") or o.get("pattern") or o.get("id"))[:120]  # noqa: E731
        out.append((s.get("type", ""), name(s), str(r.get("relationship_type")), t.get("type", ""), name(t)))
    return list(dict.fromkeys(out))


async def main() -> None:
    export, out = zipfile.ZipFile(sys.argv[1]), Path(sys.argv[2])
    out.mkdir(parents=True, exist_ok=True)
    d = json.loads(export.read("evaluation.json"))
    ds = zipfile.ZipFile(io.BytesIO(export.read("dependencies/datasets/dataset-0.zip")))
    entries = json.loads(ds.read("manifest.json"))["entries"]
    runs = {}
    for rd in d["evaluation_runs"]:
        name = json.loads(export.read(f"dependencies/agents/agent-{rd['agent_index']}.json"))["agent"]["name"]
        runs[name] = {r["entry_ref"]["entry_position"]: r.get("agent_output") or ""
                      for r in json.loads(export.read(rd["results_path"]))["results"]}
    model = os.environ.get("JUDGE_MODEL", "gpt-6.1-sol")
    effort = normalize_openai_reasoning_effort("max", model) or "high"
    client = openai.AsyncOpenAI()
    gate = asyncio.Semaphore(4)

    async def one(entry: dict) -> None:
        path = out / f"{entry['position']}.json"
        if path.exists():
            return
        gold = triples(json.loads(entry["expected_answer"]))
        if not gold:
            path.write_text(json.dumps({"gold": 0}))
            return
        rng = random.Random(entry["position"])  # noqa: S311 - a seeded control
        listed: list[tuple[str, tuple]] = []
        for name, answers in runs.items():
            raw = answers.get(entry["position"]) or ""
            try:
                predicted = triples(json.loads(raw)) if raw else []
            except ValueError:
                predicted = []
            listed += [(name, t) for t in predicted]
        # Negative control: a gold fact with its target swapped for another gold entity of the same type.
        targets = {}
        for g in gold:
            targets.setdefault(g[3], set()).add(g[4])
        controls = []
        for g in rng.sample(gold, min(len(gold), max(2, len(gold) // 5))):
            others = sorted(targets.get(g[3], set()) - {g[4]} - {x[4] for x in gold if x[:3] == g[:3]})
            if others:
                controls.append(("__control__", (g[0], g[1], g[2], g[3], rng.choice(others))))
        listed += controls
        rng.shuffle(listed)
        gold_lines = "\n".join(f"G{i + 1}: [{s[0]}] {s[1]} --{s[2]}--> [{s[3]}] {s[4]}" for i, s in enumerate(gold))
        cand_lines = "\n".join(f"C{i + 1}: [{t[0]}] {t[1]} --{t[2]}--> [{t[3]}] {t[4]}" for i, (_, t) in enumerate(listed))
        async with gate:
            response = await client.responses.create(
                model=model, reasoning={"effort": effort}, instructions=SYSTEM,
                input=f"GOLD:\n{gold_lines}\n\nCANDIDATES:\n{cand_lines}")
        raw = response.output_text or "{}"
        matches = {m.get("candidate"): m.get("gold") for m in json.loads(raw[raw.find("{"):raw.rfind("}") + 1]).get("matches", [])}
        rows = [{"who": who, "triple": list(t), "gold": matches.get(f"C{i + 1}")} for i, (who, t) in enumerate(listed)]
        path.write_text(json.dumps({"gold": len(gold), "rows": rows}))
        print(entry["position"], len(gold), "gold", len(listed), "listed", flush=True)

    await asyncio.gather(*(one(e) for e in entries))
    # Scores
    per = {name: [] for name in runs}
    control = [0, 0]
    for e in entries:
        rec = json.loads((out / f"{e['position']}.json").read_text())
        if not rec.get("gold"):
            continue
        for row in rec["rows"]:
            if row["who"] == "__control__":
                control[0] += 1
                control[1] += bool(row["gold"])
        for name in runs:
            mine = [r for r in rec["rows"] if r["who"] == name]
            matched = [r for r in mine if r["gold"]]
            p = len(matched) / len(mine) if mine else 0.0
            rr = len({r["gold"] for r in matched}) / rec["gold"]
            per[name].append((p, rr, 2 * p * rr / (p + rr) if p + rr else 0.0))
    for name, rows in per.items():
        n = len(rows)
        print(f"{Path(sys.argv[1]).stem:20s} {name[:30]:30s} n={n} judge-matched relationships macro "
              f"P {100 * sum(r[0] for r in rows) / n:5.1f} R {100 * sum(r[1] for r in rows) / n:5.1f} "
              f"F1 {100 * sum(r[2] for r in rows) / n:5.1f}")
    print(f"control triples accepted: {control[1]}/{control[0]}")

asyncio.run(main())
