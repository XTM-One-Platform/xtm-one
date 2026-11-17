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

"""Shared Python sandbox security utilities.

Both ``code_interpreter._SANDBOX_WRAPPER`` and
``custom_tool_executor._build_python_sandbox_header`` embed constants from this
module as source text inside the subprocess script.  Keeping the factory
function source and the blocked-module list in one place prevents divergence
bugs and allows the test suite to import and validate the production
configuration.

Security notes
--------------
* The depth counter lives in a closure cell so it is NOT reachable via
  ``builtins.__import__.__globals__`` — the known attack vector
  ``builtins.__import__.__globals__['_import_depth'][0] = 1`` is closed.
* A more sophisticated attacker could still reach ``_depth`` by iterating
  over ``builtins.__import__.__closure__`` and mutating the list found there.
  This is accepted as a known limitation: the subprocess boundary is the true
  security perimeter; the import hook is defence-in-depth only.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Blocked modules
#
# Exposed as a Python frozenset so that the test suite can import it directly
# and use the full production list rather than a hand-maintained subset.
# ---------------------------------------------------------------------------

BLOCKED_MODULES: frozenset[str] = frozenset(
    {
        # Network & HTTP
        "subprocess",
        "shutil",
        "socket",
        "http",
        "urllib",
        "requests",
        "httpx",
        "aiohttp",
        "ftplib",
        "smtplib",
        "telnetlib",
        "xmlrpc",
        # Database drivers
        "psycopg2",
        "psycopg",
        "asyncpg",
        "pg8000",
        "sqlite3",
        "pymysql",
        "mysql",
        "pymongo",
        "motor",
        "sqlalchemy",
        "databases",
        "peewee",
        "tortoise",
        "django",
        # Redis clients
        "redis",
        "aioredis",
        "fakeredis",
        # Import system bypass
        "importlib",
        # Dangerous OS / system access
        "ctypes",
        "multiprocessing",
        "signal",
        "webbrowser",
        "antigravity",
        "tkinter",
        "turtle",
        "pty",
        "termios",
        "fcntl",
        # Serialization attacks
        "pickle",
        "shelve",
        "marshal",
        # Code generation / compilation bypass
        "code",
        "codeop",
        # S3 / cloud SDKs
        "boto3",
        "botocore",
        "minio",
        "google",
        "azure",
    }
)

# ---------------------------------------------------------------------------
# Hard blocklist floor
#
# Modules that must NEVER be re-enabled by an allow-list, even when explicitly
# listed in CODE_INTERPRETER_EXTRA_ALLOWED_MODULES or
# CUSTOM_TOOL_EXTRA_ALLOWED_MODULES. These are sandbox-ESCAPE primitives:
# they grant arbitrary process/native-code execution, bypass the import hook
# itself, or enable deserialization RCE — none of which any legitimate
# "the tool needs outbound HTTP" use case requires. The extra-allowed-modules
# knobs exist to re-enable the network/HTTP stack (requests, urllib, http,
# socket, ...), not to punch a hole straight out of the sandbox, so
# ``effective_blocked_modules`` intersects any admin allow-list against this
# floor and refuses to drop these from the blocklist.
#
# Notably ``importlib`` stays here because re-enabling it would let user code
# do ``importlib.import_module("subprocess")`` and defeat the blocklist
# entirely; ``pickle``/``shelve``/``marshal`` because loading attacker data
# through them is arbitrary code execution; ``subprocess``/``multiprocessing``/
# ``ctypes`` because they spawn processes or run native code. ``socket`` is
# deliberately NOT here: re-enabling it (or the HTTP stack) is a network-policy
# decision the admin makes knowingly, and HTTP libraries import ``socket``
# transitively (depth > 0) so they keep working regardless.
# ---------------------------------------------------------------------------

ALWAYS_BLOCKED_MODULES: frozenset[str] = frozenset(
    {
        "subprocess",
        "multiprocessing",
        "ctypes",
        "importlib",
        "pickle",
        "shelve",
        "marshal",
        "code",
        "codeop",
    }
)

# ---------------------------------------------------------------------------
# _make_safe_import — source code embedded in sandbox subprocess scripts
#
# Defined here as a string so both sandbox wrappers can embed an identical
# copy without copy-pasting.  The factory accepts ``_sandbox_name`` so each
# sandbox can produce a distinct error message.
# ---------------------------------------------------------------------------

_MAKE_SAFE_IMPORT_SRC: str = """\
def _make_safe_import(_original, _blocked, _sandbox_name="sandbox"):
    # Capture the depth counter in a closure cell so it is NOT reachable via
    # _safe_import.__globals__.  A module-level counter could be mutated by
    # user code (e.g. builtins.__import__.__globals__['_import_depth'][0] = 1)
    # to bypass the blocklist entirely; a closure cell is inaccessible to
    # exec'd code without resorting to __closure__ introspection.
    _depth = [0]

    def _safe_import(name, *args, **kwargs):
        # Allow transitive imports triggered by already-allowed packages.
        # For example, matplotlib internally imports importlib during its own
        # initialisation.  Without this guard _safe_import would re-enter with
        # depth 0 and incorrectly block the import.
        if _depth[0] > 0:
            return _original(name, *args, **kwargs)
        top = name.split(".")[0]
        if top in _blocked:
            raise ImportError(
                "Module '{}' is not available in the {} "
                "for security reasons.".format(name, _sandbox_name)
            )
        _depth[0] += 1
        try:
            return _original(name, *args, **kwargs)
        finally:
            _depth[0] -= 1

    return _safe_import
