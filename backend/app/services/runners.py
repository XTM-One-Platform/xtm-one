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

"""Runner service – credentials, liveness, authorization, serialization.

Single source of truth for everything the API router, the transport layer and
the agent tools all need to agree on about a :class:`~app.models.runner.Runner`:

* credential generation + hashing (mirrors :mod:`app.api.v1.api_keys`),
* the authoritative online/offline signal (``last_seen_at`` recency — no shared
  in-process registry, so it is correct across every replica),
* the per-runner authorization gate every command passes through
  (``permission_mode`` + capability allow-list + shell/path scoping), and
* JSON serialization for the REST surface.

Deliberately does **not** import :mod:`app.api.deps` — ``deps`` imports the
resolver from here, so the dependency only ever points one way.
"""

from __future__ import annotations

import re
import secrets
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import case, select

from app.core.config import settings
from app.core.redaction import defuse_secret_markers
from app.core.security import bcrypt_checkpw_async, bcrypt_hashpw_async
from app.models.runner import (
    COMMAND_KIND_CAPABILITY,
    ENROLLMENT_KEY_PREFIX,
    PERMISSION_MODE_FULL_CONTROL,
    RUNNER_CAPABILITIES,
    RUNNER_KEY_PREFIX,
    Runner,
    RunnerCommand,
    RunnerEnrollmentKey,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

# Approval is deliberately scoped to SHELL COMMANDS ONLY. Every other
# capability (screenshot, mouse/keyboard, file ops, browser control) is an
# enable-once decision: turning the capability toggle on IS the consent, and
# the command then runs without a per-operation prompt. Shell is the one
# surface where a single string can do anything the session can, so it keeps
# the human gate (allow-list = standing approval, block-list = standing
# refusal, everything else prompts when ``require_approval`` is on).
APPROVAL_GATED_KINDS: frozenset[str] = frozenset({"run_shell"})


# ── Version gating (protocol compatibility) ─────────────────────────────────


def _parse_version(value: str | None) -> tuple[int, ...] | None:
    """Parse a ``MAJOR.YYMMDD.SEQ`` (or any dotted-int) version to a tuple.

    Returns ``None`` for a missing / non-numeric version so callers can decide
    the fail-open policy (we never lock out a runner whose version we cannot
    even parse — enrollment auth is the real gate; the version floor is a
    protocol-compatibility convenience).
    """
    if not value:
        return None
    parts = value.strip().split(".")
    out: list[int] = []
    for p in parts:
        if not p.isdigit():
            return None
        out.append(int(p))
    return tuple(out) if out else None


def runner_version_below(current: str | None, minimum: str | None) -> bool:
    """True only when we can PROVE *current* is older than *minimum*.

    Fail-open by design: an empty floor, or a version on either side we cannot
    parse, returns ``False`` (allowed). This refuses genuinely-old runners
    without risking a lockout from a malformed or future version string.
    """
    if not minimum:
        return False
    cur = _parse_version(current)
    lo = _parse_version(minimum)
    if cur is None or lo is None:
        return False
    # Compare component-wise, padding the shorter with zeros.
    width = max(len(cur), len(lo))
    cur += (0,) * (width - len(cur))
    lo += (0,) * (width - len(lo))
    return cur < lo


# The first runner build that runs background shell jobs. An older runner
# ignores ``background`` and runs the command in the foreground, and rejects
# the ``shell_job`` command kind.
SHELL_JOBS_MIN_RUNNER_VERSION = "1.261103.10467"


def runner_lacks_shell_jobs(kind: str, payload: dict | None, agent_version: str | None) -> bool:
    """True when the command needs background jobs and the runner is provably too old.

    Fail-open like :func:`runner_version_below`: a runner that reports no
    version, or one that cannot be parsed (a dev build), is not refused.
    """
    needs_jobs = kind == "shell_job" or (
        kind == "run_shell" and bool((payload or {}).get("background"))
    )
    return needs_jobs and runner_version_below(agent_version, SHELL_JOBS_MIN_RUNNER_VERSION)


# ── Credentials ─────────────────────────────────────────────────────────────


def generate_runner_key() -> str:
    """Generate a durable runner credential: ``run-<48 hex>``."""
    return RUNNER_KEY_PREFIX + secrets.token_hex(24)


def generate_enrollment_key() -> str:
    """Generate an enrollment credential: ``renr-<48 hex>``."""
    return ENROLLMENT_KEY_PREFIX + secrets.token_hex(24)


def key_display_prefix(raw_key: str) -> str:
    """The stored, indexed display prefix (first 12 chars + ellipsis)."""
    return raw_key[:12] + "..."


async def hash_key(raw_key: str) -> str:
    return (await bcrypt_hashpw_async(raw_key.encode("utf-8"))).decode("utf-8")


# ── Liveness ────────────────────────────────────────────────────────────────


def is_runner_online(runner: Runner, now: datetime | None = None) -> bool:
    """Authoritative online check: active + seen within the offline window.

    Uses ``last_seen_at`` recency rather than an in-process WebSocket registry
    so the answer is identical on every replica (a runner's WS may be held by a
    different pod than the one answering this request).
    """
    if not runner.is_active or runner.last_seen_at is None:
        return False
    now = now or datetime.now(UTC)
    last_seen = runner.last_seen_at
    if last_seen.tzinfo is None:
        last_seen = last_seen.replace(tzinfo=UTC)
    delta = (now - last_seen).total_seconds()
    return delta <= settings.runner_offline_after_seconds


def runner_status(runner: Runner, now: datetime | None = None) -> str:
    """"online" | "offline" | "revoked" for display."""
    if not runner.is_active:
        return "revoked"
    return "online" if is_runner_online(runner, now) else "offline"


# The display statuses, in the order the two readings below decide them.
RUNNER_STATUS_REVOKED = "revoked"
RUNNER_STATUS_ONLINE = "online"
RUNNER_STATUS_OFFLINE = "offline"


def runner_status_expression(now: datetime | None = None):
    """The SQL twin of :func:`runner_status`, for a set query.

    The admin Objects browser lists every runner in ONE statement (a union
    with the other entity tables) and cannot call the Python rule per row
    without loading the rows first - so the same three-way reading is written
    as a ``CASE``: revoked when the credential is inactive, online when seen
    within ``runner_offline_after_seconds`` of ``now``, offline otherwise (a
    machine that never connected has a NULL ``last_seen_at``, which fails the
    comparison and lands on offline, exactly like the Python rule).

    Deliberately NOT the cached ``Runner.status`` column: that one is the
    presence cache the flow events follow, and keeps reading "online" for a
    machine that died without saying goodbye until the next presence sweep;
    ``last_seen_at`` recency is the authoritative signal on every surface (see
    :func:`is_runner_online`).
    ``tests/test_admin_objects.py`` pins the two readings to each other.
    """
    now = now or datetime.now(UTC)
    online_since = now - timedelta(seconds=settings.runner_offline_after_seconds)
    return case(
        (Runner.is_active.is_(False), RUNNER_STATUS_REVOKED),
        (Runner.last_seen_at >= online_since, RUNNER_STATUS_ONLINE),
        else_=RUNNER_STATUS_OFFLINE,
    )


# ── Authorization ───────────────────────────────────────────────────────────


def command_capability(kind: str) -> str:
    """Capability id a command kind requires ("" for host_facts)."""
    return COMMAND_KIND_CAPABILITY.get(kind, "")


def effective_runner_capabilities(runner: Runner) -> list[str]:
    """The capabilities *runner* actually grants, as :func:`authorize_command` decides.

    A ``full_control`` runner ignores its stored allow-list and grants every
    capability; reporting that stored list (often ``[]``) told agents the
    runner could do nothing while every command ran. A ``restricted`` runner
    grants its enabled known capabilities, in the canonical order.
    """
    if runner.permission_mode == PERMISSION_MODE_FULL_CONTROL:
        return list(RUNNER_CAPABILITIES)
    enabled = set(runner.capabilities or [])
    return [cap for cap in RUNNER_CAPABILITIES if cap in enabled]


# Shell metacharacters that chain, redirect, substitute, background or split a
# command. When a shell allow-list is configured, any command containing one is
# refused outright: a prefix / executable allow-list cannot constrain what
# follows a ``;`` or ``|``, so "git status; rm -rf /" must never pass on the
# strength of "git status" being allowed. Everything richer than a single plain
# command is deliberately reserved for ``full_control``.
_SHELL_METACHARACTERS: frozenset[str] = frozenset(";&|`$><\n\r")

# A leading ``C:`` / ``d:`` segment marks a Windows drive-absolute path (after
# backslashes are folded to ``/``); a leading ``/`` marks a POSIX/UNC absolute
# path. Both are recorded as "rooted" so a relative target can never satisfy an
# absolute allow-list root.
_DRIVE_RE = re.compile(r"^[a-zA-Z]:$")


def _path_components(
    path: str, *, case_insensitive: bool
) -> tuple[bool, list[str]] | None:
    """Normalize a path to ``(is_rooted, components)``, or ``None`` if unusable.

    Backslashes fold to ``/``; empty and ``.`` segments are dropped; any ``..``
    segment (or an embedded NUL) rejects the whole path so a lexical allow-list
    can never be walked out of with traversal. ``is_rooted`` records whether the
    path is absolute - a leading ``/`` (POSIX or a UNC ``\\\\host\\share``) or a
    Windows drive like ``C:`` - and is preserved so a RELATIVE target can never
    match an ABSOLUTE allow-list root: the runner resolves a relative path from
    its own working directory, not from the configured root, so authorizing
    ``srv/audit/x`` against ``/srv/audit`` would escape the boundary. Case is
    folded only on case-insensitive filesystems (Windows) so a Linux allow-list
    stays exact.
    """
    norm = (path or "").replace("\\", "/")
    if "\x00" in norm:
        return None
    components: list[str] = []
    for segment in norm.split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            return None
        components.append(segment.lower() if case_insensitive else segment)
    rooted = norm.startswith("/") or bool(
        components and _DRIVE_RE.match(components[0])
    )
    return rooted, components


def _path_allowed(path: str, allowed_paths: list, *, case_insensitive: bool) -> bool:
    """Component-aware containment against a path allow-list.

    A plain ``startswith`` is not a safe allow-list: allowing ``/srv/audit``
    would also permit ``/srv/audit-escape`` and ``/srv/audit/../secret``. The
    target must instead match an allowed root on a component boundary (equal to
    it or nested under it), the target and the root must agree on whether they
    are absolute (a relative target resolves from the runner's working
    directory, not the configured root, so it must not satisfy an absolute
    root), and any ``..`` traversal is rejected outright. This is the
    server-side lexical gate; the runner re-checks where the path really leads,
    links resolved, against the same folders (:func:`delivery_payload`).
    """
    target = _path_components(path, case_insensitive=case_insensitive)
    if target is None:
        return False
    target_rooted, target_parts = target
    for prefix in allowed_paths:
        if not isinstance(prefix, str) or not prefix.strip():
            continue
        allow = _path_components(prefix, case_insensitive=case_insensitive)
        if allow is None:
            continue
        allow_rooted, allow_parts = allow
        if not allow_parts or target_rooted != allow_rooted:
            continue
        if target_parts[: len(allow_parts)] == allow_parts:
            return True
    return False


def _shell_allowed(command: str, allowed: list) -> bool:
    """Match a command against the shell allow-list (standing approvals).

    Two match shapes, in order:

    1. **Exact string** — the whole command equals an entry (case-insensitive,
       trimmed). This is what "approve & always allow" stores, and it is the
       ONLY shape that can bless a command containing shell metacharacters:
       the operator saw and approved that exact chained/piped string, and
       exact equality cannot be extended into anything else.
    2. **Prefix on a word boundary** — for metacharacter-free commands only,
       an entry matches when the command equals it or extends it with a space
       ("git" allows "git status", never "gitfoo"). Commands carrying a
       metacharacter are excluded here because a prefix cannot constrain what
       follows a ``;`` or ``|`` — "git status; rm -rf /" must never pass on
       the strength of "git status" being allowed.

    Empty / whitespace-only entries are skipped rather than matching every
    command.
    """
    cmd = (command or "").strip()
    if not cmd:
        return False
    lowered = cmd.lower()
    entries = [
        e.strip().lower()
        for e in allowed
        if isinstance(e, str) and e.strip()
    ]
    if lowered in entries:
        return True
    if any(ch in cmd for ch in _SHELL_METACHARACTERS):
        return False
    return any(lowered.startswith(prefix + " ") for prefix in entries)


def _shell_blocked(command: str, blocked: list) -> bool:
    """Match a command against the shell block-list (standing refusals).

    A blocked entry matches when it appears as a whole token / phrase
    ANYWHERE in the command (case-insensitive), not just as a prefix —
    otherwise "echo ok; shutdown /s" would smuggle a blocked "shutdown"
    behind an innocuous head. Boundaries are whitespace, quotes and the shell
    metacharacters, so blocking "shutdown" never refuses "myshutdown-report".
    A block-list is best-effort by nature (obfuscation can evade any lexical
    filter); the approval gate remains the real control for everything not
    explicitly allowed.
    """
    cmd = (command or "").strip().lower()
    if not cmd:
        return False
    boundary = r"[\s;&|`$><()\"']"
    for entry in blocked:
        if not isinstance(entry, str):
            continue
        needle = entry.strip().lower()
        if not needle:
            continue
        pattern = (
            rf"(?:^|{boundary}){re.escape(needle)}(?=$|{boundary})"
        )
        if re.search(pattern, cmd):
            return True
    return False


def authorize_command(runner: Runner, kind: str, payload: dict | None) -> str | None:
    """Return a refusal reason, or ``None`` when the command is allowed.

    ``full_control`` runners bypass every restriction by deliberate design (the
    opt-in the user asked for — warned + audited in the UI). ``restricted``
    runners are least-privilege: the capability must be enabled, and any
    configured shell/path allow-list must match.
    """
    if not runner.is_active:
        return "This runner has been revoked."

    if kind not in COMMAND_KIND_CAPABILITY:
        return f"Unknown command kind '{kind}'."

    if runner.permission_mode == PERMISSION_MODE_FULL_CONTROL:
        return None

    cap = command_capability(kind)
    if cap and cap not in (runner.capabilities or []):
        return (
            f"The '{cap}' capability is not enabled for this runner. "
            "The user can enable it on the Desktop page (sidebar > XTM One "
            "Desktop > this machine), or switch the runner to Full control."
        )

    payload = payload or {}
    if kind == "run_shell":
        command = str(payload.get("command", ""))
        # Block-list first: a standing refusal beats everything, including an
        # allow-list entry and the approval queue (the operator already said
        # "never" — the command must not even be offered for approval).
        blocked = runner.blocked_shell_commands
        if blocked and _shell_blocked(command, blocked):
            return (
                "This shell command matches the runner's blocked-commands "
                "list."
            )
        # The allow-list is a HARD scope only when no human is in the loop:
        # with ``require_approval`` on, a non-listed command is not refused —
        # it goes to the approval queue instead (and an approved one can be
        # remembered into the allow-list). With approval off, the allow-list
        # keeps its least-privilege meaning: only listed commands run at all
        # (the unattended / MDM fleet lockdown).
        if (
            runner.allowed_shell_commands
            and not runner.require_approval
            and not _shell_allowed(command, runner.allowed_shell_commands)
        ):
            return (
                "This shell command is not in the runner's allowed-commands "
                "list."
            )

    if kind in ("read_file", "write_file", "list_dir") and runner.allowed_paths:
        path = str(payload.get("path", ""))
        # Windows paths are case-insensitive; POSIX paths are not, so an exact
        # allow-list on Linux/macOS must not be defeated by a case flip.
        case_insensitive = (runner.platform or "").lower() == "windows"
        if not _path_allowed(
            path, runner.allowed_paths, case_insensitive=case_insensitive
        ):
            return "This path is not in the runner's allowed-paths list."

    return None


FILE_COMMAND_KINDS: frozenset[str] = frozenset({"read_file", "write_file", "list_dir"})
# The payload keys the runner reads its confinement from
# (``apps/runner/src/capabilities/files.rs``).
ALLOWED_ROOTS_KEY = "allowed_roots"
STRICT_CONFINEMENT_KEY = "strict_confinement"
# The runner's own default when a read names no size (``DEFAULT_READ_LIMIT``).
RUNNER_DEFAULT_READ_BYTES = 16 * 1024 * 1024
# The largest text the runner returns inline rather than as a file
# (``INLINE_TEXT_LIMIT``): such a result never goes through the upload limit.
RUNNER_INLINE_TEXT_BYTES = 256 * 1024
# Optional: how much a read may return when it comes back as inline text,
# where ``max_bytes`` is lower. A runner that does not know it reads
# ``max_bytes``, as before.
MAX_INLINE_BYTES_KEY = "max_inline_bytes"
# In a runner's inline text result: the most text it returns inline. A runner
# that honours ``max_inline_bytes`` sends it on every inline result, reads that
# did not need the allowance included; a runner from before sends neither.
INLINE_TEXT_LIMIT_KEY = "inline_text_limit"


def delivery_payload(runner: Runner | None, kind: str, payload: dict | None) -> dict:
    """The payload a runner receives: what was asked, plus what the server decides.

    * A file command on a restricted runner with an allowed-paths list carries
      those folders as ``allowed_roots``, as configured (``_path_allowed``
      compares them unstripped). ``authorize_command`` checks the path as
      written; the runner resolves links and checks the real target against
      these roots. A path that leads outside them through a link still runs,
      as it always has, with a warning in its result, unless the
      ``runner_strict_path_confinement`` security option is on (Settings >
      Security, or its environment variable): then ``strict_confinement``
      tells the runner to refuse it. Values already in the payload are
      replaced, never trusted, and removed where no confinement applies.
    * ``strict_confinement`` rides every file command while that setting is
      on, full-control runners included: it is also what makes the runner
      refuse a file that is not a regular file (a device, a FIFO, a socket,
      as the OS reports what the command opened - never judged by its name),
      which it otherwise reads and writes with a warning.
    * ``read_file`` never asks for more than a positive
      ``runner_max_result_bytes`` (``max_bytes``): a file result past that size
      is refused at upload, so the runner cuts it there. Text up to
      ``RUNNER_INLINE_TEXT_BYTES`` comes back inline and never meets that
      limit, so where the limit is lower the runner may read that much text
      (``max_inline_bytes``, the size asked capped at the inline size) - a
      runner that ignores it reads ``max_bytes``. A size the runner would not
      read (absent, negative, not an integer) becomes its own default, under
      the same cap. A limit of zero or less keeps its meaning (no bound here,
      every non-empty file result refused at upload); the security audit
      reports it.

    Applied when the command is delivered, so a held command approved later
    carries the runner's allowed paths as they are then.
    """
    out = dict(payload or {})
    if kind not in FILE_COMMAND_KINDS:
        return out
    out.pop(ALLOWED_ROOTS_KEY, None)
    out.pop(STRICT_CONFINEMENT_KEY, None)
    out.pop(MAX_INLINE_BYTES_KEY, None)
    if (
        runner is not None
        and runner.permission_mode != PERMISSION_MODE_FULL_CONTROL
        and runner.allowed_paths
    ):
        out[ALLOWED_ROOTS_KEY] = [
            p for p in runner.allowed_paths if isinstance(p, str) and p.strip()
        ]
    from app.core.security_options import security_option

    if security_option("runner_strict_path_confinement"):
        out[STRICT_CONFINEMENT_KEY] = True
    if kind == "read_file":
        requested = out.get("max_bytes")
        if isinstance(requested, bool) or not isinstance(requested, int) or requested < 0:
            requested = RUNNER_DEFAULT_READ_BYTES
        limit = settings.runner_max_result_bytes
        if limit > 0:
            inline = min(requested, RUNNER_INLINE_TEXT_BYTES)
            requested = min(requested, limit)
            if inline > requested:
                out[MAX_INLINE_BYTES_KEY] = inline
        out["max_bytes"] = requested
    return out


def command_requires_approval(
    runner: Runner, kind: str, payload: dict | None = None
) -> bool:
    """Whether a command must be human-approved before dispatch.

    Approval gates **shell commands only** (see ``APPROVAL_GATED_KINDS``):
    every other capability is consented to once via its toggle and then runs
    without a per-operation prompt. Within shell, an allow-listed command is
    a *standing approval* and skips the queue — that is exactly what
    "approve & always allow" writes. Only in ``restricted`` mode, only when
    the runner opts into approval.
    """
    if runner.permission_mode == PERMISSION_MODE_FULL_CONTROL:
        return False
    if kind not in APPROVAL_GATED_KINDS or not runner.require_approval:
        return False
    command = str((payload or {}).get("command", ""))
    # An allow-list match is a standing approval — skip the queue.
    return not (
        runner.allowed_shell_commands
        and _shell_allowed(command, runner.allowed_shell_commands)
    )


# ── Resolver (used by app.api.deps.get_current_runner) ──────────────────────


async def resolve_runner_credential(token: str, db: AsyncSession) -> Runner | None:
    """Resolve a ``run-`` credential to its active :class:`Runner`.

    Mirrors :func:`app.api.deps._resolve_api_key`: narrow by the indexed
    display prefix, then bcrypt-compare each candidate. Returns ``None`` on any
    failure so the caller controls the error shape.
    """
    if not token or not token.startswith(RUNNER_KEY_PREFIX):
        return None
    prefix = key_display_prefix(token)
    result = await db.execute(
        select(Runner).where(
            Runner.key_prefix == prefix, Runner.is_active.is_(True)
        )
    )
    for runner in result.scalars().all():
        if await bcrypt_checkpw_async(token.encode(), runner.key_hash.encode()):
            return runner
    return None


async def resolve_enrollment_key(
    token: str, db: AsyncSession
) -> RunnerEnrollmentKey | None:
    """Resolve a ``renr-`` enrollment credential to an active, unexhausted key."""
    if not token or not token.startswith(ENROLLMENT_KEY_PREFIX):
        return None
    prefix = key_display_prefix(token)
    result = await db.execute(
        select(RunnerEnrollmentKey).where(
            RunnerEnrollmentKey.key_prefix == prefix,
            RunnerEnrollmentKey.is_active.is_(True),
        )
    )
    now = datetime.now(UTC)
    for key in result.scalars().all():
        if not await bcrypt_checkpw_async(token.encode(), key.key_hash.encode()):
            continue
        if key.expires_at is not None:
            expires = key.expires_at
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=UTC)
            if expires < now:
                return None
        if key.max_uses is not None and key.use_count >= key.max_uses:
            return None
        return key
    return None


