"""The OpenCTI Ask Ariane lab: OpenCTI's own front, signed in, with the Ask Ariane panel answering.

``[service.lab-opencti-ask-ariane]`` in ``.xtm-one/checks.toml`` runs this script (standard library only)
on the ``python-dev`` image, next to the lab's OpenCTI (``[service.lab-opencti]`` and its four stores,
each on its ``AGENTIC_LABS_IMAGES`` key). It renders the Ask Ariane panel of an OpenCTI checkout the
agent chooses, so a UI change of ``@filigran/chatbot`` is reviewed in the product that embeds it:

1. The OpenCTI source is a nested checkout under this checkout, ``opencti/`` by default
   (``LAB_OPENCTI_CHECKOUT``): materialize_repository(OpenCTI-Platform/opencti, ref=<branch>,
   sub_paths=OPENCTI_SUB_PATHS, path="<this checkout>/opencti"). Re-materialize it at another ref
   and restart the service to compare two branches (master for the before captures).
2. Node and yarn are the lab's pinned downloads (``lab.NODE``, ``lab.YARN``: yarn 4.18.1 is OpenCTI's
   ``packageManager``); ``yarn workspaces focus opencti-front`` installs the front alone, then the
   Relay compiler runs on the committed schema and Vite serves the front on ``VITE_PORT``.
3. The lab's OpenCTI answers GraphQL (Vite proxies it to ``localhost:4000``); its AI terms are
   accepted at start (``filigran_chatbot_ai_cgu_status`` enabled) with the lab admin token.
4. This script listens on ``PORT``, the preview port: ``/chatbot/*`` is answered from the fixtures
   below (the shapes of opencti-front ``tests_e2e/chatbot/chatbotBackendMock.ts``), everything else,
   websockets included, goes to Vite. A message whose text holds ``error`` ends on an error frame,
   one holding ``slow`` holds the working state for ``SLOW_SECONDS`` before it streams, and one
   holding ``TOOLS_KEYWORD`` (``tools``) streams a turn with a tool call and reasoning, so the
   answer's Reasoning details button and dialog render in the host. The thinking and done frames
   carry the context usage, so the composer's context gauge renders; a message holding ``full``
   reports a context over 95% of the window (the pruning headline). A message holding ``FILES_KEYWORD``
   (``files``) answers with ``[[FILE:...]]``, ``[[PRESENTATION:...]]``, ``[[REPORT:...]]`` and ``[[VIDEO:...]]``
   markers in a table, a list item, a quote and alone on a line, with the files as attachments (download and
   working files) and the deck, report and video as ready artifacts; the seeded conversation "Files, decks and
   reports" restores the same answer. ``GET /chatbot/files/<id>/download`` serves each file's bytes and
   ``/chatbot/artifacts/<kind>/<id>`` describes and downloads the artifacts, so a card round needs no
   page-side ``fetch`` override.

To review a ``@filigran/*`` release the lab's OpenCTI tag does not pin yet, name the versions instead of editing
the nested checkout: an ``opencti-pins.json`` file at this checkout's root, written with sandbox_write_file
(``{"@filigran/chatbot": "3.25.0", "@filigran/design-system": "1.5.0"}``, no service env needed), or
``LAB_OPENCTI_PINS=@filigran/chatbot@3.25.0,@filigran/design-system@1.5.0``, which wins over the file. The front's package.json pins are
rewritten before the install and yarn resolves the lock for them (immutable installs off; OpenCTI's
``npmMinimalAgeGate`` still applies, ``@filigran/*`` being preapproved). The start log says
``ask-ariane: front pins overridden: ...`` and the ``lab up`` line names the chatbot version served.

To review a HOST change of OpenCTI master or a PR head (the Ask Ariane panel, the themes, the design tokens) on the
tag front, whose backend fields match the lab's image, materialize that ref a second time with the host paths only:
materialize_repository(OpenCTI-Platform/opencti, ref=<master or the PR branch>, sub_paths=["opencti-platform/opencti-front/"
+ p for p in HOST_PATHS], path="<this checkout>/opencti-host") (``LAB_OPENCTI_HOST_CHECKOUT`` names another directory).
At start the script copies those paths (``HOST_PATHS``, or ``LAB_OPENCTI_HOST_PATHS``: paths under opencti-front, comma
separated) over the tag checkout before the install and logs ``ask-ariane: host files from <repository>@<commit> (<ref>)
overlaid on the <version> front: ...``; the ``lab up`` line names the host ref. A relative import of an overlaid file the
tag front cannot resolve is logged as a WARNING. The copy edits the tag checkout: re-materialize ``opencti/`` to drop it.

Sign in as lab@filigran.io / lab-only-password. The theme is the user's (Profile, Theme: dark or
light). The start log names the seeded conversations (``seeded Ask Ariane conversation: ...``).
"""

from __future__ import annotations

import calendar
import http.client
import http.server
import io
import json
import os
import re
import select
import socket
import sys
import tarfile
import threading
import time
import urllib.request
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lab  # noqa: E402 - .xtm-one/lab/lab.py: the pinned toolchain and the process helpers

