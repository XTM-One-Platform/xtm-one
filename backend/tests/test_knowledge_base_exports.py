from __future__ import annotations

import io
import json
import uuid
import zipfile
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException, UploadFile

from app.api.v1 import knowledge_bases as kb_mod
from tests.archive_helpers import corrupt_member, declare_size, peak_mib_of


class _ExecScalarOne:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _ExecScalars:
    def __init__(self, items):
        self._items = items

    def scalars(self):
        return self

    def all(self):
        return list(self._items)


def _make_export_zip(*, export_id: str, part: int = 1, total_parts: int = 1) -> bytes:
    manifest = {
        "format": kb_mod._KB_EXPORT_FORMAT,
        "version": kb_mod._KB_EXPORT_VERSION,
        "exported_at": datetime.now(UTC).isoformat(),
        "export_id": export_id,
        "part": part,
        "total_parts": total_parts,
        "knowledge_base": {
            "name": "Imported KB",
            "description": None,
            "tags": [],
            "embedding_model": "text-embedding-3-large",
            "chunk_size": 512,
            "chunk_overlap": 128,
        },
        "documents": [],
        "sync_sources": [],
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
    return buffer.getvalue()


@pytest.mark.asyncio
async def test_render_export_zip_part_writes_manifest_and_document_content():
    kb = SimpleNamespace(
        id=uuid.uuid4(),
        name="Docs",
        description="desc",
        tags=["a"],
        embedding_model="text-embedding-3-large",
        chunk_size=512,
        chunk_overlap=128,
    )
    doc = SimpleNamespace(filename="readme.md", source_type="markdown", source_url=None)
    export_id = uuid.uuid4()

    zip_bytes, docs = kb_mod._render_export_zip_part(
        kb=kb,
        export_id=export_id,
        items=[
            {
                "doc": doc,
                "archive_path": "documents/0001-readme.md",
                "content_bytes": b"# hello",
            }
        ],
        part_number=1,
        total_parts=1,
        sync_sources=[],
    )

    assert docs[0].archive_path == "documents/0001-readme.md"
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        manifest = json.loads(zf.read("manifest.json"))
        assert manifest["export_id"] == str(export_id)
        assert manifest["part"] == 1
        assert manifest["total_parts"] == 1
        assert manifest["documents"][0]["filename"] == "readme.md"
        assert zf.read("documents/0001-readme.md") == b"# hello"


@pytest.mark.asyncio
async def test_export_knowledge_base_includes_empty_content_documents(monkeypatch):
    """A document whose content is an empty string ("") must still be exported
    with a payload so it round-trips on import (regression: a truthy test on
    doc.content dropped it, leaving archive_path null and silently losing the
    document on re-import)."""
    kb = SimpleNamespace(
        id=uuid.uuid4(),
        name="Docs",
        description=None,
        tags=[],
        embedding_model="text-embedding-3-large",
        chunk_size=512,
        chunk_overlap=128,
    )
    user = SimpleNamespace(id=uuid.uuid4(), is_admin=False)
    empty_doc = SimpleNamespace(
        id=uuid.uuid4(),
        content="",
        filename="empty.md",
        source_type="markdown",
        source_url=None,
    )

    monkeypatch.setattr(kb_mod, "get_user_knowledge_base", AsyncMock(return_value=kb))
    monkeypatch.setattr(kb_mod, "log_kb_activity", AsyncMock())
    monkeypatch.setattr(kb_mod, "log_audit", AsyncMock())

    captured: dict[str, bytes] = {}

    async def _upload(*, data, filename, content_type, prefix):
        captured["data"] = data
        return f"{prefix}/{filename}"

    storage = SimpleNamespace(upload=_upload, delete=AsyncMock())
    monkeypatch.setattr(kb_mod, "file_storage", storage)

    class _DB:
        def __init__(self):
            self._calls = 0

        async def execute(self, _statement):
            self._calls += 1
            # First query loads documents, second loads sync sources.
            return _ExecScalars([empty_doc] if self._calls == 1 else [])

        def add(self, _obj):
            return None

        async def flush(self):
            return None

        async def commit(self):
            return None

    response = await kb_mod.export_knowledge_base(
        kb_id=str(kb.id),
        user=user,
        db=_DB(),
    )

    assert len(response.packages) == 1
    with zipfile.ZipFile(io.BytesIO(captured["data"])) as zf:
        names = zf.namelist()
        doc_members = [n for n in names if n.startswith("documents/")]
        assert doc_members, "empty-content document should still be exported"
        # The payload is present (empty) and the manifest references it.
        assert zf.read(doc_members[0]) == b""
        manifest = json.loads(zf.read("manifest.json"))
        assert manifest["documents"][0]["archive_path"] == doc_members[0]


@pytest.mark.asyncio
async def test_export_knowledge_base_rejects_documents_without_content(monkeypatch):
    """A document with NULL content cannot round-trip (no payload member, dropped
    on import), so the export fails with 409 rather than silently emitting a
    non-roundtrippable package."""
    kb = SimpleNamespace(
        id=uuid.uuid4(),
        name="Docs",
        description=None,
        tags=[],
        embedding_model="text-embedding-3-large",
        chunk_size=512,
        chunk_overlap=128,
    )
    user = SimpleNamespace(id=uuid.uuid4(), is_admin=False)
    null_doc = SimpleNamespace(
        id=uuid.uuid4(),
        content=None,
        filename="broken.pdf",
        source_type="pdf",
        source_url=None,
    )

    monkeypatch.setattr(kb_mod, "get_user_knowledge_base", AsyncMock(return_value=kb))
    storage = SimpleNamespace(upload=AsyncMock(), delete=AsyncMock())
    monkeypatch.setattr(kb_mod, "file_storage", storage)

    class _DB:
        def __init__(self):
            self._calls = 0

        async def execute(self, _statement):
            self._calls += 1
            # First query loads documents, second loads sync sources.
            return _ExecScalars([null_doc] if self._calls == 1 else [])

    with pytest.raises(HTTPException) as err:
        await kb_mod.export_knowledge_base(kb_id=str(kb.id), user=user, db=_DB())

    assert err.value.status_code == 409
    storage.upload.assert_not_awaited()


@pytest.mark.asyncio
async def test_list_kb_export_packages_queries_with_stable_ordering(monkeypatch):
    kb = SimpleNamespace(id=uuid.uuid4())
    user = SimpleNamespace(id=uuid.uuid4(), is_admin=False)

    monkeypatch.setattr(kb_mod, "get_user_knowledge_base", AsyncMock(return_value=kb))

    now = datetime.now(UTC) + timedelta(hours=1)
    package = SimpleNamespace(
        id=uuid.uuid4(),
        export_id=uuid.uuid4(),
        part_number=1,
        total_parts=2,
        filename="knowledge-base-docs-part-01-of-02.zip",
        file_size=123,
        expires_at=now,
    )

    seen = {}

    class _DB:
        async def execute(self, statement):
            seen["sql"] = str(statement)
            return _ExecScalars([package])

    response = await kb_mod.list_kb_export_packages(
        kb_id=str(kb.id),
        user=user,
        db=_DB(),
    )

    assert len(response) == 1
    assert response[0].download_url.endswith(f"/exports/{package.id}/download")
    assert "ORDER BY kb_export_packages.expires_at DESC" in seen["sql"]
    assert "kb_export_packages.export_id ASC" in seen["sql"]
    assert "kb_export_packages.part_number ASC" in seen["sql"]


@pytest.mark.asyncio
async def test_download_kb_export_package_deletes_expired_package(monkeypatch):
    user = SimpleNamespace(id=uuid.uuid4(), is_admin=False)
    package = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=user.id,
        knowledge_base_id=uuid.uuid4(),
        export_id=uuid.uuid4(),
        filename="expired.zip",
        s3_object_name="kb-exports/x.zip",
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )

    db = AsyncMock()
    db.execute = AsyncMock(return_value=_ExecScalarOne(package))
    db.delete = AsyncMock()
    db.commit = AsyncMock()
    storage = SimpleNamespace(delete=AsyncMock(), presigned_url=AsyncMock())
    monkeypatch.setattr(kb_mod, "file_storage", storage)

    with pytest.raises(HTTPException) as err:
        await kb_mod.download_kb_export_package(
            package_id=str(package.id),
            user=user,
            db=db,
        )

    assert err.value.status_code == 410
    storage.delete.assert_awaited_once_with(package.s3_object_name)
    db.delete.assert_awaited_once_with(package)
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_import_knowledge_base_rejects_mixed_export_ids():
    user = SimpleNamespace(id=uuid.uuid4(), is_admin=False)
    first = UploadFile(
        file=io.BytesIO(_make_export_zip(export_id=str(uuid.uuid4()), part=1, total_parts=2)),
        filename="part1.zip",
    )
    second = UploadFile(
        file=io.BytesIO(_make_export_zip(export_id=str(uuid.uuid4()), part=2, total_parts=2)),
        filename="part2.zip",
    )

    with pytest.raises(HTTPException) as err:
        await kb_mod.import_knowledge_base(
            files=[first, second],
            user=user,
            db=AsyncMock(),
            _agentic_gate=None,
        )

    assert err.value.status_code == 400
    assert err.value.detail == "Provided ZIP files belong to different exports"


