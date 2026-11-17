"""The voice cache of the Video Studio in memory, for the tests that speak through the real voice pipeline.

``voice.py`` keeps its takes, its check records and its provider lexicon ids
in object storage; :func:`in_memory_storage` swaps the storage methods they
use for a dictionary, so a test runs the pipeline end to end without S3.
"""

from __future__ import annotations

from types import SimpleNamespace


def in_memory_storage(monkeypatch) -> dict[str, bytes]:
    """Object storage as a dictionary (``name -> bytes``), for the methods the voice cache calls."""
    from app.services.file_storage import FileNotFoundInStorageError, file_storage
    from app.services.video_studio import voice

    objects: dict[str, bytes] = {}

    async def upload_path(path, name, content_type=None):
        with open(path, "rb") as source:
            objects[name] = source.read()
        return name

    async def download_to_path(name, path, *, missing_ok=False):
        if name not in objects:
            raise FileNotFoundInStorageError(name)
        with open(path, "wb") as out:
            out.write(objects[name])

    async def read_range(name, offset, length):
        if name not in objects:
            raise FileNotFoundInStorageError(name)
        return objects[name][offset : offset + length]

    async def upload_to(data, name, content_type=None):
        objects[name] = data
        return name

    async def upload(data, filename, content_type=None, prefix=""):
        name = f"{prefix}/{len(objects)}-{filename}"
        objects[name] = data
        return name

    async def names(prefix, *, recursive=True):
        return [name for name in objects if name.startswith(prefix)]

    for attribute, fake in {
        "upload_path": upload_path,
        "download_to_path": download_to_path,
        "read_range": read_range,
        "upload_to": upload_to,
        "upload": upload,
        "list_object_names": names,
    }.items():
        monkeypatch.setattr(file_storage, attribute, fake)
    monkeypatch.setattr(voice, "_LEXICON_IDS", {})
    monkeypatch.setattr(voice, "_PLACED", type(voice._PLACED)())
    monkeypatch.setattr(voice, "_SECONDS", type(voice._SECONDS)())
    return objects


def speech_config(name: str = "synthetic") -> SimpleNamespace:
    """A provider config as a speech target carries it (``router.SpeechTarget.config``)."""
    import uuid

    return SimpleNamespace(id=uuid.uuid4(), provider=name, display_name=name.title())
