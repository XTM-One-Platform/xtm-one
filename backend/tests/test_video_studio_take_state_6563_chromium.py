"""A take starts from a clean page state, a step that misses its element names the page state, and a 60 fps take falls
back to the size the worker sustains - in a real Chromium.

The fixture is a report graph that keeps its layout where an application graph does: "Lay out the graph around it"
switches it to a radial layout - ``?layoutMode=radial&layoutCentreId=42`` written into its address, ``layoutMode`` into
its localStorage, ``graphState`` into its sessionStorage -, and the radial layout has no "Layered layout" button. Opened
again, it reads its layout back from its localStorage and writes it into its address. It is signed in while its
localStorage holds ``auth`` (the profile marks it with ``signed_in_selector``), the key the profile declares its
session's (``keep_storage_keys``).

* Take 1 applies the radial layout; take 2 on the session it saved opens on the clean layout and finds "Layered
  layout"; take 2 with ``reset_state: false`` fails that step naming the address's query and "found on take 1", its
  contact sheet ending on the frame where it failed.
* The reset keeps ``auth`` and clears the other keys, and the session probe still says signed in; a profile that keeps
  nothing is signed out by the reset, which says so without touching the saved session.
* A ``goto`` resets its page mid-take, and ``snapshot_browser_profile_page`` reads the names a take will find.
* A 60 fps take on a worker throttled to 50 fps at 2400x1350 records, with ``fps_fallback: size``, at the largest window
  measured sustaining it.

Skipped where Playwright's Chromium is not installed; the ``Browser tool (Chromium)`` CI job installs it and sets
``REQUIRE_CHROMIUM=1``, which turns the skip into a failure.
"""

from __future__ import annotations

import dataclasses
import os
import tempfile
import threading
import uuid
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest

from app.services import web_fetch
from app.services.browser_profiles import browser, signin
from app.services.browser_profiles.browser import TargetPolicy
from app.services.browser_profiles.profiles import ProfileSnapshot
from app.services.video_studio import capture, capture_rate, spec, take_state

pytestmark = pytest.mark.chromium

HOST = "app.localhost"
PAGE = """<!doctype html><html><head><title>Report graph</title></head><body>
<form id="signin"><input name="user" aria-label="User"><input type="password" name="secret" aria-label="Password">
<button type="submit">Sign in</button></form>
<main id="graph" hidden><div role="toolbar" id="toolbar"></div><canvas width="400" height="240"></canvas></main>
<script>
const signedIn = localStorage.getItem('auth') === 'token-1';
const params = new URLSearchParams(location.search);
let mode = params.get('layoutMode') || localStorage.getItem('layoutMode') || 'layered';
const address = () => location.pathname + '?layoutMode=' + mode + '&layoutCentreId=42';
function render() {
  document.getElementById('signin').hidden = signedIn;
  document.getElementById('graph').hidden = !signedIn;
  if (!signedIn) return;
  const bar = document.getElementById('toolbar');
  bar.innerHTML = mode === 'radial'
    ? '<button type="button">Radial layout</button>'
    : '<button type="button">Layered layout</button><button type="button" id="around">Lay out the graph around it</button>';
  const around = document.getElementById('around');
  if (around) around.onclick = () => {
    mode = 'radial';
    localStorage.setItem('layoutMode', mode);
    sessionStorage.setItem('graphState', 'centred');
    history.replaceState(null, '', address());
    render();
  };
}
if (signedIn && mode !== 'layered' && !params.has('layoutMode')) history.replaceState(null, '', address());
render();
</script></body></html>"""


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = PAGE.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args) -> None:
        return


@pytest.fixture
def app(monkeypatch) -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    async def validate(url: str, allow_private: bool = False) -> str:
        if urlparse(url).hostname == HOST:
            return url
        return await web_fetch.validate_outbound_url_async(url, allow_private=allow_private)

    monkeypatch.setattr(browser, "validate_outbound_url_async", validate)
    monkeypatch.setattr(browser, "private_targets_enabled", lambda: False)
    try:
        yield f"http://{HOST}:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def _skipped_without_chromium(exc: Exception) -> None:
    if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc).lower():
        if os.environ.get("REQUIRE_CHROMIUM") == "1":
            pytest.fail(f"Playwright's Chromium is required here and is not installed: {exc}")
        pytest.skip("Playwright's Chromium is not installed (python -m playwright install chromium)")


def _signed_in(app: str, *local: tuple[str, str], session: dict | None = None) -> dict:
    """A saved session: the application's localStorage (``auth`` first), and its sessionStorage when given."""
    items = [{"name": "auth", "value": "token-1"}, *({"name": name, "value": value} for name, value in local)]
    state: dict = {"cookies": [], "origins": [{"origin": app, "localStorage": items}]}
    if session:
        state[browser.SESSION_STORAGE_KEY] = {app: session}
    return state