@pytest.mark.asyncio
async def test_import_knowledge_base_rejects_incomplete_multipart():
    user = SimpleNamespace(id=uuid.uuid4(), is_admin=False)
    export_id = str(uuid.uuid4())
    only_part = UploadFile(
        file=io.BytesIO(_make_export_zip(export_id=export_id, part=1, total_parts=2)),
        filename="part1.zip",
    )

    with pytest.raises(HTTPException) as err:
        await kb_mod.import_knowledge_base(
            files=[only_part],
            user=user,
            db=AsyncMock(),
            _agentic_gate=None,
        )

    assert err.value.status_code == 400
    assert "Incomplete multipart import" in err.value.detail
    assert "[2]" in err.value.detail


@pytest.mark.asyncio
async def test_import_knowledge_base_rejects_duplicate_parts():
    user = SimpleNamespace(id=uuid.uuid4(), is_admin=False)
    export_id = str(uuid.uuid4())
    first = UploadFile(
        file=io.BytesIO(_make_export_zip(export_id=export_id, part=1, total_parts=2)),
        filename="part1.zip",
    )
    duplicate = UploadFile(
        file=io.BytesIO(_make_export_zip(export_id=export_id, part=1, total_parts=2)),
        filename="part1-again.zip",
    )

    with pytest.raises(HTTPException) as err:
        await kb_mod.import_knowledge_base(
            files=[first, duplicate],
            user=user,
            db=AsyncMock(),
            _agentic_gate=None,
        )

    assert err.value.status_code == 400
    assert err.value.detail == "Duplicate package part 1 provided"


