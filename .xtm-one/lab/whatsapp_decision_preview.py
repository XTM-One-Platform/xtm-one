"""The lab's render of the WhatsApp decision message (#842, S12): every state, drawn from the real payload builder.

WhatsApp offers no surface a review can open: an interactive message is only seen on a phone linked to a
WhatsApp Business number. ``lab_asgi`` answers ``GET /api/v1/lab/whatsapp-decision`` (the preview proxies
``/api``) with an HTML page that draws, for each state, the exact ``interactive`` object
:func:`app.services.decision_whatsapp.render_whatsapp_decision` returns (or the text of
:func:`~app.services.decision_whatsapp.decision_text` when it returns ``None``) and the reply texts of
:func:`~app.services.decision_whatsapp.answer_reply`, as a WhatsApp chat lays them out: the body bubble with its
``*bold*``, ``_italic_``, ``~strike~`` and backtick formatting applied, the footer, the reply buttons under the
bubble, the list's "Choose" button and its opened sheet. The raw Cloud API payload is printed under each state.

Query: ``lang`` (en, es, fr, ja), ``theme`` (light, dark), ``width`` (phone: 390 px, desktop: 760 px),
``state`` (one state id; every state without it). ``GET /api/v1/lab/whatsapp-decision/index`` lists the links.
Never part of a deployment: the file lives under ``.xtm-one/lab`` and only a lab serves it.
"""

from __future__ import annotations

import html
import json
import re
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs

from app.models.decision_hooks import run_ask
from app.services import decision_whatsapp as wa
from app.services.channel_notices import notice

PATH = "/api/v1/lab/whatsapp-decision"
INDEX_PATH = f"{PATH}/index"
LANGUAGES = ("en", "es", "fr", "ja")
THEMES = ("light", "dark")
WIDTHS = {"phone": 390, "desktop": 760}

ROW_ID = uuid.UUID("5f0c2a1e-3b4d-4e5f-8a9b-0c1d2e3f4a5b")
SOURCE_ID = uuid.UUID("0b8e6f8c-2d5c-4c55-9a51-8a3c1d2e3f40")
ANSWERED_AT = datetime(2026, 11, 22, 8, 41, tzinfo=UTC)
DEADLINE = "2026-11-23T09:00:00Z"

#: The agent's own words of each fixture, per language (the catalog strings come from the platform).
TEXTS: dict[str, dict[str, Any]] = {
    "en": {
        "question": "Ship the fix for the login loop tonight?",
        "context": "CI is green on the release branch. Two customers reported the loop today.",
        "options": ["Ship tonight", "Wait for Monday", "Ship to staging only"],
        "effects": ["Deploys at 22:00 UTC", "Deploys Monday 09:00 UTC", "Production unchanged"],
        "free": "What should the release note say about the login loop?",
        "answer": "Tonight is fine, I will watch the dashboards.",
        "long": ["Escalate to on-call now", "Escalate to on-call later"],
        "regions": ["Europe West", "Europe North", "US East", "US West", "Asia Pacific", "Middle East"],
        "many": "Region {n}",
        "format": "Delete *all* staging data in _eu-west_ (~2 TB)?",
        "approval": "Release Manager wants to deploy the login loop fix.",
    },
    "es": {
        "question": "¿Publicar esta noche la corrección del bucle de inicio de sesión?",
        "context": "La CI está en verde en la rama de versión. Dos clientes informaron del bucle hoy.",
        "options": ["Publicar esta noche", "Esperar al lunes", "Solo en staging"],
        "effects": ["Despliegue a las 22:00 UTC", "Despliegue el lunes 09:00 UTC", "Producción sin cambios"],
        "free": "¿Qué debe decir la nota de versión sobre el bucle de inicio de sesión?",
        "answer": "Esta noche está bien, vigilaré los paneles.",
        "long": ["Avisar a la guardia hoy", "Avisar a la guardia luego"],
        "regions": ["Europa Oeste", "Europa Norte", "EE. UU. Este", "EE. UU. Oeste", "Asia Pacífico", "Oriente Medio"],
        "many": "Región {n}",
        "format": "¿Borrar *todos* los datos de staging en _eu-west_ (~2 TB)?",
        "approval": "Release Manager quiere desplegar la corrección del bucle de inicio de sesión.",
    },
    "fr": {
        "question": "Publier ce soir le correctif de la boucle de connexion ?",
        "context": "La CI est verte sur la branche de version. Deux clients ont signalé la boucle aujourd'hui.",
        "options": ["Publier ce soir", "Attendre lundi", "Staging seulement"],
        "effects": ["Déploiement à 22:00 UTC", "Déploiement lundi 09:00 UTC", "Production inchangée"],
        "free": "Que doit dire la note de version sur la boucle de connexion ?",
        "answer": "Ce soir me va, je surveille les tableaux de bord.",
        "long": ["Appeler l'astreinte ce soir", "Appeler l'astreinte demain"],
        "regions": ["Europe Ouest", "Europe Nord", "États-Unis Est", "États-Unis Ouest", "Asie-Pacifique", "Moyen-Orient"],
        "many": "Région {n}",
        "format": "Supprimer *toutes* les données de staging dans _eu-west_ (~2 To) ?",
        "approval": "Release Manager veut déployer le correctif de la boucle de connexion.",
    },
    "ja": {
        "question": "ログインループの修正を今夜リリースしますか？",
        "context": "リリースブランチの CI は成功しています。本日 2 社の顧客からループの報告がありました。",
        "options": ["今夜リリース", "月曜まで待つ", "ステージングのみ"],
        "effects": ["22:00 UTC にデプロイ", "月曜 09:00 UTC にデプロイ", "本番は変更なし"],
        "free": "ログインループについてリリースノートには何と書きますか？",
        "answer": "今夜で問題ありません。ダッシュボードを監視します。",
        "long": ["当番のセキュリティ担当エンジニアへ連絡：今すぐ", "当番のセキュリティ担当エンジニアへ連絡：明日"],
        "regions": ["欧州西部", "欧州北部", "米国東部", "米国西部", "アジア太平洋", "中東"],
        "many": "リージョン {n}",
        "format": "_eu-west_ のステージングデータを*すべて*削除しますか（~2 TB）？",
        "approval": "Release Manager がログインループ修正のデプロイを求めています。",
    },
}

