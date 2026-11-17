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

"""Video Studio: record demos in the backend browser, produce and render videos.

The package is split along the two halves the feature keeps apart:

* capture - ``browser`` (the recording browser and its URL policy),
  ``capture`` (a scripted take: high-DPI screencast, action log, cursor path),
  ``signin`` / ``profiles`` / ``totp`` (credentials, MFA, persistent sessions);
* production - ``spec`` (the validated timeline), ``timeline`` (the pure
  planner: durations, camera, cursor, transitions, captions), ``compositor``
  (the deterministic frame renderer), ``encoder`` (ffmpeg), ``voice`` and
  ``clips`` (speech and AI video providers), ``render`` (one production);

with ``jobs`` and ``assets`` shared by both and the orchestration adapters in
``services/orchestration_adapters/video_studio.py``. Design:
``dev-docs/video-studio.md``.

Importing this package stays cheap: the heavy modules (Playwright, the
providers' SDKs) are imported by the functions that need them.
"""
