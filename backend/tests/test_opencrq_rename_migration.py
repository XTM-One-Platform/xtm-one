"""The rename is only finished when every stored spelling moves with it.

Renaming a product renames three different kinds of stored value, and they
are easy to miss one at a time because each hides a different way:

* a **provider key** (``integrations.provider``, the ``integration_bindings``
  maps) — the integration stops resolving, quietly;
* a **tool name**, which carries the provider as an infix
  (``update_opengrc_control_settings``) — and in ``agents.tool_deny_list``
  this one fails **open**: ``apply_tool_deny_list`` matches literal names, so
  a stale entry stops blocking the tool it was added to block;
* a **prefixed key** (``integration_element_bindings`` is keyed
  ``"{provider}:{element_type}"``) — whole-key equality never matches it.

The completeness tests below are the point of this file. Three rounds of
review each found another column the migration did not cover, and
every one of them was a column that already existed — not a new one. So these
assert against the models: if someone adds a provider-keyed or tool-name
column later, or this revision is edited to drop one, the test names it.

The revision's source is read rather than imported, and the expected values
are spelled as literals: a migration must keep meaning what it meant on the
day it ran, and importing today's constants would let a later rename silently
rewrite the history of a database that has already been upgraded.
"""

from __future__ import annotations

import pathlib

import pytest

REVISION = (
    pathlib.Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "o1crq2opencrq1_rename_opengrc_to_opencrq.py"
)


@pytest.fixture(scope="module")
def source() -> str:
    return REVISION.read_text(encoding="utf-8")


class TestItRenamesTheRightThing:
    def test_it_moves_opengrc_to_opencrq(self, source):
        assert '_OLD = "opengrc"' in source
        assert '_NEW = "opencrq"' in source

    def test_it_moves_the_builtin_assistant_slug(self, source):
        assert '_OLD_SLUG = "opengrc-assistant"' in source
        assert '_NEW_SLUG = "opencrq-assistant"' in source

    def test_the_tool_infix_is_anchored_to_the_provider_segment(self, source):
        # ``_opengrc_`` rather than ``opengrc``: the underscores keep the
        # match on the provider segment of a tool name.
        assert '_TOOL_NAME_INFIX_OLD = f"_{_OLD}_"' in source
        assert '_TOOL_NAME_INFIX_NEW = f"_{_NEW}_"' in source


class TestEveryProviderKeyedColumnIsCovered:
    """Each of these stores a provider key that the rename invalidates."""

    @pytest.mark.parametrize(
        ("table", "column"),
        [
            ("integrations", "provider"),
            ("verticals", "platform_identifier"),
            ("platform_registrations", "platform_identifier"),
            ("platform_intent_declarations", "platform_identifier"),
            ("agents", "integration_bindings"),
            ("agents", "integration_template_bindings"),
            ("agents", "integration_allowed_tools"),
            ("agents", "integration_element_bindings"),
            ("assignments", "integration_bindings"),
            ("assignments", "integration_allowed_tools"),
            ("assignments", "integration_element_bindings"),
            ("general_assistant_config", "integration_bindings"),
            ("users", "ga_integration_bindings"),
            ("platform_settings", "disabled_integration_providers"),
        ],
    )
    def test_column_is_named_in_the_migration(self, source, table, column):
        # Some columns sit in a mapping tuple, others in a dedicated helper
        # that names them the same way minus the parentheses.
        assert f'"{table}", "{column}"' in source, (
            f"{table}.{column} stores a provider key but this revision never "
            f"rewrites it — the binding would survive the rename pointing at "
            f"a provider that no longer exists"
        )

    def test_the_model_columns_still_exist(self):
        """Guards the list above from rotting into a tautology."""
        from app.models.agent import Agent
        from app.models.assignment import Assignment
        from app.models.general_assistant_config import GeneralAssistantConfig
        from app.models.user import User

        for model, column in (
            (Agent, "integration_bindings"),
            (Agent, "integration_allowed_tools"),
            (Agent, "integration_element_bindings"),
            (Agent, "tool_deny_list"),
            (Agent, "mcp_allowed_tools"),
            (Assignment, "integration_bindings"),
            (Assignment, "integration_allowed_tools"),
            (Assignment, "integration_element_bindings"),
            (GeneralAssistantConfig, "integration_bindings"),
            (User, "ga_integration_bindings"),
        ):
            assert hasattr(model, column), (
                f"{model.__name__}.{column} no longer exists — the migration "
                f"still rewrites it, so this list needs updating"
            )