# ── Re-enrollment (same machine converges on its existing row) ──────────────


def apply_reenrollment(
    runner: Runner,
    *,
    key_prefix: str,
    key_hash: str,
    hostname: str | None,
    host_facts: dict | None,
    agent_version: str | None,
    now: datetime,
) -> None:
    """Fold a re-enrollment of an already-enrolled machine into its row.

    Rotates the credential (the previous ``run-`` key stops working — one
    machine, one live credential) and refreshes everything the machine
    *reports* (hostname, host facts, agent version, liveness). Everything the
    *operator* configured is deliberately preserved: name, permission mode,
    capabilities, shell allow/block lists, path allow-list, approval
    requirement, labels, tags, sharing. The enrollment key's defaults are NOT
    re-applied — a reusable
    fleet key must never silently re-widen a runner an admin tightened.
    """
    runner.key_prefix = key_prefix
    runner.key_hash = key_hash
    if hostname:
        runner.hostname = hostname
    if host_facts is not None:
        runner.host_facts = host_facts
    if agent_version:
        runner.agent_version = agent_version
    runner.status = "online"
    runner.last_seen_at = now
    runner.enrolled_at = now


# ── Serialization ───────────────────────────────────────────────────────────


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def serialize_runner(
    runner: Runner,
    *,
    now: datetime | None = None,
    can_manage: bool = True,
) -> dict:
    """JSON-safe representation of a runner for the REST surface.

    Never exposes ``key_hash``. ``allowed_shell_commands`` / ``allowed_paths``
    are configuration (not secrets) and are surfaced so the owner can review
    the scope.
    """
    return {
        "id": str(runner.id),
        "user_id": str(runner.user_id) if runner.user_id else None,
        "name": runner.name,
        "platform": runner.platform,
        "hostname": runner.hostname,
        "key_prefix": runner.key_prefix,
        "permission_mode": runner.permission_mode,
        "capabilities": runner.capabilities or [],
        "allowed_shell_commands": runner.allowed_shell_commands,
        "blocked_shell_commands": runner.blocked_shell_commands,
        "allowed_paths": runner.allowed_paths,
        "require_approval": runner.require_approval,
        "labels": runner.labels or [],
        "tags": runner.tags or [],
        "host_facts": runner.host_facts,
        "agent_version": runner.agent_version,
        "status": runner_status(runner, now),
        "online": is_runner_online(runner, now),
        "last_seen_at": _iso(runner.last_seen_at),
        "transport": runner.transport,
        "is_active": runner.is_active,
        "is_company_managed": runner.is_company_managed,
        "shared_group_ids": runner.shared_group_ids,
        "shared_group_permissions": runner.shared_group_permissions,
        "shared_user_ids": runner.shared_user_ids,
        "shared_user_permissions": runner.shared_user_permissions,
        "access_restricted": runner.access_restricted,
        "command_count": runner.command_count,
        "can_manage": can_manage,
        "enrolled_at": _iso(runner.enrolled_at),
        "created_at": _iso(runner.created_at),
        "updated_at": _iso(runner.updated_at),
    }


