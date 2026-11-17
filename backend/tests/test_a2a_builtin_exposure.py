"""Built-in agents are configurable enough to serve a partner, and no more.

Two independent things had to be true before an XTM One built-in could answer
an A2A partner, and each is covered here:

1. **The binding has to be writable.** Exposure follows ``integration_bindings``
   and a built-in is read-only in XTM One mode, so the update endpoint needs a
   carve-out — and one narrow enough that it cannot become a way to rewrite a
   built-in's seeded definition. ``_is_integration_binding_change`` is that
   gate; these tests are what stop it widening. A2A gets no special treatment
   in it: an A2A integration is bound exactly like OpenCTI or GitHub, which is
   the point — binding one IS the exposure opt-in.
2. **The run has to keep its tools.** Every built-in carries its OpenCTI /
   OpenAEV / OpenCRQ tools through ``integration_template_bindings``, resolved
   at run time against whoever is running — and because a non-None
   ``session_integration_ids`` REPLACES the resolved set in ``run_agent_chat``,
   an executor passing only the fixed half would leave a partner an agent with
   no product data at all. It passes none.
"""

import uuid

from app.a2a import providers
from app.api.v1.agents import _is_integration_binding_change


class _Agent:
    def __init__(self, bindings=None, *, template=None, allowed=None, elements=None):
        self.id = uuid.uuid4()
        self.user_id = uuid.uuid4()
        self.is_company_managed = True
        self.is_builtin = True
        self.integration_bindings = bindings
        self.integration_template_bindings = template
        self.integration_allowed_tools = allowed
        self.integration_element_bindings = elements
        self.builtin_tool_ids = []
        self.custom_tool_ids = []
        self.mcp_server_ids = []
        self.knowledge_base_ids = []


class _Body:
    """Stand-in for ``UpdateAgentRequest`` — the gate reads only these."""

    def __init__(self, fields: dict):
        self.model_fields_set = set(fields)
        for key, value in fields.items():
            setattr(self, key, value)


OAUTH = providers.OAUTH_A2A_PROVIDER
ENTRA = providers.ENTRA_A2A_PROVIDER


class TestBuiltinCarveOutAdmitsIntegrationsAndNothingElse:
    def test_binding_any_provider_is_admitted(self):
        """The A2A special case is gone. A built-in reaching the customer's
        OpenCTI is the same kind of decision as one being reachable by a
        partner, and both are the deployment's accounts rather than the agent's
        seeded definition."""
        for provider in ("opencti", "github", OAUTH, ENTRA):
            agent = _Agent({})
            body = _Body({"integration_bindings": {provider: [str(uuid.uuid4())]}})
            assert _is_integration_binding_change(agent, body), provider

    def test_several_providers_may_move_at_once(self):
        agent = _Agent({})
        body = _Body(
            {
                "integration_bindings": {
                    "opencti": [str(uuid.uuid4())],
                    OAUTH: [str(uuid.uuid4())],
                }
            }
        )
        assert _is_integration_binding_change(agent, body)

    def test_unbinding_is_admitted(self):
        """As reachable as binding, or an administrator could expose a built-in
        and never be able to stop."""
        agent = _Agent({OAUTH: [str(uuid.uuid4())]})
        assert _is_integration_binding_change(agent, _Body({"integration_bindings": {}}))

    def test_the_whole_integration_family_is_admitted(self):
        """The Tools tab saves these together — moving a provider from per-user
        to a fixed integration touches two of them in one request — so admitting
        only some would block the common case rather than the unwanted one."""
        agent = _Agent({}, template=["opencti"])
        body = _Body(
            {
                "integration_bindings": {"opencti": [str(uuid.uuid4())]},
                "integration_template_bindings": [],
                "integration_allowed_tools": {"opencti": ["search"]},
            }
        )
        assert _is_integration_binding_change(agent, body)

    def test_a_commit_message_may_ride_along(self):
        """The versioning layer attaches one to any update; it annotates the
        change rather than being one."""
        agent = _Agent({})
        body = _Body(
            {
                "integration_bindings": {"opencti": [str(uuid.uuid4())]},
                "commit_message": "connect opencti",
            }
        )
        assert _is_integration_binding_change(agent, body)

    def test_a_seeded_field_riding_along_refuses_the_whole_request(self):
        """The attack this gate exists for: hide a persona rewrite beside a real
        binding change and the built-in's read-only rule is gone."""
        agent = _Agent({})
        body = _Body(
            {
                "integration_bindings": {"opencti": [str(uuid.uuid4())]},
                "persona": "rewritten",
            }
        )
        assert not _is_integration_binding_change(agent, body)

    def test_any_non_integration_field_refuses(self):
        """Even ones that look harmless. The carve-out is 'this update changes
        integrations and nothing else', not 'this update includes them'."""
        for field, value in (
            ("name", "Renamed"),
            ("builtin_tool_ids", ["web_fetch"]),
            ("mcp_server_ids", [str(uuid.uuid4())]),
            ("llm_model", "gpt-4"),
            ("is_company_managed", False),
        ):
            agent = _Agent({})
            body = _Body({"integration_bindings": {"opencti": ["x"]}, field: value})
            assert not _is_integration_binding_change(agent, body), field

    def test_a_no_op_is_not_a_carve_out(self):
        """Nothing moved, so this falls through to the ordinary refusal rather
        than being waved through on an empty diff."""
        integ = str(uuid.uuid4())
        agent = _Agent({OAUTH: [integ]})
        assert not _is_integration_binding_change(
            agent, _Body({"integration_bindings": {OAUTH: [integ]}})
        )

    def test_omitting_every_binding_field_is_not_a_carve_out(self):
        assert not _is_integration_binding_change(
            _Agent({}), _Body({"commit_message": "x"})
        )

    def test_reordering_the_same_ids_is_not_a_change(self):
        """Compared order-insensitively — the columns are lists, and a client
        rebuilding them may reorder without meaning anything by it."""
        one, two = str(uuid.uuid4()), str(uuid.uuid4())
        agent = _Agent({"opencti": [one, two]})
        body = _Body({"integration_bindings": {"opencti": [two, one]}})
        assert not _is_integration_binding_change(agent, body)

    def test_nested_element_bindings_diff_without_blowing_up(self):
        """``integration_element_bindings`` holds lists of OBJECTS, so the
        normaliser has to recurse rather than assume scalars."""
        agent = _Agent({}, elements={"github:organization": [{"id": "filigran"}]})
        same = _Body(
            {"integration_element_bindings": {"github:organization": [{"id": "filigran"}]}}
        )
        moved = _Body(
            {"integration_element_bindings": {"github:organization": [{"id": "other"}]}}
        )
        assert not _is_integration_binding_change(agent, same)
        assert _is_integration_binding_change(agent, moved)


class TestExternalRunKeepsPerUserBindings:
    """The second half of the built-in case, answered structurally.

    Every XTM One built-in binds its A2A integration fixedly and its OpenCTI /
    OpenAEV / OpenCRQ tools **per user** (``integration_template_bindings``),
    resolved at run time against whoever is running. A non-None
    ``session_integration_ids`` REPLACES that whole resolved set, so the
    executor passing one would leave a partner an agent with no product data.
    It passes none, and ``run_agent_chat`` resolves both halves against the
    run-as user as it does for chat.
    """

    def test_the_executor_passes_no_integration_scope(self):
        import inspect

        from app.a2a.tasks import execute_task_run

        assert "session_integration_ids" not in inspect.getsource(execute_task_run)
