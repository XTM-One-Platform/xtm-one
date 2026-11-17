"""Test connection: what the platform can do with a vault provider.

Each step is run in order and reported with its reason code and the vault's
detail (an HTTP status, an AWS error code) when it fails, never a value: the
configuration builds, the platform signs in, it can list secrets, it can read
one. The read step reads the first secret the listing finds and drops it at
once; with nothing listable, it is reported as not run.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Literal

from app.services.vault.base import ListedSecret, VaultProvider
from app.services.vault.config import VaultConfigError, VaultProviderConfig
from app.services.vault.errors import REASON_DESCRIPTIONS, VaultReadError

Step = Literal["configuration", "login", "list", "read"]
#: The steps run against the vault, in order, once the configuration builds.
_VAULT_STEPS: tuple[Step, ...] = ("login", "list", "read")

#: How long a whole test may take (each request to the vault has its own 10 s bound).
TEST_TIMEOUT_SECONDS = 45.0
#: How many folders the read step opens at most looking for a secret to read.
_MAX_FOLDERS_OPENED = 3


@dataclass(frozen=True)
class CheckStep:
    step: Step
    #: ``None``: not run, because an earlier step failed or there was nothing to read.
    ok: bool | None
    reason: str | None = None
    description: str | None = None
    detail: str | None = None


@dataclass
class ConnectionReport:
    steps: list[CheckStep] = field(default_factory=list)
    #: HashiCorp only: the KV mounts the token sees, for the mount field.
    mounts: list[dict[str, str]] | None = None

    @property
    def ok(self) -> bool:
        return all(step.ok is not False for step in self.steps) and any(step.ok for step in self.steps)

    @property
    def failure(self) -> CheckStep | None:
        return next((step for step in self.steps if step.ok is False), None)


def _failed(step: Step, exc: VaultReadError) -> CheckStep:
    return CheckStep(step=step, ok=False, reason=exc.reason, description=REASON_DESCRIPTIONS.get(exc.reason), detail=exc.detail or None)


def _as_read_error(exc: Exception) -> VaultReadError:
    if isinstance(exc, VaultReadError):
        return exc
    if isinstance(exc, TimeoutError):
        return VaultReadError("unreachable", "the vault did not answer in time")
    return VaultReadError("unreachable", type(exc).__name__)


async def _first_secret(provider: VaultProvider, entries: list[ListedSecret]) -> str | None:
    """The path of a secret to read: the first listed, else the first one found in the first folders."""
    folders: list[str] = []
    for entry in entries:
        if not entry.folder:
            return entry.path
        folders.append(entry.path)
    opened = 0
    while folders and opened < _MAX_FOLDERS_OPENED:
        opened += 1
        listing = await provider.list_secrets(folders.pop(0), page_size=20)
        for entry in listing.entries:
            if not entry.folder:
                return entry.path
        folders.extend(entry.path for entry in listing.entries if entry.folder)
    return None


async def _run(provider: VaultProvider, report: ConnectionReport) -> None:
    try:
        await provider.authenticate()
    except Exception as exc:
        report.steps.append(_failed("login", _as_read_error(exc)))
        report.steps.extend(CheckStep(step=step, ok=None) for step in ("list", "read"))
        return
    report.steps.append(CheckStep(step="login", ok=True))
    mounts = getattr(provider, "mounts", None)
    if callable(mounts):
        try:
            report.mounts = await mounts()
        except Exception:
            report.mounts = None
    try:
        listing = await provider.list_secrets("", page_size=20)
    except Exception as exc:
        report.steps.append(_failed("list", _as_read_error(exc)))
        report.steps.append(CheckStep(step="read", ok=None))
        return
    report.steps.append(CheckStep(step="list", ok=True))
    try:
        path = await _first_secret(provider, listing.entries)
    except Exception:
        path = None
    if path is None:
        report.steps.append(CheckStep(step="read", ok=None, detail="no secret was found to read"))
        return
    try:
        await provider.read(path)
    except Exception as exc:
        report.steps.append(_failed("read", _as_read_error(exc)))
        return
    report.steps.append(CheckStep(step="read", ok=True))


async def test_connection(config: VaultProviderConfig, provider: VaultProvider | None = None) -> ConnectionReport:
    """Test *config*: with the process's own *provider* when given (its login is reused), else a fresh one closed after."""
    from app.services.vault.resolver import build_provider

    report = ConnectionReport()
    owned = provider is None
    if provider is None:
        try:
            provider = build_provider(config)
        except VaultConfigError as exc:
            report.steps.append(CheckStep(step="configuration", ok=False, reason="misconfigured", description=REASON_DESCRIPTIONS["misconfigured"], detail=str(exc)))
            return report
    report.steps.append(CheckStep(step="configuration", ok=True))
    try:
        async with asyncio.timeout(TEST_TIMEOUT_SECONDS):
            await _run(provider, report)
    except TimeoutError:
        reported = {step.step for step in report.steps}
        pending = [step for step in _VAULT_STEPS if step not in reported]
        if pending:
            report.steps.append(_failed(pending[0], _as_read_error(TimeoutError())))
            report.steps.extend(CheckStep(step=step, ok=None) for step in pending[1:])
    finally:
        if owned:
            await provider.aclose()
    return report
