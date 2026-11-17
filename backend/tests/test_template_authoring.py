"""Tests for the agent-facing template WRITE surface.

Template lookup is read-only on purpose. This is the write half, and
what has to be locked in is not "the row was inserted" — it is the set of
refusals that keep an agent from producing a template that *looks* saved and
silently does nothing:

1. A ``theme_config`` key the renderer ignores is reported, not stored.
2. A layout id that does not exist is refused, not advertised.
3. A built-in (code-managed) container is never edited — the next deploy would
   overwrite the change — the agent is told to duplicate it instead.
4. An agent never publishes a template org-wide, whatever the actor's role.
5. A file the acting user cannot read cannot be laundered into a container.

No DB required: the tool closures open their own sessions, which are
monkeypatched with capturing fakes, mirroring ``test_template_containers.py``.
"""

from __future__ import annotations

import ast
import asyncio
import copy
import hashlib
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

import app.services.template_containers as tpl_service
from app.models.template_container import MAX_ASSETS_PER_CONTAINER, TemplateContainer

BACKEND_ROOT = Path(__file__).resolve().parents[1]


# ── Fakes ─────────────────────────────────────────────────────────────────────


class _Result:
    def __init__(self, one=None, rows=None):
        self._one = one
        self._rows = rows or []

    def scalars(self):
        return self

    def all(self):
        return self._rows

    def scalar_one_or_none(self):
        return self._one


class _FakeSession:
    """Async-context session that records writes and fakes column defaults."""

    def __init__(self, results=None):
        self._results = list(results or [])
        self.added: list = []
        self.deleted: list = []
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, statement, *a, **k):
        return self._results.pop(0) if self._results else _Result()

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        # The PK default is applied by the INSERT in a real session; the tools
        # read ``container.id`` straight after flushing, so the fake has to
        # honour that contract or every read-back asserts against ``None``.
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = uuid.uuid4()

    async def commit(self):
        self.committed = True

    async def refresh(self, *a, **k):
        return None

    async def delete(self, obj):
        self.deleted.append(obj)


