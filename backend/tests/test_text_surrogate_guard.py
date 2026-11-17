"""A UTF-16 surrogate in a text value never breaks a write.

UTF-8 cannot encode a surrogate, so the driver refused any statement binding a
string holding one - a JSON ``"\\ud800"`` escape in an agent name, a prompt, a
conversation title - and the request answered 500. The engine repairs every
bound string where it already strips a NUL (``core/database.py``): a
surrogate pair becomes the character it encodes, a lone surrogate U+FFFD, and
the flushed object is kept in step. The PostgreSQL behaviour is pinned in
``test_text_surrogate_guard_postgres.py``.
"""

import uuid

import pytest
from sqlalchemy import String, create_engine, text
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from app.core.database import strip_null_bytes_from_parameters
from app.core.utils import storable_text

EMOJI = "\U0001f600"


class _Base(DeclarativeBase):
    pass


class _Note(_Base):
    __tablename__ = "surrogate_guard_notes"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(50))


@pytest.mark.parametrize(
    ("value", "stored"),
    [
        pytest.param("a\ud800b", "a\ufffdb", id="lone-high"),
        pytest.param("\udc00", "\ufffd", id="lone-low"),
        pytest.param("\ud83d\ude00", EMOJI, id="pair-joined"),
        pytest.param("\ude00\ud83d", "\ufffd\ufffd", id="reversed-pair"),
        pytest.param("\ud800\ud800\udc00", "\ufffd\U00010000", id="high-before-a-pair"),
        pytest.param("x\u0000\ud800", "x\ufffd", id="nul-and-surrogate"),
        pytest.param("\ud83d\u0000\ude00", "\ufffd\ufffd", id="halves-a-nul-separates"),
    ],
)
def test_a_surrogate_becomes_text_postgres_can_hold(value, stored):
    assert storable_text(value) == stored
    stored.encode("utf-8")


def test_the_json_repair_never_joins_halves_a_nul_separates():
    from app.core.utils import sanitize_json_strings

    assert sanitize_json_strings({"v": "\ud83d\u0000\ude00"}) == {"v": "\ufffd\ufffd"}
    assert sanitize_json_strings({"v": "\ud83d\ude00\u0000"}) == {"v": "\ud83d\ude00"}


@pytest.mark.parametrize("value", ["plain", "caf\u00e9 \u4e2d " + EMOJI, "\\ud800", ""])
def test_a_string_without_one_is_returned_as_is(value):
    assert storable_text(value) is value


def test_every_bound_string_is_repaired_and_the_row_shape_kept():
    assert strip_null_bytes_from_parameters(("a\ud800", 1), False) == ("a\ufffd", 1)
    assert isinstance(strip_null_bytes_from_parameters(("a\ud800",), False), tuple)
    assert strip_null_bytes_from_parameters({"t": "\ud83d\ude00"}, False) == {"t": EMOJI}
    assert strip_null_bytes_from_parameters([("\udc00",), ("b",)], True) == [("\ufffd",), ("b",)]


def test_an_array_bind_is_repaired_item_by_item():
    assert strip_null_bytes_from_parameters((["a\ud800", "b\x00"],), False) == (["a\ufffd", "b"],)
    assert strip_null_bytes_from_parameters({"tags": ["\ud800"]}, False) == {"tags": ["\ufffd"]}


def test_every_bound_string_keeps_halves_a_nul_separates_apart():
    """The scalar rule holds wherever the string sits in the bind."""
    split = "\ud83d\x00\ude00"
    apart = "\ufffd\ufffd"
    assert strip_null_bytes_from_parameters((split,), False) == (apart,)
    assert strip_null_bytes_from_parameters(([split],), False) == ([apart],)
    assert strip_null_bytes_from_parameters({"t": split}, False) == {"t": apart}
    assert strip_null_bytes_from_parameters({"tags": [split]}, False) == {"tags": [apart]}
    assert strip_null_bytes_from_parameters([([split],)], True) == [([apart],)]


def test_a_named_row_keeps_its_nul_rule_for_containers():
    """As strip_null_bytes walked it: a tuple becomes a list, a dict loses its NULs."""
    assert strip_null_bytes_from_parameters({"ids": ("a\x00", "b")}, False) == {"ids": ["a", "b"]}
    assert strip_null_bytes_from_parameters({"doc": {"k\x00": "v\x00"}}, False) == {"doc": {"k": "v"}}
    assert strip_null_bytes_from_parameters({"t\x00": "x"}, False) == {"t": "x"}


def test_clean_parameters_are_still_returned_as_is():
    row = ("abc", f"caf\u00e9 {EMOJI}", 1, None, b"\xed\xa0\x80", uuid.uuid4())
    assert strip_null_bytes_from_parameters(row, False) is row
    named = {"a": "caf\u00e9", "b": ["x", EMOJI], "c": {"k": "v"}}
    assert strip_null_bytes_from_parameters(named, False) is named


def test_a_json_bind_is_left_alone():
    """The serializer binds JSON as ASCII text, escapes included."""
    serialized = '{"v": "\\ud800"}'
    assert strip_null_bytes_from_parameters((serialized,), False)[0] is serialized


def test_the_guard_covers_a_brand_new_engine():
    engine = create_engine("sqlite://")
    with engine.connect() as conn:
        value = conn.execute(text("SELECT :v"), {"v": "a\ud800b"}).scalar_one()
    engine.dispose()

    assert value == "a\ufffdb"


def test_a_flushed_object_holds_the_value_that_was_stored():
    engine = create_engine("sqlite://")
    _Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        note = _Note(title="Q3\ud800 plan")
        session.add(note)
        session.commit()
        assert note.title == "Q3\ufffd plan"

        note.title = "\ud83d\ude00 done"
        session.commit()
        assert note.title == f"{EMOJI} done"
    engine.dispose()
