"""Tests for the code_interpreter sandbox's _safe_import transitive-import guard.

The sandbox patches builtins.__import__ with _safe_import to block dangerous
modules.  Before the fix, _safe_import checked every import at depth 0, which
caused allowed libraries (e.g. matplotlib) to break because their own __init__
internally imports a blocked module (e.g. importlib).

After the fix, a recursive depth counter in a closure cell ensures that only
*top-level* user imports are checked against the blocklist; transitive imports
triggered by already-allowed packages are let through.  Using a closure cell
(rather than a module-level variable) also prevents user code from manipulating
the counter via ``builtins.__import__.__globals__``.
"""

from __future__ import annotations

import ast
import subprocess
import sys
import textwrap

from app.services._sandbox_utils import (
    _MAKE_SAFE_IMPORT_SRC,
    ALWAYS_BLOCKED_MODULES,
    BLOCKED_MODULES,
    _blocked_modules_repr,
    build_sandbox_security_src,
    effective_blocked_modules,
)


def _run_sandbox_script(script: str) -> tuple[int, str, str]:
    """Execute *script* in a fresh Python subprocess and return (rc, stdout, stderr)."""
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
    )
    # Test helper intentionally executes the provided script in an isolated subprocess.
    return result.returncode, result.stdout, result.stderr


# ---------------------------------------------------------------------------
# Sandbox headers that mirror the production sandbox patterns.
#
# Both headers use the production BLOCKED_MODULES frozenset (imported above)
# and the shared _MAKE_SAFE_IMPORT_SRC factory, so any change to either in
# the production modules is automatically reflected in these tests.
#
# ---------------------------------------------------------------------------
# These headers reproduce ONLY the import-hook portion of each production
# sandbox (the _make_safe_import wiring against the shared BLOCKED_MODULES),
# which is what these tests exercise. They deliberately omit the env scrubbing
# and os-function stripping that the full production wrappers
# (code_interpreter._SANDBOX_WRAPPER / custom_tool_executor.
# _build_python_sandbox_header) also embed — those are covered elsewhere.
# ---------------------------------------------------------------------------

# Serialise the production frozenset into a Python literal for the subprocess.
# _blocked_modules_repr() is the shared helper from _sandbox_utils to avoid
# duplicating the sort+join logic here.
_BLOCKED_REPR = _blocked_modules_repr()

_CODE_INTERPRETER_HEADER = (
    "import builtins\n"
    f"_BLOCKED = {_BLOCKED_REPR}\n\n"
    + _MAKE_SAFE_IMPORT_SRC
    + "\nbuiltins.__import__ = _make_safe_import(builtins.__import__, _BLOCKED, \"code interpreter sandbox\")\n\n"
)

# custom_tool_executor uses 'import builtins as _builtins' alias
_CUSTOM_TOOL_HEADER = (
    "import builtins as _builtins\n"
    f"_BLOCKED = {_BLOCKED_REPR}\n\n"
    + _MAKE_SAFE_IMPORT_SRC
    + "\n_builtins.__import__ = _make_safe_import(_builtins.__import__, _BLOCKED, \"custom tool sandbox\")\n\n"
)


def _run_code_interpreter_code(user_code: str) -> tuple[int, str, str]:
    script = _CODE_INTERPRETER_HEADER + textwrap.dedent(user_code)
    return _run_sandbox_script(script)


def _run_custom_tool_code(user_code: str) -> tuple[int, str, str]:
    script = _CUSTOM_TOOL_HEADER + textwrap.dedent(user_code)
    return _run_sandbox_script(script)


# ---------------------------------------------------------------------------
# Blocklist coverage assertion
#
# Ensures the test blocked-module set is identical to the production set, so
# drift is caught immediately rather than silently producing false-positive
# passing tests.
# ---------------------------------------------------------------------------


