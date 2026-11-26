"""The built-in OCR benchmark: its documents, its declarations and their licences.

Offline half. The seed itself, against a real PostgreSQL, is in
``test_builtin_evaluation_seed_postgres.py``.
"""

from __future__ import annotations

import contextlib
import io
import logging
import re

import pytest

from app.evaluations.documents import (
    PDF_CONTENT_TYPE,
    PNG_CONTENT_TYPE,
    DocumentError,
    document_fingerprint,
    document_markdown,
    render_document,
    validate_document,
)
from app.evaluations.seed import (
    SEED_DATA_DIR,
    load_seed_files,
    seed_entry_fields,
    seed_object_name,
    seeded_fingerprint,
)

DATASETS = load_seed_files("datasets")
EVALUATIONS = load_seed_files("evaluations")
DOCUMENTS = [
    (dataset["slug"], entry["document"])
    for dataset in DATASETS
    for entry in dataset["entries"]
    if entry.get("document")
]


def _texts(document: dict) -> list[str]:
    texts: list[str] = []
    for block in document["blocks"]:
        if block["type"] in ("title", "heading", "paragraph"):
            texts.append(block["text"])
        elif block["type"] == "list":
            texts.extend(block["items"])
        elif block["type"] == "table":
            texts.extend(cell for row in block["rows"] for cell in row)
    return texts


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def test_the_ocr_benchmark_ships_text_pdfs_and_scanned_pages():
    slugs = {d["slug"] for d in DATASETS}
    assert "ocr-benchmark" in slugs
    formats = [doc["format"] for slug, doc in DOCUMENTS if slug == "ocr-benchmark"]
    assert formats.count("pdf") >= 3 and formats.count("scan") >= 2


def test_every_entry_attaches_one_valid_document_with_a_unique_file_name():
    for dataset in DATASETS:
        names = []
        for entry in dataset["entries"]:
            assert entry.get("document"), f"{dataset['slug']} entry {entry['position']} has no document"
            validate_document(entry["document"])
            names.append(entry["document"]["file_name"])
        assert len(names) == len(set(names))
        positions = [e["position"] for e in dataset["entries"]]
        assert len(positions) == len(set(positions))


@pytest.mark.parametrize(
    ("entries", "outcome"),
    [
        ([{}, {}, {"position": 5}], [0, 1, 5]),
        ([{"position": 1}, {}], "entries 0 and 1 share position 1"),
        ([{"position": 2}, {"position": 2}], "entries 0 and 1 share position 2"),
    ],
    ids=["numbered-by-index", "a-default-ties", "declared-tie"],
)
def test_a_seed_entry_takes_its_index_as_position_and_a_tie_is_named_at_load(monkeypatch, tmp_path, entries, outcome):
    """Positions are unique in a dataset: a tie would fail the boot's insert, so
    the loader names it."""
    import json

    from app.evaluations import seed as seed_module

    (tmp_path / "datasets").mkdir()
    (tmp_path / "datasets" / "d.json").write_text(json.dumps({"slug": "d", "entries": entries}))
    monkeypatch.setattr(seed_module, "SEED_DATA_DIR", tmp_path)
    if isinstance(outcome, str):
        with pytest.raises(ValueError, match=f"d.json: {outcome}"):
            load_seed_files("datasets")
    else:
        assert [e["position"] for e in load_seed_files("datasets")[0]["entries"]] == outcome


@pytest.mark.parametrize(("slug", "document"), DOCUMENTS, ids=[d["file_name"] for _, d in DOCUMENTS])
def test_the_pdf_text_layer_holds_every_word_of_the_declaration(slug, document):
    if document["format"] != "pdf":
        pytest.skip("scanned page")
    from pypdf import PdfReader

    rendered = render_document(document)
    assert rendered.content_type == PDF_CONTENT_TYPE
    extracted = _squash(" ".join(page.extract_text() for page in PdfReader(io.BytesIO(rendered.data)).pages))
    for text in _texts(document):
        assert _squash(text) in extracted, f"{document['file_name']}: {text!r} missing from the text layer"


