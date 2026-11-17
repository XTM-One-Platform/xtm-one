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

"""The class of worker a recording may ask for (``record_browser_demo(worker_class=...)``).

A recording worker says its class in its presence record (``recording_class``, with the GPU model it draws
WebGL on under ``recording_gpu``): the GPU model read from its WebGL renderer, lower-cased (``l4``, ``v100s``),
``gpu`` for a GPU whose model is not read, ``cpu`` on software GL. A take that asks for a class is claimed only by a
worker of that class (``VideoTakeAdapter.deal_claims``) and waits in the recording lane while that class is busy; one
that asks for none (or ``any``) is claimed by any recording worker, as before. A class no running recording worker
serves is refused before the take exists, naming the classes there are.

Nothing here changes how a take is captured or paced: it only chooses the worker.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from typing import Any

#: The class that asks for no class: any recording worker claims the take.
ANY = "any"
#: A worker recording on software GL.
CPU = "cpu"
#: A worker drawing on a GPU whose model its renderer does not name.
GPU = "gpu"
#: Where a take's execution keeps the class it asked for.
PAYLOAD_KEY = "worker_class"
#: What a class looks like: lower-case letters, digits and hyphens.
_CLASS_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
#: The model an NVIDIA renderer names: ``NVIDIA L4/PCIe``, ``Tesla V100S-PCIE-32GB``, ``NVIDIA A10G``.
_MODEL_RE = re.compile(r"\b(?:NVIDIA|Tesla)\s+(?:Tesla\s+|GeForce\s+|RTX\s+)?([A-Za-z]*\d+[A-Za-z0-9]*)")


def model_of(renderer: str | None) -> str | None:
    """The GPU model a WebGL renderer names (``L4``, ``V100S``), ``None`` when it names none."""
    found = _MODEL_RE.search(str(renderer or ""))
    return found.group(1) if found else None


def class_of(*, gpu: bool, renderer: str | None) -> str:
    """The class of a worker whose recordings draw on a GPU (*gpu*) under *renderer*: its model lower-cased,
    :data:`GPU` when the renderer names none, :data:`CPU` on software GL."""
    if not gpu:
        return CPU
    model = model_of(renderer)
    return model.lower() if model else GPU


def normalized(asked: Any) -> str | None:
    """*asked* as a class (lower-cased, stripped); ``None`` for none or :data:`ANY`."""
    said = str(asked or "").strip().lower()
    return None if said in ("", ANY) else said


def malformed(asked: str) -> bool:
    """Whether *asked* cannot be a class at all."""
    return _CLASS_RE.match(asked) is None


def served(records: Iterable[dict[str, Any]], *, serves: Callable[[dict[str, Any]], bool]) -> list[str]:
    """The classes the workers of *records* that record (*serves*: running and claiming the recording lane) say,
    sorted, each once."""
    return sorted(
        {str(record["recording_class"]) for record in records if record.get("recording_class") and serves(record)}
    )


def refusal(asked: str, available: list[str]) -> str | None:
    """Why a take asking for class *asked* is refused before it exists, ``None`` when a running worker serves it."""
    if malformed(asked):
        return f"worker_class {asked!r} is not a class: give any, or one of {_listed(available)}."
    if asked in available:
        return None
    return (
        f"No running recording worker is of class {asked!r}: nothing was recorded or charged. "
        f"Classes available now: {_listed(available)} (or any)."
    )


def _listed(available: list[str]) -> str:
    return ", ".join(available) if available else "none (no recording worker says its class now)"


def asked_of(execution: Any) -> str | None:
    """The class the take of *execution* asked for, ``None`` for none."""
    return normalized((getattr(execution, "payload", None) or {}).get(PAYLOAD_KEY))


def claimable(due: list[Any], own: str | None) -> list[Any]:
    """The executions of *due* a worker of class *own* may claim: those asking for no class, and those asking
    for *own*; a worker whose class is not decided yet (``None``) claims only those asking for none."""
    return [execution for execution in due if (asked := asked_of(execution)) is None or asked == own]


def words(record: dict[str, Any] | None) -> str:
    """``a worker of class l4 (GPU L4)``, ``a worker of class cpu (software GL)``; empty when *record* says no
    class."""
    if not isinstance(record, dict) or not record.get("class"):
        return ""
    cls = str(record["class"])
    gpu = record.get("gpu")
    what = f"GPU {gpu}" if gpu else "software GL" if cls == CPU else "GPU, model not read"
    return f"a worker of class {cls} ({what})"
