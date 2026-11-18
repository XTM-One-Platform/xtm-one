"""The extraction's ATT&CK techniques on the SoK split, end to end and reproducible.

The SoK split (Büchel et al., "SoK: Automated TTP Extraction from CTI Reports", USENIX Security
2025; Zenodo record 16753555, CC-BY-SA-4.0) holds MITRE TRAM2's and Bosch AnnoCTR's train and
test documents with their ATT&CK labels, on which ARBITRA (arXiv 2610.39433) reports frontier and
fine-tuned systems. Nothing of the data is in this repository: ``fetch`` downloads it and checks
its digest. Each step writes files the next reads, so a result can be rebuilt from the command
lines alone (dev-docs/stix-grounded-extraction.md, FROZEN-SoK):

  fetch    <dir>                          datasets.zip from Zenodo, digest-checked, unpacked
  datasets <dir>                          filigran-dataset-v1 ZIPs: the test splits and the
                                          dev samples (30 TRAM2 train, 30 AnnoCTR devtrain
                                          documents, seed 11), each report attached as text,
                                          its gold techniques as a STIX bundle of
                                          attack-patterns (importable in an XTM One instance)
  run      <model> <dataset zip> <out>    the extraction as the tool ships it, OPTS (JSON)
                                          overriding its options; <out>/<position>.json
  score    <tram2|annoctr> <dataset zip> <out>
                                          ARBITRA's protocol: TRAM2 exact ids, AnnoCTR parent
                                          ids; macro P / R / F1, 95% bootstrap interval
  labels   <train dataset zip> <file>     its gold's label space; LABELS=<file> makes run
                                          classify into it and score keep only answers in it
                                          (a closed-set task: TRAM2's 50 (sub-)techniques), an id
                                          ATT&CK revoked since answered by its replacement, a
                                          sub-technique outside them by its parent label
                                          (ATTACK=<the ATT&CK 20.1 file fetch wrote>)
  closed   <answers dir> <out dir>         a run's answers restricted to the label space (LABELS,
                                          ATTACK), for an instance's metrics
  answers  <evaluation export> <dir>       an exported evaluation's completed runs as answers
                                          folders (<dir>/<model>), for score
  judge    <dataset zip> <out> <judged dir> <n reports>
                                          a blind judge of another model family on every
                                          predicted technique, in the gold or not (train or
                                          spent data only)
  package  <evaluation export> <dataset zip> <label>=<model>=<out> ... <package>
                                          an evaluation exported from an XTM One instance with
                                          one more run per answers folder, scored by the
                                          instance's own STIX metrics, ready to import

Run from backend/ with its venv; ``run`` needs the provider key in the environment.
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import io
import json
import os
import random
import re
import sys
import time
import urllib.request
import uuid
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ZENODO = "https://zenodo.org/api/records/16753555/files/datasets.zip/content"
ZENODO_SHA256 = "ba93bcf8b87d38944589829e522587ae8c2877f7748a474d95bf935670ad9731"
#: ATT&CK 20.1 (MITRE's STIX data), for its revoked-by relationships: a label the benchmark
#: annotated under an id ATT&CK later revoked is answered by the id that replaced it.
ATTACK = ("https://raw.githubusercontent.com/mitre-attack/attack-stix-data/master/enterprise-attack/"
          "enterprise-attack-20.1.json")
ATTACK_SHA256 = "539a843a7b53ed9749740b2123dddae3726b1a4831841c32aca740b011b910b5"
TID = re.compile(r"^T\d{4}(?:\.\d{3})?$")
#: (dataset, split file under datasets/, text of a document, gold technique ids of a document)
SPLITS = {
    "tram2-test": ("MITRE TRAM2/test_split.json", None),
    "annoctr-test": ("Bosch AnnoCTR/bosch_cti_test_ds.json", None),
    "tram2-dev": ("MITRE TRAM2/train_split.json", 30),
    "annoctr-dev": ("Bosch AnnoCTR/bosch_cti_devtrain_ds.json", 30),
    "tram2-train": ("MITRE TRAM2/train_split.json", None),
    # A second development sample: 40 other train documents (not in tram2-dev), seed 12.
    "tram2-dev2": ("MITRE TRAM2/train_split.json", 40),
    "annoctr-dev2": ("Bosch AnnoCTR/bosch_cti_devtrain_ds.json", 40),
}
PROMPT = "Extract the report as a STIX 2.1 bundle."
_NAMESPACE = uuid.UUID("6d0f8f2c-9a3f-4c55-9d64-2f1b8a6f0c11")


def _listed(value: object) -> list:
    """A label field as the split holds it: a list, or its Python text."""
    return value if isinstance(value, list) else ast.literal_eval(str(value))


def _text(document: dict) -> str:
    """A document's text: TRAM2 holds sentences (joined by line breaks), AnnoCTR the text."""
    report = document["cti_report"]
    return report if isinstance(report, str) else "\n".join(s.strip() for s in report)


