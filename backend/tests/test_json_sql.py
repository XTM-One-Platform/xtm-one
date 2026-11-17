"""A ``json`` value PostgreSQL stores but cannot parse, without a database.

The write side (``sanitize_json_strings``, the engine's serializer and deserializer), the webhook
routes' warning, the visibility helper's guard per model, the migrations' copy of the test, and a
tripwire over ``app/``: a ``jsonb`` cast of a column sits inside ``when_json_parseable`` or is listed
with the reason no row can hold such a value. The PostgreSQL half is
``test_unparseable_json_rows_postgres.py``.
"""

from __future__ import annotations

import ast
import json
import logging
import re
import uuid
from typing import get_args

import pytest
from sqlalchemy import JSON
from sqlalchemy.dialects import postgresql

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.api.v1 import helpers, webhooks
from app.core import json_sql
from app.core.database import Base, json_deserializer, json_serializer
from app.core.utils import sanitize_json_strings, strip_null_bytes
from app.models.agent import Agent
from app.models.integration import Integration
from app.services import integration_notification_auth
from tests.app_sources import BACKEND_DIR, modules, parents, source, walk

# -- the write side ----------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param("\ud800", "\ufffd", id="lone-high"),
        pytest.param("x\udc00y", "x\ufffdy", id="lone-low"),
        pytest.param("\ud800\ud800\udc00", "\ufffd\ud800\udc00", id="high-then-pair"),
        pytest.param("\ud800\udc00\udc00", "\ud800\udc00\ufffd", id="pair-then-low"),
        pytest.param("\udc00\ud800", "\ufffd\ufffd", id="reversed-pair"),
        pytest.param("a\u0000\ud800", "a\ufffd", id="nul-and-lone"),
        pytest.param({"k\ud800": ["\udfff", 1, None]}, {"k\ufffd": ["\ufffd", 1, None]}, id="keys-and-lists"),
        pytest.param(("\ud800",), ["\ufffd"], id="tuple"),
    ],
)
def test_a_lone_surrogate_becomes_the_replacement_character(value, expected):
    assert sanitize_json_strings(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        pytest.param({"a": ["b", 1, 2.5, True, None], "c": {"d": "e"}}, id="ascii"),
        pytest.param({"note": "caf\u00e9 \U0001f600", "list": ["\u00e9t\u00e9"]}, id="non-ascii"),
        pytest.param(["\ud83d\ude00", {"pair": "a\udbff\udfffb"}], id="valid-pairs"),
    ],
)
def test_a_value_with_nothing_to_replace_is_returned_as_the_same_object(value):
    assert sanitize_json_strings(value) is value


def test_strip_null_bytes_still_leaves_surrogates_to_its_callers():
    """The tracing sinks' helper keeps its behaviour: only the JSON choke point replaces surrogates."""
    assert strip_null_bytes({"a": "\ud800\u0000"}) == {"a": "\ud800"}


def test_the_serializer_writes_no_lone_surrogate_and_keeps_pairs():
    serialized = json_serializer({"lone": "\ud800", "pair": "\ud83d\ude00", "emoji": "\U0001f600", "nul": "a\u0000"})
    assert serialized == '{"lone": "\\ufffd", "pair": "\\ud83d\\ude00", "emoji": "\\ud83d\\ude00", "nul": "a"}'


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        pytest.param('{"a": "\\ud800"}', {"a": "\ufffd"}, id="lone"),
        pytest.param('{"a": "\\u0000b"}', {"a": "b"}, id="nul"),
        pytest.param('{"a": "\\ud83d\\ude00"}', {"a": "\U0001f600"}, id="pair"),
        pytest.param('{"a": "\\\\ud800"}', {"a": "\\ud800"}, id="escaped-backslash"),
        pytest.param('["caf\\u00e9", 1]', ["caf\u00e9", 1], id="clean"),
    ],
)
def test_the_deserializer_reads_what_the_serializer_would_store(stored, expected):
    assert json_deserializer(stored) == expected


