import pytest

from app.tools.mcp_client import _resolve_stdio_command


def test_resolve_stdio_command_keeps_existing_command(monkeypatch):
    monkeypatch.setattr("app.tools.mcp_client.shutil.which", lambda cmd: "/usr/bin/node")

    command, args = _resolve_stdio_command("node", ["server.js"])

    assert command == "node"
    assert args == ["server.js"]


def test_resolve_stdio_command_trims_command_and_args(monkeypatch):
    seen_commands: list[str] = []

    def _which(cmd: str) -> str | None:
        seen_commands.append(cmd)
        if cmd == "uvx":
            return "/usr/bin/uvx"
        return None

    monkeypatch.setattr("app.tools.mcp_client.shutil.which", _which)

    command, args = _resolve_stdio_command(" uvx ", [" --from ", " acme-mcp ", " metabase "])

    assert seen_commands == ["uvx"]
    assert command == "uvx"
    assert args == ["--from", "acme-mcp", "metabase"]


def test_resolve_stdio_command_drops_blank_args(monkeypatch):
    """Args that are empty or whitespace-only after trimming must be
    dropped entirely — passing empty argv tokens to the subprocess can
    break servers that treat ``""`` as a positional argument.
    """
    monkeypatch.setattr(
        "app.tools.mcp_client.shutil.which",
        lambda cmd: "/usr/bin/uvx" if cmd == "uvx" else None,
    )

    command, args = _resolve_stdio_command("uvx", ["", "  ", "--from", " acme-mcp "])

    assert command == "uvx"
    assert args == ["--from", "acme-mcp"]


@pytest.mark.parametrize("raw_command", ["", "   ", "\t"])
def test_resolve_stdio_command_rejects_blank_command(monkeypatch, raw_command):
    """A command that is empty or whitespace-only after trimming must be
    rejected with a clear config error, not a PATH-based
    ``FileNotFoundError`` for the confusing empty string ``''``.
    """
    monkeypatch.setattr("app.tools.mcp_client.shutil.which", lambda _: None)

    with pytest.raises(ValueError, match="empty or whitespace-only"):
        _resolve_stdio_command(raw_command, ["--flag"])


@pytest.mark.parametrize("raw_command", [None, 42, ["uvx"]])
def test_resolve_stdio_command_rejects_non_string_command(monkeypatch, raw_command):
    """Imported configs are untyped JSON, so the command may not be a
    string at all — reject with a clear config error instead of an
    ``AttributeError`` on ``.strip()`` during connect.
    """
    monkeypatch.setattr("app.tools.mcp_client.shutil.which", lambda _: None)

    with pytest.raises(ValueError, match="must be a string"):
        _resolve_stdio_command(raw_command, ["--flag"])


def test_resolve_stdio_command_coerces_non_string_args(monkeypatch):
    """Non-string args from imported JSON (e.g. a bare port number) are
    coerced to strings — argv tokens must be strings before reaching the
    subprocess, and coercion beats crashing with ``AttributeError``.
    """
    monkeypatch.setattr(
        "app.tools.mcp_client.shutil.which",
        lambda cmd: "/usr/bin/server" if cmd == "server" else None,
    )

    command, args = _resolve_stdio_command("server", ["--port", 8080])

    assert command == "server"
    assert args == ["--port", "8080"]


def test_resolve_stdio_command_falls_back_from_uvx_to_uv(monkeypatch):
    def _which(cmd: str) -> str | None:
        if cmd == "uvx":
            return None
        if cmd == "uv":
            return "/usr/bin/uv"
        return None

    monkeypatch.setattr("app.tools.mcp_client.shutil.which", _which)

    command, args = _resolve_stdio_command("uvx", ["--from", "acme-mcp", "metabase"])

    assert command == "uv"
    assert args == ["tool", "run", "--from", "acme-mcp", "metabase"]


def test_resolve_stdio_command_trims_before_uvx_fallback(monkeypatch):
    def _which(cmd: str) -> str | None:
        if cmd == "uvx":
            return None
        if cmd == "uv":
            return "/usr/bin/uv"
        return None

    monkeypatch.setattr("app.tools.mcp_client.shutil.which", _which)

    command, args = _resolve_stdio_command(" uvx ", [" --from ", " acme-mcp ", " metabase "])

    assert command == "uv"
    assert args == ["tool", "run", "--from", "acme-mcp", "metabase"]


def test_resolve_stdio_command_raises_with_path_message_for_bare_command(
    monkeypatch,
):
    """A bare command (no path separator) that's missing should produce
    the PATH-oriented message — installing it / setting ``PATH`` is
    the right fix.
    """
    monkeypatch.setattr("app.tools.mcp_client.shutil.which", lambda _: None)

    with pytest.raises(FileNotFoundError, match="missing-bin") as exc_info:
        _resolve_stdio_command("missing-bin", ["--flag"])

    msg = str(exc_info.value)
    assert "was not found in PATH" in msg, (
        f"bare-command failure must produce the PATH-oriented message; got {msg!r}"
    )
    assert "executable bit" not in msg, (
        f"bare-command failure must NOT produce the explicit-path message; got {msg!r}"
    )


def test_resolve_stdio_command_raises_with_explicit_path_message_for_absolute(
    monkeypatch,
):
    """``shutil.which`` returns ``None`` for an absolute path that
    doesn't exist or isn't executable too — the actionable fix here
    is "check the path / chmod +x", NOT "install it on PATH".
    Pin the tailored wording so a future refactor doesn't regress to
    the misleading PATH message for explicit-path configs.
    """
    monkeypatch.setattr("app.tools.mcp_client.shutil.which", lambda _: None)

    with pytest.raises(
        FileNotFoundError, match="/opt/missing/server"
    ) as exc_info:
        _resolve_stdio_command("/opt/missing/server", ["--flag"])

    msg = str(exc_info.value)
    assert "does not exist or is not executable" in msg, (
        f"explicit-path failure must produce the path-oriented message; got {msg!r}"
    )
    assert "was not found in PATH" not in msg, (
        f"explicit-path failure must NOT mention PATH; got {msg!r}"
    )


def test_resolve_stdio_command_raises_with_explicit_path_message_for_relative(
    monkeypatch,
):
    """Same tailored wording for relative paths like ``./bin/server``
    or ``bin/server`` — both contain a path separator and ``shutil.which``
    short-circuits to ``_access_check`` for them.
    """
    monkeypatch.setattr("app.tools.mcp_client.shutil.which", lambda _: None)

    with pytest.raises(
        FileNotFoundError, match=r"\./bin/server"
    ) as exc_info:
        _resolve_stdio_command("./bin/server", [])

    msg = str(exc_info.value)
    assert "does not exist or is not executable" in msg
    assert "was not found in PATH" not in msg
