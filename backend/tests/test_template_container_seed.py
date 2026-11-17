"""The retirement phase of the built-in template container seed.

Phase 3 of ``seed_builtin_template_containers`` MOVES the asset rows of a
retired built-in (the three logo libraries) into the brand deck that
inherits them, then deletes the emptied container. That delete CASCADES onto
``file_attachments``, so these tests lock in the guard around it:

1. A retired container that still has an asset row bound to it after the move
   is KEPT, never deleted with the row - and the warning promises only what the
   next boot delivers: a template asset written meanwhile is moved then, a row
   of any other source (which the re-home never moves) is named for an operator.
2. The re-count is of every bound row, not only the seeded ones, and it runs
   after the move, before the delete, in the same transaction.
3. Decks and reports linked to a retired container are counted and let go free
   (their FK is ``ON DELETE SET NULL``); they are never re-pointed at the heir.
4. A second boot, once the libraries are gone, moves and deletes nothing.
5. The removal half also runs on its own, on every start whatever the AI state:
   an XTM One platform drops the Filigran Brand Deck with every asset
   it holds, a retired library whose heir ships is left to the full seed, and
   boot calls the removal outside the AI gate. The rows go in the removal's
   transaction, their objects only once it committed.

No DB: the session is a dispatching fake that answers each statement by the
table it reads, so the assertions are on the statements the seed emits.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql import Update

import app.services.template_container_seed as seed_mod
from app.models.template_container import TEMPLATE_ASSET_SOURCE, TemplateContainer
from app.services.template_container_seed import (
    BUILTIN_TAG,
    RETIRED_ASSET_HOMES,
    seed_builtin_template_containers,
    slug_tag,
)

# The seed ships the video templates only while the Video Studio is on;
# these tests describe that deployment, where Copilot ships every built-in.
pytestmark = pytest.mark.usefixtures("video_studio_enabled")

HEIR_SLUG = "filigran-brand"
RETIRED_SLUG = "filigran-partner-logos"


def _sql(statement) -> str:
    return str(
        statement.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )


def _table(statement) -> str:
    """The one table a seed statement reads or writes, by name."""
    sql = _sql(statement)
    for table in ("file_attachments", "presentations", "reports", "template_containers"):
        if f"FROM {table}" in sql or f"UPDATE {table}" in sql:
            return table
    raise AssertionError(f"unexpected statement: {sql}")


class _Scalar:
    def __init__(self, value: int):
        self._value = value

    def scalar_one(self):
        return self._value


class _Rows:
    def __init__(self, rows):
        self._rows = list(rows)

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class _Session:
    """Answers the managed-container load, the ``FOR UPDATE`` lock on a retired
    row, the re-home UPDATEs (``rowcount``), the per-source asset count and the
    two COUNTs; records every statement in ``events`` in order. ``bound_after``
    is the number of template asset rows still bound after the move,
    ``foreign_after`` the rows of other sources, ``fail_commit`` a commit that
    raises."""

    def __init__(
        self, managed, *, moved=0, bound_after=0, foreign_after=None, decks=0, reports=0, fail_commit=False
    ):
        self.managed = list(managed)
        self.moved = moved
        self.bound_after = bound_after
        self.foreign_after = dict(foreign_after or {})
        self.decks = decks
        self.reports = reports
        self.fail_commit = fail_commit
        self.statements: list = []
        self.events: list[str] = []
        self.deleted: list = []
        self.added: list = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, statement, *a, **k):
        self.statements.append(statement)
        table = _table(statement)
        if isinstance(statement, Update):
            self.events.append(f"update:{table}")
            return SimpleNamespace(rowcount=self.moved if table == "file_attachments" else 0)
        if "FOR UPDATE" in _sql(statement):
            self.events.append(f"lock:{table}")
            return _Rows([])
        self.events.append(f"select:{table}")
        if table == "template_containers":
            return _Rows(self.managed)
        if table == "file_attachments":
            rows = [(TEMPLATE_ASSET_SOURCE, self.bound_after)] if self.bound_after else []
            rows.extend(self.foreign_after.items())
            return _Rows(rows)
        if table == "presentations":
            return _Scalar(self.decks)
        return _Scalar(self.reports)

    def add(self, obj):
        if getattr(obj, "id", None) is None:
            obj.id = uuid.uuid4()
        self.added.append(obj)

    async def flush(self):
        self.events.append("flush")

    async def delete(self, obj):
        self.events.append(f"delete:{obj.name}")
        self.deleted.append(obj)

    async def commit(self):
        if self.fail_commit:
            raise RuntimeError("commit failed")
        self.events.append("commit")


def _builtin(slug: str, *, name: str, kind: str = "presentation") -> TemplateContainer:
    return TemplateContainer(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        is_company_managed=True,
        name=name,
        description="seeded",
        kind=kind,
        tags=[BUILTIN_TAG, slug_tag(slug)],
        theme=None,
        theme_config={},
        aspect_ratio="16:9",
        layouts=[],
        extra_metadata={},
    )


def _heir() -> TemplateContainer:
    return _builtin(HEIR_SLUG, name="Filigran Brand Deck")


def _retired() -> TemplateContainer:
    return _builtin(RETIRED_SLUG, name="Filigran Partner Logos", kind="asset-library")


def _boot(monkeypatch, session: _Session, *, mode: str = "copilot") -> int:
    async def _owner(_db):
        return uuid.uuid4()

    monkeypatch.setattr(
        "app.api.v1.helpers.resolve_company_managed_owner_id", _owner, raising=False
    )
    return asyncio.run(seed_builtin_template_containers(session, platform_mode=mode))


def test_the_fixture_slugs_are_the_declared_retirement():
    assert RETIRED_ASSET_HOMES[RETIRED_SLUG] == HEIR_SLUG
    assert HEIR_SLUG in {c["slug"] for c in seed_mod.builtin_containers_for("copilot")}


def test_an_emptied_library_is_re_homed_then_deleted_in_that_order(monkeypatch, caplog):
    heir, retired = _heir(), _retired()
    session = _Session([heir, retired], moved=57, bound_after=0)
    with caplog.at_level(logging.INFO, logger=seed_mod.__name__):
        _boot(monkeypatch, session)

    assert session.deleted == [retired]
    moved = [s for s in session.statements if isinstance(s, Update) and _table(s) == "file_attachments"]
    assert moved, "the assets are moved before the container goes"
    first_move = _sql(moved[0])
    assert str(heir.id) in first_move and str(retired.id) in first_move
    # Lock -> move -> re-count -> delete -> commit, and nothing commits in between.
    order = [
        e for e in session.events
        if e.startswith(("lock:", "update:file", "select:file", "delete:", "commit"))
    ]
    assert order.index("lock:template_containers") < order.index("update:file_attachments")
    assert order.index("update:file_attachments") < order.index("select:file_attachments")
    assert order.index("select:file_attachments") < order.index("delete:Filigran Partner Logos")
    assert order.index("delete:Filigran Partner Logos") < order.index("commit")
    assert order.count("commit") == 1
    assert "Re-homed 57 assets" in caplog.text


def test_the_retired_row_is_locked_for_update_before_anything_is_moved_or_counted(monkeypatch):
    """The re-count alone leaves a window between the count and the ``DELETE``
    the ORM flushes at commit. A ``FOR UPDATE`` lock on the container row closes
    it: an insert referencing the container needs ``FOR KEY SHARE`` on that row
    for its FK check, so one in flight blocks the lock until it commits (and the
    move and the count then see it), and one arriving later waits until this
    transaction ends and fails against the deleted container. The lock is on the
    retired row only, is taken once per retired container, and is never taken
    when nothing is retired."""
    heir, retired = _heir(), _retired()
    session = _Session([heir, retired], moved=57, bound_after=0)
    _boot(monkeypatch, session)

    locks = [s for s in session.statements if "FOR UPDATE" in _sql(s)]
    assert len(locks) == 1
    sql = _sql(locks[0])
    assert _table(locks[0]) == "template_containers"
    assert str(retired.id) in sql and str(heir.id) not in sql
    assert session.events.index("lock:template_containers") < session.events.index(
        "update:file_attachments"
    )

    quiet = _Session([_heir()])
    _boot(monkeypatch, quiet)
    assert not any("FOR UPDATE" in _sql(s) for s in quiet.statements)


def test_a_library_that_still_has_a_bound_asset_is_kept_and_retried_next_boot(monkeypatch, caplog):
    """The FK cascades: a row left bound to the container would be destroyed with
    it, object key and all. The seed re-counts after the move and keeps the
    container when anything remains; a template asset written meanwhile is what
    the next boot's re-home moves, so THAT is what the warning promises."""
    heir, retired = _heir(), _retired()
    session = _Session([heir, retired], moved=54, bound_after=3)
    with caplog.at_level(logging.WARNING, logger=seed_mod.__name__):
        _boot(monkeypatch, session)

    assert session.deleted == [], "a non-empty container is never deleted"
    assert "Keeping retired built-in template container Filigran Partner Logos" in caplog.text
    assert "3 template asset row(s)" in caplog.text
    assert "next startup moves them and retires it" in caplog.text
    assert "Detach" not in caplog.text, "a template asset needs no operator"
    # The heir's own sync still lands: the guard skips one delete, not the boot.
    assert "commit" in session.events

    # Next boot: the same rows are found again, the move takes the late rows,
    # the re-count is clean, and the container goes.
    caplog.clear()
    again = _Session([heir, retired], moved=3, bound_after=0)
    with caplog.at_level(logging.INFO, logger=seed_mod.__name__):
        _boot(monkeypatch, again)
    assert again.deleted == [retired]
    assert "Keeping retired" not in caplog.text


