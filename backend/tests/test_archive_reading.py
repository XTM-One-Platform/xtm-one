"""Reading an uploaded archive's members within a byte budget.

What a member declares is set by whoever built the archive, so the memory a read
holds is measured, never inferred from the declared sizes.
"""

import io
import zipfile

import pytest

from app.core.archive_reading import ArchiveBudget, ArchiveTooLargeError, read_bounded
from tests.archive_helpers import corrupt_member, declare_size, peak_mib

_MIB = 2**20


def _archive(members: dict[str, int], *, declare: dict[str, int] | None = None) -> zipfile.ZipFile:
    """An archive of zero-filled members of the given sizes, *declare* rewriting what some declare."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as out:
        for name, size in members.items():
            out.writestr(name, b"\0" * size)
    raw = buf.getvalue()
    for name, declared in (declare or {}).items():
        raw = declare_size(raw, name, declared)
    return zipfile.ZipFile(io.BytesIO(raw))


def test_a_member_within_its_limit_is_read_whole():
    archive = _archive({"a.json": 1000})
    assert read_bounded(archive, "a.json", 1000) == b"\0" * 1000


def test_a_member_past_its_limit_is_refused_holding_no_more_than_the_limit():
    archive = _archive({"big.bin": 64 * _MIB})
    peak, raised = peak_mib(lambda: read_bounded(archive, "big.bin", 4 * _MIB))
    assert isinstance(raised, ArchiveTooLargeError)
    assert "big.bin" in str(raised)
    # ``ZipFile.read`` holds the member twice over: 128 MiB here.
    assert peak <= 16


def test_a_member_declaring_less_than_it_holds_is_refused_as_corrupt_having_decompressed_what_it_declared():
    """The measured bomb: 10 bytes declared, far more held, a 2 GB budget."""
    archive = _archive({"big.bin": 64 * _MIB}, declare={"big.bin": 10})
    peak, raised = peak_mib(lambda: read_bounded(archive, "big.bin", 2 * 2**30))
    assert isinstance(raised, zipfile.BadZipFile)
    assert peak <= 16


def test_the_budget_is_spent_across_members():
    archive = _archive({"a.bin": 600, "b.bin": 600})
    budget = ArchiveBudget(1000)
    assert len(budget.read(archive, "a.bin")) == 600
    with pytest.raises(ArchiveTooLargeError):
        budget.read(archive, "b.bin")


def test_one_member_past_its_own_cap_is_refused_and_spends_nothing():
    archive = _archive({"a.json": 600})
    budget = ArchiveBudget(10_000)
    with pytest.raises(ArchiveTooLargeError):
        budget.read(archive, "a.json", max_member=500)
    assert budget.remaining == 10_000
    assert len(budget.read(archive, "a.json", max_member=600)) == 600


def _damaged(how: str) -> zipfile.ZipFile:
    """A one-member archive that cannot be read: its stream, its method or its encryption."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as out:
        out.writestr("a.bin", bytes(range(256)) * 64)
    return zipfile.ZipFile(io.BytesIO(corrupt_member(buf.getvalue(), "a.bin", how)))


@pytest.mark.parametrize("how", ["damaged stream", "unknown compression", "encrypted"])
def test_a_member_that_cannot_be_read_is_a_bad_zip_file(how):
    """Not the ``zlib.error``, ``NotImplementedError`` or ``RuntimeError`` reading it raises,
    which an import would answer with a 500."""
    with pytest.raises(zipfile.BadZipFile, match=r"a\.bin"):
        read_bounded(_damaged(how), "a.bin", 1_000_000)
