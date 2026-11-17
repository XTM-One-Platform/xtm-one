# SPDX-License-Identifier: Apache-2.0

"""Who a registered platform is, and where XTM One reaches it.

A product of the XTM Suite has one identity, the ``platform_url`` it registers
with: the ``iss`` of the tokens it signs, the ``aud`` of the tokens addressed
to it. Where XTM One reaches it is another matter in a deployment whose
containers share a network (Docker Compose, Kubernetes): OpenCTI is
``http://localhost:8080`` for its users and ``http://opencti:8080`` for XTM One.
That internal URL is ``PlatformRegistration.api_url``, captured at registration
from XTM One's own ``{PROVIDER}_API_URL`` - never from what the platform sends,
never from an integration's editable ``base_url``.

The rules every cross-platform call follows:

* trust is decided on the identity: a token is trusted when its issuer is the
  identity of an active registration, compared normalized
  (:mod:`app.core.url_identity`);
* the signing keys of a trusted issuer are fetched where XTM One reaches it
  (:attr:`RegisteredPlatform.reach_url`), a URL that comes from the
  registration only, never from the token;
* a token XTM One sends a platform is addressed to its identity only when it
  is sent to a URL that registration vouches for (its identity or its captured
  internal URL): an address anyone can edit, such as an integration's
  ``base_url``, never receives a token another URL would accept;
* a URL a person is handed (a deep link, a link the agent is told to build
  from) is on the identity: a browser cannot reach the internal URL, which is
  only ever called server to server. A URL on the internal URL is the same
  platform, so it is recognised and shown on the identity
  (:meth:`RegisteredPlatform.shown`).
"""

from __future__ import annotations

import functools
import logging
import re
import uuid
from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING
from urllib.parse import quote, urlsplit

from sqlalchemy import case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.url_identity import redact_url, same_url_identity, url_identity
from app.core.utils import format_exception_message
from app.models.platform_procedure_manifest import PlatformProcedureManifest
from app.models.platform_registration import PlatformRegistration

if TYPE_CHECKING:
    from app.integrations.providers.opencrq.procedure_manifest import Manifest

logger = logging.getLogger(__name__)

# Registration is also the platforms' heartbeat: an unlatched warning would
# repeat on every beat. Bounded so a stream of distinct values cannot grow it.
_WARNED_MAX = 64
_warned_unusable_api_urls: set[str] = set()


def _warn_once(value: str, message: str, *args: object) -> None:
    if value in _warned_unusable_api_urls or len(_warned_unusable_api_urls) >= _WARNED_MAX:
        return
    _warned_unusable_api_urls.add(value)
    logger.warning(message, *args)


@dataclass(frozen=True)
class RegisteredPlatform:
    """The identity of a registration and the URL XTM One reaches it on."""

    platform_identifier: str
    platform_url: str
    api_url: str | None = None

    @property
    def identity(self) -> str:
        """``platform_url`` as the platform registered it, without a trailing slash."""
        return self.platform_url.strip().rstrip("/")

    @property
    def public_url(self) -> str | None:
        """The identity as a link carries it (:func:`_shown_identity`), ``None`` when it names none."""
        return _shown_identity(self.platform_url)

    @property
    def reach_url(self) -> str:
        """Where XTM One reaches the platform: its internal URL when one was captured."""
        return (self.api_url or self.identity).rstrip("/")

    def is_named_by(self, url: object) -> bool:
        """Whether *url* is a spelling of this platform's identity."""
        return same_url_identity(url, self.platform_url)

    def is_reached_at(self, url: object) -> bool:
        """Whether *url* is a URL this registration vouches for: its identity or its internal URL."""
        return self.is_named_by(url) or (self.api_url is not None and same_url_identity(url, self.api_url))

    def shown(self, text: str) -> str:
        """*text* as a person is shown it: every URL on the internal URL moved onto the identity.

        The path, query and fragment after the internal URL are kept. A URL on
        another host, port or path, or one carrying userinfo, is left as it
        is, and so is everything when no internal URL was recorded or the
        registered URL names no identity (:func:`show_on_public_urls`).
        """
        return show_on_public_urls(text, (self,))


# What continues the internal URL's host, port or last path segment:
# ``http://opencti:8080`` is not the start of ``http://opencti:80801``,
# ``http://opencti:8080.example`` or ``http://opencti:8080:9090``, and
# ``http://host/cti`` is not the start of ``http://host/cti-old``. A ``.`` or
# ``:`` with no URL character after it ends a sentence, not the URL.
_URL_CHARACTER = r"[\w\-~%@+]"
_URL_CONTINUES = rf"(?:{_URL_CHARACTER}|[.:]{_URL_CHARACTER})"