def test_a_row_of_another_source_keeps_the_container_and_names_the_operator_step(monkeypatch, caplog):
    """The re-home moves template assets only, so a row of any other source
    bound to the container is one no retry can clear: the same filtered move
    would find it again on every boot. The warning must not promise a retry -
    it names the sources and the count, and what clears it (detach or delete)."""
    heir, retired = _heir(), _retired()
    session = _Session([heir, retired], moved=57, foreign_after={"chat_upload": 2, "kb_document": 1})
    with caplog.at_level(logging.WARNING, logger=seed_mod.__name__):
        _boot(monkeypatch, session)

    assert session.deleted == []
    assert "Keeping retired built-in template container Filigran Partner Logos" in caplog.text
    assert "3 row(s) bound to it are not template assets (2 chat_upload, 1 kb_document)" in caplog.text
    assert "Detach them from the container (or delete them)" in caplog.text
    assert "next startup moves them" not in caplog.text, "no retry can move a foreign row"
    # A mixed leftover is an operator case too: the foreign rows are what blocks.
    caplog.clear()
    mixed = _Session([heir, retired], moved=57, bound_after=1, foreign_after={"chat_upload": 1})
    with caplog.at_level(logging.WARNING, logger=seed_mod.__name__):
        _boot(monkeypatch, mixed)
    assert mixed.deleted == []
    assert "1 row(s) bound to it are not template assets (1 chat_upload)" in caplog.text


