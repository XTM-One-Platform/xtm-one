# SPDX-License-Identifier: Apache-2.0

"""Module account preflight of a per-user CTEM stage run (#2042).

A per-user CTEM stage run (scoping, discovery, prioritization, validation,
remediation, the exposure briefing) acts as one member through that member's
own OpenCTI / OpenAEV identity. A member who never signed in to a module has no
account there: every call answers 401 (OpenCTI) or 403 (OpenAEV), which is
expected, and the run used to spend several hundred thousand tokens before it
completed "degraded" with nothing read.

The preflight reads the member's identity on each module the stage may use,
once, with the run's own credential (OpenCTI ``me { id }``, OpenAEV
``GET /api/me``), before the run is charged and before any model call. When the
stage's need is not met the run ends at zero tokens with the reason
:data:`NO_MODULE_ACCOUNT`, naming the module(s). Anything other than a clear
"no account" answer (a timeout, a 5xx, an unreadable body, no credential) is
UNKNOWN and lets the run go ahead: the preflight never blocks a run on a guess.

Module authentication is not changed: the probe sends the same token the run's
tools would send.
"""

from __future__ import annotations

import contextlib
import logging
import time
import uuid
from collections.abc import Awaitable, Callable, Iterable, Iterator, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Final, Literal

from app.core.egress import egress_client
from app.core.utils import format_exception_message

logger = logging.getLogger(__name__)

#: The reason a run skipped by the preflight carries.
NO_MODULE_ACCOUNT: Final = "no_module_account"

AccountState = Literal["present", "missing", "unknown"]

#: Display names of the modules the preflight reads.
MODULE_LABELS: Final[dict[str, str]] = {"opencti": "OpenCTI", "openaev": "OpenAEV"}

#: A need is a tuple of groups, ALL required; a group is met by ANY of its modules.
Need = tuple[tuple[str, ...], ...]

#: Every CTEM stage reads OpenCTI, OpenAEV or both, and falls back to the one
#: it has (validation reads OpenCTI detection coverage without OpenAEV): one
#: account of the two is enough for the stage to produce something.
ANY_CTEM_MODULE: Final[Need] = (("opencti", "openaev"),)

#: Per-user CTEM stage agents (seeded slugs) and the module accounts they need.
STAGE_NEEDS: Final[dict[str, Need]] = {
    "ctem-scoping-analyst": ANY_CTEM_MODULE,
    "ctem-discovery-analyst": ANY_CTEM_MODULE,
    "ctem-prioritization-analyst": ANY_CTEM_MODULE,
    "ctem-validation-analyst": ANY_CTEM_MODULE,
    "ctem-mobilization-analyst": ANY_CTEM_MODULE,
    "exposure-briefing-analyst": ANY_CTEM_MODULE,
}

#: Seconds an identity answer is reused for the same member and integration.
_PRESENT_TTL_SECS: Final = 30 * 60
_MISSING_TTL_SECS: Final = 10 * 60
#: Seconds one identity read may take.
_PROBE_TIMEOUT_SECS: Final = 8.0

_cache: dict[tuple[str, str, str], tuple[float, AccountState]] = {}

#: Substrings of an OpenCTI GraphQL error meaning the identity is not known.
_OCTI_NO_ACCOUNT_MARKERS: Final = ("auth_required", "you must be logged in", "forbidden_access")


@dataclass(frozen=True)
class PreflightVerdict:
    """What the preflight decided for one run."""

    proceed: bool
    states: dict[str, AccountState]
    missing: tuple[str, ...] = ()

    @property
    def summary(self) -> str:
        """The run's result line when it is skipped."""
        names = " and ".join(MODULE_LABELS.get(m, m) for m in self.missing)
        return (
            f"{NO_MODULE_ACCOUNT}: skipped before any model call, this member has no "
            f"{names} account. Sign in to {names} once, then run the stage again."
        )


def stage_need(agent_slug: str | None, *, is_builtin: bool) -> Need | None:
    """The module need of a seeded per-user CTEM stage agent, ``None`` for any other agent."""
    if not is_builtin or not agent_slug:
        return None
    return STAGE_NEEDS.get(agent_slug)


def needed_modules(need: Need) -> tuple[str, ...]:
    """Every module a need names, in order, once."""
    seen: list[str] = []
    for group in need:
        for module in group:
            if module not in seen:
                seen.append(module)
    return tuple(seen)


