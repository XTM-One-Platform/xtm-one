"""Read surface for investigation runs.

The load-bearing property here is anti-drift: the block hydrates from
``GET /investigations/{id}`` and then merges WebSocket frames into the same
step objects, so the REST payload and the frame payload must describe a step
with the identical field list. If they diverge, a step changes shape (or
disappears) the moment a frame arrives — and the divergence would be invisible
until someone watched a live run.
"""

import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.models.investigation import Investigation, InvestigationStep
from app.services.investigation_events import planned_frame, step_frame, step_payload
from app.services.investigations import serialize_investigation


def _run(**kwargs) -> Investigation:
    return Investigation(
        id=kwargs.pop("id", uuid.uuid4()),
        subject_type=kwargs.pop("subject_type", "cve"),
        subject_value=kwargs.pop("subject_value", "CVE-2024-3400"),
        source_pack_slugs=kwargs.pop("source_pack_slugs", ["web-general"]),
        status=kwargs.pop("status", "completed"),
        revision=kwargs.pop("revision", 8),
        iterations_used=kwargs.pop("iterations_used", 2),
        max_iterations=kwargs.pop("max_iterations", 10),
        **kwargs,
    )


def _step(**kwargs) -> InvestigationStep:
    return InvestigationStep(
        id=kwargs.pop("id", uuid.uuid4()),
        position=kwargs.pop("position", 0),
        source_name=kwargs.pop("source_name", "NVD"),
        status=kwargs.pop("status", "completed"),
        findings_count=kwargs.pop("findings_count", 1),
        evidence=kwargs.pop("evidence", []),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Anti-drift between the two paths
# ---------------------------------------------------------------------------


def test_a_rest_step_and_a_frame_step_have_the_identical_field_list():
    """The block merges frames into fetched objects with no translation layer."""
    run, step = _run(), _step()
    rest_step = serialize_investigation(run, [step])["steps"][0]
    frame = step_frame(run, step)
    # The frame adds its own envelope keys; every step field must match.
    for key, value in rest_step.items():
        assert key in frame, f"{key} missing from the frame payload"
        assert frame[key] == value


def test_the_planned_frame_and_the_rest_payload_share_the_step_builder():
    run = _run(status="planning")
    steps = [_step(position=i, status="pending", findings_count=0) for i in range(3)]
    assert planned_frame(run, steps)["steps"] == serialize_investigation(run, steps)["steps"]


def test_the_step_state_field_is_step_status_not_status():
    """A step payload spread into a frame must not overwrite the frame kind."""
    payload = step_payload(_step(status="error", error_type="timeout"))
    assert payload["step_status"] == "error"
    assert "status" not in payload


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def test_steps_are_returned_in_display_order_whatever_the_row_order():
    """Ordering is server-assigned; the client never sorts."""
    run = _run()
    shuffled = [
        _step(position=2, source_name="C"),
        _step(position=0, source_name="A"),
        _step(position=1, source_name="B"),
    ]
    payload = serialize_investigation(run, shuffled)
    assert [s["source_name"] for s in payload["steps"]] == ["A", "B", "C"]
    assert [s["position"] for s in payload["steps"]] == [0, 1, 2]


def test_the_summary_is_computed_server_side():
    """`all_succeeded` gates the "everything passed" affordance everywhere."""
    run = _run()
    payload = serialize_investigation(
        run,
        [
            _step(position=0, status="completed"),
            _step(position=1, status="error", error_type="timeout", findings_count=0),
            _step(position=2, status="skipped", findings_count=0),
        ],
    )
    summary = payload["summary"]
    assert summary["total"] == 3
    assert summary["all_succeeded"] is False
    assert summary["failed"] == 1
    assert summary["by_status"]["skipped"] == 1


def test_an_all_completed_run_reports_all_succeeded():
    run = _run()
    payload = serialize_investigation(run, [_step(position=0), _step(position=1, source_name="CVE.org record")])
    assert payload["summary"]["all_succeeded"] is True


def test_duration_is_null_until_the_run_finished():
    """No client-side fallback exists, so a null here means the block shows none."""
    run = _run(status="running")
    assert serialize_investigation(run, [])["duration_seconds"] is None


def test_duration_is_the_measured_wall_clock():
    run = _run()
    run.started_at = datetime(2026, 9, 3, 10, 0, 0, tzinfo=UTC)
    run.completed_at = run.started_at + timedelta(seconds=247)
    payload = serialize_investigation(run, [_step()])
    assert payload["duration_seconds"] == pytest.approx(247.0)
    # Two steps, 247 seconds: nothing here may be derived from the step count.
    assert payload["duration_seconds"] != len(payload["steps"]) * 3.5


def test_an_absent_confidence_survives_serialization_as_null():
    payload = serialize_investigation(_run(), [_step(status="empty", findings_count=0)])
    assert payload["steps"][0]["confidence"] is None
    assert payload["summary"]["confidence"] is None


def test_the_end_reason_is_carried_so_an_early_finish_never_reads_as_complete():
    run = _run()
    run.end_reason = "iteration budget spent (2)"
    payload = serialize_investigation(run, [])
    assert payload["end_reason"] == "iteration budget spent (2)"
    assert payload["iterations_used"] == 2
    assert payload["max_iterations"] == 10


def test_the_payload_validates_against_the_response_schema():
    from app.api.v1.schemas.investigation import InvestigationResponse

    run = _run()
    run.started_at = datetime.now(UTC)
    run.completed_at = run.started_at + timedelta(seconds=3)
    payload = serialize_investigation(
        run,
        [
            _step(position=0, status="completed", confidence=0.75),
            _step(position=1, status="degraded", status_detail="truncated"),
            _step(position=2, status="error", error_type="rate_limited"),
        ],
    )
    model = InvestigationResponse(**payload)
    assert model.revision == 8
    assert [s.step_status for s in model.steps] == ["completed", "degraded", "error"]


def test_the_payload_is_json_safe():
    import json

    parent = _step()
    payload = serialize_investigation(
        _run(),
        [parent, _step(position=1, lead_from_step_id=parent.id)],
    )
    json.dumps(payload)
    assert payload["steps"][1]["lead_from_step_id"] == str(parent.id)


# ---------------------------------------------------------------------------
# REST surface
# ---------------------------------------------------------------------------


def test_read_endpoint_is_owner_or_admin():
    from fastapi import HTTPException

    from app.api.v1.investigations import _assert_readable

    owner = uuid.uuid4()
    run = SimpleNamespace(user_id=owner)

    _assert_readable(run, SimpleNamespace(id=owner, is_admin=False))
    _assert_readable(run, SimpleNamespace(id=uuid.uuid4(), is_admin=True))
    # Someone else's run is answered like an unknown one: the 404 never
    # confirms that the id exists.
    with pytest.raises(HTTPException) as exc:
        _assert_readable(run, SimpleNamespace(id=uuid.uuid4(), is_admin=False))
    assert (exc.value.status_code, exc.value.detail) == (404, "Investigation not found")
    # Orphaned run (author deleted) stays readable for auditing, admins only.
    _assert_readable(SimpleNamespace(user_id=None), SimpleNamespace(id=owner, is_admin=True))
    with pytest.raises(HTTPException) as exc:
        _assert_readable(SimpleNamespace(user_id=None), SimpleNamespace(id=owner, is_admin=False))
    assert exc.value.status_code == 404


#: The only routes on this router allowed to use a write verb, as
#: ``(method, path)``. FROZEN — never extend it to make a new endpoint pass.
#:
#: This list replaced a blanket "GET/HEAD/OPTIONS only" assertion, and the
#: swap is deliberately narrower rather than looser. That assertion was a
#: *proxy* for the real guarantee — runs are written only through the state
#: machine — and the proxy stopped matching once a route needed to START a run
#: without writing one. The two tests below now check the guarantee itself,
#: which is why this allowlist can exist without weakening anything: adding a
#: route still fails, and a route that writes step state fails even if it is
#: listed here.
_ALLOWED_WRITE_ROUTES = {
    ("POST", "/{investigation_id}/continuation"),
}


def test_investigation_api_exposes_no_unexpected_write_route():
    """Runs are only ever written through the state machine, which is what
    guarantees a failed source cannot be stored as a success. A write route
    here would be a way around that guarantee.

    The continuation POST is listed because it writes nothing: it re-authorises
    a selection of sources and hands them to the orchestrator, which stays the
    exclusive writer. See the companion test below, which is what actually
    holds that line.
    """
    from app.api.v1 import investigations as api

    writes = {
        (method, getattr(route, "path", ""))
        for route in api.router.routes
        for method in getattr(route, "methods", set())
        if method not in {"GET", "HEAD", "OPTIONS"}
    }
    assert writes <= _ALLOWED_WRITE_ROUTES, writes - _ALLOWED_WRITE_ROUTES


def test_the_api_cannot_write_step_state_itself():
    """The guarantee the method allowlist above is only a proxy for.

    A route may start a run; it may not decide what a step's outcome was. So
    the module must not reach for the state machine's writers at all — those
    belong to the orchestrator, which is what makes "a failure never becomes a
    success" enforceable in one place. Importing one here would let an
    endpoint stamp an outcome directly, which is the escape hatch this test
    exists to keep shut.
    """
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "app" / "api" / "v1" / "investigations.py").read_text()
    forbidden = (
        "record_step_result",
        "record_step_error",
        "mark_step_active",
        "mark_step_skipped",
        "plan_steps",
        "append_lead_step",
        "finish_run",
        "start_run",
        "create_investigation",
    )
    used = [name for name in forbidden if name in source]
    assert not used, f"the investigation API reaches for state-machine writers: {used}"