def test_test_blocklist_matches_production():
    """Test header _BLOCKED must equal the production BLOCKED_MODULES frozenset."""
    # _BLOCKED_REPR is "frozenset({'aiohttp', ...})".  Strip the outer
    # "frozenset(...)" wrapper and use ast.literal_eval on the inner set literal
    # to avoid exec() in test code.  Use removeprefix/removesuffix so the
    # strip is robust to whitespace changes in _blocked_modules_repr().
    inner = _BLOCKED_REPR.removeprefix("frozenset(").removesuffix(")")
    parsed = frozenset(ast.literal_eval(inner))
    assert parsed == BLOCKED_MODULES, (
        "Test blocklist diverges from production BLOCKED_MODULES.  "
        "Update _sandbox_utils.BLOCKED_MODULES."
    )


# ---------------------------------------------------------------------------
# code_interpreter sandbox tests
# ---------------------------------------------------------------------------


def test_direct_blocked_import_raises():
    """User code that directly imports a blocked module must still fail."""
    rc, stdout, stderr = _run_code_interpreter_code("import importlib")
    assert rc != 0, "Expected non-zero exit when importing a blocked module"
    assert "not available in" in stderr or "not available in" in stdout


def test_direct_blocked_submodule_raises():
    """User code that imports a blocked sub-module must still fail."""
    rc, _stdout, _stderr = _run_code_interpreter_code("import importlib.util")
    assert rc != 0, "Expected non-zero exit when importing a blocked sub-module"


def test_allowed_module_that_transitively_imports_blocked_module():
    """An allowed module that internally imports a blocked module must succeed.

    We create a temporary helper module whose body does ``import importlib``
    (mirroring what matplotlib's __init__.py does on Python 3.14).  The
    sandbox should allow this transitive import because it is triggered from
    within _original (depth > 0), not by the top-level user code.
    """
    user_code = textwrap.dedent("""\
        import sys, os, tempfile

        # Create a fake third-party module that internally imports importlib.
        # This mirrors matplotlib/__init__.py line 143 on Python >= 3.14.
        tmpdir = tempfile.mkdtemp()
        modpath = os.path.join(tmpdir, "fake_lib.py")
        with open(modpath, "w") as fh:
            fh.write("import importlib\\nversion = '1.0'\\n")

        sys.path.insert(0, tmpdir)
        import fake_lib
        print(fake_lib.version)
    """)
    rc, stdout, stderr = _run_code_interpreter_code(user_code)
    assert rc == 0, f"Transitive import should not be blocked.  stderr={stderr!r}"
    assert "1.0" in stdout


def test_allowed_stdlib_import_works():
    """Plain allowed imports must continue to work."""
    rc, stdout, stderr = _run_code_interpreter_code("import json; print(json.dumps({'ok': True}))")
    assert rc == 0, f"json import should work.  stderr={stderr!r}"
    assert '"ok"' in stdout


def test_depth_resets_after_import():
    """After an allowed import completes, depth must be back to 0 so a subsequent
    direct blocked import is still rejected."""
    user_code = textwrap.dedent("""\
        import json          # allowed, depth resets to 0 afterwards
        import importlib     # must still be blocked
    """)
    rc, _stdout, _stderr = _run_code_interpreter_code(user_code)
    assert rc != 0, "importlib must be blocked even after a prior allowed import"


def test_user_cannot_bypass_via_globals():
    """User code cannot manipulate the depth counter via __globals__ to skip the blocklist.

    The depth counter lives in a closure cell, not in the function's __globals__,
    so ``builtins.__import__.__globals__['_import_depth']`` raises KeyError and
    the blocked import remains blocked.
    """
    user_code = textwrap.dedent("""\
        import builtins
        # Pre-fix attack: set _import_depth in globals to skip the depth==0 check.
        # With the closure approach _import_depth is NOT in __globals__ → KeyError.
        try:
            builtins.__import__.__globals__['_import_depth'][0] = 1
        except KeyError:
            pass
        # subprocess must still be blocked regardless of the above attempt.
        import subprocess
        print('ESCAPED')
    """)
    _rc, stdout, _stderr = _run_code_interpreter_code(user_code)
    assert "ESCAPED" not in stdout, "User must not be able to bypass sandbox via __globals__"


# ---------------------------------------------------------------------------
# custom_tool_executor sandbox tests (uses _builtins alias)
# ---------------------------------------------------------------------------