def test_a_page_break_starts_a_second_pdf_page():
    from pypdf import PdfReader

    document = next(d for _, d in DOCUMENTS if any(b["type"] == "page_break" for b in d["blocks"]))
    reader = PdfReader(io.BytesIO(render_document(document).data))
    assert len(reader.pages) == 2


def test_a_cp1252_character_reads_back_from_the_text_layer_and_the_title():
    """0x80-0x9F is where WinAnsi (the page font) and PDFDocEncoding (the Info
    dictionary) disagree: an en dash, an accented letter and the euro sign."""
    from pypdf import PdfReader

    title = "Avis \u2013 \u00e9t\u00e9 2026 \u20ac"
    document = {"file_name": "t.pdf", "format": "pdf", "blocks": [{"type": "title", "text": title}]}
    reader = PdfReader(io.BytesIO(render_document(document).data))
    assert reader.metadata.title == title
    assert _squash(reader.pages[0].extract_text()) == title


@pytest.mark.parametrize(("slug", "document"), DOCUMENTS, ids=[d["file_name"] for _, d in DOCUMENTS])
def test_a_scanned_page_is_a_greyscale_png(slug, document):
    if document["format"] != "scan":
        pytest.skip("text PDF")
    from PIL import Image

    rendered = render_document(document)
    assert rendered.content_type == PNG_CONTENT_TYPE
    image = Image.open(io.BytesIO(rendered.data))
    assert image.format == "PNG" and image.mode == "L"
    assert image.width == 1240 and 300 < image.height < 3000


def test_rendering_is_deterministic_so_the_seed_uploads_a_document_once():
    for slug, document in DOCUMENTS:
        first, second = render_document(document), render_document(document)
        assert first.data == second.data
        assert seed_object_name(slug, first) == seed_object_name(slug, second)


def test_the_seed_key_follows_the_bytes():
    slug, document = DOCUMENTS[0]
    key = seed_object_name(slug, render_document(document))
    assert key.startswith(f"dataset/builtin/{slug}/") and key.endswith(f"-{document['file_name']}")
    changed = {**document, "blocks": [*document["blocks"], {"type": "paragraph", "text": "One more line."}]}
    assert seed_object_name(slug, render_document(changed)) != key


def test_the_fingerprint_follows_the_declaration_and_the_renderer_not_the_bytes(monkeypatch):
    from app.evaluations import documents

    slug, document = DOCUMENTS[0]
    fingerprint = document_fingerprint(document)
    rendered = render_document(document)
    assert rendered.fingerprint == fingerprint
    assert seeded_fingerprint(seed_object_name(slug, rendered)) == fingerprint
    # Same text, another document: the transcription is unchanged, the test case is not.
    with_break = {**document, "blocks": [*document["blocks"], {"type": "page_break"}]}
    assert document_markdown(with_break) == document_markdown(document)
    assert document_fingerprint(with_break) != fingerprint
    monkeypatch.setattr(documents, "RENDERER_VERSION", documents.RENDERER_VERSION + 1)
    assert document_fingerprint(document) != fingerprint
    assert seeded_fingerprint("dataset/abc/upload.pdf") == ""


def test_the_expected_answer_is_the_document_text_as_markdown():
    document = {
        "file_name": "x.pdf",
        "format": "pdf",
        "blocks": [
            {"type": "title", "text": "Title"},
            {"type": "heading", "text": "Part"},
            {"type": "paragraph", "text": "Body."},
            {"type": "list", "items": ["one", "two"]},
            {"type": "table", "rows": [["A", "B"], ["1", "2"]]},
        ],
    }
    assert document_markdown(document) == (
        "# Title\n\n## Part\n\nBody.\n\n- one\n- two\n\n| A | B |\n| --- | --- |\n| 1 | 2 |"
    )
    fields = seed_entry_fields({"position": 3, "input_prompt": "Transcribe.", "document": document})
    assert fields["expected_answer"] == document_markdown(document)
    assert fields["position"] == 3 and fields["input_prompt"] == "Transcribe."


