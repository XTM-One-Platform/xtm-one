"""Score tool-run answer folders against a dataset ZIP with the checkout's STIX metrics (instance metrics, suite
16), then pair every variant with the baseline per entry (mean of the baseline's runs vs mean of the variant's
runs), with a 95% bootstrap interval (10,000 resamples, seed 7). usage (backend/): uv run --no-sync python scripts/stix_score_variants.py <dataset zip> <base dir>[,<base dir 2>] <variant dir>[,<variant dir 2>] ..."""
import json
import random
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.services.evaluation.extraction.pipeline import quality_with_extraction
from app.services.evaluation.metric_package.quality_adapter import build_quality_config

Q = ["stix.entity_prf.aligned", "stix.relationship_prf.aligned", "stix.entity_prf", "stix.relationship_prf"]
config = build_quality_config("STIX_BUNDLE", None, [])
zp = Path(sys.argv[1])
entries = json.loads(zipfile.ZipFile(zp).read("manifest.json"))["entries"]


def scores(dirs: str) -> dict[int, dict[str, float]]:
    out: dict[int, dict[str, list[float]]] = {}
    for d in dirs.split(","):
        for e in entries:
            f = Path(d) / zp.stem / f"{e['position']}.json"
            answer = json.loads(f.read_text()).get("answer", "") if f.exists() else ""
            res = quality_with_extraction(extractor=None, family="STIX_BUNDLE", reference=e["expected_answer"],
                                          agent_output=answer or "", execution_details=None, output_schema=None,
                                          config=config).metric_results
            for q in Q:
                m = res.get(q) or {}
                if m.get("status") == "scored":
                    out.setdefault(e["position"], {}).setdefault(q, []).append(m["value"])
    return {p: {q: sum(v) / len(v) for q, v in qs.items()} for p, qs in out.items()}


base = scores(sys.argv[2])
for variant in sys.argv[3:]:
    other = scores(variant)
    for q in Q:
        pairs = [(other[p][q], base[p][q]) for p in base if q in base[p] and q in other.get(p, {})]
        diffs = [a - b for a, b in pairs]
        rng = random.Random(7)  # noqa: S311 - a seeded bootstrap
        boot = sorted(sum(diffs[rng.randrange(len(diffs))] for _ in diffs) / len(diffs) for _ in range(10_000))
        mv, mb = sum(a for a, _ in pairs) / len(pairs), sum(b for _, b in pairs) / len(pairs)
        print(f"{zp.stem:20s} {Path(variant.split(',')[0]).name:12s} {q.replace('stix.', ''):24s} n={len(pairs):3d} "
              f"variant {mv:5.1f} base {mb:5.1f} diff {mv - mb:+5.1f} [{boot[249]:+5.1f}, {boot[9749]:+5.1f}]")
