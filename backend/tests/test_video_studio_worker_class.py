"""A recording asking for a worker class (``record_browser_demo(worker_class=...)``, #10959).

What is proven: a worker's class is read from its WebGL renderer (``worker_class.class_of``); a take asking for a
class is claimed only by a worker of that class while one asking for none is claimed by any
(``VideoTakeAdapter.deal_claims``); a class no running recording worker serves is refused naming those there are;
the class is kept in the submitted script only when given, so a re-record copies it only when its source asked; the
worker that recorded a take starts its action log's first line.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.services.video_studio import capture_rate, take_script, take_state, worker_class


@pytest.mark.parametrize(
    ("renderer", "gpu", "expected"),
    [
        ("ANGLE (NVIDIA, NVIDIA L4/PCIe/SSE2, OpenGL 4.5)", True, "l4"),
        ("ANGLE (NVIDIA Corporation, Tesla V100S-PCIE-32GB/PCIe/SSE2, OpenGL 4.5.0)", True, "v100s"),
        ("ANGLE (NVIDIA, NVIDIA A10G Vulkan 1.3)", True, "a10g"),
        ("ANGLE (Unknown vendor)", True, worker_class.GPU),
        ("ANGLE (Google, SwiftShader Device)", False, worker_class.CPU),
        (None, False, worker_class.CPU),
    ],
)
def test_a_worker_class_is_read_from_its_renderer(renderer, gpu, expected):
    assert worker_class.class_of(gpu=gpu, renderer=renderer) == expected


def test_any_and_blank_ask_for_no_class():
    assert worker_class.normalized(None) is None
    assert worker_class.normalized("  ") is None
    assert worker_class.normalized("ANY") is None
    assert worker_class.normalized(" L4 ") == "l4"


def _execution(asked=None):
    payload = {"take_id": "t"}
    if asked:
        payload[worker_class.PAYLOAD_KEY] = asked
    return SimpleNamespace(payload=payload)


def test_a_take_asking_for_a_class_is_claimed_only_by_that_class():
    free, l4, v100s = _execution(), _execution("l4"), _execution("v100s")
    due = [free, l4, v100s]
    assert worker_class.claimable(due, "l4") == [free, l4]
    assert worker_class.claimable(due, "v100s") == [free, v100s]
    # A worker whose class is not decided yet claims only the takes asking for none.
    assert worker_class.claimable(due, None) == [free]


def test_the_adapter_deals_by_its_worker_class(monkeypatch):
    from app.services.orchestration_adapters.video_studio import VideoTakeAdapter

    free, l4, v100s = _execution(), _execution("l4"), _execution("v100s")
    monkeypatch.setattr(capture_rate, "worker_record", lambda: {"class": "v100s", "gpu": "V100S"})
    assert asyncio.run(VideoTakeAdapter().deal_claims(None, [free, l4, v100s], "w1")) == [free, v100s]
    monkeypatch.setattr(capture_rate, "worker_record", lambda: None)
    assert asyncio.run(VideoTakeAdapter().deal_claims(None, [free, l4, v100s], "w1")) == [free]


def test_the_adapter_presence_says_its_class(monkeypatch):
    from app.services.orchestration_adapters.video_studio import VideoTakeAdapter

    monkeypatch.setattr(capture_rate, "lane_gl", lambda: None)
    monkeypatch.setattr(capture_rate, "worker_record", lambda: {"class": "l4", "gpu": "L4"})
    assert VideoTakeAdapter().presence() == {"recording_class": "l4", "recording_gpu": "L4"}
    monkeypatch.setattr(capture_rate, "worker_record", lambda: None)
    assert VideoTakeAdapter().presence() == {}


def test_a_class_no_worker_serves_is_refused_naming_those_available():
    records = [
        {"state": "running", "recording_class": "v100s"},
        {"state": "running", "recording_class": "cpu"},
        {"state": "draining", "recording_class": "l4"},
        {"state": "running"},
    ]
    available = worker_class.served(records, serves=lambda record: record.get("state") == "running")
    assert available == ["cpu", "v100s"]
    said = worker_class.refusal("l4", available)
    assert said is not None and "l4" in said and "cpu, v100s" in said and "nothing was recorded or charged" in said
    assert worker_class.refusal("v100s", available) is None
    assert "is not a class" in (worker_class.refusal("L4 GPU!", available) or "")


def test_the_class_is_kept_only_when_given_and_a_rerecord_copies_it():
    asked = take_script.submitted([{"action": "wait"}], {"start_url": "https://x", "worker_class": "l4"})
    assert asked["params"]["worker_class"] == "l4"
    none_asked = take_script.submitted([{"action": "wait"}], {"start_url": "https://x", "worker_class": None})
    assert "worker_class" not in none_asked["params"]
    _, params = take_script.merged(asked, steps=None, params={"worker_class": None})
    assert params["worker_class"] == "l4"
    _, params = take_script.merged(none_asked, steps=None, params={"worker_class": None})
    assert "worker_class" not in params
    _, params = take_script.merged(asked, steps=None, params={"worker_class": "v100s"})
    assert params["worker_class"] == "v100s"


def test_the_action_log_first_line_names_the_worker():
    worker = {"class": "l4", "gpu": "L4"}
    said = take_state.opened_words({"url": "https://x", "carried": False}, worker=worker)
    assert said.startswith("recorded on a worker of class l4 (GPU L4), opened on https://x")
    cpu = take_state.opened_words({"url": "https://x", "carried": False}, worker={"class": "cpu", "gpu": None})
    assert cpu.startswith("recorded on a worker of class cpu (software GL)")
    assert take_state.opened_words({"url": "https://x", "carried": False}).startswith("opened on https://x")


def test_a_software_worker_says_cpu_and_an_undecided_one_says_nothing(monkeypatch):
    monkeypatch.setattr(capture_rate, "gl_mode", lambda: "software")
    assert capture_rate.worker_record() == {"class": "cpu", "gpu": None}
    monkeypatch.setattr(capture_rate, "gl_mode", lambda: "gpu")
    monkeypatch.setattr(capture_rate, "_BACKEND", [])
    assert capture_rate.worker_record() is None
    monkeypatch.setattr(
        capture_rate,
        "_BACKEND",
        [capture_rate.GlBackend((), gpu=True, renderer="ANGLE (NVIDIA, NVIDIA L4/PCIe/SSE2)")],
    )
    assert capture_rate.worker_record() == {"class": "l4", "gpu": "L4"}
