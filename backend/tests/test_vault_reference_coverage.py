"""Tripwire: every encrypted column either accepts a vault reference or says why it does not.

A secret an administrator or a user types into the platform can come from a
vault instead: its column is listed in ``app.services.vault.REFERENCE_COLUMNS``,
its write paths accept a ``vault://`` reference and its readers resolve it
(``tests/test_vault_secret_readers.py``). An encrypted column (``*_enc``) of a
model that is not listed there is listed below with the reason a reference
does not belong in it. A new encrypted column fails this test until it is one
or the other.
"""

from __future__ import annotations

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.core.database import Base
from app.services.vault import ENTITY_KINDS, REFERENCE_COLUMNS

#: Columns that never hold a secret a person types: an identifier, what the platform obtains or generates itself.
NOT_A_TYPED_SECRET: dict[tuple[str, str], str] = {
    ("integrations", "client_id_enc"): "an OAuth client id: an identifier, not a secret",
    ("mcp_servers", "client_id_enc"): "an OAuth client id: an identifier, not a secret",
    ("channel_bots", "client_id_enc"): "an app id: an identifier, not a secret",
    ("oauth_defaults", "client_id_enc"): "an OAuth client id: an identifier, not a secret",
    ("sso_config", "oidc_client_id_enc"): "the OIDC client id: an identifier, not a secret",
    ("integrations", "access_token_enc"): "written by the platform from an OAuth flow, never typed",
    ("integrations", "refresh_token_enc"): "written by the platform from an OAuth flow, never typed",
    ("mcp_servers", "access_token_enc"): "written by the platform from an OAuth flow, never typed",
    ("mcp_servers", "refresh_token_enc"): "written by the platform from an OAuth flow, never typed",
    ("integration_user_credentials", "access_token_enc"): "written by the platform from a user's OAuth flow",
    ("integration_user_credentials", "refresh_token_enc"): "written by the platform from a user's OAuth flow",
    ("mcp_server_user_credentials", "access_token_enc"): "written by the platform from a user's OAuth flow",
    ("mcp_server_user_credentials", "refresh_token_enc"): "written by the platform from a user's OAuth flow",
    ("browser_profiles", "storage_state_enc"): "the cookies a browser session captured, never typed",
    ("agent_account_subscriptions", "client_state_enc"): "the platform's own client state for a subscription",
    ("xtm_hub_registrations", "token_enc"): "the token XTM Hub issued at registration",
    ("presentation_public_links", "token_enc"): "a public link token the platform generates",
    ("video_public_links", "token_enc"): "a public link token the platform generates",
    ("custom_dashboard_public_links", "token_enc"): "a public link token the platform generates",
    ("ctem_webhook_sources", "secret_enc"): "an HMAC signing secret the platform generates, shown once",
    ("platform_registrations", "enterprise_license_pem_enc"): "a license document, not a credential",
    ("secret_vault_providers", "credentials_enc"): "what opens a vault: it cannot itself be read from one",
}

#: Typed secrets whose reference support is pending, each with what it needs.
#: This list only shrinks: a new column never joins it.
PENDING: dict[tuple[str, str], str] = {
    ("integrations", "extra_config_enc"): "a JSON document whose secret entries are read at about 60 call sites",
    ("oauth_defaults", "extra_config_enc"): "a JSON document whose secret entries the webhook handlers read",
    ("integration_user_credentials", "account_config_enc"): "a per-user JSON document of account settings",
}


def _encrypted_columns() -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for table in Base.metadata.sorted_tables:
        for column in table.columns:
            if column.name.endswith("_enc"):
                found.add((table.name, column.name))
    return found


def _accepting() -> set[tuple[str, str]]:
    return {(table, column) for table, columns in REFERENCE_COLUMNS.items() for column in columns}


def test_every_encrypted_column_accepts_a_reference_or_says_why_not():
    unclassified = sorted(_encrypted_columns() - _accepting() - set(NOT_A_TYPED_SECRET) - set(PENDING))
    assert unclassified == [], (
        "These encrypted columns neither accept a vault reference nor say why they do not: "
        f"{unclassified}. List the column in app.services.vault.REFERENCE_COLUMNS (and accept and resolve "
        "references in its write paths and readers), or add it to NOT_A_TYPED_SECRET in "
        "tests/test_vault_reference_coverage.py with the reason."
    )


def test_the_lists_name_real_columns_and_never_overlap():
    columns = _encrypted_columns()
    stale = sorted((_accepting() | set(NOT_A_TYPED_SECRET) | set(PENDING)) - columns)
    assert stale == [], f"listed columns that no model declares any more, remove them: {stale}"
    assert not _accepting() & set(NOT_A_TYPED_SECRET)
    assert not _accepting() & set(PENDING)
    assert not set(NOT_A_TYPED_SECRET) & set(PENDING)


def test_every_table_accepting_references_has_an_entity_kind():
    assert set(REFERENCE_COLUMNS) <= set(ENTITY_KINDS)
