"""The Slack decision card render lab: the Block Kit of the checkout drawn in a browser (#842, S10).

``[service.lab-slack-cards]`` in ``.xtm-one/checks.toml`` runs this script on the ``python-dev`` image, whose
interpreter holds the backend's locked dependencies. No Slack workspace, no database and no network: the script
imports the checkout's own ``app.services.slack_decision_cards`` and calls ``render_slack_decision`` and
``text_modal`` on fixed decisions, one per state the UX review covers, then draws the Block Kit JSON as HTML with
Slack's message, modal and ephemeral layout, so a card change is reviewed rendered, not read as code. What it
draws is the payload the platform posts; the drawing is an approximation of Slack's client (fonts, spacing,
colours of the light and dark themes), never Slack itself, and the JSON beside each card is the exact payload.

Pages (the preview port, ``PORT``):

* ``/`` lists every state with its links;
* ``/card?state=<state>&lang=<en|es|fr|ja>&theme=<light|dark>&width=<desktop|narrow>`` one state;
* ``/all?lang=..&theme=..&width=..`` every state on one page;
* ``/card.json?state=..&lang=..`` the Block Kit payload as posted (paste it in Slack's Block Kit Builder);
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

PORT = 4300
CHECKOUT = Path(__file__).resolve().parents[2]
LANGUAGES = ("en", "es", "fr", "ja")
THEMES = ("light", "dark")
WIDTHS = {"desktop": 1000, "narrow": 360}
FRONTEND_URL = "http://localhost:3100"
READY: dict[str, bool] = {}

ROW_ID = uuid.UUID("5f0c2a1e-3b4d-4e5f-8a9b-0c1d2e3f4a5b")
SOURCE_ID = uuid.UUID("0b8e6f8c-2d5c-4c55-9a51-8a3c1d2e3f40")
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
#: The refusals a click can meet, each read only by the clicker (an ephemeral message).
REFUSALS = (
    "card_decision_refused",
    "card_decision_already_answered",
    "card_changed",
    "card_not_linked_decision",
    "card_choose_approval",
    "card_decision_failed",
)


def _states() -> list[str]:
    names: list[str] = []
    for source in SOURCES:
        names += [f"open-{source}", f"open-{source}-bare"]
    names += ["open-tool-approval-many", "decided-option", "decided-text", "decided-tools", "expired", "cancelled",
              "modal", "refusal"]
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
        "recipient": "user:11111111-1111-1111-1111-111111111111",
        "status": "open",
        "answer": None,
        "answered_by": None,
        "answered_at": None,
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
    """``{"kind": "message" | "modal" | "ephemeral", "payload": ..., "under": message or None}`` of *state*."""
    _backend()
    from app.services import slack_decision_cards as cards
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
        return {"kind": "message", "payload": cards.render_slack_decision(view, language), "under": None}
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
        return {"kind": "message", "payload": cards.render_slack_decision(view, language), "under": None}
    if state in ("expired", "cancelled"):
        view = _view(_row("flow_run", "flow_ask", ASK, status=state))
        return {"kind": "message", "payload": cards.render_slack_decision(view, language), "under": None}
    open_view = _view(_row("work_item", "work_item", ASK))
    under = cards.render_slack_decision(open_view, language)
    if state == "modal":
        return {"kind": "modal", "payload": cards.text_modal(open_view, language, channel="C0LAB", ts="1.0"),
                "under": under}
    if state == "refusal":
        texts = [notice(key, language) for key in REFUSALS]
        return {"kind": "ephemeral", "payload": {"texts": texts}, "under": under}
    raise KeyError(state)


# --- Drawing ---------------------------------------------------------------------------------------------------

_TOKEN = re.compile(r"<([^<>]*)>")
_EMOJI = {":warning:": "\u26a0\ufe0f", ":white_check_mark:": "\u2705"}


def _slack_date(token: str) -> str:
    """``!date^<epoch>^<format>|<fallback>`` as a client shows it (UTC here, the viewer's zone in Slack)."""
    head, _, fallback = token.partition("|")
    parts = head.split("^")
    try:
        when = datetime.fromtimestamp(int(parts[1]), tz=UTC)
    except (IndexError, ValueError):
        return html.escape(fallback)
    return html.escape(when.strftime("%b %d, %Y at %H:%M"))


def mrkdwn(text: str) -> str:
    """Slack mrkdwn as HTML: ``*bold*``, ``_italic_``, ```code```, ``<url|label>`` and ``<!date>``.

    The text is Slack-escaped (``&amp;``, ``&lt;``, ``&gt;``) as Slack reads it, so the only raw ``<`` are
    Slack's own tokens.
    """
    tokens: list[str] = []

    def keep(match: re.Match[str]) -> str:
        body = match.group(1)
        if body.startswith("!date^"):
            tokens.append(f'<span class="date">{_slack_date(body)}</span>')
        elif body.startswith(("http://", "https://")):
            url, _, label = body.partition("|")
            tokens.append(f'<a href="{html.escape(url)}">{label or html.escape(url)}</a>')
        else:
            tokens.append(html.escape(match.group(0)))
        return f"\x00{len(tokens) - 1}\x00"

    out = _TOKEN.sub(keep, text).replace('"', "&quot;")
    out = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", out)
    out = re.sub(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])", r"<b>\1</b>", out)
    out = re.sub(r"(?<![\w_])_([^_\n]+)_(?![\w_])", r"<i>\1</i>", out)
    for name, glyph in _EMOJI.items():
        out = out.replace(name, glyph)
    out = re.sub("\x00(\\d+)\x00", lambda m: tokens[int(m.group(1))], out)
    return out.replace("\n", "<br>")