def test_the_router_is_registered_under_the_expected_prefix():
    from app.main import application

    paths = {getattr(r, "path", "") for r in application.routes}
    assert "/api/v1/investigations/{investigation_id}" in paths


# ---------------------------------------------------------------------------
# Backend vocabulary ↔ frontend rendering parity
# ---------------------------------------------------------------------------


def _block_source() -> str:
    from pathlib import Path

    return (
        Path(__file__).resolve().parents[2]
        / "frontend"
        / "src"
        / "components"
        / "investigation"
        / "InvestigationBlock.tsx"
    ).read_text()


def test_every_step_status_has_a_visual_treatment_in_the_block():
    """A backend state with no entry in `STEP_STYLES` falls back to `pending`.

    That fallback is the dangerous kind of silent: a new `error`-like state
    would render as a muted clock and read as "still planned" — the block would
    be lying about coverage, which is the one thing it exists not to do.
    """
    from app.models.investigation import STEP_STATUSES

    source = _block_source()
    styles = source[source.index("STEP_STYLES") : source.index("ERROR_LABELS")]
    missing = [s for s in STEP_STATUSES if f"{s}:" not in styles]
    assert not missing, f"InvestigationBlock has no STEP_STYLES entry for {missing}"


def test_only_completed_is_rendered_green_in_the_block():
    """The "a non-success never looks like a success" rule, asserted.

    `empty` in particular must not borrow the success tone: a source that
    answered with nothing has to be distinguishable from one that found
    something.
    """
    source = _block_source()
    styles = source[source.index("STEP_STYLES") : source.index("ERROR_LABELS")]
    # One success tone in the table, and it belongs to `completed`. The tone is
    # the design system's token; it was `text-emerald-400` before.
    success_tone = "text-feedback-success-primary"
    assert styles.count(success_tone) == 1
    completed_entry = styles[styles.index("completed:") : styles.index("empty:")]
    assert success_tone in completed_entry