def _user(**overrides) -> SimpleNamespace:
    base = dict(
        id=uuid.uuid4(), role="user", full_name="Dana Lee", is_admin=False
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _container(**overrides) -> TemplateContainer:
    """A real ORM object — the tools mutate it, so a namespace would not do."""
    container = TemplateContainer(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        name="Acme Deck",
        description="Acme brand deck",
        kind="presentation",
        tags=["acme", "brand"],
        theme="acme-dark",
        theme_config={"background": "#0b1020", "accent": "#00b4ff"},
        aspect_ratio="16:9",
        layouts=[{"id": "title", "name": "Title"}],
        extra_metadata={},
        is_company_managed=False,
    )
    for key, value in overrides.items():
        setattr(container, key, value)
    return container


def _asset(**overrides) -> SimpleNamespace:
    base = dict(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        filename="acme-logo.png",
        content_type="image/png",
        size=4096,
        asset_role="logo",
        s3_object_name="chat/xyz/acme-logo.png",
        conversation_id=uuid.uuid4(),
        assignment_run_id=None,
        template_container_id=None,
        source="chat",
        created_at=datetime.now(UTC),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class _Harness:
    """Everything a tool call touched, so a test can assert on the writes."""

    def __init__(self):
        self.session = _FakeSession()
        self.audits: list[dict] = []
        self.deleted_attachment_filters: list[dict] = []
        self.uploads: list[dict] = []


def _build(
    monkeypatch,
    *,
    actor=None,
    container=None,
    manage_error=None,
    assets=None,
    source_file=None,
    file_access_error=None,
) -> tuple[object, _Harness]:
    """Build the authoring executor with its collaborators faked out."""
    from fastapi import HTTPException

    from app.services.builtin_tools import build_template_authoring_tools

    actor = actor if actor is not None else _user()
    harness = _Harness()
    # First ``execute`` in every closure resolves the acting user; the second
    # (attach only) resolves the source file.
    harness.session._results = [_Result(one=actor)]
    if source_file is not None:
        harness.session._results.append(_Result(one=source_file))

    monkeypatch.setattr(
        "app.core.database.async_session", lambda: harness.session, raising=False
    )

    async def _fake_manage(db, model_cls, user, entity_id, **kwargs):
        if manage_error is not None:
            raise HTTPException(status_code=403, detail=manage_error)
        if container is None:
            raise HTTPException(status_code=404, detail="Template container not found")
        return container

    monkeypatch.setattr(
        "app.api.v1.helpers.get_manageable_entity", _fake_manage, raising=False
    )

    async def _fake_audit(db, **kwargs):
        harness.audits.append(kwargs)
        return None

    monkeypatch.setattr("app.services.audit.log_audit", _fake_audit, raising=False)

    async def _fake_load_assets(db, container_id):
        return list(assets or [])

    monkeypatch.setattr(tpl_service, "load_assets", _fake_load_assets)

    async def _fake_counts(db, ids):
        return {str(i): len(assets or []) for i in ids}

    monkeypatch.setattr(tpl_service, "count_assets_by_container", _fake_counts)

    async def _fake_delete_attachments(db, **filters):
        harness.deleted_attachment_filters.append(filters)
        return len(assets or [])

    monkeypatch.setattr(
        "app.services.file_cleanup.delete_file_attachments",
        _fake_delete_attachments,
        raising=False,
    )

    async def _fake_authorize(file_att, user, db):
        if file_access_error:
            raise HTTPException(status_code=403, detail="Access denied")

    monkeypatch.setattr(
        "app.services.file_access.authorize_file_access",
        _fake_authorize,
        raising=False,
    )

    class _Storage:
        async def download(self, object_name):
            harness.uploads.append({"downloaded": object_name})
            return b"binary-bytes"

        async def upload(self, *, data, filename, content_type, prefix):
            harness.uploads.append(
                {"filename": filename, "prefix": prefix, "size": len(data)}
            )
            return f"{prefix}/{filename}"

    monkeypatch.setattr(
        "app.services.file_storage.file_storage", _Storage(), raising=False
    )

    _defs, executor = build_template_authoring_tools(
        db=None,  # type: ignore[arg-type]
        user_id=str(actor.id),
    )
    return executor, harness


def _call(executor, name: str, arguments: dict) -> str:
    from app.tools.base import ToolCall

    result = asyncio.run(
        executor.execute_tool(ToolCall(id="call-1", name=name, arguments=arguments))
    )
    return result.content


def _tool_defs() -> dict:
    from app.services.builtin_tools import build_template_authoring_tools

    definitions, _executor = build_template_authoring_tools(
        db=None,  # type: ignore[arg-type]
        user_id="",
    )
    return {d.name: d for d in definitions}


# ── Registration ──────────────────────────────────────────────────────────────


def test_template_authoring_is_a_recognized_builtin_tool_id():
    """Otherwise the agent-builder tool would reject a valid configuration."""
    from app.services.builtin_tools.agent_builder import _VALID_BUILTIN_TOOL_IDS

    assert "template_authoring" in _VALID_BUILTIN_TOOL_IDS


def test_authoring_is_a_separate_id_from_lookup():
    """Reading templates must not imply being able to rewrite them."""
    read_names = {"search_templates", "get_template"}
    assert read_names.isdisjoint(_tool_defs())


def test_builtin_tool_catalog_advertises_the_authoring_sub_tools():
    from app.services.builtin_tool_catalog import build_builtin_tool_member_catalog

    # The builder keeps its own id in the member catalog; the registry lists it under ``design``.
    catalog = build_builtin_tool_member_catalog()
    assert [entry["name"] for entry in catalog["template_authoring"]] == [
        "create_template",
        "update_template",
        "delete_template",
        "attach_template_asset",
        "detach_template_asset",
        "validate_presentation",
    ]


def test_every_write_tool_is_dispatched_on_the_new_id():
    """All three runtimes must gate the builder, or an agent silently loses it."""
    for rel_path in (
        "app/services/agent_chat.py",
        "app/services/assignment_engine.py",
        "app/services/general_chat.py",
    ):
        source = (BACKEND_ROOT / rel_path).read_text(encoding="utf-8")
        assert "template_authoring" in source, rel_path
        assert "build_template_authoring_tools" in source, rel_path


def test_tool_descriptions_carry_the_machine_truth_not_prose():
    """The whitelists are interpolated from the constants that enforce them."""
    from app.services.presentation_spec import SLIDE_LAYOUTS
    from app.services.presentations import THEME_CONFIG_KEYS

    description = _tool_defs()["create_template"].description
    for key in THEME_CONFIG_KEYS:
        assert key in description
    for layout in SLIDE_LAYOUTS:
        assert layout in description


# ── AC: a template is created and its id reported ─────────────────────────────


def test_create_template_writes_a_presentation_container_and_reports_its_id(
    monkeypatch,
):
    actor = _user()
    executor, harness = _build(monkeypatch, actor=actor)

    out = _call(
        executor,
        "create_template",
        {
            "name": "Acme Brand Deck",
            "tags": " Acme , Brand ",
            "theme": "acme-dark",
            "theme_config": {"background": "#0b1020", "accent": "#00b4ff"},
            "aspect_ratio": "16:9",
            "layouts": [{"id": "title", "name": "Title"}],
        },
    )

    (written,) = harness.session.added
    assert isinstance(written, TemplateContainer)
    assert written.kind == "presentation"
    assert written.user_id == actor.id
    assert written.tags == ["acme", "brand"]  # normalised for the search filter
    # The legacy free-text ``theme`` label is ignored on the agent path:
    # the deck themes a template supports are derived from its palette, so a
    # stale model still passing the argument writes no label.
    assert written.theme is None
    assert "deck themes supported: dark only" in out
    assert str(written.id) in out
    assert harness.session.committed
    # The whole point of the feature is the preview loop, so the tool result
    # has to push the agent into it rather than stop at "created".
    assert "generate_presentation" in out


def test_create_template_tells_the_agent_to_show_a_deck_not_hex_values(monkeypatch):
    executor, _h = _build(monkeypatch)
    out = _call(executor, "create_template", {"name": "Acme"})
    assert "presentation_id" in out


def test_create_template_accepts_a_json_string_for_an_object_parameter(monkeypatch):
    """LLMs regularly hand a JSON string to an object-typed parameter."""
    executor, harness = _build(monkeypatch)
    out = _call(
        executor,
        "create_template",
        {"name": "Acme", "theme_config": '{"accent": "#00b4ff"}'},
    )
    (written,) = harness.session.added
    assert written.theme_config == {"accent": "#00b4ff"}
    assert "Created template" in out


def test_create_template_requires_a_name(monkeypatch):
    executor, harness = _build(monkeypatch)
    out = _call(executor, "create_template", {"name": "   "})
    assert "name is required" in out
    assert harness.session.added == []


def test_an_anonymous_caller_cannot_author_a_template(monkeypatch):
    from app.services.builtin_tools import build_template_authoring_tools

    def _no_session():
        raise AssertionError("must not open a session for an anonymous caller")

    monkeypatch.setattr("app.core.database.async_session", _no_session, raising=False)
    _defs, executor = build_template_authoring_tools(
        db=None,  # type: ignore[arg-type]
        user_id="anonymous",
    )
    out = _call(executor, "create_template", {"name": "Acme"})
    assert "signed-in user" in out


# ── AC: only theme keys the renderer honours ──────────────────────────────────


def test_a_theme_config_key_outside_the_whitelist_is_refused_not_stored(monkeypatch):
    executor, harness = _build(monkeypatch)

    out = _call(
        executor,
        "create_template",
        {"name": "Acme", "theme_config": {"accent": "#00b4ff", "sidebar": "#fff"}},
    )

    assert harness.session.added == [], "nothing may be written on a refusal"
    assert "NOT created" in out
    assert "sidebar" in out
    # The refusal names the keys that DO work, so the retry can succeed.
    assert "background" in out and "heading_font" in out


def test_a_css_escaping_theme_value_is_refused(monkeypatch):
    executor, harness = _build(monkeypatch)
    out = _call(
        executor,
        "create_template",
        {"name": "Acme", "theme_config": {"accent": "red; } body { display:none"}},
    )
    assert harness.session.added == []
    assert "not a plain colour or font-family token" in out


def test_a_theme_value_that_loads_a_url_is_refused_by_name(monkeypatch):
    """An image function needs no ``url(`` and no colon to make every
    viewer's browser fetch an outside address. The refusal names the key and
    the function, on create and on update, and nothing is written."""
    executor, harness = _build(monkeypatch)
    out = _call(
        executor,
        "create_template",
        {
            "name": "Acme",
            "theme_config": {"accent": "#00b4ff", "background": "image-set('//example.com/x.png' 1x)"},
        },
    )
    assert harness.session.added == []
    assert "NOT created" in out
    assert "theme_config['background']" in out and "image-set()" in out

    container = _container(theme_config={"background": "#0b1020"}, extra_metadata={})
    executor, harness = _build(monkeypatch, container=container)
    out = _call(
        executor,
        "update_template",
        {
            "template_id": str(container.id),
            "extra_metadata": {"theme_variants": {"light": {"surface": "-webkit-cross-fade('//e.com/a.png', '//e.com/b.png', 50%)"}}},
        },
    )
    assert not harness.session.committed
    assert "was NOT modified" in out
    assert "extra_metadata.theme_variants.light['surface']" in out and "-webkit-cross-fade()" in out
    assert container.extra_metadata == {}


def test_the_whitelist_comes_from_the_renderer_constant_not_a_copy():
    """A key added to THEME_CONFIG_KEYS must become accepted with no other edit.

    File-id keys are typed — they hold the UUID of an asset, not a CSS
    token — so each key is fed a value of its own kind.
    """
    import uuid

    from app.services.presentations import THEME_CONFIG_KEYS, THEME_FILE_ID_KEYS
    from app.services.template_containers import validate_theme_config

    proposed = {
        k: (str(uuid.uuid4()) if k in THEME_FILE_ID_KEYS else "#000000")
        for k in THEME_CONFIG_KEYS
    }
    accepted, problems = validate_theme_config(proposed)
    assert set(accepted) == set(THEME_CONFIG_KEYS)
    assert problems == []


def test_a_file_id_theme_key_refuses_a_non_uuid_with_a_pointer_to_assets():
    """A free-form value here would become a download URL — refuse it, explain."""
    from app.services.template_containers import validate_theme_config

    accepted, problems = validate_theme_config({"logo_file_id": "../etc/passwd"})
    assert accepted == {}
    assert len(problems) == 1
    assert "UUID" in problems[0] and "'logo'" in problems[0]


# ── AC: every advertised layout id exists ─────────────────────────────────────


def test_an_invented_layout_id_is_refused(monkeypatch):
    executor, harness = _build(monkeypatch)

    out = _call(
        executor,
        "create_template",
        {"name": "Acme", "layouts": [{"id": "hero_banner", "name": "Hero"}]},
    )

    assert harness.session.added == []
    assert "hero_banner" in out
    assert "the validator refuses every slide built with it" in out  # says WHY it matters, not just "invalid"


def test_a_retired_layout_id_is_dropped_without_a_word(monkeypatch):
    """The four v1 ids are a stored value the platform outgrew, not an
    author's mistake: an agent re-saving an older template's catalog is not
    told off for them, they just stop being advertised."""
    from app.services.presentation_legacy import RETIRED_LAYOUTS

    executor, harness = _build(monkeypatch)
    out = _call(
        executor,
        "create_template",
        {"name": "Acme", "layouts": [{"id": "title"}, *({"id": retired} for retired in RETIRED_LAYOUTS)]},
    )

    (written,) = harness.session.added
    assert [layout["id"] for layout in written.layouts] == ["title"]
    for retired in RETIRED_LAYOUTS:
        assert retired not in out


def test_a_valid_layout_gets_its_fields_derived_from_the_spec_models(monkeypatch):
    """A container must not be able to advertise a field the validator rejects."""
    from app.services.presentation_spec import layout_catalog

    executor, harness = _build(monkeypatch)
    _call(
        executor,
        "create_template",
        {
            "name": "Acme",
            "layouts": [{"id": "content", "name": "Body", "fields": ["made_up"]}],
        },
    )

    (written,) = harness.session.added
    assert written.layouts[0]["fields"] == layout_catalog()["content"]


def test_report_layout_ids_are_enforced_against_the_block_catalog(monkeypatch):
    """Reports have a renderer contract, so their layout ids are
    checked exactly like a deck's — against ``REPORT_BLOCKS``."""
    executor, harness = _build(monkeypatch)
    out = _call(
        executor,
        "create_template",
        {"name": "Acme Report", "kind": "report", "layouts": [{"id": "cover_page"}]},
    )
    assert harness.session.added == []
    assert "cover_page" in out
    assert "renders nothing for that section" in out  # says WHY, not just "invalid"

    executor, harness = _build(monkeypatch)
    _call(
        executor,
        "create_template",
        {"name": "Acme Report", "kind": "report", "layouts": [{"id": "risk_table"}]},
    )
    (written,) = harness.session.added
    assert written.kind == "report"
    assert written.layouts[0]["id"] == "risk_table"


def test_a_kind_without_a_renderer_is_refused_with_the_closed_choice(monkeypatch):
    """A template dresses a presentation or a report - nothing else can be
    created. The refusal names the two kinds, so the agent does not keep guessing."""
    executor, harness = _build(monkeypatch)
    out = _call(
        executor,
        "create_template",
        {
            "name": "Acme Dashboard",
            "kind": "dashboard",
            "layouts": [{"id": "cover_page"}],
        },
    )
    assert harness.session.added == []
    assert out.startswith("Error: kind must be one of presentation, report")
    assert "'dashboard'" in out


def test_kind_is_case_insensitive_and_blank_means_presentation(monkeypatch):
    executor, harness = _build(monkeypatch)
    _call(executor, "create_template", {"name": "Acme Report", "kind": " Report "})
    (written,) = harness.session.added
    assert written.kind == "report"

    executor, harness = _build(monkeypatch)
    _call(executor, "create_template", {"name": "Acme Deck", "kind": ""})
    (written,) = harness.session.added
    assert written.kind == "presentation"


# ── AC: the schemes a template supports are DECLARED ──────────────────


def test_create_pins_the_declared_schemes_from_the_parameter(monkeypatch):
    executor, harness = _build(monkeypatch)
    out = _call(
        executor,
        "create_template",
        {
            "name": "Acme Light",
            "theme_config": {"background": "#0b1020", "foreground": "#ffffff"},
            "supported_schemes": "light",
        },
    )
    (written,) = harness.session.added
    # The declaration wins over what the palette's background reads as: the
    # designer said light, the template is light-only.
    assert written.extra_metadata["supported_schemes"] == ["light"]
    assert "deck themes supported: light only" in out


def test_create_derives_the_declaration_from_the_palettes_when_none_is_given(monkeypatch):
    executor, harness = _build(monkeypatch)
    _call(
        executor,
        "create_template",
        {"name": "Acme Dark", "theme_config": {"background": "#0b1020", "foreground": "#ffffff"}},
    )
    (written,) = harness.session.added
    assert written.extra_metadata["supported_schemes"] == ["dark"]

    executor, harness = _build(monkeypatch)
    _call(
        executor,
        "create_template",
        {
            "name": "Acme Both",
            "theme_config": {"background": "#0b1020", "foreground": "#ffffff"},
            "extra_metadata": {"theme_variants": {"light": {"background": "#ffffff", "foreground": "#111111"}}},
        },
    )
    (written,) = harness.session.added
    assert written.extra_metadata["supported_schemes"] == ["dark", "light"]

    # No palette at all: nothing to read, both schemes until the author decides.
    executor, harness = _build(monkeypatch)
    _call(executor, "create_template", {"name": "Acme Blank"})
    (written,) = harness.session.added
    assert written.extra_metadata["supported_schemes"] == ["dark", "light"]


def test_create_refuses_a_scheme_the_renderer_does_not_know(monkeypatch):
    executor, harness = _build(monkeypatch)
    out = _call(
        executor,
        "create_template",
        {"name": "Acme Sepia", "supported_schemes": "dark, sepia"},
    )
    assert harness.session.added == []
    assert out.startswith("Error: supported_schemes: unknown scheme(s) sepia")
    assert "dark, light" in out


def test_create_refuses_a_bad_declaration_in_extra_metadata(monkeypatch):
    executor, harness = _build(monkeypatch)
    out = _call(
        executor,
        "create_template",
        {"name": "Acme", "extra_metadata": {"supported_schemes": "dark"}},
    )
    assert harness.session.added == []
    assert "NOT created" in out
    assert "extra_metadata.supported_schemes must be a list" in out


def test_a_report_template_carries_no_scheme_declaration(monkeypatch):
    executor, harness = _build(monkeypatch)
    _call(
        executor,
        "create_template",
        {
            "name": "Acme Report",
            "kind": "report",
            "extra_metadata": {"supported_schemes": ["dark"], "guidance": "x"},
        },
    )
    (written,) = harness.session.added
    # A report dresses no deck: the key is dropped, not refused.
    assert written.extra_metadata == {"guidance": "x"}


def test_update_changes_the_declaration_in_place(monkeypatch):
    container = _container(
        theme_config={"background": "#0b1020", "foreground": "#ffffff"},
        extra_metadata={"supported_schemes": ["dark"], "deck_defaults": {"theme": "dark"}},
    )
    executor, harness = _build(monkeypatch, container=container)
    out = _call(
        executor,
        "update_template",
        {"template_id": str(container.id), "supported_schemes": "dark,light"},
    )
    assert harness.session.committed
    # The rest of the metadata is untouched: the declaration lives next to it.
    assert container.extra_metadata == {
        "supported_schemes": ["dark", "light"],
        "deck_defaults": {"theme": "dark"},
    }
    assert "deck themes supported: dark / light" in out


def test_update_refuses_a_scheme_declaration_on_a_report(monkeypatch):
    container = _container(kind="report")
    executor, harness = _build(monkeypatch, container=container)
    out = _call(
        executor,
        "update_template",
        {"template_id": str(container.id), "supported_schemes": "dark"},
    )
    assert not harness.session.committed
    assert "supported_schemes applies to presentation and video templates only" in out


def test_a_kind_change_alone_revalidates_the_stored_metadata_against_the_new_kind(monkeypatch):
    """Changing a presentation template into a report with no metadata argument
    must still drop the deck-only declarations it stores (the REST
    update's rule): otherwise the header / footer pattern and the scheme list
    survive on a report template, and the read-back keeps describing a footer
    no deck draws. The rest of the metadata is kept, and an echo of the kind
    re-validates nothing."""
    footer = {
        "footer": "{{event}} - {{title}} - {{classification}}",
        "variables": [{"key": "classification", "label": "Classification", "default": "Internal"}],
    }
    container = _container(
        extra_metadata={
            "chrome": footer,
            "supported_schemes": ["dark"],
            "theme_variants": {"light": {"background": "#ffffff"}},
            "guidance": "keep it short",
        },
        layouts=[],
    )
    executor, harness = _build(monkeypatch, container=container)
    out = _call(
        executor,
        "update_template",
        {"template_id": str(container.id), "kind": "report"},
    )
    assert harness.session.committed
    assert container.kind == "report"
    # Dropped: the two deck-only keys. Kept: everything else, the palette
    # variants included - a kind change alone re-checks nothing about them.
    assert container.extra_metadata == {
        "theme_variants": {"light": {"background": "#ffffff"}},
        "guidance": "keep it short",
    }
    assert out.startswith("Updated kind, extra_metadata on "), out
    assert "footer on every slide" not in out
    assert harness.audits[0]["details"]["fields"] == ["kind", "extra_metadata"]
    # The call passed no metadata, so `extra_metadata` in the changed list is
    # a change with no author unless the result says WHICH keys the change of
    # kind dropped, and why - the validators drop them without a problem.
    assert (
        "Dropped from extra_metadata: chrome, supported_schemes - "
        "deck-only declaration(s) a report template never reads." in out
    ), out

    # Nothing deck-only stored: the kind change is recorded alone, and there
    # is nothing to report dropped.
    container = _container(extra_metadata={"guidance": "x"}, layouts=[])
    executor, harness = _build(monkeypatch, container=container)
    out = _call(executor, "update_template", {"template_id": str(container.id), "kind": "report"})
    assert container.extra_metadata == {"guidance": "x"}
    assert out.startswith("Updated kind on "), out
    assert "Dropped from extra_metadata" not in out

    # An echo of the stored kind is not a change and re-validates nothing.
    container = _container(extra_metadata={"chrome": footer, "supported_schemes": ["dark"]})
    executor, harness = _build(monkeypatch, container=container)
    out = _call(
        executor,
        "update_template",
        {"template_id": str(container.id), "kind": "presentation", "name": "Acme Deck v2"},
    )
    assert container.extra_metadata["chrome"] == footer
    assert container.extra_metadata["supported_schemes"] == ["dark"]
    assert out.startswith("Updated name on "), out


def test_update_records_no_change_for_metadata_that_already_reads_that_way(monkeypatch):
    """The changed list and the audit record bytes that MOVED: a PATCH that
    echoes the stored metadata, a scheme the template already declares, or the
    stored kind writes nothing - on every path, not only the kind-only re-read -
    and the reply says what was passed already reads that way, instead of
    claiming every field was left empty (which would send the agent hunting for
    a missing argument)."""
    stored = {
        "chrome": {
            "footer": "{{event}} - {{title}} - {{classification}}",
            "header": None,
            "variables": [{"key": "classification", "label": "Classification", "default": "Internal"}],
        },
        "supported_schemes": ["dark"],
        "guidance": "keep it short",
    }
    container = _container(extra_metadata=copy.deepcopy(stored), layouts=[])
    executor, harness = _build(monkeypatch, container=container)
    out = _call(
        executor,
        "update_template",
        {"template_id": str(container.id), "extra_metadata": copy.deepcopy(stored)},
    )
    assert container.extra_metadata == stored
    assert not harness.session.committed
    assert harness.audits == []
    assert out == (
        "No change on `Acme Deck`: extra_metadata already read that way, so nothing was written."
    ), out

    # The same for a scheme the template already declares, alongside an echo
    # of the stored kind: both named, nothing written.
    executor, harness = _build(monkeypatch, container=container)
    out = _call(
        executor,
        "update_template",
        {"template_id": str(container.id), "kind": "presentation", "supported_schemes": "dark"},
    )
    assert container.extra_metadata == stored
    assert not harness.session.committed
    assert harness.audits == []
    assert out.startswith("No change on `Acme Deck`: kind, supported_schemes already read that way"), out

    # Bytes that do move are still a change - the echo test is not "any
    # metadata argument is ignored".
    executor, harness = _build(monkeypatch, container=container)
    out = _call(
        executor,
        "update_template",
        {"template_id": str(container.id), "supported_schemes": "dark, light"},
    )
    assert harness.session.committed
    assert container.extra_metadata["supported_schemes"] == ["dark", "light"]
    assert out.startswith("Updated extra_metadata on "), out
    assert harness.audits[0]["details"]["fields"] == ["extra_metadata"]


# ── AC: a built-in container is duplicated, never edited ──────────────────────


def _builtin() -> TemplateContainer:
    from app.services.template_container_seed import BUILTIN_TAG

    return _container(
        name="XTM One Brand Deck",
        is_company_managed=True,
        tags=["brand", "xtm-one", BUILTIN_TAG, "slug:xtm-one-brand"],
    )


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("update_template", {"name": "XTM One Green"}),
        ("delete_template", {}),
        ("attach_template_asset", {"file_id": str(uuid.uuid4())}),
        ("detach_template_asset", {"asset_id": str(uuid.uuid4())}),
    ],
)
def test_no_write_tool_touches_a_builtin_container(monkeypatch, tool, arguments):
    """A built-in is full-synced on startup: an edit is erased by the next deploy."""
    builtin = _builtin()
    executor, harness = _build(monkeypatch, actor=_user(role="admin"), container=builtin)

    out = _call(executor, tool, {"template_id": str(builtin.id), **arguments})

    assert "built-in template" in out
    assert "create_template" in out, "the agent must be told to duplicate"
    assert harness.session.added == []
    assert harness.session.deleted == []
    assert harness.audits == []
    assert builtin.name == "XTM One Brand Deck"


