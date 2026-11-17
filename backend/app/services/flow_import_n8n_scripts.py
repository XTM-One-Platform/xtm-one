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

r"""Python that reproduces n8n's data nodes in a Code node.

n8n transforms items with built-in nodes (Aggregate, Sort, Date & Time, HTML,
XML, ...); XTM One has no node per transformation, it has a Code node running
Python in the platform sandbox. :mod:`app.services.flow_import_n8n` converts
each such n8n node into a Code node running one of the scripts below:

- the script is fixed text, the same for every workflow;
- the node's settings travel as JSON in the ``settings`` input, its
  per-item values as other inputs: an n8n ``$json.path`` reference becomes a
  path the script reads in each item (``SETTINGS["paths"]``), any other value
  is rendered once by the flow (``{{node.output}}``);
- the items are the ``items`` input, the upstream node's whole output — a
  list is a list of items, anything else one item.

Standard library only: the scripts must run in the local fallback sandbox as
in OpenSandbox. None of them contains ``{{`` or ``}}``, which the importer
breaks up in every imported string (pinned by the tests).
"""

from __future__ import annotations

PRELUDE = r'''import json

# Generated from an n8n node: SETTINGS is the node's configuration, `items`
# the upstream output (a list holds one n8n item per entry).
SETTINGS = json.loads(inputs.pop("settings", None) or "{}")


def load(value):
    """A rendered input: JSON text becomes the value it encodes."""
    if isinstance(value, str):
        text = value.strip()
        if text[:1] in ("{", "[") or text in ("true", "false", "null"):
            try:
                return json.loads(text)
            except ValueError:
                return value
    return value


def as_items(value):
    value = load(value)
    if value is None or value == "":
        return []
    return value if isinstance(value, list) else [value]


def get_path(item, path):
    for key in [k for k in str(path or "").split(".") if k]:
        if isinstance(item, dict):
            item = item.get(key)
        elif isinstance(item, list) and key.isdigit() and int(key) < len(item):
            item = item[int(key)]
        else:
            return None
    return item


def set_path(item, path, value):
    keys = [k for k in str(path).split(".") if k] or ["data"]
    for key in keys[:-1]:
        child = item.get(key)
        if not isinstance(child, dict):
            child = item[key] = dict()
        item = child
    item[keys[-1]] = value


def arg(item, name):
    """A per-item parameter: a path read in the item, or a value rendered once."""
    paths = SETTINGS.get("paths") or dict()
    if name in paths:
        return get_path(item, paths[name])
    return load(inputs.get(name))


def to_text(value):
    """A value as n8n's expressions write it: text as is, anything else compact JSON."""
    if value is None:
        return ""
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def text_arg(item, name):
    """A per-item text parameter as written: a rendered value is never parsed."""
    paths = SETTINGS.get("paths") or dict()
    return to_text(get_path(item, paths[name]) if name in paths else inputs.get(name))


def emit(value):
    # One item prints as the item itself: the Code node publishes a printed
    # object's keys as `fields`, which the importer's references read.
    if isinstance(value, list) and len(value) == 1 and isinstance(value[0], dict):
        value = value[0]
    print(json.dumps(value, ensure_ascii=False, default=str))


def each(transform):
    """One item in, one item out — for a list, item by item."""
    raw = load(inputs.get("items"))
    if isinstance(raw, list):
        emit([transform(item if isinstance(item, dict) else dict(data=item)) for item in raw])
    elif isinstance(raw, dict):
        emit(transform(raw))
    else:
        emit(transform(dict() if raw in (None, "") else dict(data=raw)))


def copy(item):
    return json.loads(json.dumps(item, default=str))
'''

AGGREGATE = r"""
items = as_items(inputs.get("items"))
if SETTINGS.get("mode") == "all":
    keep = SETTINGS.get("include") or "all"
    fields = SETTINGS.get("fields") or []

    def pick(item):
        if not isinstance(item, dict) or keep == "all":
            return item
        if keep == "specified":
            return dict((f, get_path(item, f)) for f in fields)
        return dict((k, v) for k, v in item.items() if k not in fields)

    emit(dict([(SETTINGS.get("destination") or "data", [pick(i) for i in items])]))
else:
    result = dict()
    for spec in SETTINGS.get("fields") or []:
        values = [get_path(i, spec["field"]) for i in items]
        if not SETTINGS.get("keep_missing"):
            values = [v for v in values if v is not None]
        if SETTINGS.get("merge_lists"):
            merged = []
            for value in values:
                merged.extend(value if isinstance(value, list) else [value])
            values = merged
        result[spec.get("rename") or spec["field"]] = values
    emit(result)
"""

LIMIT = r"""
items = as_items(inputs.get("items"))
count = max(0, int(SETTINGS.get("max", 1)))
if SETTINGS.get("keep") == "last":
    emit(items[-count:] if count else [])
else:
    emit(items[:count])
"""