def text_object(obj: dict[str, Any]) -> str:
    if obj.get("type") == "mrkdwn":
        return mrkdwn(str(obj.get("text") or ""))
    return html.escape(str(obj.get("text") or "")).replace("\n", "<br>")


def button(element: dict[str, Any]) -> str:
    style = element.get("style") or "default"
    label = text_object(element.get("text") or {})
    if element.get("url"):
        return f'<a class="btn {style}" href="{html.escape(element["url"])}" target="_blank">{label}</a>'
    return f'<button class="btn {style}" type="button" title="{html.escape(element.get("action_id", ""))}">{label}</button>'


def block(item: dict[str, Any]) -> str:
    kind = item.get("type")
    if kind == "header":
        return f'<div class="b-header">{text_object(item["text"])}</div>'
    if kind == "section":
        return f'<div class="b-section">{text_object(item["text"])}</div>'
    if kind == "context":
        inner = " ".join(f"<span>{text_object(e)}</span>" for e in item.get("elements") or [])
        return f'<div class="b-context">{inner}</div>'
    if kind == "actions":
        return '<div class="b-actions">' + "".join(button(e) for e in item.get("elements") or []) + "</div>"
    if kind == "divider":
        return '<hr class="b-divider">'
    if kind == "input":
        element = item.get("element") or {}
        placeholder = html.escape((element.get("placeholder") or {}).get("text", ""))
        rows = 4 if element.get("multiline") else 1
        return (f'<label class="b-input"><span class="b-label">{text_object(item.get("label") or {})}</span>'
                f'<textarea rows="{rows}" placeholder="{placeholder}"></textarea></label>')
    return f'<pre class="b-unknown">{html.escape(json.dumps(item))}</pre>'


def message(payload: dict[str, Any], *, faded: bool = False) -> str:
    blocks = "".join(block(item) for item in payload.get("blocks") or [])
    return (f'<div class="msg{" faded" if faded else ""}"><div class="avatar">X</div><div class="body">'
            '<div class="who"><b>XTM One</b><span class="app">APP</span><span class="time">08:00</span></div>'
            f"{blocks}</div></div>")


def ephemeral(texts: list[str]) -> str:
    rows = "".join(
        '<div class="msg ephemeral"><div class="avatar">X</div><div class="body"><div class="who"><b>XTM One</b>'
        '<span class="app">APP</span><span class="only">Only visible to you</span></div>'
        f'<div class="b-section">{html.escape(text)}</div></div></div>'
        for text in texts
    )
    return rows


def modal(payload: dict[str, Any]) -> str:
    blocks = "".join(block(item) for item in payload.get("blocks") or [])
    return (
        '<div class="overlay"><div class="modal"><div class="m-head">'
        f'<span class="m-title">{text_object(payload.get("title") or {})}</span><span class="m-x">\u00d7</span></div>'
        f'<div class="m-body">{blocks}</div><div class="m-foot">'
        f'<button class="btn default" type="button">{text_object(payload.get("close") or {})}</button>'
        f'<button class="btn primary" type="button">{text_object(payload.get("submit") or {})}</button>'
        "</div></div></div>"
    )


def figure(state: str, language: str) -> str:
    built = build(state, language)
    if built["kind"] == "message":
        drawn = message(built["payload"])
    elif built["kind"] == "modal":
        drawn = f'<div class="stage">{message(built["under"], faded=True)}{modal(built["payload"])}</div>'
    else:
        drawn = message(built["under"]) + ephemeral(built["payload"]["texts"])
    raw = json.dumps(built["payload"], ensure_ascii=False, indent=2)
    return (f'<section class="state" id="{state}"><h2>{html.escape(state)} <small>{language}</small></h2>'
            f'<div class="channel">{drawn}</div>'
            f"<details><summary>Block Kit JSON</summary><pre>{html.escape(raw)}</pre></details></section>")