def test_the_re_count_covers_every_bound_row_not_only_the_seeded_source(monkeypatch):
    """What the cascade destroys is every row whose FK points at the container,
    whatever its ``source``; the guard must count exactly that set - grouped by
    source so the warning can tell a late template asset from a foreign row."""
    heir, retired = _heir(), _retired()
    session = _Session([heir, retired], moved=57, bound_after=0)
    _boot(monkeypatch, session)

    counts = [
        s for s in session.statements
        if not isinstance(s, Update) and _table(s) == "file_attachments"
    ]
    assert len(counts) == 1
    sql = _sql(counts[0])
    assert "count(" in sql
    assert str(retired.id) in sql
    where = sql.split("WHERE", 1)[1].split("GROUP BY", 1)[0]
    assert "source" not in where, "the guard counts every bound row, not only template_asset ones"
    assert "GROUP BY file_attachments.source" in sql
    # The move itself is scoped to the seeded source, as before.
    (move, *_rest) = [s for s in session.statements if isinstance(s, Update) and _table(s) == "file_attachments"]
    assert "source" in _sql(move)


def test_linked_decks_and_reports_are_counted_and_let_go_free_never_re_pointed(monkeypatch, caplog):
    """A link to a logo library dressed no deck (a non-presentation kind has no
    supported scheme and no chrome logo), and the logos a slide placed are file
    ids the re-home keeps valid - so the deck loses a label, not a look. The FK
    is ``ON DELETE SET NULL``; re-pointing at the heir would restyle a deck in a
    brand its owner never chose, so the seed reports the count and does nothing."""
    heir, retired = _heir(), _retired()
    session = _Session([heir, retired], moved=57, bound_after=0, decks=2, reports=1)
    with caplog.at_level(logging.INFO, logger=seed_mod.__name__):
        _boot(monkeypatch, session)

    assert session.deleted == [retired]
    assert "2 deck(s) and 1 report(s) linked to it" in caplog.text
    assert "ON DELETE SET NULL" in caplog.text
    touched = {_table(s) for s in session.statements if isinstance(s, Update)}
    assert touched == {"file_attachments"}, "nothing re-points a deck or a report"
    # Both counts are scoped to the retired container.
    for table in ("presentations", "reports"):
        (count,) = [s for s in session.statements if not isinstance(s, Update) and _table(s) == table]
        assert str(retired.id) in _sql(count)


def test_no_linked_deck_means_no_retirement_notice(monkeypatch, caplog):
    heir, retired = _heir(), _retired()
    session = _Session([heir, retired], moved=57)
    with caplog.at_level(logging.INFO, logger=seed_mod.__name__):
        _boot(monkeypatch, session)
    assert session.deleted == [retired]
    assert "linked to it" not in caplog.text