@pytest.mark.asyncio
async def test_import_knowledge_base_rejects_inconsistent_total_parts():
    user = SimpleNamespace(id=uuid.uuid4(), is_admin=False)
    export_id = str(uuid.uuid4())
    first = UploadFile(
        file=io.BytesIO(_make_export_zip(export_id=export_id, part=1, total_parts=2)),
        filename="part1.zip",
    )
    second = UploadFile(
        file=io.BytesIO(_make_export_zip(export_id=export_id, part=2, total_parts=3)),
        filename="part2.zip",
    )

    with pytest.raises(HTTPException) as err:
        await kb_mod.import_knowledge_base(
            files=[first, second],
            user=user,
            db=AsyncMock(),
            _agentic_gate=None,
        )

    assert err.value.status_code == 400
    assert err.value.detail == "Provided ZIP files report inconsistent total_parts"


@pytest.mark.asyncio
async def test_import_knowledge_base_rejects_aggregate_uncompressed_overflow(monkeypatch):
    """The decompressed-size bound is enforced across ALL uploaded parts, not
    just per ZIP: an attacker cannot split an oversized payload across many
    parts that each pass the per-ZIP check."""
    user = SimpleNamespace(id=uuid.uuid4(), is_admin=False)
    export_id = str(uuid.uuid4())
    raw1 = _make_export_zip(export_id=export_id, part=1, total_parts=2)
    raw2 = _make_export_zip(export_id=export_id, part=2, total_parts=2)

    def _uncompressed(raw: bytes) -> int:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            return sum(
                i.file_size for i in zf.infolist() if not i.filename.endswith("/")
            )

    # Cap = the larger single part, so each part clears the per-ZIP check but the
    # two together exceed the aggregate bound.
    cap = max(_uncompressed(raw1), _uncompressed(raw2))
    monkeypatch.setattr(kb_mod, "_KB_IMPORT_MAX_TOTAL_UNCOMPRESSED_BYTES", cap)

    first = UploadFile(file=io.BytesIO(raw1), filename="part1.zip")
    second = UploadFile(file=io.BytesIO(raw2), filename="part2.zip")

    with pytest.raises(HTTPException) as err:
        await kb_mod.import_knowledge_base(
            files=[first, second],
            user=user,
            db=AsyncMock(),
            _agentic_gate=None,
        )

    assert err.value.status_code == 400
    assert "Combined decompressed size" in err.value.detail


def test_import_per_file_cap_covers_export_document_cap():
    """Any document that can be exported must be re-importable: the import
    per-file cap must be at least the export per-document cap."""
    assert kb_mod._KB_IMPORT_MAX_FILE_BYTES >= kb_mod._KB_EXPORT_PART_MAX_BYTES


