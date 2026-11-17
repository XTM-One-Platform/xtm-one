from app.models.assignment import AssignmentRun
from app.models.chat import Message
from app.models.evaluation import EvaluationResultTrace


def _assert_indexed_uuid_column(model: type, column_name: str, expected_index: str) -> None:
    """Verify llm_trace_id is a nullable, indexed UUID column (no FK)."""
    table = model.__table__
    column = table.c[column_name]
    assert column.nullable is True
    assert any(idx.name == expected_index for idx in table.indexes)
    # No FK constraint — trace row is committed asynchronously
    assert len(column.foreign_keys) == 0


def test_assignment_run_llm_trace_id_column() -> None:
    _assert_indexed_uuid_column(AssignmentRun, "llm_trace_id", "ix_assignment_runs_llm_trace_id")


def test_message_llm_trace_id_column() -> None:
    _assert_indexed_uuid_column(Message, "llm_trace_id", "ix_messages_llm_trace_id")


def test_evaluation_result_trace_trace_id_column() -> None:
    """trace_id is a NOT NULL indexed soft reference — no FK, because the
    llm_traces row is committed asynchronously by the tracing dispatcher."""
    table = EvaluationResultTrace.__table__
    column = table.c["trace_id"]
    assert column.nullable is False
    assert column.index is True
    assert len(column.foreign_keys) == 0
