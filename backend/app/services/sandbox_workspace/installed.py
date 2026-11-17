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

"""Installed packages, read where the install left them: the check container.

An install run by ``sandbox_run_command`` (``npm ci``, ``yarn install``, a
``pip install`` into ``.venv``) writes under directories the workspace never
records (:data:`commands.IGNORED_PATTERNS`): the manifest stays the source
tree, a commit never carries a dependency. A library's types, ``dist`` or
sources are then invisible to ``sandbox_grep`` and ``sandbox_read_file``,
which read the manifest only.

This module answers those two reads from the workspace's live command
container, read-only: :func:`installed_path` says whether a workspace path
lies under an installed tree, :func:`list_tree` lists the files under one,
:func:`read_files` fetches some of them as one archive. Nothing read here is
written to the manifest, so ``sandbox_checkout_status`` and a commit never see
it. No container is ever created for a read: with none held (it ended, or no
install ran) the caller says which check to run again.
"""

from __future__ import annotations

import io
import json
import logging
import shlex
import tarfile
from dataclasses import dataclass, field
from typing import Any

from app.services.sandbox_workspace.paths import normalize
from app.services.sandbox_workspace.session import WorkspaceSession

logger = logging.getLogger(__name__)

#: Directory names whose content an install writes and the manifest never records.
INSTALLED_DIRS = frozenset({"node_modules", ".venv", "site-packages"})
#: The last line of a read tool's answer taken from a command container instead of the workspace.
INSTALLED_READ_LABEL = "[read from the live command container: installed files, never recorded in the workspace]"
#: Files an installed-tree listing names at most (a search past it says so).
MAX_TREE_FILES = 20000


class InstalledUnavailableError(Exception):
    """No live command container holds the installed tree; ``str()`` says what to run."""


class NoLiveContainerError(InstalledUnavailableError):
    """No command container of the workspace is live (none started, or every one ended).

    Not a wrong argument: the read needs the install check run first. The tools answer it as a
    normal result naming that call (:func:`install_directory` gives the directory to install),
    never as an error.
    """


def install_directory(path: str, root: str) -> str:
    """The directory of the checkout at *root* whose install wrote the installed tree *path* lies in.

    ``co/frontend/node_modules/lib`` under the checkout ``co`` is ``frontend``; a tree at the
    checkout root is ``""``. *path* must lie under *root* and in an installed directory.
    """
    segments = path.split("/")
    cut = next(i for i, segment in enumerate(segments) if segment in INSTALLED_DIRS)
    head = "/".join(segments[:cut])
    if head == root:
        return ""
    prefix = f"{root}/" if root else ""
    return head[len(prefix) :] if head.startswith(prefix) else head


@dataclass
class Tree:
    """The files under an installed directory of the container, ``(path, size)`` in path order."""

    exists: bool = False
    files: list[tuple[str, int]] = field(default_factory=list)
    overflow: bool = False
    image: str = ""


def installed_path(path: str) -> bool:
    """Whether the workspace path *path* lies in (or is) an installed directory (:data:`INSTALLED_DIRS`)."""
    return any(segment in INSTALLED_DIRS for segment in path.split("/"))


def _held_images(state: dict[str, Any] | None) -> list[str]:
    """The image keys whose command container ``kernel_state`` records, the most recently started first."""
    held = (state or {}).get("commands") or {}
    rows = [(str(info.get("started_at") or ""), image) for image, info in held.items() if (info or {}).get("sandbox_id")]
    return [image for _at, image in sorted(rows, reverse=True)]


async def _connect(state: dict[str, Any] | None, image: str, api: Any) -> Any | None:
    sandbox_id = (((state or {}).get("commands") or {}).get(image) or {}).get("sandbox_id")
    if not sandbox_id:
        return None
    try:
        return await api.connect(sandbox_id)
    except Exception:
        logger.debug("installed-tree read: container of %s is gone", image, exc_info=True)
        return None


def _default_api(user_id: Any) -> Any:
    from app.services.sandbox_workspace.commands import _default_api as commands_api

    return commands_api(user_id)


async def _helper(sandbox: Any, action: str, *args: str) -> None:
    from app.services.sandbox_workspace.remote import REMOTE_WORKSPACE, TOOL_PATH, _run, install_tool

    await install_tool(sandbox)
    await _run(sandbox, " ".join(["python", TOOL_PATH, action, shlex.quote(REMOTE_WORKSPACE), *args]))