def test_every_error_type_has_a_label_in_the_block():
    """An unlabelled error type renders as its raw snake_case identifier."""
    from app.models.investigation import STEP_ERROR_TYPES

    source = _block_source()
    labels = source[source.index("ERROR_LABELS") : source.index("function StepRow")]
    missing = [e for e in STEP_ERROR_TYPES if f"{e}:" not in labels]
    assert not missing, f"InvestigationBlock has no ERROR_LABELS entry for {missing}"


def test_the_frontend_step_status_type_matches_the_backend_vocabulary():
    """The TS union and the Python tuple must name the same seven states."""
    from pathlib import Path

    from app.models.investigation import STEP_STATUSES

    types_src = (Path(__file__).resolve().parents[2] / "frontend" / "src" / "types" / "index.ts").read_text()
    block = re.search(r"export type InvestigationStepStatus\s*=([^;]+);", types_src)
    assert block, "frontend must declare InvestigationStepStatus"
    declared = set(re.findall(r'"([a-z_]+)"', block.group(1)))
    assert declared == set(STEP_STATUSES), declared ^ set(STEP_STATUSES)


def test_conversation_previews_strip_every_artifact_marker():
    """A list preview cannot resolve a marker, so it must not show one.

    Regression guard for a bug the investigation block surfaced: the
    conversation list truncated raw message content, so a message whose body
    was mostly `[[INVESTIGATION:…]]` (or `[[FILE:…]]`, `[[REPORT:…]]`,
    `[[PRESENTATION:…]]` — the defect predated this feature) previewed as the
    marker itself.
    """
    import uuid as _uuid

    from app.channels.base import strip_artifact_markers

    ids = [str(_uuid.uuid4()) for _ in range(4)]
    text = (
        f"Here is what I found.\n\n[[INVESTIGATION:{ids[0]}]]\n"
        f"[[REPORT:{ids[1]}]]\n[[PRESENTATION:{ids[2]}]]\n[[FILE:{ids[3]}]]"
    )
    cleaned = strip_artifact_markers(text)
    assert cleaned == "Here is what I found."
    for marker in ("INVESTIGATION", "REPORT", "PRESENTATION", "FILE"):
        assert marker not in cleaned.upper()


def test_stripping_collapses_the_whitespace_a_marker_leaves_behind():
    """A marker usually sits alone after a blank line; stripping it in place
    would leave a preview starting with newlines and looking truncated."""
    import uuid as _uuid

    from app.channels.base import strip_artifact_markers

    text = f"Done.\n\n[[INVESTIGATION:{_uuid.uuid4()}]]\n\nMore."
    assert strip_artifact_markers(text) == "Done. More."


def test_stripping_is_a_no_op_on_marker_free_text():
    from app.channels.base import strip_artifact_markers

    assert strip_artifact_markers("") == ""
    assert strip_artifact_markers("Plain text.") == "Plain text."


def test_the_new_block_strings_exist_in_every_locale():
    """A missing key falls back to the English source string, which produced a
    half-French summary line the first time this block was rendered
    (`{count} failed` had a French translation from another feature; the
    neighbouring keys did not)."""
    import json
    from pathlib import Path

    frontend = Path(__file__).resolve().parents[2] / "frontend" / "src"
    source = (frontend / "components" / "investigation" / "InvestigationBlock.tsx").read_text()
    keys = set(re.findall(r't_i18n\(\s*\n?\s*"((?:[^"\\]|\\.)*)"', source))
    keys |= set(re.findall(r'label:\s*"([^"]+)"', source))
    assert keys, "no translatable strings found — did the block change shape?"

    for lang in ("en", "fr", "es", "ja"):
        catalog = json.loads((frontend / "i18n" / "lang" / f"{lang}.json").read_text())
        missing = sorted(k for k in keys if k not in catalog)
        assert not missing, f"{lang}.json is missing {missing}"


# ---------------------------------------------------------------------------
# Model ↔ migration parity
# ---------------------------------------------------------------------------


#: Tables whose migrated and `create_all` shapes must match exactly.
PARITY_TABLES = ("investigations", "investigation_steps")