def test_custom_tool_direct_blocked_import_raises():
    """custom_tool sandbox: direct blocked imports are rejected."""
    rc, _stdout, _stderr = _run_custom_tool_code("import subprocess")
    assert rc != 0, "subprocess must be blocked in custom tool sandbox"


def test_custom_tool_transitive_import_allowed():
    """custom_tool sandbox: transitive imports from allowed packages pass through."""
    user_code = textwrap.dedent("""\
        import sys, os, tempfile

        tmpdir = tempfile.mkdtemp()
        modpath = os.path.join(tmpdir, "fake_lib2.py")
        with open(modpath, "w") as fh:
            fh.write("import importlib\\nvalue = 42\\n")

        sys.path.insert(0, tmpdir)
        import fake_lib2
        print(fake_lib2.value)
    """)
    rc, stdout, stderr = _run_custom_tool_code(user_code)
    assert rc == 0, f"Transitive import should not be blocked.  stderr={stderr!r}"
    assert "42" in stdout


def test_custom_tool_cannot_bypass_via_globals():
    """custom_tool sandbox: _import_depth is not in __globals__ so bypass fails."""
    user_code = textwrap.dedent("""\
        import builtins
        try:
            builtins.__import__.__globals__['_import_depth'][0] = 1
        except KeyError:
            pass
        import subprocess
        print('ESCAPED')
    """)
    _rc, stdout, _stderr = _run_custom_tool_code(user_code)
    assert "ESCAPED" not in stdout, "User must not be able to bypass custom tool sandbox via __globals__"


# ---------------------------------------------------------------------------
# Per-sandbox allow override (CODE_INTERPRETER_EXTRA_ALLOWED_MODULES)
#
# The code interpreter can re-enable a blocked module (e.g. socket) for itself
# only, without weakening the shared blocklist or the custom tool sandbox.
# ---------------------------------------------------------------------------


def _run_with_security_src(user_code: str, allow=None) -> tuple[int, str, str]:
    """Run *user_code* under a sandbox header built by the production factory."""
    header = (
        "import builtins\n"
        + build_sandbox_security_src("builtins", "code interpreter sandbox", allow=allow)
        + "\n"
    )
    return _run_sandbox_script(header + textwrap.dedent(user_code))


def test_effective_blocked_modules_removes_allowed_without_mutating_production():
    assert "socket" in BLOCKED_MODULES  # baseline: blocked by default

    effective = effective_blocked_modules(frozenset({"socket"}))
    assert "socket" not in effective
    # Other dangerous modules remain blocked.
    assert "subprocess" in effective
    # The shared production frozenset is never mutated.
    assert "socket" in BLOCKED_MODULES
    # No allow-list => identical object/contents as production.
    assert effective_blocked_modules(None) == BLOCKED_MODULES
    assert effective_blocked_modules(frozenset()) == BLOCKED_MODULES


def test_allow_override_unblocks_socket_in_code_interpreter():
    rc, stdout, stderr = _run_with_security_src(
        "import socket; print('SOCKET_OK', hasattr(socket, 'socket'))",
        allow=frozenset({"socket"}),
    )
    assert rc == 0, f"socket should import when explicitly allowed. stderr={stderr!r}"
    assert "SOCKET_OK True" in stdout


def test_allow_override_is_scoped_socket_blocked_by_default():
    # Without the override, socket stays blocked (secure default unchanged).
    rc, stdout, stderr = _run_with_security_src("import socket", allow=None)
    assert rc != 0, "socket must remain blocked when no allow-list is configured"
    assert "not available in" in (stdout + stderr)


def test_allow_override_does_not_unblock_other_modules():
    # Allowing socket must NOT incidentally allow other blocked modules.
    rc, _stdout, _stderr = _run_with_security_src(
        "import subprocess", allow=frozenset({"socket"})
    )
    assert rc != 0, "subprocess must stay blocked even when socket is allowed"


def test_custom_tool_sandbox_keeps_socket_blocked():
    # By default (no allow-list) the custom tool sandbox keeps the full blocklist.
    rc, stdout, stderr = _run_custom_tool_code("import socket")
    assert rc != 0, "socket must stay blocked in the custom tool sandbox"
    assert "not available in" in (stdout + stderr)


