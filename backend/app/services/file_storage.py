# SPDX-License-Identifier: Apache-2.0

"""S3-compatible file storage service (MinIO / AWS S3).

Handles upload, download, deletion, and pre-signed URL generation
for all file attachments across the platform (chat, assignments, KB).

Usage::

    from app.services.file_storage import file_storage

    # Upload a file
    object_name = await file_storage.upload(
        data=raw_bytes,
        filename="report.pdf",
        content_type="application/pdf",
        prefix="chat/conv-id",
    )

    # Get a pre-signed download URL (1 hour)
    url = await file_storage.presigned_url(object_name)

    # Download file bytes
    data = await file_storage.download(object_name)

    # Delete a file
    await file_storage.delete(object_name)
"""

from __future__ import annotations

import asyncio
import io
import logging
import threading
import uuid
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import urllib3
from minio import Minio
from minio.credentials import IamAwsProvider
from minio.error import S3Error

from app.core.cancellation import run_cleanup_to_completion
from app.core.config import settings
from app.core.utils import format_exception_message

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

MAX_UPLOAD_SIZE_BYTES = 40 * 1024 * 1024  # 40 MB
#: Keys one S3 DeleteObjects request carries (the API's own ceiling).
DELETE_BATCH = 1000

# S3/MinIO error codes that mean "the object/bucket isn't there" — i.e. the
# referenced key has been deleted, expired via a lifecycle policy, or was
# never persisted. These are *expected* in production (a generated file
# referenced by an old chat message may simply be gone) and must be handled
# gracefully rather than surfacing the raw S3 stack trace to callers.
_MISSING_OBJECT_S3_CODES = frozenset({"nosuchkey", "nosuchbucket"})


class FileNotFoundInStorageError(Exception):
    """Raised when an object cannot be found in the backing object store.

    This is the storage-layer's decoupled signal for a missing S3 key. The
    API layer catches it to return a user-friendly "file no longer available"
    response instead of leaking ``minio.error.S3Error`` internals (error
    codes, bucket names, request ids) into HTTP responses or logs.
    """

    def __init__(self, object_name: str):
        self.object_name = object_name
        super().__init__(f"Object not found in storage: {object_name}")


def _is_missing_object_error(exc: S3Error) -> bool:
    """Return whether an S3 error maps to a missing object/bucket key."""
    return (exc.code or "").lower() in _MISSING_OBJECT_S3_CODES

# ---------------------------------------------------------------------------
# MIME type helpers
# ---------------------------------------------------------------------------

MIME_TYPES: dict[str, str] = {
    # Documents
    "pdf": "application/pdf",
    "doc": "application/msword",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xls": "application/vnd.ms-excel",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "odt": "application/vnd.oasis.opendocument.text",
    "ods": "application/vnd.oasis.opendocument.spreadsheet",
    "rtf": "application/rtf",
    # Text / data
    "csv": "text/csv",
    "tsv": "text/tab-separated-values",
    "txt": "text/plain",
    "log": "text/plain",
    "md": "text/markdown",
    "markdown": "text/markdown",
    "json": "application/json",
    "ndjson": "application/x-ndjson",
    "jsonl": "application/x-ndjson",
    "xml": "application/xml",
    "html": "text/html",
    "htm": "text/html",
    "yaml": "application/yaml",
    "yml": "application/yaml",
    "toml": "application/toml",
    "ini": "text/plain",
    "conf": "text/plain",
    # Security / threat-intel formats — common ``generate_file`` outputs
    # for the CTI / CTEM agents. Mapping these explicitly means the
    # download endpoint can serve them with a meaningful Content-Type
    # instead of the legacy ``application/octet-stream`` fallback that
    # forced the browser to "guess" how to handle the file.
    "yar": "text/plain",          # YARA rule
    "yara": "text/plain",         # YARA rule
    "sigma": "application/yaml",  # Sigma rule (YAML-based)
    "stix": "application/json",   # STIX bundle (JSON-based)
    "snort": "text/plain",        # Snort/Suricata rule
    "rule": "text/plain",
    "rules": "text/plain",
    "ioc": "application/json",
    # Code / config (commonly attached as scratch / report artifacts)
    "py": "text/x-python",
    "js": "text/javascript",
    "ts": "text/typescript",
    "tsx": "text/typescript",
    "jsx": "text/javascript",
    "sh": "application/x-sh",
    "ps1": "text/plain",
    "sql": "application/sql",
    # Images
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
    "svg": "image/svg+xml",
    "bmp": "image/bmp",
    "tiff": "image/tiff",
    "tif": "image/tiff",
    # Audio / video
    "mp3": "audio/mpeg",
    "wav": "audio/wav",
    "webm": "audio/webm",
    "ogg": "audio/ogg",
    "mp4": "video/mp4",
    "m4a": "audio/mp4",
    "mov": "video/quicktime",
    "vtt": "text/vtt",
    # Archives
    "zip": "application/zip",
    "gz": "application/gzip",
    "tar": "application/x-tar",
    "tgz": "application/gzip",
    "7z": "application/x-7z-compressed",
}