PORT = 3200
VITE_PORT = 3201
OPENCTI_URL = "http://127.0.0.1:4000"
OPENCTI_HEALTH = f"{OPENCTI_URL}/health?health_access_key=lab-only-opencti-health"
#: The lab OpenCTI's admin token ([service.lab-opencti] APP__ADMIN__TOKEN).
OPENCTI_ADMIN_TOKEN = "00000000-0000-4000-8000-000000000001"
#: What the nested OpenCTI checkout must hold: the front, its workspace siblings' manifests and the lock.
OPENCTI_SUB_PATHS = (
    "package.json",
    "yarn.lock",
    ".yarnrc.yml",
    "patch",
    "opencti-platform/opencti-front",
    "opencti-platform/eslint-plugin-custom-rules",
    "opencti-platform/opencti-graphql/package.json",
)
SLOW_SECONDS = 20
#: The front pin override (module docstring): the variable, then the file at this checkout's root.
PINS_ENV = "LAB_OPENCTI_PINS"
PINS_FILE = "opencti-pins.json"
#: The host overlay (module docstring): the second checkout, the paths copied from it, relative to opencti-front.
HOST_CHECKOUT_ENV = "LAB_OPENCTI_HOST_CHECKOUT"
HOST_PATHS_ENV = "LAB_OPENCTI_HOST_PATHS"
FRONT = Path("opencti-platform") / "opencti-front"
HOST_PATHS = (
    "src/private/components/chatbox",
    "src/components/Theme.d.ts",
    "src/components/ThemeDark.ts",
    "src/components/ThemeLight.ts",
    "src/components/fds-tokens.generated.ts",
)
#: A relative import in an overlaid source file, checked against the tag front.
RELATIVE_IMPORT = re.compile(r"""(?:from|import)\s*\(?\s*['"](\.{1,2}/[^'"]+)['"]""")
SOURCE_SUFFIXES = (".ts", ".tsx", ".js", ".jsx")
#: The suffixes probed when resolving a relative import: a declaration file (``Theme.d.ts``) resolves ``./Theme`` too.
RESOLVE_SUFFIXES = (*SOURCE_SUFFIXES, ".d.ts")
#: A pinned version: OpenCTI pins exact versions (``defaultSemverRangePrefix: ''``), prereleases included.
VERSION = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")
#: Seconds between two streamed chunks of an answer, so the streaming state can be captured.
CHUNK_SECONDS = 0.15
#: The word of a message that makes its turn call a tool (the Reasoning details fixture).
TOOLS_KEYWORD = "tools"
#: The tool the ``tools`` turn calls, its arguments and its result: a long JSON output, as a real search returns.
TOOL_NAME = "search_stix_core_objects"
TOOL_INPUT = {"search": "powershell loader", "types": ["Indicator", "Malware"], "first": 25}
TOOL_OUTPUT = {
    "edges": [
        {
            "node": {
                "id": f"indicator--5a1d0c2e-0b6f-4c41-9a51-6f7f0f6c{i:04d}",
                "entity_type": "Indicator",
                "name": f"PowerShell loader stage {i}",
                "pattern": f"[file:hashes.'SHA-256' = '{i:064x}']",
                "x_opencti_score": 70 + i,
                "created": f"2026-10-{i + 1:02d}T09:00:00Z",
                "objectLabel": ["loader", "powershell"],
            }
        }
        for i in range(12)
    ],
    "pageInfo": {"hasNextPage": False, "globalCount": 12},
}
TOOLS_REASONING = (
    "The question asks for the loader indicators. I search the STIX core objects for the PowerShell loader, "
    "keep the indicators scored above 70 and summarise them with the techniques of the campaign."
)

#: The word of a message that makes its answer carry file, deck, report and video markers (the file-card fixture).
FILES_KEYWORD = "files"


class StubFile(NamedTuple):
    """A file the stub serves: its name, content type and bytes (``chatbot_route`` answers it as a download)."""

    filename: str
    content_type: str
    data: bytes


#: A one-page PDF with no text: the smallest file every viewer opens (a deck or report download).
STUB_PDF = (
    b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"
)
#: The head of an MP4 (its ``ftyp`` box): enough for the download to carry a video type, not to play.
STUB_MP4 = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2mp41"

#: The files the ``files`` answer attaches, by id: two download files and one working file.
FILES = {
    "6b2e1f40-3c1d-4a8e-9f0b-2d4c6e8a0001": (
        StubFile("loader-indicators.csv", "text/csv",
                 b"name,pattern,score\nPowerShell loader stage 1,[file:hashes.'SHA-256' = '01'],71\n"
                 b"PowerShell loader stage 2,[file:hashes.'SHA-256' = '02'],72\n"),
        "download_file",
    ),
    "6b2e1f40-3c1d-4a8e-9f0b-2d4c6e8a0002": (
        StubFile("campaign-brief.pdf", "application/pdf", STUB_PDF),
        "download_file",
    ),
    "6b2e1f40-3c1d-4a8e-9f0b-2d4c6e8a0003": (
        StubFile("search-trace.json", "application/json",
                 json.dumps({"tool": "search_stix_core_objects", "results": 12}, indent=2).encode()),
        "working_file",
    ),
}


