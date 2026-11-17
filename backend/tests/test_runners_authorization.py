"""Unit tests for the runner authorization gate (pure, no DB).

Covers the security-critical scoping helpers that decide whether an agent's
``computer_use`` command is allowed to reach a machine:

* ``_path_allowed`` - component-aware path containment (no ``startswith`` or
  ``..`` bypass),
* ``_shell_allowed`` - shell allow-list (standing approvals): exact-string
  blessing + metacharacter-safe prefix matching,
* ``_shell_blocked`` - shell block-list (standing refusals): token-boundary
  matching anywhere in the command (no chained smuggling),
* ``authorize_command`` - the full ``permission_mode`` + capability + scoping
  gate (block-list first; allow-list is a hard scope only without approval),
* ``command_requires_approval`` - approval scoped to SHELL COMMANDS ONLY,
  with allow-listed commands pre-approved, and
* ``runner_version_below`` - the fail-open protocol-floor comparison.
"""

from types import SimpleNamespace

from app.models.runner import (
    PERMISSION_MODE_FULL_CONTROL,
    PERMISSION_MODE_RESTRICTED,
)
from app.services.runners import (
    _path_allowed,
    _shell_allowed,
    _shell_blocked,
    authorize_command,
    command_requires_approval,
    runner_version_below,
)


