# SPDX-License-Identifier: Apache-2.0

"""Neutralise the lab targets of an OpenAEV scenario export before it is published.

A scenario is tested on a lab that reaches a real host; the archive shared on a
public catalogue must name only example hosts. :func:`neutralise_export`
rewrites the scenario JSON of an export archive in memory:

* ``inject_content.uri`` (HTTP query injector): the URL's host becomes
  :data:`EXAMPLE_HOST`, its scheme, port, path, query and fragment kept;
* ``inject_content.targets`` when ``target_selector`` is ``manual`` (Nuclei,
  Nmap): each host of the list becomes :data:`EXAMPLE_HOST`, each address
  :data:`EXAMPLE_IPV4` / :data:`EXAMPLE_IPV6`, separators and URL parts kept;
* ``scenario_mail_from``: a sender outside the example domains becomes
  :data:`EXAMPLE_SENDER`;
* the scenario description and each inject's title and description: the exact
  hosts neutralised above, and only those, get the same replacement.

What may stay mirrors ``policy/data/allowed-targets.yaml`` of the catalogue
repository: the RFC 2606 / RFC 6761 example names with their subdomains,
loopback and the documentation ranges. That repository stays the authority (its
POL-003 check runs again on the pull request); this module only errs on the
side of replacing. Platform variables (``#{...}``, ``${...}``, ``{{...}}``) are
never touched.

Every other member of the archive, and every member's ``ZipInfo`` (comment
included), is kept; an archive with nothing to replace is returned as it came.
"""

from __future__ import annotations

import ipaddress
import json
import re
import zipfile
from dataclasses import dataclass
from io import BytesIO
from typing import Any

from app.core.archive_reading import ArchiveBudget, ArchiveTooLargeError

EXAMPLE_HOST = "host.example.com"
EXAMPLE_IPV4 = "192.0.2.10"
EXAMPLE_IPV6 = "2001:db8::10"
EXAMPLE_SENDER = "user@example.com"

#: The zip comment OpenAEV gives the scenario JSON member of an export.
SCENARIO_COMMENT = b"Scenario"

#: Example names (RFC 2606, RFC 6761), each with its subdomains.
_ALLOWED_DOMAINS = ("example", "example.com", "example.net", "example.org", "test", "invalid", "localhost")
#: Loopback and the documentation ranges (RFC 5737, RFC 3849).
_ALLOWED_NETWORKS = tuple(
    ipaddress.ip_network(net)
    for net in ("127.0.0.0/8", "::1/128", "192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24", "2001:db8::/32")
)

#: What the archive's members may expand to in all, and the scenario JSON alone.
_MAX_EXPANDED_BYTES = 256 * 1024 * 1024
_MAX_SCENARIO_JSON_BYTES = 32 * 1024 * 1024

_NAME = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_VARIABLE = re.compile(r"#\{[^}]*\}|\$\{[^}]*\}|\{\{[^}]*\}\}")
#: One target of a list: anything but a separator, a variable kept whole.
_TARGET = re.compile(r"(?:#\{[^}]*\}|\$\{[^}]*\}|\{\{[^}]*\}\}|[^,\s])+")
#: A URL's scheme, only at the start: a ``://`` further in (a query) starts no authority.
_SCHEME = re.compile(r"\s*[A-Za-z][A-Za-z0-9+.-]*://")
_PORT = re.compile(r"[0-9]*|#\{[^}]*\}|\$\{[^}]*\}|\{\{[^}]*\}\}")
#: An address range a scanner reads (``10.1.2.1-50``, ``10.1.*.1``): never a host name.
_IPV4_LIKE = re.compile(r"[0-9.*-]+")


class ExportNeutraliseError(ValueError):
    """The export cannot be read as a scenario archive, so it cannot be neutralised."""


@dataclass(frozen=True)
class NeutralisedExport:
    """The archive to deliver and how many target values were replaced in it."""

    data: bytes
    replaced: int


def host_is_allowed(host: str) -> bool:
    """Whether *host*, a name or an address, may be named by a published scenario."""
    host = host.strip().strip("[]").lower().removesuffix(".")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
            address = address.ipv4_mapped  # the IPv4 address it reaches
        return any(address in network for network in _ALLOWED_NETWORKS)
    if not _NAME.fullmatch(host):
        return False
    return any(host == domain or host.endswith(f".{domain}") for domain in _ALLOWED_DOMAINS)


