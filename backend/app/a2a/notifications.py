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

"""Activity-feed event types for exposure changes.

``app/services/notify.py`` is run-oriented: every one of its helpers loads an
``AssignmentRun`` and fans out to web/Teams/Slack/WhatsApp/email. An exposure
event is not a run, so it has no run to load — which is why these live here
rather than there. They write to the existing activity feed rather than
introducing a new delivery channel: the point is that the affected user
*learns* their reach went external (or stopped being), and a new notification
transport was scope this feature deliberately declined.

Only the event names live here. Both notices are written inline by the
listeners in ``a2a.hooks`` — ``_notify_exposed`` and
``_revoke_live_exposures`` — because that listener runs in ``before_flush``,
which is synchronous and cannot await a helper that adds to an
``AsyncSession``. Exposure follows an integration binding, so the hook is the
only place either event can be raised from.
"""

from __future__ import annotations

ACTIVITY_EXPOSED = "a2a_agent_exposed"
ACTIVITY_REVOKED = "a2a_exposure_revoked"