def guess_content_type(filename: str) -> str:
    """Guess MIME type from filename extension."""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return MIME_TYPES.get(ext, "application/octet-stream")


# ---------------------------------------------------------------------------
# File storage service
# ---------------------------------------------------------------------------


def _credentials(timeout: urllib3.Timeout) -> dict:
    """Static keys, or the pod or instance role when ``S3_USE_AWS_ROLE`` is set."""
    if not settings.s3_use_aws_role:
        return {"access_key": settings.s3_access_key, "secret_key": settings.s3_secret_key}
    # Its own pool: STS and metadata calls are bounded, retried like the SDK
    # default, and never get the S3 TLS bypass.
    pool = urllib3.PoolManager(
        timeout=timeout,
        retries=urllib3.Retry(total=5, backoff_factor=0.2, status_forcelist=[500, 502, 503, 504]),
    )
    return {"credentials": IamAwsProvider(http_client=pool, region=settings.s3_region or None)}


class FileStorageService:
    """S3-compatible file storage abstraction."""

    def __init__(self) -> None:
        self._client: Minio | None = None
        self._client_lock = threading.Lock()
        self._bucket: str = settings.s3_bucket

    @property
    def client(self) -> Minio:
        """Lazy-init the MinIO client, once per process."""
        if self._client is None:
            with self._client_lock:
                if self._client is None:
                    client = self._new_client()
                    try:
                        self._ensure_bucket(client)
                    except S3Error:
                        # The store answered: object calls may still be allowed
                        # (no s3:ListBucket). Only a transport failure is retried.
                        self._client = client
                        raise
                    self._client = client
        return self._client

    @staticmethod
    def _new_client() -> Minio:
        # Bound every S3 HTTP operation so the synchronous MinIO SDK (run
        # via asyncio.to_thread) can never pin a default-executor thread
        # forever on a slow/blackholed object store — which would starve
        # asyncpg DNS resolution on that same executor and wedge the worker.
        # ``read`` is a per-read (between-bytes) timeout, so
        # steady large transfers are unaffected; only a stalled peer trips it.
        timeout = urllib3.Timeout(connect=settings.s3_connect_timeout, read=settings.s3_read_timeout)
        pool_kwargs: dict = {"num_pools": 4, "maxsize": 25, "timeout": timeout}
        if settings.s3_use_ssl and not settings.s3_reject_unauthorized:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            pool_kwargs["cert_reqs"] = "CERT_NONE"
        return Minio(
            endpoint=settings.s3_endpoint,
            **_credentials(timeout),
            secure=settings.s3_use_ssl,
            region=settings.s3_region or None,
            http_client=urllib3.PoolManager(**pool_kwargs),
        )

    def _ensure_bucket(self, client: Minio) -> None:
        try:
            if not client.bucket_exists(self._bucket):
                client.make_bucket(self._bucket)
                logger.info("Created S3 bucket: %s", self._bucket)
        except S3Error as exc:
            if exc.code == "BucketAlreadyOwnedByYou":  # another replica created it first
                return
            logger.error("Failed to check/create S3 bucket: %s", format_exception_message(exc))
            raise

    async def upload(
        self,
        data: bytes,
        filename: str,
        content_type: str | None = None,
        prefix: str = "",
    ) -> str:
        """Upload file bytes to S3 and return the object name.

        Args:
            data: Raw file bytes.
            filename: Original filename (used for extension preservation).
            content_type: MIME type. Auto-detected from filename if not provided.
            prefix: Optional path prefix (e.g. "chat/conv-uuid").

        Returns:
            The S3 object name (path) that can be used for download/deletion.
        """
        if not content_type:
            content_type = guess_content_type(filename)

        # Generate a unique object name preserving the original extension - one
        # the platform knows only: the name is logged, and what follows the last
        # dot of a file name can be any text its sender chose. ASCII is
        # checked first: a non-ASCII letter can lowercase to an ASCII one.
        ext = filename.rsplit(".", 1)[-1] if "." in filename else ""
        unique_name = f"{uuid.uuid4().hex}"
        if ext.isascii() and ext.lower() in MIME_TYPES:
            unique_name = f"{unique_name}.{ext.lower()}"

        object_name = f"{prefix}/{unique_name}" if prefix else unique_name
        return await self.upload_to(data, object_name, content_type)

    async def upload_to(
        self,
        data: bytes,
        object_name: str,
        content_type: str | None = None,
    ) -> str:
        """Upload file bytes under an EXPLICIT object name and return it.

        :meth:`upload` mints a random key so two uploads of the same file never
        collide; this variant is for callers whose key is deliberately
        deterministic — the built-in brand-asset seed derives it from the
        content hash, so re-running the seed converges on the same object instead
        of accumulating copies. The caller owns the key's uniqueness: uploading to
        an existing key overwrites it, as S3 semantics dictate.
        """
        if not content_type:
            content_type = guess_content_type(object_name)
        # Normalize slashes
        object_name = object_name.replace("//", "/").lstrip("/")

        stream = io.BytesIO(data)
        try:
            await asyncio.to_thread(
                lambda: self.client.put_object(self._bucket, object_name, stream, len(data), content_type)
            )
            logger.info("Uploaded file to S3: %s (%d bytes)", object_name, len(data))
        except S3Error as exc:
            logger.error("S3 upload failed for %s: %s", object_name, format_exception_message(exc))
            raise

        return object_name

    async def upload_path(self, path: str, object_name: str, content_type: str | None = None) -> str:
        """Upload a local file under an explicit object name, streamed from disk (large media)."""
        if not content_type:
            content_type = guess_content_type(object_name)
        object_name = object_name.replace("//", "/").lstrip("/")
        try:
            await asyncio.to_thread(
                lambda: self.client.fput_object(self._bucket, object_name, path, content_type=content_type)
            )
            logger.info("Uploaded file to S3: %s (from disk)", object_name)
        except S3Error as exc:
            logger.error("S3 upload failed for %s: %s", object_name, format_exception_message(exc))
            raise
        return object_name

    async def download_to_path(self, object_name: str, path: str, *, missing_ok: bool = False) -> None:
        """Download an object into a local file, streamed to disk (large media).

        *missing_ok* says a missing object is the caller's normal case (a cache
        miss): still :class:`FileNotFoundInStorageError`, logged at debug.
        """
        try:
            await asyncio.to_thread(lambda: self.client.fget_object(self._bucket, object_name, path))
        except S3Error as exc:
            if _is_missing_object_error(exc):
                logger.log(
                    logging.DEBUG if missing_ok else logging.WARNING,
                    "S3 object no longer available: %s (code=%s)",
                    object_name,
                    exc.code,
                )
                raise FileNotFoundInStorageError(object_name) from None
            logger.error("S3 download failed for %s: %s", object_name, format_exception_message(exc))
            raise

    async def read_range(self, object_name: str, offset: int, length: int) -> bytes:
        """``length`` bytes of an object from ``offset`` (an HTTP range request)."""

        def _sync_read() -> bytes:
            response = self.client.get_object(self._bucket, object_name, offset=offset, length=length)
            try:
                return response.read()
            finally:
                response.close()
                response.release_conn()

        try:
            return await asyncio.to_thread(_sync_read)
        except S3Error as exc:
            if _is_missing_object_error(exc):
                raise FileNotFoundInStorageError(object_name) from None
            raise

    _DOWNLOAD_CHUNK = 64 * 1024  # 64 KiB

    async def download(self, object_name: str) -> bytes:
        """Download file bytes from S3.

        Reads in chunks to avoid holding the entire response buffer in the
        MinIO urllib3 pool while also building the result.

        Args:
            object_name: The S3 object name/path.

        Returns:
            Raw file bytes.
        """

        def _sync_download() -> bytes:
            response = self.client.get_object(self._bucket, object_name)
            try:
                chunks: list[bytes] = []
                while True:
                    chunk = response.read(self._DOWNLOAD_CHUNK)
                    if not chunk:
                        break
                    chunks.append(chunk)
                return b"".join(chunks)
            finally:
                response.close()
                response.release_conn()

        try:
            return await asyncio.to_thread(_sync_download)
        except S3Error as exc:
            if _is_missing_object_error(exc):
                # Expected, recoverable condition: the key is simply gone
                # (deleted/expired/never-persisted). Log at warning — not
                # error — to avoid alerting noise, and raise a decoupled
                # domain exception so callers don't depend on MinIO codes.
                logger.warning(
                    "S3 object no longer available: %s (code=%s)",
                    object_name,
                    exc.code,
                )
                # Suppress the exception chain (``from None``): the warning
                # above already records the object name and S3 code, and
                # keeping the ``S3Error`` as ``__cause__`` would re-leak MinIO
                # internals (bucket, request/host ids) into any traceback a
                # downstream ``except Exception`` handler might log — defeating
                # the point of the decoupled, user-safe signal.
                raise FileNotFoundInStorageError(object_name) from None
            logger.error("S3 download failed for %s: %s", object_name, format_exception_message(exc))
            raise

    async def presigned_url(self, object_name: str, expires: int | None = None) -> str:
        """Generate a pre-signed download URL.

        Off the event loop: signing may first refresh role credentials over the network.
        """
        expiry = expires or settings.s3_presigned_expiry
        try:
            return await asyncio.to_thread(
                lambda: self.client.presigned_get_object(
                    bucket_name=self._bucket,
                    object_name=object_name,
                    expires=timedelta(seconds=expiry),
                )
            )
        except S3Error as exc:
            logger.error("S3 presigned URL failed for %s: %s", object_name, format_exception_message(exc))
            raise

    async def delete(self, object_name: str) -> None:
        """Delete a file from S3.

        Args:
            object_name: The S3 object name/path.
        """
        from app.llm.attachment_cache import forget_stored_object

        forget_stored_object(object_name)
        try:
            await asyncio.to_thread(lambda: self.client.remove_object(self._bucket, object_name))
            logger.info("Deleted file from S3: %s", object_name)
        except S3Error as exc:
            logger.error("S3 delete failed for %s: %s", object_name, format_exception_message(exc))
            # Don't raise on delete failures — best-effort cleanup

    async def delete_many(self, object_names: Iterable[str]) -> int:
        """Delete objects in bulk, :data:`DELETE_BATCH` keys per S3 DeleteObjects request. Returns objects removed.

        One request per thousand keys instead of one per key: a sandbox
        workspace holds thousands of objects, and deleting them one by one kept
        the workspace sweep on a single workspace for minutes. A key already
        gone counts as removed; a key the store refuses is logged and not
        counted. A store that does not answer stops the call at that batch,
        like :func:`app.services.file_cleanup.purge_storage_objects`, and what
        is left is logged. A store without the bulk API (``NotImplemented``)
        gets one delete per key.
        """
        from minio.deleteobjects import DeleteObject

        from app.llm.attachment_cache import forget_stored_object

        names = list(dict.fromkeys(name for name in object_names if name))
        removed = 0
        for start in range(0, len(names), DELETE_BATCH):
            chunk = names[start : start + DELETE_BATCH]
            for name in chunk:
                forget_stored_object(name)

            def _sync_delete(chunk: list[str] = chunk) -> list:
                return list(self.client.remove_objects(self._bucket, [DeleteObject(name) for name in chunk]))

            try:
                errors = await asyncio.to_thread(_sync_delete)
            except S3Error as exc:
                if (exc.code or "") != "NotImplemented":
                    logger.warning(
                        "S3 bulk delete refused (%s); %d object(s) left, starting with %s",
                        format_exception_message(exc),
                        len(names) - start,
                        chunk[0],
                    )
                    return removed
                for name in chunk:
                    await self.delete(name)
                removed += len(chunk)
                continue
            except Exception as exc:
                logger.warning(
                    "Object store unreachable during a bulk delete (%s); %d object(s) left, starting with %s",
                    type(exc).__name__,
                    len(names) - start,
                    chunk[0],
                )
                return removed
            refused = [error for error in errors if (getattr(error, "code", "") or "").lower() not in _MISSING_OBJECT_S3_CODES]
            for error in refused[:5]:
                logger.error("S3 delete failed for %s: %s", getattr(error, "name", "?"), getattr(error, "message", error))
            removed += len(chunk) - len(refused)
        return removed

    async def list_object_names(self, prefix: str, *, recursive: bool = True) -> list[str]:
        """Object names under ``prefix``, recursively by default.

        Used by trace retention to sweep everything one trace owns under its
        ``traces/<trace_id>/`` prefix. ``recursive=False`` lists one level: the
        objects directly under ``prefix`` and each sub-prefix once, ending in
        ``/``, so a caller can find the groups without listing every object in
        them. Returns an empty list on a storage
        error rather than raising: callers use this for best-effort cleanup,
        where an unreachable bucket must not abort the deletion it accompanies.
        """
        if not prefix:
            return []

        def _sync_list() -> list[str]:
            return [
                obj.object_name
                for obj in self.client.list_objects(
                    self._bucket, prefix=prefix, recursive=recursive
                )
                if obj.object_name
            ]

        try:
            return await asyncio.to_thread(_sync_list)
        except Exception as exc:
            # Every failure, not just ``S3Error``: a misconfigured endpoint, a
            # DNS error or a thread failure must degrade to "nothing to delete"
            # exactly like a bucket error does, because the callers use this for
            # best-effort cleanup alongside a row deletion that has to proceed.
            logger.warning(
                "S3 list failed for prefix %s: %s",
                prefix,
                format_exception_message(exc),
            )
            return []

    async def list_objects_modified(self, prefix: str) -> list[tuple[str, datetime | None]]:
        """``(name, last modified)`` of every object under ``prefix``, in one listing; empty on a storage error.

        For a retention sweep that ages objects without a row of their own
        (the Video Studio's figure stills): one listing, never one
        ``stat`` per object.
        """
        if not prefix:
            return []

        def _sync_list() -> list[tuple[str, datetime | None]]:
            return [
                (obj.object_name, obj.last_modified)
                for obj in self.client.list_objects(self._bucket, prefix=prefix, recursive=True)
                if obj.object_name
            ]

        try:
            return await asyncio.to_thread(_sync_list)
        except Exception as exc:
            logger.warning("S3 list failed for prefix %s: %s", prefix, format_exception_message(exc))
            return []

    async def stat(self, object_name: str) -> dict | None:
        """Get object metadata (size, content type, etc.).

        Returns:
            Dict with size, content_type, last_modified — or None if not found.
        """

        def _sync_stat() -> dict:
            s = self.client.stat_object(self._bucket, object_name)
            return {
                "size": s.size,
                "content_type": s.content_type,
                "last_modified": (
                    s.last_modified.isoformat() if s.last_modified else None
                ),
                "etag": s.etag,
            }

        try:
            return await asyncio.to_thread(_sync_stat)
        except S3Error:
            return None