# ---------------------------------------------------------------------------
# Settings -> wrapper wiring (CODE_INTERPRETER_EXTRA_ALLOWED_MODULES)
#
# Hermetic: exercises the parse + wrapper-assembly path without spawning the
# full interpreter subprocess (which imports matplotlib and is therefore
# environment-sensitive). The actual import-hook behaviour is covered by the
# subprocess tests above.
# ---------------------------------------------------------------------------


def test_resolve_allowed_modules_parses_setting(monkeypatch):
    from app.core.config import settings
    from app.services.code_interpreter import _resolve_allowed_modules

    monkeypatch.setattr(settings, "code_interpreter_extra_allowed_modules", "socket, http , ")
    assert _resolve_allowed_modules() == frozenset({"socket", "http"})


def test_resolve_allowed_modules_empty_by_default(monkeypatch):
    from app.core.config import settings
    from app.services.code_interpreter import _resolve_allowed_modules

    monkeypatch.setattr(settings, "code_interpreter_extra_allowed_modules", "")
    assert _resolve_allowed_modules() == frozenset()


def test_resolve_allowed_modules_normalizes_dotted_paths(monkeypatch):
    # An admin pasting a dotted import path from an ImportError (e.g.
    # "urllib.request") must still re-enable the top-level package, because the
    # import hook blocks/permits on name.split(".")[0].
    from app.core.config import settings
    from app.services.code_interpreter import _resolve_allowed_modules

    monkeypatch.setattr(
        settings, "code_interpreter_extra_allowed_modules", "urllib.request, http.client"
    )
    assert _resolve_allowed_modules() == frozenset({"urllib", "http"})


def test_build_wrapper_applies_allow_to_embedded_blocklist():
    from app.services.code_interpreter import _build_sandbox_wrapper

    # With socket allowed, the embedded blocklist drops socket but keeps the rest.
    allowed_src = _build_sandbox_wrapper(frozenset({"socket"}))
    assert "'socket'" not in allowed_src
    assert "'subprocess'" in allowed_src

    # Default (no override) keeps socket blocked.
    default_src = _build_sandbox_wrapper(frozenset())
    assert "'socket'" in default_src


# ---------------------------------------------------------------------------
# Settings -> custom tool sandbox wiring (CUSTOM_TOOL_EXTRA_ALLOWED_MODULES)
#
# Hermetic tests: exercises parse + header-assembly without spawning the
# full interpreter subprocess.
# ---------------------------------------------------------------------------


def test_custom_tool_allowed_modules_parses_setting(monkeypatch):
    from app.core.config import settings
    from app.services.custom_tool_executor import _resolve_custom_tool_allowed_modules

    monkeypatch.setattr(settings, "custom_tool_extra_allowed_modules", "requests, urllib , http")
    assert _resolve_custom_tool_allowed_modules() == frozenset({"requests", "urllib", "http"})


def test_custom_tool_allowed_modules_empty_by_default(monkeypatch):
    from app.core.config import settings
    from app.services.custom_tool_executor import _resolve_custom_tool_allowed_modules

    monkeypatch.setattr(settings, "custom_tool_extra_allowed_modules", "")
    assert _resolve_custom_tool_allowed_modules() == frozenset()


def test_custom_tool_allowed_modules_drops_degenerate_entries(monkeypatch):
    # Malformed entries whose top-level segment is empty (a lone ".", a
    # leading-dot relative path like ".requests", or bare whitespace) must be
    # dropped entirely - an empty string in the allow-list would never match
    # an import name, but it must not survive parsing either.
    from app.core.config import settings
    from app.services.custom_tool_executor import _resolve_custom_tool_allowed_modules

    monkeypatch.setattr(
        settings, "custom_tool_extra_allowed_modules", ".requests, ., ,requests"
    )
    assert _resolve_custom_tool_allowed_modules() == frozenset({"requests"})