SORT = r"""
items = as_items(inputs.get("items"))


def key_of(value):
    if value is None:
        return (0, 0, "")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return (1, value, "")
    if isinstance(value, bool):
        return (1, int(value), "")
    return (2, 0, value if isinstance(value, str) else json.dumps(value, sort_keys=True))


if SETTINGS.get("random"):
    import random

    random.shuffle(items)
else:
    for spec in reversed(SETTINGS.get("fields") or []):
        field = spec.get("field")
        items.sort(key=lambda item, field=field: key_of(get_path(item, field)), reverse=spec.get("order") == "descending")
emit(items)
"""

REMOVE_DUPLICATES = r"""
items = as_items(inputs.get("items"))
compare = SETTINGS.get("compare") or "allFields"
fields = SETTINGS.get("fields") or []


def signature(item):
    if compare == "selectedFields":
        value = [get_path(item, f) for f in fields]
    elif compare == "allFieldsExcept" and isinstance(item, dict):
        value = dict((k, v) for k, v in item.items() if k not in fields)
    else:
        value = item
    return json.dumps(value, sort_keys=True, default=str)


seen = set()
kept = []
for item in items:
    key = signature(item)
    if key not in seen:
        seen.add(key)
        kept.append(item)
emit(kept)
"""

SUMMARIZE = r"""
items = as_items(inputs.get("items"))
split_by = SETTINGS.get("split_by") or []
PREFIX = dict(
    append="appended", average="average", concatenate="concatenated", count="count",
    countUnique="unique_count", max="max", min="min", sum="sum",
)


def to_number(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def summarize(group):
    row = dict()
    for spec in SETTINGS.get("fields") or []:
        field = spec["field"]
        how = spec.get("aggregation") or "count"
        present = [v for v in (get_path(i, field) for i in group) if v not in (None, "")]
        numbers = [n for n in (to_number(v) for v in present) if n is not None]
        if how == "count":
            value = len(present)
        elif how == "countUnique":
            value = len(set(json.dumps(v, sort_keys=True, default=str) for v in present))
        elif how == "append":
            value = present
        elif how == "concatenate":
            value = str(spec.get("separator", ",")).join(v if isinstance(v, str) else json.dumps(v) for v in present)
        elif how == "sum":
            value = sum(numbers)
        elif how == "average":
            value = sum(numbers) / len(numbers) if numbers else None
        elif how == "min":
            value = min(numbers) if numbers else None
        else:
            value = max(numbers) if numbers else None
        row[PREFIX.get(how, how) + "_" + field.replace(".", "_")] = value
    return row


if split_by:
    groups, order = dict(), []
    for item in items:
        key = json.dumps([get_path(item, f) for f in split_by], default=str)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(item)
    rows = []
    for key in order:
        row = dict(zip(split_by, json.loads(key)))
        row.update(summarize(groups[key]))
        rows.append(row)
    emit(rows)
else:
    emit(summarize(items))
"""

RENAME_KEYS = r"""
def rename(item):
    item = copy(item)
    for pair in SETTINGS.get("keys") or []:
        old, new = pair.get("current"), pair.get("new")
        if not old or not new:
            continue
        *parents, leaf = old.split(".")
        parent = get_path(item, ".".join(parents)) if parents else item
        if isinstance(parent, dict) and leaf in parent:
            set_path(item, new, parent.pop(leaf))
    return item


each(rename)
"""