def test_the_flush_hook_sees_every_json_column_a_decorator_included():
    """``Presentation.spec`` is a ``TypeDecorator`` over JSON: its written value is kept in step too."""
    from sqlalchemy.orm import configure_mappers

    from app.core.database import _json_attribute_keys
    from app.models.presentation import Presentation, PresentationVersion

    configure_mappers()
    assert "spec" in _json_attribute_keys(Presentation.__mapper__)
    assert "spec" in _json_attribute_keys(PresentationVersion.__mapper__)
    for mapper in Base.registry.mappers:
        for attr in mapper.column_attrs:
            column_type = attr.columns[0].type
            json_typed = isinstance(getattr(column_type, "impl_instance", column_type), JSON)
            assert (attr.key in _json_attribute_keys(mapper)) is json_typed, f"{mapper.class_.__name__}.{attr.key}"


def test_both_engines_read_through_the_deserializer():
    from app.core.database import engine
    from app.services.tracing import local_sink

    local_sink._build_tracing_session_factory()
    for dialect in (engine.dialect, local_sink._TRACING_ENGINE.dialect):
        assert getattr(dialect, "_json_deserializer", None) is json_deserializer


# -- the webhook routes' warning --------------------------------------------


def test_the_warning_names_ids_only_bounded_and_rate_limited(caplog):
    integration_notification_auth._log_state.clear()
    ids = [uuid.UUID(int=i) for i in range(1, 8)]
    try:
        with caplog.at_level(logging.WARNING):
            webhooks._warn_unreadable_bindings("Provider webhook github", ids)
            webhooks._warn_unreadable_bindings("Provider webhook github", ids)
            webhooks._warn_unreadable_bindings("Provider webhook github", [])
    finally:
        integration_notification_auth._log_state.clear()
    [message] = [record.getMessage() for record in caplog.records]
    assert message.startswith("Provider webhook github: skipped 7 bound agent(s)")
    named = sorted(str(agent_id) for agent_id in ids)
    assert all(agent_id in message for agent_id in named[:5])
    assert named[5] not in message and "and 2 more" in message


# -- the guards --------------------------------------------------------------