PALETTES = {
    "light": {
        "page": "#ffffff", "chat": "#efeae2", "bubble": "#ffffff", "outgoing": "#d9fdd3", "text": "#111b21",
        "muted": "#667781", "action": "#027eb5", "line": "#e9edef", "link": "#027eb5", "sheet": "#ffffff",
        "code": "#f0f2f5",
    },
    "dark": {
        "page": "#111b21", "chat": "#0b141a", "bubble": "#202c33", "outgoing": "#005c4b", "text": "#e9edef",
        "muted": "#8696a0", "action": "#53bdeb", "line": "#2a3942", "link": "#53bdeb", "sheet": "#233138",
        "code": "#111b21",
    },
}


def _ask(language: str, count: int = 3, *, deadline: bool = False, labels: list[str] | None = None) -> dict[str, Any]:
    t = TEXTS[language]
    names = labels or (t["options"] if count <= 3 else t["regions"] if count <= 6 else None)
    options = []
    for i in range(count):
        label = names[i] if names and i < len(names) else t["many"].format(n=i + 1)
        option: dict[str, Any] = {"key": chr(65 + i), "label": label}
        if count <= 3 and not labels:
            option["effect"] = t["effects"][i]
        options.append(option)
    ask: dict[str, Any] = {"question": t["question"], "context": t["context"], "options": options, "recommendation": "A"}
    if deadline:
        ask["deadline"] = DEADLINE
    return ask


def _approval_ask(language: str) -> dict[str, Any]:
    """The ask production stores for a tool approval: ``run_ask`` with the run's question and ``APPROVAL_OPTIONS``."""
    _kind, ask = run_ask(TEXTS[language]["approval"], [], [{"id": "c1"}])
    return ask


#: The pending calls of the tool approval fixtures, as ``_proposals`` hands them to a decider.
CALLS: tuple[dict[str, Any], ...] = (
    {"tool_name": "deploy_service", "arguments": {"service": "auth-api", "version": "2.14.1", "region": "eu-west"}},
    {"tool_name": "purge_cdn_cache", "arguments": {"paths": ["/login", "/session"]}},
    {"tool_name": "post_status_update", "arguments": {"channel": "#release", "text": "auth-api 2.14.1 is live"}},
)


def _calls(count: int) -> tuple[dict[str, Any], ...]:
    return tuple(
        CALLS[i] if i < len(CALLS) else {"tool_name": f"scale_worker_pool_{i}", "arguments": {"replicas": i}}
        for i in range(count)
    )


