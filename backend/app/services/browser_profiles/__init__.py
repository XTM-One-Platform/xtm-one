# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""Browser profiles: a signed-in browser identity every browser of the platform can open.

* ``browser`` - the profile browser (headless Chromium) and its URL policy;
* ``profiles`` - the ONE reader and writer of a profile's secrets and session;
* ``signin`` / ``totp`` - the sign-in, MFA included, and the session probe;
* ``keepalive`` - the worker's sweep that keeps saved sessions signed in;
* ``agent_access`` / ``agent_sessions`` - which profiles an agent's ``browser``
  tool may open for a run, and the signed-in session it opens.

A Video Studio recording opens a profile through the same browser
(``services/video_studio/capture.py``). Design: ``dev-docs/browser-profiles.md``.

Importing this package stays cheap: Playwright is imported by the functions
that launch a browser.
"""
