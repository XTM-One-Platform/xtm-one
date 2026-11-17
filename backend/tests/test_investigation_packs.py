"""Subject classification and source-pack planning.

Classification is deterministic on purpose (see the module docstring of
``investigation_packs``), which is what makes it testable at all: a wrong
subject type sends the whole investigation at the wrong sources, and a regex
that mis-fires is a bug we can pin here rather than a model that occasionally
guesses.
"""

import pytest

from app.services.investigation_packs import (
    WEB_GENERAL,
    SourceDefinition,
    SourcePack,
    UnknownSourcePackError,
    classify_subject,
    get_pack,
    plan_for,
    recommend_packs,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("8.8.8.8", "ip"),
        ("185.220.101.42", "ip"),
        ("2001:4860:4860::8888", "ip"),
        ("evil.example.com", "domain"),
        ("example.co.uk", "domain"),
        ("https://evil.example.com/payload", "url"),
        ("http://1.2.3.4/x", "url"),
        ("CVE-2024-1234", "cve"),
        ("cve-2024-1234567", "cve"),
        ("d41d8cd98f00b204e9800998ecf8427e", "hash"),
        ("da39a3ee5e6b4b0d3255bfef95601890afd80709", "hash"),
        ("e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855", "hash"),
        ("APT29", "unknown"),
        ("Cozy Bear", "unknown"),
        ("", "unknown"),
        ("   ", "unknown"),
    ],
)
def test_classify_subject(value, expected):
    assert classify_subject(value) == expected


def test_a_hex_string_of_the_wrong_length_is_not_a_hash():
    """A near-miss hash must not be sent at hash-only sources."""
    assert classify_subject("a" * 39) == "unknown"
    assert classify_subject("a" * 33) == "unknown"


def test_a_url_is_classified_before_the_domain_it_contains():
    assert classify_subject("https://example.com") == "url"


def test_a_bare_hostname_without_a_tld_is_unknown():
    """`localhost` or a partial name must not be treated as a domain."""
    assert classify_subject("localhost") == "unknown"
    assert classify_subject("example.") == "unknown"


def test_source_url_is_percent_encoded():
    source = SourceDefinition(name="X", url_template="https://x.test/?q={value}", subject_types=("unknown",))
    assert source.build_url("a b/c&d") == "https://x.test/?q=a%20b%2Fc%26d"


def test_sources_for_filters_by_subject_type():
    cve_sources = {s.name for s in WEB_GENERAL.sources_for("cve")}
    assert "NVD" in cve_sources
    assert "Shodan InternetDB" not in cve_sources


def test_the_host_transform_lets_a_url_reach_host_keyed_sources():
    """Without it a URL subject would have no applicable source at all."""
    (source,) = [s for s in WEB_GENERAL.sources_for("url") if s.name == "RDAP registration (host)"]
    assert source.build_url("https://evil.example.com/a/b?c=d") == ("https://rdap.org/domain/evil.example.com")


def test_the_host_transform_falls_back_to_the_value_for_a_bare_hostname():
    """A bare name has no netloc; producing an empty query would be worse."""
    source = SourceDefinition(
        name="X",
        url_template="https://x.test/{value}",
        subject_types=("url",),
        value_transform="host",
    )
    assert source.build_url("evil.example.com") == "https://x.test/evil.example.com"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("APT29", "apt29"),
        ("Cozy Bear", "cozy_bear"),
        ("  Sandworm Team  ", "sandworm_team"),
        ("win.cobalt_strike", "win_cobalt_strike"),
        ("--APT-29--", "apt_29"),
    ],
)
def test_the_slug_transform_matches_malpedias_url_shape(value, expected):
    """Malpedia addresses actors as `/actor/apt29`; a raw name 404s."""
    source = SourceDefinition(
        name="Malpedia",
        url_template="https://malpedia.test/actor/{value}",
        subject_types=("unknown",),
        value_transform="slug",
    )
    assert source.build_url(value) == f"https://malpedia.test/actor/{expected}"


def test_a_subject_the_transform_empties_is_not_planned_on_that_source():
    """The slug of an all-punctuation name is empty: querying would fetch the
    source's bare root (the Malpedia index) and could report it as found."""
    slugged = SourceDefinition(
        name="Malpedia",
        url_template="https://malpedia.test/actor/{value}",
        subject_types=("unknown",),
        value_transform="slug",
    )
    raw = SourceDefinition(name="Raw", url_template="https://x.test/?q={value}", subject_types=("unknown",))
    pack = SourcePack(slug="p", label="p", description="", subject_types=("unknown",), sources=(slugged, raw))

    assert not slugged.can_query("?!--")
    assert slugged.can_query("APT29")
    planned, executable = plan_for([pack], "unknown", "?!--")
    assert [p.source_name for p in planned] == ["Raw"]
    assert [s.name for _slug, s in executable] == ["Raw"]
    # Without a subject the plan is the pack's, as before.
    assert len(plan_for([pack], "unknown")[0]) == 2


