"""Golden-file guard for the derived platform read whitelist.

The shipped whitelist is a hand-written constant, and its module says so: "this
file is data, and is reviewed as data". Deriving the platform half from the tool
classifier buys accuracy across releases and gives that property up — adding
``list_opencti_widgets`` to a provider module would exempt it from human
approval on every deployment, with nothing in the diff to review.

This test buys the property back. The snapshot below is the effective derived
set, checked in, so widening it is impossible without an explicit diff line
saying which tool now runs unattended. When it fails, read the reported names
and decide whether each one should really skip approval; only then update the
snapshot.

It is a *review* gate, not a judgement: the snapshot is what we ship, and this
test has no opinion about whether it is right.
"""

from pathlib import Path

import pytest

from app.services.hitl_whitelist_platform import (
    HAND_GATED,
    PLATFORM_PROVIDERS,
    platform_read_tools,
    provider_of,
    read_tools_for,
)

_SNAPSHOT = Path(__file__).with_name("data") / "hitl_platform_read_whitelist.txt"


def _snapshot() -> dict[str, set[str]]:
    """Parse the checked-in snapshot into ``{provider: names}``."""
    by_provider: dict[str, set[str]] = {}
    for line in _SNAPSHOT.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        provider, _, name = line.partition(":")
        by_provider.setdefault(provider, set()).add(name)
    return by_provider


@pytest.fixture(scope="module")
def derived() -> dict[str, frozenset[str]]:
    tools = platform_read_tools()
    # Guard the guard. Every provider builder is called with a dummy token; if
    # that stops producing definitions, every assertion below would pass
    # vacuously while the product silently gated every platform read.
    assert set(tools) == set(PLATFORM_PROVIDERS)
    for provider, names in tools.items():
        assert len(names) > 10, f"{provider} introspection looks broken: {names}"
    return tools


def test_derived_set_matches_the_reviewed_snapshot(derived):
    expected = _snapshot()
    added = {
        provider: sorted(names - expected.get(provider, set()))
        for provider, names in derived.items()
        if names - expected.get(provider, set())
    }
    dropped = {
        provider: sorted(expected.get(provider, set()) - names)
        for provider, names in derived.items()
        if expected.get(provider, set()) - names
    }
    assert not added, (
        "These platform tools would now skip human approval without anyone "
        f"having reviewed that decision: {added}. If each one really is a "
        "read that may run unattended, add it to "
        f"{_SNAPSHOT.name}; if not, name it in HAND_GATED with the reason."
    )
    assert not dropped, (
        "These platform tools no longer skip human approval — a rename, a "
        f"reclassification, or a removed tool: {dropped}. Update "
        f"{_SNAPSHOT.name} once you know which."
    )


def test_hand_gated_tools_are_excluded_everywhere(derived):
    """The human override actually overrides."""
    exempt = read_tools_for(frozenset(PLATFORM_PROVIDERS))
    still_exempt = sorted(HAND_GATED.keys() & exempt)
    assert not still_exempt, (
        f"HAND_GATED names are still exempt: {still_exempt}. Someone listed a "
        "reason to gate these and the derivation ignored it."
    )


def test_hand_gated_names_are_real_read_classified_tools():
    """A typo in HAND_GATED gates nothing and says nothing.

    An entry that matches no tool is invisible: the name it meant to re-gate
    stays exempt, and the reason recorded next to it reads as though the
    decision took effect.

    Every provider, not only the platform ones: ``hitl_whitelist_derived``
    applies this same set to the ~100 integration providers, so a name that is
    real there is doing real work even though no Suite platform declares it.
    """
    from app.api.v1.integration_catalog import build_dummy_token
    from app.services.integration_tools import PROVIDER_TOOLS
    from app.services.tool_classification import infer_tool_type
    from app.tools.base import CallableToolExecutor

    all_names: set[str] = set()
    for provider in PROVIDER_TOOLS:
        try:
            defs = PROVIDER_TOOLS[provider](
                build_dummy_token(provider),
                CallableToolExecutor(),
                refresh_fn=lambda: None,
            )
        except Exception:  # noqa: S112 - see comment
            # A builder that cannot be introspected here contributes no names;
            # it must not turn a real entry into a reported typo. Nothing to log:
            # the assertion below names every entry that went unmatched.
            continue
        all_names.update(d.name for d in defs)

    unknown = sorted(HAND_GATED.keys() - all_names)
    assert not unknown, f"HAND_GATED names no tool answers to: {unknown}"

    # A HAND_GATED entry for a tool the classifier already calls ``write`` is
    # dead weight that implies a decision nobody needs to keep making.
    redundant = sorted(k for k in HAND_GATED if infer_tool_type(k) != "read")
    assert not redundant, (
        f"HAND_GATED entries the classifier already gates: {redundant}. Drop "
        "them so the list only holds real overrides."
    )