def test_the_builtin_marker_is_the_seed_tag_not_a_guessed_name():
    """``is_builtin_container`` must track the seeder, not a hardcoded slug."""
    from app.services.template_container_seed import BUILTIN_TAG
    from app.services.template_containers import is_builtin_container

    assert is_builtin_container(
        _container(is_company_managed=True, tags=[BUILTIN_TAG])
    )
    # An admin-created company-managed container is NOT a built-in.
    assert not is_builtin_container(
        _container(is_company_managed=True, tags=["brand"])
    )
    # Nor is a private container that merely carries the tag.
    assert not is_builtin_container(
        _container(is_company_managed=False, tags=[BUILTIN_TAG])
    )


# ── AC: an agent never publishes org-wide ─────────────────────────────────────


def test_a_created_container_is_always_private_even_for_an_admin(monkeypatch):
    executor, harness = _build(monkeypatch, actor=_user(role="admin", is_admin=True))
    out = _call(executor, "create_template", {"name": "Company Deck"})
    (written,) = harness.session.added
    assert written.is_company_managed is False
    assert "private to its owner" in out


def test_no_write_tool_exposes_a_company_managed_parameter():
    """Promotion stays a human action; there must be no lever to ask for it."""
    for definition in _tool_defs().values():
        properties = definition.input_schema.get("properties", {})
        assert "is_company_managed" not in properties, definition.name
        assert "shared_group_ids" not in properties, definition.name
        assert "user_id" not in properties, definition.name


