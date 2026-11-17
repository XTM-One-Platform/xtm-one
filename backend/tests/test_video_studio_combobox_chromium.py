"""A recording picks an option in an ARIA combobox by its name, in a real Chromium.

``select`` used to drive a native ``<select>`` only: on a button or an input
opening a list (an OpenCRQ tenant picker, an autocomplete) it clicked, then
failed. Each page here needs the mechanism its test names:

* a button combobox whose listbox is portalled to the end of the document and
  named by ``aria-controls``, its option named in another case than asked;
* an editable combobox that lists nothing until something is typed;
* a list without the option asked: the step fails naming what it offered,
  and the list is closed;
* a native ``<select>``, still chosen by its value;
* a navigation entry named like the option is never clicked, an optional
  step gives up on its own wait, and a list id holding a backslash fails the
  step rather than the take.

Skipped where Playwright's Chromium is not installed; the ``Browser tool
(Chromium)`` CI job installs it and sets ``REQUIRE_CHROMIUM=1``, which turns
the skip into a failure.
"""

from __future__ import annotations

import os
import time
from types import SimpleNamespace

import pytest

from app.services.video_studio import capture
from app.services.video_studio.spec import TakeScript

pytestmark = pytest.mark.chromium

PAGE = """<!doctype html><html><body style="font: 16px sans-serif; margin: 40px">
<nav style="position: absolute; right: 40px; top: 40px"><a id="side" role="menuitem" href="#"
  onclick="window.sideClicked = true; return false;">Filigran</a></nav>
<button id="weird" role="combobox" aria-label="Weird" aria-controls="list\\" style="position: absolute; right: 40px; top: 90px">Weird</button>
<button id="tenant" role="combobox" aria-label="Tenant" aria-haspopup="listbox" aria-expanded="false"
  aria-controls="tenants" style="width: 220px; height: 36px">Choose a tenant</button>
<div style="height: 30px"></div>
<label for="org">Organization</label>
<input id="org" role="combobox" aria-autocomplete="list" aria-controls="orgs" aria-expanded="false"
  style="width: 220px; height: 32px">
<ul id="orgs" role="listbox" style="list-style: none; padding: 0; margin: 4px 0; width: 220px"></ul>
<div style="height: 30px"></div>
<select id="region" aria-label="Region"><option value="eu">Europe</option><option value="us">United States</option></select>
<script>
const button = document.getElementById('tenant');
button.addEventListener('click', () => {
  if (document.getElementById('tenants')) return;
  // Portalled: the list is the document's last child, as most component libraries draw it.
  setTimeout(() => {
    const list = document.createElement('ul');
    list.id = 'tenants'; list.setAttribute('role', 'listbox');
    list.style.cssText = 'position: absolute; top: 80px; left: 40px; list-style: none; padding: 4px; margin: 0; background: #eee';
    for (const name of ['Acme Corp', 'Filigran', 'Globex']) {
      const item = document.createElement('li');
      item.setAttribute('role', 'option'); item.textContent = name; item.style.padding = '6px 12px';
      item.addEventListener('click', () => { button.textContent = name; list.remove(); });
      list.appendChild(item);
    }
    document.body.appendChild(list);
    button.setAttribute('aria-expanded', 'true');
  }, 120);
});
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') document.getElementById('tenants')?.remove(); });
const input = document.getElementById('org');
const orgs = document.getElementById('orgs');
input.addEventListener('input', () => {
  orgs.innerHTML = '';
  const typed = input.value.toLowerCase();
  if (!typed) return;
  for (const name of ['Northwind', 'Umbrella', 'Wayne Enterprises']) {
    if (!name.toLowerCase().includes(typed)) continue;
    const item = document.createElement('li');
    item.setAttribute('role', 'option'); item.textContent = name; item.style.padding = '6px 12px';
    item.addEventListener('click', () => { input.value = name; orgs.innerHTML = ''; });
    orgs.appendChild(item);
  }
});
</script></body></html>"""