DATE_TIME = r"""
import calendar
from datetime import datetime, timedelta, timezone

MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
UNITS = dict(
    millisecond=0.001, milliseconds=0.001, ms=0.001, second=1, seconds=1, s=1,
    minute=60, minutes=60, m=60, hour=3600, hours=3600, h=3600,
    day=86400, days=86400, d=86400, week=604800, weeks=604800, w=604800,
)
MONTH_UNITS = dict(month=1, months=1, M=1, quarter=3, quarters=3, Q=3, year=12, years=12, y=12)


def parse_date(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(value / 1000 if abs(value) > 1e11 else value, timezone.utc)
    text = str(value if value is not None else "").strip()
    if not text:
        raise ValueError("The date to read is empty.")
    if text.lstrip("-").isdigit():
        return parse_date(int(text))
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        from email.utils import parsedate_to_datetime

        try:
            parsed = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            raise ValueError("Cannot read the date " + repr(text[:80])) from None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def shift(moment, amount, unit):
    if unit in MONTH_UNITS:
        months = moment.month - 1 + int(amount) * MONTH_UNITS[unit]
        year, month = moment.year + months // 12, months % 12 + 1
        day = min(moment.day, calendar.monthrange(year, month)[1])
        return moment.replace(year=year, month=month, day=day)
    return moment + timedelta(seconds=float(amount) * UNITS.get(unit, 86400))


def tokens(moment, luxon):
    hour12 = moment.hour % 12 or 12
    meridiem = "AM" if moment.hour < 12 else "PM"
    table = dict()
    table["yyyy" if luxon else "YYYY"] = "%04d" % moment.year
    table["yy" if luxon else "YY"] = "%02d" % (moment.year % 100)
    table["MMMM"] = MONTHS[moment.month - 1]
    table["MMM"] = MONTHS[moment.month - 1][:3]
    table["MM"] = "%02d" % moment.month
    table["M"] = str(moment.month)
    table["dd" if luxon else "DD"] = "%02d" % moment.day
    table["d" if luxon else "D"] = str(moment.day)
    table["EEEE" if luxon else "dddd"] = DAYS[moment.weekday()]
    table["EEE" if luxon else "ddd"] = DAYS[moment.weekday()][:3]
    table["HH"] = "%02d" % moment.hour
    table["H"] = str(moment.hour)
    table["hh"] = "%02d" % hour12
    table["h"] = str(hour12)
    table["mm"] = "%02d" % moment.minute
    table["m"] = str(moment.minute)
    table["ss"] = "%02d" % moment.second
    table["s"] = str(moment.second)
    table["SSS"] = "%03d" % (moment.microsecond // 1000)
    table["a" if luxon else "A"] = meridiem
    if not luxon:
        table["a"] = meridiem.lower()
    offset = moment.strftime("%z")
    table["ZZ"] = offset
    table["Z"] = offset[:3] + ":" + offset[3:] if offset else ""
    table["X"] = str(int(moment.timestamp()))
    table["x"] = str(int(moment.timestamp() * 1000))
    return table


def format_date(moment, pattern, luxon):
    if not pattern:
        return moment.isoformat()
    table = tokens(moment, luxon)
    names = sorted(table, key=len, reverse=True)
    out, i = [], 0
    while i < len(pattern):
        ch = pattern[i]
        close = "'" if luxon and ch == "'" else ("]" if not luxon and ch == "[" else "")
        if close:
            end = pattern.find(close, i + 1)
            end = len(pattern) if end < 0 else end
            out.append(pattern[i + 1:end])
            i = end + 1
            continue
        name = next((n for n in names if pattern.startswith(n, i)), None)
        if name is None:
            out.append(ch)
            i += 1
        else:
            out.append(table[name])
            i += len(name)
    return "".join(out)


# Luxon's DateTime.diff, which n8n's Get Time Between Dates calls: calendar
# units count whole steps from the start, the smallest one takes the rest as a
# fraction of its next step; clock units share the remaining milliseconds.
CALENDAR = ["years", "quarters", "months", "weeks", "days"]
CLOCK = dict(hours=3600000, minutes=60000, seconds=1000, milliseconds=1)
DIFF_UNITS = dict(y="years", Q="quarters", M="months", w="weeks", d="days", h="hours", m="minutes", s="seconds", ms="milliseconds")


def plus(moment, counts):
    months = 12 * counts.get("years", 0) + 3 * counts.get("quarters", 0) + counts.get("months", 0)
    days = 7 * counts.get("weeks", 0) + counts.get("days", 0)
    return shift(moment, months, "months") + timedelta(days=days)


def calendar_steps(unit, a, b):
    if unit == "years":
        return b.year - a.year
    if unit == "quarters":
        return (b.month - 1) // 3 - (a.month - 1) // 3 + (b.year - a.year) * 4
    if unit == "months":
        return b.month - a.month + (b.year - a.year) * 12
    days = (b.date() - a.date()).days
    return days // 7 if unit == "weeks" else days


def number(value):
    return int(value) if value == int(value) else value


def between(start, end, units):
    wanted = []
    for unit in units or ["day"]:
        unit = DIFF_UNITS.get(str(unit), str(unit).rstrip("s") + "s")
        wanted.append(unit if unit in CALENDAR or unit in CLOCK else "days")
    sign = -1 if end < start else 1
    start, end = sorted([start.astimezone(timezone.utc), end.astimezone(timezone.utc)])
    counts, cursor, high, lowest = dict(), start, start, None
    for unit in CALENDAR:
        if unit not in wanted:
            continue
        lowest = unit
        counts[unit] = calendar_steps(unit, cursor, end)
        high = plus(start, counts)
        if high > end:
            counts[unit] -= 1
            cursor = plus(start, counts)
            if cursor > end:
                high = cursor
                counts[unit] -= 1
                cursor = plus(start, counts)
        else:
            cursor = high
    rest = (end - cursor) // timedelta(milliseconds=1)
    clock = [unit for unit in CLOCK if unit in wanted]
    if clock:
        for unit in clock[:-1]:
            counts[unit], rest = divmod(rest, CLOCK[unit])
        counts[clock[-1]] = rest / CLOCK[clock[-1]]
    elif lowest is not None:
        if high <= cursor or high < end:
            high = plus(cursor, dict([(lowest, 1)]))
        counts[lowest] += rest / ((high - cursor) // timedelta(milliseconds=1))
    return dict((unit, number(sign * counts[unit])) for unit in wanted)


def transform(item):
    operation = SETTINGS.get("operation") or "formatDate"
    luxon = SETTINGS.get("tokens") != "moment"
    now = datetime.now(timezone.utc)
    if operation == "getCurrentDate":
        value = now.isoformat() if SETTINGS.get("include_time", True) else now.date().isoformat()
    elif operation in ("addToDate", "subtractFromDate"):
        sign = -1 if operation == "subtractFromDate" else 1
        amount = float(arg(item, "duration") or 0) * sign
        value = shift(parse_date(arg(item, "date")), amount, str(arg(item, "unit") or "days")).isoformat()
    elif operation == "formatDate":
        value = format_date(parse_date(arg(item, "date")), str(arg(item, "format") or ""), luxon)
    elif operation == "roundDate":
        moment = parse_date(arg(item, "date"))
        unit = SETTINGS.get("to_nearest") or "day"
        if unit == "week":
            floored = moment.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=moment.weekday())
        else:
            order = ["year", "month", "day", "hour", "minute", "second"]
            level = order.index(unit) if unit in order else 2
            reset = dict(month=1, day=1, hour=0, minute=0, second=0)
            floored = moment.replace(microsecond=0, **dict((k, v) for k, v in reset.items() if order.index(k) > level))
        if SETTINGS.get("mode") == "roundUp" and floored != moment:
            floored = shift(floored, 1, unit)
        value = floored.isoformat()
    elif operation == "getTimeBetweenDates":
        value = between(parse_date(arg(item, "start")), parse_date(arg(item, "end")), SETTINGS.get("units"))
    elif operation == "extractDate":
        moment = parse_date(arg(item, "date"))
        part = SETTINGS.get("part") or "month"
        value = moment.isocalendar()[1] if part == "week" else getattr(moment, part, None)
    else:
        raise ValueError("Unsupported Date & Time operation: " + str(operation))
    out = copy(item)
    set_path(out, SETTINGS.get("output") or "data", value)
    return out


each(transform)
"""

