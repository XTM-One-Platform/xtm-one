"""The xtm-one lab's language switch (#1653): one browser ``navigate`` reloads the app in another language.

``lab_asgi`` answers ``GET /api/v1/lab/locale/<lang>?next=<path>`` (the preview proxies ``/api``): it stores
``<lang>`` as the ``language`` of every user of the lab (the administrator, the peer and every seeded account; the
lab is a throwaway instance) and redirects to ``next`` (``/`` by default, a same-origin path only), so the app
reloads and resolves that language from ``/auth/me``. ``default`` stores ``auto`` (the platform language, then the
browser's). ``lab.py`` reads :data:`LAB_LOCALE_ENV` in a declaration's env and stores it for the administrator and
the peer before their signed-in links print, as ``LAB_THEME`` does.

The language codes are the backend's ``SUPPORTED_LANGUAGES`` (``app.core.utils``); a short code (``fr``) names its
locale. Never part of a deployment: the file lives under ``.xtm-one/lab`` and only a lab serves it. Importable
without the backend (``lab.py`` runs outside its virtual environment): the database write imports it lazily.

The theme switch (#1848) works the same way: ``GET /api/v1/lab/theme/<light|dark>?next=<path>`` stores that theme
as the ``theme`` preference of every user of the lab and redirects to ``next``. An explicit preference wins over
the browser's ``copilot-theme`` cache (``resolveTheme``), so one ``navigate`` repaints a self-check in the other
theme; ``emulate_media`` cannot, since the app never reads ``prefers-color-scheme``.
"""

from __future__ import annotations

from urllib.parse import parse_qs, unquote

#: The path prefix ``lab_asgi`` answers; the language follows it.
PATH_PREFIX = "/api/v1/lab/locale/"
#: The path prefix of the theme switch; the theme follows it.
THEME_PATH_PREFIX = "/api/v1/lab/theme/"
#: The declaration env variable ``lab.py`` stores at seed time.
LAB_LOCALE_ENV = "LAB_LOCALE"
#: The values of the user's ``theme`` preference (``PUT /settings/preferences`` keeps these two only).
THEMES = ("light", "dark")
#: Short code and full code of each language the platform ships, and ``default`` for ``auto``.
LOCALES = {
    "en": "en-us",
    "fr": "fr-fr",
    "es": "es-es",
    "ja": "ja-jp",
    "default": "auto",
}


def resolve(value: str) -> str | None:
    """The stored ``language`` for *value* (``fr``, ``fr-fr``, ``default``...), or ``None`` when unknown."""
    code = (value or "").strip().strip("/").lower()
    if code in LOCALES:
        return LOCALES[code]
    if code in LOCALES.values():
        return code
    return None


def safe_next(query_string: bytes) -> str:
    """The ``next`` path of *query_string*, or ``/``: a same-origin absolute path only (no scheme, no ``//``)."""
    values = parse_qs(query_string.decode("latin-1")).get("next") or []
    target = values[0] if values else ""
    if not target.startswith("/") or target.startswith("//") or "\\" in target:
        return "/"
    return target


def _segment(scope: dict, prefix: str) -> str | None:
    if scope.get("type") != "http" or scope.get("method") not in ("GET", "HEAD"):
        return None
    path = scope.get("path") or ""
    if not path.startswith(prefix):
        return None
    return unquote(path[len(prefix):])


def asks_locale(scope: dict) -> str | None:
    """The raw language segment when *scope* is ``GET /api/v1/lab/locale/<lang>``, else ``None``."""
    return _segment(scope, PATH_PREFIX)


def asks_theme(scope: dict) -> str | None:
    """The raw theme segment when *scope* is ``GET /api/v1/lab/theme/<theme>``, else ``None``."""
    return _segment(scope, THEME_PATH_PREFIX)


def resolve_theme(value: str) -> str | None:
    """The stored ``theme`` for *value* (``light``, ``Dark``...), or ``None`` when unknown."""
    code = (value or "").strip().strip("/").lower()
    return code if code in THEMES else None


async def store_language(language: str) -> int:
    """Store *language* on every user of the lab; answer how many rows changed."""
    from sqlalchemy import update

    from app.core.database import async_session
    from app.models.user import User

    async with async_session() as db:
        result = await db.execute(update(User).values(language=language))
        await db.commit()
        return int(result.rowcount or 0)


async def store_theme(theme: str) -> int:
    """Store *theme* as the preference of every user of the lab; answer how many rows changed."""
    from sqlalchemy import update

    from app.core.database import async_session
    from app.models.user import User

    async with async_session() as db:
        result = await db.execute(update(User).values(theme=theme))
        await db.commit()
        return int(result.rowcount or 0)


async def _not_found(send, body: bytes) -> None:
    await send({
        "type": "http.response.start",
        "status": 404,
        "headers": [(b"content-type", b"text/plain; charset=utf-8"), (b"content-length", str(len(body)).encode())],
    })
    await send({"type": "http.response.body", "body": body})


async def answer(scope, send, raw: str, store=store_language) -> None:
    """Store the language *raw* names and redirect to ``next``; a 404 naming the choices when it is unknown."""
    language = resolve(raw)
    if language is None:
        await _not_found(send, f"no language {raw!r}; use one of {', '.join(LOCALES)}\n".encode())
        return
    await store(language)
    await _redirect(scope, send)


async def answer_theme(scope, send, raw: str, store=store_theme) -> None:
    """Store the theme *raw* names and redirect to ``next``; a 404 naming the choices when it is unknown."""
    theme = resolve_theme(raw)
    if theme is None:
        await _not_found(send, f"no theme {raw!r}; use one of {', '.join(THEMES)}\n".encode())
        return
    await store(theme)
    await _redirect(scope, send)


async def _redirect(scope, send) -> None:
    target = safe_next(scope.get("query_string") or b"")
    await send({
        "type": "http.response.start",
        "status": 302,
        "headers": [(b"location", target.encode()), (b"cache-control", b"no-store"), (b"content-length", b"0")],
    })
    await send({"type": "http.response.body", "body": b""})
