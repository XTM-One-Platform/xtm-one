"""Live smoke tests of the Vertex AI provider - opt-in, real cost (see ``README.md``).

The service account key builds a google-genai client for Vertex AI, its Veo
models are read from the key's region, and a Veo clip is made through the
Gemini provider's operation, asked silent, its video read from the answer's
bytes and read back when its shot resumes.
"""

from __future__ import annotations

from tests.live.live_llm_support import build_live_provider, call_api, close_provider, live_marks, model_for

pytestmark = live_marks("vertex_ai")

VEO_MODEL = model_for("LIVE_LLM_VERTEX_VIDEO_MODEL", "veo-3.1-fast-generate-001")


async def test_the_keys_region_lists_its_veo_models():
    provider = build_live_provider("vertex_ai")
    try:
        models = await call_api(provider.list_video_models())
    finally:
        await close_provider(provider)
    assert VEO_MODEL in models, models


async def test_a_veo_clip_is_made_silent_and_read_back_when_its_shot_resumes():
    """One 4 s 720p clip without audio: the most expensive test of the suite."""
    from app.llm.video import VideoRequest

    provider = build_live_provider("vertex_ai")
    jobs: list[str] = []

    async def on_job(job_id: str) -> None:
        jobs.append(job_id)

    request = VideoRequest(
        model=VEO_MODEL,
        prompt="A paper boat drifts on a calm pond at sunrise, eye-level camera.",
        duration_seconds=4,
        resolution="720p",
        sound=False,
    )
    try:
        made = await call_api(provider.generate_video(request, on_job=on_job))
        again = await call_api(provider.generate_video(request, resume_job=made.job_id))
    finally:
        await close_provider(provider)
    assert made.data[4:8] == b"ftyp", "Veo answered with something other than an MP4"
    assert jobs == [made.job_id]
    assert again.data == made.data