async def list_tree(
    workspace: WorkspaceSession, prefix: str, *, user_id: Any = None, sandbox_api: Any = None
) -> tuple[Tree, Any]:
    """The installed files under the workspace directory *prefix* (or that one file), and the container that holds them.

    The command containers the workspace records are tried, the most recent
    first; the first one where *prefix* exists answers. Raises
    :class:`InstalledUnavailableError` when none is live or none holds it.
    """
    from app.services.sandbox_workspace.remote import TOOL_DIR

    rel = normalize(prefix)
    state = await workspace.latest_kernel_state()
    images = _held_images(state)
    if not images:
        raise NoLiveContainerError(
            f"workspace/{rel} is an installed tree the workspace does not record, and no command container of this "
            "workspace is live: run the checkout's install check with sandbox_run_command, then read it again."
        )
    api = sandbox_api if sandbox_api is not None else _default_api(user_id)
    live = False
    for image in images:
        sandbox = await _connect(state, image, api)
        if sandbox is None:
            continue
        live = True
        try:
            await sandbox.files.write_file(f"{TOOL_DIR}/tree-in.json", json.dumps(rel).encode("utf-8"))
            await _helper(sandbox, "tree", f"{TOOL_DIR}/tree-in.json", f"{TOOL_DIR}/tree-out.json", str(MAX_TREE_FILES))
            raw = json.loads(await sandbox.files.read_bytes(f"{TOOL_DIR}/tree-out.json"))
        except Exception:
            logger.debug("installed-tree listing failed on %s", image, exc_info=True)
            continue
        if raw.get("exists"):
            files = [(str(p), int(s)) for p, s in raw.get("files", [])]
            return Tree(exists=True, files=files, overflow=bool(raw.get("overflow")), image=image), sandbox
    if not live:
        raise NoLiveContainerError(
            f"workspace/{rel} is an installed tree the workspace does not record, and the command containers of "
            "this workspace have ended: run the checkout's install check with sandbox_run_command again, then read it."
        )
    raise InstalledUnavailableError(
        f"workspace/{rel} is not in the workspace nor in its live command containers ({', '.join(images)}): "
        "check the path, or run the install check of that directory with sandbox_run_command."
    )


async def read_files(sandbox: Any, paths: list[str], *, max_bytes: int) -> dict[str, bytes]:
    """The bytes of the installed files *paths* (workspace paths, each at most *max_bytes*), fetched as one tar."""
    from app.services.sandbox_workspace.remote import TOOL_DIR

    if not paths:
        return {}
    await sandbox.files.write_file(f"{TOOL_DIR}/packin.json", json.dumps(list(paths)).encode("utf-8"))
    await _helper(sandbox, "packin", f"{TOOL_DIR}/packin.json", f"{TOOL_DIR}/packin.tar", str(int(max_bytes)))
    raw = await sandbox.files.read_bytes(f"{TOOL_DIR}/packin.tar")
    wanted = set(paths)
    out: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(raw)) as tf:
        for member in tf.getmembers():
            if not member.isfile() or member.name not in wanted:
                continue
            handle = tf.extractfile(member)
            if handle is not None:
                out[member.name] = handle.read()
    return out


async def read_file(
    workspace: WorkspaceSession, path: str, *, max_bytes: int, user_id: Any = None, sandbox_api: Any = None
) -> bytes:
    """The bytes of one installed file, fetched alone from the first live command container that holds it.

    Raises :class:`InstalledUnavailableError` when no command container is
    live, :class:`FileNotFoundError` when none holds the file (or it is past
    *max_bytes*).
    """
    rel = normalize(path)
    if not installed_path(rel):
        raise FileNotFoundError(rel)
    state = await workspace.latest_kernel_state()
    api = sandbox_api if sandbox_api is not None else _default_api(user_id)
    live = False
    for image in _held_images(state):
        sandbox = await _connect(state, image, api)
        if sandbox is None:
            continue
        live = True
        try:
            data = await read_files(sandbox, [rel], max_bytes=max_bytes)
        except Exception:
            logger.debug("installed-file read failed on %s", image, exc_info=True)
            continue
        if rel in data:
            return data[rel]
    if not live:
        raise NoLiveContainerError(
            f"workspace/{rel} is in an installed tree the workspace does not record, and no command container of "
            "this workspace is live: run the checkout's install check with sandbox_run_command, then read it again."
        )
    raise FileNotFoundError(rel)
