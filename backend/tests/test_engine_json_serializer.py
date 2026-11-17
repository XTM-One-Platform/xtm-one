"""Engine-level JSON serializer NUL sanitization.

PostgreSQL ``json`` columns store the serialized text verbatim, so a
``\\u0000`` escape arriving in an external payload (webhook bodies,
integration events) persists silently — and later poisons any read that
parses the column as ``jsonb`` (``UntranslatableCharacterError``: jsonb
cannot represent NUL), e.g. the agent-builder ``list_runs`` cross-run
search. The engine's ``json_serializer`` strips NUL from every ORM JSON
column write in one place, whatever the entry point.
"""

import json

from app.core.database import engine, json_serializer


def test_serializer_strips_nul_from_nested_payloads():
    payload = {
        "action": "opened",
        "body": "before\u0000after",
        "nested": {"items": ["a\u0000b", {"k\u0000ey": "v"}]},
    }

    serialized = json_serializer(payload)

    assert "\\u0000" not in serialized
    out = json.loads(serialized)
    assert out["body"] == "beforeafter"
    assert out["nested"]["items"][0] == "ab"
    assert out["nested"]["items"][1] == {"key": "v"}


def test_serializer_matches_plain_json_dumps_for_clean_payloads():
    """Clean payloads must serialize byte-identically to the SQLAlchemy
    default (plain ``json.dumps``) — the sanitizer is copy-on-write, so the
    common case adds no formatting drift."""
    payload = {"a": 1, "b": ["x", {"c": None}], "d": "text", "e": True}

    assert json_serializer(payload) == json.dumps(payload)


def test_engine_is_wired_to_the_sanitizing_serializer():
    """The guard only exists if the engine actually uses it — a regression
    that drops the ``json_serializer=`` kwarg would silently reopen the
    write path for poisoned payloads.

    ``_json_serializer`` is a private SQLAlchemy dialect attribute (there is
    no public accessor for the ``json_serializer`` engine kwarg), so read it
    with ``getattr`` — if SQLAlchemy renames it, the test fails with a clear
    assertion instead of an AttributeError."""
    serializer = getattr(engine.dialect, "_json_serializer", None)
    assert serializer is json_serializer


def test_tracing_engine_is_wired_to_the_sanitizing_serializer():
    """The dedicated tracing engine writes ORM JSON columns too (trace /
    span payloads) — it must go through the same sanitizing serializer so
    the engine-level choke point covers BOTH engines, not just the app
    engine (the local sink's call-site sanitization stays for its TEXT
    columns, which no JSON serializer can cover)."""
    from app.services.tracing import local_sink

    local_sink._build_tracing_session_factory()
    serializer = getattr(local_sink._TRACING_ENGINE.dialect, "_json_serializer", None)
    assert serializer is json_serializer


# ── Non-finite numbers, the same door ─────────────────────────────────
#
# JSON cannot spell NaN or Infinity, so PostgreSQL refuses both outright --
# ``invalid input syntax for type json``, taking the transaction with it. Python
# is the odd one out: ``json.loads`` accepts the bare tokens and ``json.dumps``
# writes them back, so anything decoded from an external payload can carry one
# into a column that will not take it. Nulled in the same place and for the same
# reason as NUL above.


def test_serializer_nulls_non_finite_numbers():
    payload = {
        "judge_duration_ms": float("nan"),
        "nested": {"items": [1, float("inf"), {"k": float("-inf")}]},
    }

    serialized = json_serializer(payload)

    # The tokens PostgreSQL rejects must not appear at all.
    assert "NaN" not in serialized
    assert "Infinity" not in serialized
    out = json.loads(serialized)
    assert out["judge_duration_ms"] is None
    assert out["nested"]["items"] == [1, None, {"k": None}]


def test_serializer_leaves_ordinary_numbers_alone():
    """Null means "not measured"; it must not swallow a real value, or a zero."""
    out = json.loads(
        json_serializer({"a": 0, "b": 0.0, "c": -1.5, "d": 1e308, "e": True})
    )
    assert out == {"a": 0, "b": 0.0, "c": -1.5, "d": 1e308, "e": True}