@pytest.mark.asyncio
async def test_import_knowledge_base_rejects_too_many_parts():
    """An unbounded number of per-ZIP-valid parts is rejected up-front so an
    attacker can't drive CPU/DB work before the aggregate guard trips."""
    user = SimpleNamespace(id=uuid.uuid4(), is_admin=False)
    raw = _make_export_zip(export_id=str(uuid.uuid4()), part=1, total_parts=1)
    files = [
        UploadFile(file=io.BytesIO(raw), filename=f"part{i}.zip")
        for i in range(kb_mod._KB_IMPORT_MAX_PARTS + 1)
    ]

    with pytest.raises(HTTPException) as err:
        await kb_mod.import_knowledge_base(
            files=files,
            user=user,
            db=AsyncMock(),
            _agentic_gate=None,
        )

    assert err.value.status_code == 400
    assert "Too many package parts" in err.value.detail


@pytest.mark.asyncio
async def test_import_knowledge_base_rejects_duplicate_archive_path():
    """The same document payload path appearing across the provided parts means
    a corrupted / tampered package, so the import is rejected rather than
    silently keeping first-seen (which would hide data loss)."""
    user = SimpleNamespace(id=uuid.uuid4(), is_admin=False)
    export_id = str(uuid.uuid4())
    archive_path = "documents/0001-a.txt"

    def _zip_with_doc(part: int) -> bytes:
        manifest = {
            "format": kb_mod._KB_EXPORT_FORMAT,
            "version": kb_mod._KB_EXPORT_VERSION,
            "exported_at": datetime.now(UTC).isoformat(),
            "export_id": export_id,
            "part": part,
            "total_parts": 2,
            "knowledge_base": {
                "name": "Imported KB",
                "description": None,
                "tags": [],
                "embedding_model": "text-embedding-3-large",
                "chunk_size": 512,
                "chunk_overlap": 128,
            },
            "documents": [
                {
                    "filename": "a.txt",
                    "source_type": "text",
                    "source_url": None,
                    "archive_path": archive_path,
                }
            ],
            "sync_sources": [],
        }
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("manifest.json", json.dumps(manifest))
            zf.writestr(archive_path, "hello")
        return buffer.getvalue()

    first = UploadFile(file=io.BytesIO(_zip_with_doc(1)), filename="part1.zip")
    second = UploadFile(file=io.BytesIO(_zip_with_doc(2)), filename="part2.zip")

    with pytest.raises(HTTPException) as err:
        await kb_mod.import_knowledge_base(
            files=[first, second],
            user=user,
            db=AsyncMock(),
            _agentic_gate=None,
        )

    assert err.value.status_code == 400
    assert "Duplicate document payload" in err.value.detail


@pytest.mark.asyncio
async def test_download_kb_export_package_denies_when_kb_access_revoked(monkeypatch):
    user = SimpleNamespace(id=uuid.uuid4(), is_admin=False)
    package = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=user.id,
        knowledge_base_id=uuid.uuid4(),
        export_id=uuid.uuid4(),
        filename="pkg.zip",
        s3_object_name="kb-exports/x.zip",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )

    db = AsyncMock()
    db.execute = AsyncMock(return_value=_ExecScalarOne(package))

    async def _deny(*_args, **_kwargs):
        raise HTTPException(status_code=404, detail="Knowledge base not found")

    monkeypatch.setattr(kb_mod, "get_user_knowledge_base", _deny)

    with pytest.raises(HTTPException) as err:
        await kb_mod.download_kb_export_package(
            package_id=str(package.id),
            user=user,
            db=db,
        )

    assert err.value.status_code == 404


@pytest.mark.asyncio
async def test_download_kb_export_package_no_audit_when_object_missing(monkeypatch):
    """A 404 from a missing storage object must NOT record a successful
    download in the audit trail (audit runs only after a successful fetch)."""
    from app.services.file_storage import FileNotFoundInStorageError

    user = SimpleNamespace(id=uuid.uuid4(), is_admin=False)
    package = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=user.id,
        knowledge_base_id=uuid.uuid4(),
        export_id=uuid.uuid4(),
        filename="pkg.zip",
        s3_object_name="kb-exports/gone.zip",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )

    db = AsyncMock()
    db.execute = AsyncMock(return_value=_ExecScalarOne(package))

    monkeypatch.setattr(
        kb_mod,
        "get_user_knowledge_base",
        AsyncMock(return_value=SimpleNamespace(id=package.knowledge_base_id)),
    )
    storage = SimpleNamespace(
        download=AsyncMock(side_effect=FileNotFoundInStorageError("gone"))
    )
    monkeypatch.setattr(kb_mod, "file_storage", storage)
    audit = AsyncMock()
    monkeypatch.setattr(kb_mod, "log_audit", audit)

    with pytest.raises(HTTPException) as err:
        await kb_mod.download_kb_export_package(
            package_id=str(package.id),
            user=user,
            db=db,
        )

    assert err.value.status_code == 404
    audit.assert_not_awaited()