CRYPTO = r"""
import base64
import hashlib
import hmac
import secrets
import string
import uuid

ALGORITHMS = dict(
    MD5="md5", SHA1="sha1", SHA256="sha256", SHA384="sha384", SHA512="sha512",
    SHA3_256="sha3_256", SHA3_384="sha3_384", SHA3_512="sha3_512", RIPEMD160="ripemd160",
)


def transform(item):
    action = SETTINGS.get("action") or "hash"
    if action == "generate":
        kind = SETTINGS.get("generate") or "uuid"
        length = int(SETTINGS.get("length") or 32)
        if kind == "uuid":
            value = str(uuid.uuid4())
        elif kind == "hex":
            value = secrets.token_hex(length)[:length]
        elif kind == "base64":
            value = base64.b64encode(secrets.token_bytes(length)).decode()[:length]
        else:
            value = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(length))
    elif action in ("hash", "hmac"):
        data = text_arg(item, "value")
        name = ALGORITHMS.get(str(SETTINGS.get("type") or "MD5").upper().replace("-", "_"), "md5")
        if action == "hmac":
            digest = hmac.new(str(inputs.get("secret") or "").encode(), data.encode(), name).digest()
        else:
            digest = hashlib.new(name, data.encode()).digest()
        value = base64.b64encode(digest).decode() if SETTINGS.get("encoding") == "base64" else digest.hex()
    else:
        raise ValueError("Signing with a private key is not reproduced here.")
    out = copy(item)
    set_path(out, SETTINGS.get("output") or "data", value)
    return out


each(transform)
"""

