"""SQL on a lab's PostgreSQL, from inside the lab (``[check.lab-sql]``).

Run with ``sandbox_run_command(check="lab-sql", service_id=<the lab>, args=[...])``: the command runs in the lab's
workspace container, next to the lab's services on the pod's ``localhost``. The stores' own ``psql`` lives in the
container of the service that started them (``.xtm-one/stores.py``, ``pginstall/bin``), not in the workspace
container, and the lab images carry no PostgreSQL driver: this script speaks the PostgreSQL simple query protocol
itself, standard library only, so a desk reads or seeds rows the API seed does not create (OpenAEV expectations
and findings for a screenshot, an OpenCRQ tenant, an XTM Hub token) without hand-writing a client.

    --database NAME     the database (openaev, opencrq, xtm_hub, the licences lab's, copilot for xtm-one's own)
    --sql TEXT          one or more statements, run as one simple query; every result set is answered
    --file PATH         the statements from a file of the lab's copy of the checkout, instead of --sql
    --var NAME=VALUE    bound where the SQL says :'NAME' (a quoted literal) or :"NAME" (a quoted identifier),
                        psql's syntax, outside string literals, quoted identifiers, comments and dollar-quoted
                        bodies only, as psql; the value is quoted, never spliced in raw; repeatable
    --list              the databases of the cluster, instead of a query

Answers JSON: a list of ``{"command", "columns", "rows"}`` (rows as lists of text, null for NULL). Exit 0, 1 on a
SQL error (its SQLSTATE and message on stderr and in the JSON), 2 when the database does not answer, 3 on a
usage error (an unbound variable). The stores trust local connections (``initdb -A trust``); a cleartext or MD5
password request is answered with ``--password`` (default the lab's ``lab``).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import socket
import struct
import sys
from pathlib import Path

#: .xtm-one/stores.py PG_PORT and its superuser (initdb -U copilot -A trust): lab-only, never a real credential.
PG_PORT = 5432
USER = "copilot"
PASSWORD = "lab"
PROTOCOL_V3 = 196608
LIST_SQL = "SELECT datname FROM pg_database WHERE NOT datistemplate ORDER BY datname"
#: psql's variable syntax: :'name' is a literal, :"name" an identifier; matched at a ':' outside quotes only.
VARIABLE = re.compile(r""":(['"])([A-Za-z_][A-Za-z0-9_]*)\1""")
#: A dollar-quote opening tag: $$ or $tag$ (a tag never starts with a digit, so $1 is a parameter, not a quote).
DOLLAR_TAG = re.compile(r"\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$")


class SqlError(Exception):
    """An ErrorResponse of the server: its fields by code (S severity, C SQLSTATE, M message, D detail, H hint)."""

    def __init__(self, fields: dict[str, str]):
        self.fields = fields
        super().__init__(f"{fields.get('C', '')}: {fields.get('M', '')}")


def quote_literal(value: str) -> str:
    """A standard-conforming string literal: quotes doubled; E'' with doubled backslashes when it holds one."""
    quoted = value.replace("'", "''")
    if "\\" in value:
        return "E'" + quoted.replace("\\", "\\\\") + "'"
    return "'" + quoted + "'"


def quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _skip_quoted(sql: str, start: int) -> int:
    """The index just past the quoted span opening at ``start``, or ``start`` when none opens there.

    The spans psql never interpolates in: '...' (and E'...' with backslash escapes), "..." identifiers, -- line
    comments, /* */ comments (nested), and $tag$ ... $tag$ dollar-quoted bodies. An unterminated span runs to the end.
    """
    char = sql[start]
    if char in "'\"":
        # E'...' takes backslash escapes; the E is a prefix only when no identifier character precedes it.
        prefix = sql[start - 2] if start >= 2 else " "
        escapes = char == "'" and start > 0 and sql[start - 1] in "eE" and not (prefix.isalnum() or prefix == "_")
        index = start + 1
        while index < len(sql):
            if escapes and sql[index] == "\\":
                index += 2
                continue
            if sql[index] == char:
                if index + 1 < len(sql) and sql[index + 1] == char:
                    index += 2
                    continue
                return index + 1
            index += 1
        return len(sql)
    if sql.startswith("--", start):
        end = sql.find("\n", start)
        return len(sql) if end < 0 else end + 1
    if sql.startswith("/*", start):
        depth, index = 1, start + 2
        while index < len(sql) and depth:
            if sql.startswith("/*", index):
                depth, index = depth + 1, index + 2
            elif sql.startswith("*/", index):
                depth, index = depth - 1, index + 2
            else:
                index += 1
        return index
    if char == "$" and not (start > 0 and (sql[start - 1].isalnum() or sql[start - 1] == "_")):
        tag = DOLLAR_TAG.match(sql, start)
        if tag:
            end = sql.find(tag.group(0), tag.end())
            return len(sql) if end < 0 else end + len(tag.group(0))
    return start


def bind(sql: str, variables: dict[str, str]) -> str:
    """Replace :'name' and :"name" by the quoted value, outside quoted spans only, as psql does.

    A ':' inside a string literal, a quoted identifier, a comment or a dollar-quoted body is left as written (a JSON
    literal such as '{"role":"admin"}' stays intact); '::' is a cast, never a variable. An unbound name is a usage
    error, never left in the SQL.
    """
    out: list[str] = []
    index = 0
    while index < len(sql):
        end = _skip_quoted(sql, index)
        if end > index:
            out.append(sql[index:end])
            index = end
            continue
        if sql.startswith("::", index):
            out.append("::")
            index += 2
            continue
        match = VARIABLE.match(sql, index) if sql[index] == ":" else None
        if match:
            quote, name = match.group(1), match.group(2)
            if name not in variables:
                raise SystemExit(f"lab-sql: the SQL uses :{quote}{name}{quote} and no --var {name}=... binds it")
            value = variables[name]
            out.append(quote_literal(value) if quote == "'" else quote_identifier(value))
            index = match.end()
            continue
        out.append(sql[index])
        index += 1
    return "".join(out)


def _message(kind: bytes, payload: bytes) -> bytes:
    return kind + struct.pack("!I", len(payload) + 4) + payload


def _fields(payload: bytes) -> dict[str, str]:
    fields: dict[str, str] = {}
    for part in payload.split(b"\0"):
        if part:
            fields[chr(part[0])] = part[1:].decode("utf-8", "replace")
    return fields


class Connection:
    """One connection, the simple query protocol (version 3.0) only."""

    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.buffer = b""

    def _read(self, size: int) -> bytes:
        while len(self.buffer) < size:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("the server closed the connection")
            self.buffer += chunk
        data, self.buffer = self.buffer[:size], self.buffer[size:]
        return data

    def receive(self) -> tuple[bytes, bytes]:
        kind = self._read(1)
        (length,) = struct.unpack("!I", self._read(4))
        return kind, self._read(length - 4)

    def startup(self, database: str, user: str, password: str) -> None:
        params = b"".join(f"{k}\0{v}\0".encode() for k, v in (("user", user), ("database", database)))
        body = struct.pack("!I", PROTOCOL_V3) + params + b"\0"
        self.sock.sendall(struct.pack("!I", len(body) + 4) + body)
        while True:
            kind, payload = self.receive()
            if kind == b"R":
                (code,) = struct.unpack("!I", payload[:4])
                if code == 3:
                    self.sock.sendall(_message(b"p", password.encode() + b"\0"))
                elif code == 5:
                    inner = hashlib.md5((password + user).encode()).hexdigest()  # noqa: S324 - the protocol's own
                    outer = hashlib.md5(inner.encode() + payload[4:8]).hexdigest()  # noqa: S324
                    self.sock.sendall(_message(b"p", b"md5" + outer.encode() + b"\0"))
                elif code != 0:
                    raise ConnectionError(f"authentication method {code} is not supported (trust, cleartext, md5)")
            elif kind == b"E":
                raise SqlError(_fields(payload))
            elif kind == b"Z":
                return

    def query(self, sql: str) -> list[dict]:
        """Run *sql*; every result set in order. An error raises SqlError once the server is ready again."""
        self.sock.sendall(_message(b"Q", sql.encode() + b"\0"))
        results: list[dict] = []
        current: dict | None = None
        error: SqlError | None = None
        while True:
            kind, payload = self.receive()
            if kind == b"T":
                (count,) = struct.unpack("!H", payload[:2])
                columns, offset = [], 2
                for _ in range(count):
                    end = payload.index(b"\0", offset)
                    columns.append(payload[offset:end].decode())
                    offset = end + 1 + 18
                current = {"command": "", "columns": columns, "rows": []}
            elif kind == b"D":
                (count,) = struct.unpack("!H", payload[:2])
                row, offset = [], 2
                for _ in range(count):
                    (size,) = struct.unpack("!i", payload[offset : offset + 4])
                    offset += 4
                    if size < 0:
                        row.append(None)
                    else:
                        row.append(payload[offset : offset + size].decode("utf-8", "replace"))
                        offset += size
                if current is not None:
                    current["rows"].append(row)
            elif kind == b"C":
                tag = payload.rstrip(b"\0").decode()
                if current is None:
                    current = {"command": tag, "columns": [], "rows": []}
                current["command"] = tag
                results.append(current)
                current = None
            elif kind == b"E":
                error = SqlError(_fields(payload))
            elif kind == b"Z":
                if error is not None:
                    error.results = results  # type: ignore[attr-defined]
                    raise error
                return results

    def close(self) -> None:
        try:
            self.sock.sendall(_message(b"X", b""))
        except OSError:
            pass
        self.sock.close()


def connect(database: str, *, host: str = "127.0.0.1", port: int = PG_PORT, user: str = USER,
            password: str = PASSWORD, timeout: float = 60.0) -> Connection:
    sock = socket.create_connection((host, port), timeout=timeout)
    connection = Connection(sock)
    try:
        connection.startup(database, user, password)
    except BaseException:
        sock.close()
        raise
    return connection


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    asked = parser.add_mutually_exclusive_group(required=True)
    asked.add_argument("--sql", metavar="TEXT")
    asked.add_argument("--file", metavar="PATH")
    asked.add_argument("--list", action="store_true")
    parser.add_argument("--database", default="postgres")
    parser.add_argument("--var", action="append", default=[], metavar="NAME=VALUE")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=PG_PORT)
    parser.add_argument("--user", default=USER)
    parser.add_argument("--password", default=PASSWORD)
    parser.add_argument("--timeout", type=float, default=60.0)
    args = parser.parse_args(argv)
    variables: dict[str, str] = {}
    for pair in args.var:
        name, sep, value = pair.partition("=")
        if not sep or not name:
            print(f"lab-sql: --var {pair!r} is not NAME=VALUE", file=sys.stderr)
            return 3
        variables[name] = value
    if args.list:
        sql = LIST_SQL
    elif args.file:
        sql = Path(args.file).read_text(encoding="utf-8")
    else:
        sql = args.sql
    try:
        sql = bind(sql, variables)
    except SystemExit as refusal:
        print(refusal, file=sys.stderr)
        return 3
    try:
        connection = connect(args.database, host=args.host, port=args.port, user=args.user,
                             password=args.password, timeout=args.timeout)
    except SqlError as error:
        print(json.dumps({"error": error.fields}, indent=2))
        print(f"lab-sql: {error}", file=sys.stderr)
        return 1
    except OSError as error:
        print(f"lab-sql: the database {args.database} on {args.host}:{args.port} does not answer: {error}", file=sys.stderr)
        return 2
    try:
        results = connection.query(sql)
    except SqlError as error:
        print(json.dumps({"results": getattr(error, "results", []), "error": error.fields}, indent=2))
        print(f"lab-sql: {error}", file=sys.stderr)
        return 1
    finally:
        connection.close()
    print(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
