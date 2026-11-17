import pytest

from app.llm.base import BaseLLMProvider, ChatMessage, ChatResponse
from app.tools.agent_loop import run_agent_loop


class _LoopStubLLM(BaseLLMProvider):
    provider_name = "stub"
    context_window = 10_000

    async def chat(self, messages, model=None, temperature=0.7, max_tokens=4096, tools=None, **kwargs):
        return ChatResponse(content="final answer", model=model or "stub")

    async def chat_stream(self, messages, model=None, temperature=0.7, max_tokens=4096, tools=None, **kwargs):
        yield "final answer"

    async def embed(self, text, model=None):
        return [0.0]

    async def list_models(self):
        return []


@pytest.mark.asyncio
async def test_compaction_trigger_honors_explicit_session_budget():
    llm = _LoopStubLLM()
    messages = [ChatMessage(role="user", content="question")]
    for idx in range(6):
        messages.append(
            ChatMessage(
                role="tool",
                content=f"tool-{idx}-" + ("x" * 450),
                tool_call_id=f"tool-{idx}",
                name="stub_tool",
            )
        )
    events: list[dict] = []

    async def _on_event(event: dict) -> None:
        events.append(event)

    response = await run_agent_loop(
        llm=llm,
        messages=messages,
        tools=None,
        max_iterations=1,
        max_session_chars=1_200,
        on_event=_on_event,
    )

    assert response.content == "final answer"
    assert any(event.get("status") == "context_compaction" for event in events)
