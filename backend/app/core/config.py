# SPDX-License-Identifier: Apache-2.0

"""Application configuration via environment variables.

ONLY infrastructure settings go here.  All authentication configuration
(SSO, OIDC, SAML, registration, allowed domains) is managed via the
Settings UI and stored in the database.  Integration credentials are
stored per-integration in the database as well.

Exception: a small set of ``BUILTIN_*`` env vars (see the XTM One
built-in integrations section below) may be used to seed company-managed
integrations on first XTM One / dev-mode startup.  Credentials are only
written once on first creation; subsequent restarts do not overwrite them.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from contextvars import ContextVar
from typing import Annotated, Any
from urllib.parse import urlsplit

from pydantic import PrivateAttr, ValidationInfo, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, PydanticBaseSettingsSource

from app.core.feature_flags import FEATURE_FLAG_ALL, KNOWN_FEATURE_FLAGS

_PROMETHEUS_TOKEN_MIN_LENGTH = 16

# Per-key default OpenSandbox container images. OPENSANDBOX_IMAGES overrides
# are merged on top of this (not replacing it wholesale), so overriding just
# one key via env still leaves the other on its built-in default.
#
# Names the images backend/sandbox/build-image.sh builds as `<image>:<version>`,
# or the published xtmone/sandbox-* images retagged. The version is the one of
# backend/sandbox/published-images.json (check-published-images.py holds them
# together); a merge that bumps it there publishes it (sandbox-image-pin.yml).
_OPENSANDBOX_DEFAULT_IMAGES = {"python": "sandbox-python:0.8", "nodejs": "sandbox-nodejs:0.8"}

# Default per-pod reconciler heartbeat path. Derived from the platform temp dir
# so it resolves to ``/tmp/reconciler_heartbeat`` on Linux containers
# (production unchanged) while staying valid on Windows / macOS local dev, where
# a hardcoded ``/tmp`` path would raise ``FileNotFoundError`` on every cycle.
_DEFAULT_RECONCILER_HEARTBEAT_PATH = os.path.join(tempfile.gettempdir(), "reconciler_heartbeat")


def _per_key_value(raw: object, key: str, fallback: str) -> str:
    """*raw* as a bare value, or the ``key:value`` CSV entry for *key*, else the entry without a key, else *fallback*.

    ``"1"`` is the value for every key; ``"1,python-dev:2"`` is 2 for ``python-dev``
    and 1 for the others; ``"python-dev:2"`` leaves the other keys on *fallback*.
    """
    text = str(raw if raw is not None else "").strip()
    if not text:
        return fallback
    if "," not in text and ":" not in text:
        return text
    default = fallback
    for entry in text.split(","):
        entry = entry.strip()
        if not entry:
            continue
        name, sep, value = entry.partition(":")
        if not sep:
            default = name.strip() or default
        elif name.strip() == key and value.strip():
            return value.strip()
    return default


def _parse_csv_key_value_pairs(v: object, env_var_name: str) -> dict[str, str]:
    """Parse a comma-separated ``key:value`` string into a dict.

    Splits each entry on the first ``:`` only, so a value containing further
    colons (e.g. a registry:port-qualified image reference) survives intact.
    An entry with no ``:`` — or an empty key/value — is malformed and is
    dropped rather than guessed at; malformed entries are logged as a single
    warning naming *env_var_name* so a typo'd .env value is diagnosable
    instead of silently doing the wrong thing.
    """
    if isinstance(v, dict):
        return v
    raw = str(v or "").strip()
    if not raw:
        return {}
    pairs: dict[str, str] = {}
    malformed: list[str] = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        key, sep, value = entry.partition(":")
        key = key.strip()
        value = value.strip()
        if sep and key and value:
            pairs[key] = value
        else:
            malformed.append(entry)
    if malformed:
        import logging

        logging.getLogger(__name__).warning(
            '%s is not fully valid: malformed entr%s %s dropped (expected comma-separated "key:value" pairs).',
            env_var_name,
            "y" if len(malformed) == 1 else "ies",
            malformed,
        )
    return pairs


def load_dotenv_files() -> None:
    """Load the ``.env`` files (``backend/.env``, then the repository's) into ``os.environ`` without overriding it.

    pydantic-settings' ``env_file`` only populates declared model fields and
    silently drops unknown keys when ``extra="ignore"``, so the dynamic keys
    (``LLM_PROVIDER_*_API_KEY``, ``SECRET_VAULT_<NAME>_*``) never reach
    ``os.environ`` otherwise.
    """
    from pathlib import Path

    from dotenv import load_dotenv

    backend_dir = Path(__file__).resolve().parents[2]
    load_dotenv(backend_dir / ".env", override=False)
    load_dotenv(backend_dir.parent / ".env", override=False)


def parse_allowed_modules(raw: object) -> frozenset[str]:
    """Parse a comma-separated list of module names into their top-level names.

    Whitespace is trimmed and empty entries are dropped, so an empty value is
    the full blocklist. Each entry keeps only its top-level segment
    (``urllib.request`` -> ``urllib``) because the sandbox import hook blocks
    and permits on ``name.split(".")[0]``: an administrator can paste a dotted
    path straight from an ImportError and it still re-enables the package.
    Shared by ``CODE_INTERPRETER_EXTRA_ALLOWED_MODULES``,
    ``CUSTOM_TOOL_EXTRA_ALLOWED_MODULES`` and their Settings > Access and security fields.
    """
    return frozenset(top for m in str(raw or "").split(",") if (top := m.strip().split(".")[0]))


def parse_trusted_origins(v: object, env_var_name: str, example: str) -> list[str]:
    """Parse a comma-separated allow-list of origins into ``scheme://host[:port]``.

    Normalised to exactly the form an origin comparison produces, so matching
    is a set membership test. A malformed entry RAISES rather than being
    skipped: these settings widen a security boundary, and an operator who
    mistypes one must not be told everything is fine while the origin they
    meant to trust is silently absent. The same parse validates the
    Settings > Access and security field of ``OAUTH_TOKEN_DEV_TRUSTED_ORIGINS``.
    """
    if v is None or v == "":
        return []
    raw = v.split(",") if isinstance(v, str) else list(v)  # type: ignore[arg-type]
    parsed: list[str] = []
    for item in raw:
        entry = str(item).strip()
        if not entry:
            continue
        parts = urlsplit(entry)
        if not parts.scheme or not parts.hostname:
            raise ValueError(
                f"{env_var_name} entry {entry!r} is not an origin with a scheme "
                f"and host, e.g. {example}"
            )
        port = f":{parts.port}" if parts.port else ""
        parsed.append(f"{parts.scheme.lower()}://{parts.hostname.lower()}{port}")
    # dict.fromkeys: de-duplicate, order preserved, so the startup warning
    # lists each origin once.
    return list(dict.fromkeys(parsed))


#: Bounds on the certificates an administrator adds to the web fetch trust
#: store: far above a real private CA chain, low enough to keep the PEM a
#: setting rather than a bundle.
MAX_TRUSTED_CERTIFICATES = 20
MAX_TRUSTED_CERTIFICATES_CHARS = 64_000

_PEM_CERTIFICATE = re.compile(r"-----BEGIN CERTIFICATE-----\s.*?-----END CERTIFICATE-----", re.S)


def parse_trusted_certificates(v: object, env_var_name: str) -> str:
    """Validate PEM certificates an administrator trusts; return them normalised.

    The certificates (a private CA, or a host's self-signed certificate) are
    ADDED to the trust store of the web fetches: verification stays on, the
    host name must still match. Raises on anything that is not certificates,
    so a mistyped value never reads as accepted while it trusts nothing: a
    private key pasted by mistake is refused by name (it must never sit in a
    setting), and every block must load into an SSL context. Empty = none.
    The same parse validates the Settings > Access and security field.
    """
    import ssl

    if v is None:
        return ""
    if not isinstance(v, str):
        raise ValueError(f"{env_var_name} must be PEM text")
    text = v.replace("\r\n", "\n").strip()
    if not text:
        return ""
    if "PRIVATE KEY" in text:
        raise ValueError(f"{env_var_name} holds a private key: paste the certificate only, never its key")
    if len(text) > MAX_TRUSTED_CERTIFICATES_CHARS:
        raise ValueError(f"{env_var_name} must be at most {MAX_TRUSTED_CERTIFICATES_CHARS} characters long")
    blocks = _PEM_CERTIFICATE.findall(text)
    if not blocks or _PEM_CERTIFICATE.sub("", text).strip():
        raise ValueError(
            f"{env_var_name} must hold PEM certificates only, each between -----BEGIN CERTIFICATE----- "
            "and -----END CERTIFICATE-----"
        )
    if len(blocks) > MAX_TRUSTED_CERTIFICATES:
        raise ValueError(f"{env_var_name} must hold at most {MAX_TRUSTED_CERTIFICATES} certificates")
    normalised = "\n".join(dict.fromkeys(block.strip() for block in blocks)) + "\n"
    try:
        ssl.create_default_context().load_verify_locations(cadata=normalised)
    except (ssl.SSLError, ValueError) as exc:
        raise ValueError(f"{env_var_name} holds a certificate that cannot be read: {exc}") from None
    return normalised


def count_trusted_certificates(pem: str) -> int:
    """How many certificates a value of :func:`parse_trusted_certificates` holds."""
    return len(_PEM_CERTIFICATE.findall(pem or ""))


#: The values ``PLATFORM_MODE`` accepts once trimmed and lowercased; the
#: validator refuses any other. ``apps/generate-manifests.sh`` accepts the same
#: ones (``tests/test_generate_manifests.py``).
PLATFORM_MODES = ("xtm_one", "copilot", "dev", "ai_disabled")

#: The licence-required agentic modes (``Settings.is_copilot_like``).
COPILOT_LIKE_MODES = ("copilot", "dev")

#: The modes where AI runs: every mode but ``ai_disabled``. A generic AI
#: capability (evaluation, dev-docs/mode-split.md) is on in all of them; the
#: built-in benchmark is seeded in these modes.
AI_MODES = ("xtm_one", "copilot", "dev")

#: Bounds on ``PROMPT_GUARD_SCANNED_TOOLS`` and its Settings > Access and security field,
#: far above the tools an operator screens.
MAX_SCANNED_TOOLS = 200
MAX_SCANNED_TOOL_CHARS = 200

#: The longest absolute session lifetime and idle timeout, in minutes (a
#: year): ``JWT_EXPIRE_MINUTES`` / ``SESSION_IDLE_TIMEOUT_MINUTES`` are clamped
#: to it, and Settings > Access and security refuses more.
SESSION_MINUTES_MAX = 525_600

#: The shortest idle timeout, in minutes; ``0`` turns it off. The web app
#: reports activity at least every minute (``frontend/src/lib/session.ts``),
#: so a session in use always has minutes left when an administrator turns
#: the timeout on, before the web app has learnt it.
SESSION_IDLE_MINUTES_MIN = 5

#: A custom tool's entry: ``custom:<its id>``. Its owner may rename it, so it
#: is screened by the id its definition carries, never by its name.
CUSTOM_TOOL_SCAN_PREFIX = "custom:"


def parse_scanned_tools(v: object, env_var_name: str = "PROMPT_GUARD_SCANNED_TOOLS") -> list[str]:
    """Parse the tools whose results the prompt guard screens: sorted, each once.

    An entry is a tool's registered name (a built-in, an integration or an MCP
    tool) or ``custom:<uuid>`` for a custom tool, its id in canonical form. A
    malformed entry RAISES rather than being skipped, like the origin
    allow-lists: an entry that matches nothing screens nothing, and the
    operator must not believe a tool is screened when it is not. The same
    parse validates the Settings > Access and security field.
    """
    import uuid
    if v is None or v == "":
        return []
    if isinstance(v, str):
        raw: list[object] = list(v.split(","))
    elif isinstance(v, list | tuple | set | frozenset):
        raw = list(v)
    else:
        raise ValueError(f"{env_var_name} must be a comma-separated list of tool names")
    parsed: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            raise ValueError(f"{env_var_name} entries must be text")
        entry = item.strip()
        if not entry:
            continue
        if len(entry) > MAX_SCANNED_TOOL_CHARS:
            raise ValueError(f"{env_var_name} entry {entry[:40]!r}... is longer than {MAX_SCANNED_TOOL_CHARS} characters")
        if "," in entry or any(ord(char) < 32 or ord(char) == 127 for char in entry):
            raise ValueError(f"{env_var_name} entry {entry!r} is not a tool name")
        if entry.lower().startswith(CUSTOM_TOOL_SCAN_PREFIX):
            try:
                entry = CUSTOM_TOOL_SCAN_PREFIX + str(uuid.UUID(entry[len(CUSTOM_TOOL_SCAN_PREFIX) :].strip()))
            except ValueError:
                raise ValueError(
                    f"{env_var_name} entry {entry!r} names no custom tool: write custom:<the custom tool's id>"
                ) from None
        parsed.add(entry)
    if len(parsed) > MAX_SCANNED_TOOLS:
        raise ValueError(f"{env_var_name} names more than {MAX_SCANNED_TOOLS} tools")
    return sorted(parsed)


#: The fields a ``Settings`` being built had a value for, collected by
#: ``_EnvironmentRecord``; ``None`` outside ``Settings.__init__``.
_recorded_fields: ContextVar[set[str] | None] = ContextVar("_recorded_fields", default=None)


class _EnvironmentRecord(PydanticBaseSettingsSource):
    """Which fields the other sources set, as ``Settings`` itself read them.

    The last source: it contributes no value. It reads ``current_state`` -
    what the process environment, the ``.env`` files and any other source
    produced, merged in their priority - so an option is locked in Settings >
    Security exactly when a variable is in force for ``Settings``:
    same files, same order, same parsing, one read of each file. A blank
    value counts as unset.
    """

    def get_field_value(self, field: Any, field_name: str) -> tuple[Any, str, bool]:
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        record = _recorded_fields.get()
        if record is not None:
            record.update(
                name
                for name, value in self.current_state.items()
                if value is not None and not (isinstance(value, str) and not value.strip())
            )
        return {}


class Settings(BaseSettings):
    """Global application settings, loaded from environment / .env file."""

    # --- Platform mode ---
    # "xtm_one"     = XTM One (default, open, EE-gated via registered platforms)
    # "copilot"     = Filigran Copilot (enterprise license required)
    # "dev"         = Copilot behavior + XTM One seed (dev/testing)
    # "ai_disabled" = platform shell: nothing seeded, no AI schedulers, no
    #                 provider traffic. A default only — the resolved answer
    #                 comes from ``services/ai_state.py``, never from here.
    platform_mode: str = "xtm_one"
    platform_registration_token: str = ""

    # Enterprise license PEM (env var takes precedence over DB setting).
    # Required in copilot mode; ignored in xtm_one mode.
    enterprise_license: str = ""

    # Optional forced platform instance ID (UUID). Mirrors OpenCTI's
    # ``PLATFORM_ID`` and OpenAEV's ``OPENAEV_INSTANCEID``: when set, the
    # ``platform_settings`` singleton row (whose primary key IS the anonymous
    # telemetry ``instance_id``) is created with this UUID on a fresh install,
    # and an existing row is re-keyed to it at startup (clone/restore parity).
    # When empty (default) a random UUID is generated on first boot.
    platform_id: str = ""

    # --- XTM Hub sign-in entitlement gate ---
    # The role, within this instance's XTM Hub claim, that means administrator.
    # Matched exactly, including case.
    #
    # Setting it is what ENABLES the gate. Empty (default) means no Hub check
    # happens at all and OIDC behaves exactly as it did before this feature.
    # The switch is the admin group rather than the identifier because
    # ``platform_id`` below is set for reasons that have nothing to do with XTM
    # Hub (clone/restore identity continuity), and an instance that happens to
    # force one must not thereby acquire a sign-in gate nobody asked for.
    xtm_hub_admin_group: str = ""

    # Local escape hatch, not a deployment setting: deliberately absent from
    # ``.env.sample``, the documentation and the specification. The identifier
    # the gate looks for is ``platform_id`` above; this only overrides it, so a
    # developer can point an instance at a Hub tenant that still issues
    # human-readable names while ``platform_id`` must be a UUID. Delete it once
    # the Hub emits UUIDs — nothing in the design depends on it.
    xtm_hub_platform_id: str = ""

    # The XTM Hub this platform registers with by default, like OpenCTI's
    # ``XTM__XTMHUB_URL`` and OpenAEV's ``OPENAEV_XTM_HUB_URL``: the URL the
    # Settings registration card proposes, the one the platform deployer's
    # auto-registration uses, and the XTM Hub integration's fallback when it
    # names no URL of its own. An absolute http(s) URL, without a trailing slash.
    xtm_hub_default_url: str = "https://hub.filigran.io"

    # --- Telemetry tags ---
    # Optional comma-separated freeform tags (e.g. "saas,eu-west") attached to
    # the anonymous usage telemetry as the ``filigran.telemetry.tags`` OTLP
    # resource attribute, so adoption analytics can slice every metric by
    # deployment dimension (deployment type, region, ...). Never exposed in
    # the UI or any API — telemetry only. Normalized before export (trimmed,
    # lowercased, deduped, sorted) via ``telemetry_tags_normalized``.
    telemetry_tags: str = ""

    # --- Built-in template brand assets ---
    # Optional comma-separated folders holding more brand asset packs
    # (``<pack>/manifest.json`` next to its files), read after the packs the
    # image ships (``app/agents/seed_data/brand_assets``). For a deployment
    # that adds packs the public repository does not carry. See
    # ``services/template_asset_seed.py``.
    brand_assets_extra_dirs: str = ""

    # --- Branding overrides ---
    # Optional env-var overrides for the product name and logo (base64 data URL).
    # Precedence: DB `platform_settings` > these env vars > mode defaults.
    # Only `name` and `logo` are configurable via env; all other branding fields
    # (description, theme colors, favicon, OG image) remain DB-only.
    platform_name: str = ""
    platform_logo: str = ""

    # Filigran AI extraction endpoints (override builtin integration defaults).
    filigran_ai_base_url_stix: str = ""
    filigran_ai_base_url_ttp: str = ""

    # --- Application ---
    # Injected at build time via the APP_VERSION env var (Docker/CI);
    # matches the Dockerfile ARG default for local/dev runs.
    app_version: str = "0.0.0-dev"
    # The commit the image was built from (GIT_SHA, set by the Dockerfile from
    # the same build argument the frontend's About dialog reads); empty in a
    # local build. Recorded with the version in ``platform_builds``.
    git_sha: str = ""
    # Seeds ``platform_registrations.approval_policy`` the first time a Suite
    # platform (OpenCTI / OpenAEV / OpenGRC) registers, and only then. Decides
    # what happens to that platform's tools while it cannot display an approval
    # prompt: "require" gates them (its agents explain themselves instead of
    # acting) and "skip" lets them run unapproved.
    #
    # Deliberately a seed rather than an override. It sets a value an
    # administrator can then see and change in Settings, instead of silently
    # bypassing a control the settings page still shows as enabled.
    #
    # Defaults to "require" because turning off an approval control has to be a
    # deliberate act. Set it to "skip" when deploying beside a platform whose
    # release cannot render prompts yet, so its agents keep working without
    # anyone making an API call on first boot.
    tool_approvals_platform_default: str = "require"

    # Seeds the platform-wide approval kill switch when the platform settings
    # row is first created — an upgrade, or a fresh install. Read once and then
    # never again: the stored value is what the gate consults, so a variable
    # re-read on boot would undo an administrator's choice on every restart.
    #
    # Unset leaves approvals on. An unrecognised value also leaves them on and
    # logs: a typo must not be the thing that disables an approval control.
    tool_approvals_enabled: str | None = None

    debug: bool = False
    log_level: str = ""  # "debug" | "info" | "warning" | "error" | "critical" — empty = auto (debug→DEBUG, else INFO)
    log_format: str = ""  # "json" | "console" — empty = auto (json in prod, console when debug)
    secret_key: str = ""
    # Former SECRET_KEY values, comma-separated, still accepted when a decision
    # answer link (``/decide/<token>``, #842) signed before a key rotation is
    # opened; new links are always signed with SECRET_KEY. Empty: none.
    decision_link_previous_secrets: str = ""

    # --- Vault references ---
    # How long a secret read from a vault (``vault://<provider>/<path>#<key>``)
    # is kept in memory before the vault is read again: a secret rotated in the
    # vault is picked up within this time, without a restart. 0 reads the vault
    # on every use (a negative value counts as 0). The providers are
    # ``SECRET_VAULT_<NAME>_*`` variables (``app/services/vault/config.py``).
    secret_vault_cache_seconds: int = 300

    # Origins a vault provider connected in Settings > Access and security > Secret
    # vaults may reach on a LOOPBACK address: an on-host vault. Such a
    # provider's address is otherwise held to the operator-network policy of
    # ``app.core.destination_policy`` (``app.services.vault.destinations``):
    # private networks are reached, loopback, link-local, reserved and cloud
    # metadata addresses are not. Link-local and metadata addresses stay
    # refused for a listed origin. Providers declared in the environment
    # (``SECRET_VAULT_<NAME>_*``) are the operator's and are not checked.
    #
    # An environment variable only, never a Settings field: an administrator
    # session must not be able to widen it. Comma-separated, scheme and host
    # required, port optional:
    #   SECRET_VAULT_TRUSTED_ORIGINS=http://127.0.0.1:8200,https://localhost:8443
    secret_vault_trusted_origins: Annotated[list[str], NoDecode] = []

    # --- Database ---
    database_url: str = "postgresql+asyncpg://copilot:copilot@localhost:5432/copilot"
    # Connection pool tuning (total max connections = pool_size + max_overflow PER PROCESS).
    # With multiple uvicorn workers, each gets its own pool.
    # Ensure: (pool_size + max_overflow) * (web_workers * platform replicas + num_saq_workers)
    # < PG max_connections, with one more platform replica during a surge rollout.
    # Pool budget sized so the DEFAULT concurrency caps satisfy the platform
    # sizing rule (runs + sub-agent tasks <= 0.6 * (pool + overflow)):
    # 10 + 5 = 15 <= 0.6 * (15 + 10) = 15. The old 10/10 defaults made every
    # fresh install log the pool-overcommit warning at startup.
    db_pool_size: int = 15  # Persistent connections kept open (per worker)
    db_max_overflow: int = 10  # Extra connections under burst load (per worker)
    db_pool_timeout: int = 30  # Seconds to wait for a free connection
    db_pool_recycle: int = 1800  # Recycle connections after 30 min (avoid stale/dropped)
    db_pool_pre_ping: bool = True  # Verify connection liveness before use
    # asyncpg connection-establishment timeout (seconds). Without it asyncpg
    # uses its 60s default, so a single bad connect stalls a whole reconciler
    # cycle for a full minute. A tighter bound makes a failing cycle return
    # fast so the worker's in-process recovery + retry kick in promptly. With
    # the dedicated default executor (DNS resolution is never starved) a healthy
    # connect completes in well under a second, so this is generous headroom —
    # not a tight leash.
    db_connect_timeout: int = 10
    # How long (seconds) a NEW connection keeps being retried while PostgreSQL
    # refuses or drops it (a restart, a failover, a Service briefly without a
    # ready endpoint): the request waits instead of failing, and past it gets a
    # 503 + Retry-After rather than a 500. A timed-out connect is never retried
    # (db_connect_timeout bounds it). 0 disables the retry.
    db_connect_retry_seconds: float = 15.0
    # Bounds (seconds) on every statement of the Alembic migration connection,
    # so a migration that waits on a lock or a snapshot fails startup with an
    # error instead of holding the rollout silently. A migration that
    # needs longer sets its own ``SET LOCAL statement_timeout``. They replace a
    # timeout set on the role or the database; 0 keeps that one (or none).
    migration_lock_timeout_seconds: int = 300
    migration_statement_timeout_seconds: int = 1800
    # After this many seconds without completing its startup, the platform
    # logs at ERROR the step in progress and what blocks the migration, and
    # repeats it at the same interval; the SAQ worker's schema wait escalates
    # to ERROR after the same delay. 0 disables both.
    startup_watchdog_seconds: int = 300

    # --- Evaluation output extraction ---
    # Bounds for the declarative per-agent prediction extractor. They are
    # conservative estimates, not measurements (no representative stored output
    # exists for the agents they were sized against), so they are configurable:
    # a wrong estimate must be adjustable without a release.
    extraction_max_operations: int = 8
    extraction_max_path_segments: int = 16
    extraction_max_source_bytes: int = 1_048_576  # 1 MiB
    extraction_max_sample_bytes: int = 262_144  # 256 KiB
    extraction_max_parse_depth: int = 32
    extraction_max_values: int = 2_000
    extraction_max_value_chars: int = 200

    # --- Redis ---
    redis_url: str = "redis://localhost:6379"
    redis_max_connections: int = 50  # Max connections in the shared pool
    redis_socket_connect_timeout: int = 5  # Seconds to wait for TCP connect
    redis_socket_timeout: int = 5  # Seconds to wait for response
    redis_health_check_interval: int = 15  # Seconds between liveness pings
    redis_retry_on_timeout: bool = True  # Auto-retry on socket timeout
    # Prefix of every key, worker queue and pub/sub channel the platform and
    # the worker use, to share a Redis: a user whose ACL allows
    # ``~<prefix>* &<prefix>*`` is enough. Empty = the names at the root.
    # See ``app/core/redis_key_prefix.py``.
    redis_key_prefix: str = ""

    # --- Redis HA mode (Sentinel) ---
    # "standalone" (default) → use REDIS_URL (single endpoint or proxy).
    # "sentinel"             → discover the master via Redis Sentinel.
    redis_mode: str = "standalone"
    # Comma-separated "host:port" list of Sentinel nodes.
    redis_sentinel_hosts: str = ""
    # Logical master/service name as configured in sentinel.conf.
    redis_sentinel_service: str = "mymaster"
    # Sentinel auth (separate from Redis data-plane auth).
    redis_sentinel_password: str = ""
    # Redis data-plane auth (applies to master/replica connections).
    # In standalone mode these can also be embedded in REDIS_URL.
    redis_username: str = ""
    redis_password: str = ""
    redis_db: int = 0
    # TLS for sentinel mode (standalone uses rediss:// in REDIS_URL).
    redis_ssl: bool = False
    redis_ssl_ca_certs: str = ""
    redis_ssl_cert_reqs: str = "required"  # "required" | "optional" | "none"

    # --- Concurrency limits ---
    # Assignment runs and sub-agent tasks may hold DB connections during
    # metadata/tool DB operations. The combined limit should still leave
    # headroom for API requests, WebSocket connections, and background services.
    # Rule of thumb: max_concurrent_runs + max_concurrent_sub_agent_tasks
    #   <= (db_pool_size + db_max_overflow) * 0.6
    max_concurrent_runs: int = 10  # Concurrent assignment executions
    # Concurrent sub-agent tasks (platform-wide). 5 matches the compose /
    # .env.sample default AND keeps the default install inside its own
    # sizing rule (runs + tasks <= 0.6 * (pool + overflow) = 15) — the old
    # default of 20 made every fresh install log the pool-overcommit
    # warning at startup.
    max_concurrent_sub_agent_tasks: int = 5
    # Per-orchestrator-run cap: a single agent run may not hold more than this
    # many sub-agent tasks concurrently.  Prevents one fan-out pattern from
    # monopolising the entire DB connection pool.
    # Must be <= max_concurrent_sub_agent_tasks.
    max_concurrent_sub_agent_tasks_per_run: int = 5
    # Questions a delegated background task may ask its direct parent (the
    # agent that spawned it, when it is one of the task agent's consult
    # peers) through ``consult_agent`` in one run. The parent answers
    # with no handover tool and a short iteration budget; a background task
    # back to it stays refused by the chain guard. 0 refuses every such
    # consult, as before.
    delegated_parent_consult_limit: int = 3
    # Self-fan-out: allow an agent to spawn parallel background copies of
    # ITSELF (target ``self``) to parallelise a large/independent workload —
    # even when it has no peer handover agents configured. Only available at the
    # top delegation level (``delegation_depth == 0``) so a self-spawned worker
    # cannot recursively fan out again, and still bounded by
    # ``max_concurrent_sub_agent_tasks_per_run``. Set to False to disable
    # platform-wide.
    agent_self_fanout_enabled: bool = True

    # Self-verification loop: the agentic loop merges several updates
    # of one calendar event (or edits of one Teams message) asked in the same
    # tool round into ONE provider call, so attendees get one notification.
    # Fleet-wide, no per-agent setting. False restores one call per update.
    self_verification_loop: bool = True

    # --- Autonomous attack-path runs (OpenAEV Autonomous Attack Path) ---
    # Dedicated concurrency budget for ``autonomous_pentest`` orchestration
    # executions, SEPARATE from the shared ``max_concurrent_runs`` +
    # ``max_concurrent_sub_agent_tasks`` reconciler pool. Autonomous runs are
    # long-lived, cheaply-parked engagements that can legitimately number in the
    # dozens at once; giving them their own semaphore stops a burst of
    # simultaneous engagements from starving normal assignment / sub-agent
    # execution (and vice-versa). Each actively-cycling run holds ~0-1 pooled DB
    # connections (every cycle opens only short-lived sessions and releases the
    # connection during the agentic loop), so this budget adds far less pool
    # pressure than its slot count suggests — but if you push it well past a
    # dozen, raise ``db_pool_size`` accordingly. 0 (or negative) falls back to
    # the shared reconciler pool (legacy behaviour, no dedicated budget).
    max_concurrent_autonomous_runs: int = 12
    # In-process "continue" burst: on a ``continue`` decision an autonomous run
    # keeps running further decision cycles UNDER THE SAME LEASE instead of
    # parking + rescheduling after every micro-decision. This removes the
    # per-cycle park delay + reconciler poll latency (several seconds each) that
    # otherwise dominates a multi-cycle engagement's wall-clock. The burst yields
    # back to the reconciler (a short park) once EITHER bound below is hit, so
    # leases still rotate fairly across runs. Only ``continue`` chains in-process;
    # genuine long waits (``await_finding`` / ``waiting_input``) still park.
    autonomous_burst_max_cycles: int = 8
    autonomous_burst_budget_seconds: int = 180
    # Belt-and-suspenders for a run parked on ``waiting_input``: schedule a
    # periodic self-resume so an answered operator directive (or an out-of-band
    # OpenAEV stop) is picked up on the next re-check even if the directive
    # callback's resume event was lost. A ``waiting_input`` park has NO deadline
    # by design (a human may take arbitrarily long), so without this a lost
    # resume would strand the run forever. 0 disables the periodic re-check.
    # Kept short (5 min): the re-check is a cheap OpenAEV status read, and a
    # shorter interval means any residual stale park (or a lost directive resume)
    # self-heals in minutes instead of an hour - the 3600s default is what turned
    # a transient orchestration hiccup into a 13-minute "stuck" cockpit.
    autonomous_waiting_input_recheck_seconds: int = 300
    # Upper bound (seconds) for the ``waiting_input`` recheck interval. Each
    # consecutive UNANSWERED recheck backs the next one off exponentially from
    # ``autonomous_waiting_input_recheck_seconds`` up to this cap, so a run
    # waiting a long time on a human does not burn a full decision cycle every
    # 5 minutes forever. A real operator answer arrives via the (immediate,
    # event-driven) directive resume regardless, so stretching the periodic
    # SAFETY recheck costs nothing in responsiveness - it only bounds idle token
    # burn. Clamped to be >= the base interval.
    autonomous_waiting_input_recheck_max_seconds: int = 3600
    # Max consecutive UNANSWERED ``waiting_input`` rechecks before a BUILD
    # (plan / author-scenario) run is stopped with a clear "operator did not
    # answer" reason. A build is a bounded planning session, so an unanswered
    # HITL question must not park it indefinitely (the absolute engagement
    # deadline is the ultimate backstop; this fires first with a precise
    # message). The counter resets the instant the operator responds, so a
    # legitimate multi-question dialogue is never capped. 0 (or negative) ->
    # fall back to a sane built-in default FOR BUILDS ONLY (never truly
    # unlimited for a build). LIVE runs are NOT subject to this cap - a real
    # engagement legitimately waits on a human for as long as it takes.
    autonomous_max_waiting_input_rechecks: int = 0
    # Fast operator-cancel detection for an IN-FLIGHT autonomous burst. When the
    # operator stops/deletes a run, OpenAEV calls the cancel endpoint which flips
    # the orchestration execution terminal, but a burst already running under its
    # lease (multiple decision cycles + a long agent loop) would otherwise keep
    # authoring decisions until the cycle ends. The reconciler polls the row this
    # often (seconds) and fires the adapter's cancel_event the instant it goes
    # terminal, so the agent loop aborts between iterations and the burst stops
    # NOW instead of at the end of the current cycle. 0 (or negative) disables the
    # fast poll and falls back to the lease heartbeat (lease/3 ~= 20s) detection.
    autonomous_cancel_poll_seconds: int = 2
    # The same fast cancel poll for the assignment, sub-agent, flow and
    # evaluation adapters, so an emergency stop aborts their loop between
    # iterations instead of at the lease heartbeat (~20s). 0 disables it.
    orchestration_cancel_poll_seconds: int = 2
    # Attempts per client IP per window on the session-less emergency stop,
    # counted before the token is checked. Fail-closed when Redis is down,
    # except for a request carrying the current token's prefix.
    panic_emergency_rate_limit: int = 10
    panic_emergency_rate_window_seconds: int = 600
    # Forward-progress (convergence) guard. A decision cycle "makes progress" when
    # it performs at least one state-changing OpenAEV action (author/execute a
    # chained attack-path step, create/update a threat-arsenal action, promote a
    # finding, set the run scope, ensure a target team, or update the run status).
    # Reading state and consuming operator directives (an intake the persona calls
    # every cycle) do NOT count - only the action taken in response does. A cycle
    # that only READS/searches/consults and then
    # signals ``continue`` (or fails to signal, which defaults to ``continue``)
    # advances nothing. Absent this guard, such a cycle repeats up to the hard
    # ``MAX_CYCLES`` (1000) cap - burning tokens/quota for hours while authoring
    # zero steps (the observed "20+ decisions, 0 attack-path steps, still looping"
    # stall). After this many CONSECUTIVE no-progress ``continue`` cycles the run
    # is stopped and reported FAILED to OpenAEV with a clear reason, instead of
    # spinning to the cap. Any progress cycle, or a genuine park (await_finding /
    # waiting_input), resets the streak. The elevated streak is also surfaced in
    # the cycle prompt so the orchestrator is told it is looping and must converge
    # BEFORE the hard stop trips. 0 (or negative) disables the guard.
    autonomous_max_no_progress_cycles: int = 6

    # OPTIONAL deterministic per-assignment spread window for schedule-triggered
    # runs (OFF by default). Each assignment's offset is derived from its UUID
    # (``offset = uuid.int % (window + 1)``) so the same assignment lands in the
    # same bucket on every tick; the offset is added to the orchestration
    # execution's ``run_at``.
    #
    # Default 0 = DISABLED, so a scheduled run starts at (approximately) its
    # scheduled time instead of up to ``window`` seconds late. The thundering
    # herd at ":00" is ALREADY absorbed downstream by the reconciler: it caps
    # concurrency with a semaphore (``max_concurrent_runs +
    # max_concurrent_sub_agent_tasks``) and only claims up to the free capacity
    # each poll cycle, so a burst of simultaneously-due runs is throttled to the
    # configured concurrency regardless of this setting. This window is therefore
    # an OPT-IN extra for very large multi-tenant fleets that want to trickle a
    # top-of-hour burst in over several minutes (e.g. to stay under an LLM
    # provider's rate limit); set e.g. 900 to smear morning bursts across 15
    # minutes. Most deployments should leave it at 0 (punctual starts).
    scheduled_assignment_spread_seconds: int = 0
    # Per-user fan-out spread window (seconds). When a company-managed
    # assignment fans out to ALL active users (``run_as_all_users`` — e.g. the
    # weekly CTEM stage assessments), the per-user runs are spread evenly
    # across this window instead of all firing at the same instant. Unlike the
    # per-assignment spread above, this is ON by default and keyed per user:
    # a fan-out herd is N heavy agentic runs against the SAME LLM provider
    # account, and — because the orchestration deadline is derived from
    # ``run_at`` — runs that queue behind the reconciler's concurrency cap
    # burn their timeout budget while waiting and get dead-lettered without
    # ever executing. Spreading ``run_at`` fixes both: each run's deadline
    # starts at its own scheduled start, and the provider sees a trickle
    # instead of a burst. Set to 0 to disable (all runs due immediately).
    fanout_spread_window_seconds: int = 3600
    # Automatic retry of runs that failed on a TRANSIENT LLM provider error
    # (rate limit / quota, 5xx overload/outage, connection failure) after the
    # in-call retries and provider failover were already exhausted. Applies
    # to every assignment — no ``retry_on_failure`` opt-in needed — because
    # the failure is about the provider's minute-scale weather, not about the
    # assignment. Bounded and delayed: up to ``max`` retries, each waiting
    # ``base * 2^attempt`` seconds (capped) with jitter so retries don't land
    # in the same rate-limit window. Set max to 0 to disable.
    assignment_transient_retry_max: int = 2
    assignment_transient_retry_base_seconds: int = 120
    assignment_transient_retry_max_delay_seconds: int = 900

    # --- Web workers ---
    # Uvicorn worker count. MUST stay in sync with the launcher default in
    # ``backend/start-uvicorn.sh`` (``WEB_WORKERS:-2``): this value feeds the
    # pool-overcommit estimator in ``startup_checks._validate_pool_config``,
    # so a drift makes the connection-budget check undercount live workers.
    web_workers: int = 2

    # Per-worker in-app concurrency limit enforced by ConcurrencyLimitMiddleware.
    # Overflow above this many concurrent application requests is shed with a
    # fast 429 (backpressure) instead of queuing, and health-check probes are
    # exempt so a burst can never make Kubernetes kill a busy-but-healthy pod.
    # This sits BELOW uvicorn's --limit-concurrency backstop
    # (MAX_CONCURRENCY) so the app sheds load before the protocol-level limiter
    # — which would 503 every request, probes included — is ever reached.
    # 0 disables the in-app limiter (uvicorn's backstop still applies).
    app_max_concurrency: int = 200

    # Max inbound channel messages (Slack / Teams / WhatsApp / Telegram /
    # Discord / Google Chat) processed CONCURRENTLY per web process. Each
    # inbound activity is handled in a fire-and-forget background task that
    # opens its own DB session (routing + the agentic loop), so an inbound
    # burst — e.g. a busy Teams tenant at the start of the workday — would
    # otherwise spawn an UNBOUNDED number of concurrent tasks that each
    # contend for the small web DB pool, exhausting it and tripping uvicorn's
    # concurrency backstop (the morning pool-exhaustion incident). A bounded
    # semaphore applies backpressure: excess inbound messages queue (the
    # webhook already acknowledged 200) instead of stampeding the pool. Keep
    # it comfortably below ``db_pool_size + db_max_overflow`` so channel
    # processing can never monopolise the pool. 0 disables the cap (restores
    # the legacy unbounded behaviour — not recommended in production).
    channel_max_concurrent_processing: int = 10
    # On an external channel (Slack, Teams, WhatsApp, Telegram, Discord, Google
    # Chat), an identified user's ``@agent-key`` also makes that agent answer
    # after the routed one, when the agent's own channel settings let
    # it answer there. Off by default: a channel message is answered by its
    # routed agent alone.
    channel_agent_mentions_enabled: bool = False

    # Human gates (#842, S2b, dev-docs/human-gates.md section 8): the inbox,
    # the bell and the decision cards read the ``decisions`` table only when
    # this is on AND the backfill count check finds every open gate covered
    # (``decision_backfill.read_switch``). Off by default: the readers keep
    # reading each engine's own columns, which the dual write keeps current.
    decisions_read_enabled: bool = False

    # --- Worker (SAQ) ---
    worker_concurrency: int = 20  # Max concurrent background jobs
    worker_job_timeout: int = 600  # Max seconds per job (10 min default)
    # Upper bound (seconds) for a single blocking dequeue (Redis BLMOVE).
    # SAQ's default of 0 blocks FOREVER on a single read: if that TCP
    # connection dies silently (network blip, NAT/idle reset, container
    # restart, host sleep), the worker hangs indefinitely with no error
    # while jobs pile up. A bounded dequeue makes every worker slot cycle
    # back through redis-py (reconnect + health check) at least this often.
    worker_dequeue_timeout: int = 30
    worker_document_processing_concurrency: int = 3  # Max concurrent document embedding jobs within one worker process
    worker_document_job_timeout: int = 1200  # Max seconds per document job
    worker_document_job_retries: int = 3  # Retry count for document job failures/timeouts
    worker_document_job_retry_delay_seconds: float = (
        5.0  # Base delay before retrying document jobs (fractional seconds supported)
    )
    worker_document_job_retry_backoff: bool = True  # Enable exponential backoff for document job retries
    worker_document_slow_warning_seconds: int = 300  # Emit warning when one document job runs longer than this
    # Grace period (seconds) the SAQ worker gives in-flight jobs to finish
    # cleanly before sending cancellation signals during a rolling restart /
    # SIGTERM.  SAQ's default of None / 0 means NO grace period — any job
    # that is still running when SIGTERM arrives is immediately cancelled.
    #
    # Sizing constraint — MUST stay well below the orchestrator's kill
    # deadline: SAQ only runs the worker's shutdown hook (Langfuse flush,
    # Redis queue close) AFTER this grace elapses, so the grace plus ~15 s
    # of teardown headroom must fit inside Kubernetes'
    # ``terminationGracePeriodSeconds`` (default 30 s) / Docker Compose's
    # ``stop_grace_period``.  A value >= the kill deadline guarantees the
    # pod is SIGKILLed mid-shutdown whenever a job is in flight — losing
    # the graceful teardown entirely.  Do NOT size this against the max
    # job runtime (jobs may legitimately run for minutes); it only gives
    # short in-flight jobs a window to complete.  The "Some tasks did not
    # finish within the shutdown grace period" warning can still appear
    # even on idle workers (SAQ's sweep upkeep timer sleeps 60 s between
    # ticks and is awaited by the same gather) — that residual warning is
    # benign; forced cancellation of upkeep pollers is a clean no-op.
    worker_shutdown_grace_period: int = 20
    # How long the worker's deployment lets a rolling restart take: the
    # worker pod's ``terminationGracePeriodSeconds`` (Docker Compose: its
    # ``stop_grace_period``), then how long a new worker pod may take to start
    # (its startup probe's ``initialDelaySeconds`` + ``failureThreshold`` x
    # ``periodSeconds``). A Video Studio render a stopping worker hands back
    # waits that long for the next worker (``video_studio.jobs.resume_wait``).
    # 0 = not given: Kubernetes' default 30 s grace and 570 s to start, 10 min.
    worker_termination_grace_seconds: int = 0
    worker_startup_seconds: int = 0

    # --- Async thread pool (default executor) ---
    # asyncpg resolves DNS for every new DB connection on asyncio's default
    # ThreadPoolExecutor, which is ALSO shared by every asyncio.to_thread() /
    # run_in_executor(None, ...) call (S3/MinIO file I/O, the synchronous
    # Bedrock SDK, docx rendering, SMTP, RSS, ...). Python sizes that pool at
    # min(32, cpu + 4) (~6-8 on a small container); under the worker's high
    # agentic concurrency a burst of blocking S3 work saturates it and starves
    # DB connection establishment, wedging the orchestration reconciler with
    # repeated connect TimeoutErrors until a restart. We install a
    # generously sized named executor at startup. 0 = auto (derive from the
    # worker concurrency budget + DNS headroom); a positive value overrides.
    # Clamped to never go below Python's default, so it can only add capacity.
    default_thread_pool_max_workers: int = 0

    # --- Dedicated document-parse thread pool ---
    # CPU-bound document parsing (pypdf, python-docx, openpyxl, xlrd,
    # python-pptx, trafilatura/BeautifulSoup) is offloaded to its OWN small
    # executor (see core/parse_offload.py) so a burst of multi-minute parses
    # can neither block the event loop nor starve the default executor that
    # serves asyncpg DNS resolution. Clamped to [1, 8] at load time.
    parse_thread_pool_max_workers: int = 2

    # --- Event-loop stall watchdog ---
    # Logs a WARNING with the blocking stack (sampled from a sidecar thread)
    # whenever the event loop stops being scheduled for longer than the
    # threshold, so a wedged pod produces actionable logs instead of silence
    # before the kubelet kills it. Threshold clamped to [0.5, 60] seconds.
    event_loop_watchdog_enabled: bool = True
    event_loop_stall_threshold_seconds: float = 2.0

    # --- Embedding auto-retry (self-healing for failed documents) ---
    # When a document fails to embed for a *retryable* reason (provider
    # misconfigured / down, transient network error, empty vectors), it is
    # automatically re-tried by the autonomous ``recover_failed_documents``
    # sweeper with exponential backoff + jitter — no manual reprocess needed.
    # Structural/permanent failures (no extractable text, no content, KB
    # deleted) are NOT retried.
    embedding_auto_retry_enabled: bool = True
    # Base backoff before the first auto-retry; doubles each subsequent attempt
    # (``base * 2^retry_count``), capped at ``max_delay``.
    embedding_auto_retry_base_delay_seconds: int = 300  # 5 min
    # 30 min cap (was 6 h): transient infra waves (DDL lock storms, provider
    # blips) make a document accumulate retries quickly, and a 6 h cap then
    # parked it in ``error`` — with the dashboard warning banner up — for
    # hours after the cause had cleared. 30 min bounds the worst-case
    # self-heal latency while still backing off a genuinely-down provider
    # (≤ 48 cheap re-embed attempts per doc per day, batch-capped per tick).
    embedding_auto_retry_max_delay_seconds: int = 1_800  # 30 min cap
    # Random jitter (0..N seconds) added to each scheduled retry so a large
    # backlog re-embeds in a spread-out fashion instead of a thundering herd.
    embedding_auto_retry_jitter_seconds: int = 60
    # Max automatic retries per document. 0 = unlimited (keep retrying forever
    # with the capped backoff, so the backlog self-heals whenever the provider
    # comes back). Set a positive value to give up after N attempts.
    embedding_auto_retry_max_attempts: int = 0
    # Max documents re-enqueued per sweep tick (bounds worker / Redis load).
    embedding_auto_retry_batch_size: int = 100

    # --- Stale-embedding auto-remediation (self-healing for ``ready`` docs) ---
    # After an admin changes the platform embedding provider/model/dimensions,
    # previously-embedded ``ready`` documents whose stored embeddings no longer
    # match the new target are automatically re-embedded by the autonomous
    # ``recover_stale_documents`` sweeper — the same work the dashboard
    # "Re-embed all" button does, but without manual intervention. This is what
    # makes "Re-indexing in progress (auto-remediation active)" actually true.
    # Disable to require a manual re-embed instead.
    embedding_stale_auto_remediation_enabled: bool = True
    # Max documents re-embedded per stale-remediation sweep tick (bounds worker
    # / Redis load on a large corpus after a config change).
    embedding_stale_remediation_batch_size: int = 100

    # --- Orchestration engine (durable FSM + outbox reconciler) ---
    # The reconciler loop runs in the worker process and is the single
    # scheduler/recovery contract for all long-running jobs (assignment
    # dispatch, evaluation runs, sub-agent tasks).
    reconciler_interval_seconds: float = 2.0  # Base poll cadence (a Redis "wake" nudges it sooner)
    orchestration_lease_seconds: int = 60  # Lease validity; heartbeated at lease/3 while running
    # The lease a background task (a sub-agent delegation) runs under, renewed
    # every third of it, instead of ``orchestration_lease_seconds``. An agent
    # turn holds the worker's event loop for as long as its longest tool call
    # (a wait of up to five minutes, a CI poll, a large file read): a renewal
    # missed three times in a row under a 60 s lease threw the attempt away
    # and re-ran the task from scratch while its worker was still working.
    # Three times the longest tool call, so one call never costs the lease;
    # crash recovery of a dead worker waits that much longer. 0 keeps the
    # engine's lease.
    sub_agent_lease_seconds: int = 900
    orchestration_default_max_attempts: int = 3  # Claims before a crashed execution dead-letters
    orchestration_claim_batch: int = 20  # Max executions/outbox events claimed per adapter per tick
    orchestration_grace_seconds: int = 120  # Extra slack added to job timeout when computing deadline_at
    # How long an assignment run or a background task may wait in the queue for a
    # free worker before it is failed with ``deadline_exceeded_before_start``
    # (no model call, no ``run.failed``). Its execution budget (timeout + grace)
    # counts from the moment a worker starts it, never from its creation, so a
    # saturated queue no longer eats the time it may run. 0 = no queue bound.
    # An assignment overrides it with ``trigger_config.max_queue_wait_seconds``.
    orchestration_max_queue_wait_seconds: int = 3600
    # How long background AI work that came due while agentic AI is unavailable
    # (Community Edition, or the AI switch off) - or while the quota store
    # cannot be reached and quotas fail closed - is HELD before it is
    # failed with the reason. 0 fails it at its start instead of
    # holding it.
    ai_unavailable_hold_max_seconds: int = 604800
    # How long an evaluation run may sit "running" after its owning execution went
    # terminal before the periodic sweep drives it to a terminal state.
    #
    # The bound is measured; this value is a chosen margin above it. A run
    # cannot legitimately be abandoned sooner than the worst-case retry backoff plus
    # lease expiry: 5s base doubling over 3 attempts with +25% jitter is 44s, plus
    # 60s lease per attempt is 180s -- ~3.7 minutes, derived from live settings.
    # 600s clears it with a wide margin; nothing measured says 600 rather than 400
    # or 900. The 600s backoff cap never binds at 3 attempts (5 x 2^2 = 20s), so
    # raising ``orchestration_default_max_attempts`` is what moves the bound.
    #
    # The clock is the execution's ``updated_at``, never anything on the run: a run
    # records no last-touched time, and its start time cannot tell a stranded run
    # from one legitimately taking hours.
    #
    # Floored at the use site to the measured bound by
    # ``stranded_run_grace_floor_seconds()`` in
    # ``services/evaluation_continuation.py``, which derives it from the retry
    # settings, so raising the attempt count raises the floor. Zero or negative
    # would otherwise put the cutoff at or after now and abolish the window.
    #
    # Not in ``.env.sample``: an internal safety margin, not a knob an operator
    # sizes, and every variable listed there must be documented under
    # ``docs/docs/`` (CI gate ``check-new-env-vars-documented.sh``). The env var
    # still overrides it for an incident.
    evaluation_stranded_run_grace_seconds: int = 600
    # Per-pod reconciler liveness heartbeat. The reconciler touches this file
    # every cycle; the worker's livenessProbe (``python -m app.healthcheck
    # reconciler``) fails when it goes stale so Kubernetes restarts a dead/hung
    # worker instead of leaving runs/tasks stuck on pending with no recovery.
    # The worker has no HTTP server, so an exec probe on this file is the only
    # way to detect a wedged reconciler. Set to "" to disable the heartbeat.
    reconciler_heartbeat_path: str = _DEFAULT_RECONCILER_HEARTBEAT_PATH
    reconciler_heartbeat_max_age_seconds: int = 120
    # In-process self-recovery for the reconciler. After this many
    # CONSECUTIVE failed cycles the reconciler disposes the SQLAlchemy
    # connection pool so the next cycle builds fresh connections — recovering
    # from a wedged pool (DB failover, half-open sockets after a network blip,
    # connections cancelled mid-handshake) WITHOUT a pod restart. Re-applied
    # every N failures while the streak continues.
    reconciler_pool_recovery_after_failures: int = 3
    # Last-resort restart valve. While failing, the reconciler keeps its
    # liveness heartbeat fresh so a transient DB outage / failover does NOT
    # cause restart churn — it waits and self-heals in-process when the DB
    # returns. Only after it has been unable to complete a SINGLE cycle for this
    # long does it stop refreshing the heartbeat, letting the livenessProbe
    # restart it as a true last resort. Set well above a normal failover window.
    reconciler_unhealthy_restart_seconds: int = 600
    # Flow execution. A run's hard wall-clock ceiling: the
    # orchestration deadline is set this far ahead at creation and re-applied
    # on every park, after which the run dead-letters and is failed. Generous
    # by default — flows legitimately sleep on `util.delay` nodes.
    flow_run_timeout_seconds: int = 86400

    # Safety-net deadline (seconds) for assignment runs the orchestration engine
    # is NOT managing — chat-handover / consult / transfer tracing runs created
    # in-process (``assignment_id IS NULL``, no orchestration execution row) and
    # legacy pre-orchestration runs. The autonomous
    # ``_cleanup_stale_assignment_runs`` sweep force-fails such runs once they
    # have been in an active status (``running`` / ``pending`` / ``delayed``)
    # for longer than this, since the reconciler can only recover runs that
    # still have a live execution row. Runs that DO carry an assignment timeout
    # use that timeout + grace instead; this is the fallback for the
    # timeout-less tracing/legacy runs. Clamped to ``>= 60`` at use time. Set
    # generously so a legitimately long in-process chat turn is never failed
    # mid-flight. Matches the orphan threshold of a background task
    # (``stale_in_process_run_seconds``): a run nobody works on is
    # failed, and the work item claims it held released, within 15 minutes.
    stale_assignment_run_seconds: int = 900  # 15 min

    # Shorter safety-net deadline for *in-process tracing runs* specifically
    # (``assignment_id IS NULL``, no orchestration execution row — created by
    # ``agent_chat`` for chat-handover / consult / transfer scenarios).  These
    # runs have a Redis ``chat:processing:{conv_id}`` liveness marker (300s TTL,
    # refreshed every 20 s) that the sweep probes before force-failing; a
    # missing marker after this threshold reliably indicates an orphan from a
    # pod crash.  Using a tighter deadline reduces the "stale warning flood"
    # from ~20 minutes (600 s warn → 1800 s fail) down to a few ticks.
    # Clamped to ``>= 60`` at use time.  Set to 0 to fall back to
    # ``stale_assignment_run_seconds`` for all orphaned runs.
    stale_in_process_run_seconds: int = 900  # 15 min (vs 30 min global fallback)

    # Early-warning threshold (seconds) for the stale-run sweep. When a run has
    # been in an active status for longer than this but has not yet crossed the
    # stale deadline, a WARNING is emitted so operators are alerted well before
    # the run is actually force-failed. Must be less than
    # ``stale_assignment_run_seconds`` to be effective; set to 0 to disable.
    # Clamped to ``>= 0`` at use time.
    stale_assignment_warn_seconds: int = 600  # 10 min early warning

    # An assignment run handed back by a stopping worker (rollout, SIGTERM)
    # and not resumed after ``handed_back_resume_seconds`` gets its execution
    # made due again; still not resumed after ``handed_back_orphan_seconds``
    # (or its execution gone or terminal), it is failed and the work items it
    # held are released (``handed_back_sweep``). Clamped to >= 60 and
    # orphan >= resume at use time.
    handed_back_resume_seconds: int = 180  # 3 min
    handed_back_orphan_seconds: int = 300  # 5 min

    # --- Per-assignment webhook delivery queue ---
    # ``POST /api/hooks/assignments/{id}`` is the documented way to drive an
    # agent from an external system (SIEM/SOAR firing on every alert). The
    # overlap guard that keeps one assignment from running in parallel used to
    # DROP a delivery arriving while a run was active and still answer HTTP 200,
    # so the sender recorded a success for a run that never happened. The queue
    # below supplies the ordering / bounds / backpressure semantics that the
    # guard itself deliberately does not provide: an accepted delivery becomes a
    # ``queued`` AssignmentRun that the reconciler promotes, oldest first, into
    # the assignment's next free slot.
    #
    # Set False to restore the legacy drop-and-200 behaviour.
    assignment_queue_enabled: bool = True
    # Runs of ONE assignment in flight at once when the delivery queue starts
    # them (webhook deliveries, polled and pushed integration events).
    # ``trigger_config.max_concurrent_runs`` overrides it per assignment, and
    # ``trigger_config.ordering_key`` (a dotted path into the trigger payload)
    # keeps the runs sharing a value one at a time, in arrival order. 1 restores
    # one run at a time. Clamped to 1..50 at use time. Schedules, manual
    # triggers and follow-ups keep their own overlap guard. To keep up, a cap
    # must exceed deliveries per second x seconds a run holds its slot: 30 a
    # minute of 6-22 s runs need 8 on average, so 10 leaves headroom.
    assignment_max_concurrent_runs: int = 10
    # Due assignment executions allowed to wait for a worker slot at once: the
    # queue drain stops promoting when this many are waiting, so a promoted
    # run starts within a reconciler cycle and one busy assignment cannot fill
    # the pool ahead of the others (the drain is round-robin across agents and
    # assignments). 0 (default) = ``max_concurrent_runs``.
    assignment_dispatch_max_pending_runs: int = 0
    # Fair share of the worker pool when its slots claim due assignment runs
    # (``assignment_dispatch.fair_share_order``): delegated children of a live
    # parent first, then one run per fleet in turn (an agent outside any fleet
    # is its own group), oldest first within a fleet. A fleet holding
    # ``assignment_fleet_max_share`` of the pool (``max_concurrent_runs`` x the
    # workers claiming assignment runs), or an agent holding
    # ``assignment_agent_max_share`` of it, goes after every other fleet's due
    # work - never refused: an otherwise idle pool still runs it. A share of 0
    # or 1 lifts that cap. False restores the strict oldest-first claim. A
    # fleet's optional run budget (``Fleet.run_budget``, #1526) then holds the
    # due runs of a fleet over it (``services/fleet_run_budget``).
    assignment_fair_share_enabled: bool = True
    assignment_fleet_max_share: float = 0.5
    assignment_agent_max_share: float = 0.5
    # Delegation reserve: slots per worker above the shared pool that only a
    # delegated child whose parent run is still running may take, once the
    # shared pool is full (assignment child runs and background sub-agent
    # tasks alike). A parent holds its slot while it waits for its child, so N
    # parents filling the pool and each waiting on one child would otherwise
    # wait until a deadline. Idle or not full, the pool claims as before. 0
    # turns the reserve off.
    delegation_reserve_slots: int = 2
    # Once an assignment's oldest queued delivery has waited this long, the
    # drain alternates its newest and oldest deliveries so fresh work is not
    # delayed by the whole backlog (nothing is dropped), and a WARNING says so
    # (at most once a minute). 0 disables both.
    assignment_queue_backlog_seconds: int = 300
    # How long the drain halves an assignment's concurrency cap after one of
    # its runs met an HTTP 429 (model provider or integration). A 429 met
    # outside an assignment run throttles nothing, and the window is fixed from
    # the first 429. The providers' own retries are unchanged. 0 disables it.
    assignment_dispatch_rate_limit_backoff_seconds: int = 60
    # Max ``queued`` runs held per assignment (the incoming delivery is counted
    # against this cap). Clamped to ``>= 1`` at use time. Bounds the backlog a
    # stuck or slow assignment can accumulate.
    assignment_queue_max_depth: int = 20
    # What happens to a delivery that arrives when the queue is already at
    # ``assignment_queue_max_depth``:
    #   "reject"      (default) HTTP 429 + ``Retry-After`` — real backpressure.
    #                 Most SIEM/SOAR senders retry a 429 and never retry a 200,
    #                 so this is the only policy under which a burst is
    #                 eventually delivered in full.
    #   "drop_oldest" the oldest queued run is finalized as ``skipped`` (visible
    #                 as a run row carrying the overflow reason) and the new
    #                 delivery takes its slot — "freshest N alerts win".
    #   "drop_newest" the incoming delivery is discarded with HTTP 200 and a
    #                 WARNING — "first N alerts win". No run row is created, so
    #                 prefer "reject" unless the sender cannot handle a 429.
    assignment_queue_overflow_policy: str = "reject"
    # ``Retry-After`` (seconds) returned with the 429 raised by the "reject"
    # overflow policy. Clamped to ``>= 1`` at use time.
    assignment_queue_retry_after_seconds: int = 30
    # Whether a run parked on human-in-the-loop input holds the queue.
    #
    # True (default): ``awaiting_human`` keeps its place — the backlog builds
    # behind the unanswered approval instead of running the next delivery in
    # parallel with a half-finished run that still owns the same tools and DB
    # writes. Unlike the legacy drop, a held queue is bounded and VISIBLE
    # (queued run rows + 429 backpressure at the cap), so an unanswered approval
    # no longer silently swallows the intake.
    #
    # False: a paused run releases the queue and the next delivery starts. Only
    # safe for assignments whose HITL pause does not leave shared state
    # half-written.
    assignment_queue_hold_on_awaiting_human: bool = True
    # Hours after which a run paused for a person stops holding its assignment
    # (human-gates.md section 7, F3): later triggers run, the run stays
    # resumable. Applies to scheduled, event and delivery runs; a chat-started
    # run always holds. 0 turns the release off.
    human_gate_queue_release_hours: float = 4.0
    # Max distinct assignments that promote queued deliveries per drain. The
    # drain runs on every reconciler cycle, and the enqueue and the end of every
    # assignment run wake it, so the rest follow within a cycle.
    assignment_queue_drain_batch: int = 20
    # Pushed integration events - a Slack event, a Graph or LinkedIn
    # notification, a provider or agent webhook - that arrive while every slot
    # of their assignment is taken (``assignment_max_concurrent_runs``). False
    # (default): the overlap guard skips them, as it always did (a Slack event
    # is left to the poller). True: they queue, past the queue cap if
    # need be, and start as slots free. Polled events (email, Teams, Slack
    # messages) always queue, whatever this says: a full queue gives
    # them back to their poller, which fetches them again later.
    integration_event_queue_enabled: bool = False
    # One run per inbound event on an agent (``services/event_run_dedup.py``):
    # a second event-triggered run of the same dedup key (a mail's Message-ID,
    # a calendar event's iCalUId and modification instant, a provider event's
    # id) on the same AGENT within this many seconds is recorded as a skipped
    # run (``skipped: duplicate of run <id>``) instead of starting, across the
    # agent's assignments and across delivery paths (a Graph notification and
    # the poll behind it, two mailboxes the mail reached, a delegated pick-up).
    # An assignment's ``trigger_config.dedup_window_seconds`` overrides it;
    # ``0`` turns the dedup off. Scheduled runs are not affected.
    assignment_event_dedup_window_seconds: int = 600
    # One turn per chat conversation (``services/chat_turn_coalescing.py``):
    # the run of a chat message (a Teams chat, a Teams channel
    # thread) starts this many seconds late, and a later message of the same
    # sender in the same conversation that finds it still waiting joins its
    # turn instead of starting a second run that would answer again. An
    # assignment's ``trigger_config.coalesce_window_seconds`` overrides it
    # (0..60); ``0`` turns the window off (the per-conversation ordering stays).
    chat_event_coalesce_window_seconds: int = 6
    # The last line behind that dedup, on the send itself
    # (``services/mail_send_guard.py``): before a Microsoft mail tool
    # sends (a new email, a draft, a reply), it takes a short Redis lock on the
    # conversation for the sending agent and looks for a message the agent
    # (any of its runs) already sent in that conversation within this many
    # seconds - its own record, then the mailbox's Sent Items - and refuses
    # the send naming that message unless the tool is called with
    # ``force_duplicate=true``. ``0`` turns the guard off.
    mail_send_guard_window_seconds: int = 300
    # Microsoft Graph change-notification subscriptions for integration-event
    # assignments (``services/graph_event_subscriptions.py``): their events
    # arrive within seconds instead of on the 5-minute poll. Only effective
    # when ``BASE_URL`` is a public https URL Graph can call; otherwise, or
    # False, every assignment is polled.
    integration_event_graph_subscriptions_enabled: bool = True
    # Jira dynamic webhooks for Jira integration-event assignments
    # (``services/jira_webhooks.py``): registered under the connected account,
    # refreshed before their 30-day expiry, deleted with the assignment. Only
    # effective when ``BASE_URL`` is a public https URL; the 60 s poll stays the
    # fallback either way.
    integration_event_jira_webhooks_enabled: bool = True

    # --- Inbound webhook receivers ---
    # Per-assignment fixed-window throttle applied by every webhook receiver
    # before the request body is read. Raising it suits alert-driven
    # assignments whose sender bursts above 60 deliveries/minute; the delivery
    # queue above still bounds how many of those become runs. Clamped to
    # ``>= 1`` at use time. A throttled delivery gets HTTP 429 + ``Retry-After``.
    webhook_requests_per_minute: int = 60
    # Event matching of integration-event and webhook triggers. A
    # trigger saved before exact matching existed keeps its prefix match
    # (``pull_request`` also fires on ``pull_request_review``) until a person
    # chooses its events again or switches it; true applies exact matching to
    # every trigger at once. Read through ``security_option()`` (also in
    # Settings > Access and security, the variable wins).
    webhook_exact_event_matching: bool = False
    # Idempotency window (seconds) for the webhook dedup layer: a delivery whose
    # key (``Idempotency-Key`` / ``X-GitHub-Delivery`` / ``Webhook-Id``, then a
    # provider id in the payload, then a SHA-256 of the URL scope + raw body)
    # was already seen within this window replays the first delivery's response
    # instead of firing a second run. Senders that emit no idempotency header
    # and no stable id fall back to the body hash, where two genuinely distinct
    # events with identical bodies inside the window collapse into one run —
    # lower this for such senders, or (better) have them send an
    # ``Idempotency-Key``. Clamped to ``>= 1`` at use time.
    webhook_dedup_ttl_seconds: int = 300

    # An OLE2 container with no dedicated extractor (a legacy Word ``.doc``,
    # an Outlook ``.msg``, an ``.xls`` xlrd cannot parse) is decoded as text:
    # its body is stored as plain 8-bit / UTF-16 runs that stay searchable.
    # ``True`` extracts such files to nothing instead.
    strict_binary_text_extraction: bool = False

    # Max seconds per KB source sync run.  The Pydantic validator below
    # clamps this to ``[1, 86_400]`` at settings-load time so a
    # misconfigured ``KB_SYNC_TIMEOUT_SECS`` env var (0, negative, or
    # >24h) is corrected at startup rather than silently passing a bad
    # value to PostgreSQL's ``statement_timeout`` or to
    # ``asyncio.wait_for``.  The scheduler also re-applies the same
    # clamp at runtime via ``_kb_sync_timeout_secs()`` as defence in
    # depth (tests monkeypatch this attribute directly, bypassing the
    # model validator).
    kb_sync_timeout_secs: int = 1800

    # --- KB sync stale / stuck detection ---
    # Maximum time a sync may go WITHOUT reporting progress (a heartbeat)
    # before ``reset_stale_syncs`` considers it stuck/orphaned and resets
    # it.  This is deliberately **decoupled** from ``kb_sync_timeout_secs``
    # (the per-attempt execution timeout): a legitimately long sync of a
    # large repo/drive keeps bumping ``sync_heartbeat_at`` and is never
    # falsely reset, while an orphaned sync — e.g. the worker that started
    # it was restarted/OOM-killed, so its watching ``asyncio.wait_for`` is
    # gone and the row would otherwise sit in ``syncing`` until
    # ``timeout + 5 min`` (~35 min by default) — recovers within minutes.
    # Clamped to ``[60, 86_400]`` at settings-load time.
    kb_sync_stale_after_secs: int = 600  # 10 min without a heartbeat
    # Cadence at which a running sync updates ``sync_heartbeat_at`` so the
    # stale sweeper can tell a live sync apart from an orphaned/hung one.
    # Must be well below ``kb_sync_stale_after_secs`` so a healthy sync
    # heartbeats several times within the stale window.
    kb_sync_heartbeat_interval_secs: int = 60

    # --- KB sync auto-retry (self-healing for failed / timed-out syncs) ---
    # When a scheduled KB source sync fails or times out, the scheduler
    # schedules an automatic retry with exponential backoff + jitter
    # (``base * 2^(failure_count - 1)``, capped at ``max_delay``) instead
    # of waiting the full sync interval (which could be daily/weekly) or
    # hammering a persistently-failing source every 60s tick. The failure
    # counter resets to 0 on the first successful sync.
    kb_sync_retry_base_delay_seconds: int = 300  # 5 min before the 1st retry
    kb_sync_retry_max_delay_seconds: int = 86_400  # 24 h cap (genuine errors)
    # A stuck/stale reset is usually a transient infrastructure event (a
    # worker restart) rather than the source's own fault, so it gets a
    # LOWER retry cap than a genuine error: a reset source retries within
    # minutes and never escalates to the multi-hour backoff a genuinely
    # broken source eventually reaches. This is what stops a single stuck
    # event from jumping straight to a ~3 h retry delay.
    kb_sync_stale_retry_max_delay_seconds: int = 1800  # 30 min cap for resets
    # Random jitter (0..N seconds) added to each scheduled retry so many
    # sources that failed together (e.g. a provider outage) don't all
    # retry on the same tick.
    kb_sync_retry_jitter_seconds: int = 60
    # Consecutive-failure count at which the scheduler escalates to an
    # error-level "persistent failure" log line and an admin-facing alert
    # (KB activity event + company SSE broadcast) so operators can act on
    # a source that repeatedly fails to sync.
    kb_sync_retry_alert_threshold: int = 5
    # Consecutive stale-reset count at which the scheduler quarantines
    # (disables) a sync source that repeatedly gets stuck in 'syncing'
    # without a heartbeat.  A source hitting this threshold is not just
    # failing — it hangs without cleanup, consuming sync capacity on every
    # retry.  The owner is notified via a KB activity event.  Recovery is
    # via force-resync or by re-enabling the source in settings.  Set to 0
    # to disable quarantine (never auto-disable).
    kb_sync_stale_quarantine_threshold: int = 5
    # Consecutive-error count at which the scheduler quarantines (disables)
    # a sync source that keeps returning clean failures — catching
    # permanently misconfigured sources (e.g. a renamed SharePoint folder
    # returning 404 on every sync attempt) that would otherwise retry
    # indefinitely with growing backoff.  Unlike the stale-reset quarantine
    # this targets clean error failures, not hung ones.
    # Recovery is via Force Re-sync or by re-enabling the source in
    # settings.  Set to 0 to disable error-based quarantine.
    kb_sync_failure_quarantine_threshold: int = 10
    # Consecutive permanent-error count at which the scheduler quarantines
    # (disables) a sync source that repeatedly returns permanently-
    # unresolvable errors (HTTP 404/410 - the configured path or resource no
    # longer exists).  A dedicated, stricter budget than the general
    # error-count quarantine above: a 404 can never succeed without
    # reconfiguration, so it is quarantined faster and flagged as "needs
    # reconfiguration" instead of being retried up to the general threshold.
    # Transient errors (5xx, timeouts, rate limits) reset this counter, so a
    # provider outage cannot trigger it.  Recovery is via Force Re-sync after
    # fixing the config or by re-enabling the source in settings.  Set to 0
    # to disable the permanent-error quarantine.
    kb_sync_perm_fail_quarantine_threshold: int = 5
    # Consecutive partial-page-failure count at which the scheduler emits a
    # one-shot alert for a sync source that keeps returning some page-level
    # errors (e.g. 404/400 Notion page refs within a larger database sync)
    # even though the overall sync succeeds (other pages synced OK).  Unlike
    # the quarantine thresholds above, crossing this threshold does NOT
    # disable the source — it fires a single informational alert (KB activity
    # event + SSE notification) so the operator can identify and prune the
    # stale page references.  Reset to 0 on the first clean sync (zero
    # page-level errors), a full sync failure (the streak is interrupted),
    # or a forced resync.  Set to 0 to disable this alert (partial failures
    # are then only visible in the warning logs).
    kb_sync_partial_fail_alert_threshold: int = 3

    # Consecutive 4xx-failure count at which the skill auto-refresh scheduler
    # stops retrying a URL-based skill whose remote source returns permanent
    # errors (e.g. HTTP 404 – path renamed or repo deleted).  A 4xx response
    # can never succeed without reconfiguration, so after this many daily
    # failures the skill is excluded from further auto-refresh attempts.
    # Transient errors (5xx, network timeouts) do NOT count toward this
    # threshold and do NOT reset it.  Recovery is by updating the skill URL
    # (which resets the counter) or by re-enabling the skill after fixing the
    # source.  Set to 0 to disable quarantine (retry indefinitely).
    skill_refresh_quarantine_threshold: int = 3

    # Consecutive authentication-failure count at which the skill auto-refresh
    # scheduler raises an operator-facing ERROR alert for a skill whose bound
    # GitHub credential keeps being rejected (401/403, or a 404 that the repo
    # probe attributes to the credential).
    #
    # This is deliberately an ALERT threshold, not a quarantine threshold: an
    # expired, revoked or under-scoped token is a recoverable
    # misconfiguration, so the skill keeps its daily retry (a rotated
    # credential then heals it with no operator action on the skill itself)
    # and is never disabled.  Auth failures do NOT advance
    # ``skill_refresh_quarantine_threshold`` — conflating them would report
    # "this URL is permanently broken" when the real fix is rotating a token.
    # The counter resets to 0 on any successful refresh, and whenever the URL
    # or the bound credential changes.  Set to 0 to disable the alert
    # (failures are still logged individually at WARNING).
    skill_refresh_auth_fail_alert_threshold: int = 3

    # --- Progressive skill disclosure ---
    # Instead of concatenating every attached skill body into the system
    # prompt, the prompt carries a name/description catalog and the agent pulls
    # a body in with the ``load_skill`` tool.  These three knobs bound the
    # worst case so one pathological skill (or a model that keeps loading)
    # cannot blow the context window.
    #
    # Maximum characters returned by a single ``load_skill`` call.  Longer
    # bodies are truncated with an explicit marker naming how much was cut —
    # never silently, because a silently halved SKILL.md reads as a complete
    # (but wrong) instruction set.
    skill_load_max_chars: int = 60_000
    # Maximum number of DISTINCT skills a single run may load.  Reloading an
    # already-loaded skill is idempotent and never counts again, so this caps
    # the total body volume a run can pull in at
    # ``skill_load_budget × skill_load_max_chars``.  0 = unlimited.
    skill_load_budget: int = 8
    # Maximum bytes returned by a single ``read_skill_resource`` call
    # (packaged ``scripts/`` / ``references/`` / ``assets/`` files).  Lower
    # than the SKILL.md cap: a reference file is meant to be consulted in
    # fragments, and binary assets are reported as metadata rather than dumped.
    skill_resource_max_bytes: int = 40_000

    # --- Assignment auth-failure quarantine ---
    # Consecutive-run count at which the assignment scheduler auto-disables
    # (quarantines) a scheduled assignment whose integration tools keep
    # returning authentication/permission failures across separate runs —
    # catching expired / revoked OAuth tokens or API keys that would otherwise
    # burn cycles indefinitely until a human notices.
    #
    # Each SCHEDULER-triggered run (``schedule`` / ``schedule_fanout``) that
    # contains at least one integration tool auth-failure result increments
    # the counter — manual, webhook, and work-item runs never advance it, so
    # a user retrying while diagnosing a broken credential cannot quarantine
    # their own assignment.  A run with no auth failures (any trigger type)
    # resets the counter (so a rotated credential clears the alert
    # immediately on the next successful run).  The counter is stored in
    # Redis with a 7-day TTL (auto-expires after a week of silence).
    #
    # When the threshold is crossed:
    #   1. The assignment is disabled (``enabled = False``).
    #   2. A durable ``assignment_auth_quarantine`` Activity record is
    #      persisted for the assignment owner.
    #   3. An SSE notification is published to the owner's personal feed;
    #      company-managed (or owner-less) assignments broadcast to the
    #      company-wide feed instead, matching the scoping of every other
    #      assignment notification.
    #
    # Recovery: re-authorize the integration then re-enable the assignment.
    # Set to 0 to disable auto-quarantine (monitor-only via ERROR logs).
    assignment_auth_fail_quarantine_threshold: int = 3

    # --- Assignment stale-reference pause ---
    # An integration that keeps reporting a resource the run uses as missing -
    # a Notion database deleted or no longer shared with the integration, once
    # it failed in two separate runs - fails the run it serves
    # (``failure_cause = stale_integration_reference``, the agent's summary
    # kept) and its owner is told once. After this many CONSECUTIVE scheduled
    # runs failed that way the assignment is paused
    # (``paused_reason = stale_reference``); re-enabling it starts a fresh
    # count. Manual / webhook / event runs neither advance nor reset the
    # count, whatever their outcome; a scheduled run that succeeds or fails for
    # another reason resets it; built-in or per-user fan-out assignments are
    # reported, never paused. The default stops an hourly job about three
    # hours after its reference went stale. 0 disables the pause (runs still
    # fail and still report).
    assignment_stale_reference_pause_after: int = 3

    # --- Model prices source + git-backed sync ---
    # Optional override for the model-prices source file used by
    # ``services.tracing.price_loader``. When empty, the loader uses:
    #   - ``MODEL_PRICES_GIT_SYNC_TARGET_PATH`` if git sync is enabled
    #   - otherwise the versioned seed file ``app/data/model_prices.json``.
    model_prices_source_path: str = ""
    # Enable periodic pull of LiteLLM's model pricing/context map from the
    # configured URL into a local JSON file.
    model_prices_git_sync_enabled: bool = False
    # Raw JSON URL to pull. Empty uses the built-in LiteLLM staging URL.
    model_prices_git_sync_url: str = ""
    # Local file path receiving pulled JSON payloads.
    model_prices_git_sync_target_path: str = ""
    # Pull cadence in minutes (autonomous tick runs every 60s).
    model_prices_git_sync_interval_minutes: int = 360
    # HTTP timeout for each pull attempt.
    model_prices_git_sync_timeout_secs: int = 30

    # --- Tracing local flush pipeline ---
    tracing_queue_maxsize: int = 100
    tracing_overflow_ttl_seconds: int = 86_400
    tracing_overflow_max_entries: int = 10_000
    tracing_db_pool_size: int = 5
    tracing_db_max_overflow: int = 5
    tracing_store_provider: str = "postgres"
    clickhouse_host: str = "localhost"
    clickhouse_port: int = 8123
    clickhouse_username: str = "default"
    clickhouse_password: str = ""
    clickhouse_database: str = "default"
    clickhouse_secure: bool = False
    clickhouse_connect_timeout_seconds: int = 5

    # --- Trace payload storage ---
    # Trace payloads are audit material and are never truncated on the durable
    # path.  A payload above the offload threshold is written to object storage
    # and replaced in the row by a pointer envelope (see
    # ``services/tracing/payload_offload.py``), which keeps multi-megabyte tool
    # results out of the Postgres primary, the tracing queue and the ClickHouse
    # insert batch without losing a single byte.
    #
    # ``trace_payload_hard_max_bytes`` only matters when object storage is
    # unreachable: below it an offload failure stores the payload inline
    # (fidelity beats tidiness), above it the row records the failure so the
    # loss is visible rather than silent.  Set the threshold to 0 to disable
    # offloading entirely and always store inline.
    trace_payload_offload_threshold_bytes: int = 1_048_576  # 1 MiB
    trace_payload_hard_max_bytes: int = 33_554_432  # 32 MiB

    # Media blobs (base64 images, PDFs) inside a trace payload are written to
    # object storage individually and replaced by a reference carrying their
    # content type, byte size and digest (see
    # ``services/tracing/media_offload.py``).  They used to be replaced by the
    # placeholder ``"[image]"``, so the image an agent actually saw was not
    # recoverable from its trace.  Disable to fall back to that stripping —
    # the external Langfuse export strips either way, because its SDK would
    # otherwise try to upload every blob to its own media storage.
    trace_media_offload_enabled: bool = True

    # --- Knowledge-base retrieval ---
    # Per-statement timeout applied to KB vector + hybrid search queries.
    # The global pool default is 30 000 ms (see core/database.py); a
    # tighter 5 000 ms cap on search hot-path queries forces individual
    # bad plans (stale pgvector cost estimates, missing index hint,
    # etc.) to fail fast instead of holding a pooled connection long
    # enough to starve concurrent RAG requests.  Use 0 to inherit the
    # global pool timeout (no per-query cap), or a higher value if
    # legitimate searches over very large KBs need more headroom.
    kb_search_statement_timeout_ms: int = 5000

    # Per-statement timeout (seconds) applied to the bulk KB embedding
    # purge that runs when the platform embedding model/dimensions
    # change (see ``_reset_kb_embeddings_for_new_model`` in
    # api/v1/settings.py). The global pool default is 30 000 ms
    # (core/database.py); a single ``DELETE FROM embeddings`` over a large
    # corpus easily exceeds that and gets cancelled
    # (``QueryCanceledError: canceling statement due to statement timeout``),
    # which used to surface as a 500 on the AI Models save. We raise the
    # per-transaction timeout for that one operation only — same pattern as
    # the KB sync scheduler, which already lifts the cap for embedding
    # deletes. ``0`` disables the per-statement cap (waits indefinitely).
    embedding_reset_statement_timeout_secs: int = 600

    # Per-transaction statement timeout (seconds) of a sandbox workspace
    # commit (``WorkspaceStore.commit`` in services/sandbox_workspace/store.py).
    # A checkout of thousands of files reads, deletes and upserts its manifest
    # under the workspace row lock, and a second commit of the same workspace
    # waits on that lock: both ran past the 30 000 ms pool default
    # (core/database.py) and the tool failed with ``QueryCanceledError`` on
    # ``SELECT ... FOR UPDATE`` or ``UPDATE sandbox_workspaces``. ``0`` keeps
    # the pool default.
    sandbox_workspace_commit_statement_timeout_secs: int = 300

    # --- Admin user (mandatory, overwritten on every startup) ---
    admin_email: str = "admin@filigran.io"
    admin_password: str = ""
    admin_token: str = ""

    # --- Auth – JWT ---
    jwt_algorithm: str = "HS256"
    # --- Auth – web sessions ---
    # The absolute lifetime of a web session, from its sign-in, however much it
    # is used (7 days by default), and how long one may go unused before it
    # ends (0 = no idle timeout, the default). Both also in Settings >
    # Security: the variable, when set, wins and locks the field
    # (``core/security_options.py``, read through ``security_option``). See
    # ``core/sessions.py``.
    jwt_expire_minutes: int = 60 * 24 * 7
    session_idle_timeout_minutes: int = 0

    # --- Auth – password policy ---
    # Enforced on registration, invitation registration, password change, and
    # admin-created local accounts. In addition to length, passwords must
    # contain at least one letter and one digit (see security.check_password_policy).
    # Also in Settings > Access and security: the variable, when set, wins and locks the
    # field (``core/security_options.py``, read through ``security_option``).
    min_password_length: int = 8

    # --- Terms of Use acceptance gate ---
    # A blocking consent screen shown after authentication until the user
    # explicitly accepts the current Terms of Use. Deliberately NOT a
    # ``feature_flags.py`` flag: that registry is fail-CLOSED (unset means
    # off), and a legal-consent gate must default ON. Set
    # ``TERMS_OF_USE_ENABLED=false`` for self-hosted / on-premise
    # deployments that manage acceptance outside the product.
    #
    # ``terms_of_use_version`` is the identity of the published document.
    # Changing it re-opens the gate for every user on their next request, so
    # a ToU revision is a deliberate config/deployment change (there is no
    # admin UI toggle). A blank value is treated as "disabled"
    # (see ``terms_of_use_active``) so a misconfigured deployment degrades to
    # no gate instead of locking every user behind a version nobody can ever
    # match.
    terms_of_use_enabled: bool = True
    terms_of_use_version: str = "2026-07-14"
    terms_of_use_url: str = "https://filigran.io/ai-terms/"
    # Comma-separated emails the gate never applies to: the vendor accounts
    # that operate a SaaS instance, so they can sign in from the first day of a
    # deployment without accepting the terms on the customer's behalf. Matched
    # exactly, case-insensitively, against the signed-in user's email (see
    # ``terms_of_use_exempt``). The platform stays unaccepted for everyone else.
    terms_of_use_exempt_emails: str = ""

    # --- URLs ---
    # The platform's own public URL: OAuth redirect URIs, webhook and e-mail
    # links, the MCP origin check. The default is the dev stack's backend
    # (``docker-compose.dev.yml``); ``docker-compose.yml`` sets its own, and any
    # other deployment sets ``BASE_URL`` (a warning says so at startup).
    base_url: str = "http://localhost:8100"
    frontend_url: str = "http://localhost:3000"
    # URLs the Suite platforms are configured to reach XTM One on, besides
    # ``BASE_URL`` (e.g. ``http://xtm-one:4000`` in Docker Compose): accepted as
    # the audience of their cross-platform tokens, for platform versions that
    # address tokens to that URL instead of the identity published at
    # ``/xtm/auth/metadata``. Comma-separated http(s) URLs; a malformed entry
    # fails startup.
    xtm_auth_accepted_audiences: Annotated[list[str], NoDecode] = []
    # URL path prefix the platform is served under when a reverse proxy mounts
    # it somewhere other than ``/`` - ``/xtm-one`` for
    # ``https://intranet.example.com/xtm-one/``. Blank = the root, the
    # default for every existing deployment. Works whether the proxy forwards
    # the prefix or strips it (see ``app/core/base_path.py``); ``BASE_URL``
    # must carry the same prefix, because every absolute link the backend
    # hands out (OAuth callbacks, webhook URLs, SSO redirects, e-mails) is built
    # from it. Normalised to one leading slash and no trailing slash; a prefix
    # colliding with a platform namespace (``/api``, ``/mcp``, ...) is refused.
    base_path: str = ""
    # What the reverse proxy does with the prefix: ``auto`` (default) infers
    # the shape of every request - an ``X-Forwarded-Prefix`` header naming the
    # prefix, else the path - and refuses at startup a prefix that is also the
    # address of a platform page (``/tools``, ``/agents``, ...), which that
    # inference cannot resolve behind a stripping proxy; ``forwarded`` declares
    # the proxy keeps the prefix, ``stripped`` that it removes it and sends the
    # header (Traefik's StripPrefix does). Either declaration lifts the
    # refusal. See ``app/core/base_path.py``.
    base_path_mode: str = "auto"
    # Where the headless Chromium behind presentation export loads the
    # deck's static render page from. Empty = auto: the loopback address of the
    # unified image when it serves the frontend itself, ``FRONTEND_URL``
    # otherwise (split dev setup). Set it explicitly when the platform sits
    # behind an ingress the pod cannot reach or an auth proxy that would
    # intercept the render page (the render token is the page's only credential).
    presentation_render_base_url: str = ""
    # Whether a saved deck is rendered in that same headless Chromium right
    # after ``generate_presentation`` saves it, to measure what the schema
    # cannot see - a body shrunk to fit, text cut by a box, text painted past
    # the canvas - and report it to the agent in the tool result so it
    # fixes the slide before answering. Off = the check is skipped everywhere
    # (tool result, ``get_presentation``, ``POST .../layout-check``).
    presentation_layout_check_enabled: bool = True
    # Whether a saved video is played in the compositor's headless Chromium
    # right after ``create_video`` / ``edit_video`` save it, to measure what
    # the spec cannot show - text covering text, text past the frame, text
    # shrunk to fit, text still coming in when its scene ends - and
    # report it to the agent in the tool result. Off = the check is skipped
    # everywhere (tool results, ``GET .../quality``).
    video_quality_check_enabled: bool = True
    # Whether a deck's managers may give it a public link that opens it
    # read-only for anyone, without an account. Off = every public link
    # answers 404 at once and none can be created (Share dialog and agent
    # tool); the links are kept, so turning it back on restores those that
    # were not turned off. Also in Settings > Access and security: the variable, when
    # set, wins and locks the field.
    presentation_public_links_enabled: bool = True
    # Whether a video's managers may give it a public link that plays its
    # current render for anyone, without an account. Off = every
    # public video link answers 404 at once and none can be created; the links
    # are kept, so turning it back on restores them. Also in Settings >
    # Security: the variable, when set, wins and locks the field.
    video_public_links_enabled: bool = True
    # Whether a custom dashboard's managers may publish it as a link anyone
    # can open without an account, showing a snapshot of its data read with
    # the publisher's access. Off by default: a dashboard reads platform data
    # an administrator should choose to expose. Off = every published link
    # answers 404 at once and none can be created; the links are kept, so
    # turning it on again restores them. Also in Settings > Access and security: the
    # variable, when set, wins and locks the field.
    dashboard_public_links_enabled: bool = False

    # --- Browser profiles ---
    # Whether a browser profile may open an application on a private network
    # (the hosts it declares) - its sign-in, an agent browsing with it, a
    # Video Studio recording - under the operator-network policy: loopback,
    # link-local and cloud metadata addresses stay refused. Off = profiles
    # reach public addresses only, like the agent browser tool. Also in
    # Settings > Access and security: the variable, when set, wins and locks it.
    browser_private_targets: bool = False

    # --- Web fetch trust ---
    # PEM certificates (a private CA, or a host's self-signed certificate)
    # ADDED to the trust store of the web page fetches (web_fetch, the
    # knowledge base URL crawl, Deep Investigation pages, HTTP sources;
    # core/pinned_request.py). Verification stays on and the host name must
    # still match: nothing is ever fetched with verification off. Empty =
    # the system store alone. Also in Settings > Access and security: the
    # variable, when set, wins and locks the field.
    web_fetch_trusted_ca_certificates: str = ""

    # --- Video Studio ---
    # The encoder binaries (the platform image ships both); blank = the ones
    # on PATH.
    video_studio_ffmpeg_path: str = ""
    video_studio_ffprobe_path: str = ""
    # The longest recording, and the longest video a render produces.
    video_studio_max_take_seconds: int = 300
    video_studio_max_video_seconds: int = 1200
    # Renders and recordings one worker runs at a time (each holds Chromium
    # and ffmpeg); clips only wait on a provider, so more of them run. 0 = this
    # worker draws no whole render nor scene range: scene previews only.
    video_studio_render_concurrency: int = 1
    # Scene previews one worker runs at a time: a lane of their own,
    # beside the whole renders, never on their slots. 0 = this worker draws no
    # preview and keeps no preview reserve.
    video_studio_preview_concurrency: int = 2
    # The most scene previews one worker runs at once while previews are
    # queued: above VIDEO_STUDIO_PREVIEW_CONCURRENCY, a slot added for
    # each preview queued beyond its slots, as many as the CPUs its whole
    # renders leave hold, dropped as the queue empties. 0 = static.
    video_studio_preview_concurrency_max: int = 0
    # Recordings one worker runs at a time. 0 = this worker records none: the
    # recording lane is kept to the workers that do, a GPU pool. A recording
    # lane (VIDEO_STUDIO_RECORDING_GL=gpu) records one at a time whatever this says.
    video_studio_take_concurrency: int = 2
    # What a recording's Chromium draws its WebGL and canvases on:
    # `auto` = the worker's GPU where Chromium finds one to draw WebGL on,
    # SwiftShader (software GL, on the CPU) otherwise; `software` = SwiftShader
    # always, never looking for a GPU; `gpu` = the GPU or nothing: the
    # recording lane of a GPU node, probed at the worker's start (its GL
    # renderer logged), and a worker whose Chromium reads SwiftShader there
    # claims no recording rather than recording one in software.
    video_studio_recording_gl: str = "auto"
    video_studio_clip_concurrency: int = 4
    # Renders one person may have queued or running at once, across all their
    # videos; beyond it a render is refused with the ones in flight listed.
    video_studio_max_renders_per_user: int = 10
    # How long generate_video_clip waits for its shot inside the call,
    # the same for every call of a batch, from the call's start: past it the
    # shot finishes in the background and the answer says why. Never more
    # than the call's own deadline allows. 0 = always in the background.
    video_studio_clip_inline_wait_seconds: int = 200
    # Headless pages one render draws its frames with, each with its encoder:
    # never more than the worker's CPUs, nor than its memory ceiling holds.
    # 0 = the worker's CPU quota less the one its own jobs keep.
    video_studio_render_pages: int = 0
    # The cgroup v2 cpu.weight a whole render's process tree draws at, against
    # the 100 of the worker's other jobs (assignment runs, scene previews),
    # where the worker's cgroup is delegated; else the same ratio as a raised
    # priority where the worker holds CAP_SYS_NICE. 0 = a render draws
    # at the worker's own priority.
    video_studio_render_cpu_weight: int = 1000
    # How a render captures each frame: `screenshot` (Page.captureScreenshot),
    # `begin_frame` (HeadlessExperimental.beginFrame: Chromium produces exactly one
    # frame, painted whole, when the frame is asked - Linux only), `auto` (begin_frame
    # on Linux, screenshot elsewhere). A page where begin_frame cannot run takes screenshots.
    # begin_frame stays opt-in: on the CI headless shell a begun frame never returned.
    video_studio_frame_capture: str = "screenshot"
    # The scene ranges a render is split into at most when one worker cannot
    # draw it within the 4 h a render may run, each range drawn on a worker of
    # its own. 1 = a render is never split.
    video_studio_render_max_ranges: int = 8
    # A render that would take longer than this many minutes on the pages its
    # worker's memory opens is split into scene ranges across the workers
    # there, as many as bring each range under it; with one worker it
    # is drawn whole. 0 = split only past the 4 h a render may run.
    video_studio_render_split_minutes: int = 30
    # Each render runs in a process of its own, stopped and failed with
    # its peak memory once it and everything it started (Chromium, ffmpeg) use
    # more than this many MiB. 0 = 60% of the container's memory limit (4096
    # MiB when it sets none), so a render never takes the worker down with it.
    # Never more than the container's limit less the preview reserve below.
    video_studio_render_memory_mb: int = 0
    # The ceiling of an ultra (2160p60) render, in MiB: at least this
    # (16 GiB by default), raised where the container has the memory to what
    # the pages its CPUs want need at their measured 2160p peak, never
    # past the container's memory limit (the host's without one) less 4 GiB
    # kept for the worker's own jobs or the preview reserve, whichever is
    # larger, never less than the other renders' ceiling. 0 = an ultra render
    # is held to the same ceiling as the others.
    video_studio_ultra_render_memory_mb: int = 16384
    # The memory every worker that draws scene previews keeps out of every
    # render's reach, in MiB: a hard floor of 8192, the largest
    # preview's worst case - a value below it keeps 8192, one above
    # raises it. A render's ceiling is never more than the container's limit
    # (the host's memory without one) less this, so a render is admitted only
    # where that covers it and its pages are laid out in what is left.
    video_studio_preview_reserve_mb: int = 8192
    # The ceiling of a scene preview's process, in MiB. 0 = twice the
    # estimate of the preview at its own size (720p30 or 1080p30) on its
    # pages, within the preview reserve, never more than the worker's memory.
    # A preview starts on its pages only: one its worker does not hold goes to
    # the worker with room, or waits for its pages.
    video_studio_preview_memory_mb: int = 0
    # The screen-insert frames a worker keeps on its disk (its temporary
    # directory), in MiB: drawn once, read back by every page, attempt
    # and render that shows them again at the same size. Never more than a
    # quarter of what that disk has free; the least recently used go first,
    # and what nothing read for 6 h. 0 = none is kept.
    video_studio_insert_frames_mb: int = 1024
    # A render whose progress stands still this many seconds is stopped and
    # failed as stalled at its last progress. 0 = never.
    video_studio_render_stall_seconds: int = 120
    # How long a stopping worker waits for the whole renders it draws past
    # half of their frames to end before it hands them over, from the
    # stop signal - never past its termination grace
    # (WORKER_TERMINATION_GRACE_SECONDS) less the time the hand-over takes.
    # Those under half go to a running worker at once. 0 = never waits.
    video_studio_render_drain_seconds: int = 1200
    # How long a recording's and an AI shot's own media are kept once no video
    # uses them: media a video uses are never deleted. This
    # many days after the last video using them stopped (removed from its
    # timeline, or the video deleted), and - for media no video ever used -
    # this many days after they were made. A rendered video keeps its own MP4;
    # "Keep" on a recording or a shot exempts it. 0 = kept forever.
    video_studio_media_retention_days: int = 30
    video_studio_unused_media_retention_days: int = 14
    # The editor's figure stills: a still is deleted this many days
    # after it was drawn; one a video still shows is drawn again by the next
    # save or preview that finds it missing. 0 = kept forever.
    video_studio_figure_still_retention_days: int = 30

    # --- Check files ---
    # The pictures a tool makes for the agent to look at (frames, region
    # crops, contact sheets, insert sweeps, slide captures, page renders;
    # ``FileAttachment.source = "agent_check"``) are deleted this many days
    # after they were made, except one a video uses. 0 = kept forever.
    agent_check_file_retention_days: int = 7

    # --- File retention (Settings > Storage & retention) ---
    # Days each category of stored file is kept; the value an administrator
    # stores in Settings wins, unless the variable is SET in the environment,
    # which locks it (``services/file_retention.py``). 0 = kept forever.
    # The agent's scratch files (``agent_working``) and delivered files
    # (``agent_download``) count from their LAST USE (download, preview, read,
    # attach); the others from their creation. A file an open work item, a
    # workset, an active shared state, a presentation, a report or a video
    # still names is kept. The bytes go, the row stays as a tombstone so the
    # chat shows a "deleted" card.
    file_retention_agent_working_days: int = 60
    file_retention_agent_download_days: int = 60
    file_retention_agent_days: int = 0
    file_retention_chat_days: int = 0
    file_retention_channel_days: int = 0

    # --- Waiting arcade ---
    # The Space Invaders game a user can take control of while an agent works:
    # the full-screen arcade, the per-user scores and the cross-user
    # leaderboard. Off = the chat's waiting panel keeps only its self-playing
    # animation and every ``/api/v1/arcade`` route answers 404.
    arcade_enabled: bool = True

    # --- Reverse proxy / trusted forwarded headers ---
    # ``FORWARDED_ALLOW_IPS`` is the recommended setting behind a proxy
    # (uvicorn trusts the headers only from those peers and reads the chain
    # from its end). This flag relaxes that boundary, so it is reported once
    # at startup and by the security audit while on (``app.core.proxy_headers``).
    # When True, the rate limiter, the audit trail and every other
    # request-IP-aware code path take the leftmost ``X-Forwarded-For``
    # value as the client IP (``app.core.utils.request_client_ip``; a value
    # that is not an IP address is ignored). MUST stay ``False`` for
    # deployments where the backend is reachable directly by clients (or by
    # a proxy that does not strip / rewrite the header), otherwise any
    # client can spoof the header to bypass per-IP throttling on
    # ``/auth/login``, ``/auth/forgot-password``, etc. and pick the address
    # the audit trail records. Set to ``True`` only behind a reverse proxy
    # that is known to overwrite this header with the real client IP (nginx
    # with ``proxy_set_header X-Forwarded-For $remote_addr``, Cloudflare
    # with ``CF-Connecting-IP`` re-mapped, etc.). A proxy that appends to the
    # header (nginx's ``$proxy_add_x_forwarded_for``) leaves the caller's own
    # value leftmost: set ``FORWARDED_ALLOW_IPS`` for it instead (UPGRADING.md).
    # Also in Settings > Access and security, like the two fail modes below: the
    # variable, when set, wins and locks the field (read every one of them
    # through ``core/security_options.security_option``).
    trust_proxy_headers: bool = False
    # Opt-in: key, log and store every client address in its canonical form
    # (``app.core.utils.client_address``: port, brackets and IPv6 zone index
    # dropped, IPv4-mapped read as IPv4, IPv6 lowercase), so one client is one
    # rate-limit key whatever the proxy wrote - a proxy that writes ``IP:port``
    # into ``X-Forwarded-For`` otherwise makes every connection its own bucket
    # under ``trust_proxy_headers``. Off, an address is used as it was read, as
    # on earlier releases. A value that is not an IP address is ignored either way.
    client_ip_strict: bool = False
    # Prefix length the per-IP rate limits key a native IPv6 client on
    # (``app.core.rate_limit.client_bucket``). 128, the default, is one
    # bucket per address. 64 is opt-in: it stops a client drawing a fresh
    # bucket from every address of the /64 it is handed, but every device of
    # one /64 (a corporate IPv6 LAN) then shares one budget. IPv4 is always
    # keyed per address.
    rate_limit_ipv6_prefix: int = 128

    # --- Content-Security-Policy (``app/core/csp.py``) ---
    # ``report-only`` (default): browsers block nothing and report what the
    # policy would have blocked to ``/api/v1/csp-report``. ``enforce``: the
    # same policy, blocking. ``off``: no policy header. A typo fails startup
    # rather than silently meaning another mode.
    content_security_policy_mode: str = "report-only"
    # Origins, besides the platform itself and the platforms registered with it
    # (those only behind an operator-chosen PLATFORM_REGISTRATION_TOKEN), that
    # may frame the platform's pages once the policy is enforced, e.g. an
    # intranet portal embedding XTM One in an iframe: comma-separated CSP
    # sources such as ``https://portal.example.com`` or ``https://*.example.com``.
    # A malformed entry fails startup; one that admits every site is logged.
    content_security_policy_frame_ancestors: Annotated[list[str], NoDecode] = []

    # --- Fail modes for rate limiting and quota enforcement ---
    # What happens when the backing store (Redis / DB) is unreachable
    # while a rate-limit or quota check runs:
    #   "open"   (default) — skip the check and let the request through;
    #            an infrastructure hiccup never blocks users, but limits
    #            are unenforced for its duration.
    #   "closed" — reject the request (503 for rate limits, the regular
    #            quota-refusal surface for quota checks) until the
    #            backend recovers. Recommended for enterprise
    #            deployments where unmetered usage is unacceptable.
    # In XTM One mode (xtm_one, ai_disabled) quotas always fail closed,
    # whatever QUOTA_FAIL_MODE says: quotas are what the licence sells
    # (``core/security_options.py`` pins it).
    rate_limit_fail_mode: str = "open"  # "open" | "closed"
    quota_fail_mode: str = "open"  # "open" | "closed"

    # --- Agent failure quota pool (env-only, no UI surface) ---
    # An agentic execution is metered up-front, before the agent runs, so a
    # run that ends in failure still spends one action of the user's agentic
    # action quota. That is correct when the failure is the user's
    # (a bad prompt, an exhausted context) and wrong when it is not (a
    # provider outage, a broken tool, a crash) — and the user cannot tell
    # the two apart. This pool forgives a BOUNDED number of failures per
    # agent instead of trying to classify them: the first
    # ``agent_failure_quota_pool`` failed runs of a given agent, for a given
    # user, in the current agentic quota period are refunded; every failure
    # after that is charged normally, so a permanently broken agent cannot
    # run for free. The pool resets with the quota period it is counted in.
    #
    # Deliberately env-only (no PlatformSettings column, no admin UI): this
    # changes what is billable, so it belongs to whoever operates the
    # deployment, not to whoever administers the tenant.
    #
    # On by default: spending a user's action on a run the platform failed to
    # deliver is the wrong default, and the pool is what keeps that from turning
    # into free unlimited retries. Set the flag false (or the pool to 0) to
    # deduct an action for every failure, like a success.
    agent_failure_quota_refund_enabled: bool = True
    # Pool size: refunded failures per (user, agent, quota period).
    # ``0`` (or any negative value) means "refund nothing" even when the
    # flag above is on, so the pool can be closed without a redeploy of the
    # enable flag.
    agent_failure_quota_pool: int = 5

    # --- A2A inbound server (offered exactly while agentic chat is; see
    # ``api.v1.platform.is_chat_enabled``) ---
    # Rate limits are the ONLY control this feature places on external load:
    # the specification deliberately sets no latency target and no cap on
    # concurrent external runs, so total external submission rate is bounded
    # by (number of exposures x a2a_task_create_rate_limit). Splitting
    # creation from reads is not cosmetic - one submission starts a full
    # agent run, one read is a single indexed lookup - so a single number
    # would either throttle the polling the architecture requires or admit a
    # submission burst that floods the run queue.
    a2a_task_create_rate_limit: int = 10  # per minute, per registration
    # Sized from measurement, not from an estimate of how partners poll:
    # Foundry — the primary target platform — polls ``tasks/get`` in a
    # zero-backoff loop MEASURED at 8-10 requests per SECOND (ngrok capture,
    # 2026-08-20), and surfaces any 429 to its user as a failed delegation
    # rather than honouring ``Retry-After``. 30/s gives the measured client
    # 3x margin, and a read is a single indexed lookup, so headroom is cheap.
    a2a_task_read_rate_limit: int = 1800  # per minute, per registration
    a2a_rate_limit_window_seconds: int = 60
    # Per client IP, BEFORE authentication - covering the anonymous card fetch
    # and the jsonrpc requests that fail auth, which the per-registration
    # limits above (bound to a verified credential) cannot reach.
    a2a_unauthenticated_rate_limit: int = 60  # per minute, per client IP

    # Two hard ceilings on what one submission may cost, because the rate
    # limits bound the NUMBER of requests and say nothing about the size of
    # each. Neither is a tuning knob for normal traffic: a legitimate
    # delegation is orders of magnitude below both.
    #
    # ``a2a_max_request_bytes`` is checked against ``Content-Length`` before the
    # body is read, so an oversized payload is refused without being parsed.
    # ``a2a_max_message_chars`` bounds the FLATTENED message after part
    # validation — the value that is persisted verbatim in
    # ``A2ATask.inbound_parts`` and handed to the executor as the prompt, and
    # the one a chunked request (no Content-Length) still has to pass.
    a2a_max_request_bytes: int = 1_048_576  # 1 MiB of JSON envelope
    a2a_max_message_chars: int = 100_000  # flattened prompt text

    # A blocking send holds the HTTP response open until the task is
    # terminal, polling the projection at the interval below. It exists
    # because the reference SDK's v1.0 client has NO polling loop: it makes
    # exactly one SendMessage call and treats what comes back as the final
    # answer (blocking is its default; ``returnImmediately`` is the opt-out).
    # A run that outlives the timeout returns in whatever state it reached —
    # the same non-terminal task an immediate return would have carried.
    # The lease (a2a_run_lease_seconds) bounds the true worst case; this cap
    # only bounds how long one HTTP connection is held for it.
    a2a_blocking_send_timeout_seconds: int = 120
    a2a_blocking_poll_interval_seconds: float = 1.0

    # Task creation deduplicates on the client-minted message id. A long
    # window is safe precisely BECAUSE the key is client-minted and unique:
    # a partner re-sending the same id a day later is by definition referring
    # to the same message, not asking a new question.
    a2a_dedup_window_seconds: int = 86400  # 24 hours

    # How long an external run may hold its lease without a heartbeat before
    # the reaper drives it to a terminal state. Without this a run whose
    # replica died stays pollable forever, which the specification forbids.
    a2a_run_lease_seconds: int = 900

    # How often a running external task checks whether it has been cancelled
    # (a2a.tasks._supervise_run). Deliberately not derived from the lease: this
    # is the delay a user who says stop sits through, while the lease measures
    # how long a dead replica may go unnoticed. It is also steady-state query
    # load on every running task, which is why it is tunable.
    a2a_cancel_poll_seconds: int = 15

    # Cadence of the loop that enforces the lease (main._a2a_reaper_loop).
    # Every worker sweeps independently — the reap claims rows with
    # FOR UPDATE SKIP LOCKED, so concurrent sweeps take disjoint work.
    a2a_reap_interval_seconds: int = 60

    # Total life of a task, distinct from the lease above. A task whose run
    # delegated background work stays non-terminal while that work runs
    # (spec 010 FR-001) and keeps renewing its lease to say so, so the lease
    # cannot double as the total bound. Four hours: long enough for a
    # delegation chain, short enough that a partner is never left polling a
    # task for a day.
    a2a_task_max_lifetime_seconds: int = 14400

    # Push notifications: how hard the platform tries to reach a partner's
    # callback, and how many callbacks one task will hold. The attempt budget
    # is the whole bound — a callback is registered per task, so there is no
    # shared target for the client breaker to hold state on.
    a2a_push_max_attempts: int = 5
    a2a_push_configs_per_task: int = 5

    # --- A2A client (outbound): this platform tasking a partner's agent ---
    # Availability is not a setting here: A2A is offered exactly while agentic
    # chat is (``api.v1.platform.is_chat_enabled``), the same gate as the
    # inbound half. Otherwise nothing outbound is attempted. The
    # bounds below are tuning values and correctly live here.
    # How often the sweep re-verifies partner cards. The sweep is an
    # optimisation, NOT the binding control: a cycle that never runs widens
    # nothing, because a delegation re-verifies for itself once the recorded
    # verdict is older than ``a2a_client_verdict_staleness_seconds`` (FR-050).
    a2a_client_sweep_interval_seconds: int = 3600
    # The age past which a delegation MUST re-verify before submitting, and
    # MUST refuse if that re-verification cannot complete. This is the number
    # that makes "delegation stops when a partner changes its card" a
    # measurable promise rather than an undefined cycle (FR-050, SC-008).
    a2a_client_verdict_staleness_seconds: int = 900
    # Ceiling on ONE delegation attempt, further capped by whatever remains of
    # the calling run's own deadline. Per attempt: a continuation after
    # ``input-required`` starts a fresh one, so the total a partner can hold is
    # bounded by the run's deadline, not by this (FR-024).
    a2a_client_delegation_ceiling_seconds: int = 300
    # How long a delegation may stay outstanding — go without reaching a
    # terminal state — before it is ended and its caller told so. It is not a
    # measure of the partner's silence: one that answers every collect with
    # "working" is bounded by this too, because a row that never concludes is
    # what has to end. Deliberately not derived from the ceiling above: that
    # bounds one attempt's WAIT, and the hand-back and the callback exist
    # precisely because the work outlasts the attempt. The
    # default is this platform's own ``a2a_task_max_lifetime_seconds`` — the
    # longest a task may live on the surface we ourselves serve — because
    # presuming a partner gone sooner than that would have the two halves of
    # this feature disagreeing about the same piece of work. The floor keeps
    # ASKING for the whole window, so a longer one costs polling, not silence.
    a2a_client_delegation_abandoned_seconds: int = 14400
    # Largest partner response body ingested before the delegation fails
    # (FR-027). Text extracted from it is then bounded by the existing
    # ``a2a_max_message_chars``, so what a partner may return to us is capped
    # the same way as what a partner may send us.
    a2a_client_response_max_bytes: int = 1_048_576  # 1 MiB
    # Consecutive TRANSIENT failures that open the circuit, and how long it
    # stays open before the first retry. Durable failures never count toward
    # this — they mark the registration unusable directly (FR-030, FR-053).
    a2a_client_breaker_threshold: int = 5
    a2a_client_breaker_cooldown_seconds: int = 300
    # How long an attempt waits before handing a live task back, WHEN a
    # callback will report the outcome. Without one the ceiling above is what
    # ends the attempt. It exists because a partner answers a submission
    # before the work is done — this platform's own inbound surface detaches
    # the run and replies ``working`` — so handing back the instant a task
    # reports non-terminal would cost the calling agent two turns for work
    # that finished in three seconds. Necessarily shorter than the ceiling.
    a2a_client_grace_seconds: int = 15
    # The AGE at which the floor will ask a partner directly about an
    # outstanding delegation — not how often it looks, which is
    # ``a2a_client_sweep_interval_seconds``, the loop that runs it. The floor
    # beneath every other mechanism, and not only beneath callbacks: a partner
    # bounds its delivery attempts and then gives up — this platform's own
    # sender stops after five — and a calling agent may never collect at all,
    # so without it either one waits forever (FR-018).
    #
    # Comfortably longer than the ceiling, because this is patience for
    # someone ELSE to report: a registered callback should get its chance
    # before the platform starts asking instead, and a calling agent handed a
    # task id should get room to collect it inside its own reasoning rather
    # than have the answer arrive out of band. Equal to the ceiling would mean
    # chasing the instant an attempt gives up, which pre-empts both.
    a2a_client_floor_interval_seconds: int = 900
    # Whether a partner can reach this deployment to call back. ``None`` means
    # decide from the configured public address, treating anything the
    # destination guard would refuse — loopback, private, link-local,
    # cloud-metadata — as unreachable. Set it explicitly to refuse: registering
    # a callback tells a partner where this deployment lives, which in an
    # outbound-only relationship it would otherwise never learn.
    a2a_client_callbacks_reachable: bool | None = None
    # Where a partner should call back, when that is not ``base_url`` — a
    # deployment a partner reaches at a different address than a browser does.
    # A BASE: the delegation id is appended, so give an origin and no path.
    # Naming one implies reachable; the refusal above still wins.
    a2a_client_callback_base_url: str = ""
    # Origins this deployment may reach over plain HTTP, and at addresses the
    # destination guard otherwise refuses. Empty by default; exists to stand
    # two deployments up on one machine and exercise the outbound half end to
    # end, which loopback addresses and http URLs otherwise make impossible.
    #
    # An ALLOW-LIST rather than a boolean, deliberately: "disable the guard"
    # would be one environment variable away from following an operator-
    # supplied card URL to a private address with stored credentials attached.
    # Everything not named stays refused, and a listed origin still pins.
    #
    # Comma-separated, scheme and host required, port optional:
    #   A2A_CLIENT_DEV_TRUSTED_ORIGINS=http://localhost:8200,http://localhost:8100
    # Also in Settings > Access and security (validated the same way at save): the
    # variable, when set, wins and locks the field.
    a2a_client_dev_trusted_origins: Annotated[list[str], NoDecode] = []

    # Opt-in strict checks on OAuth token URLs. Every token request
    # (code exchange, scheduled and inline refresh, for integrations and MCP
    # servers) and every save of a token URL is checked. By default only a
    # cloud-metadata address, or a URL no request could be sent to, is
    # refused, so every token URL that worked before the check existed keeps
    # working. Strict mode also refuses loopback, link-local, unspecified,
    # reserved and multicast addresses and any scheme but http(s); private
    # networks stay allowed (on-prem identity providers). Default False for
    # backward compatibility, like ``webhook_require_secret``. Also in
    # Settings > Access and security: the variable, when set, wins and locks the field.
    oauth_token_url_strict: bool = False

    # Under ``oauth_token_url_strict``, origins whose OAuth token URL may
    # resolve to a LOOPBACK address, for local development against an identity
    # provider or MCP server running on the same machine. Link-local and
    # metadata addresses stay refused even for a listed origin. No effect while
    # strict mode is off, which accepts loopback token URLs anyway.
    #
    # An ALLOW-LIST of origins, like A2A_CLIENT_DEV_TRUSTED_ORIGINS above: under
    # strict mode every loopback token URL it does not name stays refused.
    #
    # Comma-separated, scheme and host required, port optional:
    #   OAUTH_TOKEN_DEV_TRUSTED_ORIGINS=http://localhost:8080,http://127.0.0.1:9000
    # Also in Settings > Access and security (validated the same way at save): the
    # variable, when set, wins and locks the field.
    oauth_token_dev_trusted_origins: Annotated[list[str], NoDecode] = []

    # Which addresses a remote MCP server (sse / streamable_http) may be
    # reached at, checked when its URL is saved and on the address every
    # connection actually opens to, redirects included
    # (``app.tools.mcp_destinations``). Cloud metadata addresses are refused
    # in every mode. "permissive" (default) refuses only them, so every MCP
    # server that worked before keeps working; "private_networks" also refuses
    # loopback, link-local, unspecified, reserved and multicast addresses
    # (an on-prem server on a private network keeps working);
    # "public_only" refuses every address that is not globally routable - an
    # on-prem server is then reached through a relay group. Also in
    # Settings > Access and security: the variable, when set, wins and locks the field.
    mcp_server_url_policy: str = "permissive"  # "permissive" | "private_networks" | "public_only"

    # Origins an MCP server may be reached at whatever address they resolve
    # to, under the two strict URL policies - an internal MCP server the
    # operator vouches for. Cloud metadata addresses stay refused for them.
    # No effect under "permissive", which accepts those addresses anyway.
    #
    # Comma-separated, scheme and host required, port optional:
    #   MCP_SERVER_TRUSTED_ORIGINS=http://mcp.internal:8080,https://10.0.0.5
    # Also in Settings > Access and security (validated the same way at save): the
    # variable, when set, wins and locks the field.
    mcp_server_trusted_origins: Annotated[list[str], NoDecode] = []

    # Treat real loopback as confirmed encrypted
    # (``core/utils.py::is_secure_transport``). A deployment federated with
    # itself reaches its own card over plain http from 127.0.0.1 and sends no
    # forwarded headers for a proxy setting to read, so this is what lets the
    # A2A suite run without terminating TLS.
    #
    # Separate from ``debug`` because that also turns on SQL echo, which puts
    # bearer tokens and encrypted credential columns into stdout. Never set it
    # behind a same-host reverse proxy: with ``trust_proxy_headers`` off every
    # partner request arrives from 127.0.0.1, and the guarantee would be void
    # for all of them. Only the real socket peer counts: a request carrying
    # ``X-Forwarded-For`` is never exempt, since uvicorn may have rewritten its
    # peer from that header. Where transport is enforced (the A2A
    # surface, and runner enrollment and the MCP OAuth registration and token
    # endpoints under ``credential_transport_strict``), this exempts loopback
    # from the refusal.
    # Also in Settings > Access and security: the variable, when set, wins and locks the
    # field. ``test_only_insecure_transport_peers`` below needs the VARIABLE:
    # it is checked here, at startup, before any stored value is read.
    a2a_allow_insecure_loopback: bool = False

    # Opt-in strict transport for the three routes that issue credentials to
    # clients nothing holds to HTTPS: runner enrollment
    # (``POST /runners/enroll``, the ``run-`` credential), MCP OAuth client
    # registration (``POST /mcp/oauth/register``, a confidential client's
    # ``client_secret``) and the MCP OAuth token endpoint
    # (``POST /mcp/oauth/token``). Off by default: they serve a request whose
    # transport the platform cannot confirm encrypted and only log it once per
    # route, so no deployment changes on upgrade. On: they refuse it like the
    # A2A surface (403 ``https required`` on enrollment, the RFC 7591
    # ``invalid_client_metadata`` error on registration, the RFC 6749
    # ``invalid_request`` error on the token endpoint), with its loopback and
    # test-peer exemptions. Behind a
    # TLS-terminating proxy, name it in FORWARDED_ALLOW_IPS before turning
    # this on. Default False for backward compatibility, like
    # ``oauth_token_url_strict``. Also in Settings > Access and security: the variable,
    # when set, wins and locks the field.
    credential_transport_strict: bool = False

    # TEST ONLY. Peer addresses (IPs or CIDRs, comma-separated) whose plain
    # http requests count as encrypted on the A2A surface (and, under
    # ``credential_transport_strict``, runner enrollment and the MCP OAuth
    # registration and token endpoints), for an end-to-end
    # suite that reaches a containerised platform from the Docker bridge
    # gateway rather than from loopback. Empty by default and never meant for a
    # deployment: every partner request from a listed network skips the HTTPS
    # guarantee. So it is REFUSED at startup, not merely warned about, unless
    # ``a2a_allow_insecure_loopback`` (the end-to-end stack's own switch, whose
    # exemption this extends) is on too, and for any network that is not
    # private - an operator whose partners are refused behind a proxy must not
    # be able to exempt the proxy's network instead of trusting the proxy
    # (``FORWARDED_ALLOW_IPS``). Like the loopback exemption it applies to the
    # real socket peer only - a request carrying ``X-Forwarded-For`` is never
    # exempt - and it is logged at boot.
    test_only_insecure_transport_peers: Annotated[list[str], NoDecode] = []

    # --- MCP OAuth Dynamic Client Registration (RFC 7591) ---
    # The MCP specification requires the registration endpoint
    # (``POST /mcp/oauth/register``) to be reachable without authentication
    # (clients self-register before any user session exists), so abuse
    # resistance comes from throttling + a bounded client table instead of
    # a login gate. Operators who do not use MCP OAuth at all can disable
    # registration entirely (the endpoint then returns 403). Also in
    # Settings > Access and security, like ``mcp_outbound_dcr_enabled`` below: the
    # variable, when set, wins and locks the field.
    mcp_dcr_enabled: bool = True
    # Upper bound on the number of dynamically registered OAuth clients.
    # When the cap is reached, the oldest registrations are evicted to make
    # room (RFC 7591 clients re-register transparently when their
    # ``client_id`` is no longer recognised), so a registration flood churns
    # stale rows instead of growing ``mcp_oauth_clients`` without limit.
    mcp_dcr_max_clients: int = 500
    # Registrations per hour from one client IP. A hosted client
    # registers from its provider's egress addresses, one client per fresh
    # connection, so a platform many people connect to from one such client
    # may need more. Values below 1 are clamped to 1.
    mcp_dcr_rate_limit: int = 5
    # OAuth Client ID Metadata Documents (MCP 2025-11-25): a client
    # names its own HTTPS metadata document as its ``client_id`` instead of
    # registering - what Claude and VS Code use when it is advertised. The
    # platform fetches the document (public addresses only, bounded, cached).
    # Also in Settings > Access and security: the variable, when set, wins and locks the
    # field.
    mcp_cimd_enabled: bool = True

    # Token-endpoint requests per hour, bucketed per client IP. A ceiling on
    # bcrypt work from one source, not a tight quota — and the bucket key
    # collapses to the reverse proxy's address unless ``FORWARDED_ALLOW_IPS``
    # names that proxy (or ``trust_proxy_headers`` keys on the
    # leftmost ``X-Forwarded-For`` hop), so ALL clients behind one proxy share
    # it. Sized so
    # that a large org's hourly MCP token refreshes (one per active session,
    # access tokens expire in 60 minutes) fit behind a single egress IP.
    mcp_oauth_token_rate_limit: int = 1200

    # --- Outbound MCP OAuth (this platform acting as the OAuth *client*) ---
    # When a remote MCP server is configured with ``auth_mode = "oauth"`` but no
    # ``client_id``, the platform discovers the authorization server (RFC 9728 +
    # RFC 8414) and registers itself dynamically (RFC 7591) so an operator never
    # has to pre-provision a client_id / client_secret pair — the MCP spec's
    # intended onboarding path. Operators who require every OAuth client to be
    # provisioned by hand can turn the automatic registration off; connecting a
    # server then requires an explicitly configured ``client_id``.
    mcp_outbound_dcr_enabled: bool = True

    # --- Invitations ---
    # Default lifetime of an admin-generated invitation link (7 days).
    # Clamped in ``_apply_mode_defaults`` so a misconfigured env var
    # fails closed at startup instead of producing a Redis 500 or a
    # downstream 400 the first time an admin tries to issue an invite.
    # Both lifetimes are also in Settings > Access and security (same 1-720 range): the
    # variable, when set, wins and locks the field.
    invitation_expire_hours: int = 168
    # Lifetime of a password reset link (24 hours). Same clamp applies.
    password_reset_expire_hours: int = 24

    # --- Personal API keys ---
    # The longest a personal API key created or re-dated from then on may live,
    # in days; 0 = no maximum. Existing keys keep their expiry (the Security
    # Audit lists those beyond it). Clamped to [0, 3650]; also in Settings >
    # Security, where the variable, when set, wins and locks the field.
    api_key_max_lifetime_days: int = 0

    # --- Email (SMTP) ---
    # When smtp_hostname is empty, transactional emails (e.g. password reset)
    # are not sent — the link is logged instead (useful in dev).
    smtp_hostname: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from: str = ""  # falls back to smtp_username when empty
    smtp_from_name: str = "Filigran Copilot"
    # Two distinct transport modes — don't conflate them:
    #   smtp_use_tls  → implicit TLS/SSL, encrypted from the first byte (port 465).
    #   smtp_starttls → connect in plaintext, then upgrade via STARTTLS (port 587).
    # If both are set, implicit TLS (smtp_use_tls) wins. Default suits a
    # 587/STARTTLS server; set SMTP_USE_TLS=true for a 465 server.
    smtp_use_tls: bool = False
    smtp_starttls: bool = True

    # --- Model catalog announcement emails ---
    # Platform-wide kill switch: no model announcement email is ever sent, the
    # ones recorded at deploy (``MODEL_CATALOG_EMAILS_ENABLED``) and the ones an
    # admin sends from Settings alike. A boot with it on closes the queued
    # ones unsent, a process with it on delivers nothing, and Settings no
    # longer offers to email the users. Changes are still recorded.
    model_announcement_emails_disabled: bool = False
    # When the env-managed model catalog changes at boot (``DEFAULT_MODEL`` or
    # an ``LLM_PROVIDER_<P>_MODELS`` list), email the platform's users that a
    # model is now their default or newly available. Opt-in: a change recorded
    # while this is off (or without SMTP) is marked skipped, never sent later.
    model_catalog_emails_enabled: bool = False
    # ``all`` = every active user, ``admins`` = active admins only. The
    # bootstrap ``ADMIN_EMAIL`` account is never a recipient.
    model_catalog_emails_recipients: str = "all"
    # Emails sent per scheduler tick (one tick a minute): bounded because SMTP
    # accounts are capped per hour. The rest goes out on the following ticks.
    model_catalog_emails_batch_size: int = 20
    # Minutes the env catalog must stay unchanged before its change is emailed.
    # Every env change recorded meanwhile (a corrected rollout, a second
    # deploy) joins the same email and restarts the wait. 0 = from the next
    # tick. Admin announcements are never held.
    model_catalog_emails_settle_minutes: int = 30
    # How far back the model changes an admin saves in Settings > AI and models > Models are
    # consolidated into the one change they are offered to email.
    model_catalog_changes_window_hours: int = 24

    # --- S3 / MinIO file storage ---
    s3_endpoint: str = "localhost:9000"
    s3_access_key: str = "copilot"
    s3_secret_key: str = ""
    s3_bucket: str = "copilot-files"
    s3_use_ssl: bool = False
    s3_reject_unauthorized: bool = True
    s3_region: str = ""  # Required for AWS S3, ignored by MinIO
    # AWS credentials from the pod/instance role (IRSA, EKS Pod Identity, ECS, EC2)
    # instead of S3_ACCESS_KEY / S3_SECRET_KEY.
    s3_use_aws_role: bool = False
    s3_presigned_expiry: int = 3600  # Pre-signed URL expiry in seconds (1 hour)
    # Per-operation timeouts (seconds) for the S3/MinIO HTTP client. The MinIO
    # SDK is synchronous and is driven via ``asyncio.to_thread``; without a
    # timeout a slow / over-subscribed / blackholed object store blocks a
    # default-executor thread INDEFINITELY. Because asyncpg resolves DNS on that
    # SAME executor, enough stuck S3 threads starve DB connection establishment
    # and wedge the worker. Bounded connect + read timeouts
    # guarantee those threads are always returned to the pool. ``read`` is the
    # between-bytes timeout (not a total transfer cap), so steady large
    # uploads/downloads are unaffected; only a stalled peer trips it.
    s3_connect_timeout: int = 10
    s3_read_timeout: int = 60

    # --- Pre-provisioned LLM defaults (optional) ---
    # Single platform-wide default model (not per-provider).
    # When set, this model becomes the platform default for all agents
    # and the General Assistant.
    default_model: str = ""
    # When true, LLM providers are managed only via environment variables.
    # Admin UI/API provider management — add, edit, rename, delete — is
    # blocked: POST/PUT/DELETE on ``/api/v1/settings/llm-providers`` return
    # 403 (see ``ensure_llm_provider_management_allowed``). Operators must
    # configure providers through ``LLM_PROVIDER_*`` env vars and restart
    # the backend to apply changes.
    models_env_providing_only: bool = False
    # When true, the model catalog of an env-seeded LLM provider
    # (``LLM_PROVIDER_*_API_KEY``) is admin-curated instead of env-pinned:
    # ``LLM_PROVIDER_{PROVIDER}_MODELS`` becomes a *baseline* that is seeded
    # on first boot and then delta-merged on every restart (models added to
    # the env var are added, models removed from it are removed, every
    # other admin edit is preserved — see ``app.llm.env_models``), and the
    # Settings -> AI Models "Models" editor is unlocked for those providers.
    # The API key itself stays env-managed (env wins on every boot) and the
    # provider still cannot be deleted from the UI (the seed would recreate
    # it). Has no effect under ``MODELS_ENV_PROVIDING_ONLY=true``, which
    # blocks every provider mutation regardless.
    llm_provider_models_editable: bool = False
    # BYOLLM — "bring your own LLM": the LLM configuration of the install
    # belongs to the tenant administrator, not to the environment. The
    # operator may still ship a baseline; the admin owns it from then on.
    # When true:
    #   * every ``DEFAULT_*`` platform value (agent default, General
    #     Assistant chat model, routing, fallback, reasoning effort,
    #     embedding provider/model/dimensions, image provider/model)
    #     furnishes an install that has no LLM configuration of its own yet,
    #     and nothing after that (see ``main._seed_llm_providers_from_env``).
    #     The unit is the install, not the field: once anything is stored,
    #     every one of these variables is inert, so an empty field stays
    #     empty — a cleared fallback, a General Assistant on "Agent default"
    #     — and no restart takes a choice back;
    #   * nothing is reported as env-managed, so every Settings → AI and models → Models
    #     control stays editable and ``update_general_assistant_config``
    #     accepts it (``_env_managed_ga_fields`` returns an empty set);
    #   * admins may add their own providers and curate the catalog of
    #     env-seeded ones: it implies ``LLM_PROVIDER_MODELS_EDITABLE=true``
    #     and overrides ``MODELS_ENV_PROVIDING_ONLY=true`` (combining the two
    #     is contradictory — a WARNING naming both is logged at startup).
    # Provider *credentials* stay env-managed either way: an
    # ``LLM_PROVIDER_{P}_API_KEY`` is still re-applied on every boot, and
    # removing it still removes the provider. BYOLLM is about who chooses the
    # models and the defaults, not about who holds an operator's key.
    # Read the two resolved predicates below, never these raw flags.
    byollm_activated: bool = False
    # Optional override for the routing evaluator model used to decide
    # mandatory-transfer handovers. When empty, routing falls back to the
    # platform default model resolution.
    default_routing_model: str = ""
    # Optional platform-wide fallback model. When the primary model fails
    # (rate limit / overloaded / transient provider errors) after retries are
    # exhausted, the failover layer tries this explicit model first (on its
    # owning provider) before falling back to other providers' default models.
    # When set, it is re-seeded into PlatformSettings on every startup (env
    # always wins) and the Settings -> AI Models "Fallback Model" control
    # becomes read-only (env-managed), the same way DEFAULT_MODEL,
    # DEFAULT_ROUTING_MODEL, and the embedding/image defaults are locked.
    # The model must exist in some configured provider's available_models.
    default_fallback_model: str = ""
    # Platform-wide default reasoning effort for thinking/reasoning models
    # (OpenAI o-series, Anthropic Opus, Gemini 2.5). When set, this is seeded
    # into PlatformSettings on every startup and the Settings → AI and models → Models
    # "Default Reasoning Effort" control becomes read-only (env-managed), the
    # same way the env-seeded default model, routing, embedding, and image
    # fields are locked. Valid values:
    # "low" | "medium" | "high" | "max" (empty = no platform override; agents
    # fall back to the provider default).
    default_reasoning_effort: str = ""
    # --- Agent runs: wait policy ---
    # Platform bounds of the ``wait`` tool for every agent run, in seconds:
    # the longest single pause and the total one run may spend waiting. When
    # set they win over Settings > Agents and automations > Agent runs (the controls turn
    # read-only), like the env-seeded default model. Unset = the stored value
    # (default 300 s / 1800 s). Clamped to 3600 s and 86400 s.
    agent_maximum_wait_time: int | None = None
    agent_maximum_total_wait_time: int | None = None
    # --- Anthropic prompt caching ---
    # Enables Anthropic prompt caching on the direct Anthropic provider:
    # explicit cache breakpoints on the tool definitions and the system
    # prompt, plus automatic (top-level) caching for the growing
    # conversation.  Repeated prefixes across agentic-loop iterations and
    # chat turns are then billed at the cache-read rate (0.1x input) instead
    # of the full input rate, and cache reads are not deducted from
    # Anthropic rate limits.  Requests below the per-model minimum cacheable
    # length are silently processed uncached by the API, so small utility
    # calls are unaffected.  Set to false to restore fully uncached requests.
    prompt_caching_enabled: bool = True
    # Cache TTL: "5m" (default, refreshed free on every hit) or "1h" (2x
    # write cost — use for slow HITL / channel conversations where follow-up
    # requests routinely arrive more than 5 minutes apart).
    prompt_cache_ttl: str = "5m"
    # --- Anthropic thinking replay ---
    # Hands the thinking blocks of the current tool round back to the models
    # that think by default (Claude Sonnet 5, Opus 5, Fable 5, Mythos), so a
    # tool round resumes with the reasoning that asked for the tools. A turn's
    # blocks are re-sent verbatim only while the request prefix they were
    # produced under (system prompt, tools, earlier messages) is unchanged,
    # and a request the API refuses over them is sent again without them.
    # Off by default: every request strips them, as before; the LLM trace
    # metadata (``thinking_replay``) records what was kept and why not.
    anthropic_thinking_replay_enabled: bool = False
    # Override the auto-detected embedding and image models.
    # When set, these take priority over the per-provider defaults in
    # EMBEDDING_DEFAULTS / IMAGE_DEFAULTS (router.py) and are re-applied to
    # PlatformSettings on every startup (env always wins), so the Settings →
    # AI Models "Embedding"/"Image Generation" controls become read-only
    # (env-managed) — see ``_env_managed_ga_fields`` in api/v1/settings.py.
    default_embedding_provider: str = ""
    default_embedding_model: str = ""
    # Base URL for embedding providers that require a compound credential
    # (custom_openai, litellm, tei). Composed automatically with the provider's
    # API key when DEFAULT_EMBEDDING_PROVIDER is one of those providers.
    default_embedding_provider_base_url: str = ""
    # Override the vector dimensions used for embeddings (e.g. 1536, 3072).
    # Only set this when using a model that supports Matryoshka/variable dims
    # and you want a smaller index than the model's native maximum.
    default_embedding_dimensions: int | None = None
    default_image_provider: str = ""
    # Base URL for image providers that require a compound credential
    # (custom_openai, litellm). Composed automatically with the provider's
    # API key when DEFAULT_IMAGE_PROVIDER is one of those providers.
    default_image_provider_base_url: str = ""
    default_image_model: str = ""
    # The video, speech, music and decision models, pinned like the image
    # model: ``DEFAULT_<ROLE>_PROVIDER`` is a provider type, ``*_MODEL`` the
    # model it serves (video, speech and music take the provider's built-in
    # model when empty). Re-applied on every boot (env wins, the Settings
    # section read-only), once under BYOLLM_ACTIVATED. The rules live in
    # ``app/llm/env_capability_defaults.py``.
    default_video_provider: str = ""
    default_video_model: str = ""
    default_speech_provider: str = ""
    default_speech_model: str = ""
    default_speech_voice: str = ""
    default_music_provider: str = ""
    default_music_model: str = ""
    default_decision_provider: str = ""
    default_decision_model: str = ""
    # Optional platform-wide default chat model for the General Assistant
    # specifically (distinct from DEFAULT_MODEL, which is the agent default).
    # When set, it is re-seeded into ``GeneralAssistantConfig.llm_model`` on
    # every startup (env always wins) and the Settings → AI and models → General Assistant
    # "Chat Model" control becomes read-only (env-managed), the same way
    # DEFAULT_MODEL / DEFAULT_ROUTING_MODEL / the embedding & image defaults
    # are locked. When NOT set, the GA chat model defaults to "Agent default"
    # (it follows DEFAULT_MODEL / ``PlatformSettings.agent_default_model``) and
    # remains admin-configurable. The model must be chat-capable and resolve to
    # a configured provider (listed in some provider's ``available_models`` or
    # matching a provider by name prefix).
    default_general_assistant_model: str = ""

    # --- Langfuse observability (optional) ---
    # When both keys are set, Langfuse config is seeded/overwritten on every
    # startup (enabled automatically).  Env vars always take precedence over
    # the database configuration.  Host defaults to Langfuse Cloud when not set.
    langfuse_host: str = ""
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    # Controls whether financial cost data is exposed in observability UI.
    # Set to false in deployment modes where cost amounts should be masked.
    observability_cost_data_enabled: bool = True

    # --- Prometheus Metrics ---
    # Exposes /metrics endpoint for Prometheus scraping.
    prometheus_enabled: bool = False
    prometheus_bearer_token: str = ""

    # --- Usage telemetry (anonymous adoption metrics) ---
    # XTM One exports anonymous platform usage metrics to Filigran's telemetry
    # collector (OTLP/HTTP) to measure product adoption and prioritise the
    # roadmap, as OpenCTI and OpenAEV do. Only aggregate counts, the platform
    # version and the deployment edition: no personal data, credentials,
    # message content or entity names. One process per install exports every
    # 6 hours (``services/telemetry.py``); every metric is listed in
    # ``docs/docs/deployment/telemetry.md``.
    # ``TELEMETRY_ENABLED=false`` turns it off: no connectivity probe, no
    # exporter, no export lease (OpenCTI's ``TELEMETRY_MANAGER__ENABLED``).
    # On by default; it also stays off when the collector is unreachable at
    # startup (air-gapped installs) and in CI.
    telemetry_enabled: bool = True
    # Override the telemetry collector endpoint (OTLP/HTTP ``/v1/metrics``).
    # When empty, the bundled Filigran endpoint is used (staging in dev mode).
    # Primarily useful for self-hosted collectors and integration testing.
    telemetry_endpoint: str = ""

    # --- Prompt / persona / agent-config / skill versioning ---
    # Retention applies uniformly to ALL versioned snapshot types:
    #   - ``prompt_versions`` (per prompt_id)
    #   - ``prompt_versions`` rows that snapshot agent personas
    #     (per agent_id, where ``prompt_id is NULL``)
    #   - ``agent_config_versions`` (per (agent_id, field_name) — covers
    #     temperature / max_iterations / reasoning_effort / etc.)
    #   - ``skill_versions`` (per skill_id)
    # All four pathways call ``_enforce_retention`` with this value, so a
    # single knob keeps history bounded across the whole versioning
    # surface.  Locked versions are NEVER pruned regardless of N.
    # 0 = unlimited history, N > 0 = keep latest N versions per series.
    prompt_version_retention_count: int = 0

    # --- Shared-state lifecycle ---
    # Shared state is a durable cross-run key -> value store agents read/write.
    # Agents own keeping it tidy (resolve when done, purge when useless), but
    # these knobs are the platform backstop so it never grows unbounded:
    #   - An ``active`` entry untouched for this many days is *derived* stale
    #     (never stored) and surfaced so its owner resolves or purges it. 0 (or a
    #     negative value) disables the stale signal entirely.
    shared_state_stale_after_days: int = 14
    #   - ``resolved`` entries are purged by the autonomous sweep once their
    #     last update is older than this many days. 0 (or a negative value)
    #     disables the retention purge (resolved entries are kept until an agent
    #     or admin deletes them). The default of Settings > Observability >
    #     Coordination retention: a value an administrator stores there wins
    #     (services/coordination_retention.py), as for the four knobs below.
    shared_state_resolved_retention_days: int = 90
    #   - Items an ``archive_key`` write moved into an archive entry are stamped
    #     and purged by the same sweep once older than this many days (an
    #     archive is an active entry, which the resolved purge never touches).
    #     0 (or a negative value) keeps archived items until an agent drops them.
    shared_state_archive_retention_days: int = 14
    #   - Size of a value, in characters of its JSON. Over the soft limit a
    #     write lands and its read-back warns, naming the largest keys; over the
    #     hard limit a write is refused unless it shrinks the value (an archive
    #     write drops its oldest items instead, and the tick compacts an archive
    #     already over it). 0 disables either limit.
    shared_state_value_soft_limit_chars: int = 32768
    shared_state_value_hard_limit_chars: int = 65536
    #   - Items of one top-level list: a write leaving a list longer than this
    #     and longer than before is refused (an archive drops its oldest items
    #     instead). 0 disables the cap.
    shared_state_list_max_items: int = 500

    # --- Work-item lifecycle ---
    # Work items are a durable, owned, claimable agent-to-agent task queue.
    # Agents pick them up via a ``work_item``-trigger inbox assignment (the
    # autonomous consumer), and these knobs keep the queue self-cleaning so a
    # human never has to babysit it:
    #   - A non-terminal item is *derived* "stale" (needs attention; never
    #     stored) ONLY when it is past an explicit ``due_at`` OR it was
    #     claimed/in_progress/blocked and has gone quiet for this many HOURS (a
    #     claim is a promise to act). An OPEN, unclaimed, no-due-date item is
    #     backlog and is never stale on inactivity alone. 0 (or negative)
    #     disables the inactivity branch (only past-due still flags).
    work_item_stale_after_hours: int = 24
    #   - A BLOCKED item waiting on pull requests, issues, deployments or
    #     workflow runs (``blocked_on_refs``) the platform watches is a live
    #     wait, not a dropped promise: it is stale only once quiet this many
    #     DAYS (the reference has gone nowhere). An item whose ``not_before``
    #     is ahead is gated on a date and never stale. 0 (or negative) treats
    #     such a wait like any blocked item (the hours above).
    work_item_stale_ref_wait_days: int = 7
    #   - Comma-separated ``owner/name`` repositories whose deployments the
    #     platform observes: a ``blocked_on_refs`` entry of kind
    #     ``deployment`` is released when a running build carries the merge,
    #     which only holds for the repository the platform's own builds come
    #     from. A deployment reference on any other repository is refused at
    #     write with the alternatives (a ``workflow_run`` reference on that
    #     repository's release workflow, or ``blocked_reason`` with a dated
    #     ``not_before``), never accepted as a wait that can never release.
    work_item_deployment_repositories: str = "XTM-One-Platform/xtm-one"
    #   - An OPEN item untouched for this many DAYS is auto-cancelled by the
    #     retention sweep (an audit note is written to ``result``), so orphaned
    #     follow-ups nobody ever picked up disappear on their own. 0 (or
    #     negative) disables auto-cancellation.
    work_item_abandoned_after_days: int = 14
    #   - Terminal (done/cancelled) items are purged by the sweep once their
    #     last update is older than this many days. 0 (or negative) disables the
    #     retention purge.
    work_item_resolved_retention_days: int = 90

    # --- Assignment run retention (Operations > Executions) ---
    #   - A finished run (completed, failed, skipped, cancelled) is deleted by
    #     the hourly retention pass once its last activity (``completed_at``,
    #     else ``created_at``) is older than this many days; a run still
    #     working, waiting for a person, holding a claim or a lease, or the
    #     parent of a kept run never is. A window saved in Settings >
    #     Observability wins. 0 (or negative) keeps every run.
    assignment_run_retention_days: int = 90

    # --- Workset lifecycle ---
    # A workset is a WORKSPACE, not a deliverable: a scoped corpus an agent
    # builds to answer one question, holding documents, chunks and vectors. It
    # is the heaviest of the three coordination primitives, so leaving one
    # behind costs storage rather than a row — which is why the sweep is the
    # backstop and not an optimisation.
    #   - A ``resolved`` workset is purged once its resolution is older than
    #     this many days (kept briefly so an operator can still inspect what a
    #     run collected). 0 (or negative) disables the purge.
    workset_resolved_retention_days: int = 7
    #   - A workset carrying an explicit ``expires_at`` (the builder node's
    #     "keep for" field) is purged once past it, whether or not anybody
    #     resolved it. That is the TTL a run-scoped corpus relies on: nothing
    #     resolves a workset when its run fails halfway.
    #   - An ACTIVE workset with no expiry is never purged by the sweep. It may
    #     be the corpus of a recurring job that runs fortnightly, and deleting
    #     it on inactivity would silently make the next run start cold.
    workset_sweep_batch: int = 200
    #   - An IMPLICIT workset (the corpus the runtime derives for an agent's
    #     chat or an assignment, never one a flow node names) gets an idle
    #     expiry this many days after its last use, renewed on every use, so
    #     the sweep purges a corpus its agent stopped using. 0 (or negative)
    #     keeps implicit worksets until resolved.
    workset_implicit_idle_days: int = 30
    #   - An implicit workset's automatic summarisation budget
    #     (``max_auto_calls``) is per window of this many hours rather than
    #     for its whole life, so a long-lived agent never ends up with a dead
    #     corpus. 0 (or negative) makes it a lifetime ceiling like the
    #     budget of an explicit workset.
    workset_implicit_budget_window_hours: int = 24
    # Flow event retention. ``flow_events`` is an append-only dispatch
    # record: listener runs are started synchronously at emit time, so an
    # emission has done its job the moment it is stored, and what is left is
    # the audit view plus what a parked ``Wait for event`` can still hear.
    # Memory writes made the table's biggest producer, so it needs a floor.
    #   - Emissions older than this many days are purged by the hourly sweep.
    #     0 (or negative) disables the purge entirely.
    #   - The sweep never uses a window shorter than ``flow_run_timeout_seconds``
    #     however low this is set: a run cannot outlive that timeout, so a
    #     window at least that long provably cannot strand a parked waiter.
    flow_event_retention_days: int = 30
    #   - Rows deleted per sweep pass, oldest first. Bounded like the other
    #     retention sweeps so one tick cannot hold a long transaction.
    flow_event_sweep_batch: int = 2000
    #   - Number of OPEN candidate items a single ``work_item`` inbox assignment
    #     scans per scheduler tick to find one it can claim. The inbox is serial
    #     (``skip_if_active`` + an active-run guard), so exactly ONE item is
    #     dispatched per assignment per tick; a larger pool only adds claim-race
    #     headroom when several consumers contend for the same queue.
    work_item_dispatch_per_tick: int = 5
    #   - Max times a failing work-item dispatch is retried before the item is
    #     parked in ``blocked`` (so a permanently-failing item can't re-dispatch
    #     forever). The attempt counter rides in the item's ``payload``.
    work_item_max_dispatch_attempts: int = 5
    #   - A decision recipient that has not signed in for this many days (or
    #     never, or the seed admin account) is named in a warning at the write
    #     and in the fleet health finding decision_recipient_inactive (#2390).
    work_item_recipient_inactive_days: int = 7
    #   - A decision gate nobody opened for this many hours notifies every
    #     active administrator once and raises work_item.needs_human_unseen.
    work_item_decision_unseen_hours: int = 24
    #   - Comma-separated targets a TEST work item (correlation key starting
    #     with ``retest:`` or ``test:``, title starting with ``Notify retest``)
    #     may notify: an address (``payload.notify.user``), a Teams chat
    #     id (``chat_id``) or ``team_id/channel_id``. Any other recipient
    #     is refused at save, so a platform retest never messages a real
    #     person. The account's own chat (``48:notes``) is always allowed.
    work_item_notify_test_recipients: str = ""
    # Scheduled work items: an item whose ``payload.action`` the
    # platform runs at ``due_at``, with no assignment and no flow
    # (``services/scheduled_work_items.py``).
    #   - The platform kill switch. Off = every agent's scheduling tools answer
    #     that scheduling is disabled by the administrator and the tick
    #     dispatches nothing. Also in Settings > Access and security: the variable, when
    #     set, wins and locks the field.
    scheduled_work_items_enabled: bool = True
    # Phone notifications (``services/mobile_push.py``): ``full`` shows the
    # notification's title and text on the phone (encrypted end to end to it,
    # the push service reads nothing); ``minimal`` sends only "New
    # notification", for a deployment that wants nothing on a lock screen.
    # Also in Settings > Access and security: the variable, when set, wins and locks it.
    phone_notification_content: str = "full"
    # Companion PIN (``services/app_lock.py``): true makes every person unlock
    # the mobile companion with a PIN before it shows or does anything (they
    # choose one the first time). False leaves it to each person (Profile >
    # Phones). Also in Settings > Access and security: the variable, when set, wins.
    companion_pin_required: bool = False
    #   - Minutes after ``due_at`` an item is still run late (the tick runs
    #     every 60 s, a worker may be busy). Past it the item is MISSED: blocked
    #     with ``outcome_code = missed`` and its creator notified, never run at
    #     a time nobody chose.
    scheduled_work_item_grace_minutes: int = 15
    #   - Attempts of one run before its item is blocked. Only a failure
    #     before the action was sent is retried; a refused check, an invalid
    #     payload and an error the tool returned never are.
    scheduled_work_item_max_attempts: int = 3
    #   - Seconds between two attempts of a transient failure.
    scheduled_work_item_retry_seconds: int = 60
    #   - Due items one tick dispatches (the rest wait for the next tick).
    scheduled_work_item_dispatch_per_tick: int = 50
    #   - How far ahead an item may be scheduled.
    scheduled_work_item_max_horizon_days: int = 366

    # --- Evaluation execution ---
    # Maximum number of evaluation runs (agent × model matrix cells) executed
    # concurrently for a single parallel-mode evaluation batch. Each run drives
    # a full agentic loop against the LLM provider, so firing the entire matrix
    # at once (the previous behaviour) overwhelms provider rate limits — the
    # 429/529 retries then back off *inside* each call (up to 5 retries,
    # 2s→60s each), inflating per-run durations into thousands of seconds. A
    # bounded pool keeps the provider busy without thrashing. Batches with
    # fewer runs than the cap are unaffected (they all run at once). Set to 0
    # (or a negative value) to restore the legacy unbounded behaviour.
    evaluation_max_concurrent_runs: int = 5

    # --- XTM One built-in integrations (XTM One / dev mode only) ---
    # Google Custom Search: "search_engine_id||api_key" compound token.
    # When empty (or whitespace-only), the built-in Google Search integration
    # is not seeded.
    builtin_google_search_api_key: str = ""
    # Brave Search API subscription token. When set, Brave is the built-in web
    # search of every agent and Google (above) is not seeded; Google stays the
    # fallback while only its key is set. Neither: no built-in web search.
    builtin_brave_search_api_key: str = ""
    # Optional built-in embedding endpoint seed (XTM One / dev mode only).
    # When URL + model are set, startup seeds/updates a company-managed
    # provider of type "tei" (generic /embed endpoint provider). If the
    # platform has no embedding provider configured yet, this seed is also
    # applied to PlatformSettings as the active embedding provider/model.
    builtin_embedding_server_base_url: str = ""
    builtin_embedding_server_token: str = ""
    builtin_embedding_server_model: str = ""
    builtin_embedding_server_dimensions: int | None = None

    # --- Microsoft Teams message listings ---
    # The longest text of one message the Teams channel and chat listings
    # show. A longer one is cut at a word boundary and ends with the exact
    # get_microsoft_channel_message / get_microsoft_chat_message call that
    # reads it in full. A listing's messages also share one text budget, so a
    # busy channel stays within one tool result.
    microsoft_teams_message_text_limit: int = 4000

    # --- CORS ---
    cors_origins: list[str] = [
        "http://localhost:3000",
        "http://localhost:5173",
        "https://localhost:3100",
        "http://localhost:3100",
    ]

    # --- Security feature flags ---
    # Set these in the environment / .env file to disable dangerous features platform-wide.
    # Defaults depend on platform_mode:
    #   copilot / dev → False  (enabled by default)
    #   xtm_one       → True   (disabled by default)
    # Set explicitly in env to override. These three, the widget and webhook
    # switches below, the runner confinement and the sandbox module / Node
    # permission settings are also in Settings > Access and security: the variable, when
    # set, wins and locks the field (``core/security_options.py``).
    disable_code_interpreter: bool | None = None
    disable_custom_tools: bool | None = None
    disable_custom_variables: bool | None = None
    # Opt-out for per-user sandbox usage quota counting (usage_type="sandbox"
    # rows in quota_usage). False by default — sandbox calls are metered like
    # llm_proxy/agentic usage out of the box. Set True for self-hosted/on-prem
    # deployments that manage their own sandbox infrastructure independently
    # of any hosted usage-based billing. Unlike disable_code_interpreter/
    # disable_custom_tools above, this does NOT vary by platform_mode — it is
    # a plain bool, not resolved later, since the underlying feature (usage
    # counting, not sandbox availability) has no XTM-One-vs-Copilot distinction.
    disable_sandbox_quota_usage: bool = False
    ai_catalog_enabled: bool = False
    # Platform Health spends outside the agentic quota (dev-docs/platform-health.md
    # sections 6 and 12.5), bounded only by its own spend ceilings (Budget,
    # Analysis, Module Analyst). False by default: those ceilings are locked at
    # their built-in defaults, read-only in Settings > Platform Health and a write
    # to them refused; a stored value is kept, not applied. True: the stored
    # values apply and the ceilings are editable again. Blank is unset.
    platform_health_spend_limits_unlocked: bool = False
    # When True, every agent tool of an admin (the agent builder, the flow
    # builder, the trace inspection and the work-items delegation, in chat and
    # on the XTM One MCP server) sees only the admin's three tiers, like every
    # other caller: agents, assignments, runs, traces, flows, evaluations,
    # knowledge bases and the other entities it looks up, lists or binds. The
    # admin role still counts in the permission gates. Default False: an
    # admin's agent reaches everything, as the REST routes do; the Security
    # Audit reports it while an admin's agent can. Also in Settings >
    # Security: the variable, when set, wins and locks the field.
    agent_tools_strict_admin_access: bool = False
    # The tools whose results the connected prompt guard (Nova) screens,
    # comma-separated registered tool names:
    #   PROMPT_GUARD_SCANNED_TOOLS=web_fetch,search_jira_issues
    # Empty by default: no result is screened and an agent run does exactly
    # what it did before the option existed. A match is recorded in the audit
    # log and the result is left as it is, unless
    # ``prompt_guard_block_tool_results`` withholds it. Both are also in
    # Settings > Access and security: the variable, when set, wins and locks the field.
    prompt_guard_scanned_tools: Annotated[list[str], NoDecode] = []
    prompt_guard_block_tool_results: bool = False
    # Public chat widget: when True, an answer shows only the images the
    # platform serves (any other image becomes a link, so the visitor's browser
    # never fetches a URL an answer chose), every link opens a new tab, and
    # answers drop inline styles, presentational markup, SVG / MathML and links
    # other than http(s) / mailto / tel. Default False keeps answers rendering
    # as they always did (script is removed in both); the Security Audit
    # reports it while a widget is enabled.
    public_widget_strict_rendering: bool = False
    # Public chat widget visitor uploads: the files a visitor attached
    # are deleted once their session has had no message for this many days
    # (and a file uploaded but never sent after a day). A value below 1 is
    # read as 1: the retention cannot be switched off.
    public_widget_upload_retention_days: int = 7
    # SVGs a model is shown (a chat upload, a user-message image, a tool
    # result): when True, an SVG that declares an XML entity or references
    # anything outside itself (a file path, a URL, another document) is not
    # rendered at all. Default False renders it with that reference dropped, as
    # resvg itself drew nothing there - either way nothing is fetched or read
    # from disk, and an internal entity is expanded only within a bound.
    svg_strict_rendering: bool = False

    # Prompt guards on the General Assistant and the public widget:
    # while a guard (Nova) is connected, the user's message is evaluated
    # before the model is called and a rule match is audited. Default False:
    # the message continues even when a `block` rule matched, since a surface
    # the guard never covered before only logs; True refuses it with the
    # guard's message. Agent chat is unaffected (its `block` rules refuse).
    # Also in Settings > Access and security: the variable, when set, wins and locks it.
    prompt_guard_block_general_assistant: bool = False
    prompt_guard_block_public_widget: bool = False
    # Prompt guards on the model's answer: "off" (default) evaluates
    # nothing; "log" evaluates every answer in the background once it is
    # delivered and audits a match; "block" evaluates an answer before it is
    # delivered and withholds one a `block` rule matches, where it is
    # delivered whole - a streamed answer has already left, so it is
    # evaluated and audited as in "log". Also in Settings > Access and security: the
    # variable, when set, wins and locks the field.
    prompt_guard_model_output: str = "off"  # "off" | "log" | "block"
    # When True, webhook deliveries targeting assignments WITHOUT a configured
    # ``webhook_secret`` are rejected with 401 (strict enforcement).  Default
    # False for backward compatibility: secretless assignments keep working
    # but each one logs a WARNING (once per assignment per process).  New
    # webhook assignments auto-generate a secret at creation, so this flag
    # mainly hardens deployments with legacy secretless assignments.
    webhook_require_secret: bool = False
    # When True, the Slack and Microsoft Graph notification endpoints refuse every
    # delivery they cannot authenticate. Default False keeps what an install relied on
    # before senders were checked: a Slack integration holding no usable
    # signing secret, and a Graph subscription recorded without a clientState, keep
    # receiving their events, with a WARNING and a Security Audit finding. A delivery
    # that a configured secret or clientState does not prove is refused either way.
    # True also limits the admin OAuth app's webhook and the LinkedIn notification
    # URL to the admin app's integrations the agent owner may use and
    # drops LinkedIn notifications when no client secret is configured. It also makes
    # POST /api/channels/teams/graph-notifications (the Teams bot's private and shared
    # channels) act only on a notification that proves the subscription recorded for
    # its channel, and the maintenance pass replace the subscriptions created with the
    # derivable clientState or whose clientState can no longer be decrypted.
    integration_notifications_require_verification: bool = False

    # --- XTM One Runners (native, pull-based OS-control daemons) ---
    # Master switch for the whole Runner subsystem: enrollment, the runner
    # transport (WebSocket + long-poll), and the ``computer_use`` agent tools.
    # Enabled by default everywhere (the Fidelity audit-evidence use case runs
    # in XTM One mode); set RUNNERS_ENABLED=false to remove it entirely.
    runners_enabled: bool = True
    # A runner reports liveness on connect + heartbeat. ``last_seen_at``
    # recency is the authoritative online/offline signal (multi-replica safe),
    # so a runner is "online" only if seen within this many seconds. Keep it a
    # small multiple of the heartbeat interval to absorb one missed beat.
    runner_offline_after_seconds: int = 75
    # Heartbeat / WebSocket keepalive cadence the runner is told to use.
    runner_heartbeat_interval_seconds: int = 30
    # Long-poll hold time (fallback transport): the poll endpoint blocks up to
    # this long waiting for a command before returning empty so the runner can
    # re-poll. Effectively real-time; the WebSocket fast-path is preferred.
    runner_long_poll_timeout_seconds: int = 25
    # How long an agent tool waits for a dispatched command's terminal result
    # before giving up (the runner may be mid-capture / mid-shell). Unset (the
    # default): as long as the machine may take to report one - from
    # the command's delivery, its execution limit (the default timeout below +
    # 30 s) plus the 50 s it retries a result upload through a platform restart
    # plus 5 s, 205 s by default, ending before the command's lease
    # (runner_transport.default_result_wait_seconds). A number: that many
    # seconds from dispatch (the default used to be a fixed 180). Never past
    # the time the agent run allows one tool call.
    runner_result_wait_timeout_seconds: int | None = None
    # Default per-command execution timeout handed to the runner.
    runner_command_default_timeout_seconds: int = 120
    # Hard ceiling on an uploaded screenshot / file-read result (bytes).
    runner_max_result_bytes: int = 25_000_000
    # A restricted runner's file command whose path leads outside its allowed
    # folders through a link (symlink, junction, mount point) runs, as it
    # always has, with a warning in its result. On: refused instead.
    # Also in Settings > Access and security: the variable, when set, wins and locks it.
    runner_strict_path_confinement: bool = False
    # Default TTL stamped on a single-use enrollment key at creation.
    runner_enrollment_ttl_seconds: int = 900
    # Minimum runner binary version the platform will accept (protocol gate).
    # Empty = no floor. A runner below this is refused at connect and told to
    # self-update. Format: MAJOR.YYMMDD.SEQ.
    runner_min_supported_version: str = ""
    # Directory holding the embedded runner distribution: installers, signed
    # self-update artifacts, bare-binary archives, install scripts and the
    # VERSION marker. Populated at image build time by the release workflow's
    # runner-build job, in XTM-One-Platform/release (there is no external
    # download URL — the platform itself is the only distribution channel).
    # Empty/absent on source builds, which reports downloads as unavailable
    # with a clear message. A relative path resolves against the backend
    # working directory (/app in the image).
    runner_dist_dir: str = "runner_dist"
    # Fleet bounds — keep a "thousands of clients" deployment bounded. Both
    # accept 0 = unlimited (the explicit escape hatch for a genuinely large
    # fleet).
    # Max ACTIVE runners a single user (or the company-managed fleet) may have
    # enrolled at once; enforced at enrollment so an unattended MDM roll-out
    # can't create an unbounded number of Runner rows against one identity.
    runner_max_per_user: int = 200
    # Max concurrent in-flight (non-terminal) commands per runner. A dispatch
    # beyond this is refused with a transient "busy" so a burst of parallel
    # tool calls in one agent turn can't swamp a single machine.
    runner_max_inflight_commands: int = 10

    # --- XTM One Relays (pull-based on-premise egress) ---
    # Master switch for relays: enrollment, the relay transport (WebSocket +
    # long-poll), the Relays page and routing integrations / MCP servers /
    # flow HTTP nodes through a relay group. On by default; false removes the
    # whole subsystem (every relay endpoint 404s, nothing is routed).
    relays_enabled: bool = True
    # A relay is online only if seen within this many seconds (heartbeat or
    # any frame). A routed request to a group with no online relay fails at
    # once instead of waiting in the queue.
    relay_offline_after_seconds: int = 60
    # Heartbeat cadence the relay is told to use.
    relay_heartbeat_interval_seconds: int = 20
    # Long-poll hold time (fallback transport).
    relay_long_poll_timeout_seconds: int = 25
    # Ceiling on a relayed request body and on a relayed response body
    # (bytes). A request over the first is refused before it is queued; a
    # response over the second is cut by the relay with ``too_large``.
    relay_max_request_bytes: int = 20_000_000
    relay_max_response_bytes: int = 100_000_000
    # How long a relayed stream may stay silent (no frame in either direction)
    # before it is abandoned: covers a relay or a platform replica that died
    # mid-stream.
    relay_stream_idle_timeout_seconds: int = 120
    # How long a request waits in its group's queue for a relay to pick it up
    # (every relay of the group busy) before it fails.
    relay_queue_timeout_seconds: int = 30
    # Default TTL stamped on a single-use enrollment key at creation.
    relay_enrollment_ttl_seconds: int = 3600
    # Minimum relay version the platform accepts. Empty = no floor. A relay
    # below it is refused at connect and told to self-update.
    relay_min_supported_version: str = ""
    # Directory holding the embedded relay distribution (archives, signed
    # self-update artifacts, install scripts, VERSION), populated at image
    # build time by the release workflow's relay-build job, in
    # XTM-One-Platform/release.
    relay_dist_dir: str = "relay_dist"
    # Days the metadata of relayed requests is kept (0 = forever).
    relay_request_log_retention_days: int = 30
    # Max relays enrolled in one group (0 = unlimited).
    relay_max_per_group: int = 50

    # --- Custom tool sandbox mode ---
    # Controls how user-defined custom tools (JavaScript / Python) are isolated:
    #   "auto"       (default) probe Docker availability (cached: positive
    #                results for the process lifetime, negative results
    #                re-probed after a short interval) and prefer the
    #                hardened Docker sandbox (--network=none, --read-only,
    #                --cap-drop=ALL, memory cap); fall back to a plain
    #                subprocess with a one-time WARNING when Docker is
    #                unavailable.
    #   "docker"     require the Docker sandbox; tool execution fails with a
    #                clear error when Docker is unavailable (no silent
    #                fallback).
    #   "subprocess" always run in a plain subprocess — no hardened Docker
    #                container isolation (JS runs under the Node.js
    #                permission model when supported, see
    #                custom_tool_node_permission below; Python runs in
    #                isolated mode with a best-effort module-blocklist
    #                sandbox). A one-time WARNING is still logged.
    #   "opensandbox" run inside an OpenSandbox-managed container via the
    #                opensandbox Python SDK. Requires OPENSANDBOX_DOMAIN,
    #                OPENSANDBOX_PROTOCOL, and OPENSANDBOX_API_KEY to be set.
    #                The container image is controlled by OPENSANDBOX_IMAGES.
    # Backward compatibility: the legacy CUSTOM_TOOL_DOCKER=true env var is
    # honoured as "docker" only when this setting is not provided at all; an
    # explicitly configured value — including an explicit "auto" — always
    # wins. A deprecation warning is logged once per process (see
    # services/custom_tool_executor.py).
    custom_tool_sandbox: str = "auto"

    # --- Custom tool subprocess hardening: Node.js permission model ---
    # When the subprocess fallback is used (no Docker), JavaScript custom
    # tools are executed under the Node.js permission model
    # (``node --permission``) when the installed Node supports it (stable
    # since Node 24): file system access, child processes, worker threads
    # and native addons are DENIED; network stays available (``--allow-net``
    # is added automatically on Node 25+ where the permission model would
    # otherwise deny it). Tool scripts are piped via stdin and receive their
    # inputs/files inline, so well-behaved tools are unaffected.
    # Set to false only if legacy custom tools genuinely need direct
    # file-system or child-process access in the subprocess fallback.
    # Also in Settings > Access and security: the variable, when set, wins and locks it.
    custom_tool_node_permission: bool = True

    # --- OpenSandbox connection settings (required when custom_tool_sandbox="opensandbox") ---
    opensandbox_domain: str = "localhost:8090"
    opensandbox_protocol: str = "http"
    opensandbox_api_key: str = ""
    # Per-key container image: OPENSANDBOX_IMAGES="python:my-image:1.0,nodejs:my-js-image:1.0".
    # Overrides merge on top of _OPENSANDBOX_DEFAULT_IMAGES (see above), so
    # overriding one key leaves the other on its default. Parsed by
    # _parse_opensandbox_images below using the same "key:value" CSV
    # convention as opensandbox_k8s_pool_names — split on the first ":" only,
    # so a value containing further colons (e.g. a registry:port prefix)
    # survives intact.
    opensandbox_images: Annotated[dict[str, str], NoDecode] = dict(_OPENSANDBOX_DEFAULT_IMAGES)
    # k8s-only: per-key Pool binding (pre-warming): OPENSANDBOX_K8S_POOL_NAMES=
    # "python:my-python-pool,nodejs:my-js-pool". A Pool's warm pods run one
    # fixed image, so one name can't back both sandbox kinds — keys in use
    # today are "python" and "nodejs". Parsed by
    # _parse_opensandbox_k8s_pool_names below; entries missing a ":"
    # (including a bare single-pool-name value) are dropped and that key
    # cold-starts, same as leaving this unset. Empty by default — this is a
    # k8s-specific opt-in, not something every deployment needs.
    opensandbox_k8s_pool_names: Annotated[dict[str, str], NoDecode] = {}
    # k8s-only: the namespace the OpenSandbox server creates its BatchSandboxes
    # and Pool pods in. Set, the worker's pool reaper
    # (services/sandbox_pool_reaper.py) deletes there, every minute and at once
    # after a pooled create timed out, the Pool pods that ended (Failed,
    # Succeeded, Evicted) or sat not Running past the age below, and the
    # BatchSandboxes past their expireTime: a Pool counts such pods toward its
    # buffer and never replaces them. The platform's service account needs
    # pods get/list/delete and sandbox.opensandbox.io batchsandboxes
    # get/list/delete and pools get in it (pools: the starved-Pool read that
    # sends a create direct, _sandbox_selector). Empty = the reaper is off.
    opensandbox_k8s_namespace: str = ""
    # A Pool pod still Pending (or Unknown) this long after its creation is
    # deleted by the reaper so the Pool creates a fresh one (seconds; keep it
    # above the server's sandbox_create_timeout_seconds).
    opensandbox_pool_stuck_seconds: int = 600
    # Route all sandbox traffic through the OpenSandbox server instead of the
    # SDK connecting directly to each sandbox's published endpoint. Useful for
    # any deployment topology where this process can reach the OpenSandbox
    # server but not the individual sandbox endpoints it hands back — e.g. a
    # process on the host against a containerized server, which advertises
    # endpoints via [docker].host_ip (host.docker.internal), a name only
    # resolvable from inside containers; or a production/k8s deployment where
    # sandbox pods/containers aren't directly routable from this process but
    # the server's service is.
    opensandbox_use_server_proxy: bool = False
    # Comma-separated list of domains or IPs to allow as egress from every
    # OpenSandbox container (formatted as ``NetworkRule.target`` values, e.g.
    # ``"api.example.com,10.0.0.1,*.internal.corp"``).  Empty by default —
    # all egress is denied unless explicitly listed here.  Applied to both
    # custom-tool sandboxes (JS + Python) and the code-interpreter sandbox.
    opensandbox_network_allow: str = ""
    # Outbound internet for the OpenSandbox code interpreter (one-shot runs and
    # the session kernel) and nothing else: on, its container is created under
    # a default-allow egress policy whose deny rules keep out the cloud metadata
    # endpoints, the cluster's internal names and the private, loopback,
    # link-local and CGNAT ranges (``_sandbox_selector.OPEN_EGRESS_DENY``), and
    # its import hook gives back the HTTP stack (socket, http, urllib, requests,
    # httpx, aiohttp); the escape primitives stay blocked. The local code
    # interpreter shares the backend's network and is never opened. The
    # deployment's network policy of the code interpreter's Pool stays the
    # packet-level floor. Off (the default): every egress denied, as before.
    code_interpreter_open_egress: bool = False
    # Read-only npm and PyPI mirror proxy of the command and service containers
    # (sandbox_run_command, sandbox_start_service): its host joins their egress
    # allow-list and the registry variables point at it. Empty: off, the dev
    # images install offline from their caches. No credential in the URL.
    sandbox_registry_mirror_url: str = ""
    # CPU/memory requested for each OpenSandbox container (code interpreter +
    # custom JS/Python tools). Values are passed through as-is to the
    # OpenSandbox SDK's ``resource`` argument, so use whatever unit strings
    # it/the underlying runtime expects (e.g. "1" or "500m" for CPU, "128Mi"
    # or "1Gi" for memory). A bare value applies to every image key; per-key
    # values use the OPENSANDBOX_IMAGES convention ("python:1,python-dev:2",
    # "128Mi,python-dev:2Gi": the entry without a key is the default), read
    # through ``sandbox_resources(key)`` by every container creation: the code
    # interpreter and its kernel ("python"), the custom JS and Python tools
    # ("nodejs", "python") and sandbox_run_command (the check's image key).
    # Never read the two fields raw: the list form is not a quantity.
    sandbox_cpu: str = "1"
    sandbox_ram: str = "128Mi"

    def sandbox_resources(self, image_key: str) -> dict[str, str]:
        """The ``resource`` argument for a container of *image_key*: its own values, else the defaults."""
        return {
            "cpu": _per_key_value(self.sandbox_cpu, image_key, "1"),
            "memory": _per_key_value(self.sandbox_ram, image_key, "128Mi"),
        }

    # --- Code interpreter sandbox: extra allowed modules ---
    # Comma-separated list of modules to RE-ENABLE inside the built-in code
    # interpreter sandbox even though they are on the shared security blocklist
    # (e.g. "socket" or "socket,http,urllib"). Empty by default so the secure
    # posture is unchanged out of the box.
    #
    # SECURITY: the code interpreter runs in a plain subprocess that shares the
    # host network namespace, so the import blocklist is the only thing stopping
    # sandboxed code from opening network connections. Re-enabling "socket" (or
    # the HTTP stack) grants that code raw network access (SSRF to internal
    # services / the cloud metadata endpoint, data exfiltration). Only set this
    # when the backend runs with appropriate network isolation (e.g. a
    # locked-down container / egress policy).
    #
    # Sandbox-escape primitives (subprocess, multiprocessing, ctypes, importlib,
    # pickle, shelve, marshal, code, codeop — see
    # ``_sandbox_utils.ALWAYS_BLOCKED_MODULES``) can NEVER be re-enabled through
    # this knob: they are intersected out before the blocklist is relaxed.
    # Also in Settings > Access and security, like the custom tool list below: the
    # variable, when set, wins and locks the field.
    code_interpreter_extra_allowed_modules: str = ""

    # --- Code interpreter sandbox workspace ---
    # A directory that survives across code_interpreter calls of one
    # conversation (or one assignment run): ``workspace/`` in the sandbox's
    # working directory. Stored in the object store (S3_BUCKET, under
    # ``sandbox-workspaces/``) with its manifest in PostgreSQL, so every
    # replica and both sandbox backends see the same files; the sandbox gets
    # only its own workspace's files and no storage credential.
    # ``services/sandbox_workspace/`` and docs/docs/usage/sandbox-workspace.md.
    code_interpreter_workspace_enabled: bool = True
    # Per workspace, in MiB. A run that would go past it saves nothing it
    # wrote under workspace/ and says so.
    code_interpreter_workspace_quota_mb: int = 2048
    # Files one workspace may hold.
    code_interpreter_workspace_max_files: int = 20_000
    # A workspace unused for this long is deleted (hours). Archiving or
    # deleting its conversation deletes it at once.
    code_interpreter_workspace_idle_ttl_hours: int = 168
    # An assignment run's workspace is deleted when the run ends; one
    # whose run has not ended and does not wait on a person is deleted after
    # this long unused (hours).
    code_interpreter_run_workspace_idle_ttl_hours: int = 24
    # Entries of the manifest the code_interpreter description and results
    # list; the rest are counted.
    code_interpreter_workspace_manifest_entries: int = 40
    # The local sandbox keeps the workspace files it downloaded in a
    # per-process cache under the temp directory (MiB, least recently used
    # first out). 0 disables the cache: every call downloads what it mounts.
    code_interpreter_workspace_cache_mb: int = 2048
    # ``session: true`` kernels: a kernel unused for this long exits
    # (seconds), and one process keeps at most this many (least recently used
    # first out).
    code_interpreter_kernel_idle_seconds: int = 900
    code_interpreter_kernel_max_per_process: int = 8
    # ``sandbox_run_command`` (OpenSandbox only): the checks a checkout
    # declares in .xtm-one/checks.toml run in a container of the check's
    # image key, kept per workspace like the session kernel. The longest a
    # command may run (seconds; a check's own timeout is capped by it), and
    # whether an argv the checkout did not declare may run at all (off by
    # default: only declared checks).
    code_interpreter_command_max_seconds: int = 1800
    code_interpreter_command_allow_argv: bool = False
    # Packages ``code_interpreter(packages=[...])`` may install into the
    # workspace (``workspace/.venv``): comma-separated distribution names.
    # Empty (the default) disables installs. The install runs on the server,
    # from the index below, wheels only (no build step, no code run), and the
    # sandbox stays offline. A package imported by sandboxed code can import
    # blocked modules itself (as any installed library can), so list only
    # packages you trust.
    code_interpreter_package_allowlist: str = ""
    # The package index installs read: a PEP 691 / PEP 503 "simple" index
    # (a PyPI mirror, a proxy). Empty with a non-empty allow-list = PyPI.
    code_interpreter_package_index_url: str = ""

    # --- Agentic coding labs (ENABLED_FEATURES=AGENTIC_CODING, copilot or dev mode) ---
    # One Kubernetes pod per lab, created and reaped by the platform through
    # the API server (services/sandbox_workspace/labs.py, kube.py). Off until
    # the namespace is set: the platform's service account needs pods,
    # pods/log and pods/exec rights in it, nothing else. No quota, cap or
    # count limit of any kind: the requests below exist for scheduling, and
    # the idle and deadline hours are hygiene (0 = never).
    # dev-docs/reference/agentic-labs.md.
    agentic_labs_namespace: str = ""
    # The API server; empty = the in-cluster address (KUBERNETES_SERVICE_HOST).
    agentic_labs_api_url: str = ""
    agentic_labs_token_path: str = "/var/run/secrets/kubernetes.io/serviceaccount/token"  # noqa: S105 - a path
    agentic_labs_ca_path: str = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
    # Requested per container of a lab pod (no limits are set).
    agentic_labs_cpu_request: str = "250m"
    agentic_labs_memory_request: str = "512Mi"
    # Seconds a lab pod may take to be scheduled and start its workspace container.
    agentic_labs_start_seconds: int = 600
    # Seconds one attempt of the checkout copy into a lab may take, apart from the start budget; a copy
    # that times out is tried once more, so a stalled stream costs at most twice this.
    agentic_labs_copy_seconds: int = 300
    # A lab unused this long is deleted by the hourly sweep (0 = never).
    agentic_labs_idle_hours: int = 12
    # Kubernetes ends a lab pod this old (activeDeadlineSeconds; 0 = never).
    agentic_labs_deadline_hours: int = 72
    # Enterprise Edition licences for the labs (services/sandbox_workspace/licences.py):
    # at every lab start the platform asks the licensing server for one short-lived
    # ci licence per entry of AGENTIC_LABS_LICENCES ("ENV_VAR:product name,...")
    # and hands each to every container of the pod under ENV_VAR. Off while the
    # URL, the token or the mapping is empty. The token stays on the platform.
    agentic_labs_licensing_url: str = ""
    agentic_labs_licensing_token: str = ""
    agentic_labs_licences: Annotated[dict[str, str], NoDecode] = {}
    # Hours each licence is valid for (the licensing server caps it at 168).
    agentic_labs_licence_hours: int = 24
    # A live model for the labs that opt in (LAB_MODEL = "live" in a service's env;
    # services/sandbox_workspace/model_keys.py): at the lab's start the platform mints one
    # personal API key of this account, scoped to the LLM proxy only, and hands it to the pod
    # with the proxy's URL; revoked when the lab goes. No provider credential reaches a pod.
    # Off while the account or the URL is empty (the lab keeps its stub model).
    agentic_labs_model_key_account: str = ""
    # The LLM proxy as a lab reaches it (its OpenAI-compatible /v1, e.g. https://<host>/v1).
    agentic_labs_model_base_url: str = ""
    # Comma-separated model names the lab's backend offers (LLM_PROVIDER_CUSTOM_OPENAI_MODELS).
    agentic_labs_model_names: str = ""
    # Hours a lab's model key is valid for (at most 72); revoked earlier when the lab stops.
    agentic_labs_model_key_hours: int = 12
    # Extra images a lab service may run on, "key:image,..." (e.g. opencti:docker.io/opencti/platform:6.9.0):
    # the operator's allow-list for the products a lab runs next to the checkout (a search engine, a broker, a
    # product image). A lab pod runs each service of a chain in its own container, so a service naming one of
    # these keys gets that image; the OpenSandbox path keeps one image per chain. Empty = none.
    agentic_labs_images: Annotated[dict[str, str], NoDecode] = {}

    # --- Custom tool Python sandbox: extra allowed modules ---
    # Comma-separated list of modules to RE-ENABLE inside the custom Python tool
    # sandbox even though they are on the shared security blocklist. Useful for
    # deployments where custom tools need outbound HTTP access (e.g.
    # "requests,urllib,http" to call external APIs such as OpenCTI). Empty by
    # default so the secure posture is unchanged out of the box.
    #
    # SECURITY: the subprocess fallback shares the host network namespace, so
    # re-enabling HTTP/socket modules grants sandboxed code raw network access
    # (SSRF to internal services / the cloud metadata endpoint, data
    # exfiltration). The Docker sandbox (CUSTOM_TOOL_SANDBOX=docker) provides
    # OS-level network isolation regardless of this setting. Only set this in
    # the subprocess fallback when the backend runs with appropriate network
    # isolation (e.g. a locked-down container with egress policy).
    #
    # Sandbox-escape primitives (subprocess, multiprocessing, ctypes, importlib,
    # pickle, shelve, marshal, code, codeop — see
    # ``_sandbox_utils.ALWAYS_BLOCKED_MODULES``) can NEVER be re-enabled through
    # this knob: they are intersected out before the blocklist is relaxed, so a
    # misconfiguration cannot punch a hole straight out of the sandbox.
    custom_tool_extra_allowed_modules: str = ""

    # --- Progressive tool disclosure (agentic loop) ---
    # When an agent has many tools (integrations + MCP servers + custom
    # tools), sending every tool's JSON schema to the LLM on every turn is
    # expensive: a trivial "hello" pays the full tool-definition token cost
    # for tools it never uses, and large tool lists measurably degrade
    # tool-selection accuracy.  Progressive disclosure instead injects a
    # compact catalogue of tool *groups* (one per integration provider / MCP
    # server, plus one for custom tools) and a single ``load_tools``
    # meta-tool; the model reveals a group's full tool schemas only when it
    # actually needs them.  See ``app/tools/tool_gating.py``.
    progressive_tools_enabled: bool = True
    # Minimum number of *gateable* tools (integration + MCP + custom) before
    # progressive disclosure activates.  Below this threshold all tools are
    # sent up-front — gating would only add a discovery round-trip with no
    # meaningful token savings.
    progressive_tools_min_tools: int = 16

    # --- Per-run spending budget (agentic loop) ---
    # One budget object bounds a single run along four dimensions, enforced
    # in the agent loop itself (provider-independent) with the
    # reserve -> call -> debit -> block pattern: before every LLM call the
    # loop reserves that call's worst-case spend, and the call is refused
    # when the reservation no longer fits.  ``0`` disables a dimension; with
    # every dimension at ``0`` there is no budget object at all and the loop
    # behaves exactly as before.  See ``app/tools/run_budget.py``.
    #
    # Hard ceiling (USD) on the priced LLM spend of one run.  Requires a
    # price row for the model in ``llm_model_prices`` (seeded from the
    # LiteLLM catalogue) — with no price the cost dimension fails OPEN (a
    # WARNING is logged and the other dimensions still apply), because
    # missing pricing data must never be able to stop every run.
    agent_loop_max_cost_usd: float = 0.0
    # Ceiling on the accumulated LLM token usage of one run.  Hard once the
    # budget object is active (the call that would cross it is refused); the
    # legacy ``run_agent_loop(max_total_tokens=...)`` argument keeps its
    # softer wind-down semantics for callers that pass it directly.
    agent_loop_max_total_tokens: int = 0
    # Ceiling on the number of tool calls one run may dispatch.  Distinct
    # from ``max_iterations``: a single iteration can fan out into many
    # parallel tool calls, so the iteration cap is a poor proxy for this.
    agent_loop_max_tool_calls: int = 0
    # Cut a parallel batch that crosses the tool-call ceiling to the calls
    # left: the calls past it are answered "Not executed" instead of
    # run.  Off by default, as before: the crossing batch runs in full, the
    # overshoot is logged and traced, and the next LLM call is refused.
    agent_loop_max_tool_calls_strict: bool = False
    # Ceiling (seconds) on the wall-clock duration of one run.  Folded into
    # the loop's existing ``wall_clock_deadline`` — whichever bound is
    # tighter wins — so duration keeps its battle-tested enforcement path
    # (graceful forced-final-answer synthesis) instead of a second one.
    agent_loop_max_duration_seconds: int = 0
    # Fraction of the cost/token limits held back for the closing synthesis
    # call: in-loop calls are gated at ``limit * (1 - reserve)`` while the
    # forced-final-answer backstop may spend the full limit.  Without it,
    # every exhausted budget would degrade to a deterministic tool-result
    # digest instead of a real answer.  Clamped to [0, 0.5].
    agent_loop_budget_final_answer_reserve: float = 0.1
    # Opt-in: when the COST dimension refuses the next LLM call and a
    # platform fallback model is configured (``PlatformSettings.fallback_model``
    # / ``DEFAULT_FALLBACK_MODEL`` — the same surface the failover chain uses),
    # downgrade the run to that model and CONTINUE instead of hard-stopping.
    # One downgrade per run, cost only: tokens / tool calls / duration are
    # model-independent, so a cheaper model buys no extra headroom there.
    # Default ``False`` — finishing a run on a weaker model must be something
    # an operator chose, not a silent quality change.
    agent_loop_budget_fallback_enabled: bool = False

    # --- Loop breaker (agentic loop) ---
    # A model that calls one tool with the SAME arguments over and over is
    # stuck, whatever the tool answers: the 25 Sep 2026 run made ~40
    # ``read_file_content`` calls over three file ids. The loop counts every
    # dispatched call per ``(tool, arguments)`` and per ``(tool, target)`` -
    # the target being the ``file_id`` / ``url`` the call names, so a re-read
    # that only varies ``offset`` / ``max_chars`` is the "similar" shape - and
    # WARNS the model with a system message when a count reaches the first
    # bound, then REFUSES the call at the second. Read-tool counts reset when
    # a write tool runs (re-reading after a mutation is legitimate); polling
    # tools (``wait``, ``check_task_status``) are never counted. ``0`` on a
    # bound disables that bound.
    agent_loop_identical_call_warn_after: int = 3
    agent_loop_identical_call_refuse_after: int = 5
    agent_loop_similar_call_warn_after: int = 6
    agent_loop_similar_call_refuse_after: int = 10
    # Fraction of ``max_iterations`` at which the loop tells the model, once,
    # how much of its step budget it has spent and on which tools (the top 3
    # by call count) - before the wind-down nudge, which comes a few
    # iterations from the cap. ``0`` disables the nudge.
    agent_loop_step_budget_nudge_fraction: float = 0.6
    # Long runs: a chat turn, a task it delegates or an assignment run
    # still making progress when it reaches its ``max_iterations`` gets another
    # block of the same size instead of winding down, up to
    # ``MAX_ITERATIONS_CEILING``, and a chat turn's wall clock is sized for
    # that reach. The per-run budget, an assignment's own timeout and the loop
    # breaker still end it. ``false`` makes ``max_iterations`` a hard cap again.
    agent_loop_auto_extend_iterations: bool = True
    # --- Vision budget (agentic loop) ---
    # How many DISTINCT images reach the model as vision blocks in one turn
    # (chat turn, assignment run, delegated task) PER 25 STEPS of its step
    # budget, whatever tool attached them: a ``read_file_content``, a browser
    # screenshot, a rendered figure, a generated image. A run allowed 100
    # steps - an agent's own max iterations, or a run its progress extended -
    # may look at four times as many (``agent_loop.vision_cap_for``), so a
    # long visual job (a video, a deck) keeps its sight while it has steps.
    # Enforced by the loop where every tool result is appended: past the cap
    # the result keeps its text and loses its pixels, with an explicit
    # ``vision budget exhausted`` note naming the files - never a silent
    # send. The same file is attached at most ONCE per turn - a second read
    # returns the note the model saved with ``annotate_file`` instead of the
    # pixels. ``0`` removes the cap (the once-per-file rule stays), like
    # every other bound in this block.
    agent_loop_vision_views_per_turn: int = 10
    # --- Read windows per file (agentic loop) ---
    # How many text windows ``read_file_content`` serves for ONE file in one
    # turn, per view (the source, or the ``extract_structured`` rendering of
    # an HTML / JSON / JSON Lines / CSV file). The next call is an explicit tool error
    # naming what was read and the ways forward (the rendered view, a
    # computation in the code interpreter where the file is mounted), never
    # another window. A 558k attachment paged in twelve 50k windows, each
    # pushing the previous out of the context, is how the 25 Sep 2026 turn
    # reached 97 iterations. Images are never counted. ``0`` removes the cap.
    agent_loop_read_windows_per_file: int = 4

    # --- Platform URL guard (assistant response post-processing) ---
    # Deterministic post-processor that validates platform deep links
    # (OpenCTI / OpenAEV ``/dashboard/...`` routes) in final assistant
    # responses, auto-corrects commonly hallucinated route segments, and
    # falls back to the universal ID resolver or a search link when a URL
    # cannot be verified.  Pure CPU regex/string work — no network calls.
    # See ``app/services/url_guard.py``.
    url_guard_enabled: bool = True
    # Optional path to a JSON file ({"routes": {...}, "corrections": {...}})
    # merged over the built-in route manifest and correction rules, so
    # product teams can extend the known-route data without code changes.
    url_guard_manifest_path: str | None = None

    # --- Nova prompt-safety engine ---
    # Only read once the Nova integration is connected AND the ``nova-hunting``
    # engine is installed (``services/nova_engine.py``); the built-in keyword
    # tier makes no provider call. How many provider calls one evaluation runs
    # at once: its ``llm:`` conditions, and the ``semantics:`` pattern
    # embeddings not cached yet. The same calls are made and give the same
    # verdicts whatever the value; ``1`` makes them one after another.
    nova_evaluation_concurrency: int = 4
    # The most distinct ``llm:`` conditions one evaluation asks the model.
    # ``0`` (default) asks every one, as the guard always has: a ceiling
    # trades detection for cost, so it is an operator's choice, reported by
    # the Security Audit (``agents.nova_llm_every_condition``) and logged
    # once per rule set with the number of conditions it leaves unasked.
    nova_llm_max_conditions: int = 0

    # --- Feature flags (dev/experimental features) ---
    # Comma-separated list of feature flag ids to enable, e.g.
    # "MY_FLAG,OTHER_FLAG". Use "*" to enable every known flag at once.
    # Unset/empty = NO flags enabled (fail-closed default): these gate
    # unfinished/experimental code paths, so "not configured" must never
    # mean "everything on". Requires a restart to change — there is no
    # admin UI toggle by design. See app/core/feature_flags.py.
    enabled_features: str = ""

    model_config = {
        "env_file": (".env", "../.env"),
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }

    # What ``_EnvironmentRecord`` saw while this instance was built.
    _environment_fields: frozenset[str] = PrivateAttr(default_factory=frozenset)

    def __init__(self, **values: Any) -> None:
        record: set[str] = set()
        token = _recorded_fields.set(record)
        try:
            super().__init__(**values)
        finally:
            _recorded_fields.reset(token)
        self._environment_fields = frozenset(record)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # pydantic-settings' own sources in their own order; the record comes
        # last so it sees what they set, and adds nothing.
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            file_secret_settings,
            _EnvironmentRecord(settings_cls),
        )

    @property
    def environment_fields(self) -> frozenset[str]:
        """The fields a variable set (not blank) when this instance was built.

        The process environment, the ``.env`` files or any other settings
        source - never a default. A security option in this set is locked in
        Settings > Access and security (``core/security_options.py``).
        """
        return self._environment_fields

    @field_validator("*", mode="before")
    @classmethod
    def _blank_switch_is_unset(cls, v: object, info: ValidationInfo) -> object:
        # A blank boolean switch keeps its default, as it is for the field's
        # lock (``_EnvironmentRecord``): docker-compose.yml forwards a switch
        # that is also a Settings > Access and security option as ``${NAME:-}``, which
        # must neither lock that option nor fail the boolean parse.
        if isinstance(v, str) and not v.strip() and info.field_name is not None:
            field = cls.model_fields[info.field_name]
            if field.annotation in (bool, bool | None):
                return field.default
        return v

    @field_validator(
        "a2a_client_sweep_interval_seconds",
        "a2a_client_verdict_staleness_seconds",
        "a2a_client_delegation_ceiling_seconds",
        "a2a_client_delegation_abandoned_seconds",
        "a2a_client_grace_seconds",
        "a2a_client_floor_interval_seconds",
        "a2a_client_response_max_bytes",
        "a2a_client_breaker_threshold",
        "a2a_client_breaker_cooldown_seconds",
        "a2a_task_max_lifetime_seconds",
        "a2a_cancel_poll_seconds",
        "a2a_push_max_attempts",
        "a2a_push_configs_per_task",
    )
    @classmethod
    def _validate_a2a_bounds(cls, v: int, info) -> int:
        # Fail closed on a misconfigured bound rather than degrading silently.
        # Every one of these is a control, not a tuning knob: a zero staleness
        # threshold would re-verify on every delegation, a zero ceiling would
        # abandon every delegation instantly, a zero breaker threshold would
        # open the circuit on the first blip, a zero task lifetime would reap
        # every task on creation, and a zero callback allowance would advertise
        # a capability the card declares and then refuse it. A typo in
        # any of them is a startup error, not a behaviour change nobody
        # notices (Principle II).
        if v <= 0:
            raise ValueError(f"{info.field_name.upper()} must be a positive integer, got {v!r}")
        return v

    @field_validator(
        "agent_loop_identical_call_warn_after",
        "agent_loop_identical_call_refuse_after",
        "agent_loop_similar_call_warn_after",
        "agent_loop_similar_call_refuse_after",
        "agent_loop_vision_views_per_turn",
        "agent_loop_read_windows_per_file",
    )
    @classmethod
    def _validate_loop_breaker_bounds(cls, v: int, info) -> int:
        # ``0`` disables the bound; a negative value is a typo, not a setting,
        # and would otherwise be clamped to ``0`` silently - a disabled breaker
        # nobody asked for.
        if v < 0:
            raise ValueError(f"{info.field_name.upper()} must be >= 0 (0 disables it), got {v!r}")
        return v

    @field_validator("admin_email")
    @classmethod
    def _normalise_admin_email(cls, v: str) -> str:
        # Every user email is stored lowercased; the admin account is matched
        # against this value, so it must be lowercased too.
        return v.strip().lower()

    @field_validator("microsoft_teams_message_text_limit")
    @classmethod
    def _validate_teams_message_text_limit(cls, v: int) -> int:
        # A listing that shows none of a message's text is no listing: a typo
        # must not turn every message into its truncation marker.
        if v < 1:
            raise ValueError(f"MICROSOFT_TEAMS_MESSAGE_TEXT_LIMIT must be a positive integer, got {v!r}")
        return v

    @field_validator("sandbox_registry_mirror_url")
    @classmethod
    def _validate_sandbox_registry_mirror_url(cls, v: str) -> str:
        # A malformed or credentialed mirror URL is a startup error, never a
        # credential copied into every command container's environment: the
        # mirror is read-only and anonymous.
        from urllib.parse import urlsplit

        url = (v or "").strip()
        if not url:
            return ""
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError(f"SANDBOX_REGISTRY_MIRROR_URL must be an http(s) URL with a host, got {url!r}")
        if parts.username or parts.password:
            raise ValueError(
                "SANDBOX_REGISTRY_MIRROR_URL must carry no credential: the mirror is read-only and anonymous"
            )
        if parts.query or parts.fragment:
            raise ValueError(f"SANDBOX_REGISTRY_MIRROR_URL must carry no query or fragment, got {url!r}")
        return url.rstrip("/")

    @field_validator(
        "migration_lock_timeout_seconds",
        "migration_statement_timeout_seconds",
        "startup_watchdog_seconds",
    )
    @classmethod
    def _validate_startup_bounds(cls, v: int, info) -> int:
        # A negative timeout would reach PostgreSQL as a connection error that
        # does not name the setting, and a negative watchdog delay would mean
        # something different to the platform and to the worker.
        if v < 0:
            raise ValueError(f"{info.field_name.upper()} must be >= 0 (0 disables it), got {v!r}")
        return v

    @field_validator("nova_evaluation_concurrency")
    @classmethod
    def _validate_nova_evaluation_concurrency(cls, v: int) -> int:
        # ``0`` or a negative value would ask nothing at all; past the bound a
        # screened message would open that many provider calls at once.
        if not 1 <= v <= 32:
            raise ValueError(f"NOVA_EVALUATION_CONCURRENCY must be between 1 and 32, got {v!r}")
        return v

    @field_validator("nova_llm_max_conditions")
    @classmethod
    def _validate_nova_llm_max_conditions(cls, v: int) -> int:
        # ``0`` asks every condition; a negative value is a typo, not a ceiling.
        if v < 0:
            raise ValueError(f"NOVA_LLM_MAX_CONDITIONS must be >= 0 (0 asks every condition), got {v!r}")
        return v

    @field_validator("model_catalog_emails_recipients")
    @classmethod
    def _validate_model_catalog_emails_recipients(cls, v: str) -> str:
        normalized = (v or "").strip().lower() or "all"
        if normalized not in {"all", "admins"}:
            raise ValueError(f"MODEL_CATALOG_EMAILS_RECIPIENTS must be all|admins, got {v!r}")
        return normalized

    @field_validator("model_catalog_emails_batch_size")
    @classmethod
    def _validate_model_catalog_emails_batch_size(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"MODEL_CATALOG_EMAILS_BATCH_SIZE must be >= 1, got {v!r}")
        return v

    @field_validator("model_catalog_emails_settle_minutes")
    @classmethod
    def _validate_model_catalog_emails_settle_minutes(cls, v: int) -> int:
        # A day at most: a change held past ``MAX_AGE`` (7 days) would expire unsent.
        if not 0 <= v <= 1440:
            raise ValueError(f"MODEL_CATALOG_EMAILS_SETTLE_MINUTES must be between 0 and 1440, got {v!r}")
        return v

    @field_validator("model_catalog_changes_window_hours")
    @classmethod
    def _validate_model_catalog_changes_window_hours(cls, v: int) -> int:
        # Past 7 days an admin catalog change is no longer delivered (``MAX_AGE``).
        if not 1 <= v <= 168:
            raise ValueError(f"MODEL_CATALOG_CHANGES_WINDOW_HOURS must be between 1 and 168, got {v!r}")
        return v

    @field_validator("assignment_stale_reference_pause_after")
    @classmethod
    def _validate_stale_reference_pause_after(cls, v: int) -> int:
        # ``0`` disables the pause; a negative value is a typo, not a setting,
        # and would otherwise read as "disabled" without anyone asking for it.
        if v < 0:
            raise ValueError(
                f"ASSIGNMENT_STALE_REFERENCE_PAUSE_AFTER must be >= 0 (0 disables the pause), got {v!r}"
            )
        return v

    @field_validator("agent_loop_step_budget_nudge_fraction")
    @classmethod
    def _validate_step_budget_nudge_fraction(cls, v: float) -> float:
        # A fraction of the iteration budget: ``0`` disables the nudge, ``1``
        # leaves it no iteration to land on before the cap (disabled too, the
        # loop says so), anything outside ``[0, 1]`` is a typo.
        if not 0 <= v <= 1:
            raise ValueError(
                f"AGENT_LOOP_STEP_BUDGET_NUDGE_FRACTION must be between 0 and 1 (0 disables it), got {v!r}"
            )
        return v

    @model_validator(mode="after")
    def _warn_loop_breaker_warn_at_or_past_refuse(self) -> Settings:
        # The refusal is checked first, so a warn bound at or past its refuse
        # bound never fires: the model is refused with no nudge before it. A
        # legitimate configuration (an operator who wants refusals only sets
        # the warn bound to 0), so this is a startup warning, not an error.
        for shape in ("identical", "similar"):
            warn = getattr(self, f"agent_loop_{shape}_call_warn_after")
            refuse = getattr(self, f"agent_loop_{shape}_call_refuse_after")
            if warn and refuse and warn >= refuse:
                logging.getLogger(__name__).warning(
                    "AGENT_LOOP_%s_CALL_WARN_AFTER=%d is not below AGENT_LOOP_%s_CALL_REFUSE_AFTER=%d: "
                    "the warning never fires, the call is refused without a nudge before it "
                    "(set the warn bound to 0 to say so on purpose)",
                    shape.upper(),
                    warn,
                    shape.upper(),
                    refuse,
                )
        return self

    @model_validator(mode="after")
    def _warn_runner_max_result_bytes_not_positive(self) -> Settings:
        # Accepted as it always was, so a deployment that sets it keeps
        # booting and behaving the same, but said out loud.
        if self.runner_max_result_bytes <= 0:
            logging.getLogger(__name__).warning(
                "RUNNER_MAX_RESULT_BYTES=%d is not a positive size: every non-empty runner file "
                "result is refused at upload, and runner file reads are delivered with the size asked, "
                "unbounded",
                self.runner_max_result_bytes,
            )
        return self

    @model_validator(mode="after")
    def _warn_base_url_unset(self) -> Settings:
        # The localhost default only fits the dev stack: a deployment that
        # forgets ``BASE_URL`` builds OAuth redirects, webhook and e-mail links
        # that point at localhost. Said when the settings load, not refused,
        # so a quick local run outside Compose still boots. An empty value (a
        # manifest that passes ``BASE_URL=${BASE_URL}`` unset) is said the same.
        if not self.debug and ("base_url" not in self.model_fields_set or not self.base_url.strip()):
            logging.getLogger(__name__).warning(
                "BASE_URL is not set: the platform uses %s for its OAuth redirects, webhook and "
                "e-mail links. Set BASE_URL to the URL users reach the platform on.",
                self.base_url.strip() or "an empty URL",
            )
        return self

    @field_validator("custom_tool_sandbox")
    @classmethod
    def _validate_custom_tool_sandbox(cls, v: str) -> str:
        # Normalise + fail closed on a misconfigured value so a typo
        # (e.g. ``CUSTOM_TOOL_SANDBOX=dokcer``) is caught at startup rather
        # than silently degrading to the weaker subprocess sandbox.
        normalized = (v or "").strip().lower()
        if not normalized:
            return "auto"
        allowed = {"auto", "docker", "subprocess", "opensandbox"}
        if normalized not in allowed:
            raise ValueError(f"CUSTOM_TOOL_SANDBOX must be one of auto|docker|subprocess|opensandbox, got {v!r}")
        return normalized

    @field_validator("platform_id")
    @classmethod
    def _validate_platform_id(cls, v: str) -> str:
        # Normalise + fail closed on a malformed forced instance ID so a typo
        # (e.g. a truncated UUID pasted into ``PLATFORM_ID``) is caught at
        # startup rather than silently ignored — silently generating a random
        # ID instead would defeat the whole point of forcing it (clone /
        # restore identity continuity). Mirrors OpenCTI, which raises a
        # ConfigurationError on a non-UUID ``platform_id``.
        normalized = (v or "").strip()
        if not normalized:
            return ""
        import uuid as _uuid

        try:
            return str(_uuid.UUID(normalized))
        except ValueError as exc:
            raise ValueError(f"PLATFORM_ID must be a valid UUID (or empty), got {v!r}") from exc

    @field_validator("xtm_hub_default_url")
    @classmethod
    def _validate_xtm_hub_default_url(cls, v: str) -> str:
        # Empty keeps the public XTM Hub; anything else must be an address the
        # XTM Hub client accepts (``xtm_hub_base_url``), refused at startup
        # rather than at the first registration.
        normalized = (v or "").strip()
        if not normalized:
            return "https://hub.filigran.io"
        from urllib.parse import urlsplit

        try:
            parts = urlsplit(normalized)
        except ValueError:
            parts = None
        if (
            parts is None
            or parts.scheme not in ("http", "https")
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.query
            or parts.fragment
        ):
            raise ValueError(
                "XTM_HUB_DEFAULT_URL must be an absolute http(s) URL without "
                f"credentials, query or fragment, got {v!r}"
            )
        return normalized.rstrip("/")

    @model_validator(mode="after")
    def _cancel_poll_leaves_room_for_the_lease(self) -> Settings:
        """The poll paces the heartbeat, so it must fit inside the lease
        several times over: above a quarter-lease the supervisor beats slower
        than the reaper reaps, and a live run is taken as abandoned.
        """
        if self.a2a_cancel_poll_seconds > self.a2a_run_lease_seconds // 4:
            raise ValueError(
                "A2A_CANCEL_POLL_SECONDS must not exceed a quarter of "
                "A2A_RUN_LEASE_SECONDS, or a running task's lease goes stale "
                "before its supervisor refreshes it"
            )
        return self

    @model_validator(mode="after")
    def _validate_xtm_hub_gate(self) -> Settings:
        # Fail loudly at startup rather than quietly at every sign-in (FR-011).
        # The gate looks for a claim named after this instance, so enabling it
        # on an instance with no identifier would refuse EVERY sign-in — with
        # nothing in the log to say why, since each refusal looks like an
        # ordinary unentitled person. Refusing to boot costs one restart
        # instead of an outage nobody can diagnose from a login page.
        if self.xtm_hub_admin_group.strip() and not self.xtm_hub_effective_platform_id:
            raise ValueError(
                "PLATFORM_ID must be set when XTM_HUB_ADMIN_GROUP is set: the "
                "XTM Hub entitlement gate looks for a claim named after this "
                "instance, and would otherwise refuse every sign-in"
            )
        return self

    @property
    def xtm_hub_effective_platform_id(self) -> str:
        """The identifier the XTM Hub gate looks for, in the claim name.

        ``platform_id`` is the real answer. ``xtm_hub_platform_id`` overrides it
        only so the integration can be tested against a Hub tenant that still
        issues non-UUID names (see the field comment); production leaves it
        empty and gets ``platform_id``.
        """
        return (self.xtm_hub_platform_id or "").strip() or (self.platform_id or "").strip()

    @field_validator("default_reasoning_effort")
    @classmethod
    def _validate_default_reasoning_effort(cls, v: str) -> str:
        # Normalise + fail closed on a misconfigured value so a typo
        # (e.g. ``DEFAULT_REASONING_EFFORT=hihg``) is caught at startup
        # rather than silently seeding an unusable level into
        # PlatformSettings. The allowed set mirrors the per-agent
        # ``reasoning_effort`` values and the frontend
        # ``REASONING_EFFORT_LEVELS`` constant.
        normalized = (v or "").strip().lower()
        if not normalized:
            return ""
        allowed = {"low", "medium", "high", "max"}
        if normalized not in allowed:
            raise ValueError(f"DEFAULT_REASONING_EFFORT must be one of low|medium|high|max (or empty), got {v!r}")
        return normalized

    @field_validator("prompt_cache_ttl")
    @classmethod
    def _validate_prompt_cache_ttl(cls, v: str) -> str:
        # Normalise + fail closed so a typo (e.g. ``PROMPT_CACHE_TTL=1hr``)
        # is caught at startup instead of triggering API 400s on every
        # Anthropic call once caching is active.
        normalized = (v or "").strip().lower()
        if not normalized:
            return "5m"
        allowed = {"5m", "1h"}
        if normalized not in allowed:
            raise ValueError(f"PROMPT_CACHE_TTL must be one of 5m|1h, got {v!r}")
        return normalized

    @field_validator("platform_mode")
    @classmethod
    def _validate_platform_mode(cls, v: str) -> str:
        # Fail closed on a typo: an unrecognised mode compares as "not
        # xtm_one", so ``ai_disable`` would run a fully enabled AI platform on
        # a deployment that asked for the opposite.
        normalized = (v or "").strip().lower()
        if not normalized:
            return "xtm_one"
        if normalized not in PLATFORM_MODES:
            raise ValueError(f"PLATFORM_MODE must be one of {'|'.join(PLATFORM_MODES)}, got {v!r}")
        return normalized

    @field_validator("base_path")
    @classmethod
    def _validate_base_path(cls, v: str) -> str:
        # Canonical form (``/xtm-one``: one leading slash, no trailing slash)
        # so every consumer - the ASGI normaliser, the index.html renderer,
        # the manifest - concatenates without a second normalisation, and a
        # refusal for a prefix the platform could not route unambiguously
        # (``/api``, ``/mcp``, ...): under such a prefix a proxy that strips
        # it and one that forwards it would produce the same request path for
        # two different resources.
        from app.core.base_path import normalize_base_path, reserved_segment_problem

        normalized = normalize_base_path(v)
        problem = reserved_segment_problem(normalized)
        if problem:
            raise ValueError(problem)
        return normalized

    @field_validator("base_path_mode")
    @classmethod
    def _validate_base_path_mode(cls, v: str) -> str:
        from app.core.base_path import normalize_base_path_mode

        return normalize_base_path_mode(v)

    @model_validator(mode="after")
    def _base_path_is_unambiguous_in_its_mode(self) -> Settings:
        # A prefix that is also a platform page (``/tools``) cannot be routed
        # by inference behind a stripping proxy - the public ``/tools/tools/1``
        # arrives as ``/tools/1``, the forwarded shape of another page. Refused
        # unless the operator declares the proxy (``BASE_PATH_MODE``), which is
        # what makes the request rule exact. Fail at startup, never misroute
        # silently.
        from app.core.base_path import spa_route_problem

        problem = spa_route_problem(self.base_path, self.base_path_mode)
        if problem:
            raise ValueError(problem)
        return self

    @field_validator("content_security_policy_mode")
    @classmethod
    def _validate_content_security_policy_mode(cls, v: str) -> str:
        from app.core.csp import normalize_csp_mode

        return normalize_csp_mode(v)

    @field_validator("content_security_policy_frame_ancestors", mode="before")
    @classmethod
    def _parse_content_security_policy_frame_ancestors(cls, v: object) -> list[str]:
        from app.core.csp import normalize_frame_ancestor_sources

        return normalize_frame_ancestor_sources(v)

    @field_validator("xtm_auth_accepted_audiences", mode="before")
    @classmethod
    def _parse_xtm_auth_accepted_audiences(cls, v: object) -> list[str]:
        # Raises URLSettingError, not ValueError: pydantic would quote the raw
        # value, and with it any password typed into an entry.
        from app.core.url_identity import parse_url_identity_list

        return parse_url_identity_list(v, setting="XTM_AUTH_ACCEPTED_AUDIENCES")

    @field_validator("assignment_queue_overflow_policy")
    @classmethod
    def _validate_assignment_queue_overflow_policy(cls, v: str) -> str:
        # Normalise + fail closed on a typo (e.g. ``drop-oldest``) so a
        # misconfigured overflow policy is caught at startup rather than
        # silently falling back to a behaviour the operator did not choose —
        # the difference between "reject" and "drop_newest" is the difference
        # between a sender that retries and one that loses the alert.
        normalized = (v or "").strip().lower()
        if not normalized:
            return "reject"
        allowed = {"reject", "drop_oldest", "drop_newest"}
        if normalized not in allowed:
            raise ValueError(
                "ASSIGNMENT_QUEUE_OVERFLOW_POLICY must be one of "
                f"reject|drop_oldest|drop_newest, got {v!r}"
            )
        return normalized

    @field_validator("phone_notification_content")
    @classmethod
    def _validate_phone_notification_content(cls, v: str) -> str:
        normalized = (v or "").strip().lower()
        if normalized not in {"full", "minimal"}:
            raise ValueError(f"PHONE_NOTIFICATION_CONTENT must be one of full|minimal, got {v!r}")
        return normalized

    @field_validator("rate_limit_fail_mode", "quota_fail_mode")
    @classmethod
    def _validate_fail_mode(cls, v: str, info) -> str:
        # Normalise + fail closed on a misconfigured value so a typo
        # (e.g. ``QUOTA_FAIL_MODE=close`` or ``strict``) is caught at
        # startup instead of silently meaning "open" — for a security
        # fail-closed flag, a typo degrading to fail-open is exactly the
        # bypass the flag exists to prevent. Mirrors the
        # ``default_reasoning_effort`` / ``redis_mode`` validators.
        normalized = (v or "").strip().lower()
        if normalized not in {"open", "closed"}:
            raise ValueError(f"{info.field_name.upper()} must be one of open|closed, got {v!r}")
        return normalized

    @field_validator("prompt_guard_model_output")
    @classmethod
    def _validate_prompt_guard_model_output(cls, v: str) -> str:
        # A typo must not silently mean "off" for a switch someone set on purpose.
        normalized = (v or "").strip().lower()
        if normalized not in {"off", "log", "block"}:
            raise ValueError(f"PROMPT_GUARD_MODEL_OUTPUT must be one of off|log|block, got {v!r}")
        return normalized

    @field_validator("rate_limit_ipv6_prefix")
    @classmethod
    def _validate_rate_limit_ipv6_prefix(cls, v: int) -> int:
        # Refused at startup rather than clamped: above 128 there is no such
        # network, and below 32 one bucket would span many unrelated sites.
        if not 32 <= v <= 128:
            raise ValueError(
                f"RATE_LIMIT_IPV6_PREFIX must be between 32 and 128 (128 = one bucket per address), got {v!r}"
            )
        return v

    @field_validator("app_max_concurrency")
    @classmethod
    def _validate_app_max_concurrency(cls, v: int) -> int:
        # Fail closed on a misconfigured limit. ``0`` is the documented
        # "disable the in-app limiter" value; a negative value is a typo that
        # would ALSO silently disable overload shedding (the middleware treats
        # ``<= 0`` as off), so reject it at startup rather than let a
        # misconfiguration quietly remove the health-exempt 429 backpressure.
        if v < 0:
            raise ValueError(f"APP_MAX_CONCURRENCY must be >= 0 (0 disables the in-app limiter), got {v!r}")
        return v

    @field_validator("default_thread_pool_max_workers")
    @classmethod
    def _validate_default_thread_pool_max_workers(cls, v: int) -> int:
        # Fail closed on a misconfigured size. ``0`` is the documented "auto"
        # value; a negative value is a typo that should be caught at startup
        # rather than silently coerced. The executor helper additionally
        # clamps the effective size to never drop below Python's default.
        if v < 0:
            raise ValueError(f"DEFAULT_THREAD_POOL_MAX_WORKERS must be >= 0 (0 = auto), got {v!r}")
        return v

    @field_validator("worker_shutdown_grace_period")
    @classmethod
    def _validate_worker_shutdown_grace_period(cls, v: int) -> int:
        # Fail closed on a misconfigured grace window. ``0`` is the documented
        # explicit opt-out (immediate cancellation on SIGTERM — SAQ's legacy
        # behaviour); a negative value is a typo that would ALSO silently
        # reintroduce immediate cancellation (SAQ treats ``None``/falsy as no
        # grace), so reject it at startup rather than mask the intent.
        if v < 0:
            raise ValueError(
                "WORKER_SHUTDOWN_GRACE_PERIOD must be >= 0 (0 disables the "
                f"grace period — immediate cancellation), got {v!r}"
            )
        return v

    @field_validator("worker_termination_grace_seconds", "worker_startup_seconds")
    @classmethod
    def _validate_worker_restart_seconds(cls, v: int) -> int:
        if v < 0:
            raise ValueError(f"WORKER_TERMINATION_GRACE_SECONDS and WORKER_STARTUP_SECONDS must be >= 0, got {v!r}")
        return v

    @field_validator("default_embedding_dimensions")
    @classmethod
    def _validate_default_embedding_dimensions(cls, v: int | None) -> int | None:
        # Fail closed on a misconfigured vector size so a typo
        # (``DEFAULT_EMBEDDING_DIMENSIONS=0`` or a negative value) is caught at
        # startup rather than seeded unconditionally into PlatformSettings,
        # where a non-positive value corrupts stale-embedding detection
        # (``expected_dims`` comparisons) and is treated as falsy by callers
        # that fall back when dimensions are unset.
        if v is not None and v <= 0:
            raise ValueError(f"DEFAULT_EMBEDDING_DIMENSIONS must be a positive integer (or unset), got {v!r}")
        return v

    @field_validator("enabled_features")
    @classmethod
    def _validate_enabled_features(cls, v: str) -> str:
        # Unknown flag ids are logged and stripped rather than crashing
        # startup. This keeps switching branches painless: a flag id that
        # is valid on one branch but absent on another (renamed, removed,
        # or not yet merged) must never brick boot for everyone sharing an
        # .env. Unknown ids are dropped from the stored value here so they
        # are truly fail-closed — the raw string only ever retains known
        # ids (plus the "*" wildcard), so no code path can accidentally see
        # an unknown id as enabled. It is a no-op we surface loudly in logs.
        tokens = [t.strip() for t in (v or "").split(",") if t.strip()]
        known = [t for t in tokens if t in KNOWN_FEATURE_FLAGS or t == FEATURE_FLAG_ALL]
        unknown = sorted(set(tokens) - set(known))
        if unknown:
            import logging

            logging.getLogger(__name__).warning(
                "ENABLED_FEATURES contains unknown flag id(s): %s — ignoring "
                "them. Known flags: %s (or %r for all). This is expected when "
                "switching branches; remove them from your environment to "
                "silence this warning.",
                unknown,
                sorted(KNOWN_FEATURE_FLAGS) or "(none defined yet)",
                FEATURE_FLAG_ALL,
            )
        # Return the cleaned value with unknown ids removed (deduplicated,
        # order preserved) so ``enabled_feature_flags`` and every raw reader
        # observe only known ids.
        return ",".join(dict.fromkeys(known))

    @field_validator("opensandbox_k8s_pool_names", mode="before")
    @classmethod
    def _parse_opensandbox_k8s_pool_names(cls, v: object) -> dict[str, str]:
        # An entry with no ":" (including a bare single-pool-name value from
        # before this setting became a mapping) is dropped rather than
        # applied to every key, so that key falls back to cold start — same
        # as leaving the setting unset entirely.
        return _parse_csv_key_value_pairs(v, "OPENSANDBOX_K8S_POOL_NAMES")

    @field_validator("opensandbox_images", mode="before")
    @classmethod
    def _parse_opensandbox_images(cls, v: object) -> dict[str, str]:
        # Overrides merge on top of _OPENSANDBOX_DEFAULT_IMAGES rather than
        # replacing it wholesale, so e.g. OPENSANDBOX_IMAGES=python:my-image
        # still leaves "nodejs" on its built-in default instead of vanishing.
        return {**_OPENSANDBOX_DEFAULT_IMAGES, **_parse_csv_key_value_pairs(v, "OPENSANDBOX_IMAGES")}

    @field_validator("agentic_labs_licences", mode="before")
    @classmethod
    def _parse_agentic_labs_licences(cls, v: object) -> dict[str, str]:
        # "ENV_VAR:product name": split on the first ":" only, a malformed entry dropped and logged.
        return _parse_csv_key_value_pairs(v, "AGENTIC_LABS_LICENCES")

    @field_validator("agentic_labs_images", mode="before")
    @classmethod
    def _parse_agentic_labs_images(cls, v: object) -> dict[str, str]:
        # "key:image": split on the first ":" only, so the image keeps its registry port and tag.
        return _parse_csv_key_value_pairs(v, "AGENTIC_LABS_IMAGES")

    @field_validator("a2a_client_dev_trusted_origins", mode="before")
    @classmethod
    def _parse_a2a_client_dev_trusted_origins(cls, v: object) -> list[str]:
        # Normalised to exactly the form ``destinations.origin_of`` produces,
        # so matching is a set membership test rather than a comparison of two
        # spellings of one origin. A trailing slash, an upper-case host or a
        # path all reduce to the same entry.
        #
        # A malformed entry RAISES rather than being skipped: this setting
        # widens a security boundary, and an operator who fat-fingers it must
        # not be told everything is fine while the origin they meant to trust
        # is silently absent and delegation fails for a reason that looks
        # unrelated.
        return parse_trusted_origins(v, "A2A_CLIENT_DEV_TRUSTED_ORIGINS", "http://localhost:8200")

    @field_validator("oauth_token_dev_trusted_origins", mode="before")
    @classmethod
    def _parse_oauth_token_dev_trusted_origins(cls, v: object) -> list[str]:
        # Same normalisation and the same refusal of a malformed entry as the
        # A2A allow-list above: it widens the token endpoint guard.
        return parse_trusted_origins(v, "OAUTH_TOKEN_DEV_TRUSTED_ORIGINS", "http://localhost:8080")

    @field_validator("secret_vault_trusted_origins", mode="before")
    @classmethod
    def _parse_secret_vault_trusted_origins(cls, v: object) -> list[str]:
        # Same normalisation and refusal of a malformed entry: it widens the
        # destination policy of the vaults connected in the UI.
        return parse_trusted_origins(v, "SECRET_VAULT_TRUSTED_ORIGINS", "http://127.0.0.1:8200")

    @field_validator("web_fetch_trusted_ca_certificates", mode="before")
    @classmethod
    def _parse_web_fetch_trusted_ca_certificates(cls, v: object) -> str:
        # A malformed value refuses the boot: it widens whom the fetches trust.
        return parse_trusted_certificates(v, "WEB_FETCH_TRUSTED_CA_CERTIFICATES")

    @field_validator("mcp_server_trusted_origins", mode="before")
    @classmethod
    def _parse_mcp_server_trusted_origins(cls, v: object) -> list[str]:
        # Same normalisation and refusal of a malformed entry: it widens the
        # MCP server URL policy.
        return parse_trusted_origins(v, "MCP_SERVER_TRUSTED_ORIGINS", "http://mcp.internal:8080")

    @field_validator("mcp_server_url_policy")
    @classmethod
    def _validate_mcp_server_url_policy(cls, v: str) -> str:
        # A typo must not silently mean "permissive" for a policy someone
        # tightened on purpose. Blank is unset (docker-compose forwards
        # ``${MCP_SERVER_URL_POLICY:-}``), as it is for the field's lock.
        normalized = (v or "").strip().lower() or "permissive"
        if normalized not in {"permissive", "private_networks", "public_only"}:
            raise ValueError(
                "MCP_SERVER_URL_POLICY must be one of permissive|private_networks|public_only, "
                f"got {v!r}"
            )
        return normalized

    @field_validator("prompt_guard_scanned_tools", mode="before")
    @classmethod
    def _parse_prompt_guard_scanned_tools(cls, v: object) -> list[str]:
        return parse_scanned_tools(v)

    @field_validator("test_only_insecure_transport_peers", mode="before")
    @classmethod
    def _parse_test_only_insecure_transport_peers(cls, v: object) -> list[str]:
        # Normalised to network form so ``is_secure_transport`` matches by
        # membership. A malformed entry RAISES: this widens a security
        # boundary, and a typo must not pass for a working exemption. So does
        # a network that is not private: a test stack's bridge network always
        # is, and ``0.0.0.0/0`` would switch the guarantee off for everyone.
        import ipaddress

        if v is None or v == "":
            return []
        raw = v.split(",") if isinstance(v, str) else list(v)  # type: ignore[arg-type]
        parsed: list[str] = []
        for item in raw:
            entry = str(item).strip()
            if not entry:
                continue
            try:
                network = ipaddress.ip_network(entry, strict=False)
            except ValueError as exc:
                raise ValueError(
                    f"TEST_ONLY_INSECURE_TRANSPORT_PEERS entry {entry!r} is not an "
                    "IP address or CIDR, e.g. 172.16.0.0/12"
                ) from exc
            if not network.is_private:
                raise ValueError(
                    f"TEST_ONLY_INSECURE_TRANSPORT_PEERS entry {entry!r} is not a private "
                    "network: it names the Docker bridge network an end-to-end suite "
                    "reaches the platform from, e.g. 172.16.0.0/12"
                )
            parsed.append(str(network))
        return list(dict.fromkeys(parsed))

    @model_validator(mode="after")
    def _test_only_peers_need_the_end_to_end_stack(self) -> Settings:
        # Refused rather than warned about: the peers extend the loopback
        # exemption of the end-to-end stack, the one deployment that also sets
        # A2A_ALLOW_INSECURE_LOOPBACK, and an operator whose partners are
        # refused behind a proxy must be sent to the real fix instead.
        if self.test_only_insecure_transport_peers and not self.a2a_allow_insecure_loopback:
            raise ValueError(
                "TEST_ONLY_INSECURE_TRANSPORT_PEERS is for the end-to-end test stack only "
                "and is refused without A2A_ALLOW_INSECURE_LOOPBACK=true. Behind a "
                "TLS-terminating proxy, set FORWARDED_ALLOW_IPS to the proxy's address "
                "instead: the A2A surface then confirms HTTPS from the proxy."
            )
        return self

    @field_validator("admin_token")
    @classmethod
    def _validate_admin_token(cls, v: str) -> str:
        v = v.strip()
        if v and (not v.startswith("fcp-") or len(v) < 32):
            import secrets

            example = f"fcp-{secrets.token_hex(24)}"
            raise ValueError(
                f"admin_token must start with 'fcp-' and be at least 32 characters long for security. "
                f"Example of a valid token you can use: {example}"
            )
        return v

    @field_validator("admin_password")
    @classmethod
    def _validate_admin_password(cls, v: str) -> str:
        # Only bcrypt's own limit (security.BCRYPT_MAX_BYTES, pinned by a test):
        # the admin password is hashed at startup, and bcrypt 5 raises on more.
        try:
            size = len(v.strip().encode("utf-8"))
        except UnicodeEncodeError:
            raise ValueError("admin_password is not valid UTF-8 text") from None
        if size > 72:
            raise ValueError(
                f"admin_password is {size} bytes long; bcrypt stores at most 72 "
                "(fewer characters when they are not plain ASCII)"
            )
        return v

    @model_validator(mode="after")
    def _apply_mode_defaults(self) -> Settings:
        """Resolve None security flags + clamp KB-sync timeout."""
        if self.disable_code_interpreter is None:
            self.disable_code_interpreter = not self.is_copilot_like
        if self.disable_custom_tools is None:
            self.disable_custom_tools = not self.is_copilot_like
        if self.disable_custom_variables is None:
            self.disable_custom_variables = not self.is_copilot_like

        if self.prometheus_enabled and (
            not self.prometheus_bearer_token or len(self.prometheus_bearer_token) < _PROMETHEUS_TOKEN_MIN_LENGTH
        ):
            import logging

            logging.warning(
                f"PROMETHEUS_BEARER_TOKEN is missing or under {_PROMETHEUS_TOKEN_MIN_LENGTH} characters. "
                "Prometheus metrics endpoint will be disabled for security."
            )
            self.prometheus_enabled = False

        # Per-run sub-agent concurrency cap must be ≤ the platform-wide
        # cap — otherwise a single orchestrator run could monopolise
        # every platform-wide slot and defeat the per-run guard.
        if self.max_concurrent_sub_agent_tasks_per_run > self.max_concurrent_sub_agent_tasks:
            raise ValueError(
                f"max_concurrent_sub_agent_tasks_per_run "
                f"({self.max_concurrent_sub_agent_tasks_per_run}) must be "
                f"<= max_concurrent_sub_agent_tasks "
                f"({self.max_concurrent_sub_agent_tasks})"
            )

        # Enforce (as a loud warning, not a hard failure) the documented
        # sizing rule above: max_concurrent_runs +
        # max_concurrent_sub_agent_tasks <= 0.6 x (pool_size + overflow).
        # The reconciler sizes its execution semaphore from the sum, and
        # every slot holds a pooled connection through its setup phase —
        # an oversized sum is a self-inflicted "Pool near exhaustion".
        # Warning rather than raising: existing
        # deployments must keep booting; operators get an actionable
        # message to retune.
        _pool_budget = 0.6 * (self.db_pool_size + self.db_max_overflow)
        _slot_sum = self.max_concurrent_runs + self.max_concurrent_sub_agent_tasks
        if _slot_sum > _pool_budget:
            import logging

            logging.getLogger("app.pool").warning(
                "MAX_CONCURRENT_RUNS + MAX_CONCURRENT_SUB_AGENT_TASKS (%d) "
                "exceeds 60%% of the DB pool (%.0f of %d connections). "
                "Concurrent executions can exhaust the pool during their "
                "setup phases - lower the caps or raise "
                "DB_POOL_SIZE/DB_MAX_OVERFLOW.",
                _slot_sum,
                _pool_budget,
                self.db_pool_size + self.db_max_overflow,
            )

        # Validate redis_mode and its required companions.
        mode = (self.redis_mode or "standalone").strip().lower()
        if mode not in {"standalone", "sentinel"}:
            raise ValueError(f"REDIS_MODE must be one of standalone|sentinel (got {self.redis_mode!r})")
        self.redis_mode = mode
        if mode == "sentinel" and not self.redis_sentinel_hosts.strip():
            raise ValueError('REDIS_MODE=sentinel requires REDIS_SENTINEL_HOSTS (comma-separated "host:port" list).')
        # The prefix starts the glob patterns of SCAN, KEYS and pattern
        # subscriptions, and the operator's ACL rules: no glob character,
        # no space.
        if any(c in "*?[]\\" or c.isspace() or not c.isprintable() for c in self.redis_key_prefix):
            raise ValueError(
                f"REDIS_KEY_PREFIX must not contain spaces or the characters * ? [ ] \\ (got {self.redis_key_prefix!r})"
            )

        # Clamp KB-sync timeout to the documented ``[1, 86_400]`` window
        # at settings-load time. The runtime helper in
        # ``kb_sync_scheduler`` re-applies the same cap because tests
        # monkeypatch this attribute directly (bypassing the validator),
        # but enforcing it here means the documented "(capped at 86400)"
        # contract is honoured by the model itself — not only by the
        # single downstream consumer — and a misconfigured env var fails
        # closed at startup rather than silently producing an out-of-range
        # PostgreSQL ``statement_timeout``.
        self.kb_sync_timeout_secs = max(1, min(int(self.kb_sync_timeout_secs), 86_400))

        # Stale-detection window is decoupled from the per-sync timeout:
        # clamp it to ``[60, 86_400]`` so a misconfigured
        # env var can't make the sweeper reset every in-flight sync (too
        # low) or never recover an orphaned one (too high). Keep the
        # heartbeat cadence strictly below the window so a healthy sync
        # heartbeats at least a couple of times before it could be judged
        # stale, even if the operator tunes the window down.
        self.kb_sync_stale_after_secs = max(60, min(int(self.kb_sync_stale_after_secs), 86_400))
        self.kb_sync_heartbeat_interval_secs = max(
            5,
            min(
                int(self.kb_sync_heartbeat_interval_secs),
                max(5, self.kb_sync_stale_after_secs // 2),
            ),
        )

        # Parse-offload executor + loop-watchdog bounds.
        self.parse_thread_pool_max_workers = max(1, min(int(self.parse_thread_pool_max_workers), 8))
        self.event_loop_stall_threshold_seconds = max(0.5, min(float(self.event_loop_stall_threshold_seconds), 60.0))

        # Model-prices git sync bounds (defence in depth for env typos).
        self.model_prices_git_sync_interval_minutes = max(
            1, min(int(self.model_prices_git_sync_interval_minutes), 10_080)
        )
        self.model_prices_git_sync_timeout_secs = max(5, min(int(self.model_prices_git_sync_timeout_secs), 600))
        self.tracing_queue_maxsize = max(1, min(int(self.tracing_queue_maxsize), 10_000))
        self.tracing_overflow_ttl_seconds = max(60, min(int(self.tracing_overflow_ttl_seconds), 604_800))
        self.tracing_overflow_max_entries = max(100, min(int(self.tracing_overflow_max_entries), 100_000))
        self.tracing_db_pool_size = max(1, min(int(self.tracing_db_pool_size), 20))
        self.tracing_db_max_overflow = max(0, min(int(self.tracing_db_max_overflow), 20))
        tracing_store_provider = (self.tracing_store_provider or "postgres").strip().lower()
        if tracing_store_provider not in {"postgres", "clickhouse"}:
            raise ValueError(
                f"TRACING_STORE_PROVIDER must be one of postgres|clickhouse (got {self.tracing_store_provider!r})"
            )
        self.tracing_store_provider = tracing_store_provider
        self.clickhouse_port = max(1, min(int(self.clickhouse_port), 65_535))
        self.clickhouse_connect_timeout_seconds = max(
            1, min(int(self.clickhouse_connect_timeout_seconds), 120)
        )
        # 0 disables offloading (always inline); otherwise keep the threshold
        # off the pathological ends — a few KB would offload almost every span,
        # and beyond 256 MiB the payload would not survive a JSONB round-trip
        # if the offload later failed and fell back to inline storage.
        offload_threshold = int(self.trace_payload_offload_threshold_bytes)
        self.trace_payload_offload_threshold_bytes = (
            0 if offload_threshold <= 0 else max(64 * 1024, min(offload_threshold, 268_435_456))
        )
        # The inline fallback ceiling can never sit below the offload threshold,
        # or an oversized payload would be refused inline *and* never attempted
        # as an offload — a silent loss, which is the bug this whole path exists
        # to eliminate.
        self.trace_payload_hard_max_bytes = max(
            self.trace_payload_offload_threshold_bytes,
            min(int(self.trace_payload_hard_max_bytes), 268_435_456),
        )

        # Auth / TTL clamps — fail closed at startup with sane caps so a
        # misconfigured env var doesn't surface later as a 400 from the
        # invitation endpoint, a Redis ``EX`` error from the password-reset
        # endpoint, or an unenforceably-low minimum password length.  The
        # invitation / reset caps mirror the per-request ``1..720`` (30
        # day) range already validated in ``invitations.py`` so the
        # default and any operator-tuned value stay inside the same
        # envelope the API contract advertises.  ``min_password_length``
        # is capped at 1024 — well above any sensible policy but low
        # enough that a fat-fingered ``MIN_PASSWORD_LENGTH=10000`` cannot
        # render the platform un-registrable.
        self.min_password_length = max(1, min(int(self.min_password_length), 1024))
        self.invitation_expire_hours = max(1, min(int(self.invitation_expire_hours), 720))
        self.password_reset_expire_hours = max(1, min(int(self.password_reset_expire_hours), 720))
        # A year at most: the activity of a session is kept in Redis as long
        # as the session can last (``core/sessions.py``).
        self.jwt_expire_minutes = max(1, min(int(self.jwt_expire_minutes), SESSION_MINUTES_MAX))
        idle = min(int(self.session_idle_timeout_minutes), SESSION_MINUTES_MAX)
        self.session_idle_timeout_minutes = max(SESSION_IDLE_MINUTES_MIN, idle) if idle > 0 else 0
        self.api_key_max_lifetime_days = max(0, min(int(self.api_key_max_lifetime_days), 3650))

        return self

    @property
    def is_copilot_like(self) -> bool:
        """The licence-required agentic platform: copilot or dev.

        Say this rather than ``not is_xtm_one``. That idiom meant "copilot"
        only while there were three modes; ``ai_disabled`` made it true for a
        deployment that must run *less* than xtm_one, which would have mounted
        the LLM proxy and enabled the code interpreter on the one mode whose
        purpose is that neither exists.
        """
        return self.platform_mode in COPILOT_LIKE_MODES

    @property
    def is_xtm_one(self) -> bool:
        """Which *product* this is, not whether AI runs — ask ``is_ai_enabled()``
        for that. ``ai_disabled`` is an XTM One platform with AI switched off.

        Excluding it here left the mode in *neither* camp, so all 35 positive
        readers fell to an ``else`` meaning "Copilot" and an unlicensed boot
        answered ``license_required=True``, turning the whole UI into the
        licence wall. With ``is_copilot_like`` this partitions
        ``PLATFORM_MODE`` exhaustively; keep it that way. Seeding reads
        the ``platform_mode`` each seeder is *passed*, not this, so nothing is
        provisioned by saying yes.
        """
        return self.platform_mode in ("xtm_one", "ai_disabled")

    @property
    def provider_models_admin_curated(self) -> bool:
        """Is ``LLM_PROVIDER_{P}_MODELS`` a baseline the admin may curate?

        The resolved answer for both ways of asking for it:
        ``LLM_PROVIDER_MODELS_EDITABLE`` (catalogs only) and
        ``BYOLLM_ACTIVATED`` (the whole LLM configuration). Read this, never
        ``llm_provider_models_editable`` on its own — the boot seed
        (``main._seed_llm_providers_from_env``) and the Env badge
        (``llm.env_models.models_env_locked``) must agree on one answer.
        """
        return self.llm_provider_models_editable or self.byollm_activated

    @property
    def provider_management_env_only(self) -> bool:
        """Is UI/API provider management blocked (add / edit / rename / delete)?

        ``MODELS_ENV_PROVIDING_ONLY`` says "providers are the env vars" and
        ``BYOLLM_ACTIVATED`` says "the admin owns them": the two contradict
        each other, and BYOLLM — the narrower, more recent opt-in — wins.
        ``main._log_llm_ownership_policy`` logs a WARNING naming both when a
        deployment sets them together, so the ignored flag is never silent.
        """
        return self.models_env_providing_only and not self.byollm_activated

    @property
    def code_interpreter_allowed_modules(self) -> frozenset[str]:
        """``CODE_INTERPRETER_EXTRA_ALLOWED_MODULES`` parsed (``parse_allowed_modules``).

        The environment value only: the sandbox reads the effective list,
        which Settings > Access and security can also set, through
        ``security_option("code_interpreter_extra_allowed_modules")``.
        """
        return parse_allowed_modules(self.code_interpreter_extra_allowed_modules)

    @property
    def code_interpreter_allowed_packages(self) -> frozenset[str]:
        """``CODE_INTERPRETER_PACKAGE_ALLOWLIST`` as PEP 503 normalised distribution names."""
        return frozenset(
            re.sub(r"[-_.]+", "-", name.strip()).lower()
            for name in self.code_interpreter_package_allowlist.split(",")
            if name.strip()
        )

    @property
    def custom_tool_allowed_modules(self) -> frozenset[str]:
        """``CUSTOM_TOOL_EXTRA_ALLOWED_MODULES`` parsed (``parse_allowed_modules``).

        The environment value only: the sandbox reads the effective list,
        which Settings > Access and security can also set, through
        ``security_option("custom_tool_extra_allowed_modules")``.
        """
        return parse_allowed_modules(self.custom_tool_extra_allowed_modules)

    @property
    def telemetry_tags_normalized(self) -> str:
        """Normalized telemetry tags as a canonical comma-separated string.

        Split on commas, trim, lowercase, drop empties, dedupe, sort,
        re-join — the shared Filigran contract (same normalization in OpenCTI
        and OpenAEV) so an identical tag set always produces the identical
        string in the analytics warehouse. Empty string when no tags are
        configured.
        """
        tags = {t.strip().lower() for t in (self.telemetry_tags or "").split(",")}
        return ",".join(sorted(t for t in tags if t))

    @property
    def opensandbox_network_allow_list(self) -> frozenset[str]:
        """Parse ``opensandbox_network_allow`` (CSV) into a frozenset of targets.

        Used to build the egress ``NetworkRule`` list when creating an
        OpenSandbox container.  Whitespace is trimmed and empty entries are
        dropped; an empty/unset value yields an empty frozenset (all egress denied).
        """
        raw = self.opensandbox_network_allow or ""
        return frozenset(t.strip() for t in raw.split(",") if t.strip())

    @property
    def enabled_feature_flags(self) -> frozenset[str]:
        """Parse ``enabled_features`` (CSV) into a fail-closed set.

        Whitespace is trimmed and empty entries are dropped; an empty/unset
        value yields an empty set (no flags enabled — fail-closed default).
        Unknown flag ids (typo, or a flag that only exists on another
        branch) are dropped so a stale ``.env`` can never mark an unknown
        flag as active — only ids in ``KNOWN_FEATURE_FLAGS`` (plus the
        ``"*"`` wildcard) survive. The validator ``_validate_enabled_features``
        already logged the unknown ids at startup.
        """
        raw = self.enabled_features or ""
        tokens = frozenset(t.strip() for t in raw.split(",") if t.strip())
        return tokens & (KNOWN_FEATURE_FLAGS | {FEATURE_FLAG_ALL})

    @property
    def terms_of_use_current_version(self) -> str:
        """The published ToU version, whitespace-trimmed ("" when unset)."""
        return (self.terms_of_use_version or "").strip()

    @property
    def terms_of_use_active(self) -> bool:
        """Whether the ToU acceptance gate can fire at all on this instance.

        Both conditions are configuration-only (no DB read), so this is the
        cheapest short-circuit in ``deps.enforce_terms_of_use``. A blank
        ``TERMS_OF_USE_VERSION`` deactivates the gate rather than gating every
        user behind an unmatchable version — an operator who wants no gate has
        ``TERMS_OF_USE_ENABLED=false``, and an operator who blanked the version
        by accident gets a warning at startup (``_warn_blank_terms_version``)
        instead of a locked-out platform.
        """
        return self.terms_of_use_enabled and bool(self.terms_of_use_current_version)

    @property
    def terms_of_use_exempt_email_set(self) -> frozenset[str]:
        """``TERMS_OF_USE_EXEMPT_EMAILS`` parsed: trimmed, lowercased, blanks dropped."""
        raw = self.terms_of_use_exempt_emails or ""
        return frozenset(e for t in raw.split(",") if (e := t.strip().lower()))

    def terms_of_use_exempt(self, email: str | None) -> bool:
        """Whether the Terms of Use gate skips the user with this email.

        Exact match only, never a domain: one gate decision, read by the REST
        gate (the web chat routes included) and ``/auth/me`` alike.
        """
        if not email:
            return False
        return email.strip().lower() in self.terms_of_use_exempt_email_set


settings = Settings()

if settings.terms_of_use_enabled and not settings.terms_of_use_current_version:
    logging.getLogger(__name__).warning(
        "TERMS_OF_USE_ENABLED is true but TERMS_OF_USE_VERSION is empty — "
        "the Terms of Use acceptance gate is DISABLED. Set a version "
        "identifier (e.g. '2026-07-14') to enable it, or set "
        "TERMS_OF_USE_ENABLED=false to silence this warning."
    )
