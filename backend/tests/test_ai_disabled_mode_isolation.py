"""``ai_disabled`` must not be mistaken for Copilot.

The critical defect this file exists for: ``is_xtm_one`` is
``platform_mode == "xtm_one"``, and nine call sites used ``not is_xtm_one`` to
mean "copilot". Adding a fourth value silently flipped all of them — the worst
being the OpenAI-compatible ``/v1`` proxy, which is mounted on ``not
is_xtm_one`` and would have given a direct LLM egress path to the one mode
whose entire purpose is that none exists.
"""

from __future__ import annotations

import pathlib

import pytest

from app.core.config import Settings

BACKEND = pathlib.Path(__file__).resolve().parent.parent


@pytest.mark.parametrize(
    ("mode", "expected"),
    [("xtm_one", False), ("copilot", True), ("dev", True), ("ai_disabled", False)],
)
def test_copilot_like_excludes_ai_disabled(mode, expected):
    assert Settings(platform_mode=mode).is_copilot_like is expected


@pytest.mark.parametrize(
    "flag",
    ["disable_code_interpreter", "disable_custom_tools", "disable_custom_variables"],
)
def test_ai_disabled_gets_the_restrictive_defaults(flag):
    """These resolved from ``is_xtm_one``, so ai_disabled inherited the
    *permissive* branch: sandboxed code execution and custom tools defaulted on
    in the most locked-down mode."""
    unset = dict.fromkeys(
        ["disable_code_interpreter", "disable_custom_tools", "disable_custom_variables"]
    )
    assert getattr(Settings(platform_mode="ai_disabled", **unset), flag) is True
    assert getattr(Settings(platform_mode="xtm_one", **unset), flag) is True
    assert getattr(Settings(platform_mode="copilot", **unset), flag) is False


def test_platform_mode_partition_is_exhaustive():
    """Every mode must land in exactly one camp — the invariant that broke.

    ``is_copilot_like`` fixed ``not is_xtm_one``, but ``is_xtm_one`` itself
    stayed ``== "xtm_one"``, leaving ``ai_disabled`` in *neither* camp: all 35
    positive readers fell to an ``else`` meaning "Copilot". Reading the allowed
    set from ``PLATFORM_MODES``, the validator's allow-list, makes a fifth mode
    fail until it is classified, which the previous regex guard could not do.
    """
    from app.core.config import PLATFORM_MODES

    modes = PLATFORM_MODES
    assert len(modes) >= 4, modes

    unclassified = []
    for mode in modes:
        s = Settings(platform_mode=mode)
        if s.is_xtm_one == s.is_copilot_like:
            unclassified.append(
                f"{mode}: is_xtm_one={s.is_xtm_one} is_copilot_like={s.is_copilot_like}"
            )
    assert not unclassified, (
        "each mode must be exactly one of XTM One or Copilot-like; these are in "
        f"neither camp (or both), so every `else` branch misreads them: {unclassified}"
    )


def test_ai_disabled_is_an_xtm_one_platform():
    """Product identity is not the AI axis.

    ``ai_disabled`` must answer True for the ~35 reads meaning "which product
    is this" — branding, email, the OpenCTI/OpenAEV/OpenCRQ suite, licence
    acceptance, quota ceilings, built-in write protection.
    """
    assert Settings(platform_mode="ai_disabled").is_xtm_one is True
    assert Settings(platform_mode="copilot").is_xtm_one is False
    assert Settings(platform_mode="dev").is_xtm_one is False


def test_product_identity_does_not_decide_seeding():
    """The safety property that makes the classification above harmless.

    Each seeder is *passed* a ``platform_mode`` and compares it itself, so it
    stays ignorant of this feature; ``content_mode()`` chooses which mode to
    pass. Neither reads ``is_xtm_one`` — if one did, an ``ai_disabled`` boot
    would seed the agents it is meant to omit.

    An earlier version of this test asserted on ``Settings.should_seed_xtm``,
    which no seeder ever read: it duplicated their inline tuple, so the test
    documented a mechanism that did not exist. The property is gone.
    """
    assert not hasattr(Settings(platform_mode="xtm_one"), "should_seed_xtm"), (
        "should_seed_xtm is back — wire it to the seeders or drop it again, but "
        "do not let a test imply it decides anything"
    )

    offenders = [
        f"{f.relative_to(BACKEND)}"
        for f in (BACKEND / "app" / "agents").rglob("*.py")
        if "is_xtm_one" in f.read_text(encoding="utf-8")
    ]
    assert not offenders, (
        "seeders must gate on the platform_mode they are passed, not on product "
        f"identity: {offenders}"
    )