@functools.lru_cache(maxsize=64)
def _internal_url_pattern(api_url: str) -> str | None:
    """Every spelling of the internal URL *api_url* in a text: scheme and host
    in any case, the default port written or not, the path as recorded."""
    identity = url_identity(api_url)
    if identity is None:
        return None
    parts = urlsplit(identity)
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    port = parts.port
    default_port = 443 if parts.scheme == "https" else 80
    port_pattern = f"(?::{default_port})?" if port is None else f":{port}"
    return (
        rf"(?<![\w+.\-])(?i:{re.escape(parts.scheme)}://{re.escape(host)}){port_pattern}"
        rf"{re.escape(parts.path)}(?!{_URL_CONTINUES})"
    )


# The path characters a shown identity keeps as written: RFC 3986 unreserved
# characters, percent-encoding, ``/``, ``:``, ``@`` and the sub-delims that
# cannot end a Markdown link target or an HTML attribute. Anything else (a
# space, a quote, a parenthesis, an angle bracket, a control character, a
# non-ASCII letter) is percent-encoded, and so is a ``%`` that starts no valid
# escape (``%zz`` -> ``%25zz``; ``%2F`` is kept).
_SHOWN_PATH_SAFE = "/-._~%!$&*+,;=:@"
_STRAY_PERCENT = re.compile(r"%(?![0-9A-Fa-f]{2})")
_SHOWN_HOST = re.compile(r"[a-z0-9\-.]+|\[[0-9a-f:.]+\]")
_SHOWN_IDENTITY = re.compile(r"[A-Za-z0-9\-._~%:/\[\]!$&*+,;=@]+")


def _shown_identity(platform_url: str) -> str | None:
    """The canonical identity of *platform_url* as a link carries it, or ``None``.

    The host is written in its ASCII (IDNA) form and the path percent-encoded
    past :data:`_SHOWN_PATH_SAFE`, so an internationalized registration is
    shown and no byte of the registered text can break out of a link. ``None``
    when it names no identity or its host is not a host name / IP literal.
    """
    identity = url_identity(platform_url)
    if identity is None:
        return None
    parts = urlsplit(identity)
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    elif not host.isascii():
        try:
            host = host.encode("idna").decode("ascii").lower()
        except UnicodeError:
            return None
    if not _SHOWN_HOST.fullmatch(host):
        return None
    # ``url_identity`` already refused port 0 and dropped the default port, so
    # a port here is one to write; ``None`` is the scheme's default.
    port = "" if parts.port is None else f":{parts.port}"
    path = quote(_STRAY_PERCENT.sub("%25", parts.path), safe=_SHOWN_PATH_SAFE)
    shown = f"{parts.scheme}://{host}{port}{path}"
    return shown if _SHOWN_IDENTITY.fullmatch(shown) else None


def show_on_public_urls(text: str, platforms: Iterable[RegisteredPlatform]) -> str:
    """*text* with every URL on the internal URL of one of *platforms* shown on its identity.

    The identity is shown in its canonical, link-safe form
    (:func:`_shown_identity`), never as the registration's raw text: an IDNA
    host, a percent-encoded path. A registered URL carrying userinfo, a
    query or a fragment, naming port 0, a host that is not a host name, or
    anything that is not an http(s) URL is not shown, and its platform's
    links are then left as they are.

    One pass over *text*: a URL already moved is never matched again, so a
    platform's identity that is another one's internal URL is left as shown.
    The internal URL with the longest path is tried first.
    """
    candidates: list[tuple[str, str]] = []
    for platform in platforms:
        pattern = _internal_url_pattern(platform.api_url) if platform.api_url else None
        public = _shown_identity(platform.platform_url)
        if pattern is not None and public is not None:
            candidates.append((pattern, public))
    if not text or not candidates:
        return text
    combined, identities = _combined_pattern(tuple(sorted(candidates, key=lambda c: len(c[0]), reverse=True)))
    return combined.sub(lambda match: identities[match.lastgroup or ""], text)


