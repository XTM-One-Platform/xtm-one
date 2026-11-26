"""Neutralising the lab targets of an OpenAEV scenario export (``export_targets``).

The Scenario Publisher commits the export to the public ``scenarios`` catalogue:
the archive that leaves XTM One must name only example hosts, while every other
member and every member's zip metadata stay as the platform wrote them.
"""

from __future__ import annotations

import io
import json
import zipfile

import pytest

from app.integrations.providers.openaev.export_targets import (
    EXAMPLE_HOST,
    EXAMPLE_IPV4,
    EXAMPLE_IPV6,
    EXAMPLE_SENDER,
    ExportNeutraliseError,
    host_is_allowed,
    neutralise_export,
    neutralise_scenario,
)


def _nuclei(targets: str, **extra) -> dict:
    return {
        "inject_title": extra.pop("title", "Scan"),
        "inject_description": extra.pop("description", ""),
        "inject_content": {
            "target_selector": "manual",
            "targets": targets,
            "template_url": "https://raw.githubusercontent.com/projectdiscovery/nuclei-templates/main/x.yaml",
            **extra,
        },
    }


def _http(uri: str) -> dict:
    return {"inject_title": "Query", "inject_content": {"method": "GET", "uri": uri}}


def _scenario(*injects: dict, mail_from: str = "openaev@example.com", description: str = "") -> dict:
    return {
        "export_version": 1,
        "scenario_information": {
            "scenario_name": "Lab",
            "scenario_description": description,
            "scenario_mail_from": mail_from,
        },
        "scenario_injects": list(injects),
    }


def _content(scenario: dict, index: int = 0) -> dict:
    return scenario["scenario_injects"][index]["inject_content"]


# ── Scenario rewriting ─────────────────────────────────────────────────────


def test_nuclei_manual_target_becomes_the_example_host():
    scenario = _scenario(_nuclei("honey.scanme.sh"))
    assert neutralise_scenario(scenario) == 1
    assert _content(scenario)["targets"] == EXAMPLE_HOST
    # The template URL is no target: it stays.
    assert _content(scenario)["template_url"].startswith("https://raw.githubusercontent.com/")


def test_a_target_list_keeps_its_separators_and_url_parts():
    scenario = _scenario(_nuclei("a.real.com, 10.1.2.3\nhttps://b.real.org:8443/x"))
    assert neutralise_scenario(scenario) == 3
    assert _content(scenario)["targets"] == (f"{EXAMPLE_HOST}, {EXAMPLE_IPV4}\nhttps://{EXAMPLE_HOST}:8443/x")


@pytest.mark.parametrize(
    "target",
    [
        "host.example.com",
        "scanme.example.org",
        "lab.test",
        "localhost:8080",
        "127.0.0.1",
        "192.0.2.44",
        "198.51.100.7:22",
        "https://203.0.113.9/login",
        "[2001:db8::5]:443",
        "::1",
        "192.0.2.0/24",
    ],
)
def test_an_allowed_target_is_left_as_is(target):
    scenario = _scenario(_nuclei(target))
    assert neutralise_scenario(scenario) == 0
    assert _content(scenario)["targets"] == target


def test_ipv6_targets_become_the_documentation_address():
    scenario = _scenario(_nuclei("2a00:1450:4007::71 http://[2a00:1450::1]:8080/p"))
    assert neutralise_scenario(scenario) == 2
    assert _content(scenario)["targets"] == f"{EXAMPLE_IPV6} http://[{EXAMPLE_IPV6}]:8080/p"


def test_a_real_network_becomes_one_example_address():
    scenario = _scenario(_nuclei("10.0.0.0/8"))
    neutralise_scenario(scenario)
    assert _content(scenario)["targets"] == EXAMPLE_IPV4


def test_targets_are_read_only_for_the_manual_selector():
    inject = _nuclei("honey.scanme.sh")
    inject["inject_content"]["target_selector"] = "asset-groups"
    scenario = _scenario(inject)
    assert neutralise_scenario(scenario) == 0
    assert _content(scenario)["targets"] == "honey.scanme.sh"


def test_http_uri_keeps_everything_but_its_host():
    scenario = _scenario(_http("https://user@intranet.corp.real:8443/api/v1?q=1&r=a.b#frag"))
    assert neutralise_scenario(scenario) == 1
    assert _content(scenario)["uri"] == f"https://user@{EXAMPLE_HOST}:8443/api/v1?q=1&r=a.b#frag"


def test_a_url_inside_the_query_does_not_hide_the_host():
    scenario = _scenario(_nuclei("real.com/?next=https://example.com/"))
    assert neutralise_scenario(scenario) == 1
    assert _content(scenario)["targets"] == f"{EXAMPLE_HOST}/?next=https://example.com/"


def test_http_uri_on_an_example_host_is_left_as_is():
    scenario = _scenario(_http("http://www.example.com/path"))
    assert neutralise_scenario(scenario) == 0
    assert _content(scenario)["uri"] == "http://www.example.com/path"


def test_mail_from_outside_the_example_domains_is_replaced():
    scenario = _scenario(mail_from="red-team@corp.real.com")
    assert neutralise_scenario(scenario) == 1
    assert scenario["scenario_information"]["scenario_mail_from"] == EXAMPLE_SENDER


def test_mail_from_on_an_example_domain_is_kept():
    scenario = _scenario(mail_from="noreply@lab.example.org")
    assert neutralise_scenario(scenario) == 0
    assert scenario["scenario_information"]["scenario_mail_from"] == "noreply@lab.example.org"