def test_no_code_reads_not_is_xtm_one():
    """Keep one spelling for "copilot-like".

    Now that the partition is exhaustive this idiom is merely redundant rather
    than wrong, but it is the spelling that hid the original nine-site defect,
    so it stays banned: ``is_copilot_like`` says what it means.
    """
    # Matched on the *attribute*, not on two literal spellings. The first
    # version of this test looked for "not settings.is_xtm_one" and
    # "not self.is_xtm_one", and missed `not _app_settings.is_xtm_one` in the
    # scheduler — which sent AI-adoption metrics to external vendors on the
    # mode that must make no outbound AI calls at all.
    import re

    pattern = re.compile(r"not\s+[\w.]*\bis_xtm_one\b")
    offenders = []
    for f in (BACKEND / "app").rglob("*.py"):
        for line in f.read_text(encoding="utf-8").splitlines():
            match = pattern.search(line)
            if match is None:
                continue
            before = line[: match.start()]
            # Prose naming the idiom in order to forbid it is not a use of it.
            if "#" in before or "``" in before:
                continue
            offenders.append(f"{f.relative_to(BACKEND)}: {line.strip()}")
    assert not offenders, (
        "these treat any non-xtm_one mode as Copilot, which now includes "
        f"ai_disabled: {offenders}"
    )


def test_the_llm_proxy_is_not_mounted_for_ai_disabled():
    """The specific consequence worth naming: a direct LLM egress path on the
    deployment that must have none."""
    source = (BACKEND / "app" / "main.py").read_text(encoding="utf-8")
    mount = source.index('include_router(llm_proxy.router')
    guard = source.rindex("if ", 0, mount)
    assert "is_copilot_like" in source[guard:mount], (
        "the /v1 LLM proxy must mount only for copilot-like modes"
    )


def test_ai_disabled_needs_no_licence():
    """FR-031, confirmed 2026-08-26. The licence gate exists for copilot mode;
    reading it as "not xtm_one" made a licence mandatory in the one mode
    documented as needing none, so the mode was unusable out of the box.
    """
    source = (BACKEND / "app" / "main.py").read_text(encoding="utf-8")
    # Anchored inside the class: several middlewares open with the same
    # ``scope["type"]`` check, and the first one in the file is a different gate.
    cls = source.index("class LicenseGateMiddleware")
    body = source[cls : source.index("\nclass ", cls + 1)]
    guard = body.index('if scope["type"] != "http"')
    assert "not settings.is_copilot_like" in body[guard : body.index("\n", guard)]


def test_ai_disabled_does_not_render_the_licence_wall():
    """The worst consequence of the mode landing in neither camp.

    The Copilot branch of ``/platform/config`` returns
    ``license_required=True``, so while ``ai_disabled`` fell into it an
    unlicensed boot replaced every route with ``<LicenseRequired />`` — the
    ``LicenseGateMiddleware`` carve-out could not help, the wall is
    client-side. The branch must keep reading the predicate, not ``==``.
    """
    source = (BACKEND / "app" / "api" / "v1" / "platform.py").read_text(
        encoding="utf-8"
    )
    endpoint = source.index("async def get_platform_config")
    body = source[endpoint : source.index("\n@router", endpoint)]

    assert "is_xtm = settings.is_xtm_one" in body, (
        "the platform-config branch must read is_xtm_one, which classifies "
        "ai_disabled as XTM One; a strict mode comparison redraws the wall"
    )
    # And the wall itself stays on the far side of that branch.
    assert body.count("license_required=True") == 1
    assert body.index("license_required=True") > body.index("if is_xtm:")
    assert Settings(platform_mode="ai_disabled").is_xtm_one is True