def _view(kind: str, ask: dict[str, Any], status: str = "open", *, answer: dict | None = None, proposals: int = 0):
    row = SimpleNamespace(
        id=ROW_ID, source_type="work_item", source_id=SOURCE_ID, kind=kind, ask=ask, status=status,
        answer=answer, answered_at=ANSWERED_AT if status == "answered" else None,
    )
    row.public_id = f"{row.source_type}:{row.source_id}"
    page = f"/runs/{SOURCE_ID}" if kind == "tool_approval" else f"/tasks/work-items/{SOURCE_ID}"
    return SimpleNamespace(
        # The send tool builds the view with no reader (decision_message_for_tool): may_answer None, tool names only.
        decision=row, may_answer=None, recipient_name="Ada Lovelace",
        answered_by_name="Ada Lovelace" if status == "answered" else None, agent_name="Release Manager",
        page_path=page,
        proposals=_calls(proposals),
    )


def states(language: str) -> list[dict[str, Any]]:
    """Every state of the brief: ``{id, title, view}`` for a sent message, ``{id, title, reply, said}`` for a reply."""
    t = TEXTS[language]
    long = _ask(language, 2, labels=t["long"])
    free = {"question": t["free"], "context": TEXTS[language]["context"], "options": []}
    fmt = {**_ask(language, 2), "question": t["format"]}
    chosen = t["options"][0]
    return [
        {"id": "buttons-2", "title": "2 options: reply buttons", "view": _view("work_item", _ask(language, 2))},
        {"id": "buttons-3", "title": "3 options, deadline: reply buttons",
         "view": _view("work_item", _ask(language, 3, deadline=True))},
        {"id": "list-6", "title": "6 options: list (closed, then the opened sheet)",
         "view": _view("work_item", _ask(language, 6))},
        {"id": "text-12", "title": "12 options: text with the link", "view": _view("work_item", _ask(language, 12))},
        {"id": "free-text", "title": "Free-text question: text with the link", "view": _view("flow_ask", free)},
        {"id": "tool-approval", "title": "Tool approval of 1 call: reply buttons",
         "view": _view("tool_approval", _approval_ask(language), proposals=1)},
        {"id": "tool-approval-3", "title": "Tool approval of 3 calls: reply buttons",
         "view": _view("tool_approval", _approval_ask(language), proposals=3)},
        {"id": "tool-approval-many", "title": "Tool approval of 11 calls: text with the link",
         "view": _view("tool_approval", _approval_ask(language), proposals=11)},
        {"id": "collision", "title": "2 options whose 20-character titles collide: list (A1)",
         "view": _view("work_item", long)},
        {"id": "formatting", "title": "Agent text with WhatsApp formatting characters (A2, as sent today)",
         "view": _view("work_item", fmt)},
        {"id": "answered-option", "title": "Answered option, with its text",
         "view": _view("work_item", _ask(language, 3, deadline=True), "answered",
                       answer={"option": "A", "text": t["answer"]})},
        {"id": "answered-tool", "title": "Answered tool approval",
         "view": _view("tool_approval", _approval_ask(language), "answered",
                       answer={"decisions": {"c1": "approve"}}, proposals=1)},
        {"id": "answered-tool-mixed", "title": "Answered tool approval, mixed verdicts",
         "view": _view("tool_approval", _approval_ask(language), "answered",
                       answer={"decisions": {"c1": "approve", "c2": "approve", "c3": "reject"}}, proposals=3)},
        {"id": "expired", "title": "Expired", "view": _view("work_item", _ask(language, 3), "expired")},
        {"id": "cancelled", "title": "Cancelled (no longer needed)",
         "view": _view("work_item", _ask(language, 3), "cancelled")},
        {"id": "confirmation", "title": "Reply: confirmation", "reply": chosen,
         "said": notice("card_decided_option", language, name="Ada Lovelace", option=chosen)},
        *[
            {"id": f"refusal-{key.removeprefix('card_').replace('_', '-')}", "title": f"Reply: refusal ({key})",
             "reply": chosen, "said": notice(key, language)}
            for key in ("card_not_linked_decision", "card_decision_refused", "card_decision_already_answered",
                        "card_changed", "card_decision_failed")
        ],
    ]