def _compiled(clause) -> str:
    return str(clause.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


def test_the_guard_is_a_case_whose_test_runs_before_the_cast():
    sql = _compiled(json_sql.when_json_parseable(Agent.integration_bindings, Agent.enabled.is_(True)))
    assert sql.startswith("CASE WHEN")
    assert sql.index("regexp_replace") < sql.index("THEN false ELSE")


def test_the_indexed_columns_are_the_ones_postgresql_indexes():
    indexed = {
        (column.table.name, column.name)
        for table in Base.metadata.sorted_tables
        for column in table.columns
        if isinstance(column.type, JSON) and json_sql.jsonb_cast_indexed(column)
    }
    assert indexed == {
        ("agents", "shared_user_ids"),
        ("integrations", "shared_group_ids"),
        ("integrations", "shared_user_ids"),
        ("video_projects", "asset_refs"),
    }


def _visibility_models() -> list[type]:
    """Every model of ``VisibilityQueryable``, its nested ``CompanyManagedEntity`` alias flattened."""
    models: list[type] = []
    for member in get_args(helpers.VisibilityQueryable.__value__):
        value = getattr(member, "__value__", None)
        models.extend(get_args(value) if value is not None else [member])
    return models


@pytest.mark.parametrize("model_cls", _visibility_models(), ids=lambda model: model.__tablename__)
def test_every_visibility_group_arm_is_guarded_unless_an_index_keeps_it_clean(model_cls):
    sql = _compiled(helpers._shared_group_ids_overlap(model_cls, [str(uuid.UUID(int=1))]))
    guarded = sql.startswith("CASE WHEN")
    assert guarded is not json_sql.jsonb_cast_indexed(model_cls.shared_group_ids)
    assert guarded is (model_cls is not Integration)


def test_the_migrations_spell_the_same_test():
    """A migration inlines the patterns (it must keep meaning what it meant); they are these."""
    expected = {
        "refused_escape": json_sql.REFUSED_ESCAPE_PATTERN,
        "surrogate_pair": json_sql.SURROGATE_PAIR_PATTERN,
        "escaped_backslash": json_sql.ESCAPED_BACKSLASH,
        "out_of_range_number": json_sql.OUT_OF_RANGE_NUMBER_PATTERN,
    }
    for stem in (
        "o1crq2opencrq1_rename_opengrc_to_opencrq",
        "a2a2intmodel1_a2a_partner_relationship_on_integrations",
        "a2ah2runctx01_repair_unreadable_run_trigger_context",
    ):
        module = ast.parse(source(BACKEND_DIR / "alembic" / "versions" / f"{stem}.py"))
        [params] = [
            node.value
            for node in ast.walk(module)
            if isinstance(node, ast.Assign) and [getattr(t, "id", None) for t in node.targets] == ["_UNREADABLE_PARAMS"]
        ]
        assert ast.literal_eval(params) == expected, stem


# -- the tripwire ------------------------------------------------------------

#: Guards a ``jsonb`` cast sits inside (the webhook routes' wrapper returns rows it sorts out itself).
_GUARDS = frozenset({"when_json_parseable", "_bound_or_unreadable"})

#: ``(module, function)`` casting a column no row can hold an unparseable value in, and why.
_NO_GUARD_NEEDED = {
    ("api/v1/helpers.py", "_shared_user_ids_contains"): (
        "normalize_user_grants() and, for LLM providers, apply_provider_access() are the writers and "
        "store canonical UUIDs only; agents and "
        "integrations also index the cast, which refuses such a value at write"
    ),
    ("services/video_studio/retention.py", "_indexed_refs"): (
        "asset_refs is written only by the VideoProject mapper event (spec.asset_refs: kind-prefixed ids) and "
        "its cast is indexed (ix_video_projects_asset_refs_jsonb_gin), which refuses such a value at write; a "
        "guard would also keep the media lookups (uses_media, named_by_a_video) off that index"
    ),
    ("services/company_managed_writes.py", "restricted_governors"): (
        "coordination_access is written only through normalize_coordination_access(): two scopes "
        "from a closed set and lists of canonical agent UUIDs"
    ),
}


def _is_jsonb(node: ast.AST) -> bool:
    return (isinstance(node, ast.Name) and node.id == "JSONB") or (
        isinstance(node, ast.Attribute) and node.attr == "JSONB"
    )


def _called(node: ast.AST) -> str | None:
    if isinstance(node, ast.Call):
        func = node.func
        return func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
    return None


def _jsonb_casts(module: ast.Module) -> list[ast.AST]:
    """Every ``cast(<column>, JSONB)`` / ``<column>.cast(JSONB)`` of *module* (a ``NULL`` literal aside)."""
    casts = []
    for node in walk(module):
        if not isinstance(node, ast.Call):
            continue
        name = _called(node)
        if name == "cast" and isinstance(node.func, ast.Name) and len(node.args) == 2 and _is_jsonb(node.args[1]):
            if _called(node.args[0]) != "null":
                casts.append(node)
        elif name == "cast" and isinstance(node.func, ast.Attribute) and node.args and _is_jsonb(node.args[0]):
            casts.append(node)
    return casts


def _enclosing_function(module: ast.Module, node: ast.AST) -> ast.AST | None:
    up = parents(module)
    while node in up:
        node = up[node]
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            return node
    return None


def _guarded(module: ast.Module, cast_node: ast.AST, function: ast.AST | None) -> bool:
    """Inside a guard call, directly or through the names assigned from it within the function."""
    up = parents(module)

    def inside_guard(node: ast.AST) -> bool:
        while node in up:
            node = up[node]
            if _called(node) in _GUARDS:
                return True
        return False

    if inside_guard(cast_node):
        return True
    if function is None:
        return False
    tainted: set[str] = set()
    changed = True
    while changed:
        changed = False
        for node in ast.walk(function):
            if isinstance(node, ast.Assign) and any(
                sub is cast_node or (isinstance(sub, ast.Name) and sub.id in tainted) for sub in ast.walk(node.value)
            ):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id not in tainted:
                        tainted.add(target.id)
                        changed = True
    return any(
        isinstance(node, ast.Name) and node.id in tainted and inside_guard(node) for node in ast.walk(function)
    )


@pytest.mark.parametrize(
    ("code", "guarded"),
    [
        pytest.param("def f():\n    return cast(T.c, JSONB).has_key('x')\n", False, id="bare"),
        pytest.param(
            "def f():\n    return when_json_parseable(T.c, cast(T.c, JSONB).has_key('x'))\n", True, id="inside"
        ),
        pytest.param(
            "def f():\n    j = T.c.cast(JSONB)\n    o = j.op('?|')(y)\n    return when_json_parseable(T.c, o)\n",
            True,
            id="through-names",
        ),
        pytest.param("def f():\n    j = T.c.cast(JSONB)\n    return j.op('?|')(y)\n", False, id="bare-through-names"),
        pytest.param("def f():\n    return cast(null(), JSONB)\n", None, id="null-literal"),
    ],
)
def test_the_tripwire_tells_a_guarded_cast_from_a_bare_one(code, guarded):
    module = ast.parse(code)
    casts = _jsonb_casts(module)
    if guarded is None:
        assert casts == []
        return
    [cast_node] = casts
    assert _guarded(module, cast_node, _enclosing_function(module, cast_node)) is guarded


def test_every_jsonb_cast_of_a_column_is_guarded_or_says_why_it_needs_none():
    unguarded, listed = [], set()
    for path, module in modules():
        for cast_node in _jsonb_casts(module):
            function = _enclosing_function(module, cast_node)
            key = (path, function.name if function else "<module>")
            if key in _NO_GUARD_NEEDED:
                listed.add(key)
            elif not _guarded(module, cast_node, function):
                unguarded.append(f"{path}:{cast_node.lineno} ({key[1]})")
    assert unguarded == [], (
        "A jsonb cast of a json column aborts the whole statement on one row holding a lone surrogate "
        "or NUL escape: wrap it in app.core.json_sql.when_json_parseable(), or list it in "
        "_NO_GUARD_NEEDED with the reason no row can hold such a value."
    )
    assert listed == set(_NO_GUARD_NEEDED), "stale _NO_GUARD_NEEDED entries"


#: A cast to ``jsonb`` spelled in SQL text, either way PostgreSQL accepts it.
_RAW_JSONB_CAST = re.compile(r"::\s*jsonb\b|\bas\s+jsonb\b", re.IGNORECASE)


def _raw_jsonb_casts(module: ast.Module) -> list[int]:
    """Lines of the string constants of *module* casting to ``jsonb``, docstrings and bare strings aside."""
    return [
        node.lineno
        for node in walk(module)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and _RAW_JSONB_CAST.search(node.value)
        and not isinstance(parents(module).get(node), ast.Expr)
    ]


@pytest.mark.parametrize(
    ("code", "flagged"),
    [
        pytest.param('q = text("SELECT id FROM t WHERE c::jsonb ? :p")\n', True, id="double-colon"),
        pytest.param('q = text("SELECT id FROM t WHERE CAST(c AS JSONB) ? :p")\n', True, id="cast-as"),
        pytest.param('q = text("SELECT CAST(c AS\\n  jsonb) FROM t")\n', True, id="cast-as-lowercase"),
        pytest.param('q = text(f"SELECT {c}::JSONB FROM t")\n', True, id="f-string"),
        pytest.param('q = text("SELECT CAST(c AS TEXT) FROM t")\n', False, id="cast-to-text"),
        pytest.param('def f():\n    """Reads ``c::jsonb`` as ``CAST(c AS JSONB)``."""\n', False, id="docstring"),
    ],
)
def test_the_raw_sql_tripwire_reads_both_spellings(code, flagged):
    assert bool(_raw_jsonb_casts(ast.parse(code))) is flagged


def test_no_raw_sql_in_app_casts_to_jsonb():
    """``text("... ::jsonb ...")`` or ``CAST(... AS JSONB)`` hides a cast from the tripwire above.

    Index expressions aside: the models declare them, and ``json_sql`` recognises them.
    """
    offenders = [
        f"{path}:{line}"
        for path, module in modules()
        if not path.startswith("models/") and path != "core/json_sql.py"
        for line in _raw_jsonb_casts(module)
    ]
    assert offenders == []


def test_the_python_twins_read_like_the_operators():
    assert json_sql.jsonb_has_key({"github": []}, "github")
    assert json_sql.jsonb_has_key(["github", 1], "github")
    assert json_sql.jsonb_has_key("github", "github")
    assert not json_sql.jsonb_has_key([1, ["github"]], "github")
    assert not json_sql.jsonb_has_key(None, "github")
    assert json_sql.jsonb_field({"github": ["x"]}, "github") == ["x"]
    assert json_sql.jsonb_field(["github"], "github") is None
    assert json.loads(json_serializer({"a": "b"})) == {"a": "b"}