def attachment(file_id: str) -> dict:
    """The attachment block of *file_id*, in the shape of XTM One's ``attachment_meta_from_file_attachment``."""
    stub, tag = FILES[file_id]
    return {"filename": stub.filename, "type": stub.filename.rsplit(".", 1)[-1], "size": len(stub.data),
            "file_id": file_id, "content_type": stub.content_type, "file_tag": tag,
            "created_at": "2026-11-10T09:00:00+00:00"}


#: The deck, report and video the ``files`` answer shows, ready, in the shape of ``platform_chat_artifacts``.
ARTIFACTS = {
    ("presentation", "7c3f2a51-4d2e-4b9f-8a1c-3e5d7f9b0001"): {
        "title": "Loader campaign briefing", "slide_count": 8, "downloads": ["pdf"]},
    ("report", "7c3f2a51-4d2e-4b9f-8a1c-3e5d7f9b0002"): {
        "title": "Weekly threat report", "section_count": 5, "downloads": ["pdf"]},
    ("video", "7c3f2a51-4d2e-4b9f-8a1c-3e5d7f9b0003"): {
        "title": "Campaign walkthrough", "scene_count": 4, "duration_ms": 42_000, "status": "ready", "progress": 100,
        "downloads": ["mp4"]},
}
#: What an artifact downloads as, by format.
ARTIFACT_DOWNLOADS = {"pdf": ("application/pdf", STUB_PDF), "mp4": ("video/mp4", STUB_MP4)}


def artifact(kind: str, artifact_id: str) -> dict:
    """The descriptor of one stub artifact, its viewer link on the lab's own address."""
    viewer = {"presentation": "presentations", "report": "reports", "video": "videos"}[kind]
    return {"kind": kind, "id": artifact_id, **ARTIFACTS[(kind, artifact_id)],
            "url": f"http://localhost:{PORT}/{viewer}/{artifact_id}"}


_FILE_IDS = list(FILES)
_DECK, _REPORT, _VIDEO = (f"[[{kind.upper()}:{artifact_id}]]" for kind, artifact_id in ARTIFACTS)
FILES_ANSWER = "\n".join(
    [
        "Here is what I produced for the loader campaign.",
        "",
        "| Deliverable | File |",
        "|---|---|",
        f"| Indicators | [[FILE:{_FILE_IDS[0]}]] |",
        f"| Briefing deck | {_DECK} |",
        "",
        f"- The written brief: [[FILE:{_FILE_IDS[1]}]]",
        f"- The weekly report: {_REPORT}",
        "",
        f"> The search behind it, kept as a working file: [[FILE:{_FILE_IDS[2]}]]",
        "",
        "The walkthrough video:",
        "",
        _VIDEO,
        "",
        f"[[FILE:{_FILE_IDS[0]}]]",
    ]
)
FILES_ATTACHMENTS = [attachment(file_id) for file_id in FILES]
FILES_ARTIFACTS = [artifact(kind, artifact_id) for kind, artifact_id in ARTIFACTS]

AGENTS = [
    {
        "id": "73b39208-e45e-4660-bd43-ff057a12d5dd",
        "name": "CTEM Assistant",
        "slug": "ctem-assistant",
        "description": "Primary entry point of the XTM One platform.",
    },
    {
        "id": "54f6d934-3bf1-434e-919c-3605f76c8482",
        "name": "OpenCTI Assistant",
        "slug": "opencti-assistant",
        "description": "Cyber Threat Intelligence analyst for OpenCTI.",
    },
]

LONG_ANSWER = "\n".join(
    [
        "## Campaign overview",
        "",
        "The intrusion set behind this campaign reuses **three loaders** and one *initial access broker*. "
        "The timeline below is built from the reports ingested this week [1].",
        "",
        "| Stage | Technique | First seen | Confidence |",
        "|---|---|---|---|",
        "| Initial access | T1566.001 Spearphishing attachment | 2026-10-02 | High |",
        "| Execution | T1059.001 PowerShell | 2026-10-03 | Medium |",
        "| Persistence | T1547.001 Run keys | 2026-10-05 | Medium |",
        "| Exfiltration | T1567.002 Exfiltration to cloud storage | 2026-10-09 | Low |",
        "",
        "### What to check first",
        "",
        "1. The indicators created in the last 7 days with a score above 70",
        "2. The sightings on your **crown jewel** assets",
        "   - mail gateways",
        "   - VPN concentrators",
        "3. The detection rules that never fired",
        "",
        "```kql",
        "DeviceProcessEvents",
        '| where FileName =~ "powershell.exe"',
        '| where ProcessCommandLine has_any ("-enc", "FromBase64String")',
        "| summarize count() by DeviceName, bin(Timestamp, 1h)",
        "```",
        "",
        "> The loader hashes rotate every 48 hours: prefer the behavioural rules above to hash lists.",
        "",
        "Inline code such as `T1059.001` and a link to [the MITRE page](https://attack.mitre.org/techniques/T1059/001/) "
        "render inline.",
        "",
        "**Sources**",
        "",
        "[1] Weekly threat report, internal, 2026-10-10",
    ]
)

