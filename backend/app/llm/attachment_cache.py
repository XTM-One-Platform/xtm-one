# SPDX-License-Identifier: Apache-2.0

"""Process-wide memo of the attachment work every provider request repeats.

The agentic loop sends the whole transcript on every iteration, and every
provider builder resolves each attachment the transcript carries again: the
stored bytes are downloaded, hashed, a PDF's pages counted, every image
validated, resized and re-encoded. None of it changes between two requests,
yet a conversation holding a photo, a PDF and three Retina screenshots spent
about 1.2 s per request on it, with the image work stalling the event loop
for ~300 ms at a time. The maps below keep the results so each piece
of work is done once per process:

* :data:`STORED_BYTES` - an object's bytes, keyed by object key and size. An
  object key names immutable bytes: an upload mints a fresh random key, a
  replaced template asset gets a new key, and the one deterministic writer
  (the brand-asset seed) derives the key from the content hash. A deleted
  object is forgotten by :func:`forget_stored_object`.
* :data:`CONTENT_DIGESTS` - the sha256 of an object's bytes, same key.
* :data:`PDF_PAGES` - a PDF's page count, keyed by content digest.
* :data:`PREPARED_IMAGES` - the provider-ready form of an image, keyed by a
  digest of the input and the preparation parameters: the preparation is a
  pure function of those.

Every map is bounded (entries, and bytes where values are large), so the
memory they can hold does not grow with the number of concurrent turns. A
value never grants access: a key is only ever looked up for a file the caller
already holds a reference to, and a digest only for bytes it already has.
"""

from __future__ import annotations

import hashlib
import threading
from collections import OrderedDict
from collections.abc import Callable, Hashable
from typing import Final

#: What :meth:`BoundedLRU.get` returns for a key it does not hold (``None`` is
#: a value: an image that cannot be prepared is remembered as such).
MISSING: Final = object()

#: Inputs above this size are hashed off the event loop by async callers
#: (hashlib releases the GIL, so a worker thread costs nothing).
OFFLOAD_DIGEST_MIN_BYTES: Final = 1 << 20


class BoundedLRU:
    """A thread-safe LRU map bounded by entry count and total value weight.

    ``weigh`` gives a value's weight in bytes (``0`` for small values); a
    value heavier than ``max_item_bytes`` is never kept, so one outsized file
    cannot evict everything else.
    """

    def __init__(
        self,
        *,
        max_entries: int,
        max_bytes: int = 0,
        max_item_bytes: int = 0,
        weigh: Callable[[object], int] | None = None,
    ) -> None:
        self._max_entries = max_entries
        self._max_bytes = max_bytes
        self._max_item_bytes = max_item_bytes
        self._weigh = weigh or (lambda _value: 0)
        self._items: OrderedDict[Hashable, tuple[object, int]] = OrderedDict()
        self._bytes = 0
        self._lock = threading.Lock()

    def get(self, key: Hashable) -> object:
        """The value kept for *key*, or :data:`MISSING`."""
        with self._lock:
            item = self._items.get(key)
            if item is None:
                return MISSING
            self._items.move_to_end(key)
            return item[0]

    def put(self, key: Hashable, value: object) -> None:
        weight = max(0, int(self._weigh(value)))
        if self._max_item_bytes and weight > self._max_item_bytes:
            return
        with self._lock:
            previous = self._items.pop(key, None)
            if previous is not None:
                self._bytes -= previous[1]
            self._items[key] = (value, weight)
            self._bytes += weight
            while self._items and (
                len(self._items) > self._max_entries
                or (self._max_bytes and self._bytes > self._max_bytes)
            ):
                _key, (_value, evicted) = self._items.popitem(last=False)
                self._bytes -= evicted

    def discard_where(self, predicate: Callable[[Hashable], bool]) -> None:
        with self._lock:
            for key in [k for k in self._items if predicate(k)]:
                self._bytes -= self._items.pop(key)[1]

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self._bytes = 0

    def __len__(self) -> int:
        return len(self._items)

    @property
    def weight(self) -> int:
        return self._bytes


def _weigh_bytes(value: object) -> int:
    return len(value) if isinstance(value, bytes | bytearray) else 0


def _weigh_prepared(value: object) -> int:
    if isinstance(value, tuple) and value:
        return len(value[0])
    return 0


#: Bounds of :data:`STORED_BYTES`: the largest download it keeps and its total.
#: ``resolve_payload_files`` prefetches no more than this either.
STORED_BYTES_MAX_ITEM: Final = 16 << 20
STORED_BYTES_MAX_TOTAL: Final = 48 << 20

STORED_BYTES: Final = BoundedLRU(
    max_entries=256,
    max_bytes=STORED_BYTES_MAX_TOTAL,
    max_item_bytes=STORED_BYTES_MAX_ITEM,
    weigh=_weigh_bytes,
)
CONTENT_DIGESTS: Final = BoundedLRU(max_entries=4096)
PDF_PAGES: Final = BoundedLRU(max_entries=4096)
PREPARED_IMAGES: Final = BoundedLRU(
    max_entries=512,
    max_bytes=48 << 20,
    max_item_bytes=16 << 20,
    weigh=_weigh_prepared,
)


def stored_object_key(object_name: str | None, size: int | None) -> tuple[str, int] | None:
    """The cache key of a stored object, ``None`` for a file not in storage."""
    if not object_name:
        return None
    return (object_name, int(size or 0))


def forget_stored_object(object_name: str) -> None:
    """Drop everything kept for *object_name* (the object was deleted)."""

    def _matches(key: Hashable) -> bool:
        return isinstance(key, tuple) and bool(key) and key[0] == object_name

    STORED_BYTES.discard_where(_matches)
    CONTENT_DIGESTS.discard_where(_matches)


def input_digest(data: bytes | str) -> bytes:
    """A 128-bit BLAKE2b digest of an image input, the prepared-image key."""
    if isinstance(data, str):
        data = data.encode("utf-8", errors="surrogatepass")
    return hashlib.blake2b(data, digest_size=16).digest()


def clear_all() -> None:
    """Empty every map (tests)."""
    for cache in (STORED_BYTES, CONTENT_DIGESTS, PDF_PAGES, PREPARED_IMAGES):
        cache.clear()