def test_a_second_boot_after_the_retirement_moves_and_deletes_nothing(monkeypatch):
    heir = _heir()
    session = _Session([heir, _thread_deck(), _xtm_one_deck(), *_video_templates()])
    inserted = _boot(monkeypatch, session)

    assert inserted == 0
    assert session.deleted == []
    assert not any(isinstance(s, Update) for s in session.statements)
    assert [e for e in session.events if e.startswith("select:")] == ["select:template_containers"]
    # The full-sync of the heir still commits.
    assert session.events[-1] == "commit"


def test_the_retirement_guard_is_the_only_delete_path_of_phase_three():
    """Phase 3 must route every retired container through the guard; a bare
    ``db.delete`` next to the re-home would reintroduce the cascade hazard."""
    import inspect

    source = inspect.getsource(seed_mod.seed_builtin_template_containers)
    assert "Phase 3: re-home" in source
    phase_three = source.split("Phase 3: re-home", 1)[1]
    assert "_retire_emptied_container(db, container)" in phase_three
    assert "db.delete(" not in phase_three


# ── Removal on its own: every start, whatever the AI state ───────────


def _object_key(container_id) -> str:
    return f"template-asset/{container_id}/seed/0a1b2c3d4e5f6a7b-logo.svg"


def _record_purges(monkeypatch, session: _Session | None = None) -> SimpleNamespace:
    """Stand in for the two halves of an asset removal: the row delete, in the
    seed's transaction (``rows``: its filters), which hands back the object keys
    of every row still bound, and the object purge (``objects``: the keys it was
    handed), which runs once the removal is committed. Both land in the
    session's ``events`` when one is given."""
    record = SimpleNamespace(rows=[], objects=[])

    async def _delete_rows(_db, **filters):
        record.rows.append(filters)
        if session is not None:
            session.events.append("delete-rows")
        return [_object_key(filters["template_container_id"])]

    async def _purge_objects(keys):
        keys = list(keys)
        record.objects.append(keys)
        if session is not None:
            session.events.append("purge-objects")
        return len(keys)

    monkeypatch.setattr("app.services.file_cleanup.delete_file_attachment_rows", _delete_rows)
    monkeypatch.setattr("app.services.file_cleanup.purge_storage_objects", _purge_objects)
    return record


def _xtm_one_deck() -> TemplateContainer:
    return _builtin("xtm-one-brand", name="XTM One Brand Deck")


def _thread_deck() -> TemplateContainer:
    return _builtin("thread-brand", name="THREAD Conference Deck")


def _video_templates() -> list[TemplateContainer]:
    """The built-in video templates as a previous Copilot boot left them."""
    return [
        _builtin("filigran-video", name="Filigran Product Video", kind="video"),
        _builtin("thread-video", name="THREAD Conference Video", kind="video"),
        _builtin("xtm-one-video", name="XTM One Product Video", kind="video"),
        _builtin("filigran-brand-film", name="Filigran Brand Film", kind="video"),
        _builtin("filigran-event-loop", name="Filigran Event Loop", kind="video"),
        _builtin("xtm-one-brand-film", name="XTM One Brand Film", kind="video"),
        _builtin("threat-briefing-video", name="Threat Briefing", kind="video"),
        _builtin("social-teaser-video", name="Social Teaser", kind="video"),
    ]


def _remove(session, *, mode: str) -> int:
    return asyncio.run(seed_mod.remove_unshipped_builtin_containers(session, platform_mode=mode))


@pytest.mark.parametrize("mode", ["xtm_one", "dev"])
def test_the_xtm_one_modes_remove_the_filigran_deck_with_every_asset(monkeypatch, caplog, mode):
    """The Filigran Brand Deck carries Filigran's customer logos: an XTM One
    platform drops it with EVERY asset it holds - the purge takes all the rows
    bound to the container, nothing is unbound or moved aside first - and keeps
    the XTM One deck, which ships in every mode."""
    filigran, xtm_one = _heir(), _xtm_one_deck()
    session = _Session([filigran, xtm_one], decks=2)
    purges = _record_purges(monkeypatch)
    with caplog.at_level(logging.INFO, logger=seed_mod.__name__):
        assert _remove(session, mode=mode) == 1

    assert session.deleted == [filigran]
    assert purges.rows == [{"template_container_id": filigran.id}]
    assert purges.objects == [[_object_key(filigran.id)]]
    assert not any(isinstance(s, Update) for s in session.statements), "no asset is kept aside"
    assert session.events[-1] == "commit"
    assert f"which {mode} mode does not ship, with its assets; 2 deck(s)" in caplog.text