async def _recorder(page) -> capture._Recorder:
    script = TakeScript.model_validate({"steps": [{"action": "wait", "seconds": 0}], "pace": "fast", "settle_ms": 0})
    viewport = (script.viewport.width, script.viewport.height)
    recorder = capture._Recorder(SimpleNamespace(page=page), script, viewport=viewport, max_seconds=120)
    recorder.t_start = time.time()
    return recorder


async def _on_page(run) -> None:
    from playwright.async_api import async_playwright

    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            try:
                page = await browser.new_page(viewport={"width": 900, "height": 600})
                await page.set_content(PAGE)
                await run(page, await _recorder(page))
            finally:
                await browser.close()
    except Exception as exc:
        text = str(exc)
        if "Executable doesn't exist" not in text and "playwright install" not in text.lower():
            raise
        if os.environ.get("REQUIRE_CHROMIUM") == "1":
            pytest.fail(f"Playwright's Chromium is required here and is not installed: {exc}")
        pytest.skip("Playwright's Chromium is not installed (python -m playwright install chromium)")


def _step(**fields):
    return TakeScript.model_validate({"steps": [{"action": "select", **fields}]}).steps[0]


async def test_a_button_combobox_opens_its_portalled_list_and_the_option_is_picked_by_name():
    async def run(page, recorder):
        await recorder.run_step(0, _step(role="combobox", name="Tenant", value="filigran"))
        assert await page.locator("#tenant").inner_text() == "Filigran"
        event = recorder.events[-1]
        assert event["action"] == "select" and event["option"]["name"] == "Filigran"
        # The camera frames the control and its choice: the box holds both.
        control, chosen, framed = event["control_box"], event["option"]["box"], event["box"]
        assert framed["y"] <= control["y"] and framed["y"] + framed["h"] >= chosen["y"] + chosen["h"]
        assert event["point"]["y"] > control["y"] + control["h"]
        assert event["t0"] <= event["ta"] <= event["t1"]
        # The navigation entry named like the option, visible before the list opened, is never clicked.
        assert await page.evaluate("() => !!window.sideClicked") is False

    await _on_page(run)


async def test_an_optional_step_gives_up_on_its_own_wait_not_three_times_the_list_wait():
    """The three tries shared no deadline and ignored the step's own (9 s and more for a missing option)."""

    async def run(page, recorder):
        started = time.monotonic()
        with pytest.raises(capture.TakeError, match="no option named 'Initech'"):
            await recorder.run_step(0, _step(role="combobox", name="Tenant", value="Initech", optional=True, timeout=1.0))
        assert time.monotonic() - started < 2.5

    await _on_page(run)


async def test_a_list_id_holding_a_backslash_fails_the_step_not_the_take():
    """``[id="list\\"]`` was no selector at all, and Playwright's error retried the whole recording."""

    async def run(page, recorder):
        with pytest.raises(capture.TakeError, match="no option named 'Anything'"):
            await recorder.run_step(0, _step(role="combobox", name="Weird", value="Anything", timeout=1.0))

    await _on_page(run)


async def test_an_editable_combobox_has_the_value_typed_when_its_list_does_not_show_it():
    async def run(page, recorder):
        await recorder.run_step(0, _step(role="combobox", name="Organization", value="Wayne Enterprises"))
        assert await page.locator("#org").input_value() == "Wayne Enterprises"
        assert recorder.events[-1]["option"]["name"] == "Wayne Enterprises"

    await _on_page(run)


async def test_an_option_the_list_does_not_have_fails_the_step_naming_what_it_offered():
    async def run(page, recorder):
        with pytest.raises(capture.TakeError, match="no option named 'Initech'") as caught:
            await recorder.run_step(0, _step(role="combobox", name="Tenant", value="Initech"))
        assert '"Acme Corp" (option)' in str(caught.value) and '"Globex" (option)' in str(caught.value)
        # Closed again: an optional step leaves the page as it found it.
        assert await page.locator("#tenants").count() == 0

    await _on_page(run)


async def test_a_native_select_is_still_chosen_by_its_value():
    async def run(page, recorder):
        await recorder.run_step(0, _step(selector="#region", value="us"))
        assert await page.locator("#region").input_value() == "us"
        assert "option" not in recorder.events[-1]

    await _on_page(run)