def test_every_read_classified_download_tool_is_hand_gated():
    """The download family is gated whole, including tools added later.

    ``download_`` is a leading read verb, so a provider that gains a download
    tool derives it as an exempt read on every deployment that connects it.
    The snapshot shows that as a diff line; this makes it a failure, because
    regenerating the snapshot is the step most likely to wave it through.
    """
    from app.api.v1.integration_catalog import build_dummy_token
    from app.services.integration_tools import PROVIDER_TOOLS
    from app.services.tool_classification import infer_tool_type
    from app.tools.base import CallableToolExecutor

    downloads: set[str] = set()
    for provider, builder in PROVIDER_TOOLS.items():
        try:
            defs = builder(
                build_dummy_token(provider), CallableToolExecutor(), refresh_fn=lambda: None
            )
        except Exception:  # noqa: S112 - introspection only, as above
            continue
        downloads.update(
            d.name
            for d in defs
            if d.name
            and d.name.startswith("download_")
            and infer_tool_type(d.name) == "read"
        )

    assert downloads, "the guard is meaningless if no provider declares a download"
    ungated = sorted(downloads - HAND_GATED.keys())
    assert not ungated, (
        f"Read-classified download tools not in HAND_GATED: {ungated}. Add them "
        "with the download reason."
    )


def test_no_write_classified_tool_is_exempt(derived):
    """The whole safety claim, asserted directly.

    Everything this module exempts must be something the classifier calls a
    read. A regression here — a builder returning a name the classifier reads
    differently, a widened token set — would exempt a mutating tool on every
    deployment.
    """
    from app.services.tool_classification import infer_tool_type

    for provider, names in derived.items():
        writes = sorted(n for n in names if infer_tool_type(n) != "read")
        assert not writes, f"{provider} exempts write-classified tools: {writes}"


def test_manage_tools_stay_gated(derived):
    """The action-dispatch tools are the known blind spot; keep them out.

    ``manage_*`` tools take an ``action`` enum that mixes ``list``/``get`` with
    ``create``/``update``/``delete``, so no name-keyed exemption can be right
    about them. They classify ``write`` today; this pins that, because the day
    one slips through, its destructive actions run unattended.
    """
    exempt = read_tools_for(frozenset(PLATFORM_PROVIDERS))
    assert not sorted(n for n in exempt if n.startswith("manage_"))


def test_provider_attribution_covers_every_exempt_tool(derived):
    """Every exempt name resolves to a platform.

    The screen groups by provider and the bulk re-gate acts per provider, so a
    tool that answers ``None`` here would be exempt, listed without attribution
    and unreachable by the bulk control — exempt with no way to undo it.
    """
    for name in read_tools_for(frozenset(PLATFORM_PROVIDERS)):
        assert provider_of(name) in PLATFORM_PROVIDERS, name


def test_unregistered_platforms_exempt_nothing():
    """Scoping is by registration, so an unconnected platform adds no entries."""
    assert read_tools_for(frozenset()) == frozenset()
    assert read_tools_for(frozenset({"slack", "github"})) == frozenset()


def test_openaev_legacy_identifier_still_resolves():
    """``openbas`` is a registration identifier, not a provider key.

    ``hitl_whitelist._platform_providers`` expands the pair before calling in,
    so this asserts the contract that expansion relies on: the legacy name
    alone matches no provider, and the canonical one does.
    """
    assert read_tools_for(frozenset({"openbas"})) == frozenset()
    assert read_tools_for(frozenset({"openaev"}))


# --- A failed introspection must not be the answer for the process ---------


def test_a_failed_pass_is_not_memoised(monkeypatch):
    """The cache decided gating for the whole process from whichever request
    warmed it. A provider whose builder raised contributed an empty set — right
    on its own (over-gate, never under-gate) — but ``lru_cache`` froze that,
    so all of its derived reads asked for approval until the pod restarted.

    Only a complete pass is stored now; a partial one is returned and retried.
    """
    from app.services import hitl_whitelist_platform as mod

    monkeypatch.setattr(mod, "_READ_TOOLS_CACHE", None)
    calls = {"n": 0}
    real = mod._derive_platform_read_tools

    def _failing_once():
        calls["n"] += 1
        if calls["n"] == 1:
            return {p: frozenset() for p in mod.PLATFORM_PROVIDERS}, False
        return real()

    monkeypatch.setattr(mod, "_derive_platform_read_tools", _failing_once)

    assert mod.platform_read_tools() == {p: frozenset() for p in mod.PLATFORM_PROVIDERS}
    # The retry is the point: the second call re-derives rather than serving
    # the failure back.
    second = mod.platform_read_tools()
    assert calls["n"] == 2
    assert any(second.values()), "the successful pass should carry real tools"
    # ...and that one is kept.
    assert mod.platform_read_tools() is second
    assert calls["n"] == 2


def test_every_provider_gets_a_key_even_with_no_builder(monkeypatch):
    """``read_tools_for`` uses ``.get`` so both branches were safe, but the
    asymmetry invited a future ``[provider]`` lookup to raise."""
    from app.services import hitl_whitelist_platform as mod

    monkeypatch.setattr(mod, "_READ_TOOLS_CACHE", None)
    derived, complete = mod._derive_platform_read_tools()

    assert set(derived) == set(mod.PLATFORM_PROVIDERS)
    assert complete is True


def test_a_registration_identifier_governs_whatever_its_case():
    """Registration stores the identifier verbatim from the request body.

    A platform registered as "OpenCTI" spoke for the source
    ``integration:OpenCTI``, which no tool carries — so its policy governed
    nothing and the per-connection switch took over silently. The
    capability-update path normalises for the same reason.
    """
    from app.tools.approval import platform_identifiers

    assert platform_identifiers("OpenCTI") == {"opencti"}
    assert platform_identifiers("  OPENCTI ") == {"opencti"}
    # The legacy pair keeps resolving through the normalised key.
    assert platform_identifiers("OpenBAS") == {"openbas", "openaev"}
