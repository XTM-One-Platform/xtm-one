"""One input per declared failure shape."""

from __future__ import annotations

import json
from typing import Any

_HARVESTER_MERGED = {
    "quarterly-threat-review.pdf": [
        {
            "text": "The operator staged a PowerShell loader that fetched the second stage over HTTPS.",
            "predictions": {"T1059.001": 0.93, "T1105": 0.77},
        },
        {
            "text": "Persistence was established through a scheduled task recreated on every boot.",
            "predictions": {"T1053.005": 0.88},
        },
    ],
    "incident-notes.docx": [
        {
            "text": "Credential material was read directly from process memory before exfiltration.",
            "predictions": {"T1003.001": 0.91, "T1105": 0.64},
        }
    ],
    "form_text_input": [
        {
            "text": "A signed binary in a writable directory was used to side-load the payload.",
            "predictions": {"T1574.002": 0.72},
        }
    ],
}

_DASHBOARD = {
    "name": "Exposure overview",
    "description": "Rolling view of exposure by business unit.",
    "widgets": [
        {
            "id": "w1",
            "type": "vertical-bar",
            "title": "Findings by severity",
            "entity": "finding",
            "group_by": "severity",
        },
        {
            "id": "w2",
            "type": "number",
            "title": "Open critical findings",
            "entity": "finding",
            "filter": "severity=critical",
        },
    ],
    "layout": {"columns": 12, "rows": 6},
}

_SCENARIO = {
    "name": "Phishing to persistence walkthrough",
    "description": "Two-stage scenario exercising initial access and persistence.",
    "category": "attack-scenario",
    "injects": [
        {"id": "i1", "title": "Delivery email", "type": "email", "depends_on": None},
        {"id": "i2", "title": "Loader execution", "type": "endpoint", "depends_on": "i1"},
    ],
    "teams": ["blue-team"],
}


def _as_answered(payload: object) -> str:
    """*payload* as indented JSON text, ending with a newline."""
    return json.dumps(payload, indent=2) + "\n"


def harvester_merged() -> str:
    return _as_answered(_HARVESTER_MERGED)


def harvester_empty() -> str:
    return _as_answered({"ttps": []})


def dashboard_payload() -> str:
    return _as_answered(_DASHBOARD)


def scenario_payload() -> str:
    return _as_answered(_SCENARIO)


UNPARSEABLE = "This is a prose answer. It was never JSON and never will be."

SCALAR_JSON = '"just a string"'

ABSENT_PATH = json.dumps({"present": {"here": [1, 2, 3]}})

KIND_MISMATCH = json.dumps({"values": [1, 2, 3]})

BLANK_LABEL = json.dumps({"a": {"predictions": {"": 1.0, "T1059": 0.9}}})

WHITESPACE_LABEL = json.dumps({"a": {"predictions": {"   ": 1.0, "T1059": 0.9}}})


def non_string_labels() -> str:
    return json.dumps({"values": [42, "T1059"]})


def nested_object_labels() -> str:
    return json.dumps({"values": [{"nested": True}, "T1059"]})


def oversized_source(byte_budget: int) -> str:
    """A syntactically valid document comfortably past ``byte_budget``."""
    filler = "x" * 1024
    entries = {f"k{i}": filler for i in range((byte_budget // 1024) + 8)}
    return json.dumps(entries)


def deeply_nested(depth: int) -> str:
    """A document nested ``depth`` levels, to cross the parse-depth bound."""
    doc: Any = "leaf"
    for _ in range(depth):
        doc = {"n": doc}
    return json.dumps(doc)


def expanding_payload(keys: int, per_key: int) -> str:
    """A cross-product that blows past the cardinality bound inside ``map_values``.

    ``keys * per_key`` labels, all distinct, so ``dedupe_sort`` could not shrink it
    back under the bound — which is the case a final-only check would miss.
    """
    return json.dumps(
        {
            f"file{k}.pdf": {
                "predictions": {f"T{k:04d}.{i:03d}": 0.5 for i in range(per_key)}
            }
            for k in range(keys)
        }
    )