@pytest.mark.parametrize(
    "document",
    [
        {"file_name": "x.pdf", "format": "docx", "blocks": [{"type": "paragraph", "text": "a"}]},
        {"file_name": "x.png", "format": "pdf", "blocks": [{"type": "paragraph", "text": "a"}]},
        {"file_name": "../x.pdf", "format": "pdf", "blocks": [{"type": "paragraph", "text": "a"}]},
        {"file_name": "x.pdf", "format": "pdf", "blocks": []},
        {"file_name": "x.pdf", "format": "pdf", "blocks": [{"type": "quote", "text": "a"}]},
        {"file_name": "x.pdf", "format": "pdf", "blocks": [{"type": "paragraph", "text": "a | b"}]},
        {"file_name": "x.pdf", "format": "pdf", "blocks": [{"type": "paragraph", "text": "a\nb"}]},
        {"file_name": "x.pdf", "format": "pdf", "blocks": [{"type": "paragraph", "text": "\u4e2d\u6587"}]},
        {"file_name": "x.pdf", "format": "pdf", "blocks": [{"type": "table", "rows": [["A", "B"], ["1"]]}]},
    ],
)
def test_a_document_that_cannot_be_rendered_faithfully_is_refused(document):
    with pytest.raises(DocumentError):
        validate_document(document)


def test_a_table_wider_than_the_page_is_refused_not_clipped():
    document = {
        "file_name": "wide.pdf",
        "format": "pdf",
        "blocks": [{"type": "table", "rows": [["A", "B"], ["x" * 80, "y" * 80]]}],
    }
    with pytest.raises(DocumentError):
        render_document(document)


def test_every_document_is_listed_with_its_licence_and_source():
    sources = (SEED_DATA_DIR / "SOURCES.md").read_text(encoding="utf-8")
    for slug, document in DOCUMENTS:
        row = next((line for line in sources.splitlines() if f"`{document['file_name']}`" in line), None)
        assert row is not None, f"{document['file_name']} is not listed in SOURCES.md"
        assert f"`{slug}`" in row and "Filigran SAS" in row and "Apache License 2.0" in row


def test_the_documents_are_marked_fictional():
    for _, document in DOCUMENTS:
        assert any("Fictional" in text or "fictif" in text for text in _texts(document)), document["file_name"]


def test_every_evaluation_links_a_shipped_dataset_and_built_in_agents():
    from app.agents.builtin_agents import BUILTIN_AGENTS

    agents = {agent["slug"]: agent for agent in BUILTIN_AGENTS}
    datasets = {dataset["slug"] for dataset in DATASETS}
    for evaluation in EVALUATIONS:
        assert evaluation["dataset_slug"] in datasets
        assert evaluation["agent_slugs"]
        for slug in evaluation["agent_slugs"]:
            assert slug in agents, f"{evaluation['slug']} links unknown agent {slug}"
        assert evaluation["execution_mode"] in ("llm_only", "full_loop")
        assert evaluation["timeout_seconds"] >= 60
        # Manual only: a built-in evaluation never spends tokens on its own.
        assert "schedule" not in evaluation.get("trigger_config", {})