def test_custom_tool_allowed_modules_normalizes_dotted_paths(monkeypatch):
    # "urllib.request" pasted from an ImportError must re-enable "urllib".
    from app.core.config import settings
    from app.services.custom_tool_executor import _resolve_custom_tool_allowed_modules

    monkeypatch.setattr(
        settings, "custom_tool_extra_allowed_modules", "urllib.request, requests"
    )
    assert _resolve_custom_tool_allowed_modules() == frozenset({"urllib", "requests"})


def test_build_python_sandbox_header_applies_allow_to_embedded_blocklist():
    from app.services.custom_tool_executor import _build_python_sandbox_header

    # With requests and urllib allowed, they are absent from the embedded blocklist.
    allowed_src = _build_python_sandbox_header(frozenset({"requests", "urllib"}))
    assert "'requests'" not in allowed_src
    assert "'urllib'" not in allowed_src
    # Other dangerous modules remain blocked.
    assert "'subprocess'" in allowed_src
    assert "'socket'" in allowed_src

    # Default (no override) keeps all blocked.
    default_src = _build_python_sandbox_header(frozenset())
    assert "'requests'" in default_src
    assert "'urllib'" in default_src


def test_custom_tool_sandbox_allows_requests_when_configured(monkeypatch):
    """Custom tool sandbox unblocks 'requests' when CUSTOM_TOOL_EXTRA_ALLOWED_MODULES includes it."""
    from app.core.config import settings
    from app.services.custom_tool_executor import _build_python_sandbox_header, _resolve_custom_tool_allowed_modules

    monkeypatch.setattr(settings, "custom_tool_extra_allowed_modules", "requests")
    allow = _resolve_custom_tool_allowed_modules()
    header = _build_python_sandbox_header(allow)
    # requests should NOT appear in the blocklist embedded in the header.
    assert "'requests'" not in header
    # Other dangerous modules (socket, subprocess) must still be blocked.
    assert "'socket'" in header
    assert "'subprocess'" in header


def test_custom_tool_allow_does_not_weaken_code_interpreter_sandbox(monkeypatch):
    """Allowing modules in the custom tool sandbox must not affect the code interpreter sandbox."""
    from app.core.config import settings
    from app.services.code_interpreter import _build_sandbox_wrapper

    # Set the custom-tool allow-list only (code interpreter setting is empty).
    monkeypatch.setattr(settings, "custom_tool_extra_allowed_modules", "requests,urllib")
    monkeypatch.setattr(settings, "code_interpreter_extra_allowed_modules", "")

    # The code interpreter sandbox must still block requests and urllib.
    ci_src = _build_sandbox_wrapper(frozenset())
    assert "'requests'" in ci_src
    assert "'urllib'" in ci_src


# ---------------------------------------------------------------------------
# Hard blocklist floor (ALWAYS_BLOCKED_MODULES intersection guard)
#
# An admin allow-list may re-enable the network/HTTP stack, but must NEVER be
# able to remove a sandbox-escape primitive from the blocklist - even if one
# is explicitly listed in CODE_INTERPRETER_EXTRA_ALLOWED_MODULES /
# CUSTOM_TOOL_EXTRA_ALLOWED_MODULES. This keeps the hardening intact.
# ---------------------------------------------------------------------------


def test_always_blocked_modules_is_subset_of_blocklist():
    # Every hard-floor module must actually be in the base blocklist, otherwise
    # the intersection guard would be silently guarding nothing.
    assert ALWAYS_BLOCKED_MODULES <= BLOCKED_MODULES
    # socket is deliberately NOT in the hard floor (network is an admin policy
    # decision and HTTP libs import it transitively).
    assert "socket" not in ALWAYS_BLOCKED_MODULES
    # The escape primitives that matter most.
    for mod in ("subprocess", "ctypes", "importlib", "pickle", "marshal"):
        assert mod in ALWAYS_BLOCKED_MODULES


def test_effective_blocked_modules_never_drops_escape_primitives():
    # Even when explicitly allowed, escape primitives stay blocked.
    effective = effective_blocked_modules(
        frozenset({"subprocess", "ctypes", "importlib", "pickle", "requests"})
    )
    assert "subprocess" in effective
    assert "ctypes" in effective
    assert "importlib" in effective
    assert "pickle" in effective
    # ...while the legitimately-allowable HTTP module is still dropped.
    assert "requests" not in effective


