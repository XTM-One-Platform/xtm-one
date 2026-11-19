"""SQL on a lab's PostgreSQL from inside the lab: [check.lab-sql] (#327).

The stores' psql lives in the container of the service that started them, never in the workspace container a
service_id check runs in, so .xtm-one/lab/lab_sql.py speaks the PostgreSQL simple query protocol itself. These pin
the declaration, the variable binding and the protocol against an in-process fake server.
"""

from __future__ import annotations

import importlib.util
import json
import socket
import struct
import threading
import tomllib

import pytest

from tests.test_lab_seed_declarations import CHECKS, LAB

SCRIPT = LAB.parent / "lab_sql.py"
STORES = LAB.parent.parent / "stores.py"

pytestmark = pytest.mark.skipif(not CHECKS.is_file() or not SCRIPT.is_file(), reason="no .xtm-one next to the backend")


def _module():
    spec = importlib.util.spec_from_file_location("lab_sql", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _message(kind: bytes, payload: bytes) -> bytes:
    return kind + struct.pack("!I", len(payload) + 4) + payload


def _row_description(*names: str) -> bytes:
    body = struct.pack("!H", len(names))
    for name in names:
        body += name.encode() + b"\0" + struct.pack("!IhIhih", 0, 0, 25, -1, -1, 0)
    return _message(b"T", body)


def _data_row(*values: str | None) -> bytes:
    body = struct.pack("!H", len(values))
    for value in values:
        if value is None:
            body += struct.pack("!i", -1)
        else:
            body += struct.pack("!i", len(value.encode())) + value.encode()
    return _message(b"D", body)


READY = _message(b"Z", b"I")


class FakeServer:
    """Answers one connection: trust (or MD5) authentication, then each Query by the answer *respond* builds."""

    def __init__(self, respond, auth: str = "trust"):
        self.respond = respond
        self.auth = auth
        self.queries: list[str] = []
        self.startup: dict[str, str] = {}
        self.password = b""
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _read(self, conn, size):
        data = b""
        while len(data) < size:
            chunk = conn.recv(size - len(data))
            if not chunk:
                raise ConnectionError
            data += chunk
        return data

    def _serve(self):
        conn, _ = self.listener.accept()
        with conn:
            (length,) = struct.unpack("!I", self._read(conn, 4))
            body = self._read(conn, length - 4)
            parts = body[4:].split(b"\0")
            self.startup = {parts[i].decode(): parts[i + 1].decode() for i in range(0, len(parts) - 2, 2) if parts[i]}
            if self.auth == "md5":
                conn.sendall(_message(b"R", struct.pack("!I", 5) + b"salt"))
                kind = self._read(conn, 1)
                (length,) = struct.unpack("!I", self._read(conn, 4))
                assert kind == b"p"
                self.password = self._read(conn, length - 4)
            conn.sendall(_message(b"R", struct.pack("!I", 0)) + _message(b"S", b"server_version\x0016\x00") + READY)
            while True:
                try:
                    kind = self._read(conn, 1)
                except ConnectionError:
                    return
                (length,) = struct.unpack("!I", self._read(conn, 4))
                payload = self._read(conn, length - 4)
                if kind == b"X":
                    return
                sql = payload.rstrip(b"\0").decode()
                self.queries.append(sql)
                conn.sendall(self.respond(sql) + READY)


def test_the_check_is_declared_and_runs_the_script():
    checks = tomllib.loads(CHECKS.read_text(encoding="utf-8"))["check"]
    check = checks["lab-sql"]
    assert check["argv"] == ["python", ".xtm-one/lab/lab_sql.py"]
    assert check["default_args"] == ["--list"]
    assert "service_id" in check["description"]


def test_the_port_and_user_are_the_stores():
    module = _module()
    stores = STORES.read_text(encoding="utf-8")
    assert f"PG_PORT, REDIS_PORT, S3_PORT = {module.PG_PORT}," in stores
    assert f'"-U", "{module.USER}", "-A", "trust"' in stores


def test_variables_are_quoted_never_spliced():
    module = _module()
    sql = module.bind("SELECT * FROM :\"table\" WHERE name = :'name'", {"table": 'as"sets', "name": "o'brien"})
    assert sql == "SELECT * FROM \"as\"\"sets\" WHERE name = 'o''brien'"
    assert module.bind("SELECT :'v'", {"v": "a\\b"}) == "SELECT E'a\\\\b'"
    assert module.bind("SELECT '::text', 1::int", {}) == "SELECT '::text', 1::int"
    with pytest.raises(SystemExit, match="--var missing"):
        module.bind("SELECT :'missing'", {})


@pytest.mark.parametrize(
    "sql",
    [
        """INSERT INTO t (doc) VALUES ('{"role":"admin"}')""",
        """SELECT E'it\\'s :"x"', 'a'':''b'""",
        """SELECT "col:'x'" FROM t""",
        """SELECT 1 -- :'x'\n""",
        """SELECT /* :'x' /* :"y" */ */ 1""",
        """SELECT $$ :'x' $$, $body$ {"k":"v"} $body$""",
        """SELECT 'a'::"char", 1::int""",
    ],
)
def test_quoted_spans_comments_and_casts_are_never_bound(sql):
    # psql interpolates outside quotes only: a JSON literal or a comment is left as written, no --var asked for.
    assert _module().bind(sql, {"x": "X", "y": "Y", "admin": "Z", "k": "K"}) == sql
    assert _module().bind(sql, {}) == sql


def test_variables_next_to_quoted_spans_are_bound():
    module = _module()
    sql = """INSERT INTO :"t" (doc, name) VALUES ('{"role":"admin"}', :'name') -- :'name'"""
    bound = module.bind(sql, {"t": "assets", "name": "ann"})
    assert bound == """INSERT INTO "assets" (doc, name) VALUES ('{"role":"admin"}', 'ann') -- :'name'"""
    assert module.bind("SELECT $1, :'v'", {"v": "w"}) == "SELECT $1, 'w'"


def test_a_query_answers_every_result_set_as_json(capsys):
    def respond(sql):
        return (
            _row_description("asset_name", "asset_hostname")
            + _data_row("lab-web-01", None)
            + _data_row("lab-dc-01", "lab-dc-01.lab.local")
            + _message(b"C", b"SELECT 2\0")
            + _message(b"C", b"INSERT 0 1\0")
        )

    server = FakeServer(respond)
    module = _module()
    code = module.main(
        [
            "--port",
            str(server.port),
            "--database",
            "openaev",
            "--sql",
            "SELECT asset_name, asset_hostname FROM assets; INSERT INTO t VALUES (:'v')",
            "--var",
            "v=x",
        ]
    )
    out = json.loads(capsys.readouterr().out)
    assert code == 0
    assert server.startup == {"user": "copilot", "database": "openaev"}
    assert server.queries == ["SELECT asset_name, asset_hostname FROM assets; INSERT INTO t VALUES ('x')"]
    assert out == [
        {
            "command": "SELECT 2",
            "columns": ["asset_name", "asset_hostname"],
            "rows": [["lab-web-01", None], ["lab-dc-01", "lab-dc-01.lab.local"]],
        },
        {"command": "INSERT 0 1", "columns": [], "rows": []},
    ]


def test_a_sql_error_exits_1_with_the_server_message(capsys):
    def respond(sql):
        return _message(b"E", b'SERROR\0C42601\0Msyntax error at or near "SELEC"\0\0')

    server = FakeServer(respond)
    code = _module().main(["--port", str(server.port), "--database", "openaev", "--sql", "SELEC 1"])
    captured = capsys.readouterr()
    assert code == 1
    assert json.loads(captured.out)["error"]["C"] == "42601"
    assert "42601: syntax error" in captured.err


def test_md5_authentication_answers_the_salted_digest(capsys):
    server = FakeServer(
        lambda sql: _row_description("datname") + _data_row("openaev") + _message(b"C", b"SELECT 1\0"), auth="md5"
    )
    code = _module().main(["--port", str(server.port), "--list"])
    assert code == 0
    assert json.loads(capsys.readouterr().out)[0]["rows"] == [["openaev"]]
    assert server.password.startswith(b"md5") and len(server.password) == 3 + 32 + 1


def test_no_database_exits_2(capsys):
    # 127.0.0.1:2: a refused connection, never a freed ephemeral port another test may take.
    code = _module().main(["--port", "2", "--timeout", "2", "--list"])
    assert code == 2
    assert "does not answer" in capsys.readouterr().err


def test_an_unbound_variable_is_a_usage_error(capsys):
    assert _module().main(["--sql", "SELECT :'x'"]) == 3
    assert "--var x" in capsys.readouterr().err