def unmet_modules(need: Need, states: dict[str, AccountState]) -> tuple[str, ...]:
    """The modules to name when *need* is not met, empty when the run goes ahead.

    A group is met when one of its modules is present OR unknown (fail open);
    an unmet group names its modules found missing.
    """
    unmet: list[str] = []
    for group in need:
        if any(states.get(m, "unknown") != "missing" for m in group):
            continue
        for module in group:
            if module not in unmet:
                unmet.append(module)
    return tuple(unmet)


def decide(need: Need, states: dict[str, AccountState]) -> PreflightVerdict:
    """The verdict for *need* given the account *states* read."""
    missing = unmet_modules(need, states)
    return PreflightVerdict(proceed=not missing, states=dict(states), missing=missing)


def classify_status(module: str, status: int, body: object) -> AccountState:
    """An identity read's answer as an account state.

    OpenCTI answers an unknown identity with HTTP 401, or 200 carrying an
    ``AUTH_REQUIRED`` GraphQL error; OpenAEV with 401 or 403. A 200 naming the
    user is an account. Everything else is unknown.
    """
    if status in (401, 403):
        return "missing"
    if status != 200 or not isinstance(body, dict):
        return "unknown"
    if module == "opencti":
        me = (body.get("data") or {}).get("me") if isinstance(body.get("data"), dict) else None
        if isinstance(me, dict) and me.get("id"):
            return "present"
        errors = body.get("errors")
        if isinstance(errors, list):
            text = " ".join(
                f"{e.get('message', '')} {(e.get('extensions') or {}).get('code', '')}".lower()
                for e in errors
                if isinstance(e, dict)
            )
            if any(marker in text for marker in _OCTI_NO_ACCOUNT_MARKERS):
                return "missing"
        return "unknown"
    if module == "openaev":
        return "present" if body.get("user_id") or body.get("user_email") or body.get("id") else "unknown"
    return "unknown"


async def probe_identity(module: str, base_url: str, token: str) -> AccountState:
    """Read the member's identity on *module* once; never raises."""
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    try:
        # Through the egress funnel: the probe reads the same module URLs as
        # the module tools, so it follows the run's relay route like them.
        async with egress_client(timeout=_PROBE_TIMEOUT_SECS) as client:
            if module == "opencti":
                resp = await client.post(
                    f"{base_url}/graphql", headers=headers, json={"query": "query XtmOnePreflight { me { id } }"}
                )
            elif module == "openaev":
                resp = await client.get(f"{base_url}/api/me", headers=headers)
            else:
                return "unknown"
        try:
            body: object = resp.json()
        except ValueError:
            body = None
        return classify_status(module, resp.status_code, body)
    except Exception as exc:  # a probe never blocks a run
        logger.info("CTEM preflight: %s identity read failed: %s", module, format_exception_message(exc))
        return "unknown"


def _cached(key: tuple[str, str, str]) -> AccountState | None:
    hit = _cache.get(key)
    if hit is None or hit[0] < time.monotonic():
        _cache.pop(key, None)
        return None
    return hit[1]


def _remember(key: tuple[str, str, str], state: AccountState) -> None:
    if state == "unknown":
        return
    ttl = _PRESENT_TTL_SECS if state == "present" else _MISSING_TTL_SECS
    _cache[key] = (time.monotonic() + ttl, state)


def forget(user_id: str | uuid.UUID | None = None) -> None:
    """Drop the cached answers (of one member, or all): a member who just signed in is read again."""
    if user_id is None:
        _cache.clear()
        return
    for key in [k for k in _cache if k[2] == str(user_id)]:
        _cache.pop(key, None)


#: ``(module) -> (integration_id, base_url, token)`` or ``None`` when the run has no credential.
CredentialResolver = Callable[[str], Awaitable[tuple[str, str, str] | None]]
Prober = Callable[[str, str, str], Awaitable[AccountState]]


