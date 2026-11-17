# SPDX-License-Identifier: Apache-2.0

"""A path of this platform's web app: what a link the platform builds from stored or agent-written text may open.

One rule for every such link (a dashboard widget's ``href``, a phone notification's ``path``, the
route of a notification): a single leading ``/``, a bounded run of URL characters, never ``//`` or
``/\\`` (another host), never ``/api`` (the API, not a page). Anything else is not a path of the app.
"""

from __future__ import annotations

import re

_INTERNAL_PATH_RE = re.compile(r"^/(?![/\\])[A-Za-z0-9._~!$&'()*+,;=:@%/?#-]{0,300}$")
_API_PATH_RE = re.compile(r"^/api(?:[/?#]|$)", re.IGNORECASE)


def is_internal_path(value: str) -> bool:
    """Whether *value* is a path of this platform's web app (see the module docstring)."""
    return bool(_INTERNAL_PATH_RE.match(value)) and not _API_PATH_RE.match(value)
