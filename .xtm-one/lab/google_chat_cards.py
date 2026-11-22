"""The Google Chat decision card render lab: the cardsV2 of the checkout drawn in a browser (#842, S11).

``[service.lab-google-chat-cards]`` in ``.xtm-one/checks.toml`` runs this script on the ``python-dev`` image, whose
interpreter holds the backend's locked dependencies. No Google Workspace, no database and no network: the script
imports the checkout's own ``app.services.google_chat_decision_cards`` and calls ``render_google_chat_decision`` and
``link_message`` on fixed decisions, one per state the UX review covers, then draws the cardsV2 JSON as HTML with
Google Chat's message layout, so a card change is reviewed rendered, not read as code. What it draws is the payload
the platform posts; the drawing is an approximation of Google Chat's client (fonts, spacing, colours of the light and
dark themes), never Chat itself, and the JSON beside each card is the exact payload.

Pages (the preview port, ``PORT``):

* ``/`` lists every state with its links;
* ``/card?state=<state>&lang=<en|es|fr|ja>&theme=<light|dark>&width=<desktop|narrow>`` one state;
* ``/all?lang=..&theme=..&width=..`` every state on one page;
* ``/card.json?state=..&lang=..`` the message as posted (paste its card in Google's Card Builder);
* ``/lab/ready`` the readiness probe.
"""

from __future__ import annotations

import html
import http.server
import json
import os
import re
import sys
import threading
import urllib.parse
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

PORT = 4310
CHECKOUT = Path(__file__).resolve().parents[2]
LANGUAGES = ("en", "es", "fr", "ja")
THEMES = ("light", "dark")
WIDTHS = {"desktop": 1000, "narrow": 360}
FRONTEND_URL = "http://localhost:3100"
READY: dict[str, bool] = {}

ROW_ID = uuid.UUID("6a1d3b2f-4c5e-4f60-9bac-1d2e3f4a5b6c")
SOURCE_ID = uuid.UUID("1c9f7a9d-3e6d-4d66-8b62-9b4d2e3f4a51")
RECIPIENT_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
DEADLINE = "2026-11-23T09:00:00+00:00"
ANSWERED_AT = datetime(2026, 11, 22, 8, 0, tzinfo=UTC)

#: The ask of a run HITL, a flow Ask-user and a work item: every field the contract carries.
ASK = {
    "question": "Ship the fix to production tonight?",
    "context": "CI is green on the release branch.\nThe change touches the backend only.",
    "options": [
        {"key": "A", "label": "Ship tonight", "effect": "deploys at 22:00 UTC, on call watches the rollout"},
        {"key": "B", "label": "Wait for Monday", "effect": "ships with the weekly release"},
        {"key": "C", "label": "Cancel the release", "effect": "the branch stays unmerged"},
    ],
    "recommendation": "A",
    "deadline": DEADLINE,
    "links": [{"label": "Pull request", "url": "https://github.com/example/repo/pull/1"}],
}
APPROVAL_ASK = {
    "question": "Approve the tool calls of this run?",
    "options": [
        {"key": "approve", "label": "Approve", "effect": "the calls run once"},
        {"key": "approve_always", "label": "Approve always", "effect": "these tools run without asking again"},
        {"key": "reject", "label": "Reject", "effect": "the run goes on without them"},
    ],
    "recommendation": "approve",
    "deadline": DEADLINE,
}
PROPOSALS = ({"tool_name": "send_email"}, {"tool_name": "create_github_issue"})
SOURCES = {
    "hitl": ("run", "hitl", ASK, ()),
    "tool-approval": ("run", "tool_approval", APPROVAL_ASK, PROPOSALS),
    "flow": ("flow_run", "flow_ask", ASK, ()),
    "work-item": ("work_item", "work_item", ASK, ()),
}
#: The refusals a click can meet, each read only by the clicker (a private message).
REFUSALS = (
    "card_decision_refused",
    "card_decision_already_answered",
    "card_changed",
    "card_not_linked_decision",
    "card_choose_approval",
    "card_answer_empty",
    "card_decision_failed",
)


def _states() -> list[str]:
    names: list[str] = []
    for source in SOURCES:
        names += [f"open-{source}", f"open-{source}-bare"]
    names += ["open-tool-approval-many", "decided-option", "decided-text", "decided-tools", "expired", "cancelled",
              "refusal", "user-links"]
    return names


