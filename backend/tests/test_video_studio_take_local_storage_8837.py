"""A take seeds an application's own preference into its start origin's localStorage (#8837).

``color_scheme`` only emulates ``prefers-color-scheme``: the Copilot web app keeps its theme in ``copilot-theme`` and
never reads the media query, and the page state reset clears that key. ``TakeScript.local_storage`` writes the keys
asked at the start of every document of the start origin, and every reset of the take keeps them.
"""

from __future__ import annotations

import json

import pytest

from app.services.browser_profiles import browser
from app.services.video_studio import capture, spec, take_script


def _take_script(**fields) -> spec.TakeScript:
    return spec.parse_take_script(spec.validate_take_script({"steps": [{"action": "wait", "seconds": 1}], **fields}))


def test_a_take_seeds_nothing_by_default_and_keeps_the_keys_it_is_given():
    assert _take_script().local_storage == {}
    script = _take_script(local_storage={"copilot-theme": "light"}, color_scheme="light")
    assert script.local_storage == {"copilot-theme": "light"} and script.color_scheme == "light"


def test_a_seeded_key_is_bounded():
    with pytest.raises(spec.VideoSpecError, match="at most 20 keys"):
        _take_script(local_storage={f"k{i}": "v" for i in range(spec.MAX_SEEDED_KEYS + 1)})
    with pytest.raises(spec.VideoSpecError, match="1 to 200 characters"):
        _take_script(local_storage={" ": "light"})
    with pytest.raises(spec.VideoSpecError, match="at most 4000 characters"):
        _take_script(local_storage={"copilot-theme": "x" * 4001})
    with pytest.raises(spec.VideoSpecError):
        _take_script(local_storage={"copilot-theme": 1})


def test_the_seed_script_writes_the_keys_on_its_origin_only():
    js = browser.seed_storage_script("https://copilot.example.test", {"copilot-theme": "light"})
    plan = json.loads(js.split("const plan = ", 1)[1].split(";\n", 1)[0])
    assert plan == {"origin": "https://copilot.example.test", "values": {"copilot-theme": "light"}}
    assert "location.origin !== plan.origin" in js and "window.top !== window" in js
    assert "localStorage.setItem(key, value)" in js and "__PLAN__" not in js


def test_every_reset_of_the_take_keeps_the_seeded_keys_after_the_session_s():
    assert capture.seeded_keep(("auth",), {"copilot-theme": "light"}) == ("auth", "copilot-theme")
    assert capture.seeded_keep(("auth",), {}) == ("auth",)
    assert capture.seeded_keep((), {"auth": "x", "copilot-theme": "light"}) == ("auth", "copilot-theme")
    assert capture.seeded_keep(("auth",), {"auth": "x"}) == ("auth",)
    kept = capture.storage_reset(spec.PageReset(), capture.seeded_keep(("auth",), {"copilot-theme": "light"}))
    assert kept.keep == ("auth", "copilot-theme") and kept.local_storage is True


def test_a_recording_keeps_its_seeded_keys_for_a_re_record():
    assert "local_storage" in take_script.RECORDING_PARAMS
    kept = take_script.submitted([{"action": "wait", "seconds": 1}], {"local_storage": {"copilot-theme": "light"}})
    assert kept["params"] == {"local_storage": {"copilot-theme": "light"}}
    steps, params = take_script.merged(kept, steps=None, params={"local_storage": None})
    assert params["local_storage"] == {"copilot-theme": "light"} and steps == kept["steps"]