def test_copilot_mode_removes_nothing_it_ships(monkeypatch):
    session = _Session([_heir(), _xtm_one_deck()])
    purges = _record_purges(monkeypatch)
    assert _remove(session, mode="copilot") == 0
    assert session.deleted == []
    assert purges.rows == [] and purges.objects == []
    assert "commit" not in session.events


def test_the_removed_deck_s_objects_are_purged_after_the_commit_never_before(monkeypatch):
    """The rows go in the removal's transaction and their objects only once it
    committed: a rollback brings the rows back, and a row whose object is gone
    is a broken picture. It also keeps the object store out of the transaction,
    so no row or table lock waits on an S3 round-trip."""
    filigran, retired = _heir(), _retired()
    session = _Session([_xtm_one_deck(), filigran, retired])
    purges = _record_purges(monkeypatch, session)
    assert _remove(session, mode="xtm_one") == 2

    assert purges.rows == [{"template_container_id": filigran.id}, {"template_container_id": retired.id}]
    assert purges.objects == [[_object_key(filigran.id), _object_key(retired.id)]]
    events = [e for e in session.events if e in {"delete-rows", "commit", "purge-objects"}]
    assert events == ["delete-rows", "delete-rows", "commit", "purge-objects"]


def test_no_object_is_purged_when_the_removal_does_not_commit(monkeypatch):
    session = _Session([_xtm_one_deck(), _heir()], fail_commit=True)
    purges = _record_purges(monkeypatch, session)
    with pytest.raises(RuntimeError, match="commit failed"):
        _remove(session, mode="xtm_one")
    assert purges.rows and purges.objects == []


def test_the_full_seed_purges_the_objects_of_what_its_phase_one_removed_after_its_commit(monkeypatch):
    """The full seed removes an unshipped built-in in its Phase 1 too (a mode
    switched while the platform ran, the runtime AI toggle): same rule, the
    objects go after the seed's own commit."""
    filigran = _heir()
    session = _Session([filigran, _xtm_one_deck()])
    purges = _record_purges(monkeypatch, session)
    _boot(monkeypatch, session, mode="xtm_one")

    assert session.deleted == [filigran]
    assert purges.objects == [[_object_key(filigran.id)]]
    assert session.events[-2:] == ["commit", "purge-objects"]


def test_the_removal_leaves_a_retired_library_to_the_full_seed_where_its_heir_ships(monkeypatch):
    """In Copilot mode the logo libraries' heir ships, so their assets MOVE into
    it (Phase 3 of the full seed): the standalone removal must not purge them
    first."""
    session = _Session([_heir(), _retired()])
    purges = _record_purges(monkeypatch)
    assert _remove(session, mode="copilot") == 0
    assert session.deleted == []
    assert purges.rows == [] and purges.objects == []


def test_the_xtm_one_modes_remove_the_heirless_logo_libraries_with_their_logos(monkeypatch):
    retired = _retired()
    session = _Session([_xtm_one_deck(), retired])
    purges = _record_purges(monkeypatch)
    assert _remove(session, mode="xtm_one") == 1
    assert session.deleted == [retired]
    assert purges.rows == [{"template_container_id": retired.id}]
    assert purges.objects == [[_object_key(retired.id)]]


def test_boot_runs_the_removal_outside_the_ai_gate():
    """``provision_builtin_content`` - and with it the full seed - only runs
    when AI is on. The removal must not depend on it: an XTM One platform whose
    AI is switched off still loses the Filigran deck on its next start."""
    import inspect

    from app import main

    source = inspect.getsource(main._bootstrap_platform_data)
    call = source.index("remove_unshipped_builtin_containers(db, platform_mode=content_mode())")
    assert call < source.index("if _ai_on:")


# -- The guarantee: nothing of the Filigran deck survives a non-Copilot start --