def _gold(document: dict) -> list[str]:
    """A document's technique ids as annotated: TRAM2 the union of its sentences' labels,
    AnnoCTR the technique ids of its mitre_ids (not its tactics, groups or software)."""
    if "label" in document:
        found = {t for sentence in _listed(document["label"]) for t in sentence if TID.match(t)}
    else:
        found = {t for t in _listed(document["mitre_ids"]) if TID.match(t)}
    return sorted(found)


def _label_space() -> set[str] | None:
    """The benchmark's label space (``LABELS``, a file ``labels`` wrote), when the task is a
    closed set: TRAM2 classifies into the 50 (sub-)techniques of its training split."""
    path = os.environ.get("LABELS")
    return set(json.loads(Path(path).read_text())) if path else None


def _revoked_into(space: set[str]) -> dict[str, str]:
    """For a closed-set task: each id that replaced a label ATT&CK revoked since the annotation
    (revoked-by in ATT&CK 20.1, ``ATTACK`` the file ``fetch`` wrote), mapped back to that label."""
    bundle = json.loads(Path(os.environ["ATTACK"]).read_text())
    ids = {o["id"]: r["external_id"] for o in bundle["objects"] if o.get("type") == "attack-pattern"
           for r in o.get("external_references", []) if r.get("source_name") == "mitre-attack"}
    return {ids[o["target_ref"]]: ids[o["source_ref"]] for o in bundle["objects"]
            if o.get("type") == "relationship" and o.get("relationship_type") == "revoked-by"
            and ids.get(o["source_ref"]) in space and o["target_ref"] in ids}


def _in_space(attack_id: str, space: set[str], revoked: dict[str, str]) -> str:
    """An answer read into a closed label space: an id ATT&CK revoked since the annotation as
    the label it replaced; a sub-technique the space does not hold as its parent technique when
    the space holds that (a sub-technique is a case of its technique, ATT&CK's hierarchy)."""
    attack_id = revoked.get(attack_id, attack_id)
    if attack_id not in space and attack_id.split(".")[0] in space:
        return attack_id.split(".")[0]
    return attack_id


def labels(dataset_zip: Path, target: Path) -> None:
    """The label space of a dataset ZIP's gold (a TRAIN split's, never a test's), for LABELS."""
    _, entries, _ = _entries(dataset_zip)
    space = sorted({o["x_mitre_id"] for e in entries for o in json.loads(e["expected_answer"])["objects"]})
    target.write_text(json.dumps(space))
    print(target, len(space), "techniques")


