"""Stub the background gate for tests that are not about it.

Every background entry point - channel messages, ``trigger_assignment``, the
scheduler tick and fan-out, flow hold-backs, run and task starts, wakes, the
queue drain, worker jobs - resolves ``services.agentic_gate.agentic_refusal``
(or, where a refusal would fail work, ``agentic_verdict``) at call time, so
patching those two attributes is enough. Without it a test drives the licence
read, which needs a database the unit suite does not have. The gate itself is
covered in ``test_background_edition_gate.py``.
"""

from __future__ import annotations

GATE = "app.services.agentic_gate.agentic_refusal"
VERDICT = "app.services.agentic_gate.agentic_verdict"


async def agentic_ai_available(_db) -> None:
    """A drop-in for the gate that always lets agentic AI run."""
    return None


def stub_agentic_gate(
    monkeypatch, refusal: str | None = None, *, transient: bool = False
) -> None:
    """Make every background gate check answer ``refusal`` (``None``: AI may run).

    ``transient`` marks the refusal as one nothing was read for, as a database
    error on a cold worker produces.
    """
    from app.services.agentic_gate import AgenticVerdict

    async def _refusal(_db):
        return refusal

    async def _verdict(_db):
        return AgenticVerdict(refusal, transient=transient and refusal is not None)

    monkeypatch.setattr(GATE, _refusal)
    monkeypatch.setattr(VERDICT, _verdict)