def _profile(app: str, state: dict, *, keep: tuple[str, ...] = ("auth",), sign_in_timeout: float = 20.0) -> ProfileSnapshot:
    config: dict = {"signed_in_selector": "#graph"}
    if keep:
        config["keep_storage_keys"] = list(keep)
    return ProfileSnapshot(
        id=str(uuid.uuid4()),
        name="Report graph",
        start_url=f"{app}/graph",
        login_url=None,
        login_config=config,
        storage_state=state,
        sign_in_timeout=sign_in_timeout,
        session_save_timeout=15.0,
    )


def _script(steps: list[dict], **fields) -> spec.TakeScript:
    return spec.parse_take_script(
        spec.validate_take_script(
            {"viewport": {"width": 1280, "height": 720}, "pixel_ratio": 1, "settle_ms": 0, "end_hold_ms": 200, "steps": steps, **fields}
        )
    )


async def _take(profile: ProfileSnapshot, script: spec.TakeScript, workdir: str, **options) -> capture.CaptureResult:
    try:
        return await capture.record_take(
            script, workdir=workdir, profile=profile, start_url=profile.start_url, policy=profile.policy, max_seconds=60, **options
        )
    except capture.TakeError:
        raise
    except Exception as exc:
        _skipped_without_chromium(exc)
        raise


def _local_keys(state: dict | None) -> set[str]:
    return {item["name"] for origin in (state or {}).get("origins") or [] for item in origin.get("localStorage") or []}


LAYERED = {"action": "click", "role": "button", "name": "Layered layout"}
AROUND = {"action": "click", "role": "button", "name": "Lay out the graph around it"}


async def test_a_second_take_on_a_profile_left_radial_finds_layered_layout_or_says_the_page_is_in_another_state(app):
    first_script = _script([LAYERED, AROUND])
    with tempfile.TemporaryDirectory() as workdir:
        first = await _take(_profile(app, _signed_in(app)), first_script, workdir)
    assert all(event["ok"] for event in first.events)
    radial = f"{app}/graph?layoutMode=radial&layoutCentreId=42"
    assert first.events[1]["url"] == radial
    # The session the first take saves carries the radial layout into the next one.
    assert _local_keys(first.storage_state) == {"auth", "layoutMode"}
    assert first.storage_state[browser.SESSION_STORAGE_KEY] == {app: {"graphState": "centred"}}
    take_1 = str(uuid.uuid4())
    earlier = take_state.earlier_finds(
        [SimpleNamespace(id=take_1, name="Radial take", script=spec.stored_take_script(first_script), events=first.events)]
    )

    # Take 2 with the default reset: the clean layout, its "Layered layout" found.
    second_steps = [{**LAYERED, "timeout": 2}]
    with tempfile.TemporaryDirectory() as workdir:
        second = await _take(_profile(app, first.storage_state), _script(second_steps), workdir, earlier=earlier)
    assert second.events[0]["ok"] and second.events[0]["url_before"] == f"{app}/graph"
    opened = second.capture["opened"]
    assert opened["url"] == f"{app}/graph" and opened["added_query"] == []
    assert opened["cleared"]["local_storage"] == ["layoutMode"] and opened["cleared"]["kept"] == ["auth"]
    assert opened["cleared"]["session_storage"] == ["graphState"]
    assert take_state.opened_words(opened).startswith(
        f"opened on {app}/graph - page state reset before step 0: cleared 1 localStorage key (layoutMode) and 1 "
        "sessionStorage key (graphState); kept auth"
    )

    # Take 2 with reset_state false: the radial layout the session carried, the step failing in it.
    with tempfile.TemporaryDirectory() as workdir:
        with pytest.raises(capture.StepFailedError) as failed:
            await _take(_profile(app, first.storage_state), _script(second_steps, reset_state=False), workdir, earlier=earlier)
        picks = capture.failure_sheet_frames(failed.value)
        assert picks[-1][2] == "step 0 failed: Layered layout" and (Path(workdir) / picks[-1][1]).exists()
    said = str(failed.value)
    assert f'The page was at {radial} ("Report graph"), showing 1 element of role button' in said
    assert (
        f'It was found on take {take_1} ("Radial take") at {app}/graph; this take is at {radial}: the page is in '
        "another state."
    ) in said
    assert failed.value.opened["carried"] is True and "cleared" not in failed.value.opened