@pytest.mark.asyncio
async def test_the_migration_and_the_model_agree_on_the_schema():
    """Fresh installs run `create_all`; existing installs run the migrations.

    The two must produce the same schema. They did not once: `created_at` /
    `updated_at` were `nullable=True` in the migration while the model's
    non-Optional `Mapped[datetime]` makes `create_all` emit NOT NULL, so a
    migrated database disagreed with a fresh one on the very columns the list
    endpoints order by. `test_fresh_install` compares tables and columns but
    not nullability, which is why it passed.

    This used to walk the migrations with `ast` and fold their `_ADDITIONS`
    map into a column set. That worked for one migration's style, was taught a
    second, and would have needed a third for `op.add_column` /
    `op.drop_column` — a parser tracking how migrations are WRITTEN rather than
    what they PRODUCE. Running both paths against real databases and diffing
    the result needs no such upkeep, and compares nullability, types and
    indexes in one pass.
    """
    import os
    import subprocess
    import sys
    import urllib.parse

    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError, OperationalError, ProgrammingError
    from sqlalchemy.ext.asyncio import create_async_engine

    import app.models  # noqa: F401 — registers every mapper
    from app.core.database import Base

    base = os.environ.get(
        "DATABASE_URL", "postgresql+asyncpg://copilot:copilot@localhost:5432/copilot"
    )
    parsed = urllib.parse.urlparse(base)
    admin_url = parsed._replace(path="/postgres").geturl()
    migrated = f"invparity_m_{uuid.uuid4().hex[:8]}"
    created = f"invparity_c_{uuid.uuid4().hex[:8]}"

    admin = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            for name in (migrated, created):
                await conn.execute(text(f'CREATE DATABASE "{name}"'))
    except (DBAPIError, OperationalError, ProgrammingError) as exc:
        orig = getattr(exc, "orig", None)
        if (getattr(orig, "pgcode", None) or getattr(orig, "sqlstate", None)) == "42501":
            pytest.skip("DATABASE_URL user requires CREATEDB privilege")
        raise
    except Exception as exc:  # pragma: no cover — no PG in this environment
        pytest.skip(f"Cannot connect to PostgreSQL: {exc}")
    finally:
        await admin.dispose()

    def _url(name: str) -> str:
        return parsed._replace(path=f"/{name}").geturl()

    try:
        for name in (migrated, created):
            engine = create_async_engine(_url(name))
            async with engine.begin() as conn:
                await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            await engine.dispose()

        proc = subprocess.run(
            [sys.executable, "-m", "alembic", "-c", "alembic/alembic.ini", "upgrade", "head"],
            env={**os.environ, "DATABASE_URL": _url(migrated),
                 "SECRET_KEY": os.environ.get("SECRET_KEY", "test-secret")},
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, proc.stderr[-4000:]

        engine = create_async_engine(_url(created))
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await engine.dispose()

        async def _schema(name: str) -> tuple[dict, dict]:
            engine = create_async_engine(_url(name))
            async with engine.connect() as conn:
                cols = {
                    (r[0], r[1]): (r[2], r[3])
                    for r in (
                        await conn.execute(
                            text(
                                "SELECT table_name, column_name, data_type, is_nullable "
                                "FROM information_schema.columns "
                                "WHERE table_name = ANY(:tables)"
                            ),
                            {"tables": list(PARITY_TABLES)},
                        )
                    ).all()
                }
                idx = {
                    r[0]: r[1].split(" ON ", 1)[1]
                    for r in (
                        await conn.execute(
                            text(
                                "SELECT indexname, indexdef FROM pg_indexes "
                                "WHERE tablename = ANY(:tables)"
                            ),
                            {"tables": list(PARITY_TABLES)},
                        )
                    ).all()
                }
            await engine.dispose()
            return cols, idx

        migrated_cols, migrated_idx = await _schema(migrated)
        created_cols, created_idx = await _schema(created)

        assert migrated_cols == created_cols
        assert migrated_idx == created_idx
        # Both tables were actually reached — a typo'd name would otherwise
        # make this pass by comparing two empty dicts.
        assert {t for t, _ in migrated_cols} == set(PARITY_TABLES)
        # And the column this PR widened exists on both paths, with the old
        # one gone: a migration that added `evidence` without dropping
        # `evidence_urls` would leave two competing citation fields.
        assert ("investigation_steps", "evidence") in migrated_cols
        assert ("investigation_steps", "evidence_urls") not in migrated_cols
    finally:
        cleanup = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
        try:
            async with cleanup.connect() as conn:
                for name in (migrated, created):
                    await conn.execute(
                        text(
                            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                            "WHERE datname = :db AND pid <> pg_backend_pid()"
                        ),
                        {"db": name},
                    )
                    await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
        finally:
            await cleanup.dispose()


def test_the_timestamp_columns_carry_a_server_default_on_both_paths():
    """A server default keeps a non-ORM insert (raw SQL, a backfill) valid on a
    NOT NULL column, and is what makes the two schemas byte-identical."""
    from app.models.investigation import Investigation, InvestigationStep

    for model in (Investigation, InvestigationStep):
        for name in ("created_at", "updated_at"):
            column = model.__table__.columns[name]
            assert column.nullable is False, f"{model.__tablename__}.{name}"
            assert column.server_default is not None, f"{model.__tablename__}.{name} has no server_default"


# ---------------------------------------------------------------------------
# The source-pack picker's planning endpoint
# ---------------------------------------------------------------------------


class _NoRowsSession:
    """A session that answers every query with nothing.

    The route reads the caller's knowledge bases and designated MCP tools to
    build the install-specific packs. An install with neither is the
    state these tests describe, and it is what every assertion below was
    written against — the built-in packs, alone.
    """

    async def execute(self, _stmt):
        from types import SimpleNamespace

        empty = SimpleNamespace(
            all=lambda: [], first=lambda: None, one_or_none=lambda: None
        )
        return SimpleNamespace(
            scalars=lambda: empty,
            all=lambda: [],
            first=lambda: None,
            scalar_one_or_none=lambda: None,
            scalars_all=lambda: [],
        )


def _plan(subject: str, monkeypatch=None):
    """Call the planning route directly, against an install with no extras."""
    import asyncio
    from types import SimpleNamespace

    from app.api.v1.investigations import plan_source_packs

    return asyncio.run(
        plan_source_packs(
            subject=subject,
            user=SimpleNamespace(id=uuid.uuid4(), role="user"),
            db=_NoRowsSession(),
        )
    )


def test_the_plan_classifies_the_subject_server_side():
    """The classifier is server-side, so the classification must be too.

    Duplicating the regex in TypeScript would create two answers to "what kind
    of subject is this?", and the one that picks the sources has to be the one
    shown to the user.
    """
    assert _plan("CVE-2024-3400").subject_type == "cve"
    assert _plan("8.8.8.8").subject_type == "ip"
    assert _plan("Cozy Bear").subject_type == "unknown"


def test_a_whitespace_subject_is_refused_rather_than_planned():
    """`min_length=1` counts characters, not content. A subject of spaces
    reached `classify_subject` as "", came back `unknown` with a full plan and
    `has_applicable_source: true` — for a string the turn then refuses. The
    picker must not offer packs for a subject that cannot be run."""
    import pytest
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as caught:
        _plan("   ")
    assert caught.value.status_code == 422


def test_a_pack_recommended_for_the_type_can_still_have_no_applicable_source():
    """The field that keeps the picker honest.

    `web-general` declares every subject type, so it is "recommended" for a
    file hash — but it has no hash source: no keyless GET endpoint survived
    the check. Without the count the picker would offer it and the run would
    query nothing.
    """
    plan = _plan("e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855")
    assert plan.subject_type == "hash"
    pack = next(p for p in plan.packs if p.slug == "web-general")
    assert pack.recommended is True
    assert pack.applicable_source_count == 0
    assert pack.total_source_count > 0


def test_a_subject_no_pack_can_cover_says_so(monkeypatch):
    """The picker's refusal branch, tested against a registry that has the gap.

    It used to be tested through a file hash, which no built-in could reach
    until `web-research` arrived — searching the web covers every subject
    type, hashes included, so that gap is gone and testing through it now
    asserts the opposite of what the platform does. The BRANCH still has to
    work: an install whose packs cannot touch a subject must say so rather
    than start a run with an empty plan.
    """
    from app.services import investigation_packs as packs_module
    from app.services.investigation_packs import SourceDefinition, SourcePack

    hash_blind = SourcePack(
        slug="hash-blind",
        label="Hash blind",
        description="",
        subject_types=packs_module.SUBJECT_TYPES,
        sources=(
            SourceDefinition(
                name="Domains only",
                subject_types=("domain",),
                url_template="https://example.invalid/{value}",
            ),
        ),
    )
    monkeypatch.setattr(packs_module, "BUILTIN_PACKS", {hash_blind.slug: hash_blind})
    monkeypatch.setattr(packs_module, "DEFAULT_PACK_SLUG", hash_blind.slug)

    plan = _plan("e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855")
    assert plan.subject_type == "hash"
    assert plan.has_applicable_source is False
    assert all(p.applicable_source_count == 0 for p in plan.packs)


def test_a_covered_subject_reports_its_applicable_sources_by_name():
    """The picker says WHAT it will query, not just how many."""
    plan = _plan("CVE-2024-3400")
    pack = next(p for p in plan.packs if p.slug == "web-general")
    assert pack.applicable_source_count == len(pack.applicable_source_names)
    assert "NVD" in pack.applicable_source_names
    assert plan.has_applicable_source is True


def test_the_server_owns_the_ranking():
    """Recommended first, then the widest match — the picker renders the order
    it is given rather than re-sorting."""
    plan = _plan("8.8.8.8")
    keys = [(not p.recommended, -p.applicable_source_count, p.slug) for p in plan.packs]
    assert keys == sorted(keys)


def test_the_subject_is_bounded_and_echoed_trimmed():
    from app.services.deep_investigation_chat import MAX_SUBJECT_LENGTH

    plan = _plan("  " + "A" * (MAX_SUBJECT_LENGTH + 50) + "  ")
    assert len(plan.subject) == MAX_SUBJECT_LENGTH


def test_a_pasted_list_is_planned_whole_as_a_list():
    """Planned as the turn will run it, not as its first 500 characters."""
    from app.services.deep_investigation_chat import MAX_SUBJECT_LENGTH

    text = "\n".join(f"newsroom-number-{i}.com" for i in range(40))
    assert len(text) > MAX_SUBJECT_LENGTH
    plan = _plan(text)
    assert plan.subject_type == "domain_list"
    assert plan.subject == text


def test_a_list_longer_than_one_run_takes_is_a_422():
    from fastapi import HTTPException

    from app.services.investigation_packs import MAX_LIST_SEEDS

    with pytest.raises(HTTPException) as caught:
        _plan("\n".join(f"site{i}.com" for i in range(MAX_LIST_SEEDS + 1)))
    assert caught.value.status_code == 422
    assert caught.value.detail == (
        f"A list investigation takes up to {MAX_LIST_SEEDS} domains. Split the list and try again."
    )


def test_the_literal_route_is_declared_before_the_uuid_route():
    """FastAPI matches in declaration order: registered after the greedy
    `/{investigation_id}`, `/source-packs` would be swallowed by it and
    answered as an invalid-UUID error."""
    from app.api.v1 import investigations as api

    paths = [getattr(r, "path", "") for r in api.router.routes]
    assert paths.index("/source-packs") < paths.index("/{investigation_id}")


def test_the_pickers_strings_exist_in_every_locale():
    """Same guard as the block's, extended to the picker: a missing key falls
    back to the English source string and produces a half-translated dialog."""
    import json
    from pathlib import Path

    frontend = Path(__file__).resolve().parents[2] / "frontend" / "src"
    source = (frontend / "components" / "investigation" / "SourcePackPicker.tsx").read_text()
    keys = set(re.findall(r't_i18n\(\s*\n?\s*"((?:[^"\\]|\\.)*)"', source))
    assert keys, "no translatable strings found — did the picker change shape?"

    for lang in ("en", "fr", "es", "ja"):
        catalog = json.loads((frontend / "i18n" / "lang" / f"{lang}.json").read_text())
        missing = sorted(k for k in keys if k not in catalog)
        assert not missing, f"{lang}.json is missing {missing}"

def test_the_ws_branch_only_owns_the_listener_lifecycle():
    """The turn's own responsibilities moved into the service so they could be
    tested; the branch must not grow them back.

    This is the one claim a behavioural test cannot make — it is about where
    code lives, not what it does — so it stays a source assertion, and it is
    now the ONLY one.
    """
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1] / "app" / "api" / "v1" / "chat.py"
    ).read_text()
    start = source.index("if _turn_deep_investigation:")
    branch = source[start : source.index("if conv_agent_id:", start)]

    assert "run_deep_investigation_turn(" in branch, "the branch must delegate"
    assert "_listen_during_processing()" in branch, (
        "the listener reads frames off THIS socket, so it cannot move"
    )
    for reimplemented in (
        "record_agentic_execution",
        "check_agentic_quota",
        "increment_agentic_usage",
        "QuotaExceededError",
        "EmptySubjectError",
    ):
        assert reimplemented not in branch, (
            f"{reimplemented} belongs to the service, not the handler"
        )