def test_a_hash_subject_has_no_source_in_the_general_pack():
    """Recorded deliberately: no keyless GET endpoint covers file hashes.

    An empty plan that says so beats querying sources that cannot answer; the
    integration-backed packs of the full feature are what cover hashes.
    """
    assert WEB_GENERAL.sources_for("hash") == ()


def test_an_unclassified_subject_still_gets_a_non_empty_plan():
    """A free-text actor name must not produce an investigation that does nothing."""
    planned, executable = plan_for(recommend_packs("unknown"), "unknown")
    assert planned
    assert len(planned) == len(executable)


def test_plan_for_returns_parallel_sequences_in_order():
    planned, executable = plan_for([WEB_GENERAL], "hash")
    assert [p.source_name for p in planned] == [s.name for _slug, s in executable]
    assert all(p.source_pack_slug == "web-general" for p in planned)


def test_two_sources_sharing_a_template_but_not_a_transform_both_survive():
    """Collapsing them on the template alone would silently drop one."""
    raw = SourceDefinition(name="Raw", url_template="https://x.test/{value}", subject_types=("url",))
    hosted = SourceDefinition(
        name="Hosted",
        url_template="https://x.test/{value}",
        subject_types=("url",),
        value_transform="host",
    )
    pack = SourcePack(
        slug="p",
        label="p",
        description="",
        subject_types=("url",),
        sources=(raw, hosted),
    )
    planned, _executable = plan_for([pack], "url")
    assert [p.source_name for p in planned] == ["Raw", "Hosted"]


def test_a_source_shared_by_two_packs_is_planned_once_under_the_first():
    """Querying one URL twice would burn an iteration and show a duplicate step."""
    shared = SourceDefinition(name="Shared", url_template="https://shared.test/{value}", subject_types=("ip",))
    first = SourcePack(
        slug="first",
        label="First",
        description="",
        subject_types=("ip",),
        sources=(shared,),
    )
    second = SourcePack(
        slug="second",
        label="Second",
        description="",
        subject_types=("ip",),
        sources=(shared,),
    )
    planned, executable = plan_for([first, second], "ip")
    assert len(planned) == 1
    assert planned[0].source_pack_slug == "first"
    assert executable[0][0] == "first"


def test_get_pack_refuses_an_unknown_slug():
    """Substituting another pack would make the persisted slug list a lie."""
    with pytest.raises(UnknownSourcePackError):
        get_pack("does-not-exist")


def test_recommend_packs_never_returns_empty():
    for subject_type in ("ip", "cve", "threat_actor", "unknown"):
        assert recommend_packs(subject_type)


def test_recommend_packs_orders_most_specific_first():
    from app.services import investigation_packs as packs

    narrow = SourcePack(slug="narrow", label="Narrow", description="", subject_types=("ip",))
    original = dict(packs.BUILTIN_PACKS)
    packs.BUILTIN_PACKS["narrow"] = narrow
    try:
        assert recommend_packs("ip")[0].slug == "narrow"
    finally:
        packs.BUILTIN_PACKS.clear()
        packs.BUILTIN_PACKS.update(original)


# ---------------------------------------------------------------------------
# Subject classification
#
# A misclassification is silent and expensive: the run is sent at sources that
# cannot answer, and the analyst sees a page of `empty` steps rather than an
# error. `_DOMAIN_RE` is conservative on purpose, and these pin the edges of
# that intent rather than leaving them to the luck of an optional group.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "subject",
    [
        "evil.com",
        "sub.evil.com",
        "a.b.c.example.co.uk",
        "xn--80ak6aa92e.com",  # punycode
        "EVIL.COM",
    ],
)
def test_these_are_domains(subject):
    assert classify_subject(subject) == "domain"


@pytest.mark.parametrize(
    "subject",
    [
        "..evil.com",  # empty label
        ".evil.com",  # leading dot
        "evil.com.",  # trailing dot — valid in DNS, not a subject we accept
        "evil..com",
        "-evil.com",  # label may not start with a hyphen
        "evil-.com",  # ...nor end with one
        "evil.c",  # one-character TLD
        "evil.123",  # numeric TLD
        "evil",  # no dot at all
        "evil.com/path",
        "evil com",
        "",
        "." * 5,
        "a" * 64 + ".com",  # label over 63 characters
        ("a" * 63 + ".") * 4 + "com",  # name over 253 characters
    ],
)
def test_these_are_not_domains(subject):
    assert classify_subject(subject) != "domain", (
        f"{subject!r} classified as a domain; a wrong type spends the whole "
        "budget on sources that cannot answer"
    )


def test_the_three_rdap_sources_are_distinguishable_by_name():
    """They are never in one run — `plan_for` filters by the run's single
    subject type — but the same name against three different endpoints is
    still ambiguous to anyone comparing two runs."""
    names = {
        s.name
        for s in WEB_GENERAL.sources
        if s.url_template.startswith("https://rdap.org/")
    }
    assert len(names) == 3, f"RDAP sources must not share a display name: {names}"
