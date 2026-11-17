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

"""The outbound half of A2A: agents on this platform tasking a partner's agent.

Separate from the inbound modules beside it (``card``, ``exposure``, ``tasks``,
``federation``, ``grants``, ``tokens``) because the two answer opposite
questions. Those answer "who is calling us, and may they"; these answer "who
are we calling, and can we trust their card". Flattening both into one package
is how a reader loses track of which direction a given function serves — and
this feature already changes shipped inbound behaviour, so keeping the halves
legible matters more here than it would in a greenfield package.

Three modules are deliberately NOT here and stay shared, because both
directions must agree on them:

* :mod:`app.a2a.protocol` — one version shaper. Two would eventually disagree
  about what v0.3 means, and the disagreement would surface as a card we
  publish contradicting a card we consume.
* :mod:`app.a2a.audit` — one actor record.
* :mod:`app.a2a.metrics` — one counter namespace, with outbound counters
  distinct from inbound ones inside it.

**Direction lives in** :mod:`app.a2a.providers`, not here. It governs the
inbound exposure hook as much as the outbound tool, and a second module
answering "is this integration A2A, and which way" is exactly the
two-sources-of-truth failure this feature's own FR-053 exists to prevent.
"""