STATES = _states()


def _backend() -> None:
    """The checkout's backend on the path, configured for a render with no store."""
    os.environ.setdefault("FRONTEND_URL", FRONTEND_URL)
    os.environ.setdefault("SECRET_KEY", "lab-only-secret-key-never-used-outside-the-lab")
    backend = CHECKOUT / "backend"
    if str(backend) not in sys.path:
        sys.path.insert(0, str(backend))


def _row(source_type: str, kind: str, ask: dict[str, Any], **fields: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "id": ROW_ID,
        "source_type": source_type,
        "source_id": SOURCE_ID,
        "kind": kind,
        "ask": ask,
        "recipient": f"user:{RECIPIENT_ID}",
        "status": "open",
        "answer": None,
        "answered_by": None,
        "answered_at": None,
        "deadline": None,
    }
    values.update(fields)
    row = SimpleNamespace(**values)
    row.public_id = f"{source_type}:{SOURCE_ID}"
    return row


def _view(row: SimpleNamespace, proposals: tuple[dict[str, Any], ...] = (), **fields: Any) -> Any:
    from app.services.decision_cards import DecisionView

    return DecisionView(
        decision=row,  # type: ignore[arg-type]
        may_answer=None,
        recipient_name="Ada Lovelace",
        agent_name="Release Manager",
        page_path=f"/tasks/work-items/{SOURCE_ID}",
        proposals=proposals,
        **fields,
    )


def build(state: str, language: str) -> dict[str, Any]:
    """``{"kind": "message" | "private" | "text", "payload": ..., "under": message or None}`` of *state*."""
    _backend()
    from app.services import google_chat_decision_cards as cards
    from app.services.channel_notices import notice

    if state.startswith("open-"):
        name = state[len("open-"):]
        bare = name.endswith("-bare")
        many = name == "tool-approval-many"
        source = "tool-approval" if many else name.removesuffix("-bare")
        source_type, kind, ask, proposals = SOURCES[source]
        if bare:
            ask = {k: v for k, v in ask.items() if k not in ("recommendation", "deadline", "links")}
        if many:
            proposals = tuple({"tool_name": f"tool_{index}"} for index in range(11))
        view = _view(_row(source_type, kind, ask), proposals)
        return {"kind": "message", "payload": cards.render_google_chat_decision(view, language), "under": None}
    if state.startswith("decided-"):
        if state == "decided-tools":
            answer = {"decisions": {"c1": "approve", "c2": "reject"}}
            row = _row("run", "tool_approval", APPROVAL_ASK, status="answered", answer=answer,
                       answered_by=uuid.uuid4(), answered_at=ANSWERED_AT)
            view = _view(row, PROPOSALS, answered_by_name="Ada Lovelace")
        else:
            answer = {"option": "B"}
            if state == "decided-text":
                answer["text"] = "Monday is safer: the on call engineer is away tonight."
            row = _row("work_item", "work_item", ASK, status="answered", answer=answer,
                       answered_by=uuid.uuid4(), answered_at=ANSWERED_AT)
            view = _view(row, answered_by_name="Ada Lovelace")
        return {"kind": "message", "payload": cards.render_google_chat_decision(view, language), "under": None}
    if state in ("expired", "cancelled"):
        view = _view(_row("flow_run", "flow_ask", ASK, status=state))
        return {"kind": "message", "payload": cards.render_google_chat_decision(view, language), "under": None}
    under = cards.render_google_chat_decision(_view(_row("work_item", "work_item", ASK)), language)
    if state == "refusal":
        texts = [notice(key, language) for key in REFUSALS]
        return {"kind": "private", "payload": {"texts": texts}, "under": under}
    if state == "user-links":
        # A message posted as a person (user auth): Chat draws no card for it, it carries the S9b links.
        text = cards.link_message(_row("work_item", "work_item", ASK), RECIPIENT_ID, language)  # type: ignore[arg-type]
        return {"kind": "text", "payload": {"text": text}, "under": None}
    raise KeyError(state)


# --- Drawing ---------------------------------------------------------------------------------------------------