def test_editing_a_company_managed_container_warns_that_it_is_org_wide(monkeypatch):
    """An admin may still edit one — the reply has to say who sees the change."""
    shared = _container(is_company_managed=True, tags=["brand"])
    executor, _h = _build(
        monkeypatch, actor=_user(role="admin", is_admin=True), container=shared
    )
    out = _call(
        executor,
        "update_template",
        {"template_id": str(shared.id), "description": "Light variant of the brand."},
    )
    assert "company-managed" in out
    assert "everyone in the organisation" in out


def test_update_template_ignores_the_legacy_theme_label(monkeypatch):
    """The free-text label is no longer a tool argument: a stale model
    passing only ``theme`` has asked for nothing, and is told so."""
    container = _container()
    executor, harness = _build(monkeypatch, container=container)
    out = _call(
        executor,
        "update_template",
        {"template_id": str(container.id), "theme": "acme-light"},
    )
    assert "Nothing to change" in out
    assert container.theme == "acme-dark"
    assert harness.audits == []
    for name in ("create_template", "update_template"):
        assert "theme" not in _tool_defs()[name].input_schema["properties"], name


def test_a_container_the_user_cannot_manage_is_refused(monkeypatch):
    executor, harness = _build(
        monkeypatch,
        manage_error="Only the creator or an admin can edit this template container",
    )
    out = _call(
        executor,
        "update_template",
        {"template_id": str(uuid.uuid4()), "name": "Mine now"},
    )
    assert "Only the creator or an admin" in out
    assert harness.audits == []


