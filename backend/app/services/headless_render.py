# SPDX-License-Identifier: Apache-2.0

"""Launch and context options for every headless Chromium that renders user-influenced content.

The deck export / layout check / slide capture (``presentation_export.render_deck``),
``render_html_to_image`` and the Markdown / HTML PDF renderer (``pdf_render``)
each route every request through a filter that aborts what leaves the page's
own origin. Two channels sit outside such a filter, so each of those browsers
treats them the same way:

* a resource hint (``<link rel="dns-prefetch">``, ``rel="preconnect"``, an
  anchor's DNS prefetch) resolves or connects without a request the route
  sees. Playwright's bundled headless Chromium does neither for any hint:
  its net-log records no host resolution and no socket for a hint's host (an
  IP-literal ``preconnect`` included), with or without a route installed,
  while a real request is resolved (checked on Playwright 1.60,
  re-checked on 1.63 / Chromium 153.0.8010.12).
  That configuration is what closes the channel;
  ``--dns-prefetch-disable`` is only a belt for a build that would honour
  hints, not the control. A Chromium or Playwright upgrade must keep hints
  inert - re-check it with ``--log-net-log`` when bumping either;
* a request a service worker answers or makes can bypass the context route -
  service workers are blocked on the context.

Not applied to the agent ``browser`` tool (``services/browser_tool.py``),
which browses the web on purpose and keeps its own behaviour.
"""

from __future__ import annotations

from typing import Literal

#: Chromium launch arguments of every content-rendering browser.
RENDER_CHROMIUM_ARGS: tuple[str, ...] = ("--dns-prefetch-disable",)

#: ``browser.new_context(service_workers=...)`` of every content-rendering context.
RENDER_SERVICE_WORKERS: Literal["block"] = "block"
