from app.api.v1.schemas.traces import (
    LLMScore,
    LLMSpan,
    LLMTrace,
)
from app.api.v1.traces import (
    AnalyticsSummaryMetrics,
    CostByAgentItem,
    CostByAgentResponse,
    CostByModelItem,
    CostByModelResponse,
    CostOverTimePoint,
    CostOverTimeResponse,
    ErrorItem,
    ErrorsResponse,
    LatencyByModelItem,
    LatencyDailyMedianPoint,
    LatencyOverall,
    LatencyResponse,
    VolumeOverTimePoint,
    VolumeOverTimeResponse,
)


def test_observability_entity_schemas_shape():
    trace = LLMTrace(
        id="trace-1",
        agent_id=None,
        user_id=None,
        conversation_id="conv-1",
        assignment_run_id=None,
        parent_trace_id=None,
        source="assignment",
        name="Assignment run",
        model="claude-sonnet-4.6",
        environment="production",
        metadata={"region": "eu"},
        input={"messages": []},
        output={"text": "ok"},
        tags=["ops"],
        started_at="2026-06-13T10:00:00Z",
        ended_at=None,
        duration_ms=None,
        input_tokens=100,
        output_tokens=50,
        total_tokens=150,
        input_cost=0.001,
        output_cost=0.002,
        total_cost=0.003,
        status="ok",
        error_message=None,
        level="DEFAULT",
        created_at="2026-06-13T10:00:00Z",
        updated_at="2026-06-13T10:01:00Z",
    )
    span = LLMSpan(
        id="span-1",
        trace_id="trace-1",
        parent_span_id=None,
        type="GENERATION",
        name="generation",
        model=None,
        model_parameters=None,
        input={"messages": []},
        output={"text": "ok"},
        metadata={},
        level="DEFAULT",
        status_message=None,
        started_at="2026-06-13T10:00:01Z",
        ended_at=None,
        duration_ms=None,
        time_to_first_token=None,
        input_tokens=None,
        output_tokens=None,
        input_cost=None,
        output_cost=None,
        total_cost=None,
        usage_details=None,
        tool_call_id=None,
        tool_arguments=None,
        stop_reason=None,
        position=0,
        created_at="2026-06-13T10:00:01Z",
    )
    score = LLMScore(
        id="score-1",
        trace_id="trace-1",
        span_id=None,
        user_id=None,
        name="feedback",
        source="USER",
        data_type="BOOLEAN",
        value=1.0,
        string_value=None,
        comment=None,
        metadata={},
        created_at="2026-06-13T10:02:00Z",
    )
    assert trace.source == "assignment"
    assert span.type == "GENERATION"
    assert score.data_type == "BOOLEAN"


def test_analytics_schemas_shape():
    summary_prev = AnalyticsSummaryMetrics(
        total_cost=4.2,
        total_tokens=1000,
        trace_count=10,
        avg_duration_ms=321.0,
        error_rate=0.1,
        avg_feedback_score=0.8,
    )
    summary = AnalyticsSummaryMetrics(
        total_cost=5.0,
        total_tokens=1200,
        trace_count=12,
        avg_duration_ms=300.0,
        error_rate=0.08,
        avg_feedback_score=0.85,
    )
    by_model = CostByModelResponse(
        items=[
            CostByModelItem(
                model="gpt-5.3-codex",
                trace_count=12,
                input_tokens=800,
                output_tokens=400,
                input_cost=2.1,
                output_cost=2.9,
                total_cost=5.0,
            )
        ]
    )
    cost_over_time = CostOverTimeResponse(series=[CostOverTimePoint(date="2026-06-20", model="gpt", cost=2.3)])
    volume_over_time = VolumeOverTimeResponse(series=[VolumeOverTimePoint(date="2026-06-20", source="assignment", count=7)])
    latency = LatencyResponse(
        overall=LatencyOverall(p50=10.0, p75=20.0, p90=30.0, p95=40.0, p99=50.0),
        by_model=[LatencyByModelItem(model="gpt", p50=10.0, p75=20.0, p90=30.0, p95=40.0, p99=50.0, trace_count=7)],
        daily_median=[LatencyDailyMedianPoint(date="2026-06-20", p50=15.0)],
    )
    by_agent = CostByAgentResponse(
        items=[
            CostByAgentItem(
                agent_id="agent-1",
                agent_name="Support Bot",
                agent_icon="headset",
                trace_count=7,
                total_cost=5.0,
                avg_cost_per_trace=0.71,
                avg_duration_ms=220.0,
                avg_feedback_score=0.9,
            )
        ]
    )
    errors = ErrorsResponse(items=[ErrorItem(error_message="boom", count=2, last_seen="2026-06-20T10:00:00Z", agent_name="Support Bot", model="gpt")])

    assert summary_prev.trace_count == 10
    assert summary.trace_count == 12
    assert by_model.items[0].total_cost == 5.0
    assert cost_over_time.series[0].date == "2026-06-20"
    assert volume_over_time.series[0].source == "assignment"
    assert latency.by_model[0].trace_count == 7
    assert by_agent.items[0].agent_name == "Support Bot"
    assert errors.items[0].error_message == "boom"


def test_observability_entity_schemas_accept_scalar_input_output_payloads():
    trace = LLMTrace(
        id="trace-2",
        agent_id=None,
        user_id=None,
        conversation_id=None,
        assignment_run_id=None,
        parent_trace_id=None,
        source="general-chat",
        name="General chat",
        model="gpt-5.3-codex",
        environment="production",
        metadata={},
        input="plain text input",
        output="plain text output",
        tags=[],
        started_at="2026-06-22T06:58:45Z",
        ended_at=None,
        duration_ms=None,
        input_tokens=0,
        output_tokens=0,
        total_tokens=0,
        input_cost=0.0,
        output_cost=0.0,
        total_cost=0.0,
        status="ok",
        error_message=None,
        level="DEFAULT",
        created_at="2026-06-22T06:58:45Z",
        updated_at="2026-06-22T06:58:45Z",
    )
    span = LLMSpan(
        id="span-2",
        trace_id="trace-2",
        parent_span_id=None,
        type="GENERATION",
        name="llm_call",
        model=None,
        model_parameters=None,
        input="plain text input",
        output="plain text output",
        metadata={},
        level="DEFAULT",
        status_message=None,
        started_at="2026-06-22T06:58:45Z",
        ended_at=None,
        duration_ms=None,
        time_to_first_token=None,
        input_tokens=None,
        output_tokens=None,
        input_cost=None,
        output_cost=None,
        total_cost=None,
        usage_details=None,
        tool_call_id=None,
        tool_arguments=None,
        stop_reason=None,
        position=1,
        created_at="2026-06-22T06:58:45Z",
    )

    assert trace.input == "plain text input"
    assert trace.output == "plain text output"
    assert span.input == "plain text input"
    assert span.output == "plain text output"