# A small CSS selector engine over html.parser: tag, *, #id, .class,
# [attr], [attr=|^=|$=|*=|~=|"|="value], :first-child, :last-child,
# :nth-child(n), descendant and child (>) combinators, selector lists.
HTML_DOM = r"""
import re
from html.parser import HTMLParser

VOID = set("area base br col embed hr img input link meta param source track wbr".split())
COMPOUND = re.compile(r"(?:[A-Za-z][\w-]*|\*)?(?:#[\w-]+|\.[\w-]+|\[[^\]]*\]|:[\w-]+(?:\([^)]*\))?)*")
PART = re.compile(
    r"#([\w-]+)|\.([\w-]+)|\[\s*([\w:-]+)\s*(?:([~^$*|]?=)\s*(\"[^\"]*\"|'[^']*'|[^\]\s]*))?\s*\]"
    r"|:([\w-]+)(?:\(([^)]*)\))?"
)


class Node:
    def __init__(self, tag, attrs, parent):
        self.tag, self.attrs, self.parent, self.children = tag, attrs, parent, []


class Builder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = self.current = Node("#root", dict(), None)

    def handle_starttag(self, tag, attrs):
        node = Node(tag, dict((k, v or "") for k, v in attrs), self.current)
        self.current.children.append(node)
        if tag not in VOID:
            self.current = node

    def handle_startendtag(self, tag, attrs):
        self.current.children.append(Node(tag, dict((k, v or "") for k, v in attrs), self.current))

    def handle_endtag(self, tag):
        node = self.current
        while node is not self.root and node.tag != tag:
            node = node.parent
        if node is not self.root:
            self.current = node.parent

    def handle_data(self, data):
        self.current.children.append(data)


def parse_html(text):
    builder = Builder()
    builder.feed(str(text or ""))
    builder.close()
    return builder.root


def elements(node):
    return [c for c in node.children if isinstance(c, Node)]


def split_top(text):
    parts, depth, quote, start = [], 0, "", 0
    for i, ch in enumerate(text):
        if quote:
            quote = "" if ch == quote else quote
        elif ch in "\"'":
            quote = ch
        elif ch in "[(":
            depth += 1
        elif ch in "])":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append(text[start:i])
            start = i + 1
    parts.append(text[start:])
    return [p.strip() for p in parts if p.strip()]


def compile_compound(text):
    tag = re.match(r"[A-Za-z][\w-]*|\*", text)
    compound = dict(tag=tag.group(0).lower() if tag else "*", parts=[])
    for m in PART.finditer(text, tag.end() if tag else 0):
        if m.group(6) and m.group(6) not in ("first-child", "last-child", "nth-child"):
            raise ValueError("Unsupported CSS pseudo-class :" + m.group(6))
        value = m.group(5)
        if value and value[0] in "\"'":
            value = value[1:-1]
        compound["parts"].append((m.group(1), m.group(2), m.group(3), m.group(4), value, m.group(6), m.group(7)))
    return compound


def compile_selector(selector):
    groups = []
    for part in split_top(selector):
        steps, i, combinator = [], 0, " "
        while i < len(part):
            ch = part[i]
            if ch.isspace():
                i += 1
                continue
            if ch == ">":
                combinator, i = ">", i + 1
                continue
            if ch in "+~":
                raise ValueError("Unsupported CSS combinator " + ch)
            m = COMPOUND.match(part, i)
            if m is None or m.end() == i:
                raise ValueError("Unsupported CSS selector: " + selector)
            steps.append((combinator, compile_compound(m.group(0))))
            combinator, i = " ", m.end()
        if steps:
            groups.append(steps)
    return groups


def match_compound(node, compound):
    if compound["tag"] != "*" and node.tag != compound["tag"]:
        return False
    for ident, klass, attr, op, value, pseudo, pseudo_arg in compound["parts"]:
        if ident is not None and node.attrs.get("id") != ident:
            return False
        if klass is not None and klass not in node.attrs.get("class", "").split():
            return False
        if attr is not None:
            if attr not in node.attrs:
                return False
            actual, value = node.attrs[attr], value or ""
            if op == "=" and actual != value:
                return False
            if op == "^=" and not actual.startswith(value):
                return False
            if op == "$=" and not actual.endswith(value):
                return False
            if op == "*=" and value not in actual:
                return False
            if op == "~=" and value not in actual.split():
                return False
            if op == "|=" and actual != value and not actual.startswith(value + "-"):
                return False
        if pseudo is not None:
            siblings = elements(node.parent)
            position = siblings.index(node) + 1
            if pseudo == "first-child" and position != 1:
                return False
            if pseudo == "last-child" and position != len(siblings):
                return False
            if pseudo == "nth-child" and str(position) != str(pseudo_arg or "").strip():
                return False
    return True


def matches(node, steps, index):
    combinator, compound = steps[index]
    if not match_compound(node, compound):
        return False
    if index == 0:
        return True
    parent = node.parent
    if combinator == ">":
        return parent is not None and parent.tag != "#root" and matches(parent, steps, index - 1)
    while parent is not None and parent.tag != "#root":
        if matches(parent, steps, index - 1):
            return True
        parent = parent.parent
    return False


def select(root, selector):
    groups = compile_selector(selector)
    found, stack = [], list(reversed(elements(root)))
    while stack:
        node = stack.pop()
        if any(matches(node, steps, len(steps) - 1) for steps in groups):
            found.append(node)
        stack.extend(reversed(elements(node)))
    return found


def text_of(node):
    if isinstance(node, str):
        return node
    if node.tag in ("script", "style"):
        return ""
    return "".join(text_of(c) for c in node.children)


def html_of(node, outer=False):
    if isinstance(node, str):
        return node.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    inner = "".join(html_of(c, True) for c in node.children)
    if not outer:
        return inner
    attrs = "".join(' %s="%s"' % (k, v.replace('"', "&quot;")) for k, v in node.attrs.items())
    if node.tag in VOID:
        return "<%s%s>" % (node.tag, attrs)
    return "<%s%s>%s</%s>" % (node.tag, attrs, inner, node.tag)
"""

HTML_EXTRACT = r"""
def clean(value):
    if not isinstance(value, str):
        return value
    if SETTINGS.get("clean_up"):
        value = re.sub(r"\s+", " ", value)
    return value.strip() if SETTINGS.get("trim", True) else value


def read(node, spec):
    how = spec.get("return") or "text"
    if how == "attribute":
        return node.attrs.get(spec.get("attribute") or "")
    if how == "value":
        return node.attrs.get("value")
    if how == "html":
        return html_of(node)
    return text_of(node)


def transform(item):
    source = get_path(item, SETTINGS.get("property") or "data")
    if source is None and len(item) == 1:
        source = next(iter(item.values()))
    root = parse_html(source if isinstance(source, str) else json.dumps(source))
    out = dict()
    for spec in SETTINGS.get("values") or []:
        nodes = select(root, spec.get("selector") or "*")
        if spec.get("array"):
            out[spec.get("key") or "data"] = [clean(read(n, spec)) for n in nodes]
        elif (spec.get("return") or "text") == "text":
            out[spec.get("key") or "data"] = clean("".join(text_of(n) for n in nodes))
        else:
            out[spec.get("key") or "data"] = clean(read(nodes[0], spec)) if nodes else None
    return out


each(transform)
"""

