"""External traffic must be observable separately from internal traffic.

The platform sets no cap on concurrent external runs, so observation is the
only way a partner integration misbehaving is noticed. These pin that the
external series exist, carry the labels that make them separable, and do not
touch any internal counter.
"""

from prometheus_client import REGISTRY

from app.a2a import metrics as a2a_metrics


def _value(name, **labels):
    return REGISTRY.get_sample_value(name, labels) or 0.0


class TestSeriesExist:
    def test_every_external_series_is_registered(self):
        for name in (
            "a2a_tasks_submitted_total",
            "a2a_tasks_completed_total",
            "a2a_requests_refused_total",
            "a2a_exposure_changes_total",
        ):
            assert name in REGISTRY._names_to_collectors


class TestLabelsSeparateExternalTraffic:
    def test_submissions_are_labelled_by_company_managed(self):
        before = _value(
            "a2a_tasks_submitted_total", company_managed="false", duplicate="false"
        )
        a2a_metrics.record_submission(False, duplicate=False)
        after = _value(
            "a2a_tasks_submitted_total", company_managed="false", duplicate="false"
        )
        assert after == before + 1

    def test_company_managed_traffic_is_a_distinct_series(self):
        a2a_metrics.record_submission(True, duplicate=False)
        assert (
            _value(
                "a2a_tasks_submitted_total",
                company_managed="true",
                duplicate="false",
            )
            >= 1
        )

    def test_duplicates_are_distinguishable_from_first_submissions(self):
        a2a_metrics.record_submission(False, duplicate=True)
        assert (
            _value(
                "a2a_tasks_submitted_total", company_managed="false", duplicate="true"
            )
            >= 1
        )

    def test_refusals_are_labelled_by_reason(self):
        before = _value("a2a_requests_refused_total", reason="unauthenticated")
        a2a_metrics.record_refusal("unauthenticated")
        assert (
            _value("a2a_requests_refused_total", reason="unauthenticated")
            == before + 1
        )

    def test_terminal_states_are_counted_separately(self):
        a2a_metrics.record_terminal("completed")
        a2a_metrics.record_terminal("failed")
        assert _value("a2a_tasks_completed_total", state="completed") >= 1
        assert _value("a2a_tasks_completed_total", state="failed") >= 1

    def test_automatic_revocations_carry_their_reason(self):
        a2a_metrics.record_exposure_change("revoke", "tier_change")
        assert (
            _value(
                "a2a_exposure_changes_total", action="revoke", reason="tier_change"
            )
            >= 1
        )

    def test_the_counter_has_a_real_call_site(self):
        """It had none for its whole life, which is worse than being absent:
        a counter that never increments reports 0 forever, so a dashboard
        built on it reads "no exposure ever changed" rather than "not
        instrumented". Both directions are decided in the flush hook, which is
        the single place either is written, and counted when they commit:
        a flush listener runs before COMMIT, and a counter bumped
        there counts changes a rollback then discards."""
        import inspect

        from app.a2a import hooks

        for decider in (hooks._expose, hooks._revoke_live_exposures, hooks._after_flush):
            src = inspect.getsource(decider)
            assert "_queue_exposure_metric" in src
            assert "record_exposure_change" not in src, (
                f"{decider.__name__} counts before the transaction commits"
            )
        assert "record_exposure_change" in inspect.getsource(hooks._after_commit)


class TestNoInternalCounterIsTouched:
    def test_external_helpers_only_write_a2a_series(self):
        internal = {
            k: v
            for k, v in REGISTRY._names_to_collectors.items()
            if not k.startswith("a2a_")
        }
        snapshot = {k: id(v) for k, v in internal.items()}
        a2a_metrics.record_submission(False, duplicate=False)
        a2a_metrics.record_refusal("rate_limited")
        after = {
            k: id(v)
            for k, v in REGISTRY._names_to_collectors.items()
            if not k.startswith("a2a_")
        }
        assert snapshot == after
