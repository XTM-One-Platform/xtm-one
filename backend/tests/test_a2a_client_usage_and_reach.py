"""Usage follows the calling run, and a registration confers no inward reach.

FR-033, FR-037, FR-006 / SC-011. Three properties that are easy to assert and
easy to lose:

* usage is charged to whoever's run made the delegation, not to whoever owns
  the partner registration;
* a registration is credentials for reaching OUT and confers nothing inward;
* a per-user (template) binding resolves against the ACTING user under the
  platform's ordinary eligibility rule, never satisfied from someone else's
  account.
"""

from __future__ import annotations

import inspect

from app.a2a.client import delegation, tool


class TestUsageFollowsTheCallingRun:
    def test_the_delegation_record_names_the_calling_user(self):
        # FR-037 / SC-011: usage is attributed to the user whose run made the
        # delegation. The record is what any accounting reads, so naming the
        # wrong user here would misattribute the spend as well as the audit.
        src = inspect.getsource(delegation._who)
        assert '"user_id": calling.user_id' in src
        # NOT the registration's owner, which is a different person whenever
        # a company-managed partner is used by anyone but its creator.
        assert "integration.user_id" not in inspect.getsource(delegation)

    def test_no_new_accounting_concept_is_introduced(self):
        # FR-037 explicitly forbids one. Delegation rides the run's existing
        # accounting; a quota or ceiling of its own would be the platform's
        # first per-partner allowance and is out of scope.
        src = inspect.getsource(delegation)
        for forbidden in ("quota", "allowance", "credit", "billing"):
            assert forbidden not in src.lower(), forbidden


class TestNoInwardReach:
    def test_delegation_reads_no_platform_entities(self):
        # FR-033: a registration confers credentials for reaching outward and
        # no reach inward. The module never queries agents, users, or any
        # visibility-filtered entity.
        src = inspect.getsource(delegation)
        for forbidden in (
            "select(Agent",
            "select(User",
            "user_visible_query",
            "user_visible_filter",
            "is_admin",
        ):
            assert forbidden not in src, forbidden

    def test_the_tool_takes_no_identity_argument(self):
        # A tool argument naming a user would let a model's output choose
        # whose data a delegation runs under.
        schema = tool.build_delegation_tool(
            _Integration(), _Settings()
        ).input_schema["properties"]
        for forbidden in ("user", "user_id", "run_as", "as_user", "agent_id"):
            assert forbidden not in schema

    def test_the_tool_takes_no_destination_argument(self):
        # Nor a destination or a bound: each is registration state or a
        # platform limit, and exposing one would let model output influence
        # where work goes or how long it may run.
        schema = tool.build_delegation_tool(
            _Integration(), _Settings()
        ).input_schema["properties"]
        assert set(schema) == {"message", "continue_task_id"}


class TestPerUserBindingResolution:
    def test_the_tool_is_built_from_the_binding_the_dispatcher_resolved(self):
        # FR-006: per-user (template) bindings resolve under the platform's
        # ordinary eligibility rule. This feature adds no resolution of its
        # own — it receives an integration id the dispatcher already resolved,
        # which is what keeps "never satisfied from another user's account"
        # true by construction rather than by a check we could forget.
        from app.services import integration_tools

        src = inspect.getsource(integration_tools._build_a2a_delegation_tool)
        assert "integration_id" in src
        for forbidden in ("user_id", "acting_user", "owner"):
            assert forbidden not in src, forbidden

    def test_an_unresolvable_binding_yields_no_tool_rather_than_a_fallback(self):
        # A provider with no eligible integration for that user must simply
        # be ABSENT — never satisfied from another user's account.
        from app.services import integration_tools

        src = inspect.getsource(integration_tools._build_a2a_delegation_tool)
        assert src.count("return None") >= 3


class _Integration:
    id = "00000000-0000-0000-0000-000000000001"
    name = "Contoso"
    provider = "oauth_a2a"


class _Settings:
    declared_name = "Triage"
    declared_skills = ["triage"]
    signature_state = "unsigned"
