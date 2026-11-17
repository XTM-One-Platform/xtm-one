"""What "Always allow" / "Always block" save on a runner (pure, no DB).

When a shell command waits for approval, the two standing decisions write the
exact command into the runner's allow-list or block-list, so the same command
never asks again. A command over 500 characters used to be approved or
rejected once and silently not saved: the response said ``remembered: false``,
nothing on screen did, and the command waited for approval again the next
time. It is now saved whole, whatever its length - exact equality is the
first match shape, so a whole entry is never wider than what was decided.
"""

from types import SimpleNamespace

from app.api.v1.runners import _remember_shell_decision
from app.models.runner import PERMISSION_MODE_RESTRICTED
from app.services.runners import authorize_command, command_requires_approval

# A one-line script far past the old 500-character cut, with shell
# metacharacters: only an exact allow-list entry can bless it.
LONG_PIPELINE = "cat build.log | grep -E '" + "|".join(f"error{i}" for i in range(400)) + "' | sort | uniq -c"


def _runner(**overrides):
    base = {
        "is_active": True,
        "platform": "linux",
        "permission_mode": PERMISSION_MODE_RESTRICTED,
        "capabilities": ["run_shell"],
        "allowed_shell_commands": None,
        "blocked_shell_commands": None,
        "allowed_paths": None,
        "require_approval": True,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _held(command: str, kind: str = "run_shell"):
    return SimpleNamespace(kind=kind, payload={"command": command})


def test_always_allow_saves_a_long_command_whole_and_it_no_longer_waits():
    assert len(LONG_PIPELINE) > 2000
    runner = _runner()
    cmd = _held(LONG_PIPELINE)
    assert command_requires_approval(runner, "run_shell", cmd.payload)

    assert _remember_shell_decision(runner, cmd, "allowed_shell_commands") is True

    assert runner.allowed_shell_commands == [LONG_PIPELINE]
    assert not command_requires_approval(runner, "run_shell", cmd.payload)
    assert authorize_command(runner, "run_shell", cmd.payload) is None


def test_always_block_saves_a_long_command_whole_and_it_is_refused_from_then_on():
    runner = _runner()
    cmd = _held(LONG_PIPELINE)

    assert _remember_shell_decision(runner, cmd, "blocked_shell_commands") is True

    assert runner.blocked_shell_commands == [LONG_PIPELINE]
    refusal = authorize_command(runner, "run_shell", cmd.payload)
    assert refusal is not None and "blocked-commands" in refusal


def test_a_whole_entry_is_no_wider_than_the_command_decided_on():
    runner = _runner()
    assert _remember_shell_decision(runner, _held(LONG_PIPELINE), "allowed_shell_commands")
    # A metacharacter command extended past the saved one still waits, and so
    # does a shortened one: only the command decided on is saved.
    assert command_requires_approval(runner, "run_shell", {"command": LONG_PIPELINE + " | head"})
    assert command_requires_approval(runner, "run_shell", {"command": LONG_PIPELINE[:500]})


def test_a_command_already_saved_is_not_added_twice():
    runner = _runner(allowed_shell_commands=["git status"])
    assert _remember_shell_decision(runner, _held("  GIT STATUS "), "allowed_shell_commands") is True
    assert runner.allowed_shell_commands == ["git status"]


def test_nothing_is_saved_for_a_blank_command_or_another_kind():
    runner = _runner(allowed_shell_commands=["ls"])
    assert _remember_shell_decision(runner, _held("   "), "allowed_shell_commands") is False
    assert _remember_shell_decision(runner, _held("ls -la", kind="read_file"), "allowed_shell_commands") is False
    assert runner.allowed_shell_commands == ["ls"]