def _network_is_allowed(text: str) -> bool:
    try:
        network = ipaddress.ip_network(text, strict=False)
    except ValueError:
        return False
    return any(
        network.version == allowed.version and network.subnet_of(allowed)  # type: ignore[arg-type]
        for allowed in _ALLOWED_NETWORKS
    )


def _replacement(host: str) -> str:
    """The example value that stands in for *host* (brackets excluded)."""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if ":" in host:
            return EXAMPLE_IPV6
        return EXAMPLE_IPV4 if _IPV4_LIKE.fullmatch(host) else EXAMPLE_HOST
    return EXAMPLE_IPV6 if address.version == 6 else EXAMPLE_IPV4


class _Neutraliser:
    """Rewrites one scenario, remembering each host it replaced and by what."""

    def __init__(self) -> None:
        self.hosts: dict[str, str] = {}
        self.replaced = 0

    def _authority_host(self, authority: str) -> tuple[int, int] | None:
        """The span of the host in *authority* (``[user@]host[:port]``), or None."""
        start = authority.rfind("@") + 1
        rest = authority[start:]
        if rest.startswith("["):
            end = rest.find("]")
            if end < 0:
                return None
            return start + 1, start + end
        if rest.count(":") > 1:  # an IPv6 address without brackets: no port
            return start, len(authority)
        host, colon, port = rest.partition(":")
        if colon and not _PORT.fullmatch(port):
            return None
        return start, start + len(host)

    def target(self, token: str) -> str:
        """*token* (a URL, ``host``, ``host:port``, an address or a network) with its host
        neutralised when it is not allowed; a variable host is left as it is."""
        scheme = _SCHEME.match(token)
        prefix_end = scheme.end() if scheme else 0
        body = token[prefix_end:]
        cut = len(body)
        for mark in "/?#":
            at = body.find(mark)
            if at >= 0:
                cut = min(cut, at)
        authority, tail = body[:cut], body[cut:]
        span = self._authority_host(authority)
        if span is None:
            return token
        host = authority[span[0] : span[1]]
        if not host or _VARIABLE.search(host) or host_is_allowed(host):
            return token
        if not scheme and tail.startswith("/") and tail[1:].isdigit():
            # A network (``10.0.0.0/8``) outside the allowed ranges: one example address,
            # never the same prefix around it, which would still span real hosts.
            if _network_is_allowed(f"{host}{tail}"):
                return token
            replacement = _replacement(host)
            self._record(host, replacement)
            return token[:prefix_end] + authority[: span[0]] + replacement + authority[span[1] :]
        replacement = _replacement(host)
        self._record(host, replacement)
        return token[:prefix_end] + authority[: span[0]] + replacement + authority[span[1] :] + tail

    def _record(self, host: str, replacement: str) -> None:
        self.hosts[host] = replacement
        self.replaced += 1

    def targets(self, text: str) -> str:
        """A target list (commas, spaces or new lines between the targets), each neutralised."""
        return _TARGET.sub(lambda match: self.target(match.group(0)), text)

    def sender(self, sender: str) -> str:
        _, at, domain = sender.rpartition("@")
        if not at or _VARIABLE.search(sender) or host_is_allowed(domain):
            return sender
        self.replaced += 1
        return EXAMPLE_SENDER

    def prose(self, text: str) -> str:
        """*text* with each exact occurrence of a replaced host replaced, variables kept."""
        if not self.hosts:
            return text
        pattern = self._prose_pattern()
        pieces: list[str] = []
        last = 0
        for variable in _VARIABLE.finditer(text):
            pieces.append(pattern.sub(self._prose_replacement, text[last : variable.start()]))
            pieces.append(variable.group(0))
            last = variable.end()
        pieces.append(pattern.sub(self._prose_replacement, text[last:]))
        return "".join(pieces)

    def _prose_pattern(self) -> re.Pattern[str]:
        # Longest first, so a host never loses to a shorter one it contains. A host is matched
        # only as a whole: never inside a longer name, address or number.
        alternatives = []
        for host in sorted(self.hosts, key=len, reverse=True):
            escaped = re.escape(host)
            try:
                ipaddress.ip_address(host)
            except ValueError:
                alternatives.append(rf"(?<![\w-]){escaped}(?![\w-]|\.[\w-])")
            else:
                alternatives.append(rf"(?<![\w.:-]){escaped}(?![\w:-]|\.\w)")
        return re.compile("|".join(alternatives), re.IGNORECASE)

    def _prose_replacement(self, match: re.Match[str]) -> str:
        found = match.group(0)
        for host, replacement in self.hosts.items():
            if host.lower() == found.lower():
                return replacement
        return found  # pragma: no cover - the pattern only matches recorded hosts