@functools.lru_cache(maxsize=64)
def _combined_pattern(candidates: tuple[tuple[str, str], ...]) -> tuple[re.Pattern[str], dict[str, str]]:
    combined = re.compile("|".join(f"(?P<p{index}>{pattern})" for index, (pattern, _) in enumerate(candidates)))
    return combined, {f"p{index}": identity for index, (_, identity) in enumerate(candidates)}


@dataclass(frozen=True)
class PlatformCall:
    """Where a call to a platform goes, and the ``aud`` of the token it carries."""

    base_url: str
    audience: str


def captured_api_url(api_url_override: str | None, platform_url: str) -> str | None:
    """The ``api_url`` a registration records for *api_url_override*.

    ``None`` when there is no override, when it names the platform's identity
    (the platform is then reached on it) or when it names no identity (not an
    http(s) URL, or one carrying userinfo, a query, a fragment or port 0) - the
    keys are then fetched on the identity, as before the override existed.
    """
    raw = (api_url_override or "").strip()
    if not raw:
        return None
    override = url_identity(raw)
    if override is None:
        _warn_once(
            raw,
            "Ignoring the internal URL %r for the platform registered as %s: it is not an "
            "http(s) URL with a host and no userinfo, query string or fragment, so its signing "
            "keys are fetched and its callbacks sent on its registered URL",
            redact_url(raw),
            redact_url(platform_url),
        )
        return None
    if same_url_identity(override, platform_url):
        return None
    return override


def _usable_api_url(stored: str | None) -> str | None:
    """A stored ``api_url``, re-validated as it is read: never a URL PyJWKClient's
    urllib would open outside http(s), whatever wrote the column."""
    if not stored or not stored.strip():
        return None
    usable = url_identity(stored)
    if usable is None:
        _warn_once(
            stored,
            "Ignoring the recorded internal URL %r of a platform registration: it is not an "
            "http(s) URL with a host and no userinfo, query string or fragment",
            redact_url(stored),
        )
    return usable


def _registered(row) -> RegisteredPlatform:
    return RegisteredPlatform(
        platform_identifier=row.platform_identifier,
        platform_url=row.platform_url,
        api_url=_usable_api_url(row.api_url),
    )


def _stored_spellings(identity: str) -> set[str]:
    """The lower-cased spellings a column may hold *identity* as, once trimmed
    and stripped of its trailing slashes (:func:`_stored_url_key`)."""
    parts = urlsplit(identity)
    spellings = {identity}
    if parts.port is None:
        default_port = 443 if parts.scheme == "https" else 80
        spellings.add(f"{parts.scheme}://{parts.netloc}:{default_port}{parts.path}")
    return {spelling.lower() for spelling in spellings}


def _stored_url_key(column):
    """A stored URL as :func:`_stored_spellings` spells it: trimmed, lower-cased,
    every trailing slash stripped like the normalization does."""
    return func.rtrim(func.lower(func.trim(column)), "/")


async def _active_registrations_at(db: AsyncSession, column, url: str, identity: str) -> list[RegisteredPlatform]:
    """The active registrations whose *column* may be a spelling of *identity*,
    the most recently seen first. SQL narrows; the caller compares normalized."""
    rows = (
        await db.execute(
            select(
                PlatformRegistration.platform_identifier,
                PlatformRegistration.platform_url,
                PlatformRegistration.api_url,
            )
            .where(
                PlatformRegistration.status == "active",
                or_(column == url, _stored_url_key(column).in_(_stored_spellings(identity))),
            )
            .order_by(
                PlatformRegistration.last_heartbeat_at.desc().nulls_last(),
                PlatformRegistration.platform_identifier,
            )
        )
    ).all()
    return [_registered(row) for row in rows]


async def platform_for_issuer(db: AsyncSession, issuer: str) -> RegisteredPlatform | None:
    """The active registration whose identity *issuer* names, or ``None`` when it is not trusted.

    A registration spelled exactly as the issuer wins over one that only
    normalizes to it; among equals, the most recently seen.
    """
    identity = url_identity(issuer)
    if identity is None:
        return None
    platforms = await _active_registrations_at(db, PlatformRegistration.platform_url, issuer, identity)
    exact = next((p for p in platforms if p.platform_url == issuer), None)
    if exact is not None:
        return exact
    return next((p for p in platforms if p.is_named_by(issuer)), None)