CONVERSATIONS = [
    {
        "conversation_id": "5a1d0c2e-0b6f-4c41-9a51-6f7f0f6c0001",
        "title": "Long Markdown thread: campaign overview",
        "messages": [
            {"role": "user", "content": "Summarise the campaign targeting our mail gateways, with the techniques."},
            {"role": "assistant", "content": LONG_ANSWER},
            {"role": "user", "content": "And the detection rule to start with?"},
            {"role": "assistant", "content": "Start with the PowerShell rule in the code block above: it covers "
             "the execution stage of every loader seen so far."},
        ],
    },
    {
        "conversation_id": "5a1d0c2e-0b6f-4c41-9a51-6f7f0f6c0002",
        "title": "Hello, who are you?",
        "messages": [
            {"role": "user", "content": "Hello, who are you?"},
            {"role": "assistant", "content": "I'm the **CTEM Assistant**, your entry point on the XTM One platform.\n\n"
             "## What I do\n- Coordinate the platform specialists\n- Look up threats and campaigns in OpenCTI\n\n"
             "What would you like help with today?"},
        ],
    },
    {
        "conversation_id": "5a1d0c2e-0b6f-4c41-9a51-6f7f0f6c0003",
        "title": "Files, decks and reports",
        "messages": [
            {"role": "user", "content": "Give me the files, the deck, the report and the video of the loader campaign."},
            {"role": "assistant", "content": FILES_ANSWER, "attachments": FILES_ATTACHMENTS,
             "artifacts": FILES_ARTIFACTS},
        ],
    },
]


def sse(events: list[dict]) -> list[bytes]:
    """*events* as server-sent event frames, one per event."""
    return [f"data: {json.dumps(event)}\n\n".encode() for event in events]


#: The context window the stubbed turns report, and the share of it they say is used.
CONTEXT_WINDOW = 1_000_000
CONTEXT_TOKENS = 36_000
#: A message holding ``full`` reports a context over 95% of the window, so the pruning headline renders.
CONTEXT_TOKENS_FULL = 962_000


def context_usage(text: str) -> dict:
    """The context usage fields of a turn answering *text*, as XTM One sends them on progress and done frames."""
    used = CONTEXT_TOKENS_FULL if "full" in text.lower() else CONTEXT_TOKENS
    system, tools = 9_000, 7_000
    breakdown = {"system": system, "tools": tools, "conversation": used - system - tools}
    return {"context_tokens": used, "context_window": CONTEXT_WINDOW, "context_breakdown": breakdown}


def answer_frames(text: str, conversation_id: str) -> tuple[list[dict], list[dict]]:
    """The frames of a turn answering *text*: (frames sent before the hold, frames streamed after it).

    The thinking and done frames carry the context usage (``context_usage``), so the composer's context
    gauge renders after the first message.
    """
    lowered = text.lower()
    usage = context_usage(text)
    preparing = [
        {"type": "status", "status": "preparing", "phase": "memory"},
        {"type": "status", "status": "thinking", "iteration": 1, **usage},
    ]
    if "error" in lowered:
        return preparing, [{"type": "error", "content": "The agent could not answer: the lab answered with an error on purpose."}]
    tools = TOOLS_KEYWORD in lowered
    # A word match, so "profiles" or "filesystem" keep the default answer.
    files = re.search(rf"\b{FILES_KEYWORD}\b", lowered) is not None
    answer = FILES_ANSWER if files else LONG_ANSWER
    words = answer.split(" ")
    chunks = [" ".join(words[i : i + 8]) + (" " if i + 8 < len(words) else "") for i in range(0, len(words), 8)]
    streamed: list[dict] = []
    if tools:
        # The turn calls one tool before it answers: the status line names it, the done frame carries its trace.
        streamed += [
            {"type": "status", "status": "tool_start", "tools": [TOOL_NAME], "iteration": 1},
            {"type": "status", "status": "tool_done", "tools": [TOOL_NAME], "iteration": 1},
            {"type": "status", "status": "thinking", "iteration": 2, **usage},
        ]
    streamed += [{"type": "status", "status": "streaming"}] + [{"type": "stream", "content": c} for c in chunks]
    done = {
        "type": "done",
        "content": answer,
        "conversation_id": conversation_id,
        "message_id": "786d810d-34f8-48f6-ad61-f8f18707b79a",
        **usage,
    }
    if tools:
        done.update({
            "tool_names": [TOOL_NAME],
            "tool_call_count": 1,
            "iterations": 2,
            "reasoning": TOOLS_REASONING,
            "tool_call_trace": [{"name": TOOL_NAME, "input": json.dumps(TOOL_INPUT),
                                 "output": json.dumps(TOOL_OUTPUT, indent=2), "success": True}],
        })
    if files:
        done.update({"attachments": FILES_ATTACHMENTS, "artifacts": FILES_ARTIFACTS})
    streamed.append(done)
    return preparing, streamed


