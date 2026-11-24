# SPDX-License-Identifier: Apache-2.0

"""Answer shaping for the Odoo tools: masking, bounding, arguments.

Every answer goes through :func:`render`, which masks bank account numbers to
their last four characters and redacts any secret-looking key, then bounds
the text. Argument helpers turn what an agent sends (a list, a comma-separated
string, a JSON domain) into what Odoo takes.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

#: The longest text a tool answers; the rest is cut with a note.
MAX_ANSWER_CHARS = 24_000
DEFAULT_LIMIT = 80
MAX_LIMIT = 500
REDACTED = "[redacted]"

_SECRET_KEYS = frozenset({"password", "apikey", "key", "token", "secret", "totpsecret", "signupetoken", "signuptoken"})
_MASKED_KEYS = frozenset(
    {"accnumber", "sanitizedaccnumber", "iban", "bankaccount", "accountnumber", "bankaccnumber", "aba", "abarouting"}
)
#: Many2one fields pointing at a bank account: Odoo names the record by its number.
_MASKED_REFS = frozenset({"bankaccountid", "partnerbankid", "bankaccount"})


def _norm(key: Any) -> str:
    return str(key).replace("_", "").replace("-", "").lower()


def mask(value: Any) -> Any:
    """A bank account number as its last four characters."""
    if value in (None, False, ""):
        return value
    if not isinstance(value, (str, int)):
        return value
    text = str(value).replace(" ", "")
    return "****" if len(text) <= 4 else f"****{text[-4:]}"


def sanitize(data: Any) -> Any:
    """*data* with every secret redacted and every bank account number masked."""
    if isinstance(data, dict):
        out: dict[str, Any] = {}
        for key, value in data.items():
            norm = _norm(key)
            if norm in _SECRET_KEYS:
                out[key] = REDACTED
            elif norm in _MASKED_REFS and isinstance(value, (list, tuple)) and len(value) == 2:
                out[key] = [value[0], mask(value[1])]
            elif norm in _MASKED_KEYS:
                out[key] = mask(value)
            else:
                out[key] = sanitize(value)
        return out
    if isinstance(data, list):
        return [sanitize(item) for item in data]
    return data


def render(title: str, data: Any = None) -> str:
    """A title line, then the sanitized data as compact JSON, bounded."""
    if data is None:
        return title
    body = json.dumps(sanitize(data), ensure_ascii=False, default=str, separators=(",", ":"))
    if len(body) > MAX_ANSWER_CHARS:
        body = (
            body[:MAX_ANSWER_CHARS]
            + f"... [cut at {MAX_ANSWER_CHARS} of {len(body)} characters: narrow the filters, lower limit or "
            "page with offset]"
        )
    return f"{title}\n{body}"


def page_note(count: int, offset: int, limit: int, total: int | None) -> str:
    if total is None:
        return ""
    end = offset + count
    if end < total:
        return f" (rows {offset + 1}-{end} of {total}; next page: offset={end})"
    return f" ({total} in all)"


def clamp(value: Any, default: int = DEFAULT_LIMIT, maximum: int = MAX_LIMIT) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(number, maximum))


def offset_of(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def id_list(value: Any) -> list[int]:
    """Integer ids from a list, a comma-separated string or one id."""
    if value is None or value == "" or value is False:
        return []
    items = value if isinstance(value, (list, tuple)) else str(value).split(",")
    out: list[int] = []
    for item in items:
        text = str(item).strip()
        if text.isdigit():
            out.append(int(text))
    return out


def str_list(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    items = value if isinstance(value, (list, tuple)) else str(value).split(",")
    return [str(i).strip() for i in items if str(i).strip()]


def to_date(value: Any, name: str) -> tuple[str | None, str | None]:
    """``(YYYY-MM-DD, None)``, ``(None, None)`` when empty, or ``(None, error)``."""
    raw = str(value or "").strip()
    if not raw:
        return None, None
    try:
        return date.fromisoformat(raw[:10]).isoformat(), None
    except ValueError:
        return None, f"Error: {name} must be a date (YYYY-MM-DD), not {raw!r}."


def parse_domain(value: Any) -> tuple[list[Any], str | None]:
    """An Odoo domain from a list or its JSON text; ``([], None)`` when empty."""
    if value in (None, "", []):
        return [], None
    data = value
    if isinstance(value, str):
        try:
            data = json.loads(value)
        except ValueError:
            return [], (
                "Error: domain must be an Odoo domain as a JSON list, e.g. "
                '[["state", "=", "posted"], ["date", ">=", "2026-01-01"]].'
            )
    if not isinstance(data, list):
        return [], "Error: domain must be a list of conditions."
    out: list[Any] = []
    for item in data:
        if isinstance(item, str) and item in ("&", "|", "!"):
            out.append(item)
        elif isinstance(item, (list, tuple)) and len(item) == 3:
            out.append(list(item))
        else:
            return [], f"Error: {item!r} is not a domain condition [field, operator, value] or '&', '|', '!'."
    return out, None


def parse_values(value: Any) -> tuple[dict[str, Any], str | None]:
    """Field values from a dict or its JSON text."""
    data = value
    if isinstance(value, str):
        try:
            data = json.loads(value or "{}")
        except ValueError:
            return {}, "Error: values must be a JSON object of field: value."
    if not isinstance(data, dict) or not data:
        return {}, "Error: give at least one field value."
    return data, None


def m2o(value: Any) -> tuple[int | None, str]:
    """A many2one value ``[id, "name"]`` as ``(id, name)``."""
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        return int(value[0]), str(value[1])
    if isinstance(value, int) and not isinstance(value, bool):
        return value, ""
    return None, ""


def money(value: Any) -> float:
    try:
        return round(float(value or 0), 2)
    except (TypeError, ValueError):
        return 0.0