@pytest.mark.parametrize("mode", ["xtm_one", "dev", "ai_disabled"])
def test_tripwire_the_filigran_deck_goes_whatever_still_uses_it(monkeypatch, mode):
    """Tripwire for the product decision: outside Copilot mode the
    Filigran Brand Deck is removed with every asset row and every stored
    object, however many decks and reports still link it. There is no
    keep-while-used rule - its assets are Filigran's customer and partner
    logos and team photos - so a future change that keeps it for its users
    fails here. The decks, reports and the default-template setting let go of
    it through their ``ON DELETE SET NULL`` key; a snapshot still naming one
    of its files never refuses a later write (``verify_file_references``)."""
    from app.models.file_attachment import FileAttachment
    from app.models.platform_settings import PlatformSettings
    from app.models.presentation import Presentation
    from app.models.report import Report
    from app.services.ai_state import content_mode

    monkeypatch.setattr("app.services.ai_state.settings.platform_mode", mode)
    filigran = _heir()
    session = _Session([filigran, _xtm_one_deck()], decks=12, reports=4)
    purges = _record_purges(monkeypatch, session)
    assert _remove(session, mode=content_mode()) == 1
    assert session.deleted == [filigran]
    assert purges.rows == [{"template_container_id": filigran.id}]
    assert purges.objects == [[_object_key(filigran.id)]]

    def _ondelete(column) -> str:
        (fk,) = column.property.columns[0].foreign_keys
        return fk.ondelete

    assert _ondelete(Presentation.template_container_id) == "SET NULL"
    assert _ondelete(Report.template_container_id) == "SET NULL"
    assert _ondelete(PlatformSettings.default_presentation_template_id) == "SET NULL"
    assert _ondelete(FileAttachment.template_container_id) == "CASCADE"
    assert "filigran-brand" not in {c["slug"] for c in seed_mod.builtin_containers_for(content_mode())}


def test_only_copilot_ships_the_filigran_deck():
    targets = {c["slug"]: c["platform_targets"] for c in seed_mod.BUILTIN_TEMPLATE_CONTAINERS}
    assert targets["filigran-brand"] == ["copilot"]
    # The THREAD deck includes the Filigran packs (customer and partner
    # logos, team photos): it ships where the Filigran deck does, nowhere else.
    assert targets["thread-brand"] == ["copilot"]
    assert targets["xtm-one-brand"] == ["global"]


@pytest.mark.parametrize("mode", ["xtm_one", "dev"])
def test_the_xtm_one_modes_remove_the_thread_deck_with_the_filigran_one(monkeypatch, mode):
    filigran, thread, xtm_one = _heir(), _thread_deck(), _xtm_one_deck()
    session = _Session([filigran, thread, xtm_one])
    purges = _record_purges(monkeypatch)
    assert _remove(session, mode=mode) == 2
    assert session.deleted == [filigran, thread]
    assert purges.rows == [{"template_container_id": filigran.id}, {"template_container_id": thread.id}]


def test_the_xtm_one_deck_names_none_of_the_filigran_deck_s_asset_families():
    """Its guidance is what an agent reads on every platform: it sends the
    agent to the product figures the deck ships, and never describes
    the logo sets, portraits or badges only the Copilot deck carries."""
    xtm_one = next(c for c in seed_mod.BUILTIN_TEMPLATE_CONTAINERS if c["slug"] == "xtm-one-brand")
    guidance = xtm_one["extra_metadata"]["guidance"]
    for word in ("PREDEFINED", "`product_logo`", "`diagram`", "`screenshot`"):
        assert word in guidance, word
    # Its own logo and mark win over the Filigran pack's files of the same
    # name and hold the chrome roles: a `product_logo` listing never shows them.
    assert "`logo` and `logo_mark` assets" in guidance
    for word in ("customer_logo", "partner_logo", "integration_logo", "people_photo", "`badge`"):
        assert word not in guidance, word


class _SweepSession:
    """Answers the sweep's two reads: which containers still exist, which keys a row names."""

    def __init__(self, existing: set, named: set, log: list[str]):
        self.existing, self.named, self.log = existing, named, log

    async def __aenter__(self):
        self.log.append("db:open")
        return self

    async def __aexit__(self, *exc):
        self.log.append("db:close")
        return False

    async def execute(self, statement, *a, **k):
        table = _table(statement)
        self.log.append(f"db:{table}")
        if table == "template_containers":
            return _Rows(list(self.existing))
        return _Rows(list(self.named))


class _FakeStore:
    """An object store with S3 listing semantics: recursive, or one level with
    each sub-prefix listed once (ending in ``/``). ``down`` fails every call the
    way ``file_storage`` does: a listing answers ``[]``, a delete raises."""

    def __init__(self, keys, log: list[str], *, down: bool = False):
        self.keys, self.log, self.down = set(keys), log, down
        self.deleted: list[str] = []

    async def list_object_names(self, prefix, *, recursive=True):
        self.log.append(f"list:{prefix}:{'all' if recursive else 'level'}")
        if self.down:
            return []
        under = sorted(key for key in self.keys if key.startswith(prefix))
        if recursive:
            return under
        level: list[str] = []
        for key in under:
            head, slash, _rest = key[len(prefix) :].partition("/")
            entry = f"{prefix}{head}{slash}"
            if entry not in level:
                level.append(entry)
        return level

    async def delete(self, key):
        self.log.append(f"delete:{key}")
        if self.down:
            raise ConnectionError("store unreachable")
        self.keys.discard(key)
        self.deleted.append(key)