HTML_TEMPLATE = r"""
emit(dict(html=inputs.get("html") or ""))
"""

HTML_TABLE = r"""
from html import escape

items = [i for i in as_items(inputs.get("items")) if isinstance(i, dict)]
columns = []
for item in items:
    columns.extend(k for k in item if k not in columns)
head = "".join("<th>%s</th>" % escape(str(c)) for c in columns)
rows = "".join(
    "<tr>%s</tr>" % "".join(
        "<td>%s</td>" % escape(v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)) for v in (i.get(c, "") for c in columns)
    )
    for i in items
)
emit(dict(table="<table><thead><tr>%s</tr></thead><tbody>%s</tbody></table>" % (head, rows)))
"""

MARKDOWN = r"""
import re
from html import escape
from html.parser import HTMLParser


class ToMarkdown(HTMLParser):
    BLOCKS = set("p div section article header footer main aside table thead tbody tr blockquote".split())

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.links, self.lists, self.skip, self.pre = [], [], [], 0, 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("script", "style", "head", "title"):
            self.skip += 1
        elif len(tag) == 2 and tag[0] == "h" and tag[1].isdigit():
            self.out.append("\n\n" + "#" * int(tag[1]) + " ")
        elif tag in self.BLOCKS:
            self.out.append("\n\n" + ("> " if tag == "blockquote" else ""))
        elif tag == "br":
            self.out.append("  \n")
        elif tag == "hr":
            self.out.append("\n\n---\n\n")
        elif tag in ("strong", "b"):
            self.out.append("**")
        elif tag in ("em", "i"):
            self.out.append("_")
        elif tag == "code" and not self.pre:
            self.out.append("`")
        elif tag == "pre":
            self.pre += 1
            self.out.append("\n\n```\n")
        elif tag in ("ul", "ol"):
            self.lists.append([tag, 0])
            self.out.append("\n")
        elif tag == "li":
            depth = max(0, len(self.lists) - 1)
            marker = "- "
            if self.lists and self.lists[-1][0] == "ol":
                self.lists[-1][1] += 1
                marker = "%d. " % self.lists[-1][1]
            self.out.append("\n" + "  " * depth + marker)
        elif tag == "a":
            self.links.append(a.get("href") or "")
            self.out.append("[")
        elif tag == "img":
            self.out.append("![%s](%s)" % (a.get("alt") or "", a.get("src") or ""))
        elif tag in ("td", "th"):
            self.out.append(" | ")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head", "title"):
            self.skip = max(0, self.skip - 1)
        elif len(tag) == 2 and tag[0] == "h" and tag[1].isdigit() or tag in self.BLOCKS:
            self.out.append("\n\n")
        elif tag in ("strong", "b"):
            self.out.append("**")
        elif tag in ("em", "i"):
            self.out.append("_")
        elif tag == "code" and not self.pre:
            self.out.append("`")
        elif tag == "pre":
            self.pre = max(0, self.pre - 1)
            self.out.append("\n```\n\n")
        elif tag in ("ul", "ol") and self.lists:
            self.lists.pop()
            self.out.append("\n")
        elif tag == "a":
            self.out.append("](%s)" % (self.links.pop() if self.links else ""))

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data if self.pre else re.sub(r"\s+", " ", data))

    def markdown(self):
        text = "".join(self.out)
        text = re.sub(r"[ \t]+\n", "\n", text)
        return re.sub(r"\n{3,}", "\n\n", text).strip()


def to_html(text):
    try:
        import markdown

        return markdown.markdown(text)
    except ImportError:
        pass
    blocks = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = block.splitlines()
        heading = re.match(r"(#{1,6})\s+(.*)", block)
        if heading and len(lines) == 1:
            level = len(heading.group(1))
            tag = "h" + str(level)
            blocks.append("<" + tag + ">" + inline(heading.group(2)) + "</" + tag + ">")
        elif all(re.match(r"\s*[-*+]\s", line) for line in lines):
            blocks.append("<ul>%s</ul>" % "".join("<li>%s</li>" % inline(re.sub(r"\s*[-*+]\s", "", line, count=1)) for line in lines))
        elif all(re.match(r"\s*\d+[.)]\s", line) for line in lines):
            blocks.append("<ol>%s</ol>" % "".join("<li>%s</li>" % inline(re.sub(r"\s*\d+[.)]\s", "", line, count=1)) for line in lines))
        else:
            blocks.append("<p>%s</p>" % "<br>".join(inline(line) for line in lines))
    return "\n".join(blocks)


def inline(text):
    text = escape(text, quote=False)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<![\w*])[*_]([^*_]+)[*_](?![\w*])", r"<em>\1</em>", text)
    return re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", r'<a href="\2">\1</a>', text)


def transform(item):
    source = text_arg(item, "source")
    if SETTINGS.get("mode") == "markdownToHtml":
        value = to_html(source)
    else:
        parser = ToMarkdown()
        parser.feed(source)
        parser.close()
        value = parser.markdown()
    out = copy(item)
    set_path(out, SETTINGS.get("output") or "data", value)
    return out


each(transform)
"""