def test_mail_from_at_the_top_level_is_read_too():
    scenario = {"scenario_mail_from": "x@real.io", "scenario_injects": []}
    assert neutralise_scenario(scenario) == 1
    assert scenario["scenario_mail_from"] == EXAMPLE_SENDER


def test_descriptions_and_titles_lose_only_the_neutralised_hosts():
    scenario = _scenario(
        _nuclei(
            "honey.scanme.sh 10.1.2.3",
            title="Scan honey.scanme.sh",
            description="Hits honey.scanme.sh and 10.1.2.3, not 110.1.2.34 nor other.real.net",
        ),
        description="Validates HONEY.scanme.sh exposure; see docs.example.com and sub.honey.scanme.shx.",
    )
    neutralise_scenario(scenario)
    inject = scenario["scenario_injects"][0]
    assert inject["inject_title"] == f"Scan {EXAMPLE_HOST}"
    assert inject["inject_description"] == (
        f"Hits {EXAMPLE_HOST} and {EXAMPLE_IPV4}, not 110.1.2.34 nor other.real.net"
    )
    assert scenario["scenario_information"]["scenario_description"] == (
        f"Validates {EXAMPLE_HOST} exposure; see docs.example.com and sub.honey.scanme.shx."
    )


def test_variables_are_never_touched():
    scenario = _scenario(
        _nuclei("#{target}, ${host}:8080 https://{{lab}}/x real.com:#{port}"),
        _http("https://#{target}/login"),
        mail_from="#{sender}",
        description="Targets #{target} then real.com",
    )
    assert neutralise_scenario(scenario) == 1
    assert _content(scenario)["targets"] == (
        f"#{{target}}, ${{host}}:8080 https://{{{{lab}}}}/x {EXAMPLE_HOST}:#{{port}}"
    )
    assert _content(scenario, 1)["uri"] == "https://#{target}/login"
    assert scenario["scenario_information"]["scenario_mail_from"] == "#{sender}"
    assert scenario["scenario_information"]["scenario_description"] == (f"Targets #{{target}} then {EXAMPLE_HOST}")


@pytest.mark.parametrize(
    ("host", "allowed"),
    [
        ("example", True),
        ("a.b.example.net", True),
        ("x.invalid", True),
        ("notexample.com", False),
        ("example.com.evil.io", False),
        ("127.8.9.10", True),
        ("::ffff:192.0.2.1", True),
        ("10.0.0.1", False),
        ("2001:db9::1", False),
        ("exämple.com", False),
    ],
)
def test_host_is_allowed(host, allowed):
    assert host_is_allowed(host) is allowed


# ── Archive rewriting ──────────────────────────────────────────────────────


def _archive(scenario: dict, *, comment: bytes = b"Scenario", name: str = "Lab.json") -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.comment = b"OpenAEV export"
        info = zipfile.ZipInfo(name, date_time=(2026, 11, 2, 10, 30, 0))
        info.comment = comment
        info.compress_type = zipfile.ZIP_DEFLATED
        archive.writestr(info, json.dumps(scenario))
        doc = zipfile.ZipInfo("documents/report.pdf", date_time=(2025, 1, 1, 0, 0, 0))
        doc.comment = b"Document"
        archive.writestr(doc, b"%PDF-1.7 honey.scanme.sh stays in a document")
        archive.writestr("notes.txt", "plain member", compress_type=zipfile.ZIP_STORED)
    return out.getvalue()


def _members(data: bytes) -> dict[str, tuple[zipfile.ZipInfo, bytes]]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return {info.filename: (info, archive.read(info)) for info in archive.infolist()}


def test_archive_scenario_member_is_rewritten_and_the_rest_kept():
    original = _archive(_scenario(_nuclei("honey.scanme.sh")))
    result = neutralise_export(original)
    assert result.replaced == 1
    before, after = _members(original), _members(result.data)
    assert list(before) == list(after)
    scenario = json.loads(after["Lab.json"][1])
    assert _content(scenario)["targets"] == EXAMPLE_HOST
    assert after["Lab.json"][0].comment == b"Scenario"
    for name in ("documents/report.pdf", "notes.txt"):
        assert after[name][1] == before[name][1]
    for name in before:
        old, new = before[name][0], after[name][0]
        assert (new.comment, new.date_time, new.compress_type, new.external_attr) == (
            old.comment,
            old.date_time,
            old.compress_type,
            old.external_attr,
        )
    with zipfile.ZipFile(io.BytesIO(result.data)) as archive:
        assert archive.comment == b"OpenAEV export"
        assert archive.testzip() is None


def test_archive_falls_back_to_the_single_json_member():
    original = _archive(_scenario(_http("http://lab.real.net/")), comment=b"")
    result = neutralise_export(original)
    assert result.replaced == 1
    assert (
        json.loads(_members(result.data)["Lab.json"][1])["scenario_injects"][0]["inject_content"]["uri"]
        == f"http://{EXAMPLE_HOST}/"
    )


def test_archive_with_nothing_to_neutralise_is_returned_as_is():
    original = _archive(_scenario(_nuclei("host.example.com")))
    result = neutralise_export(original)
    assert result.replaced == 0
    assert result.data == original


@pytest.mark.parametrize(
    "data",
    [b"PK\x03\x04-not-a-zip", b"{}"],
)
def test_unreadable_export_is_refused(data):
    with pytest.raises(ExportNeutraliseError):
        neutralise_export(data)


def test_archive_without_a_scenario_member_is_refused():
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("a.json", "{}")
        archive.writestr("b.json", "{}")
    with pytest.raises(ExportNeutraliseError):
        neutralise_export(out.getvalue())