def _sweep(monkeypatch, keys, *, existing=(), named=(), mode="xtm_one", down=False):
    """Run the REAL sweep and the REAL ``purge_storage_objects`` against a fake store."""
    from app.services import template_asset_seed
    from app.services.file_storage import file_storage

    log: list[str] = []
    store = _FakeStore(keys, log, down=down)
    monkeypatch.setattr(file_storage, "list_object_names", store.list_object_names)
    monkeypatch.setattr(file_storage, "delete", store.delete)
    monkeypatch.setattr(
        "app.core.database.async_session", lambda: _SweepSession(set(existing), set(named), log)
    )
    count = asyncio.run(template_asset_seed.purge_orphaned_seed_objects(platform_mode=mode))
    return count, store, log


def test_the_sweep_deletes_what_a_failed_purge_left_under_a_removed_container(monkeypatch):
    """The store was down when the removal committed, or the process died
    before the purge: the objects no row names under a container that no longer
    exists are found in the bucket and deleted - every version and filename,
    the re-homed libraries' keys included."""
    removed, library, kept = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    orphans = [
        f"template-asset/{removed}/seed/0a1b2c3d4e5f6a7b-customer.png",
        f"template-asset/{removed}/seed/ffffffffffffffff-customer.png",
        f"template-asset/{library}/seed/1111111111111111-partner.png",
    ]
    kept_keys = [
        f"template-asset/{kept}/seed/2222222222222222-xtm-one-logo.svg",
        f"template-asset/{kept}/3f2a.png",
        f"template-asset/{removed}/3f2a.png",
    ]
    count, store, _log = _sweep(monkeypatch, orphans + kept_keys, existing={kept})
    assert count == 3
    assert sorted(store.deleted) == sorted(orphans)
    assert store.keys == set(kept_keys)


def test_the_sweep_keeps_an_object_a_row_still_names(monkeypatch):
    removed = uuid.uuid4()
    named = f"template-asset/{removed}/seed/0a1b2c3d4e5f6a7b-customer.png"
    orphan = f"template-asset/{removed}/seed/1111111111111111-other.png"
    count, store, _log = _sweep(monkeypatch, [named, orphan], named={named})
    assert count == 1 and store.deleted == [orphan]


def test_the_sweep_only_deletes_the_exact_key_shape_the_seed_writes(monkeypatch):
    removed = uuid.uuid4()
    odd = [
        f"template-asset/{removed}/extra/seed/0a1b2c3d4e5f6a7b-customer.png",
        f"template-asset/{removed.hex}/seed/0a1b2c3d4e5f6a7b-customer.png",
        f"template-asset/{removed}/seed/nothex-customer.png",
        f"template-asset/{removed}/seed/0a1b2c3d4e5f6a7b-dir/customer.png",
        f"template-asset/{str(removed).upper()}/seed/0a1b2c3d4e5f6a7b-customer.png",
    ]
    count, store, _log = _sweep(monkeypatch, odd)
    assert count == 0 and store.deleted == []


def test_the_sweep_lists_only_the_seed_group_of_a_container_that_is_gone(monkeypatch):
    """Bounded: one level of ``template-asset/``, then only the ``seed/`` group
    of a gone container - the assets of the templates that exist are never
    listed, whatever their number - and no database session is open while the
    store is called."""
    removed = uuid.uuid4()
    kept = [uuid.uuid4() for _ in range(3)]
    keys = [f"template-asset/{cid}/{n:032x}.png" for cid in kept for n in range(50)]
    keys.append(f"template-asset/{removed}/seed/0a1b2c3d4e5f6a7b-customer.png")
    count, _store, log = _sweep(monkeypatch, keys, existing=set(kept))
    assert count == 1
    listings = [entry for entry in log if entry.startswith("list:")]
    assert listings == ["list:template-asset/:level", f"list:template-asset/{removed}/seed/:all"]
    open_session = False
    for entry in log:
        if entry == "db:open":
            open_session = True
        elif entry == "db:close":
            open_session = False
        elif entry.startswith(("list:", "delete:")):
            assert not open_session, f"{entry} while a session is open"


def test_the_sweep_deletes_nothing_when_the_store_is_down(monkeypatch):
    removed = uuid.uuid4()
    count, store, log = _sweep(monkeypatch, [f"template-asset/{removed}/seed/0a1b2c3d4e5f6a7b-x.png"], down=True)
    assert count == 0 and store.deleted == []
    assert not any(entry.startswith("db:") for entry in log)