def neutralise_scenario(scenario: dict[str, Any]) -> int:
    """Neutralise *scenario* (an export's scenario JSON) in place; how many values changed."""
    neutraliser = _Neutraliser()
    injects = scenario.get("scenario_injects")
    injects = [inject for inject in injects if isinstance(inject, dict)] if isinstance(injects, list) else []
    for inject in injects:
        content = inject.get("inject_content")
        if not isinstance(content, dict):
            continue
        uri = content.get("uri")
        if isinstance(uri, str) and uri:
            content["uri"] = neutraliser.target(uri)
        targets = content.get("targets")
        if content.get("target_selector") == "manual" and isinstance(targets, str) and targets:
            content["targets"] = neutraliser.targets(targets)
    # The sender sits in ``scenario_information`` in an export; read the top level too.
    information = scenario.get("scenario_information")
    holders = [holder for holder in (information, scenario) if isinstance(holder, dict)]
    for holder in holders:
        sender = holder.get("scenario_mail_from")
        if isinstance(sender, str) and sender:
            holder["scenario_mail_from"] = neutraliser.sender(sender)
    if neutraliser.hosts:
        for holder in holders:
            if isinstance(holder.get("scenario_description"), str):
                holder["scenario_description"] = neutraliser.prose(holder["scenario_description"])
        for inject in injects:
            for key in ("inject_title", "inject_description"):
                if isinstance(inject.get(key), str):
                    inject[key] = neutraliser.prose(inject[key])
    return neutraliser.replaced


def _scenario_member(infos: list[zipfile.ZipInfo]) -> zipfile.ZipInfo:
    commented = [info for info in infos if info.comment == SCENARIO_COMMENT]
    if len(commented) == 1:
        return commented[0]
    if not commented:
        jsons = [info for info in infos if info.filename.lower().endswith(".json") and not info.is_dir()]
        if len(jsons) == 1:
            return jsons[0]
    raise ExportNeutraliseError("the export holds no single scenario JSON member")


def neutralise_export(data: bytes) -> NeutralisedExport:
    """*data*, a scenario export archive, with its lab targets neutralised.

    Synchronous and CPU-bound (decompression): an async caller runs it with
    ``asyncio.to_thread``. ``ExportNeutraliseError`` when the archive or its
    scenario JSON cannot be read: the export is then never delivered as is.
    """
    try:
        with zipfile.ZipFile(BytesIO(data)) as archive:
            infos = archive.infolist()
            member = _scenario_member(infos)
            budget = ArchiveBudget(_MAX_EXPANDED_BYTES)
            raw = budget.read(archive, member, max_member=_MAX_SCENARIO_JSON_BYTES)
            scenario = json.loads(raw.decode("utf-8"))
            if not isinstance(scenario, dict):
                raise ExportNeutraliseError("the scenario JSON is not an object")
            replaced = neutralise_scenario(scenario)
            if not replaced:
                return NeutralisedExport(data=data, replaced=0)
            # Jackson writes compact JSON; non-ASCII text stays as it is.
            rewritten = json.dumps(scenario, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            members = [(info, rewritten if info is member else budget.read(archive, info)) for info in infos]
            comment = archive.comment
    except ExportNeutraliseError:
        raise
    except ArchiveTooLargeError:
        raise ExportNeutraliseError("the export expands past what can be read in memory") from None
    except (zipfile.BadZipFile, UnicodeDecodeError, json.JSONDecodeError):
        raise ExportNeutraliseError("the export is not a readable scenario archive") from None
    out = BytesIO()
    with zipfile.ZipFile(out, "w") as rebuilt:
        rebuilt.comment = comment
        for info, content in members:
            # writestr(info, ...) keeps the member's name, date, comment, attributes and
            # compression; only its sizes and CRC are recomputed.
            rebuilt.writestr(info, content)
    return NeutralisedExport(data=out.getvalue(), replaced=replaced)