"""


def effective_blocked_modules(allow: frozenset[str] | None = None) -> frozenset[str]:
    """Return ``BLOCKED_MODULES`` minus any modules in *allow*.

    *allow* lets a specific sandbox opt selected modules back in (e.g. the code
    interpreter re-enabling ``socket`` via an admin setting) WITHOUT mutating the
    shared :data:`BLOCKED_MODULES` frozenset - so other sandboxes (the custom
    tool executor) keep the full blocklist. ``None`` / empty leaves the
    production blocklist unchanged.

    Intersection guard: modules in :data:`ALWAYS_BLOCKED_MODULES` (sandbox-escape
    primitives) are subtracted from *allow* first, so an admin allow-list can
    re-enable the network/HTTP stack but can NEVER remove e.g. ``subprocess``,
    ``ctypes`` or ``importlib`` from the blocklist — even if they are explicitly
    listed in ``CODE_INTERPRETER_EXTRA_ALLOWED_MODULES`` /
    ``CUSTOM_TOOL_EXTRA_ALLOWED_MODULES``. This keeps the hardening intact
    regardless of misconfiguration.
    """
    if not allow:
        return BLOCKED_MODULES
    effective_allow = frozenset(allow) - ALWAYS_BLOCKED_MODULES
    if not effective_allow:
        return BLOCKED_MODULES
    return BLOCKED_MODULES - effective_allow


def _blocked_modules_repr(blocked: frozenset[str] | None = None) -> str:
    """Return *blocked* (default ``BLOCKED_MODULES``) as a frozenset literal string.

    The returned string can be embedded directly in sandbox subprocess scripts
    or evaluated via ``ast.literal_eval`` on the inner set literal.

    Example return value::

        frozenset({'aiohttp', 'aioredis', ...})
    """
    modules = BLOCKED_MODULES if blocked is None else blocked
    return f"frozenset({{{', '.join(repr(m) for m in sorted(modules))}}})"


def build_sandbox_security_src(
    builtins_alias: str,
    sandbox_name: str,
    allow: frozenset[str] | None = None,
) -> str:
    """Return the complete sandbox security setup as Python source code.

    The returned string defines ``_BLOCKED_MODULES``, the ``_make_safe_import``
    factory, and wires it to ``builtins.__import__``.  It is designed to be
    embedded verbatim inside a sandbox wrapper script that runs in a fresh
    subprocess.

    Args:
        builtins_alias: Name of the builtins module in the sandbox context.
            Use ``"builtins"`` for ``code_interpreter`` (which imports builtins
            directly) and ``"_builtins"`` for ``custom_tool_executor`` (which
            aliases it as ``import builtins as _builtins``).
        sandbox_name: Human-readable label injected into ``ImportError``
            messages, e.g. ``"code interpreter sandbox"``.
        allow: Optional set of module names to remove from the blocklist for
            *this* sandbox only (defence-in-depth opt-in, e.g. the code
            interpreter re-enabling ``socket``). The shared
            :data:`BLOCKED_MODULES` is never mutated, so sibling sandboxes are
            unaffected. ``None`` keeps the full production blocklist.

    Returns:
        A multi-line Python source string ready for embedding in a sandbox
        wrapper script.  The string contains the ``_BLOCKED_MODULES`` frozenset
        definition, the ``_make_safe_import`` factory function definition, and
        the ``{builtins_alias}.__import__ = _make_safe_import(...)`` call that
        installs the hook.
    """
    blocked_src = "_BLOCKED_MODULES = " + _blocked_modules_repr(effective_blocked_modules(allow))

    call_src = (
        f"{builtins_alias}.__import__ = "
        f"_make_safe_import({builtins_alias}.__import__, _BLOCKED_MODULES, {sandbox_name!r})"
    )

    return (
        "# ---- Block dangerous modules ------------------------------------------------\n"
        + blocked_src
        + "\n\n"
        + _MAKE_SAFE_IMPORT_SRC
        + "\n"
        + call_src
        + "\n"
    )