async def read_states(
    modules: Iterable[str],
    user_id: str | uuid.UUID,
    resolve: CredentialResolver,
    probe: Prober = probe_identity,
) -> dict[str, AccountState]:
    """The member's account state on each module, cached per member and integration."""
    states: dict[str, AccountState] = {}
    for module in modules:
        try:
            creds = await resolve(module)
        except Exception as exc:
            logger.info("CTEM preflight: no %s credential: %s", module, format_exception_message(exc))
            creds = None
        if creds is None:
            states[module] = "unknown"
            continue
        integration_id, base_url, token = creds
        key = (module, integration_id, str(user_id))
        state = _cached(key)
        if state is None:
            state = await probe(module, base_url, token)
            _remember(key, state)
        states[module] = state
    return states


async def preflight(
    need: Need,
    user_id: str | uuid.UUID,
    resolve: CredentialResolver,
    probe: Prober = probe_identity,
) -> PreflightVerdict:
    """Read the accounts *need* names and decide whether the run goes ahead."""
    states = await read_states(needed_modules(need), user_id, resolve, probe)
    return decide(need, states)


def module_of_tool(tool_name: str, modules: Sequence[str] = ("opencti", "openaev")) -> str | None:
    """The module a tool name belongs to (``search_opencti_x`` -> ``opencti``), else ``None``."""
    lowered = tool_name.lower()
    for module in modules:
        if module in lowered:
            return module
    return None


# ---------------------------------------------------------------------------
# Mid-run guard (#2042 slices 2 and 3)
# ---------------------------------------------------------------------------

#: Lower-cased substrings of a module tool's error meaning the member has no
#: account there: the 401 / 403 wording of ``integrations.http.format_http_error``
#: and OpenCTI's ``AUTH_REQUIRED`` GraphQL answer. A capability denial
#: (OpenCTI ``FORBIDDEN_ACCESS``, "not allowed to do this") is not one of them.
_ACCOUNT_REFUSAL_MARKERS: Final[dict[str, tuple[str, ...]]] = {
    "opencti": ("opencti authentication failed", "you must be logged in", "auth_required"),
    "openaev": ("openaev authentication failed", "openaev access denied"),
}

#: The modules that refused the member during the current per-user CTEM stage
#: run, ``None`` outside such a run (the guard is off).
_refused: ContextVar[set[str] | None] = ContextVar("ctem_module_refusals", default=None)


@contextlib.contextmanager
def guarding_module_accounts(active: bool) -> Iterator[None]:
    """Turn the mid-run account guard on for the block when *active* (a per-user CTEM stage run)."""
    token = _refused.set(set() if active else None)
    try:
        yield
    finally:
        _refused.reset(token)


def is_account_refusal(module: str, content: str | None) -> bool:
    """Whether a *module* tool's error *content* says the member has no account there."""
    text = (content or "").lower()
    return any(marker in text for marker in _ACCOUNT_REFUSAL_MARKERS.get(module, ()))


def note_tool_refusal(tool_name: str, content: str | None) -> str | None:
    """Remember a module that refused the member; return it, ``None`` when not one or the guard is off.

    The run's later calls to that module's tools are answered without being
    sent (:func:`refused_module`), and the refusal is an expected outcome, not
    a friction of the run (the member never signed in to the module).
    """
    refused = _refused.get()
    if refused is None:
        return None
    module = module_of_tool(tool_name)
    if module is None or not is_account_refusal(module, content):
        return None
    refused.add(module)
    return module


def refused_module(tool_name: str) -> str | None:
    """The module of *tool_name* when it already refused the member in this run, else ``None``."""
    refused = _refused.get()
    if not refused:
        return None
    module = module_of_tool(tool_name)
    return module if module in refused else None


def refusal_answer(module: str) -> str:
    """The answer a call to a module that already refused the member gets instead of being sent."""
    name = MODULE_LABELS.get(module, module)
    return (
        f"Not sent: {name} already refused this member in this run ({NO_MODULE_ACCOUNT}: "
        f"no {name} account). Do not call {name} tools again in this run; continue with "
        f"what the other sources give and say the member must sign in to {name} once."
    )


__all__ = [
    "ANY_CTEM_MODULE",
    "MODULE_LABELS",
    "NO_MODULE_ACCOUNT",
    "STAGE_NEEDS",
    "PreflightVerdict",
    "classify_status",
    "decide",
    "forget",
    "guarding_module_accounts",
    "is_account_refusal",
    "module_of_tool",
    "needed_modules",
    "note_tool_refusal",
    "preflight",
    "probe_identity",
    "read_states",
    "refusal_answer",
    "refused_module",
    "stage_need",
    "unmet_modules",
]