def test_the_deep_investigation_source_is_a_known_metering_source():
    """An unregistered source is still recorded but logged as a warning, and
    would read as an unknown bucket in the adoption analytics."""
    from app.models.agentic_execution import SOURCE_MAX_LENGTH
    from app.services.agentic_executions import (
        KNOWN_SOURCES,
        SOURCE_DEEP_INVESTIGATION,
    )

    assert SOURCE_DEEP_INVESTIGATION in KNOWN_SOURCES
    assert len(SOURCE_DEEP_INVESTIGATION) <= SOURCE_MAX_LENGTH


def _block_source() -> str:
    from pathlib import Path

    return (
        Path(__file__).resolve().parents[2]
        / "frontend"
        / "src"
        / "components"
        / "investigation"
        / "InvestigationBlock.tsx"
    ).read_text()


def _detail_messages_source() -> str:
    from pathlib import Path

    return (
        Path(__file__).resolve().parents[2]
        / "frontend"
        / "src"
        / "components"
        / "investigation"
        / "detail-messages.ts"
    ).read_text()


def _block_detail_messages() -> dict[str, str]:
    """The block's ``DETAIL_MESSAGES`` table (`detail-messages.ts`), as
    ``{code: message}``."""

    source = _detail_messages_source()
    # Bounded by the object literal's own closing `};`, not by the JSDoc that
    # happens to follow it: rewording that comment used to raise `ValueError`
    # inside this helper — an error rather than a readable failure, in three
    # tests at once.
    start = source.index("const DETAIL_MESSAGES")
    table = source[start : source.index("\n};", start)]
    return dict(re.findall(r'"([a-z_.]+)":\s*"((?:[^"\\]|\\.)*)"', table))