# ── AC: a file the user cannot read cannot be attached ────────────────────────


def test_attaching_a_file_the_user_cannot_read_is_refused(monkeypatch):
    container = _container()
    executor, harness = _build(
        monkeypatch,
        container=container,
        source_file=_asset(user_id=uuid.uuid4()),
        file_access_error=True,
    )

    out = _call(
        executor,
        "attach_template_asset",
        {"template_id": str(container.id), "file_id": str(uuid.uuid4())},
    )

    assert "is not a platform file you can read" in out
    assert harness.session.added == []
    assert harness.uploads == [], "an unauthorized file must not even be read"
    assert harness.audits == []


def test_attach_copies_the_bytes_instead_of_repointing_the_source_row(monkeypatch):
    """Re-pointing would strip the file out of the conversation that owns it."""
    from app.models.template_container import TEMPLATE_ASSET_SOURCE

    container = _container()
    source = _asset()
    executor, harness = _build(
        monkeypatch, container=container, source_file=source
    )

    out = _call(
        executor,
        "attach_template_asset",
        {
            "template_id": str(container.id),
            "file_id": str(source.id),
            "asset_role": "Logo",
        },
    )

    # The source row is untouched: still a chat attachment, still unbound.
    assert source.source == "chat"
    assert source.template_container_id is None
    # A new row was written, owned by the container.
    (written,) = harness.session.added
    assert written.template_container_id == container.id
    assert written.source == TEMPLATE_ASSET_SOURCE
    assert written.asset_role == "logo"
    assert harness.uploads[-1]["prefix"] == f"template-asset/{container.id}"
    assert "image_file_id" in out, "the agent needs to know what the id is for"