_ALLOWED_TAGS = re.compile(r'<(/?)(b|i|u|code|br|font)(?: color="(#[0-9a-fA-F]{6})")?>')
_URL = re.compile(r"(https?://[^\s<]+)")
#: Chat's link markup ``<url|label>`` in a text message, as ``html.escape`` leaves it.
_CHAT_LINK = re.compile(r"&lt;(https?://[^\s|]+?)\|(.+?)&gt;")


def chat_html(text: str) -> str:
    """The HTML subset a Chat text paragraph reads (``<b>``, ``<i>``, ``<u>``, ``<code>``, ``<br>``,
    ``<font color>``), every other tag shown as text; entities the renderer escaped stay escaped."""
    # A bare ``&`` is escaped; an entity the renderer already wrote (``&lt;``) is kept as Chat reads it.
    text = re.sub(r"&(?![a-zA-Z]+;|#\d+;)", "&amp;", text)
    out: list[str] = []
    last = 0
    for match in _ALLOWED_TAGS.finditer(text):
        out.append(text[last : match.start()].replace("<", "&lt;").replace(">", "&gt;"))
        closing, tag, colour = match.groups()
        if tag == "br":
            out.append("<br>")
        elif tag == "font":
            out.append("</span>" if closing else f'<span style="color:{colour or "inherit"}">')
        else:
            out.append(f"<{closing}{tag}>")
        last = match.end()
    out.append(text[last:].replace("<", "&lt;").replace(">", "&gt;"))
    return "".join(out)


def _colour(colour: dict[str, float] | None) -> str | None:
    if not colour:
        return None
    red, green, blue = (round(float(colour.get(c, 0)) * 255) for c in ("red", "green", "blue"))
    return f"#{red:02x}{green:02x}{blue:02x}"


def button(item: dict[str, Any]) -> str:
    label = html.escape(str(item.get("text") or ""))
    click = item.get("onClick") or {}
    background = _colour(item.get("color"))
    style = f' style="background:{background};border-color:{background};color:#fff"' if background else ""
    kind = "filled" if background else "outlined"
    if "openLink" in click:
        url = html.escape(str(click["openLink"].get("url") or ""))
        return f'<a class="btn {kind}"{style} href="{url}" target="_blank">{label}</a>'
    function = html.escape(str((click.get("action") or {}).get("function") or ""))
    return f'<button class="btn {kind}"{style} type="button" title="{function}">{label}</button>'


def widget(item: dict[str, Any]) -> str:
    if "textParagraph" in item:
        return f'<div class="w-text">{chat_html(str(item["textParagraph"].get("text") or ""))}</div>'
    if "decoratedText" in item:
        decorated = item["decoratedText"]
        top = html.escape(str(decorated.get("topLabel") or ""))
        return (f'<div class="w-deco"><span class="top">{top}</span>'
                f'<span>{chat_html(str(decorated.get("text") or ""))}</span></div>')
    if "buttonList" in item:
        buttons = (item["buttonList"].get("buttons")) or []
        return '<div class="w-buttons">' + "".join(button(b) for b in buttons) + "</div>"
    if "textInput" in item:
        field = item["textInput"]
        rows = 3 if field.get("type") == "MULTIPLE_LINE" else 1
        return (f'<label class="w-input"><span class="label">{html.escape(str(field.get("label") or ""))}</span>'
                f'<textarea rows="{rows}" placeholder="{html.escape(str(field.get("hintText") or ""))}"></textarea>'
                "</label>")
    return f'<pre class="w-unknown">{html.escape(json.dumps(item))}</pre>'


def card(payload: dict[str, Any]) -> str:
    inner = (payload.get("cardsV2") or [{}])[0].get("card") or {}
    header = inner.get("header") or {}
    sections = "".join(
        '<div class="section">' + "".join(widget(w) for w in section.get("widgets") or []) + "</div>"
        for section in inner.get("sections") or []
    )
    return (f'<div class="card"><div class="c-head"><div class="c-title">{html.escape(str(header.get("title", "")))}'
            f'</div><div class="c-sub">{html.escape(str(header.get("subtitle", "")))}</div></div>{sections}</div>')


def _who(private: bool = False) -> str:
    only = '<span class="only">Only visible to you</span>' if private else ""
    return (f'<div class="who"><b>XTM One</b><span class="app">App</span><span class="time">8:00 AM</span>{only}'
            "</div>")