CSS = """
:root{--bg:#fff;--fg:#1d1c1d;--muted:#616061;--line:#dddddd;--btn:#fff;--btn-fg:#1d1c1d;--btn-line:#bbbabb;
--primary:#007a5a;--primary-fg:#fff;--link:#1264a3;--code-bg:#f8f8f8;--code-fg:#e01e5a;--hover:#f8f8f8;
--eph:#f8f8f8;--overlay:rgba(0,0,0,.45);--modal:#fff;--page:#f4ede4}
.dark{--bg:#1a1d21;--fg:#d1d2d3;--muted:#ababad;--line:#35373b;--btn:#1a1d21;--btn-fg:#d1d2d3;--btn-line:#565856;
--primary:#007a5a;--primary-fg:#fff;--link:#1d9bd1;--code-bg:#232529;--code-fg:#e8912d;--hover:#222529;
--eph:#222529;--overlay:rgba(0,0,0,.6);--modal:#1a1d21;--page:#111214}
*{box-sizing:border-box}body{margin:0;background:var(--page);color:var(--fg);
font:15px/1.46668 'Slack-Lato','Lato','Helvetica Neue',Helvetica,'Segoe UI',Arial,'Noto Sans JP',sans-serif}
main{margin:0 auto;padding:16px}h1{font-size:18px}h2{font-size:13px;color:var(--muted);font-weight:700;margin:0 0 6px}
h2 small{font-weight:400}.state{margin:0 0 28px}.channel{background:var(--bg);border:1px solid var(--line);
border-radius:8px;padding:8px 0;position:relative}.msg{display:flex;gap:8px;padding:8px 20px}
.msg.faded{opacity:.5}.avatar{flex:none;width:36px;height:36px;border-radius:6px;background:#4a154b;color:#fff;
display:flex;align-items:center;justify-content:center;font-weight:900}.body{min-width:0;flex:1}
.who{display:flex;gap:6px;align-items:baseline;font-size:15px}.app{font-size:10px;font-weight:700;
color:var(--muted);background:var(--hover);border-radius:2px;padding:0 3px}.time,.only{font-size:12px;
color:var(--muted)}.b-header{font-size:18px;font-weight:900;line-height:1.33;margin:6px 0 4px}
.b-section{margin:4px 0;overflow-wrap:anywhere}.b-context{font-size:12px;color:var(--muted);margin:4px 0;
overflow-wrap:anywhere}.b-context b{color:var(--muted)}.b-actions{display:flex;flex-wrap:wrap;gap:8px;margin:8px 0}
.btn{display:inline-flex;align-items:center;height:28px;padding:0 12px;border-radius:4px;font-size:13px;font-weight:700;
line-height:1;font-family:inherit;border:1px solid var(--btn-line);background:var(--btn);color:var(--btn-fg);
text-decoration:none;cursor:pointer;max-width:100%;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.btn.primary{background:var(--primary);border-color:var(--primary);color:var(--primary-fg)}
.btn.danger{background:#e01e5a;border-color:#e01e5a;color:#fff}a{color:var(--link)}
code{font:12px Monaco,Menlo,Consolas,monospace;background:var(--code-bg);color:var(--code-fg);
border:1px solid var(--line);border-radius:3px;padding:2px 3px}.ephemeral{background:var(--eph)}
.stage{min-height:460px}.overlay{position:absolute;inset:0;background:var(--overlay);display:flex;
align-items:center;justify-content:center;border-radius:8px;padding:12px}.modal{background:var(--modal);
width:520px;max-width:100%;border-radius:8px;box-shadow:0 18px 48px rgba(0,0,0,.35)}
.m-head{display:flex;justify-content:space-between;padding:18px 24px;font-size:22px;font-weight:900}
.m-x{color:var(--muted);font-weight:400}.m-body{padding:0 24px}.m-foot{display:flex;justify-content:flex-end;
gap:12px;padding:20px 24px;border-top:1px solid var(--line);margin-top:16px}.m-foot .btn{height:36px;
font-size:15px;padding:0 16px}.b-input{display:block;margin:12px 0}.b-label{display:block;font-weight:700;
margin-bottom:6px}textarea{width:100%;font:inherit;color:var(--fg);background:var(--bg);
border:1px solid var(--btn-line);border-radius:4px;padding:8px}details{margin-top:6px;font-size:12px}
pre{white-space:pre-wrap;overflow-wrap:anywhere;background:var(--bg);border:1px solid var(--line);padding:8px;
max-height:360px;overflow:auto}nav a{margin-right:10px}
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
    body = f"<h1>Slack decision card (#842, S10)</h1><p>All states: {every}</p><ul>{''.join(rows)}</ul>"
    return page("Slack decision card", body, "light", "desktop")


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
