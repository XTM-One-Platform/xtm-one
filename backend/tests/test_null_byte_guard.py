"""The engine-level NUL guard (``core/database.py``).

PostgreSQL refuses a NUL byte in any text value, so a request carrying one in
a plain ``String`` / ``Text`` column answered 500. Every bound string is
stripped at ``before_cursor_execute``, registered on the ``Engine`` class, so
the guard covers every engine and every write path (ORM flush, Core, text()).
The PostgreSQL behaviour is pinned in ``test_null_byte_guard_postgres.py``.
"""

import uuid

from sqlalchemy import String, create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from app.core.database import strip_null_bytes_from_parameters


class _Base(DeclarativeBase):
    pass


class _Note(_Base):
    __tablename__ = "nul_guard_notes"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(50))
    body: Mapped[str | None] = mapped_column(String(50), nullable=True)


def test_strings_lose_their_nul_bytes():
    assert strip_null_bytes_from_parameters(("a\x00b", "\x00"), False) == ("ab", "")
    assert strip_null_bytes_from_parameters({"t": "x\x00"}, False) == {"t": "x"}


def test_the_row_shape_is_kept():
    assert isinstance(strip_null_bytes_from_parameters(("a\x00",), False), tuple)
    assert isinstance(strip_null_bytes_from_parameters(["a\x00"], False), list)


def test_executemany_cleans_every_row():
    rows = [("a\x00", 1), ("b", 2), ("\x00c", 3)]
    assert strip_null_bytes_from_parameters(rows, True) == [("a", 1), ("b", 2), ("c", 3)]


def test_array_values_are_cleaned():
    assert strip_null_bytes_from_parameters((["a\x00", "b"],), False) == (["a", "b"],)


def test_clean_parameters_are_returned_as_is():
    row = ("abc", 1, None, b"\x00raw", uuid.uuid4())
    assert strip_null_bytes_from_parameters(row, False) is row
    rows = [row, row]
    assert strip_null_bytes_from_parameters(rows, True) is rows


def test_bytes_and_other_values_are_untouched():
    raw = b"\x00binary"
    cleaned = strip_null_bytes_from_parameters((raw, 3, 1.5), False)
    assert cleaned[0] is raw
    assert cleaned[1:] == (3, 1.5)


def test_the_guard_is_registered_on_every_engine():
    from app.core import database

    assert event.contains(Engine, "before_cursor_execute", database._strip_null_bytes_before_execute)
    # A brand new engine, never configured by the app, is covered too. SQLite
    # stores a NUL verbatim, so what comes back is what reached the driver.
    engine = create_engine("sqlite://")
    with engine.connect() as conn:
        value = conn.execute(text("SELECT :v"), {"v": "a\x00b"}).scalar_one()
    engine.dispose()

    assert value == "ab"


def test_a_flushed_object_holds_the_value_that_was_stored():
    # The engine strips the bound value; the flush strips the object too, so a
    # response built from it after the write says what the database holds.
    engine = create_engine("sqlite://")
    _Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        note = _Note(title="Q3\x00 plan", body=None)
        session.add(note)
        session.commit()
        assert note.title == "Q3 plan"

        note.body = "\x00draft"
        session.commit()
        assert note.body == "draft"
    engine.dispose()