def message(payload: dict[str, Any]) -> str:
    return f'<div class="msg"><div class="avatar">X</div><div class="body">{_who()}{card(payload)}</div></div>'


def private(texts: list[str]) -> str:
    return "".join(
        f'<div class="msg private"><div class="avatar">X</div><div class="body">{_who(True)}'
        f'<div class="w-text">{html.escape(text)}</div></div></div>'
        for text in texts
    )


def _user_line(line: str) -> str:
    """One escaped line of a text message: ``<url|label>`` drawn as the label linked, a bare URL as itself."""
    if _CHAT_LINK.search(line):
        return _CHAT_LINK.sub(r'<a href="\1">\2</a>', line)
    return _URL.sub(r'<a href="\1">\1</a>', line)


def user_text(text: str) -> str:
    lines = "<br>".join(_user_line(html.escape(line, quote=False)) for line in text.splitlines())
    return ('<div class="msg"><div class="avatar person">A</div><div class="body"><div class="who"><b>Ada Lovelace'
            f'</b><span class="time">8:00 AM</span></div><div class="w-text">{lines}</div></div></div>')


def figure(state: str, language: str) -> str:
    built = build(state, language)
    if built["kind"] == "message":
        drawn = message(built["payload"])
    elif built["kind"] == "private":
        drawn = message(built["under"]) + private(built["payload"]["texts"])
    else:
        drawn = user_text(built["payload"]["text"])
    raw = json.dumps(built["payload"], ensure_ascii=False, indent=2)
    return (f'<section class="state" id="{state}"><h2>{html.escape(state)} <small>{language}</small></h2>'
            f'<div class="space">{drawn}</div>'
            f"<details><summary>cardsV2 JSON</summary><pre>{html.escape(raw)}</pre></details></section>")


CSS = """
:root{--bg:#fff;--fg:#1f1f1f;--muted:#5f6368;--line:#dadce0;--card:#fff;--card-line:#dadce0;--btn-fg:#0b57d0;
--btn-line:#747775;--link:#0b57d0;--code-bg:#f1f3f4;--private:#f8fafd;--page:#f0f4f9;--input:#fff}
.dark{--bg:#1f1f1f;--fg:#e3e3e3;--muted:#c4c7c5;--line:#444746;--card:#2d2f31;--card-line:#444746;--btn-fg:#a8c7fa;
--btn-line:#8e918f;--link:#a8c7fa;--code-bg:#3c4043;--private:#282a2c;--page:#131314;--input:#1f1f1f}
*{box-sizing:border-box}body{margin:0;background:var(--page);color:var(--fg);
font:14px/1.43 'Google Sans','Roboto','Helvetica Neue',Arial,'Noto Sans JP',sans-serif}
main{margin:0 auto;padding:16px}h1{font-size:18px}h2{font-size:13px;color:var(--muted);font-weight:500;margin:0 0 6px}
h2 small{font-weight:400}.state{margin:0 0 28px}.space{background:var(--bg);border:1px solid var(--line);
border-radius:12px;padding:8px 0}.msg{display:flex;gap:12px;padding:8px 16px}.private{background:var(--private)}
.avatar{flex:none;width:32px;height:32px;border-radius:50%;background:#0b57d0;color:#fff;display:flex;
align-items:center;justify-content:center;font-weight:700}.avatar.person{background:#b3261e}.body{min-width:0;flex:1}
.who{display:flex;gap:6px;align-items:baseline;font-size:14px}.app{font-size:11px;color:var(--muted);
border:1px solid var(--line);border-radius:4px;padding:0 4px}.time,.only{font-size:12px;color:var(--muted)}
.card{margin-top:6px;max-width:600px;background:var(--card);border:1px solid var(--card-line);border-radius:12px;
overflow:hidden}.c-head{padding:16px 16px 8px}.c-title{font-size:16px;font-weight:500}.c-sub{font-size:12px;
color:var(--muted);margin-top:2px;overflow-wrap:anywhere}.section{padding:0 16px 12px}.w-text{margin:8px 0;
overflow-wrap:anywhere}.w-text code{font:12px 'Roboto Mono',monospace;background:var(--code-bg);border-radius:4px;
padding:1px 4px}.w-deco{display:flex;flex-direction:column;margin:8px 0}.w-deco .top{font-size:12px;
color:var(--muted)}.w-buttons{display:flex;flex-wrap:wrap;gap:8px;margin:12px 0}.btn{display:inline-flex;
align-items:center;height:36px;padding:0 16px;border-radius:18px;font:500 14px 'Google Sans',Roboto,sans-serif;
border:1px solid var(--btn-line);background:transparent;color:var(--btn-fg);text-decoration:none;cursor:pointer;
max-width:100%;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.w-input{display:block;margin:12px 0}
.w-input .label{display:block;font-size:12px;color:var(--muted);margin-bottom:4px}textarea{width:100%;font:inherit;
color:var(--fg);background:var(--input);border:1px solid var(--btn-line);border-radius:4px;padding:8px}
a{color:var(--link)}details{margin-top:6px;font-size:12px}pre{white-space:pre-wrap;overflow-wrap:anywhere;
background:var(--bg);border:1px solid var(--line);padding:8px;max-height:360px;overflow:auto}
"""