async def platform_for_integration(db: AsyncSession, integration_id: uuid.UUID | str) -> RegisteredPlatform | None:
    """The registration that owns the platform-managed integration *integration_id*, active first."""
    try:
        wanted = integration_id if isinstance(integration_id, uuid.UUID) else uuid.UUID(str(integration_id))
    except ValueError:
        return None
    row = (
        await db.execute(
            select(
                PlatformRegistration.platform_identifier,
                PlatformRegistration.platform_url,
                PlatformRegistration.api_url,
            )
            .where(PlatformRegistration.integration_id == wanted)
            .order_by(
                case((PlatformRegistration.status == "active", 0), else_=1),
                PlatformRegistration.last_heartbeat_at.desc().nulls_last(),
                PlatformRegistration.platform_identifier,
            )
            .limit(1)
        )
    ).first()
    return _registered(row) if row is not None else None


# Manifests already built, by (registration id, fingerprint): the fingerprint names the document, so a
# stored manifest is never stale under it. Bounded: one entry per OpenCRQ registration in use.
_MANIFEST_CACHE_SIZE = 32
_manifests: OrderedDict[tuple[uuid.UUID, str], Manifest] = OrderedDict()


async def registered_procedure_manifest(db: AsyncSession, platform: RegisteredPlatform) -> Manifest | None:
    """The procedure manifest *platform* registered, ``None`` when it decides nothing.

    It decides only while its fingerprint is the one the registration reported last
    (``PlatformRegistration.procedure_manifest_fingerprint``): a manifest the platform has since
    moved past, or a platform that reports none, hides and checks nothing. The registration is
    found by its identifier, the one :func:`platform_for_integration` returned. The stored
    document is read and built only when no manifest is cached for its fingerprint.
    """
    from app.integrations.providers.opencrq.procedure_manifest import stored_manifest

    on_fingerprint = (
        PlatformRegistration.platform_identifier == platform.platform_identifier,
        PlatformRegistration.procedure_manifest_fingerprint == PlatformProcedureManifest.fingerprint,
    )
    joined = PlatformRegistration.id == PlatformProcedureManifest.registration_id
    key = (
        await db.execute(
            select(PlatformProcedureManifest.registration_id, PlatformProcedureManifest.fingerprint)
            .join(PlatformRegistration, joined)
            .where(*on_fingerprint)
        )
    ).first()
    if key is None:
        return None
    cache_key = (key.registration_id, key.fingerprint)
    cached = _manifests.get(cache_key)
    if cached is not None:
        _manifests.move_to_end(cache_key)
        return cached
    row = (
        await db.execute(
            select(PlatformProcedureManifest.procedures).join(PlatformRegistration, joined).where(*on_fingerprint)
        )
    ).first()
    manifest = stored_manifest(key.fingerprint, row.procedures) if row is not None else None
    if manifest is not None:
        _manifests[cache_key] = manifest
        while len(_manifests) > _MANIFEST_CACHE_SIZE:
            _manifests.popitem(last=False)
    return manifest


@dataclass(frozen=True)
class ProcedureManifestStatus:
    """What the registration that owns an integration says of its procedure manifest."""

    #: The version the platform reported, stripped; ``""`` when it reported none.
    platform_version: str
    #: The fingerprint the registration reported last; ``None`` when it reported none.
    fingerprint: str | None
    #: The manifest stored under that fingerprint, the one that decides; ``None`` when there is none.
    manifest: Manifest | None
    #: When the stored manifest arrived; set with ``manifest``.
    received_at: datetime | None


async def procedure_manifest_status(
    db: AsyncSession, integration_id: uuid.UUID | str
) -> ProcedureManifestStatus | None:
    """The :class:`ProcedureManifestStatus` of the registration that owns *integration_id*, ``None`` when none does.

    The registration is the one :func:`platform_for_integration` picks, and ``manifest`` is what
    :func:`registered_procedure_manifest` gives for it: the card and the tool build read the same
    manifest. Also ``None`` when the registration cannot be read; the read runs in a savepoint.
    """
    try:
        async with db.begin_nested():
            platform = await platform_for_integration(db, integration_id)
            if platform is None:
                return None
            row = (
                await db.execute(
                    select(
                        PlatformRegistration.platform_version,
                        PlatformRegistration.procedure_manifest_fingerprint,
                        PlatformProcedureManifest.received_at,
                    )
                    .join(
                        PlatformProcedureManifest,
                        (PlatformProcedureManifest.registration_id == PlatformRegistration.id)
                        & (
                            PlatformProcedureManifest.fingerprint == PlatformRegistration.procedure_manifest_fingerprint
                        ),
                        isouter=True,
                    )
                    .where(PlatformRegistration.platform_identifier == platform.platform_identifier)
                )
            ).first()
            manifest = await registered_procedure_manifest(db, platform)
    except Exception as exc:
        logger.warning(
            "Could not read the procedure manifest of the registration of integration %s: %s",
            integration_id,
            format_exception_message(exc),
        )
        return None
    if row is None:
        return None
    version = row.platform_version
    return ProcedureManifestStatus(
        platform_version=version.strip() if isinstance(version, str) else "",
        fingerprint=row.procedure_manifest_fingerprint,
        manifest=manifest,
        received_at=row.received_at if manifest is not None else None,
    )