def fetch(folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    archive = folder / "datasets.zip"
    if not archive.exists():
        urllib.request.urlretrieve(ZENODO, archive)  # noqa: S310 - fixed https URL
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    if digest != ZENODO_SHA256:
        sys.exit(f"datasets.zip digest {digest} is not the registered {ZENODO_SHA256}")
    with zipfile.ZipFile(archive) as z:
        for member in z.namelist():
            target = (folder / member).resolve()
            if not target.is_relative_to(folder.resolve()):
                sys.exit(f"refusing member outside {folder}: {member}")
        z.extractall(folder)
    attack = folder / "enterprise-attack-20.1.json"
    if not attack.exists():
        urllib.request.urlretrieve(ATTACK, attack)  # noqa: S310 - fixed https URL
    digest = hashlib.sha256(attack.read_bytes()).hexdigest()
    if digest != ATTACK_SHA256:
        sys.exit(f"{attack.name} digest {digest} is not the registered {ATTACK_SHA256}")
    print("fetched and checked", archive, attack)


def _split_file(folder: Path, relative: str) -> Path:
    found = sorted(folder.rglob(Path(relative).name))
    found = [p for p in found if p.as_posix().endswith(relative)]
    if len(found) != 1:
        sys.exit(f"{relative}: {len(found)} matches under {folder}")
    return found[0]


async def _technique_names() -> dict[str, str]:
    from app.services.stix_extraction.knowledge import attack_catalogue

    catalogue = await attack_catalogue() or []
    return {t.attack_id.upper(): t.name for t in catalogue}


def datasets(folder: Path) -> None:
    names = asyncio.run(_technique_names())
    for name, (relative, sample) in SPLITS.items():
        source = _split_file(folder, relative)
        documents = json.loads(source.read_text())
        if sample:
            labelled = [d for d in documents if _gold(d)]
            first = random.Random(11).sample(labelled, 30)  # noqa: S311 - a seeded sample
            if name.endswith("-dev2"):
                documents = random.Random(12).sample([d for d in labelled if d not in first], sample)  # noqa: S311
            else:
                documents = first
        entries, files, blobs = [], [], {}
        for position, document in enumerate(documents):
            data = _text(document).encode("utf-8")
            fid, filename = f"f{position}", f"report-{position}.txt"
            blobs[f"files/{fid}_{filename}"] = data
            files.append({"id": fid, "filename": filename, "content_type": "text/plain", "size": len(data),
                          "sha256": hashlib.sha256(data).hexdigest(), "path": f"files/{fid}_{filename}"})
            gold = [
                {"type": "attack-pattern", "spec_version": "2.1", "id": f"attack-pattern--{uuid.uuid5(_NAMESPACE, t)}",
                 "created": "2025-08-06T00:00:00.000Z", "modified": "2025-08-06T00:00:00.000Z",
                 "name": names.get(t, t), "x_mitre_id": t,
                 "external_references": [{"source_name": "mitre-attack", "external_id": t}]}
                for t in _gold(document)
            ]
            bundle = {"type": "bundle", "id": f"bundle--{uuid.uuid5(_NAMESPACE, f'{name}/{position}')}", "objects": gold}
            entries.append({"position": position, "input_prompt": PROMPT, "expected_answer": json.dumps(bundle),
                            "description": f"{name} {position}: {len(gold)} gold techniques", "files": [fid]})
        title = {"tram2-test": "SoK TRAM2 test (31 reports, MITRE)", "annoctr-test": "SoK AnnoCTR test (34 reports, Bosch)",
                 "tram2-dev": "SoK TRAM2 dev (30 train reports, MITRE)",
                 "annoctr-dev": "SoK AnnoCTR dev (30 devtrain reports, Bosch)",
                 "tram2-train": "SoK TRAM2 train (120 reports, MITRE)",
                 "tram2-dev2": "SoK TRAM2 dev 2 (40 other train reports, MITRE)",
                 "annoctr-dev2": "SoK AnnoCTR dev 2 (40 other devtrain reports, Bosch)"}[name]
        manifest = {
            "format": "filigran-dataset-v1", "version": 2,
            "dataset": {"name": f"STIX extraction - {title}", "family": "STIX_BUNDLE", "description": (
                "ATT&CK techniques of the SoK split (Büchel et al., USENIX Security 2025; Zenodo 16753555, "
                "CC-BY-SA-4.0). Reference: the document's annotated technique ids as attack-patterns, nothing "
                "else: read the attack-pattern row of stix.entity_prf.by_type. The published protocol (TRAM2 exact "
                "ids, AnnoCTR parent ids, macro over documents) is scripts/stix_sok_benchmark.py score.")},
            "entries": entries, "files": files,
        }
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
            # A fixed member time: the same data gives the same ZIP, digest included.
            for path, data in {"manifest.json": json.dumps(manifest, ensure_ascii=False).encode(), **blobs}.items():
                z.writestr(zipfile.ZipInfo(path, date_time=(2025, 8, 6, 0, 0, 0)), data, zipfile.ZIP_DEFLATED)
        out = folder / f"{name}.zip"
        out.write_bytes(buffer.getvalue())
        print(out, len(entries), "documents", hashlib.sha256(buffer.getvalue()).hexdigest()[:16])


def _entries(dataset_zip: Path) -> tuple[zipfile.ZipFile, list[dict], dict[str, dict]]:
    z = zipfile.ZipFile(dataset_zip)
    manifest = json.loads(z.read("manifest.json"))
    return z, manifest["entries"], {f["id"]: f for f in manifest.get("files", [])}


def run(model: str, dataset_zip: Path, out: Path) -> None:
    """The extraction tool's pipeline with its shipped options (``OPTS`` overriding), one call
    cache per output folder's parent (``<out>/../cache``) so variants share their common calls."""
    from app.services.builtin_tools.stix_extraction import shipped_options
    from app.services.stix_extraction import ExtractionOptions, Usage, extract_stix, provider_structured_call
    from app.services.stix_extraction.knowledge import attack_catalogue, attack_knowledge
    from scripts.stix_extraction_benchmark import replayed

    if model.startswith("claude"):
        from app.llm.anthropic_provider import AnthropicProvider

        llm = AnthropicProvider(os.environ["ANTHROPIC_API_KEY"])
    else:
        from app.llm.openai_provider import OpenAIProvider

        llm = OpenAIProvider(os.environ["OPENAI_API_KEY"])
    overrides = json.loads(os.environ.get("OPTS", "{}"))
    space = _label_space()

    async def catalogue() -> list | None:
        """ATT&CK's live techniques, only the label space's for a closed-set task."""
        found = await attack_catalogue()
        return found if space is None or found is None else [t for t in found if t.attack_id.upper() in space]

    options = ExtractionOptions(**{**shipped_options(), **overrides})
    z, entries, files = _entries(dataset_zip)
    out.mkdir(parents=True, exist_ok=True)
    (out / "run.json").write_text(json.dumps({"model": model, "options_overrides": overrides,
                                              "label_space": sorted(space) if space else None,
                                              "dataset_sha256": hashlib.sha256(dataset_zip.read_bytes()).hexdigest()}))
    cache = out.parent / "cache"
    gate = asyncio.Semaphore(4)

    async def one(entry: dict) -> None:
        path = out / f"{entry['position']}.json"
        if path.exists():
            return
        text = z.read(files[entry["files"][0]]["path"]).decode("utf-8")
        async with gate:
            usage = Usage()
            call = replayed(provider_structured_call(llm, model, usage), cache, model)
            started = time.monotonic()
            try:
                result = await extract_stix(text, call, usage=usage, document_name=f"report-{entry['position']}",
                                            options=options, knowledge=attack_knowledge, techniques=catalogue,
                                            read_title=True)
                record = {"answer": json.dumps(result.bundle), "seconds": round(time.monotonic() - started, 1),
                          # Every technique proposal with its quote and verdict, and the run's counts:
                          # where a technique was lost (error analysis on train data).
                          "technique_scores": result.stages.get("technique_scores", []),
                          "stats": {k: v for k, v in result.stats.items() if isinstance(v, int | float | str)}}
            except Exception as exc:  # recorded as the entry's failure, scored as no answer
                record = {"answer": "", "error": f"{type(exc).__name__}: {exc}"}
            path.write_text(json.dumps(record))
            print(out.name, entry["position"], flush=True)

    async def main() -> None:
        await asyncio.gather(*(one(e) for e in entries))

    asyncio.run(main())


def _predicted(answer: str, parent: bool) -> set[str]:
    found = set()
    try:
        objects = json.loads(answer).get("objects", []) if answer else []
    except (ValueError, AttributeError):
        objects = []
    for o in objects:
        if o.get("type") != "attack-pattern":
            continue
        ids = [o.get("x_mitre_id"), *(r.get("external_id") for r in o.get("external_references") or []
                                      if r.get("source_name") == "mitre-attack")]
        for t in ids:
            t = str(t or "").upper()
            if TID.match(t):
                found.add(t.split(".")[0] if parent else t)
    return found


def score(task: str, dataset_zip: Path, out: Path) -> None:
    parent = task == "annoctr"
    space = _label_space()
    revoked = _revoked_into(space) if space is not None else {}
    _, entries, _ = _entries(dataset_zip)
    rows = []
    for entry in entries:
        gold = {o["x_mitre_id"] for o in json.loads(entry["expected_answer"])["objects"]}
        gold = {t.split(".")[0] for t in gold} if parent else gold
        if not gold:
            continue
        path = out / f"{entry['position']}.json"
        answer = json.loads(path.read_text()).get("answer", "") if path.exists() else ""
        predicted = _predicted(answer, False)
        if space is not None:  # a closed-set task: what lies outside its labels is not an answer to it
            predicted = {_in_space(t, space, revoked) for t in predicted} & space
        predicted = {t.split(".")[0] for t in predicted} if parent else predicted
        tp = len(gold & predicted)
        precision = tp / len(predicted) if predicted else 0.0
        recall = tp / len(gold)
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        rows.append((precision, recall, f1))
    mean = lambda values: sum(values) / len(values)  # noqa: E731
    rng = random.Random(7)  # noqa: S311 - a seeded bootstrap
    boot = sorted(mean([rows[rng.randrange(len(rows))][2] for _ in rows]) for _ in range(10_000))
    p, r, f = (mean([row[j] for row in rows]) for j in range(3))
    print(f"{task}{' (label space)' if space else ''}: {len(rows)} documents with gold techniques; macro P {p:.3f} R {r:.3f} F1 {f:.3f} "
          f"[95% {boot[249]:.3f}, {boot[9749]:.3f}]")


def package(export: Path, dataset_zip: Path, runs: list[str], target: Path) -> None:
    """*export* (an evaluation exported from an instance, on *dataset_zip* as its only dataset)
    with one run per ``label=model=answers dir``, each answer scored by the instance's own
    STIX metrics (what an import stores), the run's agent a copy of the export's first agent
    renamed to the label."""
    from app.services.evaluation.extraction.pipeline import quality_with_extraction
    from app.services.evaluation.metric_package.quality_adapter import build_quality_config

    source = zipfile.ZipFile(export)
    definition = json.loads(source.read("evaluation.json"))
    _, entries, _ = _entries(dataset_zip)
    config = build_quality_config("STIX_BUNDLE", None, [])
    members = {name: source.read(name) for name in source.namelist() if name != "evaluation.json"}
    # Only the export's completed runs: a run cut by its deadline is not a system's result.
    for stale in [r for r in definition["evaluation_runs"] if r["status"] != "completed"]:
        members.pop(stale["results_path"], None)
    definition["evaluation_runs"] = [r for r in definition["evaluation_runs"] if r["status"] == "completed"]
    for index, kept in enumerate(definition["evaluation_runs"]):
        data = json.loads(members.pop(kept["results_path"]))
        kept["index"], kept["results_path"] = index, f"evaluation_runs/run-{index}.json"
        members[kept["results_path"]] = json.dumps({**data, "run_index": index}).encode()
    # An evaluation exported before any run (a development sample's) takes a run's
    # bookkeeping from scratch; otherwise from its first completed run.
    now = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
    template_run = definition["evaluation_runs"][0] if definition["evaluation_runs"] else {
        "is_default_model": False, "status": "completed", "trigger_reason": "manual", "execution_mode": "full_loop",
        "apply_prompt_guards": False, "require_tool_approval": False, "persona_version_number": 1,
        "prompt_versions": None, "agent_config_versions": {}, "skill_versions": None,
        "metric_suite_version": "15.0.0", "scorer_selection": [], "speed_score": None,
        "started_at": now, "completed_at": now, "created_at": now}
    agent_template = json.loads(source.read(definition["dependencies"]["agents"][0]["path"]))
    for spec in runs:
        label, model, answers = spec.split("=", 2)
        agent_index = len(definition["dependencies"]["agents"])
        agent = json.loads(json.dumps(agent_template))
        agent["agent"]["name"] = label
        agent_path = f"dependencies/agents/agent-{agent_index}.json"
        members[agent_path] = json.dumps(agent).encode()
        definition["dependencies"]["agents"].append({"index": agent_index, "path": agent_path, "tags": None,
                                                     "is_baseline": False, "output_extractor": None})
        run_index = len(definition["evaluation_runs"])
        results, seconds = [], 0.0
        for entry in entries:
            path = Path(answers) / f"{entry['position']}.json"
            record = json.loads(path.read_text()) if path.exists() else {"answer": ""}
            scored = quality_with_extraction(extractor=None, family="STIX_BUNDLE", reference=entry["expected_answer"],
                                             agent_output=record.get("answer") or "", execution_details=None,
                                             output_schema=None, config=config)
            duration = float(record.get("seconds") or 0.0)
            seconds += duration
            results.append({
                "entry_ref": {"dataset_index": 0, "entry_position": entry["position"]},
                "agent_output": record.get("answer") or "", "score": 0.0, "sub_scores": None,
                "evaluator_reasoning": f"Imported: {label}, run outside this instance by "
                                       "scripts/stix_sok_benchmark.py on the same dataset.",
                "duration_seconds": duration, "speed_score": None, "overall_score": None, "f1_score": None,
                "is_judge_failure": False, "execution_anomaly_kind": None, "is_reused": False, "trace_ids": [],
                "execution_details": {"total_duration_ms": int(duration * 1000), "agent_duration_ms": int(duration * 1000),
                                      "execution_mode": "full_loop", "metric_results": scored.metric_results},
                "created_at": template_run["completed_at"],
            })
        results_path = f"evaluation_runs/run-{run_index}.json"
        members[results_path] = json.dumps({"version": 1, "run_index": run_index, "results": results}).encode()
        definition["evaluation_runs"].append({**template_run, "index": run_index, "results_path": results_path,
                                              "agent_index": agent_index, "model_id": model, "duration_seconds": seconds,
                                              "completed_entries": len(results), "total_entries": len(results),
                                              "error": None, "score": None, "overall_score": None, "f1_score": None})
    labels = ", ".join(spec.split("=", 1)[0] for spec in runs)
    definition["evaluation"]["name"] += " (with imported runs)"
    definition["evaluation"]["description"] += f" Imported runs: {labels}."
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("evaluation.json", json.dumps(definition, ensure_ascii=False))
        for name, data in members.items():
            z.writestr(name, data)
    print(target, len(definition["evaluation_runs"]), "runs")


def answers(export: Path, folder: Path) -> None:
    """Each completed run of an evaluation exported from an instance, its answers written as
    <folder>/<model>/<position>.json, the layout ``score`` reads (one dataset per evaluation); with
    several agents, <folder>/<agent name>--<model> (the name folded to a slug: an export carries
    no slug), so that two agents on one model stay apart."""
    source = zipfile.ZipFile(export)
    definition = json.loads(source.read("evaluation.json"))
    completed = [run for run in definition["evaluation_runs"] if run["status"] == "completed"]
    several = len({run["agent_index"] for run in completed}) > 1
    for run_definition in completed:
        name = run_definition["model_id"]
        if several:
            agent = json.loads(source.read(f"dependencies/agents/agent-{run_definition['agent_index']}.json"))["agent"]
            name = f"{re.sub(r'[^a-z0-9]+', '-', agent['name'].lower()).strip('-')}--{name}"
        out = folder / name
        out.mkdir(parents=True, exist_ok=True)
        results = json.loads(source.read(run_definition["results_path"]))["results"]
        for result in results:
            position = result["entry_ref"]["entry_position"]
            (out / f"{position}.json").write_text(json.dumps({"answer": result.get("agent_output") or "",
                                                              "seconds": result.get("duration_seconds")}))
        print(out, len(results), "answers")


def closed(source: Path, target: Path) -> None:
    """A run's answers as the closed-set task reads them (``LABELS``, ``ATTACK``): each
    attack-pattern ATT&CK revoked since the annotation renamed to the label it replaced, those
    outside the label space removed with their relationships, everything else unchanged. For an
    instance, whose STIX metrics cannot restrict the answers themselves."""
    space = _label_space()
    if space is None:
        sys.exit("closed needs LABELS (and ATTACK)")
    revoked = _revoked_into(space)
    target.mkdir(parents=True, exist_ok=True)
    for path in sorted(source.glob("[0-9]*.json"), key=lambda f: int(f.stem)):
        record = json.loads(path.read_text())
        try:
            bundle = json.loads(record.get("answer") or "")
        except ValueError:
            bundle = None
        if isinstance(bundle, dict) and isinstance(bundle.get("objects"), list):
            dropped, objects = set(), []
            for o in bundle["objects"]:
                if o.get("type") == "attack-pattern":
                    found = _predicted(json.dumps({"objects": [o]}), False)
                    attack_id = _in_space(next(iter(found), ""), space, revoked)
                    if attack_id not in space:
                        dropped.add(o.get("id"))
                        continue
                    o = {**o, "x_mitre_id": attack_id, "external_references": [
                        r if r.get("source_name") != "mitre-attack" else {**r, "external_id": attack_id}
                        for r in o.get("external_references") or []]}
                objects.append(o)
            objects = [o for o in objects if o.get("source_ref") not in dropped and o.get("target_ref") not in dropped
                       and not (set(o.get("object_refs") or []) & dropped and o.get("type") != "report")]
            for o in objects:
                if o.get("type") == "report":
                    o["object_refs"] = [r for r in o.get("object_refs") or [] if r not in dropped]
            record = {**record, "answer": json.dumps({**bundle, "objects": objects})}
        (target / path.name).write_text(json.dumps(record))
    print(target, "closed-set answers")


JUDGE_SYSTEM = (
    "You check ATT&CK techniques extracted from a cyber threat intelligence report against the report. For "
    "each technique, say whether the report states or clearly describes that the activity it reports (its "
    "actor, malware, tool or campaign) performs this technique, as the technique's definition describes it. A "
    "technique only mentioned as a defence, a recommendation, a different actor's past activity, or a generic "
    "category is not supported. Answer only JSON: {\"items\": [{\"id\": \"<id>\", \"verdict\": "
    "\"supported\"|\"not_supported\"|\"unsure\", \"quote\": \"<the shortest verbatim passage of the report that "
    "shows it, empty when not supported>\"}]}."
)


def judge(dataset_zip: Path, out: Path, judged: Path, reports: int) -> None:
    """How much of the strict precision loss is the gold's sparsity: a judge of another model
    family (``JUDGE_MODEL``, default GPT-6.1 Sol, max effort) reads each sampled report (seed 7)
    with every technique the run predicts, those in the gold and the others shuffled together and
    unmarked, against its ATT&CK definition; "supported" needs a verbatim quote of the report.
    The supported rate on techniques the gold confirms says how far the judge can be trusted on
    the others. For train or spent data only, never before a registered score."""
    import openai

    from app.llm.openai_compat import normalize_openai_reasoning_effort
    from app.services.stix_extraction.knowledge import attack_catalogue

    catalogue = {t.attack_id.upper(): t for t in asyncio.run(attack_catalogue()) or []}
    model = os.environ.get("JUDGE_MODEL", "gpt-6.1-sol")
    effort = normalize_openai_reasoning_effort("max", model) or "high"
    client = openai.AsyncOpenAI()
    z, entries, files = _entries(dataset_zip)
    sample = random.Random(7).sample(entries, min(reports, len(entries)))  # noqa: S311 - a seeded sample
    judged.mkdir(parents=True, exist_ok=True)
    flat = lambda s: " ".join(str(s).split()).lower()  # noqa: E731
    gate = asyncio.Semaphore(4)

    async def one(entry: dict) -> list[dict]:
        path = judged / f"{entry['position']}.json"
        if path.exists():
            return json.loads(path.read_text())
        text = z.read(files[entry["files"][0]]["path"]).decode("utf-8")
        gold = {o["x_mitre_id"] for o in json.loads(entry["expected_answer"])["objects"]}
        record_path = out / f"{entry['position']}.json"
        answer = json.loads(record_path.read_text()).get("answer", "") if record_path.exists() else ""
        predicted = sorted(t for t in _predicted(answer, False) if t in catalogue)
        if not predicted:
            path.write_text("[]")
            return []
        # The negative control: techniques the run did not predict and the gold does not hold, as
        # many as a fifth of the predicted ones, half a sibling of a predicted technique (hard),
        # half drawn from the whole catalogue; a judge that supports them is not to be trusted.
        rng = random.Random(entry["position"])  # noqa: S311 - a seeded control
        taken = set(predicted) | gold
        n = max(2, len(predicted) // 5)
        siblings = sorted({c for t in predicted for c in catalogue if c.split(".")[0] == t.split(".")[0]} - taken)
        decoys = rng.sample(siblings, min(len(siblings), n // 2))
        decoys += rng.sample(sorted(set(catalogue) - taken - set(decoys)), n - len(decoys))
        shown = predicted + decoys
        rng.shuffle(shown)  # order unrelated to the gold or the control
        listing = "\n".join(f"{t} {catalogue[t].name}: {catalogue[t].description[:700]}" for t in shown)
        async with gate:
            response = await client.responses.create(
                model=model, reasoning={"effort": effort}, instructions=JUDGE_SYSTEM,
                input=f"REPORT:\n<<<\n{text}\n>>>\n\nTECHNIQUES:\n{listing}")
        raw = response.output_text or "{}"
        verdicts = {str(v.get("id", "")).upper(): v for v in json.loads(raw[raw.find("{"):raw.rfind("}") + 1]).get("items", [])}
        rows = []
        for t in shown:
            v = verdicts.get(t, {})
            verdict = str(v.get("verdict", "unsure"))
            if verdict == "supported" and flat(v.get("quote", "")) not in flat(text):
                verdict = "not_supported"  # a quote the report does not hold supports nothing
            rows.append({"id": t, "in_gold": t in gold, "decoy": t in decoys,
                         "sibling": t in decoys and t in siblings, "verdict": verdict})
        path.write_text(json.dumps(rows))
        return rows

    async def main() -> list[list[dict]]:
        return await asyncio.gather(*(one(e) for e in sample))

    every = [row for report in asyncio.run(main()) for row in report]
    rows = [r for r in every if not r.get("decoy")]
    for label, subset in (("in the gold", [r for r in rows if r["in_gold"]]),
                          ("not in the gold", [r for r in rows if not r["in_gold"]]),
                          ("control, a sibling (never predicted)", [r for r in every if r.get("sibling")]),
                          ("control, any technique (never predicted)",
                           [r for r in every if r.get("decoy") and not r.get("sibling")])):
        supported = sum(r["verdict"] == "supported" for r in subset)
        print(f"{label}: {len(subset)} techniques, judged supported {supported} ({supported / max(1, len(subset)):.0%})")
    strict = sum(r["in_gold"] for r in rows) / max(1, len(rows))
    adjusted = sum(r["in_gold"] or r["verdict"] == "supported" for r in rows) / max(1, len(rows))
    print(f"pooled precision over {len(sample)} reports: strict {strict:.3f}, gold or judged supported {adjusted:.3f}")


if __name__ == "__main__":
    command, args = sys.argv[1], sys.argv[2:]
    if command == "fetch":
        fetch(Path(args[0]))
    elif command == "datasets":
        datasets(Path(args[0]))
    elif command == "run":
        run(args[0], Path(args[1]), Path(args[2]))
    elif command == "score":
        score(args[0], Path(args[1]), Path(args[2]))
    elif command == "labels":
        labels(Path(args[0]), Path(args[1]))
    elif command == "answers":
        answers(Path(args[0]), Path(args[1]))
    elif command == "closed":
        closed(Path(args[0]), Path(args[1]))
    elif command == "judge":
        judge(Path(args[0]), Path(args[1]), Path(args[2]), int(args[3]))
    elif command == "package":
        package(Path(args[0]), Path(args[1]), args[2:-1], Path(args[-1]))
    else:
        sys.exit(__doc__)