def _runner(**overrides):
    """A minimal Runner-shaped object for the pure authorization functions."""
    base = {
        "is_active": True,
        "platform": "linux",
        "permission_mode": PERMISSION_MODE_RESTRICTED,
        "capabilities": [],
        "allowed_shell_commands": None,
        "blocked_shell_commands": None,
        "allowed_paths": None,
        "require_approval": False,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


# ── _path_allowed ────────────────────────────────────────────────────────────


def test_path_allowed_matches_on_component_boundary():
    allow = ["/srv/audit"]
    assert _path_allowed("/srv/audit", allow, case_insensitive=False)
    assert _path_allowed("/srv/audit/report.png", allow, case_insensitive=False)


def test_path_allowed_rejects_sibling_prefix():
    # The classic startswith bypass: /srv/audit-escape must NOT match /srv/audit.
    assert not _path_allowed(
        "/srv/audit-escape", ["/srv/audit"], case_insensitive=False
    )
    assert not _path_allowed(
        "/srv/auditing", ["/srv/audit"], case_insensitive=False
    )


def test_path_allowed_rejects_traversal():
    assert not _path_allowed(
        "/srv/audit/../secret", ["/srv/audit"], case_insensitive=False
    )
    assert not _path_allowed("/srv/../etc/passwd", ["/srv"], case_insensitive=False)


def test_path_allowed_rejects_nul_byte():
    assert not _path_allowed("/srv/audit\x00", ["/srv/audit"], case_insensitive=False)


def test_path_allowed_skips_empty_allow_entries():
    # A blank / whitespace allow entry must not match every path.
    assert not _path_allowed("/anything", ["", "   ", None], case_insensitive=False)


def test_path_allowed_case_sensitivity():
    # POSIX: case matters. Windows: it does not.
    assert not _path_allowed("/srv/AUDIT", ["/srv/audit"], case_insensitive=False)
    assert _path_allowed(
        "C:/Users/AUDIT", ["c:/users/audit"], case_insensitive=True
    )


def test_path_allowed_normalizes_backslashes():
    assert _path_allowed(
        "C:\\Users\\Public\\file.txt",
        ["C:/Users/Public"],
        case_insensitive=True,
    )


def test_path_allowed_rejects_relative_target_against_absolute_root():
    # An absolute allow-list root must NOT authorize a relative target: the
    # runner resolves a relative path from its working directory, not from
    # /srv/audit, so "srv/audit/report.txt" would escape the boundary.
    assert not _path_allowed(
        "srv/audit/report.txt", ["/srv/audit"], case_insensitive=False
    )


def test_path_allowed_rejects_absolute_target_against_relative_root():
    # The mirror case: a relative allow-list root must not authorize an
    # absolute target either (roots must agree on being absolute).
    assert not _path_allowed(
        "/srv/audit/report.txt", ["srv/audit"], case_insensitive=False
    )


def test_path_allowed_matches_relative_target_against_relative_root():
    # A relative allow-list root still authorizes a relative target nested in it.
    assert _path_allowed("audit/report.txt", ["audit"], case_insensitive=False)


# ── _shell_allowed ───────────────────────────────────────────────────────────


def test_shell_allowed_token_boundary():
    allow = ["git status"]
    assert _shell_allowed("git status", allow)
    assert _shell_allowed("git status --short", allow)
    # "git status" must not authorize a different command that merely shares a
    # textual prefix.
    assert not _shell_allowed("git statusfoo", ["git status"])


def test_shell_allowed_rejects_chained_command():
    # The classic prefix bypass: an allowed "git status" must never authorize a
    # chained destructive command.
    for payload in (
        "git status; rm -rf /",
        "git status && rm -rf /",
        "git status | tee /etc/passwd",
        "git status `whoami`",
        "git status $(whoami)",
        "git status > /etc/cron.d/x",
        "git status\nrm -rf /",
    ):
        assert not _shell_allowed(payload, ["git status"]), payload


def test_shell_allowed_skips_blank_entries():
    # A whitespace-only allow entry stripped to "" must not match everything.
    assert not _shell_allowed("rm -rf /", ["   ", "", None])


def test_shell_allowed_empty_command():
    assert not _shell_allowed("", ["git"])
    assert not _shell_allowed("   ", ["git"])


def test_shell_allowed_exact_match_blesses_metacharacter_command():
    # "Approve & always allow" stores the exact command string. That exact
    # string must match even when it contains metacharacters - the operator
    # saw and approved precisely this command...
    entry = "docker ps -a | grep xtm"
    assert _shell_allowed("docker ps -a | grep xtm", [entry])
    assert _shell_allowed("Docker PS -a | grep xtm  ", [entry])
    # ...but exact means exact: it must never act as a PREFIX for a longer
    # chained command (only metacharacter-free commands get prefix semantics).
    assert not _shell_allowed("docker ps -a | grep xtm; rm -rf /", [entry])


# ── _shell_blocked ───────────────────────────────────────────────────────────


def test_shell_blocked_matches_prefix_and_exact():
    blocked = ["shutdown"]
    assert _shell_blocked("shutdown /s /t 0", blocked)
    assert _shell_blocked("shutdown", blocked)
    assert _shell_blocked("SHUTDOWN", blocked)


def test_shell_blocked_matches_anywhere_not_just_prefix():
    # A block-list that only checked the head of the command would be smuggled
    # past with an innocuous first token.
    blocked = ["shutdown"]
    assert _shell_blocked("echo ok; shutdown /s", blocked)
    assert _shell_blocked("echo ok && shutdown /s", blocked)
    assert _shell_blocked("echo `shutdown`", blocked)


def test_shell_blocked_requires_token_boundary():
    # Blocking "shutdown" must not refuse an unrelated command that merely
    # contains the substring.
    blocked = ["shutdown"]
    assert not _shell_blocked("cat myshutdown-report.txt", blocked)
    assert not _shell_blocked("echo shutdownish", blocked)


def test_shell_blocked_multi_word_phrase():
    blocked = ["rm -rf"]
    assert _shell_blocked("rm -rf /tmp/x", blocked)
    assert _shell_blocked("cd /tmp && rm -rf .", blocked)
    assert not _shell_blocked("rm -r /tmp/x", blocked)


def test_shell_blocked_skips_blank_entries_and_empty_command():
    assert not _shell_blocked("anything at all", ["", "   ", None])
    assert not _shell_blocked("", ["shutdown"])
    assert not _shell_blocked("   ", ["shutdown"])


# ── authorize_command ────────────────────────────────────────────────────────


def test_authorize_revoked_runner():
    runner = _runner(is_active=False)
    assert authorize_command(runner, "screenshot", {}) is not None


def test_authorize_unknown_kind():
    assert authorize_command(_runner(), "frobnicate", {}) is not None


def test_authorize_full_control_bypasses_scoping():
    runner = _runner(
        permission_mode=PERMISSION_MODE_FULL_CONTROL,
        capabilities=[],
        allowed_shell_commands=["git status"],
        allowed_paths=["/srv/audit"],
    )
    # full_control ignores capability + scoping entirely (the deliberate opt-in).
    assert authorize_command(runner, "run_shell", {"command": "rm -rf /"}) is None
    assert authorize_command(runner, "write_file", {"path": "/etc/passwd"}) is None


def test_authorize_capability_required():
    runner = _runner(capabilities=["screenshot"])
    assert authorize_command(runner, "screenshot", {}) is None
    # run_shell needs the run_shell capability, which is not granted.
    assert authorize_command(runner, "run_shell", {"command": "ls"}) is not None


def test_authorize_shell_allow_list_hard_scope_without_approval():
    # With require_approval OFF, the allow-list keeps its least-privilege
    # meaning: only listed commands run at all (the unattended MDM lockdown).
    runner = _runner(
        capabilities=["run_shell"], allowed_shell_commands=["git status"]
    )
    assert authorize_command(runner, "run_shell", {"command": "git status"}) is None
    assert (
        authorize_command(runner, "run_shell", {"command": "git status; rm -rf /"})
        is not None
    )


def test_authorize_shell_allow_list_not_hard_scope_with_approval():
    # With require_approval ON, a non-listed command is NOT refused outright -
    # it passes authorization and lands in the approval queue instead (where
    # the operator can approve it once or remember it into the allow-list).
    runner = _runner(
        capabilities=["run_shell"],
        allowed_shell_commands=["git status"],
        require_approval=True,
    )
    assert authorize_command(runner, "run_shell", {"command": "npm test"}) is None
    assert command_requires_approval(runner, "run_shell", {"command": "npm test"})


def test_authorize_shell_block_list_refused_outright():
    runner = _runner(
        capabilities=["run_shell"], blocked_shell_commands=["shutdown"]
    )
    refusal = authorize_command(runner, "run_shell", {"command": "shutdown /s"})
    assert refusal is not None and "blocked" in refusal


def test_authorize_block_list_wins_over_allow_list_and_approval():
    # A standing refusal beats a standing approval AND the approval queue:
    # the command must never even be offered for approval.
    runner = _runner(
        capabilities=["run_shell"],
        allowed_shell_commands=["shutdown /s"],
        blocked_shell_commands=["shutdown"],
        require_approval=True,
    )
    assert authorize_command(runner, "run_shell", {"command": "shutdown /s"}) is not None


def test_authorize_block_list_catches_chained_smuggling():
    runner = _runner(
        capabilities=["run_shell"],
        blocked_shell_commands=["shutdown"],
        require_approval=True,
    )
    assert (
        authorize_command(runner, "run_shell", {"command": "echo hi; shutdown /s"})
        is not None
    )


def test_authorize_path_allow_list_enforced():
    runner = _runner(capabilities=["file_ops"], allowed_paths=["/srv/audit"])
    assert (
        authorize_command(runner, "read_file", {"path": "/srv/audit/x"}) is None
    )
    assert (
        authorize_command(runner, "read_file", {"path": "/srv/audit-escape/x"})
        is not None
    )


def test_authorize_host_facts_needs_no_capability():
    # host_facts carries no side effect and requires no capability grant.
    assert authorize_command(_runner(capabilities=[]), "host_facts", {}) is None


# ── command_requires_approval (shell-only HITL gate) ─────────────────────────


def test_approval_gates_shell_only():
    # Approval is scoped to run_shell: every other capability is an
    # enable-once toggle and must never queue for approval.
    runner = _runner(
        capabilities=[
            "screenshot",
            "computer_action",
            "run_shell",
            "file_ops",
            "browser_control",
        ],
        require_approval=True,
    )
    assert command_requires_approval(runner, "run_shell", {"command": "ls"})
    for kind in (
        "screenshot",
        "computer_action",
        "write_file",
        "read_file",
        "list_dir",
        "browser_control",
        "host_facts",
    ):
        assert not command_requires_approval(runner, kind, {}), kind


def test_approval_skipped_for_allow_listed_command():
    # An allow-list entry is a STANDING approval - exactly what
    # "approve & always allow" writes - so a matching command skips the queue.
    runner = _runner(
        capabilities=["run_shell"],
        allowed_shell_commands=["git status"],
        require_approval=True,
    )
    assert not command_requires_approval(
        runner, "run_shell", {"command": "git status --short"}
    )
    assert command_requires_approval(runner, "run_shell", {"command": "npm test"})


def test_approval_off_or_full_control_never_queues():
    assert not command_requires_approval(
        _runner(capabilities=["run_shell"]), "run_shell", {"command": "ls"}
    )
    assert not command_requires_approval(
        _runner(
            permission_mode=PERMISSION_MODE_FULL_CONTROL, require_approval=True
        ),
        "run_shell",
        {"command": "ls"},
    )


def test_approval_handles_missing_payload():
    runner = _runner(capabilities=["run_shell"], require_approval=True)
    assert command_requires_approval(runner, "run_shell", None)
    assert command_requires_approval(runner, "run_shell", {})


# ── runner_version_below (protocol floor, fail-open) ─────────────────────────


def test_version_below_true_when_older():
    assert runner_version_below("1.250101.1", "1.250101.2")
    assert runner_version_below("1.240101.9", "1.250101.0")


def test_version_below_false_when_equal_or_newer():
    assert not runner_version_below("1.250101.2", "1.250101.2")
    assert not runner_version_below("2.0.0", "1.999999.9")


def test_version_below_orders_multi_digit_seq():
    # Rolling/main runner builds use the CI run number as the SEQ component
    # (1.YYMMDD.<run_number>), so SEQ routinely goes multi-digit. The floor
    # comparison must order it numerically, not lexically: 2 < 10, never "10" <
    # "2". This is what makes an installed runner self-update between same-day
    # main builds.
    assert runner_version_below("1.260816.2", "1.260816.10")
    assert not runner_version_below("1.260816.10", "1.260816.2")


def test_version_below_fail_open_on_unparseable_or_empty():
    # No floor, or a version we cannot parse on either side, never locks a
    # runner out (enrollment auth is the real gate).
    assert not runner_version_below(None, "1.0.0")
    assert not runner_version_below("1.0.0", "")
    assert not runner_version_below("dev", "1.0.0")
    assert not runner_version_below("1.0.0", "latest")
