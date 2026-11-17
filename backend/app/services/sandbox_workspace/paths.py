# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""What a workspace path may be: the one rule every entry point applies.

A path is relative to ``workspace/``, ``/``-separated, NFC, and stays inside
the workspace by construction: no ``..``, no empty or ``.`` segment, no
control character, no backslash, at most :data:`MAX_PATH_CHARS` characters and
:data:`MAX_DEPTH` segments, every segment a name a filesystem accepts. The
model may write the path the way it reads it (``workspace/src/a.py``,
``/workspace/src/a.py`` or ``src/a.py``): the ``workspace`` prefix is dropped
here, once.
"""

from __future__ import annotations

import posixpath
import re
import unicodedata

#: The directory the workspace is mounted at, relative to the sandbox's working
#: directory, on every sandbox backend.
WORKSPACE_DIRNAME = "workspace"
#: The environment variable holding the workspace's absolute path in the sandbox.
WORKSPACE_ENV = "WORKSPACE_DIR"
#: Where installed packages live, relative to the workspace.
VENV_SITE_PACKAGES = ".venv/site-packages"

MAX_PATH_CHARS = 1024
MAX_DEPTH = 64
_MAX_SEGMENT_BYTES = 255
_BAD_CHARS = re.compile(r"[\x00-\x1f\x7f\\]")


class WorkspacePathError(ValueError):
    """A path the workspace refuses; ``str()`` says why, for the model."""


def _strip_mount_prefix(path: str) -> str:
    for prefix in (f"/{WORKSPACE_DIRNAME}/", f"./{WORKSPACE_DIRNAME}/", f"{WORKSPACE_DIRNAME}/"):
        if path.startswith(prefix):
            return path[len(prefix) :]
    if path in (WORKSPACE_DIRNAME, f"/{WORKSPACE_DIRNAME}", f"./{WORKSPACE_DIRNAME}"):
        return ""
    return path


def normalize(path: object, *, allow_root: bool = False) -> str:
    """*path* as the workspace stores it, or :class:`WorkspacePathError`.

    *allow_root* accepts the workspace itself (``""``), for a listing or a
    removal prefix; a file path never is.
    """
    if not isinstance(path, str):
        raise WorkspacePathError("a workspace path must be text")
    raw = unicodedata.normalize("NFC", path.strip())
    if _BAD_CHARS.search(raw):
        raise WorkspacePathError(f"{path[:80]!r}: a workspace path holds no control character and no backslash")
    raw = _strip_mount_prefix(raw)
    if raw.startswith("/"):
        raise WorkspacePathError(
            f"{path[:80]!r} is absolute: give a path inside the workspace (workspace/src/app.py, or src/app.py)"
        )
    raw = raw.rstrip("/")
    # A '.' segment names the directory it sits in (``src/.``, ``./src``): dropped, never refused.
    segments = [segment for segment in raw.split("/") if segment != "."] if raw else []
    if not segments:
        if allow_root:
            return ""
        raise WorkspacePathError("give a file path inside the workspace, not the workspace itself")
    if any(segment in ("", "..") for segment in segments):
        raise WorkspacePathError(f"{path[:80]!r}: no empty or '..' segment in a workspace path")
    raw = "/".join(segments)
    if len(raw) > MAX_PATH_CHARS or len(segments) > MAX_DEPTH:
        raise WorkspacePathError(
            f"{path[:80]!r}: a workspace path is at most {MAX_PATH_CHARS} characters and {MAX_DEPTH} levels deep"
        )
    if any(len(segment.encode("utf-8")) > _MAX_SEGMENT_BYTES for segment in segments):
        raise WorkspacePathError(f"{path[:80]!r}: a file or directory name is at most 255 bytes")
    return posixpath.join(*segments)


def is_under(path: str, prefix: str) -> bool:
    """Whether *path* is *prefix* or inside it (``prefix=""`` is the whole workspace)."""
    return not prefix or path == prefix or path.startswith(prefix + "/")


def is_bytecode_cache(path: str) -> bool:
    """Whether *path* is a Python bytecode cache (a ``__pycache__`` segment, a ``.pyc`` / ``.pyo`` file).

    What an import writes beside a module: never a change of a checkout, never
    a source write (#9518).
    """
    return "__pycache__" in path.split("/") or path.endswith((".pyc", ".pyo"))


def display(path: str) -> str:
    """*path* the way the sandbox code opens it."""
    return f"{WORKSPACE_DIRNAME}/{path}" if path else f"{WORKSPACE_DIRNAME}/"