async def platforms_reached_internally(
    db: AsyncSession, integration_ids: Iterable[uuid.UUID | str]
) -> dict[str, RegisteredPlatform]:
    """The registration of each of *integration_ids* that recorded an internal URL,
    keyed by the integration id as a string; the one :func:`platform_for_integration`
    picks. Empty, without a query, when no id parses, and empty when the
    registrations cannot be read: the URLs are then shown as stored, as before.
    The read runs in a savepoint, so a failed one leaves the caller's
    transaction usable."""
    wanted: list[uuid.UUID] = []
    for raw in integration_ids:
        try:
            wanted.append(raw if isinstance(raw, uuid.UUID) else uuid.UUID(str(raw)))
        except ValueError:
            continue
    if not wanted:
        return {}
    statement = (
        select(
            PlatformRegistration.integration_id,
            PlatformRegistration.platform_identifier,
            PlatformRegistration.platform_url,
            PlatformRegistration.api_url,
        )
        .where(PlatformRegistration.integration_id.in_(wanted))
        .order_by(
            case((PlatformRegistration.status == "active", 0), else_=1),
            PlatformRegistration.last_heartbeat_at.desc().nulls_last(),
            PlatformRegistration.platform_identifier,
        )
    )
    try:
        async with db.begin_nested():
            rows = (await db.execute(statement)).all()
        picked: dict[str, RegisteredPlatform] = {}
        for row in rows:
            picked.setdefault(str(row.integration_id), _registered(row))
    except Exception as exc:
        logger.warning(
            "Could not read the platform registrations to show their links on their registered URLs: %s",
            format_exception_message(exc),
        )
        return {}
    return {key: platform for key, platform in picked.items() if platform.api_url is not None}


async def platform_token_audience(db: AsyncSession, integration_id: uuid.UUID | str, base_url: str) -> str:
    """The ``aud`` of a token sent to *base_url* for the platform-managed integration *integration_id*.

    The owning platform's identity when *base_url* is a URL its registration
    vouches for, else *base_url* itself (an integration no registration owns,
    or one whose URL was edited away from the registered platform).
    """
    platform = await platform_for_integration(db, integration_id)
    if platform is not None and platform.is_reached_at(base_url):
        return platform.identity
    return base_url


async def resolve_platform_call(db: AsyncSession, url: str) -> PlatformCall:
    """Where to send a call a platform asked to receive at *url*, and its ``aud``.

    *url* is what the platform named (OpenAEV's ``callback_base_url``). When it
    is a spelling of a registered identity, the call goes to that
    registration's internal URL (to *url* when it has none) addressed to the
    identity as registered; when it is a registration's internal URL, it goes
    there addressed to that identity; any other URL is called and addressed as
    given. Best-effort: a registration that cannot be read leaves *url* as
    given, so a status callback is never lost to the lookup.
    """
    base_url = url.strip().rstrip("/")
    identity = url_identity(base_url)
    if identity is None:
        return PlatformCall(base_url=base_url, audience=url)
    try:
        named = await platform_for_issuer(db, base_url)
        if named is not None:
            return PlatformCall(
                base_url=named.reach_url if named.api_url else base_url,
                audience=named.identity,
            )
        reached = await _active_registrations_at(db, PlatformRegistration.api_url, base_url, identity)
    except Exception as exc:
        logger.warning(
            "Could not read the platform registrations to route a call to %s; calling it as given: %s",
            redact_url(base_url),
            format_exception_message(exc),
        )
        return PlatformCall(base_url=base_url, audience=url)
    platform = next((p for p in reached if p.api_url is not None and same_url_identity(base_url, p.api_url)), None)
    if platform is not None:
        return PlatformCall(base_url=base_url, audience=platform.identity)
    return PlatformCall(base_url=base_url, audience=url)
