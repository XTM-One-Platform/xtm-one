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

"""The structured ask of a decision waiting for a person (#9437, #842).

One model and one readability guard for every human gate
(``dev-docs/human-gates.md``): a work item waiting for a person
(``work_items.ask``, re-exported by :mod:`app.services.work_item_ask`) and a run
paused by ``request_human_input`` (:func:`normalize_run_ask`).

An item with ``needs_human`` says in ``blocked_reason`` what it waits for, in
free text; the views had to show that text, or the whole description, and the
person had to find the question in it. ``ask`` carries the decision itself, so
a view renders it as a card with one button per option:

``{"question": str, "context"?: str, "options": [{"key", "label", "effect"?, "keeps_waiting"?, "confirm"?}],
"recommendation"?: option key, "deadline"?: ISO-8601, "links"?: [{"label", "url"}]}``

* ``question``: one sentence, required.
* ``context``: 2 to 4 short plain lines for the reader (what the matter is,
  what was found, what each answer leads to) (#776).
* ``options``: up to :data:`MAX_OPTIONS`; ``key`` short and unique (``A``,
  ``merge``), ``label`` the button text, ``effect`` what choosing it does,
  ``keeps_waiting`` true for an option that means "keep waiting": answering
  with it records the answer and leaves the item blocked, never dispatched
  (#1719); ``confirm`` true for a terminal option (cancel, close, discard):
  the card asks the person to confirm it before the answer is sent (#2150).
  None at all: the person answers in free text.
* ``recommendation``: the key of the option the agent recommends.
* ``deadline``: when the answer is needed, normalized to ISO-8601 UTC.
* ``links``: the sources the reader opens to decide (a Jira ticket, a pull
  request, a document), http(s) only, up to :data:`MAX_LINKS`; rendered as
  buttons under the question. When the agent gives none, the write path fills
  them from the item's own text (:func:`with_source_links`, #834): no model.

The question, the context and the option texts are read by a person: they
are refused when they carry what only an agent reads (a timestamp, an id, a
message id, a correlation or timer key, "card sent"), with a message saying
how to rewrite them (:func:`unreadable_reasons`, #776). Those details belong
in ``blocked_reason``, which the card shows under "Technical details".

:func:`normalize_ask` validates and trims a written ask (``ValueError`` with
the reason); ``{}`` or ``null`` clears it. Stored in ``work_items.ask``
(JSON, SQL NULL when none, migration ``a9437wiask01``). A legacy item has no
ask: the views show its ``blocked_reason`` as the question.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

MAX_QUESTION_CHARS = 500
MAX_CONTEXT_CHARS = 800
MAX_OPTIONS = 6
MAX_KEY_CHARS = 40
MAX_LABEL_CHARS = 120
MAX_EFFECT_CHARS = 400
MAX_LINKS = 6
MAX_URL_CHARS = 1000
#: A run's context, kept apart from its question (``request_human_input``).
MAX_RUN_CONTEXT_CHARS = 8000

ASK_FIELDS_MESSAGE = (
    "ask is {question, context?, options?: [{key, label, effect?}], recommendation?: an option key, "
    "deadline?: ISO-8601, links?: [{label, url}]}"
)

# What a person never reads on a decision card (#776): each pattern with what
# to write instead. Checked on the question, the context and every option text.
_UNREADABLE: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"\b\d{4}-\d{2}-\d{2}[T ]\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?\b|\b\d{1,2}:\d{2}Z\b"
        ),
        "a machine timestamp: put the time in ask.deadline, the card shows it in the reader's time zone",
    ),
    (
        re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.IGNORECASE),
        "a UUID: name the thing in words (its title), ids go in blocked_reason",
    ),
    (
        re.compile(r"\b(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{8,}\b", re.IGNORECASE),
        "a hexadecimal id or SHA: name the thing in words, ids go in blocked_reason",
    ),
    (
        re.compile(r"\b\d{10,}\b"),
        "a message or object id (a long run of digits): say what it is in words, ids go in blocked_reason",
    ),
    (
        re.compile(r"\b[a-z][a-z0-9_]*:[a-z0-9][a-z0-9_.]*-[a-z0-9][a-z0-9_.-]*", re.IGNORECASE),
        "a correlation or timer key: describe the matter in plain words, keys go in blocked_reason",
    ),
    (
        re.compile(r"\bcard sent\b|\bmsg\b", re.IGNORECASE),
        "delivery bookkeeping ('card sent', 'msg'): the reader needs the decision, not how it reached them",
    ),
)

READABLE_ASK_EXAMPLE = (
    'good: {"question": "Post Legal\'s comment asking the supplier for the 4 missing documents?", '
    '"context": "The contract review found 4 documents missing. Go posts the prepared comment; '
    'Edit lets you change it first; No closes the review as is.", "links": [{"label": "FIN-796", '
    '"url": "https://example.atlassian.net/browse/FIN-796"}]}; bad: {"question": "CARD SENT '
    '2026-11-20T11:43:58Z (msg 1795175038574): waiting for GO / EDIT / NO, timer legal:fin-796-reminder"}'
)


# An action code a person types back to consent to one irreversible action
# (``APPROVE M-1A2B3C4D``, ``hitl_consent.py``): the reader needs it verbatim.
_ACTION_CODE = re.compile(r"\b[A-Z]{1,3}-[0-9A-F]{8}\b")

# An address the reader needs verbatim to tell what the decision covers (#2254):
# an http(s) URL, an email address or a dotted hostname. A hexadecimal DNS label
# (``langfuse-141a8160.example.com``, a preview host) or a number in a link is
# part of the address, not an id, so the hex and long-digit patterns skip them.
_ADDRESS = re.compile(
    r"\bhttps?://\S+"
    r"|[\w.+-]+@[a-z0-9-]+(?:\.[a-z0-9-]+)+"
    r"|\b[a-z0-9-]+(?:\.[a-z0-9-]+)+\.[a-z]{2,}\b",
    re.IGNORECASE,
)

# The patterns of ``_UNREADABLE`` matched on the text without its addresses:
# the hexadecimal id and the long run of digits.
_ADDRESS_BLIND = frozenset({_UNREADABLE[2][0], _UNREADABLE[3][0]})


def unreadable_reasons(text: str) -> list[str]:
    """What in *text* a person should not have to read, one reason per pattern found.

    A hexadecimal id or a long run of digits inside a URL, an email address or a
    hostname is not reported (#2254); a bare one next to an address still is.
    """
    readable = _ACTION_CODE.sub("", text or "")
    without_addresses = _ADDRESS.sub(" ", readable)
    return [
        reason
        for pattern, reason in _UNREADABLE
        if pattern.search(without_addresses if pattern in _ADDRESS_BLIND else readable)
    ]


def _refuse_unreadable(field: str, text: str) -> None:
    reasons = unreadable_reasons(text)
    if reasons:
        raise ValueError(
            f"ask.{field} is read by a person and holds {'; '.join(reasons)}. Rewrite it as the decision in "
            f"the reader's terms; technical details go in blocked_reason ({READABLE_ASK_EXAMPLE})"
        )


#: The mark a text cut at its cap ends with, so the reader knows words are missing (#1598).
CUT_MARK = "…"


def fit(text: str, limit: int) -> str:
    """*text* within *limit* characters: cut on the last word boundary and marked, never mid-word (#1598).

    A text that fits is returned as is. A single word longer than the room
    left is cut at the room (a URL, an identifier), still marked.
    """
    if len(text) <= limit:
        return text
    room = max(limit - len(CUT_MARK), 0)
    head = text[:room]
    space = max(head.rfind(" "), head.rfind("\n"))
    if space > room // 2:
        head = head[:space]
    return head.rstrip(" \n,;:.") + CUT_MARK


def _text(value: object, limit: int) -> str:
    return fit(" ".join(str(value).split()), limit) if value is not None else ""


def _deadline(value: object) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"ask.deadline is not an ISO-8601 date or time: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat()


def _context(value: object) -> str:
    """The context lines, each trimmed, blank lines dropped, line breaks kept."""
    if value in (None, ""):
        return ""
    if isinstance(value, list):
        value = "\n".join(str(line) for line in value)
    lines = [" ".join(line.split()) for line in str(value).splitlines()]
    return fit("\n".join(line for line in lines if line), MAX_CONTEXT_CHARS)


# The card marks the recommended option itself (dev-docs/human-gates.md, J.1):
# an effect that opens with the word repeats it beside the pill.
_RECOMMENDED_LEAD = re.compile(r"^\W*recommended\b", re.IGNORECASE)
RECOMMENDED_EFFECT_MESSAGE = (
    "the card marks the recommended option itself: drop the leading 'Recommended' from ask.options[].effect"
)


def keeps_waiting(option: object) -> bool:
    """Whether choosing *option* leaves its item waiting, answer recorded, no dispatch (#1719)."""
    return isinstance(option, dict) and option.get("keeps_waiting") is True


def _options(value: object) -> list[dict[str, Any]]:
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise ValueError("ask.options must be a list of {key, label, effect?}")
    if len(value) > MAX_OPTIONS:
        raise ValueError(f"ask.options lists at most {MAX_OPTIONS} options ({len(value)} given)")
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in value:
        if not isinstance(raw, dict):
            raise ValueError("ask.options must be a list of {key, label, effect?}")
        key = _text(raw.get("key"), MAX_KEY_CHARS)
        label = _text(raw.get("label"), MAX_LABEL_CHARS) or key
        if not key:
            raise ValueError("every ask option needs a key")
        if key.casefold() in seen:
            raise ValueError(f"ask option key {key!r} is given twice")
        seen.add(key.casefold())
        option: dict[str, Any] = {"key": key, "label": label}
        effect = _text(raw.get("effect"), MAX_EFFECT_CHARS)
        if _RECOMMENDED_LEAD.match(effect):
            raise ValueError(RECOMMENDED_EFFECT_MESSAGE)
        if effect:
            option["effect"] = effect
        keeps = raw.get("keeps_waiting")
        if keeps not in (None, False, True):
            raise ValueError("ask.options[].keeps_waiting is true or false")
        if keeps:
            # Answering with it records the answer and leaves the item waiting (#1719).
            option["keeps_waiting"] = True
        confirm = raw.get("confirm")
        if confirm not in (None, False, True):
            raise ValueError("ask.options[].confirm is true or false")
        if confirm:
            # The card asks for a second click before sending it (#2150).
            option["confirm"] = True
        out.append(option)
    return out


_URL = re.compile(r"https?://[^\s<>()\[\]\"'`]+")
_JIRA_KEY = re.compile(r"/browse/([A-Z][A-Z0-9]+-\d+)")
_GITHUB = re.compile(r"^https://github\.com/([^/]+)/([^/]+)/(pull|issues)/(\d+)")


def link_kind(url: str) -> tuple[str, str] | None:
    """What a source URL opens, ``(kind, value)``; ``None`` for a URL the platform does not recognise.

    ``kind`` is ``pull_request`` or ``issue`` (value: the number), ``ticket``
    (value: the Jira key, ``""`` when the URL names none), ``page`` (Notion) or
    ``document`` (Concord). The decision cards word their buttons from it in the
    reader's language (#1177).
    """
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return None
    if host.endswith(".atlassian.net"):
        key = _JIRA_KEY.search(url)
        return "ticket", key.group(1) if key else ""
    if host == "github.com":
        match = _GITHUB.match(url)
        if match:
            return ("pull_request" if match.group(3) == "pull" else "issue"), match.group(4)
        return None
    if host in ("notion.so", "notion.site") or host.endswith((".notion.so", ".notion.site")):
        return "page", ""
    if host == "concordnow.com" or host.endswith(".concordnow.com"):
        return "document", ""
    return None


def _link_label(url: str) -> str | None:
    """The button text of a source URL the write path recognises; ``None`` for any other URL."""
    found = link_kind(url)
    if found is None:
        return None
    kind, value = found
    if kind == "ticket":
        return value or "Jira"
    if kind in ("pull_request", "issue"):
        match = _GITHUB.match(url)
        repo = match.group(2) if match else ""
        return f"{repo} {'PR' if kind == 'pull_request' else 'Issue'} #{value}"
    return "Notion page" if kind == "page" else "Concord document"


def is_derived_label(link: dict[str, Any]) -> bool:
    """*link* carries the label the platform derived from its URL, not one an agent wrote."""
    url = str(link.get("url") or "")
    return bool(url) and str(link.get("label") or "") == (_link_label(url) or "")


#: The link buttons a decision card shows next to "Open the decision" (#1177).
MAX_CARD_LINKS = 4

_LINK_NOTICE = {
    "pull_request": "card_link_pull_request",
    "issue": "card_link_issue",
    "page": "card_link_page",
    "document": "card_link_document",
}


def card_link_label(link: dict[str, Any], language: str | None = None) -> str:
    """The button text of one ask link on a decision card, in *language* (#1177).

    A label the agent wrote ("Open PR #20153") is kept as written; a label the
    platform derived from the URL (:func:`source_links`) is worded from the
    catalog: "Open PR #N", "Open issue #N", "Open TICKET-1", "Open the ticket",
    "Open the page", "Open the document".
    """
    from app.services.channel_notices import notice

    url = str(link.get("url") or "")
    label = str(link.get("label") or "").strip()
    found = link_kind(url) if (not label or is_derived_label(link)) else None
    if found is None:
        return label or url
    kind, value = found
    if kind == "ticket":
        return notice("card_link_ticket_key", language, key=value) if value else notice("card_link_ticket", language)
    if kind in ("pull_request", "issue"):
        return notice(_LINK_NOTICE[kind], language, number=value)
    return notice(_LINK_NOTICE[kind], language)


def reference_links(refs: object, payload: object = None) -> list[dict[str, str]]:
    """The ``{label, url}`` of the GitHub pull requests and issues a work item tracks (#1177).

    Read from its ``blocked_on_refs`` (``pull_request`` and ``issue`` kinds, and a
    ``deployment`` ref naming a pull request) and its ``payload.pull_requests``
    (the url each binding records), first seen first, at most :data:`MAX_LINKS`.
    What :func:`with_reference_links` puts in an ask that names no source.
    """
    urls: list[str] = []
    for ref in refs if isinstance(refs, list) else []:
        if not isinstance(ref, dict) or ref.get("kind", "pull_request") not in ("pull_request", "issue", "deployment"):
            continue
        repo, number = ref.get("repo"), ref.get("number")
        if not isinstance(repo, str) or repo.count("/") != 1 or isinstance(number, bool) or not isinstance(number, int):
            continue
        path = "issues" if ref.get("kind") == "issue" else "pull"
        urls.append(f"https://github.com/{repo}/{path}/{number}")
    prs = payload.get("pull_requests") if isinstance(payload, dict) else None
    for pr in prs if isinstance(prs, list) else []:
        if isinstance(pr, dict) and isinstance(pr.get("url"), str):
            urls.append(pr["url"])
    links: list[dict[str, str]] = []
    seen: set[str] = set()
    for url in urls:
        url = url.strip()[:MAX_URL_CHARS]
        key = url.lower().rstrip("/")
        if key in seen or not re.match(r"^https?://\S+$", url):
            continue
        seen.add(key)
        links.append({"label": _link_label(url) or url.split("/", 3)[2], "url": url})
        if len(links) >= MAX_LINKS:
            break
    return links


def with_reference_links(ask: dict[str, Any] | None, refs: object, payload: object = None) -> dict[str, Any] | None:
    """*ask* with ``links`` from :func:`reference_links` when it has none; *ask* unchanged otherwise."""
    if not isinstance(ask, dict) or ask.get("links"):
        return ask
    links = reference_links(refs, payload)
    return {**ask, "links": links} if links else ask


def card_links(ask: object, language: str | None = None, limit: int = MAX_CARD_LINKS) -> list[dict[str, str]]:
    """The ``{label, url}`` buttons a decision card shows for the objects of its ask (#1177).

    One per ``ask.links`` entry, http(s) only, at most *limit*, in the ask's
    order; each label from :func:`card_link_label`. Every surface (the Teams
    bot and link cards, the decided card, Google Chat, Slack, the email) reads
    this one list, so a person sees the same buttons wherever the card lands.
    """
    if not isinstance(ask, dict):
        return []
    out: list[dict[str, str]] = []
    for link in ask.get("links") or []:
        if not isinstance(link, dict):
            continue
        url = str(link.get("url") or "")
        if not url.startswith(("https://", "http://")):
            continue
        out.append({"label": card_link_label(link, language), "url": url})
        if len(out) >= limit:
            break
    return out


def _strings(value: object) -> Iterator[str]:
    """Every string of *value*, nested dict values and list items included, decoded (no JSON escapes)."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list | tuple):
        for item in value:
            yield from _strings(item)
    elif value is not None:
        yield str(value)


def source_links(*texts: object) -> list[dict[str, str]]:
    """The Jira, GitHub, Notion and Concord links found in *texts*, first seen first (#834).

    Deterministic, no model: what the write path and the backfill put in
    ``ask.links`` when the agent wrote none. A payload is read string by string,
    so a JSON escape (``\\n``) never runs into a URL.
    """
    links: list[dict[str, str]] = []
    seen: set[str] = set()
    for text in texts:
        for raw in _strings(text):
            for match in _URL.finditer(raw):
                url = match.group(0).rstrip(".,;:!?")[:MAX_URL_CHARS]
                label = _link_label(url)
                if label is None or url in seen:
                    continue
                seen.add(url)
                links.append({"label": label, "url": url})
                if len(links) >= MAX_LINKS:
                    return links
    return links


def with_source_links(ask: dict[str, Any] | None, *texts: object) -> dict[str, Any] | None:
    """*ask* with ``links`` filled from *texts* when it has none; *ask* unchanged otherwise."""
    if not isinstance(ask, dict) or ask.get("links"):
        return ask
    links = source_links(*texts)
    return {**ask, "links": links} if links else ask


def _links(value: object) -> list[dict[str, str]]:
    if value in (None, "", []):
        return []
    if not isinstance(value, list):
        raise ValueError("ask.links must be a list of {label, url}")
    if len(value) > MAX_LINKS:
        raise ValueError(f"ask.links lists at most {MAX_LINKS} links ({len(value)} given)")
    out: list[dict[str, str]] = []
    for raw in value:
        if not isinstance(raw, dict):
            raise ValueError("ask.links must be a list of {label, url}")
        url = str(raw.get("url") or "").strip()[:MAX_URL_CHARS]
        if not re.match(r"^https?://\S+$", url):
            raise ValueError(f"ask.links url {url!r} is not an http(s) address")
        label = _text(raw.get("label"), MAX_LABEL_CHARS) or _link_label(url) or url.split("/", 3)[2]
        out.append({"label": label, "url": url})
    return out


#: An OpenCTI draft page (#1453): ``https://host[/base]/dashboard/data/import/draft/<uuid>``.
_OPENCTI_DRAFT_PAGE = re.compile(r"^(.*?)/dashboard/data/import/draft/[0-9a-fA-F-]{36}(?:[/?#].*)?$")
#: The OpenCTI object pages an object created inside a draft does not have until the draft is
#: validated: the analyses (Grouping, Report, ...) and the id resolver.
_OPENCTI_OBJECT_PAGE = re.compile(r"^(.*?)/dashboard/(?:analyses/[a-z_]+|id)/[^/?#]+")


def drop_links_shadowed_by_drafts(links: list[dict[str, str]]) -> list[dict[str, str]]:
    """*links* without the OpenCTI object pages a draft link of the same platform shadows (#1453).

    An object an agent creates inside an OpenCTI draft (a Grouping, a Report)
    has no live page until a person validates the draft: its page answers
    "not found". When an ask links a draft, it links that draft alone for the
    platform it lives on; an object page of the same platform (same origin and
    base path) next to it is dropped. Other platforms' links are kept. Pure.
    """
    bases = {m.group(1).rstrip("/").lower() for link in links if (m := _OPENCTI_DRAFT_PAGE.match(link["url"]))}
    if not bases:
        return links
    kept: list[dict[str, str]] = []
    for link in links:
        found = _OPENCTI_OBJECT_PAGE.match(link["url"])
        if found and found.group(1).rstrip("/").lower() in bases:
            continue
        kept.append(link)
    return kept


def normalize_ask(value: object) -> dict[str, Any] | None:
    """The ask as stored, ``None`` when cleared (``{}``, ``null``, blank text).

    Accepts the object or its JSON text; raises ``ValueError`` naming what is
    wrong. A recommendation that names no option is refused.
    """
    if value is None:
        return None
    if isinstance(value, str):
        if not value.strip():
            return None
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"ask is not valid JSON ({exc})") from exc
    if not isinstance(value, dict):
        raise ValueError(ASK_FIELDS_MESSAGE)
    if not value:
        return None
    unknown = sorted(set(value) - {"question", "context", "options", "recommendation", "deadline", "links"})
    if unknown:
        raise ValueError(f"ask has unknown field(s) {', '.join(unknown)}: {ASK_FIELDS_MESSAGE}")
    question = _text(value.get("question"), MAX_QUESTION_CHARS)
    if not question:
        raise ValueError("ask.question is required: the decision in one sentence")
    _refuse_unreadable("question", question)
    ask: dict[str, Any] = {"question": question}
    context = _context(value.get("context"))
    if context:
        _refuse_unreadable("context", context)
        ask["context"] = context
    options = _options(value.get("options"))
    for option in options:
        _refuse_unreadable("options", f"{option['label']} {option.get('effect', '')}")
    if options:
        ask["options"] = options
    recommendation = _text(value.get("recommendation"), MAX_KEY_CHARS)
    if recommendation:
        match = next((o["key"] for o in options if o["key"].casefold() == recommendation.casefold()), None)
        if match is None:
            raise ValueError(f"ask.recommendation {recommendation!r} is not the key of one of its options")
        ask["recommendation"] = match
    deadline = _deadline(value.get("deadline"))
    if deadline:
        ask["deadline"] = deadline
    links = drop_links_shadowed_by_drafts(_links(value.get("links")))
    if links:
        ask["links"] = links
    return ask


def normalize_run_ask(
    question: object,
    context: object = "",
    options: object = None,
    recommendation: object = None,
    deadline: object = None,
    links: object = None,
) -> dict[str, Any]:
    """The ask of a run paused by ``request_human_input`` (#842, S1).

    The same model and guard as a work item's ask: the question and the
    options are read by a person on the card and refused when unreadable, so
    the agent rewrites them in the same turn. The context is kept apart and
    left unguarded: a run's context carries what the agent found. A question
    alone, or with a context, is the free-text decision it always was.
    """
    value: dict[str, Any] = {"question": question}
    for name, given in (
        ("options", options),
        ("recommendation", recommendation),
        ("deadline", deadline),
        ("links", links),
    ):
        if given not in (None, "", []):
            value[name] = given
    ask = normalize_ask(value)
    if ask is None:
        raise ValueError("question is required: the decision in one sentence")
    # The question and the context kept as the agent wrote them, as the run always
    # stored them (line breaks and length included): the guard read them, it does not reword.
    if isinstance(context, list):
        context = "\n".join(str(line) for line in context)
    run_ask: dict[str, Any] = {"question": str(question).strip()[:MAX_RUN_CONTEXT_CHARS]}
    run_context = str(context or "").strip()[:MAX_RUN_CONTEXT_CHARS]
    if run_context:
        run_ask["context"] = run_context
    run_ask.update({k: v for k, v in ask.items() if k != "question"})
    return run_ask


def flow_ask(
    question: str, options: object = None, recommendation: object = None, deadline: object = None
) -> dict[str, Any] | None:
    """The ask of a flow Ask-user node (#842, S6), ``None`` for a free-text node.

    A node with no options, recommendation or deadline parks as it always did
    (no ask stored, no guard). Options may come as the list or its JSON text;
    the model and the guard are the work item's, so a bad option list fails
    the node with the reason instead of parking an unanswerable card.
    """
    if isinstance(options, str):
        text = options.strip()
        try:
            options = json.loads(text) if text else None
        except json.JSONDecodeError as exc:
            raise ValueError(f"ask.options is not valid JSON ({exc})") from exc
    given = {
        name: value
        for name, value in (("options", options), ("recommendation", recommendation), ("deadline", deadline))
        if value not in (None, "", [])
    }
    if not given:
        return None
    ask = normalize_ask({"question": question, **given})
    if ask is None:
        return None
    # The question kept as the node rendered it: the guard read it, it does not reword.
    ask["question"] = str(question).strip()[:MAX_RUN_CONTEXT_CHARS]
    return ask


def flow_option(ask: dict[str, Any] | None, response: str) -> str:
    """The option key an answer to a flow park chose, ``""`` for free text.

    The answer is the key or the label of an option (any case), or the key
    followed by ``: <note>`` as the router writes an option with a note.
    """
    options = (ask or {}).get("options")
    text = (response or "").strip()
    head = text.split(":", 1)[0].strip().casefold()
    for option in options if isinstance(options, list) else []:
        if not isinstance(option, dict):
            continue
        key = str(option.get("key") or "").strip()
        label = str(option.get("label") or key).strip()
        if key and (text.casefold() in (key.casefold(), label.casefold()) or head == key.casefold()):
            return key
    return ""


def run_question_text(ask: dict[str, Any]) -> str:
    """The one string legacy readers of ``human_input_question`` show.

    The question, then the context and the options as plain lines, so a
    client that reads the old column alone still has the whole decision.
    """
    parts = [str(ask.get("question") or "")]
    if ask.get("context"):
        parts.append(f"Context: {ask['context']}")
    options = ask.get("options") or []
    if options:
        recommended = ask.get("recommendation")
        lines = []
        for option in options:
            mark = " (recommended)" if option.get("key") == recommended else ""
            effect = f": {option['effect']}" if option.get("effect") else ""
            lines.append(f"- {option['key']}. {option['label']}{mark}{effect}")
        parts.append("Options:\n" + "\n".join(lines))
    return "\n\n".join(parts)


def run_answer_text(ask: dict[str, Any] | None, response: str) -> str:
    """The tool result a resumed run reads: the chosen option spelled out, or the free text.

    A reply that is exactly an option key (any case) or its label is that
    option; anything else is the person's own words.
    """
    text = (response or "").strip().casefold()
    options = (ask or {}).get("options")
    for option in options if isinstance(options, list) else []:
        if not isinstance(option, dict) or not str(option.get("key") or "").strip():
            continue
        key = str(option["key"]).strip()
        label = str(option.get("label") or key).strip()
        if text and text in (key.casefold(), label.casefold()):
            return f"Human chose option {key} ({label})"
    return f"Human response: {response}"


def person_ask_missing(ask: object) -> list[str]:
    """What a decision card for a person still lacks in *ask* (#776).

    An agent that parks an item for a person (``needs_human``) writes the card
    the person reads: a question, the context, at least two options and the
    one it recommends (#834). Empty when the ask is complete.
    """
    if not isinstance(ask, dict) or not str(ask.get("question") or "").strip():
        return ["ask.question", "ask.context", "ask.options (at least 2)", "ask.recommendation"]
    missing: list[str] = []
    if not str(ask.get("context") or "").strip():
        missing.append("ask.context")
    if len(ask.get("options") or []) < 2:
        missing.append("ask.options (at least 2)")
    if not str(ask.get("recommendation") or "").strip():
        missing.append("ask.recommendation")
    return missing


def refuse_incomplete_person_ask(ask: object) -> None:
    """``ValueError`` naming what an agent's needs_human write must add to its ask."""
    from app.services.work_item_blocked_refs import ASK_REQUIRED_MESSAGE

    missing = person_ask_missing(ask)
    if missing:
        # One message for the rule, whichever check meets it first (#862).
        raise ValueError(
            f"{ASK_REQUIRED_MESSAGE} (missing: {', '.join(missing)}); nothing was written ({READABLE_ASK_EXAMPLE})"
        )


def ask_lines(ask: object) -> list[str]:
    """What a tool result says of an item's ask (nothing when it has none)."""
    if not isinstance(ask, dict) or not ask.get("question"):
        return []
    lines = [f"  - ask: {ask['question']}"]
    if ask.get("context"):
        lines.append(f"    context: {' / '.join(str(ask['context']).splitlines())}")
    recommended = ask.get("recommendation")
    for option in ask.get("options") or []:
        mark = " (recommended)" if option.get("key") == recommended else ""
        effect = f": {option['effect']}" if option.get("effect") else ""
        stays = " (keeps it waiting)" if keeps_waiting(option) else ""
        lines.append(f"    - [{option.get('key')}] {option.get('label')}{mark}{stays}{effect}")
    if ask.get("deadline"):
        lines.append(f"    - answer needed by {ask['deadline']}")
    for link in ask.get("links") or []:
        lines.append(f"    - source: {link.get('label')} {link.get('url')}")
    return lines