# ── Members read within a budget, not as they declare ────────────────

_MIB = 2**20
_DOC_PATH = "documents/0001-a.txt"


def _package_with_document(payload: bytes) -> bytes:
    with zipfile.ZipFile(io.BytesIO(_make_export_zip(export_id=str(uuid.uuid4())))) as zf:
        manifest = json.loads(zf.read("manifest.json"))
    manifest["documents"] = [
        {"filename": "a.txt", "source_type": "text", "source_url": None, "archive_path": _DOC_PATH}
    ]
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        zf.writestr(_DOC_PATH, payload)
    return buffer.getvalue()


async def _import(raw: bytes):
    return await kb_mod.import_knowledge_base(
        files=[UploadFile(file=io.BytesIO(raw), filename="kb.zip")],
        user=SimpleNamespace(id=uuid.uuid4(), is_admin=False),
        db=AsyncMock(),
        _agentic_gate=None,
    )


@pytest.mark.asyncio
async def test_import_refuses_an_under_declared_member_holding_no_more_than_it_declared():
    """The bomb: 10 bytes declared, 64 MiB held. ``ZipFile.read``
    holds such a member twice over (128 MiB); the bounded read stops at what it
    declared and the member fails its CRC."""
    raw = declare_size(_package_with_document(b"\0" * (64 * _MIB)), _DOC_PATH, 10)
    peak, raised = await peak_mib_of(_import(raw))
    assert isinstance(raised, HTTPException)
    assert raised.status_code == 400
    assert raised.detail == "Invalid ZIP file."
    assert peak <= 16


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["damaged stream", "unknown compression", "encrypted"])
@pytest.mark.parametrize("member", ["manifest.json", _DOC_PATH])
async def test_import_refuses_an_unreadable_member_with_a_400(member, how):
    """Not the ``zlib.error``, ``NotImplementedError`` or ``RuntimeError`` reading it
    raises, which would be a 500."""
    raw = corrupt_member(_package_with_document(bytes(range(256)) * 64), member, how)
    with pytest.raises(HTTPException) as err:
        await _import(raw)
    assert err.value.status_code == 400
    assert err.value.detail == "Invalid ZIP file."


@pytest.mark.asyncio
async def test_import_refuses_a_document_payload_that_is_not_utf8():
    with pytest.raises(HTTPException) as err:
        await _import(_package_with_document(b"\xff\xfe\xfa"))
    assert err.value.status_code == 400
    assert err.value.detail == f"Document payload '{_DOC_PATH}' must be UTF-8 text"


@pytest.mark.asyncio
async def test_import_reads_the_document_payloads_off_the_event_loop(monkeypatch):
    """The manifest and the payloads decompress in a worker thread, not on the loop."""
    import threading

    loop_thread = threading.get_ident()
    seen: list[tuple[str, bool]] = []
    real_read = kb_mod.ArchiveBudget.read

    def _spy(self, archive, member, **kwargs):
        name = member.filename if isinstance(member, zipfile.ZipInfo) else member
        seen.append((name, threading.get_ident() != loop_thread))
        return real_read(self, archive, member, **kwargs)

    class _StopError(Exception):
        pass

    def _stop(**_kwargs):
        raise _StopError

    monkeypatch.setattr(kb_mod.ArchiveBudget, "read", _spy)
    # Every part read, the import stops where it creates the knowledge base.
    monkeypatch.setattr(kb_mod, "KnowledgeBase", _stop)
    with pytest.raises(_StopError):
        await _import(_package_with_document(b"hello"))
    assert seen == [("manifest.json", True), (_DOC_PATH, True)]


def test_members_past_the_import_budget_are_refused_as_combined_too_large():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("a.txt", b"x" * 600)
        zf.writestr("b.txt", b"y" * 600)
    budget = kb_mod.ArchiveBudget(1000)
    with zipfile.ZipFile(io.BytesIO(buffer.getvalue())) as zf:
        assert kb_mod._read_zip_member(zf, "a.txt", error="unused", budget=budget) == b"x" * 600
        with pytest.raises(HTTPException) as err:
            kb_mod._read_zip_member(zf, "b.txt", error="unused", budget=budget)
    assert err.value.status_code == 400
    assert err.value.detail.startswith("Combined decompressed size of the provided ZIP parts")
    assert budget.remaining == 400