def test_an_evaluation_of_tool_less_agents_declares_llm_only():
    """A tool-less agent has no loop to run: the mode is declared, not left to the full-loop default."""
    from app.agents.builtin_agents import BUILTIN_AGENTS

    def _is_tool_less(agent: dict) -> bool:
        # Declared empty, as the transcriber is: an agent missing the keys may still bind
        # tools another way (a ``*_tool_ref``), so it is not counted as tool-less.
        return (
            agent.get("builtin_tool_ids") == []
            and agent.get("integration_template_bindings") == []
            and not any(key.endswith("_tool_ref") and value for key, value in agent.items())
        )

    agents = {agent["slug"]: agent for agent in BUILTIN_AGENTS}
    tool_less = [
        evaluation
        for evaluation in EVALUATIONS
        if all(_is_tool_less(agents[slug]) for slug in evaluation["agent_slugs"])
    ]
    assert tool_less, "no built-in evaluation of tool-less agents to check"
    for evaluation in tool_less:
        assert evaluation["execution_mode"] == "llm_only", evaluation["slug"]


def test_the_transcriber_agent_is_tool_less_and_hidden_from_chat():
    from app.agents.builtin_agents import BUILTIN_AGENTS

    agent = next(a for a in BUILTIN_AGENTS if a["slug"] == "ocr-benchmark-transcriber")
    assert agent["disable_chat"] is True
    assert agent["builtin_tool_ids"] == [] and agent["integration_template_bindings"] == []
    persona = SEED_DATA_DIR.parent.parent / "agents" / "seed_data" / "agent_persona" / "ocr-benchmark-transcriber.md"
    assert persona.is_file()