def test_attach_records_the_digest_of_the_copy(monkeypatch):
    """The new row names a new object and records the digest of the bytes
    stored there, which the presentation package import trusts."""
    container = _container()
    source = _asset()
    executor, harness = _build(monkeypatch, container=container, source_file=source)

    _call(executor, "attach_template_asset", {"template_id": str(container.id), "file_id": str(source.id)})

    (written,) = harness.session.added
    assert written.sha256 == hashlib.sha256(b"binary-bytes").hexdigest()


def test_attach_reads_the_geometry_of_the_copy_never_the_source_row(monkeypatch):
    """The copy is written from the bytes this tool just stored (an SVG
    comes out of the sanitizer changed), so its trim box is read from them. A
    box the source row carries describes other bytes; bytes that are not an
    image a logo wall can crop carry none."""
    container = _container()
    source = _asset(image_meta={"v": 1, "w": 10, "h": 10, "trim": [0.1, 0.1, 0.9, 0.9], "ink": 0.5})
    executor, harness = _build(monkeypatch, container=container, source_file=source)

    _call(executor, "attach_template_asset", {"template_id": str(container.id), "file_id": str(source.id)})

    (written,) = harness.session.added
    assert written.image_meta is None


def test_attach_enforces_the_asset_cap(monkeypatch):
    container = _container()
    executor, harness = _build(
        monkeypatch,
        container=container,
        source_file=_asset(),
        assets=[_asset() for _ in range(MAX_ASSETS_PER_CONTAINER)],
    )
    out = _call(
        executor,
        "attach_template_asset",
        {"template_id": str(container.id), "file_id": str(uuid.uuid4())},
    )
    assert str(MAX_ASSETS_PER_CONTAINER) in out
    assert harness.session.added == []


def test_attach_rejects_a_non_uuid_file_id_without_opening_a_session(monkeypatch):
    from app.services.builtin_tools import build_template_authoring_tools

    def _no_session():
        raise AssertionError("must not open a session for an invalid id")

    monkeypatch.setattr("app.core.database.async_session", _no_session, raising=False)
    _defs, executor = build_template_authoring_tools(
        db=None,  # type: ignore[arg-type]
        user_id=str(uuid.uuid4()),
    )
    out = _call(
        executor,
        "attach_template_asset",
        {"template_id": str(uuid.uuid4()), "file_id": "not-a-uuid"},
    )
    assert "already in the platform" in out


def test_detach_cannot_reach_an_asset_of_another_container(monkeypatch):
    """Asset lookup is scoped to the container, never a bare id."""
    container = _container()
    executor, harness = _build(
        monkeypatch, container=container, assets=[_asset()]
    )
    out = _call(
        executor,
        "detach_template_asset",
        {"template_id": str(container.id), "asset_id": str(uuid.uuid4())},
    )
    assert "has no asset" in out
    assert harness.deleted_attachment_filters == []


# ── Update semantics ──────────────────────────────────────────────────────────


def test_update_patches_only_the_fields_that_were_passed(monkeypatch):
    container = _container()
    executor, _h = _build(monkeypatch, container=container)

    out = _call(
        executor,
        "update_template",
        {"template_id": str(container.id), "theme_config": {"accent": "#22c55e"}},
    )

    assert container.theme_config == {"accent": "#22c55e"}
    assert container.name == "Acme Deck"
    assert container.tags == ["acme", "brand"]
    assert container.aspect_ratio == "16:9"
    assert "theme_config" in out


def test_update_writes_nothing_when_a_value_is_invalid(monkeypatch):
    container = _container()
    executor, harness = _build(monkeypatch, container=container)

    out = _call(
        executor,
        "update_template",
        {
            "template_id": str(container.id),
            "name": "Renamed",
            "theme_config": {"sidebar": "#fff"},
        },
    )

    assert container.name == "Acme Deck", "a partial write is worse than none"
    assert "NOT modified" in out
    assert harness.audits == []


def test_update_validates_new_layouts_against_the_new_kind(monkeypatch):
    """The layouts must be checked against the kind the container is BECOMING, not
    the one it had: switching to ``report`` applies the report block catalog, and a
    kind that has no renderer at all is refused outright - the layouts
    are not even looked at."""
    container = _container()
    executor, _h = _build(monkeypatch, container=container)
    out = _call(
        executor,
        "update_template",
        {
            "template_id": str(container.id),
            "kind": "dashboard",
            "layouts": [{"id": "cover_page"}],
        },
    )
    assert container.kind == "presentation"
    assert container.layouts == [{"id": "title", "name": "Title"}]
    assert out.startswith("Error: kind must be one of presentation, report")

    # Switching to ``report`` applies the report block catalog — a slide layout id
    # is refused there even though it was valid under the container's old kind.
    container = _container()
    executor, _h = _build(monkeypatch, container=container)
    out = _call(
        executor,
        "update_template",
        {
            "template_id": str(container.id),
            "kind": "report",
            "layouts": [{"id": "content"}],
        },
    )
    assert container.kind != "report", "a partial write is worse than none"
    assert "not a report block" in out


def test_update_takes_an_echoed_legacy_kind_as_a_no_op(monkeypatch):
    """A row of a retired kind (a user's older asset library) must still be
    able to change its name or tags through the tool: only a CHANGE of kind is
    held to the closed choice - the same ``resolve_kind_update`` rule the REST
    update applies, so the two surfaces cannot disagree on what an echo means."""
    container = _container(kind="asset-library", layouts=[])
    executor, harness = _build(monkeypatch, container=container)
    out = _call(
        executor,
        "update_template",
        {
            "template_id": str(container.id),
            "kind": "asset-library",
            "name": "Partner logos (ours)",
        },
    )
    assert container.name == "Partner logos (ours)"
    assert container.kind == "asset-library", "the echo changes nothing"
    assert out.startswith("Updated name on "), out
    assert harness.audits and harness.audits[0]["details"]["fields"] == ["name"]

    # A CHANGE to another retired kind is still refused, and writes nothing.
    executor, harness = _build(monkeypatch, container=container)
    out = _call(
        executor,
        "update_template",
        {"template_id": str(container.id), "kind": "dashboard", "name": "Nope"},
    )
    assert out.startswith("Error: kind must be one of presentation, report")
    assert container.kind == "asset-library"
    assert container.name == "Partner logos (ours)"
    assert harness.audits == []

    # Moving the row onto a supported kind is how it leaves the legacy label.
    executor, _h = _build(monkeypatch, container=container)
    out = _call(
        executor,
        "update_template",
        {"template_id": str(container.id), "kind": "presentation"},
    )
    assert container.kind == "presentation"
    assert out.startswith("Updated kind on "), out