#: In a delivered command's stored payload: a keyed digest of each secret
#: variable value its deliveries carried (``core/redaction.py``), never the
#: value. What the machine sends back is stored only while every one of them can
#: still be redacted. Neither delivered nor shown.
DELIVERED_SECRETS_KEY = "_delivered_secrets"

#: In a stopped command's stored payload: who asked to stop it from its card
#: (``{"user_id", "name"}``), copied into its result as ``stopped_by``
#: when the runner reports it stopped. Neither delivered nor shown.
STOPPED_BY_KEY = "_stopped_by"


def requested_payload(payload: dict | None) -> dict:
    """A command's stored payload as it was asked, without the platform's delivery bookkeeping."""
    out = dict(payload or {})
    out.pop(DELIVERED_SECRETS_KEY, None)
    out.pop(STOPPED_BY_KEY, None)
    return out


def serialize_command(cmd: RunnerCommand, *, include_result: bool = True) -> dict:
    """JSON-safe representation of a runner command (audit / status views).

    Without ``include_result`` the ``result`` is ``null`` and never read, so
    a caller may defer loading the column. A secret variable in the payload
    shows as ``[secret]``, never as the marker the runner's delivery restores.
    """
    return {
        "id": str(cmd.id),
        "runner_id": str(cmd.runner_id),
        "user_id": str(cmd.user_id) if cmd.user_id else None,
        "agent_id": str(cmd.agent_id) if cmd.agent_id else None,
        "conversation_id": str(cmd.conversation_id) if cmd.conversation_id else None,
        "assignment_run_id": (
            str(cmd.assignment_run_id) if cmd.assignment_run_id else None
        ),
        "kind": cmd.kind,
        "payload": defuse_secret_markers(requested_payload(cmd.payload)),
        "status": cmd.status,
        "result": cmd.result if include_result else None,
        "result_file_id": str(cmd.result_file_id) if cmd.result_file_id else None,
        "error": cmd.error,
        "created_at": _iso(cmd.created_at),
        "dispatched_at": _iso(cmd.dispatched_at),
        "completed_at": _iso(cmd.completed_at),
    }


def serialize_enrollment_key(key: RunnerEnrollmentKey) -> dict:
    """JSON-safe representation of an enrollment key (never the raw secret)."""
    return {
        "id": str(key.id),
        "name": key.name,
        "key_prefix": key.key_prefix,
        "max_uses": key.max_uses,
        "use_count": key.use_count,
        "default_labels": key.default_labels or [],
        "default_capabilities": key.default_capabilities or [],
        "default_permission_mode": key.default_permission_mode,
        "default_require_approval": bool(key.default_require_approval),
        "enroll_as_company_managed": key.enroll_as_company_managed,
        "is_active": key.is_active,
        "expires_at": _iso(key.expires_at),
        "last_used_at": _iso(key.last_used_at),
        "created_at": _iso(key.created_at),
    }