def test_effective_blocked_modules_only_escape_allow_is_noop():
    # An allow-list consisting solely of hard-floor modules must leave the
    # production blocklist entirely unchanged (no partial relaxation).
    assert (
        effective_blocked_modules(frozenset({"subprocess", "importlib"}))
        == BLOCKED_MODULES
    )


def test_build_wrapper_cannot_unblock_subprocess_via_allow():
    from app.services.code_interpreter import _build_sandbox_wrapper

    # Attempt to re-enable subprocess through the code interpreter allow-list.
    src = _build_sandbox_wrapper(frozenset({"subprocess", "socket"}))
    # socket is allowed (dropped from the embedded blocklist)...
    assert "'socket'" not in src
    # ...but subprocess must remain blocked despite being in the allow-list.
    assert "'subprocess'" in src


def test_custom_tool_header_cannot_unblock_subprocess_via_allow():
    from app.services.custom_tool_executor import _build_python_sandbox_header

    # Attempt to re-enable subprocess + ctypes through the custom tool allow-list.
    header = _build_python_sandbox_header(frozenset({"requests", "subprocess", "ctypes"}))
    # requests is allowed...
    assert "'requests'" not in header
    # ...but the escape primitives stay blocked.
    assert "'subprocess'" in header
    assert "'ctypes'" in header


def test_custom_tool_sandbox_keeps_subprocess_blocked_even_when_allowed(monkeypatch):
    """End-to-end: a subprocess import stays blocked even if the admin lists it."""
    from app.core.config import settings
    from app.services.custom_tool_executor import (
        _build_python_sandbox_header,
        _resolve_custom_tool_allowed_modules,
    )

    monkeypatch.setattr(
        settings, "custom_tool_extra_allowed_modules", "requests,subprocess"
    )
    allow = _resolve_custom_tool_allowed_modules()
    header = _build_python_sandbox_header(allow)
    rc, stdout, stderr = _run_sandbox_script(header + "\nimport subprocess\n")
    assert rc != 0, "subprocess must stay blocked even when listed in the allow-list"
    assert "not available in" in (stdout + stderr)


# ---------------------------------------------------------------------------
# Native thread-pool caps survive the env scrub. The OpenSandbox
# container receives them (``_sandbox_selector.native_thread_env``); both
# wrappers scrub os.environ before matplotlib / numpy load OpenBLAS, so a cap
# the scrub dropped would leave the pool sized on the host's core count.
# ---------------------------------------------------------------------------


def _env_after_scrub(header: str, os_name: str) -> dict[str, str]:
    from app.services._sandbox_selector import NATIVE_THREAD_ENV_KEYS

    probe = header + f"\nprint(repr(dict({os_name}.environ)))\n"
    env = {
        "PATH": "/usr/bin:/bin",
        "DATABASE_URL": "postgresql://secret",
        **dict.fromkeys(NATIVE_THREAD_ENV_KEYS, "2"),
    }
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    return ast.literal_eval(result.stdout.strip().splitlines()[-1])


def test_code_interpreter_scrub_keeps_the_native_thread_caps():
    from app.services._sandbox_selector import NATIVE_THREAD_ENV_KEYS
    from app.services.code_interpreter import _SANDBOX_WRAPPER_PREFIX

    kept = _env_after_scrub(_SANDBOX_WRAPPER_PREFIX, "os")

    assert {key: kept.get(key) for key in NATIVE_THREAD_ENV_KEYS} == dict.fromkeys(NATIVE_THREAD_ENV_KEYS, "2")
    assert "DATABASE_URL" not in kept


def test_custom_tool_scrub_keeps_the_native_thread_caps():
    from app.services._sandbox_selector import NATIVE_THREAD_ENV_KEYS
    from app.services.custom_tool_executor import _build_python_sandbox_header

    kept = _env_after_scrub(_build_python_sandbox_header(frozenset()), "_os")

    assert {key: kept.get(key) for key in NATIVE_THREAD_ENV_KEYS} == dict.fromkeys(NATIVE_THREAD_ENV_KEYS, "2")
    assert "DATABASE_URL" not in kept