def test_update_with_no_fields_says_so_instead_of_committing(monkeypatch):
    container = _container()
    executor, harness = _build(monkeypatch, container=container)
    out = _call(executor, "update_template", {"template_id": str(container.id)})
    assert "Nothing to change" in out
    assert harness.session.committed is False


# ── Audit ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("tool", "arguments", "action"),
    [
        ("create_template", {"name": "Acme"}, "create"),
        ("update_template", {"name": "Renamed"}, "update"),
        ("delete_template", {}, "delete"),
    ],
)
def test_every_container_mutation_is_audited_with_the_acting_user(
    monkeypatch, tool, arguments, action
):
    """An agent write is attributable."""
    actor = _user()
    container = _container()
    executor, harness = _build(monkeypatch, actor=actor, container=container)

    _call(executor, tool, {"template_id": str(container.id), **arguments})

    (entry,) = harness.audits
    assert entry["action"] == action
    assert entry["user"] is actor
    assert entry["entity_type"] == "template_container"
    # The audit row says the write came from a tool, not the REST surface.
    assert entry["details"]["via"].startswith("builtin_tool:")


def test_asset_mutations_are_audited_against_the_container(monkeypatch):
    container = _container()
    source = _asset()
    executor, harness = _build(monkeypatch, container=container, source_file=source)

    _call(
        executor,
        "attach_template_asset",
        {"template_id": str(container.id), "file_id": str(source.id)},
    )

    (entry,) = harness.audits
    assert entry["action"] == "update"
    assert entry["entity_id"] == container.id
    assert entry["details"]["source_file_id"] == str(source.id)


def test_delete_removes_the_container_and_its_binaries(monkeypatch):
    container = _container()
    executor, harness = _build(
        monkeypatch, container=container, assets=[_asset(), _asset()]
    )

    out = _call(executor, "delete_template", {"template_id": str(container.id)})

    assert harness.deleted_attachment_filters == [
        {"template_container_id": container.id}
    ]
    assert harness.session.deleted == [container]
    # Decks already generated keep their own spec — say so rather than let the
    # agent tell the user their presentations are gone.
    assert "keep rendering" in out


# ── validate_presentation ─────────────────────────────────────────────────────


def _validate(monkeypatch, container, *, deck=None, **arguments) -> str:
    from app.services.builtin_tools import build_template_authoring_tools

    actor = _user()
    session = _FakeSession([_Result(one=container)])
    monkeypatch.setattr(
        "app.core.database.async_session", lambda: session, raising=False
    )

    async def _fake_groups(db, uid):
        return []

    monkeypatch.setattr(
        "app.api.v1.helpers.get_user_group_ids", _fake_groups, raising=False
    )

    async def _fake_load_deck(db, deck_id, *, user_id, user_group_ids=None):
        # Same shape as the real visibility-aware loader: the row plus
        # the caller's resolved group ids, or ``None`` when not visible.
        return (deck, []) if deck is not None else None

    monkeypatch.setattr(
        "app.services.presentations.load_visible_presentation",
        _fake_load_deck,
        raising=False,
    )

    _defs, executor = build_template_authoring_tools(
        db=None,  # type: ignore[arg-type]
        user_id=str(actor.id),
    )
    return _call(
        executor,
        "validate_presentation",
        {"template_id": str(container.id), **arguments},
    )


def test_validate_passes_a_well_formed_template(monkeypatch):
    container = _container(
        theme_config={"background": "#0b1020", "foreground": "#fff", "accent": "#0bf"}
    )
    out = _validate(monkeypatch, container)
    assert "is valid" in out


def test_validate_reports_a_template_that_would_silently_do_nothing(monkeypatch):
    container = _container(theme_config={}, aspect_ratio="A4-portrait")
    out = _validate(monkeypatch, container)
    assert "theme_config is empty" in out
    assert "A4-portrait" in out


def test_validate_flags_an_existing_bad_layout_id(monkeypatch):
    """The audit mode catches a container written before the guardrails, too."""
    container = _container(layouts=[{"id": "hero_banner"}])
    out = _validate(monkeypatch, container)
    assert "hero_banner" in out


def test_validate_checks_a_draft_spec_against_the_template(monkeypatch):
    container = _container(
        aspect_ratio="16:9", layouts=[{"id": "title"}, {"id": "section"}]
    )
    out = _validate(
        monkeypatch,
        container,
        spec={
            "title": "Preview",
            "aspect_ratio": "4:3",
            "slides": [
                {"layout": "title", "title": "Hello"},
                {"layout": "content", "title": "KPIs", "blocks": [
                    {"type": "metrics_grid", "items": [{"label": "Users", "value": "12"}]}
                ]},
            ],
        },
    )
    # A layout that exists but is off-template, and a mismatched ratio.
    assert "does not advertise" in out
    assert "4:3" in out and "16:9" in out


def test_validate_flags_a_deck_that_is_not_linked_to_the_template(monkeypatch):
    """An unlinked deck gets none of the palette — it looks like a broken theme."""
    container = _container(layouts=[{"id": "title"}])
    deck = SimpleNamespace(
        id=uuid.uuid4(),
        title="Preview",
        template_container_id=None,
        spec={
            "title": "Preview",
            "aspect_ratio": "16:9",
            "slides": [{"layout": "title", "title": "Hello"}],
        },
    )
    out = _validate(
        monkeypatch, container, deck=deck, presentation_id=str(deck.id)
    )
    assert "not linked to this template" in out


def test_validate_refuses_both_a_deck_and_a_draft(monkeypatch):
    container = _container()
    out = _validate(
        monkeypatch,
        container,
        presentation_id=str(uuid.uuid4()),
        spec={"title": "x", "slides": []},
    )
    assert "not both" in out