XML = r"""
import re
import xml.etree.ElementTree as ET
from xml.parsers import expat

ATTRS = SETTINGS.get("attrkey") or "$"
CHARS = SETTINGS.get("charkey") or "_"


def tag_of(element):
    return element.tag.lower() if SETTINGS.get("normalize_tags") else element.tag


# Without namespace processing, as xml2js: `dc:creator` and `xmlns` keep
# their spelling, and an undeclared prefix is no error.
def parse_xml(text):
    builder = ET.TreeBuilder()
    parser = expat.ParserCreate()
    parser.StartElementHandler = builder.start
    parser.EndElementHandler = builder.end
    parser.CharacterDataHandler = builder.data
    parser.Parse(text, True)
    return builder.close()


# xml2js, which n8n's XML node runs: attributes merged into the object by
# default, whitespace-only text dropped, other text kept as written.
def to_object(element):
    obj = dict()

    def put(key, value):
        if key in obj:
            if not isinstance(obj[key], list):
                obj[key] = [obj[key]]
            obj[key].append(value)
        else:
            obj[key] = [value] if SETTINGS.get("explicit_array") else value

    if element.attrib and not SETTINGS.get("ignore_attrs"):
        if SETTINGS.get("merge_attrs", True):
            for name, value in element.attrib.items():
                put(name, value)
        else:
            obj[ATTRS] = dict(element.attrib)
    for child in element:
        put(tag_of(child), to_object(child))
    text = (element.text or "") + "".join(child.tail or "" for child in element)
    if SETTINGS.get("trim"):
        text = text.strip()
    if SETTINGS.get("normalize"):
        text = re.sub(r"\s{2,}", " ", text).strip()
    if not text.strip():
        text = ""
    if not obj:
        return text
    if text:
        obj[CHARS] = text
    return obj


def to_element(tag, value):
    element = ET.Element(str(tag))
    if isinstance(value, dict):
        for key, child in value.items():
            if key == ATTRS and isinstance(child, dict):
                for name, attr in child.items():
                    element.set(str(name), str(attr))
            elif key == CHARS:
                element.text = str(child)
            elif isinstance(child, list):
                element.extend(to_element(key, entry) for entry in child)
            else:
                element.append(to_element(key, child))
    elif value is not None:
        element.text = value if isinstance(value, str) else json.dumps(value)
    return element


def transform(item):
    prop = SETTINGS.get("property") or "data"
    if SETTINGS.get("mode") == "jsonToxml":
        body = dict((k, v) for k, v in item.items())
        if len(body) == 1 and isinstance(next(iter(body.values())), (dict, list)):
            root, value = next(iter(body.items()))
        else:
            root, value = SETTINGS.get("root") or "root", body
        text = ET.tostring(to_element(root, value), encoding="unicode")
        if not SETTINGS.get("headless"):
            text = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n' + text
        return dict([(prop, text)])
    source = get_path(item, prop)
    element = parse_xml(source if isinstance(source, str) else str(source or ""))
    value = to_object(element)
    return dict([(tag_of(element), value)]) if SETTINGS.get("explicit_root", True) else value


each(transform)
"""

EXTRACT_TEXT = r"""
import csv
import io
import re


def ics_events(text):
    events, current = [], None
    for line in re.sub(r"\r?\n[ \t]", "", text).splitlines():
        if line == "BEGIN:VEVENT":
            current = dict()
        elif line == "END:VEVENT" and current is not None:
            events.append(current)
            current = None
        elif current is not None and ":" in line:
            name, value = line.split(":", 1)
            current[name.split(";", 1)[0].lower()] = value
    return events


operation = SETTINGS.get("operation") or "csv"
raw = inputs.get("data")
output = SETTINGS.get("output") or "data"
# n8n writes what it read under the destination field, one item.
out = dict()
if operation == "json":
    text = to_text(raw)
    set_path(out, output, json.loads(text) if text.strip() else dict())
    emit(out)
elif operation == "csv":
    text = to_text(raw)
    rows = list(csv.reader(io.StringIO(text), delimiter=SETTINGS.get("delimiter") or ","))
    if SETTINGS.get("header", True) and rows:
        header = rows[0]
        emit([dict(zip(header, row)) for row in rows[1:]])
    else:
        emit([dict(("row" + str(i), v) for i, v in enumerate(row)) for row in rows])
elif operation == "fromIcs":
    set_path(out, output, ics_events(to_text(raw)))
    emit(out)
else:
    set_path(out, output, to_text(raw))
    emit(out)
"""

