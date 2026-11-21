"""query_loki_logs output is bounded per line and in total (gap loki-query-result-uncapped)."""

from __future__ import annotations

from app.integrations.providers.grafana import _format_loki_logs


def _response(lines: list[str]) -> dict:
    values = [[str(1_763_500_000_000_000_000 + i), line] for i, line in enumerate(lines)]
    return {"status": "success", "data": {"result": [{"stream": {"job": "app"}, "values": values}]}}


def test_short_lines_are_kept_whole() -> None:
    out = _format_loki_logs(_response(["boot ok", "ready"]))
    assert "boot ok" in out and "ready" in out
    assert "cut" not in out
    assert out.endswith("Total: 2 log line(s) across 1 stream(s).")


def test_a_long_line_is_cut_and_counted() -> None:
    out = _format_loki_logs(_response(["x" * 5_000]), line_chars=2_000)
    assert "[... 3,000 chars cut]" in out
    assert "x" * 2_001 not in out
    assert "1 line(s) cut at 2,000 characters." in out


def test_total_budget_counts_the_lines_left_out() -> None:
    out = _format_loki_logs(_response(["y" * 1_500] * 7), line_chars=2_000, total_chars=3_000)
    assert len(out) < 4_000
    assert "5 more log line(s) left out past 3,000 characters" in out
    assert "narrow the LogQL" in out


def test_seven_traceback_lines_stay_far_below_the_tool_cap() -> None:
    out = _format_loki_logs(_response(["Traceback " + "z" * 28_000] * 7))
    assert len(out) < 20_000
    assert out.count("chars cut]") == 7


def test_metric_matrix_series_carry_their_label_sets() -> None:
    data = {
        "status": "success",
        "data": {
            "resultType": "matrix",
            "result": [
                {"metric": {"level": "error"}, "values": [[1_763_500_000, "12"], [1_763_500_060, "3"]]},
                {"metric": {"level": "warn"}, "values": [[1_763_500_000, "7"]]},
            ],
        },
    }
    out = _format_loki_logs(data)
    assert 'Series: {level="error"}' in out
    assert 'Series: {level="warn"}' in out
    assert "Stream: {}" not in out
    assert "[2025-11-18 21:06:40 UTC] 12" in out
    assert out.endswith("Total: 3 sample(s) across 2 series.")


def test_metric_vector_sample_is_shown_with_its_labels() -> None:
    data = {
        "status": "success",
        "data": {"resultType": "vector", "result": [{"metric": {"app": "api"}, "value": [1_763_500_000, "42"]}]},
    }
    out = _format_loki_logs(data)
    assert 'Series: {app="api"}' in out
    assert "] 42" in out
    assert out.endswith("Total: 1 sample(s) across 1 series.")


def test_empty_result_message_unchanged() -> None:
    assert _format_loki_logs({"status": "success", "data": {"result": []}}) == (
        "No log results found (Loki status: success)."
    )