class TestEveryToolNameColumnIsCovered:
    """A stale tool name in a DENY list fails open. That is the one to keep."""

    def test_the_deny_list_is_rewritten(self, source):
        assert '_TOOL_NAME_ARRAY: tuple[tuple[str, str], ...] = (("agents", "tool_deny_list"),)' in source, (
            "agents.tool_deny_list stores literal tool names; leaving them "
            "stale silently unblocks tools an administrator denied"
        )

    @pytest.mark.parametrize(
        ("table", "column"),
        [
            ("agents", "integration_allowed_tools"),
            ("agents", "mcp_allowed_tools"),
            ("assignments", "integration_allowed_tools"),
        ],
    )
    def test_allow_list_values_are_rewritten(self, source, table, column):
        block = source.split("_TOOL_NAME_OBJECT_VALUES")[1].split("\n)")[0]
        assert f'"{table}", "{column}"' in block, (
            f"{table}.{column} maps to arrays of tool names; a surviving "
            f"pre-rename name matches nothing and strips every tool"
        )

    def test_prefixed_keys_are_rewritten(self, source):
        block = source.split("_PREFIXED_KEY_OBJECT")[1].split("\n)")[0]
        for table in ("agents", "assignments"):
            assert f'"{table}", "integration_element_bindings"' in block


class TestTheConflictPathCannotLoseData:
    def test_it_refuses_rather_than_skipping(self, source):
        assert "raise RuntimeError(" in source, (
            "a skipped rename leaves the pre-rename row for the seeder to "
            "delete, cascading to its assignments"
        )

    def test_the_refusal_names_the_row_and_the_remedy(self, source):
        assert "Cannot rename" in source
        assert "re-run the upgrade" in source

    def test_it_only_deletes_a_duplicate_that_carries_no_history(self, source):
        # The ordering argument (migrations run before seeding) is not
        # something the migration can verify, so it checks the property that
        # actually matters instead — "carries no history", which is not the
        # same as "referenced by nothing" (see the seeder-owned test below).
        assert "_row_has_dependents(" in source

    def test_dependents_are_read_from_the_catalog_not_a_hand_written_list(
        self, source
    ):
        """``agents`` is the target of ~20 foreign keys across the schema.

        Enumerating them by hand is how the previous three rounds of this
        review each found another one missing, so the check asks Postgres.
        """
        assert "FROM pg_constraint c" in source
        assert "c.confrelid = to_regclass(:t)" in source
        # A hand-written table list would reintroduce exactly the gap this
        # replaced: assignments covered, conversations and ~17 others not.
        assert "SELECT 1 FROM assignments WHERE agent_id" not in source

    def test_catalog_read_column_names_are_quoted(self, source):
        # ``regclass`` output quotes the table name itself; ``attname`` is the
        # raw name, and a mixed-case or reserved-word column would turn the
        # probe into invalid SQL.
        assert "quote_ident(a.attname)" in source

    def test_a_seeded_builtins_own_rows_do_not_count_as_history(self, source):
        """Otherwise the guard refuses in the only case it can ever fire.

        The duplicate branch is unreachable unless seeding already ran, and
        a seeded built-in is referenced the moment it exists — the seeder
        writes an ``intent_bindings`` row per supported intent, and
        ``opencrq-assistant`` declares several. Counting those as history
        would turn the automatic resolution into a failed deploy.
        """
        assert '("intent_bindings", "agent_id"): "is_builtin"' in source

    def test_the_seeder_exclusion_is_narrowed_to_the_seeders_own_rows(
        self, source
    ):
        # Ignoring the whole table would let an operator-added binding be
        # cascaded away; only rows the seeder marked are excluded. A NULL
        # marker must count as history: a bare ``NOT`` would drop it.
        assert 'f" AND NOT COALESCE({seeder_owned}, false)"' in source
        assert 'f" AND NOT {seeder_owned}"' not in source

    def test_only_foreign_keys_onto_the_primary_key_are_probed(self, source):
        # A single-column FK may target any UNIQUE column. None does today,
        # but one onto agents.web_token would have the probe comparing a
        # uuid against text and erroring instead of answering.
        assert "target.attnum = c.confkey[1]) = 'id'" in source

    def test_downgrade_shares_the_same_resolution(self, source):
        # Not a second copy of the logic that could drift out of step.
        assert "def downgrade() -> None:\n    _migrate(" in source


class TestItIsSafeToRunTwice:
    @pytest.mark.parametrize(
        "guard",
        [
            "jsonb_typeof({column}::jsonb) = 'object' ",
            "jsonb_typeof({column}::jsonb) = 'array' ",
            "LIKE '%' || :old || '%'",
            # A row no cast can read, even repaired, is left out - never an aborted upgrade.
            'f"AND CASE WHEN CAST(id AS text) = ANY(CAST(:skip AS text[])) THEN false ELSE "',
        ],
    )
    def test_rewrites_are_guarded(self, source, guard):
        assert guard in source

    def test_every_jsonb_rewrite_leaves_out_only_the_rows_it_could_not_repair(self, source):
        assert source.count('f"AND CASE WHEN CAST(id AS text) = ANY(CAST(:skip AS text[])) THEN false ELSE "') == 5
        assert source.count('{"old": old, "new": new, "skip": list(skip)}') == 5
        assert "unreadable[table, column] = " not in source
        assert source.count("unreadable[table, column])") == 5

    def test_slugs_are_bound_not_interpolated(self, source):
        # The table and column names are module constants; every value that
        # comes from the rename itself is a bound parameter.
        assert '{"old": old, "new": new}' in source