def test_the_migration_adds_the_slug_the_models_declare():
    """A fresh install (``create_all`` + ``stamp head``) never runs the migration
    body, so the models must name exactly the indexes the migration builds."""
    import importlib.util

    from app.models.dataset import Dataset
    from app.models.evaluation import Evaluation

    assert Dataset.__table__.c.slug.nullable and Dataset.__table__.c.slug.index
    assert Evaluation.__table__.c.slug.nullable and Evaluation.__table__.c.slug.index
    path = next((SEED_DATA_DIR.parents[2] / "alembic" / "versions").glob("e2232seedslug1_*.py"))
    spec = importlib.util.spec_from_file_location(path.stem, path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    built = {(table, f"ix_{table}_{migration._COLUMN}") for table in migration._TABLES}
    declared = {
        (table.name, index.name)
        for table in (Dataset.__table__, Evaluation.__table__)
        for index in table.indexes
        if [column.name for column in index.columns] == [migration._COLUMN]
    }
    assert declared == built == {("datasets", "ix_datasets_slug"), ("evaluations", "ix_evaluations_slug")}


def test_provisioning_seeds_the_benchmark_after_the_agents():
    import inspect

    from app.services import provisioning

    source = inspect.getsource(provisioning.provision_builtin_content)
    assert source.index("seed_builtin_agents(") < source.index("seed_builtin_evaluation_content(")
    benchmark = inspect.getsource(provisioning.seed_builtin_evaluation_content)
    assert benchmark.index("seed_builtin_datasets(") < benchmark.index("seed_builtin_evaluations(")


@pytest.mark.usefixtures("evaluation_enabled")
async def test_a_failing_benchmark_seed_never_fails_the_boot(monkeypatch, caplog):
    """Optional content: a failure is logged and retried at the next start, and
    the steps after it (the model prices) still run."""
    from app.agents import seed as agent_seed
    from app.evaluations import seed as evaluation_seed
    from app.services import provisioning, template_asset_seed, template_container_seed
    from app.services.tracing import price_loader

    @contextlib.asynccontextmanager
    async def session():
        yield None

    async def nothing(*_args, **_kwargs):
        return 0

    async def fails(*_args, **_kwargs):
        raise RuntimeError("canceling statement due to lock timeout")

    priced: list[bool] = []

    async def prices(_db):
        priced.append(True)
        return 0, 0

    for name in (
        "seed_builtin_templates",
        "seed_builtin_agents",
        "seed_builtin_assignments",
        "seed_builtin_skills",
        "seed_builtin_integrations",
        "seed_builtin_knowledge_bases",
        "seed_builtin_intent_bindings",
    ):
        monkeypatch.setattr(agent_seed, name, nothing)
    from app.agents import fleet_seed

    monkeypatch.setattr(fleet_seed, "seed_builtin_fleets", nothing)
    from app.services import fleets as fleet_service

    monkeypatch.setattr(fleet_service, "backfill_fleet_health_advisory", nothing)
    # Platform Health is built in (#101): its fleet is seeded on every boot.
    from app.services.health import fleet_seed as health_fleet_seed

    monkeypatch.setattr(health_fleet_seed, "seed_health_fleet", nothing)
    # The CTEM access groups (#2441) are seeded on every xtm_one boot.
    from app.services import ctem_groups

    monkeypatch.setattr(ctem_groups, "seed_ctem_groups", nothing)
    monkeypatch.setattr(template_container_seed, "seed_builtin_template_containers", nothing)
    monkeypatch.setattr(template_asset_seed, "seed_builtin_template_assets", nothing)
    monkeypatch.setattr(evaluation_seed, "seed_builtin_datasets", fails)
    monkeypatch.setattr(evaluation_seed, "seed_builtin_evaluations", fails)
    monkeypatch.setattr(price_loader, "sync_model_prices", prices)
    monkeypatch.setattr(provisioning, "async_session", session)

    with caplog.at_level(logging.WARNING, logger=provisioning.logger.name):
        await provisioning.provision_builtin_content(seed_mode="xtm_one")

    assert priced == [True]
    assert sum("will retry on next startup" in record.getMessage() for record in caplog.records) == 2


# ── The table-layout limit the user docs show ────────────────────────


def _cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _is_rule(line: str) -> bool:
    return set(line.replace("|", "").strip()) <= set("-: ")


def _table_variants(markdown: str) -> dict[str, str]:
    """The answers the docs' table measures, built from the expected Markdown."""
    lines = markdown.splitlines()
    rows = [i for i, line in enumerate(lines) if line.startswith("|") and not _is_rule(line)]
    swapped = list(lines)
    first, second = (_cells(lines[i]) for i in rows[1:3])
    first[1], second[1] = second[1], first[1]
    swapped[rows[1]], swapped[rows[2]] = ("| " + " | ".join(cells) + " |" for cells in (first, second))
    return {
        "unpadded": "\n".join("|" + "|".join(_cells(line)) + "|" if line.startswith("|") else line for line in lines),
        "plain_lines": "\n".join(
            ("" if _is_rule(line) else " ".join(_cells(line))) if line.startswith("|") else line for line in lines
        ),
        "rows_swapped": "\n".join(swapped),
        "last_row_missing": "\n".join(line for i, line in enumerate(lines) if i != rows[-1]),
    }


@pytest.mark.parametrize("position", [2, 6], ids=["indicator_annex", "patch_status_table"])
def test_the_token_f1_of_a_table_matches_what_the_user_docs_show(position):
    """``docs/docs/administration/evaluations.md`` shows these: re-measure them there when this fails."""
    from types import SimpleNamespace

    from app.services.evaluation.metric_package import build_quality_config, run_quality

    (seed,) = load_seed_files("datasets")
    expected = seed_entry_fields(next(e for e in seed["entries"] if e["position"] == position))["expected_answer"]
    config = build_quality_config("PROSE", SimpleNamespace(field_scoring_overrides=None), [expected])

    def token_f1(answer: str) -> float:
        outcomes = run_quality(family="PROSE", reference=expected, prediction=answer, output_schema=None, config=config)
        return next(o.value for o in outcomes if o.key == "prose.token_prf")

    variants = _table_variants(expected)
    assert token_f1(expected) == 100
    assert 43 <= token_f1(variants["unpadded"]) <= 46
    assert 76 <= token_f1(variants["plain_lines"]) <= 77
    assert token_f1(variants["rows_swapped"]) == 100
    assert 94 <= token_f1(variants["last_row_missing"]) <= 96