#: The word of a message that moves the quota read to its near-cap state (85 of 100 today), for the warning tone.
QUOTA_KEYWORD = "quota"
#: The agentic actions the quota read reports today: (used, limit), then the near-cap pair the keyword switches to.
QUOTA_TODAY = (12, 100)
QUOTA_NEAR_CAP = (85, 100)
#: Whether a message holding ``QUOTA_KEYWORD`` was sent since the start (one lab, one panel: a process-wide switch).
QUOTA_STATE = {"near_cap": False}


def quota_status(near_cap: bool = False, now: float | None = None) -> dict:
    """The ``/chatbot/quota`` answer, in the shape of XTM One's ``GET /web/chat/quota`` (``PlatformChatQuotaOut``).

    Today's row first (the composer's quota button reads "N agentic actions today"), then this month and this
    year, each with the UTC instant its counter starts again, so the chatbot's ``QuotaIndicator`` renders natively.
    """
    used, limit = QUOTA_NEAR_CAP if near_cap else QUOTA_TODAY
    day = time.gmtime(time.time() if now is None else now)
    tomorrow = time.gmtime(calendar.timegm((day.tm_year, day.tm_mon, day.tm_mday + 1, 0, 0, 0)))
    next_month = (day.tm_year + day.tm_mon // 12, day.tm_mon % 12 + 1)
    resets = {
        "daily": time.strftime("%Y-%m-%dT00:00:00Z", tomorrow),
        "monthly": f"{next_month[0]:04d}-{next_month[1]:02d}-01T00:00:00Z",
        "yearly": f"{day.tm_year + 1:04d}-01-01T00:00:00Z",
    }
    periods = [
        {"period": "daily", "used": used, "limit": limit, "scope": "user", "resets_at": resets["daily"]},
        {"period": "monthly", "used": used * 10, "limit": limit * 30, "scope": "user", "resets_at": resets["monthly"]},
        {"period": "yearly", "used": used * 40, "limit": limit * 365, "scope": "user", "resets_at": resets["yearly"]},
    ]
    return {"used": used, "limit": limit, "period": "daily", "scope": "user", "periods": periods}


def chatbot_route(method: str, path: str, body: dict) -> tuple[int, str, object]:
    """The answer of the stubbed ``/chatbot/*`` *path*: (status, content type, JSON value or None for a stream)."""
    route = path.split("?", 1)[0].removeprefix("/chatbot")
    if route == "/config":
        return 200, "application/json", {"xtm_one_url": f"http://localhost:{PORT}", "xtm_one_configured": True}
    if route.startswith("/agents"):
        return 200, "application/json", AGENTS
    if route == "/prompts":
        return 200, "application/json", []
    if route == "/quota":
        return 200, "application/json", quota_status(QUOTA_STATE["near_cap"])
    if route == "/sessions" and method == "GET":
        summaries = [
            {"conversation_id": c["conversation_id"], "title": c["title"], "updated_at": "2026-11-10T09:00:00+00:00",
             "message_count": len(c["messages"]), "agent_id": AGENTS[0]["id"], "agent_name": AGENTS[0]["name"]}
            for c in CONVERSATIONS
        ]
        return 200, "application/json", {"conversations": summaries}
    if route == "/sessions" and method == "POST":
        wanted = body.get("conversation_id")
        found = next((c for c in CONVERSATIONS if c["conversation_id"] == wanted), None)
        if found is None:
            return 200, "application/json", {"conversation_id": wanted or "5a1d0c2e-0b6f-4c41-9a51-6f7f0f6c00ff", "messages": []}
        return 200, "application/json", {"conversation_id": found["conversation_id"], "messages": found["messages"]}
    if route.startswith("/sessions") and method == "DELETE":
        return 200, "application/json", {}
    files = re.fullmatch(r"/files/([^/]+)(?:/download)?", route)
    if files and method == "GET":
        found = FILES.get(files.group(1))
        return (200, found[0].content_type, found[0]) if found else (404, "application/json", {})
    target = re.fullmatch(r"/artifacts/([a-z]+)/([^/]+)(/download)?", route)
    if target and (target.group(1), target.group(2)) in ARTIFACTS:
        kind, artifact_id = target.group(1), target.group(2)
        described = artifact(kind, artifact_id)
        if not target.group(3) and method == "GET":
            return 200, "application/json", described
        wanted = str(body.get("format") or "")
        if target.group(3) and method == "POST" and wanted in described["downloads"]:
            ctype, data = ARTIFACT_DOWNLOADS[wanted]
            return 200, ctype, StubFile(f"{described['title']}.{wanted}", ctype, data)
    if route == "/upload":
        return 200, "application/json", {"file_ids": ["lab-file-0001"]}
    if route == "/messages" and method == "POST":
        if QUOTA_KEYWORD in str(body.get("content") or "").lower():
            QUOTA_STATE["near_cap"] = True
        return 200, "text/event-stream", None
    return 404, "application/json", {}


class Front(http.server.BaseHTTPRequestHandler):
    """The preview port: ``/chatbot/*`` from the fixtures, the rest (websockets included) to Vite."""

    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - the base class's name
        return

    def _body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _chatbot(self) -> None:
        raw = self._body()
        try:
            body = json.loads(raw) if raw and "json" in (self.headers.get("Content-Type") or "") else {}
        except ValueError:
            body = {}
        status, ctype, value = chatbot_route(self.command, self.path, body if isinstance(body, dict) else {})
        if status == 404:
            lab.log(f"ask-ariane: unstubbed {self.command} {self.path}")
        if value is None and ctype == "text/event-stream":
            self._stream(body if isinstance(body, dict) else {})
            return
        data = value.data if isinstance(value, StubFile) else json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        if isinstance(value, StubFile):
            self.send_header("Content-Disposition", f'attachment; filename="{value.filename}"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _stream(self, body: dict) -> None:
        text = str(body.get("content") or "")
        before, after = answer_frames(text, str(body.get("conversation_id") or CONVERSATIONS[0]["conversation_id"]))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        for frame in sse(before):
            self.wfile.write(frame)
            self.wfile.flush()
        time.sleep(SLOW_SECONDS if "slow" in text.lower() else 1)
        for frame in sse(after):
            self.wfile.write(frame)
            self.wfile.flush()
            time.sleep(CHUNK_SECONDS)

    def _tunnel(self) -> None:
        """A websocket (Vite's HMR, OpenCTI's subscriptions through Vite): the bytes, both ways."""
        upstream = socket.create_connection(("127.0.0.1", VITE_PORT), timeout=10)
        lines = [f"{self.command} {self.path} {self.request_version}"]
        lines += [f"{k}: {v}" for k, v in self.headers.items() if k.lower() != "host"]
        lines.append(f"Host: 127.0.0.1:{VITE_PORT}")
        upstream.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        self.close_connection = True
        sockets = [self.connection, upstream]
        upstream.settimeout(None)
        self.connection.settimeout(None)
        try:
            while True:
                readable, _, _ = select.select(sockets, [], [], 300)
                if not readable:
                    return
                for sock in readable:
                    data = sock.recv(65536)
                    if not data:
                        return
                    (upstream if sock is self.connection else self.connection).sendall(data)
        finally:
            upstream.close()

    def _forward(self) -> None:
        if "upgrade" in (self.headers.get("Connection") or "").lower():
            self._tunnel()
            return
        payload = self._body()
        upstream = http.client.HTTPConnection("127.0.0.1", VITE_PORT, timeout=600)
        headers = {k: v for k, v in self.headers.items() if k.lower() not in ("host", "connection", "content-length")}
        headers["Host"] = f"127.0.0.1:{VITE_PORT}"
        if payload:
            headers["Content-Length"] = str(len(payload))
        upstream.request(self.command, self.path, body=payload or None, headers=headers)
        lab.relay_response(self, upstream.getresponse())
        upstream.close()

    def _handle(self) -> None:
        try:
            if self.path.split("?", 1)[0].startswith("/chatbot/"):
                self._chatbot()
            else:
                self._forward()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_HEAD = _handle


def graphql(query: str, variables: dict | None = None) -> dict:
    """*query* on the lab's OpenCTI as its admin."""
    request = urllib.request.Request(
        f"{OPENCTI_URL}/graphql",
        data=json.dumps({"query": query, "variables": variables or {}}).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {OPENCTI_ADMIN_TOKEN}"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read())


def accept_ai_terms() -> None:
    """Turn the Filigran AI terms on, so the Ask Ariane button opens the panel instead of the terms dialog."""
    settings = graphql("query { settings { id filigran_chatbot_ai_cgu_status } }")["data"]["settings"]
    if settings.get("filigran_chatbot_ai_cgu_status") != "enabled":
        answer = graphql(
            "mutation($id: ID!, $input: [EditInput]!) { settingsEdit(id: $id) { fieldPatch(input: $input) { id } } }",
            {"id": settings["id"], "input": [{"key": "filigran_chatbot_ai_cgu_status", "value": ["enabled"]}]},
        )
        if answer.get("errors"):
            lab.log(f"ask-ariane: the AI terms were not turned on: {answer['errors']}")
            return
    lab.log("ask-ariane: Filigran AI terms enabled on the lab OpenCTI")


def check_versions(front: Path) -> None:
    """Log when the front's version is not the lab OpenCTI's: the front then queries fields the backend lacks."""
    wanted = json.loads((front / "package.json").read_text(encoding="utf-8")).get("version", "")
    try:
        running = graphql("query { about { version } }")["data"]["about"]["version"]
    except (OSError, KeyError, TypeError, ValueError) as exc:
        lab.log(f"ask-ariane: the lab OpenCTI's version could not be read: {exc}")
        return
    if wanted and running and wanted != running:
        lab.log(
            f"ask-ariane: WARNING the front is {wanted}, the lab OpenCTI is {running}: the pages fail on the fields "
            f"between them; materialize the tag {running} (and name a newer @filigran/chatbot in {PINS_FILE})"
        )


def requested_pins(checkout: Path) -> dict[str, str]:
    """The front pins to override: ``LAB_OPENCTI_PINS`` (``name@version`` pairs), else ``opencti-pins.json``."""
    text = os.environ.get(PINS_ENV, "").strip()
    if text:
        pins = {}
        for pair in filter(None, (part.strip() for part in text.split(","))):
            name, at, version = pair.rpartition("@")
            if not at or not name:
                lab.log(f"ask-ariane: {PINS_ENV} entry {pair!r} ignored: write name@version")
                continue
            pins[name] = version
        return pins
    path = checkout / PINS_FILE
    if not path.is_file():
        return {}
    try:
        pins = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise SystemExit(f"ask-ariane: {path} is not JSON: {exc}") from exc
    if not isinstance(pins, dict) or not all(isinstance(v, str) for v in pins.values()):
        raise SystemExit(f"ask-ariane: {path} must be an object of package name to version")
    return pins


def override_pins(front: Path, pins: dict[str, str]) -> list[str]:
    """Rewrite the front's package.json pins; answers the changes as ``name old -> new``.

    Only a package the front already depends on is rewritten (a typo is logged, never added), and only to an
    exact version, so the lab renders the release the PR under review pins and nothing else moves.
    """
    manifest = front / "package.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    changes = []
    for name, version in pins.items():
        if not VERSION.match(version):
            lab.log(f"ask-ariane: pin {name}@{version} ignored: an exact version is expected (3.25.0)")
            continue
        section = next((s for s in ("dependencies", "devDependencies") if name in data.get(s, {})), None)
        if section is None:
            lab.log(f"ask-ariane: pin {name}@{version} ignored: the front does not depend on {name}")
            continue
        old = data[section][name]
        if old != version:
            data[section][name] = version
            changes.append(f"{name} {old} -> {version}")
    if changes:
        manifest.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return changes


def host_paths() -> list[str]:
    """The paths under opencti-front to overlay: ``LAB_OPENCTI_HOST_PATHS``, else ``HOST_PATHS``; never outside the front."""
    text = os.environ.get(HOST_PATHS_ENV, "").strip()
    paths = [part.strip().strip("/") for part in text.split(",")] if text else list(HOST_PATHS)
    for path in filter(None, paths):
        if Path(path).is_absolute() or ".." in Path(path).parts:
            raise SystemExit(f"ask-ariane: {HOST_PATHS_ENV} entry {path!r} must be a path under opencti-front")
    return [path for path in paths if path]


def host_origin(host: Path) -> str:
    """The host ref the log names: repository@commit (ref) from the checkout record, else the directory."""
    # The sandbox's checkout record: ".xtm-checkout", ".xtm-checkout.json" on a checkout written before the rename.
    record = next((host / n for n in (".xtm-checkout", ".xtm-checkout.json") if (host / n).is_file()), host / ".xtm-checkout")
    if not record.is_file():
        return f"{host.name}/ (no checkout record)"
    data = json.loads(record.read_text(encoding="utf-8"))
    ref = f" ({data['ref']})" if data.get("ref") else ""
    return f"{data.get('repository', '?')}@{str(data.get('commit', '?'))[:12]}{ref}"


def _resolves(base: Path, target: str) -> bool:
    candidate = (base / target).resolve()
    if candidate.is_file():
        return True
    if any(candidate.with_name(candidate.name + suffix).is_file() for suffix in RESOLVE_SUFFIXES):
        return True
    return any((candidate / f"index{suffix}").is_file() for suffix in RESOLVE_SUFFIXES)


def overlay_host(root: Path, host: Path, paths: list[str]) -> tuple[list[str], list[str]]:
    """Copy the host paths over the tag front; answers (the files copied, the relative imports left unresolved).

    A path the host checkout lacks is skipped (the caller logs it); a directory is copied file by file, so a tag file
    the host ref deleted stays. The unresolved imports name the overlaid file and the import, so a host change that
    needs more files than the overlay names is seen in the start log rather than as a blank page.
    """
    front, host_front = root / FRONT, host / FRONT
    copied: list[str] = []
    for path in paths:
        source = host_front / path
        if source.is_dir():
            files = sorted(p for p in source.rglob("*") if p.is_file())
        else:
            files = [source] if source.is_file() else []
        for file in files:
            relative = file.relative_to(host_front)
            target = front / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(file.read_bytes())
            copied.append(relative.as_posix())
    unresolved = []
    for relative in copied:
        if not relative.endswith(SOURCE_SUFFIXES):
            continue
        target = front / relative
        for match in RELATIVE_IMPORT.finditer(target.read_text(encoding="utf-8", errors="replace")):
            if not _resolves(target.parent, match.group(1)):
                unresolved.append(f"{relative} imports {match.group(1)}")
    return copied, unresolved


def apply_host_overlay(root: Path) -> str | None:
    """Overlay the host checkout when it exists (module docstring); answers the host origin the ``lab up`` line names."""
    host = lab.CHECKOUT / os.environ.get(HOST_CHECKOUT_ENV, "opencti-host")
    if not (host / FRONT).is_dir():
        return None
    paths = host_paths()
    origin = host_origin(host)
    copied, unresolved = overlay_host(root, host, paths)
    missing = [path for path in paths if not (host / FRONT / path).exists()]
    if missing:
        lab.log(f"ask-ariane: host paths absent from {origin}, not overlaid: {', '.join(missing)}")
    version = json.loads((root / FRONT / "package.json").read_text(encoding="utf-8")).get("version", "?")
    lab.log(f"ask-ariane: host files from {origin} overlaid on the {version} front: {len(copied)} file(s) under "
            f"{', '.join(path for path in paths if path not in missing)}")
    for line in unresolved:
        lab.log(f"ask-ariane: WARNING {line}, which the tag front lacks: add its path to {HOST_PATHS_ENV}")
    return origin


def opencti_checkout() -> Path:
    where = lab.CHECKOUT / os.environ.get("LAB_OPENCTI_CHECKOUT", "opencti")
    front = where / "opencti-platform" / "opencti-front"
    if not (where / "yarn.lock").is_file() or not (front / "package.json").is_file():
        raise SystemExit(
            f"ask-ariane: no OpenCTI checkout at {where}: materialize OpenCTI-Platform/opencti at the ref to review "
            f"with sub_paths {list(OPENCTI_SUB_PATHS)} under this checkout's {where.name}/ directory, then start again"
        )
    return where


def main() -> None:
    root = opencti_checkout()
    front = root / FRONT
    host = apply_host_overlay(root)
    lab.ROOT.mkdir(parents=True, exist_ok=True)
    lab.CACHE.mkdir(exist_ok=True)
    node, yarn = lab.CACHE / "node" / "package" / "bin" / "node", lab.CACHE / "yarn" / "package" / "bin" / "yarn.js"
    for spec, target in ((lab.NODE, "node"), (lab.YARN, "yarn")):
        with tarfile.open(fileobj=io.BytesIO(lab.fetch(spec)), mode="r:gz") as archive:
            archive.extractall(lab.CACHE / target, filter="data")
    node.chmod(0o755)
    env = {"YARN_ENABLE_TELEMETRY": "0", "YARN_ENABLE_PROGRESS_BARS": "0", "YARN_ENABLE_GLOBAL_CACHE": "false",
           "YARN_CACHE_FOLDER": str(lab.CACHE / "yarn-cache"), "PATH": f"{node.parent}:{os.environ.get('PATH', '')}"}
    overridden = override_pins(front, requested_pins(lab.CHECKOUT))
    if overridden:
        # The lock no longer matches the manifest: yarn resolves the new pins (and their dependencies) itself.
        env["YARN_ENABLE_IMMUTABLE_INSTALLS"] = "false"
        lab.log(f"ask-ariane: front pins overridden: {', '.join(overridden)}")
    lab.run("yarn", [str(node), str(yarn), "workspaces", "focus", "opencti-front"], cwd=root, env={**os.environ, **env})
    lab.run("relay", [str(node), str(yarn), "relay"], cwd=front, env={**os.environ, **env})
    chatbot = json.loads((front / "package.json").read_text(encoding="utf-8"))["dependencies"].get("@filigran/chatbot")
    lab.spawn(
        "opencti-front",
        [str(node), "node_modules/vite/bin/vite.js", "--host", "127.0.0.1", "--port", str(VITE_PORT), "--strictPort"],
        cwd=front,
        env={**env, "BACK_END_URL": OPENCTI_URL, "FRONT_END_PORT": str(VITE_PORT)},
    )
    lab.wait_http(f"http://127.0.0.1:{VITE_PORT}/", 300, "opencti-front")
    lab.wait_http(OPENCTI_HEALTH, 900, "opencti")
    accept_ai_terms()
    check_versions(front)
    server = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), Front)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://localhost:{PORT}"
    for conversation in CONVERSATIONS:
        lab.log(f"seeded Ask Ariane conversation: {base}/dashboard (Ask Ariane, history: {conversation['title']})")
    hosted = f", host files from {host}" if host else ""
    lab.log(f"lab up: {base}/dashboard, OpenCTI front at @filigran/chatbot {chatbot}{hosted}, sign in as lab@filigran.io "
            "(lab-only-password); theme in Profile; a message holding 'slow' holds the working state, 'error' ends on an error, "
            f"'{TOOLS_KEYWORD}' calls a tool (Reasoning details), '{FILES_KEYWORD}' answers with file, deck, report and video "
            f"cards, '{QUOTA_KEYWORD}' moves the quota read to "
            f"{QUOTA_NEAR_CAP[0]} of {QUOTA_NEAR_CAP[1]} agentic actions today (warning tone)")
    while True:
        for child in lab.CHILDREN:
            if child.poll() is not None:
                raise SystemExit(f"ask-ariane: {child.args[0]} exited with {child.returncode}")
        time.sleep(2)


if __name__ == "__main__":
    try:
        main()
    finally:
        for child in lab.CHILDREN:
            if child.poll() is None:
                child.terminate()