def test_a_store_that_stops_answering_mid_purge_leaves_the_rest_for_the_next_hour(monkeypatch):
    from app.services import template_asset_seed
    from app.services.file_storage import file_storage

    removed = uuid.uuid4()
    keys = [f"template-asset/{removed}/seed/{n:016x}-x.png" for n in range(3)]
    log: list[str] = []
    store = _FakeStore(keys, log)
    calls = {"n": 0}

    async def _flaky_delete(key):
        calls["n"] += 1
        if calls["n"] == 2:
            raise ConnectionError("store unreachable")
        await store.delete(key)

    monkeypatch.setattr(file_storage, "list_object_names", store.list_object_names)
    monkeypatch.setattr(file_storage, "delete", _flaky_delete)
    monkeypatch.setattr("app.core.database.async_session", lambda: _SweepSession(set(), set(), log))
    count = asyncio.run(template_asset_seed.purge_orphaned_seed_objects(platform_mode="xtm_one"))
    assert count == 1 and len(store.keys) == 2


def test_copilot_mode_sweeps_nothing_and_calls_nothing(monkeypatch):
    count, store, log = _sweep(
        monkeypatch, [f"template-asset/{uuid.uuid4()}/seed/0a1b2c3d4e5f6a7b-x.png"], mode="copilot"
    )
    assert count == 0 and store.deleted == [] and log == []


class _FakeRedis:
    def __init__(self, *, held: bool = False, broken: bool = False):
        self.held, self.broken, self.calls = held, broken, []

    async def set(self, key, value, *, nx=False, ex=None):
        self.calls.append((key, nx, ex))
        if self.broken:
            raise ConnectionError("redis down")
        if self.held:
            return None
        self.held = True
        return True


def _sweep_if_due(monkeypatch, redis, *, mode="xtm_one"):
    from app.services import template_asset_seed

    runs: list[str] = []

    async def _redis_client():
        return redis

    async def _purge(*, platform_mode):
        runs.append(platform_mode)
        return 7

    monkeypatch.setattr("app.core.redis.redis_client", _redis_client)
    monkeypatch.setattr(template_asset_seed, "purge_orphaned_seed_objects", _purge)
    count = asyncio.run(template_asset_seed.purge_orphaned_seed_objects_if_due(platform_mode=mode))
    return count, runs


def test_the_sweep_runs_once_per_interval_fleet_wide_whichever_process_asks():
    """A Redis lease decides, not the per-process tick counter: a worker that
    wins the tick lock rarely, or restarts often, never stretches the retry."""
    from app.services.template_asset_seed import ORPHAN_SWEEP_INTERVAL_SECS

    monkeypatch = pytest.MonkeyPatch()
    redis = _FakeRedis()
    try:
        assert _sweep_if_due(monkeypatch, redis) == (7, ["xtm_one"])
        assert _sweep_if_due(monkeypatch, redis) == (0, [])
    finally:
        monkeypatch.undo()
    (key, nx, ex), _second = redis.calls
    assert key == "template_asset_seed:orphan_sweep"
    assert nx is True and ex == ORPHAN_SWEEP_INTERVAL_SECS == 3600


def test_the_sweep_skips_a_tick_whose_lease_cannot_be_taken(monkeypatch):
    assert _sweep_if_due(monkeypatch, _FakeRedis(broken=True)) == (0, [])


def test_copilot_mode_never_asks_for_the_sweep_lease(monkeypatch):
    redis = _FakeRedis()
    assert _sweep_if_due(monkeypatch, redis, mode="copilot") == (0, [])
    assert redis.calls == []


def test_the_scheduler_asks_for_the_sweep_on_every_tick_outside_its_session():
    """Every tick asks (the lease decides), outside the hourly counter block,
    after the tick's own session closed - so no transaction of the tick is open
    while the store is called - and whatever the AI state."""
    import inspect

    from app.services import autonomous

    source = inspect.getsource(autonomous._run_autonomous_tick_inner)
    call = source.index("purge_orphaned_seed_objects_if_due(platform_mode=content_mode())")
    assert "purge_orphaned_seed_objects(" not in source.replace("purge_orphaned_seed_objects_if_due(", "")
    line_start = source.rindex("\n", 0, call) + 1
    indent = len(source[line_start:call]) - len(source[line_start:call].lstrip())
    assert indent == 8, "the call belongs to the function body's try, not the session block"
    assert source.index("async with async_session() as db:") < source.index("# --- 12. Usage telemetry") < call
    assert "_ai_on" not in source[call - 600 : call]