def _choice(query: dict[str, list[str]], name: str, allowed: tuple[str, ...] | list[str], default: str) -> str:
    value = (query.get(name) or [default])[0]
    return value if value in allowed else default


def page(title: str, sections: str, theme: str, width: str, nav: str = "") -> bytes:
    body = (f'<!doctype html><html lang="en" class="{theme}"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width"><title>{html.escape(title)}</title>'
            f'<style>{CSS}</style></head><body><main style="max-width:{WIDTHS[width]}px">{nav}{sections}'
            "</main></body></html>")
    return body.encode()


def index() -> bytes:
    rows = []
    for state in STATES:
        links = " ".join(
            f'<a href="/card?state={state}&lang={lang}&theme={theme}&width={width}">{lang} {theme} {width}</a>'
            for lang in LANGUAGES for theme in THEMES for width in WIDTHS
        )
        rows.append(f"<li><b>{state}</b> <a href=\"/card.json?state={state}\">JSON</a><br>{links}</li>")
    every = " ".join(
        f'<a href="/all?lang={lang}&theme={theme}&width={width}">{lang} {theme} {width}</a>'
        for lang in LANGUAGES for theme in THEMES for width in WIDTHS
    )
    body = f"<h1>Google Chat decision card (#842, S11)</h1><p>All states: {every}</p><ul>{''.join(rows)}</ul>"
    return page("Google Chat decision card", body, "light", "desktop")


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A002 - the stdlib signature
        return

    def _send(self, status: int, body: bytes, kind: str = "text/html; charset=utf-8") -> None:
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - the stdlib name
        url = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(url.query)
        lang = _choice(query, "lang", LANGUAGES, "en")
        theme = _choice(query, "theme", THEMES, "light")
        width = _choice(query, "width", list(WIDTHS), "desktop")
        try:
            if url.path == "/lab/ready":
                self._send(200 if READY else 503, json.dumps({"ready": bool(READY)}).encode(), "application/json")
            elif url.path == "/":
                self._send(200, index())
            elif url.path == "/card":
                state = _choice(query, "state", STATES, STATES[0])
                self._send(200, page(f"{state} {lang}", figure(state, lang), theme, width))
            elif url.path == "/all":
                sections = "".join(figure(state, lang) for state in STATES)
                self._send(200, page(f"all {lang}", sections, theme, width))
            elif url.path == "/card.json":
                state = _choice(query, "state", STATES, STATES[0])
                payload = json.dumps(build(state, lang)["payload"], ensure_ascii=False, indent=2).encode()
                self._send(200, payload, "application/json; charset=utf-8")
            else:
                self._send(404, b"not found", "text/plain")
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True


def main() -> None:
    _backend()
    os.chdir(CHECKOUT / "backend")
    for state in STATES:  # every state renders before the preview answers: a broken renderer stops the lab
        for language in LANGUAGES:
            build(state, language)
    server = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    READY["up"] = True
    print(f"lab up: http://localhost:{PORT}/ ({len(STATES)} states, {', '.join(LANGUAGES)}, light and dark)",
          flush=True)
    threading.Thread(target=server.serve_forever, daemon=False).start()


if __name__ == "__main__":
    main()
