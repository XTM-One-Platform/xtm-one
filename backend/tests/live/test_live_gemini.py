"""Live smoke tests of the Gemini provider - opt-in, real cost (see ``README.md``).

Gemini returns thought summaries only when the request sets
``include_thoughts``, and a tool loop must hand every thought signature
back with the function call it came with, or Gemini 3 refuses the request.
A Gemini Omni clip is a background interaction the Interactions API accepts,
polled until its video comes, and read back when its shot is resumed.
"""

from __future__ import annotations

import pytest

from tests.live.live_llm_support import (
    REASONING_ANSWER,
    ReasoningCollector,
    RequestSpy,
    assert_answer_is,
    assert_reasoning_kept_out_of,
    assert_tool_loop_answered,
    build_live_provider,
    call_api,
    close_provider,
    leaked_reasoning,
    live_marks,
    model_for,
    run_tool_loop,
    stream_turn,
)

pytestmark = live_marks("gemini")

GEMINI_MODELS = [
    pytest.param(model_for("LIVE_LLM_GEMINI_25_MODEL", "gemini-2.5-flash"), id="gemini-2.5"),
    pytest.param(model_for("LIVE_LLM_GEMINI_3_MODEL", "gemini-3.8-flash"), id="gemini-3"),
]
OMNI_MODEL = model_for("LIVE_LLM_GEMINI_VIDEO_MODEL", "gemini-omni-1.1-flash")


@pytest.fixture
async def provider():
    llm = build_live_provider("gemini")
    yield llm
    await close_provider(llm)


def _signatures(parts) -> set[bytes]:
    return {part.thought_signature for part in parts or [] if getattr(part, "thought_signature", None)}


@pytest.mark.parametrize("model", GEMINI_MODELS)
async def test_thought_summaries_reach_the_consumer_only(provider, model):
    sent = RequestSpy(provider.client.aio.models, "generate_content_stream")
    reasoning = ReasoningCollector()

    answer = await call_api(stream_turn(provider, model=model, max_tokens=4096, on_thinking=reasoning))

    thinking_config = sent.calls[-1]["config"].thinking_config
    assert thinking_config is not None
    assert thinking_config.include_thoughts is True
    assert_reasoning_kept_out_of(answer, reasoning)
    assert_answer_is(answer, REASONING_ANSWER)


@pytest.mark.parametrize("model", GEMINI_MODELS)
async def test_tool_loop_hands_the_thought_signatures_back(provider, model):
    sent = RequestSpy(provider.client.aio.models, "generate_content_stream")
    reasoning = ReasoningCollector()

    result = await call_api(run_tool_loop(provider, model=model, on_thinking=reasoning, max_tokens=4096))

    assert_tool_loop_answered(result)
    assert not leaked_reasoning(result.final.content, reasoning)
    returned = set()
    for response in result.responses:
        if response.tool_calls:
            returned |= _signatures((response._provider_extra or {}).get("gemini_parts"))
    # The last request carries the whole transcript: every model turn of the loop.
    replayed = set()
    for content in sent.calls[-1]["contents"]:
        if content.role == "model":
            replayed |= _signatures(content.parts)
    assert returned <= replayed, (
        f"{len(returned - replayed)} of the {len(returned)} thought signature(s) "
        "returned with a tool call were not sent back"
    )


async def test_an_omni_clip_is_made_and_read_back_when_its_shot_resumes(provider):
    """One short 720p clip: the most expensive test of the suite."""
    from app.llm.video import VideoRequest

    jobs: list[str] = []

    async def on_job(job_id: str) -> None:
        jobs.append(job_id)

    request = VideoRequest(
        model=OMNI_MODEL,
        prompt="A paper boat drifts on a calm pond, eye-level camera.",
        duration_seconds=4,
        resolution="720p",
    )
    made = await call_api(
        provider.generate_video(request, on_job=on_job), no_access_skip=f"the key cannot use {OMNI_MODEL}"
    )
    assert made.data[4:8] == b"ftyp", "the interaction answered with something other than an MP4"
    assert jobs == [made.job_id]
    again = await call_api(provider.generate_video(request, resume_job=made.job_id))
    assert again.data == made.data