def test_every_detail_code_the_backend_emits_has_a_message():
    """A code with no entry falls through to the free-text fallback and
    silently reappears in English — the exact regression the vocabulary exists
    to prevent, and invisible without this check."""
    from app.models.investigation import DETAIL_CODES, UNTRANSLATABLE_DETAIL_CODES

    messages = _block_detail_messages()
    # The untranslatable ones are intentionally absent: their useful content is
    # text a third party wrote, and a translated message would REPLACE the only
    # diagnostic those cases have. The set is declared in the model, not here,
    # so adding one is a decision made where the code is.
    expected = set(DETAIL_CODES) - set(UNTRANSLATABLE_DETAIL_CODES)
    missing = sorted(expected - set(messages))
    assert not missing, f"InvestigationBlock has no message for {missing}"
    unknown = sorted(set(messages) - set(DETAIL_CODES))
    assert not unknown, f"messages for codes the backend never emits: {unknown}"


def test_every_detail_message_exists_in_every_locale():
    import json
    from pathlib import Path

    frontend = Path(__file__).resolve().parents[2] / "frontend" / "src"
    messages = set(_block_detail_messages().values())
    for lang in ("en", "fr", "es", "ja"):
        catalog = json.loads((frontend / "i18n" / "lang" / f"{lang}.json").read_text())
        missing = sorted(m for m in messages if m not in catalog)
        assert not missing, f"{lang}.json is missing {missing}"


def test_every_code_declares_what_it_carries():
    """`DETAIL_CODE_PARAMS` is only a single source of truth if it is total."""
    from app.models.investigation import DETAIL_CODE_PARAMS, DETAIL_CODES

    missing = sorted(set(DETAIL_CODES) - set(DETAIL_CODE_PARAMS))
    extra = sorted(set(DETAIL_CODE_PARAMS) - set(DETAIL_CODES))
    assert not missing, f"codes with no declared params: {missing}"
    assert not extra, f"declared params for codes that do not exist: {extra}"