def _formatted(text: str) -> str:
    """*text* escaped, then WhatsApp's own formatting and links applied, as the client shows it."""
    out = html.escape(text)
    out = re.sub(r"```(.+?)```", r"<code>\1</code>", out, flags=re.S)
    out = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", out)
    out = re.sub(r"(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])", r"<b>\1</b>", out)
    out = re.sub(r"(?<![\w_])_(?!\s)([^_\n]+?)(?<!\s)_(?![\w_])", r"<i>\1</i>", out)
    out = re.sub(r"(?<![\w~])~(?!\s)([^~\n]+?)(?<!\s)~(?![\w~])", r"<s>\1</s>", out)
    out = re.sub(r"(https?://[^\s<]+)", r'<a href="#">\1</a>', out)
    return out.replace("\n", "<br>")


def _message(view: Any, language: str) -> tuple[str, dict[str, Any]]:
    interactive = wa.render_whatsapp_decision(view, language)
    if interactive is None:
        payload = {"type": "text", "text": {"body": wa.decision_text(view, language)}}
        bubble = f'<div class="bubble"><div class="body">{_formatted(payload["text"]["body"])}</div>{_time()}</div>'
        return bubble, payload
    payload = {"type": "interactive", "interactive": interactive}
    footer = interactive.get("footer", {}).get("text")
    inner = f'<div class="body">{_formatted(interactive["body"]["text"])}</div>'
    if footer:
        inner += f'<div class="footer">{html.escape(footer)}</div>'
    if interactive["type"] == "button":
        buttons = "".join(
            f'<div class="action">↩ {html.escape(b["reply"]["title"])}</div>' for b in interactive["action"]["buttons"]
        )
        return f'<div class="bubble">{inner}{_time()}<div class="actions">{buttons}</div></div>', payload
    action = interactive["action"]
    closed = (f'<div class="bubble">{inner}{_time()}<div class="actions"><div class="action">☰ '
              f'{html.escape(action["button"])}</div></div></div>')
    rows = "".join(
        f'<div class="row"><div><div class="row-title">{html.escape(r["title"])}</div>'
        + (f'<div class="row-desc">{html.escape(r["description"])}</div>' if r.get("description") else "")
        + '</div><span class="radio"></span></div>'
        for section in action["sections"] for r in section["rows"]
    )
    sections = "".join(f'<div class="section">{html.escape(s["title"])}</div>' for s in action["sections"][:1])
    sheet = (f'<div class="sheet"><div class="sheet-head">✕ <span>{html.escape(action["button"])}</span></div>'
             f'{sections}{rows}<div class="send">{html.escape(notice("card_choose", language))}</div></div>')
    return closed + sheet, payload


def _time() -> str:
    return '<span class="time">08:41</span>'


def _block(state: dict[str, Any], language: str) -> str:
    title = f'<h2 id="{state["id"]}">{html.escape(state["title"])} <small>{state["id"]}</small></h2>'
    if "view" in state:
        bubble, payload = _message(state["view"], language)
        raw = html.escape(json.dumps(payload, ensure_ascii=False, indent=2))
        return f'{title}<div class="chat">{bubble}</div><details><summary>Cloud API payload</summary><pre>{raw}</pre></details>'
    outgoing = f'<div class="bubble out"><div class="quote">↩</div><div class="body">{html.escape(state["reply"])}</div>{_time()}</div>'
    said = f'<div class="bubble"><div class="body">{_formatted(state["said"])}</div>{_time()}</div>'
    return f'{title}<div class="chat">{outgoing}{said}</div>'


