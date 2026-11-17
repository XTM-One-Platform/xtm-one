"""The actor A2A audit entries are attributed to.

``log_audit`` takes a ``User``, but external work has no platform user making
the request. Every entry is anchored on the exposure's run-as user — the
identity the work executes as, and a NOT NULL column — with the partner named
in the email slot. This lived as an anonymous class built inline at four call
sites; the shape ``log_audit`` depends on is now stated once.
"""

import inspect
import uuid

from app.a2a.audit import audit_actor


class TestTheShapeLogAuditDependsOn:
    def test_it_carries_exactly_the_two_attributes_log_audit_reads(self):
        actor = audit_actor(uuid.uuid4(), "client-1")
        # log_audit reads user.id and user.email and nothing else.
        assert actor.id is not None
        assert actor.email == "a2a:client-1"

    def test_it_is_anchored_on_the_run_as_user_not_the_caller(self):
        run_as = uuid.uuid4()
        assert audit_actor(run_as, "client-1").id == run_as

    def test_an_unidentified_caller_is_still_anchored_on_the_exposure(self):
        """A request refused before any credential resolved still audits: the
        exposure is what was addressed."""
        run_as = uuid.uuid4()
        actor = audit_actor(run_as)
        assert actor.id == run_as
        assert actor.email == "a2a:anonymous"

    def test_a_typo_fails_loudly_instead_of_silently_reading_none(self):
        """The point of the dataclass over the old inline ``type(...)``: an
        attribute log_audit might grow is an error here, not a None."""
        actor = audit_actor(uuid.uuid4(), "c")
        try:
            actor.full_name  # noqa: B018
        except AttributeError:
            return
        raise AssertionError("expected AttributeError on an unknown attribute")


class TestNoInlineActorsRemain:
    def test_the_ad_hoc_type_idiom_is_gone_from_the_a2a_surface(self):
        """Four hand-built stand-ins had to stay shape-compatible with
        log_audit by hand; a fifth would have been copied from whichever was
        nearest."""
        from app.a2a import tasks as task_svc
        from app.api.v1 import a2a_server

        for module in (a2a_server, task_svc):
            assert '"_A"' not in inspect.getsource(module), (
                f"{module.__name__} still builds an audit actor inline"
            )
