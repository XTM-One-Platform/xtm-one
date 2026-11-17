/*
Copyright (c) 2025-2026 Filigran SAS

This file is part of the XTM One Enterprise Edition ("EE") and is
licensed under the XTM One Enterprise Edition License (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
*/

/**
 * Secret variable markers.
 *
 * Where model output held the value of a secret variable, XTM One stores a
 * `[[SECRET_VARIABLE:<variable id>:<tag>]]` marker instead (backend
 * `SECRET_MARKER_RE`, `app/core/redaction.py`), restored for the model only.
 * A person sees the display text the live stream shows, never the marker.
 */

const SECRET_MARKER_RE =
  /\[\[SECRET_VARIABLE:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}:[0-9a-f]{16}\]\]/g;

/** Mirrors `SECRET_DISPLAY_TEXT` in `app/core/redaction.py`. */
export const SECRET_DISPLAY_TEXT = "[secret]";

/** *text* with every stored secret marker shown as the display text. */
export function showSecretMarkers(text: string): string {
  return text.includes("[[SECRET_VARIABLE:") ? text.replace(SECRET_MARKER_RE, SECRET_DISPLAY_TEXT) : text;
}
