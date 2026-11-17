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

"""A2A (Agent2Agent) inbound server — external agents discover and task XTM One agents.

**Boundary rule: ``a2a_sdk`` may be imported ONLY from ``app/a2a/protocol.py``.**

Nothing else in the codebase — not this package's other modules, not the API
layer, not the services layer — may import ``a2a_sdk``. The reason is
containment of a young third-party dependency on a security-relevant surface:
the SDK's models and error taxonomy change between protocol versions (v0.3 and
v1.0 disagree on ``Part`` variant naming, on the spelling of the canceled
state, and on where ``extendedAgentCard`` lives), and a breaking SDK release
must therefore be absorbable by editing one module rather than by auditing
every call site.

``protocol.py`` is the only place that translates between the SDK's shapes and
ours. Everything else in this package speaks in platform terms:

- ``card.py``     — the explicit Agent Card projection (never a model dump)
- ``exposure.py`` — exposure authority, boundary resolution, tool allow-list,
                    the escalation guard, and the partner credential (including
                    the token claims that confine it to the A2A surface)
- ``tasks.py``    — task create/get/cancel, run-status <-> A2A-state mapping,
                    artifact projection, deduplication, and the stale-run reaper
- ``partner_scope.py`` — the partner-task scope every hop of a task runs in,
                    which keeps the platform-builder surface (trace
                    inspection, agent-builder writes) from the partner

Version negotiation and v1.0<->v0.3 shaping also live in ``protocol.py`` rather
than in ``card.py``, even though the card path needs them first: there must be
exactly one shaper, or the card and task surfaces would eventually disagree
about what "v0.3" means.

The single rule the whole package turns on: **the execution boundary follows
never the actor who exposed it.** External work runs as a user - the agent's
owner, or an administrator's explicit choice for a company-managed agent - and
sees exactly what that user sees, except the platform-builder surface, which
an external caller never gets with that user's reach. See
``exposure.resolve_run_as`` and ``partner_scope``.

See ``specs/006-a2a-server-inbound/`` for the specification, plan, and
contracts this package implements.
"""