def test_a_codes_placeholders_match_the_params_the_backend_sends():
    """A message and its params travel together; a renamed placeholder renders
    `{budget}` literally to the user, in every language at once.

    Asserted against the declaration rather than against a regex over the
    service modules. The regex this replaces saw TWO of the nine coded
    messages: the five `run.*` codes assign their params to a local the pattern
    cannot follow, two more are written as conditional expressions, and an
    `if code in emitted` turned every miss into a silent skip. It passed while
    checking almost nothing — including `run.budget_spent`, the example in its
    own docstring.
    """

    from app.models.investigation import (
        DETAIL_CODE_PARAMS,
        UNTRANSLATABLE_DETAIL_CODES,
    )

    messages = _block_detail_messages()
    expected_coded = set(DETAIL_CODE_PARAMS) - set(UNTRANSLATABLE_DETAIL_CODES)
    assert set(messages) == expected_coded, (
        "a code gained or lost its message: "
        f"{sorted(expected_coded ^ set(messages))}"
    )
    for code, message in messages.items():
        placeholders = frozenset(re.findall(r"\{(\w+)\}", message))
        assert placeholders == DETAIL_CODE_PARAMS[code], (
            f"{code}: message wants {sorted(placeholders)}, "
            f"the backend declares {sorted(DETAIL_CODE_PARAMS[code])}"
        )


def _block_detail_clauses() -> dict[str, dict[str, str]]:
    """The block's ``DETAIL_CLAUSE_TABLE``, as ``{code: {count: clause}}``."""
    source = _block_source()
    start = source.index("const DETAIL_CLAUSE_TABLE")
    table = source[start : source.index("} as const;", start)]
    return {
        code: dict(re.findall(r'(\w+):\s*"((?:[^"\\]|\\.)*)"', body))
        for code, body in re.findall(r'"([a-z_.]+)":\s*\{(.*?)\n  \}', table, re.S)
    }


def test_a_codes_optional_counts_each_have_a_clause_in_every_locale():
    """A count a code carries only when it is not zero is said by a clause of
    its own: the clause names that count and nothing else, and every count
    the backend declares as optional has one, in every catalog."""
    import json

    from app.models.investigation import DETAIL_CODE_OPTIONAL_PARAMS, DETAIL_CODE_PARAMS

    clauses = _block_detail_clauses()
    assert set(clauses) == set(DETAIL_CODE_OPTIONAL_PARAMS)
    frontend = Path(__file__).resolve().parents[2] / "frontend" / "src"
    catalogs = {
        lang: json.loads((frontend / "i18n" / "lang" / f"{lang}.json").read_text()) for lang in ("en", "fr", "es", "ja")
    }
    for code, table in clauses.items():
        assert set(table) == DETAIL_CODE_OPTIONAL_PARAMS[code]
        assert not DETAIL_CODE_OPTIONAL_PARAMS[code] & DETAIL_CODE_PARAMS[code]
        for name, clause in table.items():
            assert frozenset(re.findall(r"\{(\w+)\}", clause)) == {name}
            for lang, catalog in catalogs.items():
                assert clause in catalog, f"{lang}.json is missing {clause!r}"
                assert frozenset(re.findall(r"\{(\w+)\}", catalog[clause])) == {name}, (lang, clause)


def test_the_untranslatable_failures_carry_no_code():
    """Their message comes from a third-party exception, so a code would
    promise a translation that cannot exist — they must fall through.

    The count is half the test. The assertion this replaces matched
    `detail=str(exc)[:300]`, a literal that is nowhere in this file — so
    `findall` returned nothing, the loop never ran, and the guard for this
    PR's headline design decision passed while verifying zero lines. A
    FOURTH untranslatable path appearing must fail here rather than be
    quietly ignored.
    """

    from app.services import investigation_queriers as queriers

    source = Path(queriers.__file__).read_text()
    raw = re.findall(r"SourceOutcome\([^)]*detail=str\(exc\)[^)]*\)", source)
    assert len(raw) == 3, (
        f"expected the 3 known untranslatable paths, found {len(raw)} — "
        "a new one needs a decision, not a default"
    )
    for call in raw:
        assert "detail_code" not in call, call


def test_every_subject_type_has_a_label_in_every_locale():
    """`run.no_covering_source` interpolates the subject type into a translated
    sentence, so an unlabelled type produced a snake_case English token in the
    middle of a French one. The badge renders the same labels, so a missing one
    shows in two places at once."""
    import json

    from app.services.investigation_packs import SUBJECT_TYPES

    source = _block_source()
    start = source.index("const SUBJECT_TYPE_LABELS")
    table = source[start : source.index("\n};", start)]
    labels = dict(re.findall(r'^\s*(\w+): "([^"]*)"', table, re.M))

    missing = sorted(set(SUBJECT_TYPES) - set(labels))
    assert not missing, f"subject types with no label: {missing}"
    unknown = sorted(set(labels) - set(SUBJECT_TYPES))
    assert not unknown, f"labels for types the backend never emits: {unknown}"

    frontend = Path(__file__).resolve().parents[2] / "frontend" / "src"
    for lang in ("en", "fr", "es", "ja"):
        catalog = json.loads((frontend / "i18n" / "lang" / f"{lang}.json").read_text())
        absent = sorted(v for v in labels.values() if v not in catalog)
        assert not absent, f"{lang}.json is missing {absent}"