def test_validate_writes_nothing(monkeypatch):
    from app.services.builtin_tools import build_template_authoring_tools

    container = _container()
    session = _FakeSession([_Result(one=container)])
    monkeypatch.setattr(
        "app.core.database.async_session", lambda: session, raising=False
    )

    async def _fake_groups(db, uid):
        return []

    monkeypatch.setattr(
        "app.api.v1.helpers.get_user_group_ids", _fake_groups, raising=False
    )
    _defs, executor = build_template_authoring_tools(
        db=None,  # type: ignore[arg-type]
        user_id=str(uuid.uuid4()),
    )
    _call(executor, "validate_presentation", {"template_id": str(container.id)})
    assert session.added == []
    assert session.committed is False


# ── The seeded generator agent ────────────────────────────────────────────────


def _designer() -> dict:
    from app.agents.builtin_agents import BUILTIN_AGENTS

    return next(a for a in BUILTIN_AGENTS if a["slug"] == "template-designer")


def test_the_generator_agent_is_seeded_with_the_whole_loop():
    """Authoring without a preview is a page of hex values nobody can judge."""
    agent = _designer()
    assert set(agent["builtin_tool_ids"]) == {
        "template_search",
        "template_authoring",
        "generate_presentation",
    }
    assert agent["disable_chat"] is False


def test_the_generator_agent_persona_teaches_the_three_traps():
    from app.agents.seed import _resolve_seed_content
    from app.services.presentation_spec import SLIDE_LAYOUTS
    from app.services.presentations import THEME_CONFIG_KEYS

    persona = _resolve_seed_content("agent_persona", _designer()["persona"])
    # 1. built-ins are duplicated, never edited
    assert "Never edit a built-in template" in persona
    # 2. and 3. the two whitelists, spelled with the real values
    for key in THEME_CONFIG_KEYS:
        assert key in persona
    for layout in SLIDE_LAYOUTS:
        assert layout in persona
    # The preview loop, and the in-place edit that keeps it to one deck.
    assert "presentation_id" in persona


def test_the_generator_agent_icon_exists_in_the_frontend_icon_map():
    """An unmapped icon renders as nothing in the agent list."""
    constants = (
        BACKEND_ROOT.parent / "frontend/src/lib/constants.ts"
    ).read_text(encoding="utf-8")
    icon_map = constants.split("export const ICON_MAP")[1].split("};")[0]
    assert f"\n  {_designer()['icon']},\n" in icon_map


def test_the_builtin_container_seeds_wherever_the_builtin_agents_do():
    """Otherwise dev has the Template Designer but nothing for it to duplicate.

    Targeting is per container through ``platform_targets`` (the same
    matcher the built-in skills use), so the check is behavioural: every mode
    that seeds the Template Designer must also seed at least one container.
    """
    from app.services.template_container_seed import (
        BUILTIN_TEMPLATE_CONTAINERS,
        builtin_containers_for,
    )

    assert BUILTIN_TEMPLATE_CONTAINERS, "there must be something to duplicate"
    for mode in ("xtm_one", "dev", "copilot"):
        assert builtin_containers_for(mode), f"{mode} has nothing to duplicate"
    xtm_one_slugs = {c["slug"] for c in builtin_containers_for("xtm_one")}
    assert xtm_one_slugs == {c["slug"] for c in builtin_containers_for("dev")}
    assert "xtm-one-brand" in xtm_one_slugs


# ── The RAG boundary still holds ──────────────────────────────────────────────

_NEW_TEMPLATE_MODULES = (
    "app/services/builtin_tools/template_authoring.py",
    "app/services/file_access.py",
)
_FORBIDDEN_IMPORT_MODULES = (
    "app.services.vector",
    "app.services.chunking",
    "app.services.hybrid_search",
    "app.services.document_processor",
    "app.models.embedding",
    "pgvector",
)


@pytest.mark.parametrize("rel_path", _NEW_TEMPLATE_MODULES)
def test_the_write_path_never_imports_the_vector_pipeline(rel_path):
    """Templates are never RAG: the write surface must keep it so."""
    tree = ast.parse((BACKEND_ROOT / rel_path).read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
            modules.update(f"{node.module}.{a.name}" for a in node.names)
    offenders = [
        m
        for m in modules
        for bad in _FORBIDDEN_IMPORT_MODULES
        if m == bad or m.startswith(f"{bad}.")
    ]
    assert not offenders, f"{rel_path} imports RAG machinery: {sorted(offenders)}"


# ── The move must not drop a branch of the contract ──────────────────────────
#
# ``_authorize_file_access`` moved out of ``api/v1/chat.py`` into
# ``services/file_access.py`` while a new branch landed INSIDE that function at
# the same time — a conversation file is readable by anyone the conversation is
# shared with, not only its owner. Rebasing one onto the other is a
# textual conflict whose tempting resolution ("keep my side") silently deletes
# the share branch, and the failure is invisible: a read-only viewer just starts
# getting 403s on file chips they can see in the transcript.
#
# Nothing else pins that branch — the panel test next to it covers the ADMIN
# path — so it is pinned here, by the change that moved the code.


def _file_access_probe(file_att, user, *, share_level):
    """Run the real authorizer with the conversation lookup faked out."""
    from app.services.file_access import authorize_file_access

    class _Session:
        async def execute(self, statement, *a, **k):
            return _Result(one=SimpleNamespace(id=file_att.conversation_id))

    async def _fake_access(db, u, conv):
        return share_level

    import app.api.v1.chat as chat_module

    original = chat_module.conversation_access
    chat_module.conversation_access = _fake_access
    try:
        asyncio.run(authorize_file_access(file_att, user, _Session()))
        return True
    except Exception:
        return False
    finally:
        chat_module.conversation_access = original


def _conversation_file(conversation_id):
    return SimpleNamespace(
        user_id=None,  # assistant-generated: the conversation is the only scope
        conversation_id=conversation_id,
        assignment_run_id=None,
        template_container_id=None,
        source="chat",
    )


def test_a_shared_viewer_can_still_read_a_conversation_file():
    """The conversation-file share branch must survive the move into file_access.py."""
    granted = _file_access_probe(
        _conversation_file(uuid.uuid4()),
        _user(),
        share_level="read",  # CONVERSATION_ACCESS_READ — not the owner
    )
    assert granted, (
        "a user the conversation is shared with lost file access — the share "
        "branch was dropped when the authorizer moved out of chat.py"
    )


def test_a_stranger_still_cannot_read_a_conversation_file():
    """The counterpart: no access level means no bytes."""
    assert not _file_access_probe(
        _conversation_file(uuid.uuid4()), _user(), share_level=None
    )


def test_chat_keeps_exposing_the_authorizer_under_its_old_name():
    """The routes and the tests that pin the contract import the private alias."""
    from app.api.v1.chat import _authorize_file_access
    from app.services.file_access import authorize_file_access

    assert _authorize_file_access is authorize_file_access