# Module-level singleton
file_storage = FileStorageService()


@asynccontextmanager
async def objects_deleted_on_failure(db: AsyncSession) -> AsyncIterator[list[str]]:
    """The objects a transaction stores, deleted if it fails.

    Yields a list the block appends the name of each object it stores to, and
    the block commits last, so nothing it stored is durable before the end of
    the block. When it raises, or is cancelled (a client gone mid-import), the
    session is rolled back and every listed object deleted, so a failed import
    leaves neither rows nor files. The rollback and each deletion are attempted
    on their own: one that fails is logged and never replaces the block's error.
    They run to completion even when the task is cancelled again meanwhile
    (``run_cleanup_to_completion``).

    One window stays open: a cancellation landing inside the block's final
    commit can arrive after the server committed, and the files of the rows it
    kept are then deleted. Nothing on the client tells the two cases apart.
    """
    stored: list[str] = []
    try:
        yield stored
    except BaseException:
        await run_cleanup_to_completion(_roll_back_and_delete(db, stored))
        raise


async def _roll_back_and_delete(db: AsyncSession, stored: list[str]) -> None:
    try:
        await db.rollback()
    except Exception:
        # A dead connection: the server drops the transaction with it, and the
        # files are still deleted.
        logger.warning("Could not roll back a failed transaction", exc_info=True)
    for object_name in stored:
        try:
            await file_storage.delete(object_name)
        except Exception:
            logger.warning("Could not delete %s after a failed transaction", object_name, exc_info=True)