async def test_the_reset_keeps_the_profile_s_session_keys_and_the_session_still_signs_in(app):
    state = _signed_in(app, ("layoutMode", "radial"), ("prefs", "dense"), session={"graphState": "centred"})
    profile = _profile(app, state)
    with tempfile.TemporaryDirectory() as workdir:
        result = await _take(profile, _script([{"action": "wait", "seconds": 0.2}]), workdir)
    cleared = result.capture["opened"]["cleared"]
    assert sorted(cleared["local_storage"]) == ["layoutMode", "prefs"] and cleared["kept"] == ["auth"]
    assert cleared["session_storage"] == ["graphState"]
    assert result.signed_in is True
    # What it saves holds the session key and none of the page state; probed, it still signs in.
    assert _local_keys(result.storage_state) == {"auth"} and browser.SESSION_STORAGE_KEY not in result.storage_state
    assert result.probe is not None and result.probe.authenticated
    try:
        probe = await signin.probe_profile(dataclasses.replace(profile, storage_state=result.storage_state))
    except Exception as exc:
        _skipped_without_chromium(exc)
        raise
    assert probe.authenticated, probe

    # A profile that keeps none of its keys: the reset signs the page out, and says so.
    with tempfile.TemporaryDirectory() as workdir, pytest.raises(capture.ResetSignedOutError) as signed_out:
        await _take(_profile(app, state, keep=(), sign_in_timeout=3.0), _script([{"action": "wait", "seconds": 0.2}]), workdir)
    said = str(signed_out.value)
    assert said.startswith("The page state reset signed the application out: start_url rendered it signed in before")
    assert "auth" in said and "keep_storage_keys" in said and "The profile's saved session is untouched" in said


async def test_a_goto_resets_its_page_mid_take(app):
    steps = [AROUND, {"action": "goto", "url": f"{app}/graph", "reset_state": True}, {**LAYERED, "timeout": 3}]
    with tempfile.TemporaryDirectory() as workdir:
        result = await _take(_profile(app, _signed_in(app)), _script(steps), workdir)
    assert all(event["ok"] for event in result.events)
    reset = result.events[1]["reset"]
    assert reset["local_storage"] == ["layoutMode"] and reset["session_storage"] == ["graphState"] and reset["kept"] == ["auth"]
    assert result.events[2]["url_before"] == f"{app}/graph"


async def test_a_page_looked_at_before_scripting_shows_the_names_a_take_finds(app):
    profile = _profile(app, _signed_in(app, ("layoutMode", "radial")))
    try:
        clean = await capture.snapshot_page(profile, f"{app}/graph", viewport=(1280, 720))
        carried = await capture.snapshot_page(profile, f"{app}/graph", viewport=(1280, 720), reset_state=False)
    except Exception as exc:
        _skipped_without_chromium(exc)
        raise
    assert {"Layered layout", "Lay out the graph around it"} <= {entry["name"] for entry in clean.names}
    assert clean.opened is not None and clean.opened["cleared"]["local_storage"] == ["layoutMode"]
    assert "Layered layout" not in {entry["name"] for entry in carried.names} and carried.opened is None
    assert carried.url == f"{app}/graph?layoutMode=radial&layoutCentreId=42"


async def test_a_60_fps_take_records_at_the_largest_window_the_worker_sustains_with_fps_fallback_size(app, monkeypatch):
    """A worker throttled to 50 fps at 2400x1350, a frame's cost going with its pixels."""

    async def warm_up(_browser, viewport, ratio, **_kw):
        return min(120.0, 50.0 * 2400 * 1350 / (round(viewport[0] * ratio) * round(viewport[1] * ratio)))

    monkeypatch.setattr(capture_rate, "warm_up", warm_up)
    monkeypatch.setattr(capture_rate, "_TABLE", {})
    script = _script([{"action": "wait", "seconds": 0.3}], viewport={"width": 1600, "height": 900}, pixel_ratio=1.5, fps=60, fps_fallback="size")
    with tempfile.TemporaryDirectory() as workdir:
        try:
            result = await capture.record_take(
                script, workdir=workdir, profile=None, start_url=f"{app}/graph", policy=TargetPolicy(), max_seconds=30, warm_up=True
            )
        except capture.TakeError:
            raise
        except Exception as exc:
            _skipped_without_chromium(exc)
            raise
    width, height = result.viewport
    assert 0.9 * 1460.6 <= width <= 1460.6 and height == round(width * 900 / 1600) and result.pixel_ratio == 1.5
    assert result.frame_size is not None and abs(result.frame_size[0] - round(width * 1.5)) <= 1
    assert result.script is not None and (result.script.viewport.width, result.script.fps) == (width, 60)
    assert result.capture["fallback"].startswith(f"recorded at a {width}x{height} window at pixel ratio 1.5")
    assert "fps_fallback size" in result.capture["fallback"] and any("fps_fallback size" in note for note in result.notes)