# ── What is NOT asserted here, and why ─────────────────────────────────────
#
# Four tests used to live here that read `ChatInput.tsx` / `SourcePackPicker.
# tsx` as TEXT and asserted on substrings — `source.count("...investigation
# Options,") == 2`, `"if (seededForRef.current === plan.subject) return;" in
# source`. They are gone.
#
# They were false assurance, which is worse than none: they break on a Prettier
# reflow or a rename, and they pass on code that is broken in every way that
# matters. This stack's own argument for extracting the turn into a service was
# that source-text assertions are how four responsibilities went missing
# without a test noticing — encoding four more, across a language boundary, was
# the same mistake with extra steps. One of them would have broken on a
# formatter run in this very stack.
#
# The behaviours they gestured at — the picker never pre-selecting a pack with
# no applicable sources, the mode being consumed once per send, attachments and
# Deep Investigation being mutually exclusive, the picker seeding once per
# subject — are real and worth testing. They need a component-test runner,
# which this frontend does not have (Playwright E2E only, no vitest). That is a
# follow-up to open, not a reason to keep grepping.
#
# `test_the_pickers_strings_exist_in_every_locale` stays: it reads the file to
# extract DATA (which strings need translating) and checks it against the
# catalogues. That is a cross-language consistency check, not a behavioural
# assertion in disguise — and it fails loudly if the extraction stops matching.

# ── A truncated read cannot claim success ───────────────────────────────────
#
# The endpoints cap steps at `MAX_STEPS` and the summary is derived from what
# came back, so a step that errored past the cap was invisible to `summarize`
# and `all_succeeded` returned True for a run that contained failures. That is
# the one affordance this feature exists to keep unreachable, and the cap is
# designed to fire — "it cannot happen today" is not "it cannot happen".


def _run_row():
    from app.models.investigation import Investigation

    return Investigation(
        subject_type="ip",
        subject_value="8.8.8.8",
        source_pack_slugs=[],
        status="completed",
        revision=1,
        iterations_used=1,
        max_iterations=10,
    )


def _steps(n: int, *, last_status: str = "completed"):
    from app.models.investigation import InvestigationStep

    out = [
        InvestigationStep(position=i, source_name=f"s{i}", status="completed",
                          findings_count=1)
        for i in range(n - 1)
    ]
    out.append(
        InvestigationStep(position=n - 1, source_name=f"s{n - 1}",
                          status=last_status, findings_count=1)
    )
    return out


def test_a_truncated_read_never_reports_all_succeeded():
    from app.api.v1.investigations import MAX_STEPS
    from app.services.investigations import serialize_investigation

    # Every step the reader can see succeeded; the one that failed is past the
    # cap. The payload must not claim success on the strength of a sample.
    visible = _steps(MAX_STEPS)
    payload = serialize_investigation(_run_row(), visible, total_steps=MAX_STEPS + 1)

    assert payload["steps_truncated"] is True
    assert payload["summary"]["all_succeeded"] is False, (
        "a sample cannot prove every step succeeded"
    )
    assert payload["summary"]["total"] == MAX_STEPS + 1, (
        "the total must be the run's, not the page's"
    )


def test_an_untruncated_read_is_unaffected():
    from app.services.investigations import serialize_investigation

    payload = serialize_investigation(_run_row(), _steps(3), total_steps=3)
    assert payload["steps_truncated"] is False
    assert payload["summary"]["all_succeeded"] is True
    assert payload["summary"]["total"] == 3


def test_a_short_read_that_raced_a_running_run_is_not_called_truncated():
    """The steps and the aggregate are two statements, and the block polls
    DURING a run. A lead appended between them makes the total one larger than
    the page for a three-step run — nowhere near the cap, and nothing was
    withheld. Truncation is a claim about the CAP, so the cap is what decides
    it: below `page_limit`, more steps means the run grew."""
    from app.api.v1.investigations import MAX_STEPS
    from app.services.investigations import serialize_investigation

    payload = serialize_investigation(
        _run_row(), _steps(3), total_steps=4, page_limit=MAX_STEPS
    )
    assert payload["steps_truncated"] is False, (
        "a three-step page cannot have been cut by a 500-step cap"
    )
    # The total still tells the truth about the run.
    assert payload["summary"]["total"] == 4


def test_a_page_that_filled_the_cap_is_still_called_truncated():
    """The race guard must not swallow the real case it sits next to."""
    from app.api.v1.investigations import MAX_STEPS
    from app.services.investigations import serialize_investigation

    payload = serialize_investigation(
        _run_row(), _steps(MAX_STEPS), total_steps=MAX_STEPS + 1, page_limit=MAX_STEPS
    )
    assert payload["steps_truncated"] is True


def test_a_truncated_read_still_reports_a_failure_it_can_see():
    """Forcing `all_succeeded` false must not mask what the sample does show."""
    from app.api.v1.investigations import MAX_STEPS
    from app.services.investigations import serialize_investigation

    payload = serialize_investigation(
        _run_row(), _steps(MAX_STEPS, last_status="error"), total_steps=MAX_STEPS + 5
    )
    assert payload["summary"]["all_succeeded"] is False
    assert payload["summary"]["failed"] >= 1


def test_the_total_is_optional_so_other_callers_are_unchanged():
    """`total_steps` defaults to None; a caller that holds the whole run — the
    WS completion frame does — keeps the summary it always had."""
    from app.services.investigations import serialize_investigation

    payload = serialize_investigation(_run_row(), _steps(4))
    assert payload["steps_truncated"] is False
    assert payload["summary"]["total"] == 4
    assert payload["summary"]["all_succeeded"] is True