CONVERT_TEXT = r"""
import csv
import io

items = as_items(inputs.get("items"))
operation = SETTINGS.get("operation") or "csv"
if operation == "csv":
    rows = [i for i in items if isinstance(i, dict)]
    columns = []
    for row in rows:
        columns.extend(k for k in row if k not in columns)
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=SETTINGS.get("delimiter") or ",", lineterminator="\n")
    if SETTINGS.get("header", True):
        writer.writerow(columns)
    for row in rows:
        writer.writerow([v if isinstance(v, str) else json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else ("" if v is None else v) for v in (row.get(c) for c in columns)])
    text = buffer.getvalue()
elif operation in ("toJson", "json"):
    value = items if SETTINGS.get("all_items", True) or len(items) != 1 else items[0]
    text = json.dumps(value, ensure_ascii=False, indent=2) if SETTINGS.get("pretty") else to_text(value)
else:
    prop = SETTINGS.get("source") or "data"
    text = "\n".join(str(get_path(i, prop) if isinstance(i, dict) else i) for i in items)
out = dict([(SETTINGS.get("output") or "data", text)])
if SETTINGS.get("file_name"):
    out["file_name"] = SETTINGS["file_name"]
emit(out)
"""

MOVE_DATA = r"""
def transform(item):
    if SETTINGS.get("mode") == "jsonToBinary":
        key = SETTINGS.get("source")
        value = get_path(item, key) if key else item
        # n8n's JSON.stringify: compact, a text quoted unless kept raw.
        text = value if isinstance(value, str) and SETTINGS.get("raw") else json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        return dict([(SETTINGS.get("output") or "data", text)])
    source = get_path(item, SETTINGS.get("source") or "data")
    value = load(source)
    if SETTINGS.get("all", True) and isinstance(value, dict):
        return value
    out = copy(item)
    set_path(out, SETTINGS.get("output") or "data", value)
    return out


each(transform)
"""

COMPARE_DATASETS = r"""
first, second = as_items(inputs.get("input_a")), as_items(inputs.get("input_b"))
pairs = SETTINGS.get("fields") or []
skip = set(SETTINGS.get("skip") or [])


def key(item, side):
    return json.dumps([get_path(item, pair[side]) for pair in pairs], default=str)


def compared(item):
    return dict((k, v) for k, v in item.items() if k not in skip) if isinstance(item, dict) else item


index = dict()
for item in second:
    index.setdefault(key(item, 1), []).append(item)
result = dict(in_a_only=[], same=[], different=[], in_b_only=[])
matched = set()
for item in first:
    k = key(item, 0)
    if k in index:
        matched.add(k)
        other = index[k][0]
        if compared(item) == compared(other):
            result["same"].append(item)
        else:
            result["different"].append(dict(input_a=item, input_b=other))
    else:
        result["in_a_only"].append(item)
result["in_b_only"] = [item for item in second if key(item, 1) not in matched]
emit(result)
"""

TOTP = r"""
import base64
import hashlib
import hmac
import struct
import time

secret = str(inputs.get("secret") or "").replace(" ", "").upper()
if not secret:
    raise ValueError("No TOTP secret: store it as a secret variable and reference it in the `secret` input.")
key = base64.b32decode(secret + "=" * (-len(secret) % 8))
period = int(SETTINGS.get("period") or 30)
digits = int(SETTINGS.get("digits") or 6)
counter = int(time.time() // period)
digest = hmac.new(key, struct.pack(">Q", counter), (SETTINGS.get("algorithm") or "sha1").lower()).digest()
offset = digest[-1] & 15
code = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % 10 ** digits
emit(dict(token=str(code).zfill(digits), secondsRemaining=period - int(time.time()) % period))
"""

JAVASCRIPT = r"""# The n8n node ran the JavaScript below; this platform runs Python.
# Rewrite it: the upstream output is in inputs["json"], and whatever this
# script prints (JSON for an object) becomes the node's output.
#
{javascript}
raise NotImplementedError("Rewrite the n8n JavaScript above in Python.")
"""


def script(body: str) -> str:
    """A complete script: the shared prelude, then *body*."""
    return PRELUDE + body


def javascript(code: str) -> str:
    """The JavaScript of an n8n Code / Function node, kept as comments."""
    commented = "\n".join("# " + line if line.strip() else "#" for line in code.splitlines())
    return JAVASCRIPT.replace("{javascript}", commented or "#")


# Every script the importer can write, for the tests that run them.
SCRIPTS: dict[str, str] = {
    "aggregate": AGGREGATE,
    "limit": LIMIT,
    "sort": SORT,
    "remove_duplicates": REMOVE_DUPLICATES,
    "summarize": SUMMARIZE,
    "rename_keys": RENAME_KEYS,
    "date_time": DATE_TIME,
    "crypto": CRYPTO,
    "html_extract": HTML_DOM + HTML_EXTRACT,
    "html_template": HTML_TEMPLATE,
    "html_table": HTML_TABLE,
    "markdown": MARKDOWN,
    "xml": XML,
    "extract_text": EXTRACT_TEXT,
    "convert_text": CONVERT_TEXT,
    "move_data": MOVE_DATA,
    "compare_datasets": COMPARE_DATASETS,
    "totp": TOTP,
}