def _css(theme: str, width: int) -> str:
    p = PALETTES[theme]
    return f"""
body {{ margin: 0; background: {p['page']}; color: {p['text']};
  font: 14.2px/19px "Segoe UI", "Helvetica Neue", Helvetica, "Noto Sans JP", Arial, sans-serif; }}
main {{ width: {width}px; margin: 0 auto; padding: 16px 0 48px; }}
h1 {{ font-size: 16px; margin: 0 12px 8px; }}
h2 {{ font-size: 13px; font-weight: 600; margin: 20px 12px 6px; color: {p['muted']}; }}
h2 small {{ font-weight: 400; }}
.chat {{ background: {p['chat']}; padding: 12px 8% 12px 12px; display: flex; flex-direction: column; gap: 6px; }}
.bubble {{ background: {p['bubble']}; border-radius: 7.5px; padding: 6px 7px 8px 9px; max-width: 85%;
  box-shadow: 0 1px .5px rgba(11,20,26,.13); position: relative; align-self: flex-start; overflow-wrap: anywhere; }}
.bubble.out {{ background: {p['outgoing']}; align-self: flex-end; }}
.quote {{ color: {p['muted']}; font-size: 12px; }}
.footer {{ color: {p['muted']}; font-size: 13px; margin-top: 4px; }}
.time {{ display: block; text-align: right; color: {p['muted']}; font-size: 11px; line-height: 15px; }}
.actions {{ margin: 6px -7px -8px -9px; }}
.action {{ border-top: 1px solid {p['line']}; color: {p['action']}; text-align: center; padding: 10px 8px;
  font-weight: 500; }}
a {{ color: {p['link']}; }}
code {{ background: {p['code']}; padding: 0 3px; border-radius: 3px; font-family: Menlo, Consolas, monospace; }}
.sheet {{ background: {p['sheet']}; border-radius: 12px 12px 0 0; margin-top: 8px; padding-bottom: 12px;
  box-shadow: 0 -1px 3px rgba(11,20,26,.2); }}
.sheet-head {{ padding: 14px 16px; font-weight: 600; border-bottom: 1px solid {p['line']}; }}
.sheet-head span {{ margin-left: 12px; }}
.section {{ color: {p['muted']}; font-size: 13px; padding: 12px 16px 4px; }}
.row {{ display: flex; justify-content: space-between; align-items: center; gap: 12px; padding: 10px 16px;
  border-bottom: 1px solid {p['line']}; }}
.row-desc {{ color: {p['muted']}; font-size: 13px; }}
.radio {{ width: 18px; height: 18px; border: 2px solid {p['muted']}; border-radius: 50%; flex: none; }}
.send {{ margin: 12px 16px 0; text-align: center; color: {p['muted']}; padding: 10px; border-radius: 20px;
  border: 1px solid {p['line']}; }}
details {{ margin: 4px 12px; color: {p['muted']}; font-size: 12px; }}
pre {{ white-space: pre-wrap; overflow-wrap: anywhere; }}
nav a {{ margin-right: 8px; }}
"""


def _one(query: dict[str, list[str]], name: str, allowed, default: str) -> str:
    value = (query.get(name) or [default])[0]
    return value if value in allowed else default


def page(query_string: bytes | str) -> str:
    """The HTML of the render for one language, theme and width (every state, or the one ``state`` names)."""
    raw = query_string.decode("latin-1") if isinstance(query_string, bytes) else query_string
    query = parse_qs(raw)
    language = _one(query, "lang", LANGUAGES, "en")
    theme = _one(query, "theme", THEMES, "light")
    width_name = _one(query, "width", WIDTHS, "phone")
    every = states(language)
    wanted = (query.get("state") or [""])[0]
    shown = [s for s in every if s["id"] == wanted] or every
    blocks = "".join(_block(s, language) for s in shown)
    return (
        f'<!doctype html><html lang="{language}"><head><meta charset="utf-8">'
        f"<title>WhatsApp decision message ({language}, {theme}, {width_name})</title>"
        f"<style>{_css(theme, WIDTHS[width_name])}</style></head><body><main>"
        f"<h1>WhatsApp decision message: {language}, {theme}, {width_name}</h1>{blocks}</main></body></html>"
    )


def index() -> str:
    """Links to every language, theme and width, and to each state."""
    links = []
    for language in LANGUAGES:
        for theme in THEMES:
            for width in WIDTHS:
                href = f"{PATH}?lang={language}&theme={theme}&width={width}"
                links.append(f'<li><a href="{href}">{language} {theme} {width}</a></li>')
    ids = "".join(f'<li><a href="{PATH}?state={s["id"]}">{s["id"]}</a>: {html.escape(s["title"])}</li>'
                  for s in states("en"))
    return (f'<!doctype html><html><head><meta charset="utf-8"><title>WhatsApp decision render</title></head>'
            f"<body><h1>WhatsApp decision render</h1><ul>{''.join(links)}</ul><h2>States</h2><ul>{ids}</ul>"
            "</body></html>")


def answer(scope: dict) -> str | None:
    """The HTML the lab answers for *scope*, or ``None`` when the path is not this render's."""
    if scope.get("type") != "http" or scope.get("method") not in ("GET", "HEAD"):
        return None
    path = scope.get("path")
    if path == INDEX_PATH:
        return index()
    if path == PATH:
        return page(scope.get("query_string") or b"")
    return None
