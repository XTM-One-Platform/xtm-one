---
applyTo: "licensing/**,backend/app/**,frontend/src/**,apps/**"
---

# Reviewing the edition of new and moved code

XTM One ships a Community Edition (CE, Apache 2.0) and an Enterprise Edition (EE). `licensing/editions.yml` and, in a mixed directory, the CE marker (`SPDX-License-Identifier: Apache-2.0` on the first line of a CE file) decide the edition of each file; the contract is `dev-docs/reference/editions.md`. CI already fails when a new file or directory is not classified, so do not report that. Your job is whether the decision is right, because the two directions are not equal: a file published as CE can never become EE, while EE can become CE later.

The rule: **everything agentic is EE** (agents, assignments, runs, tools, chat, flows, knowledge bases, evaluations, memory, traces, presentations, reports, video studio, relays, the desktop runner). Authentication, users and groups, settings, audit logs, files, CTEM metrics, models, migrations, the edition gate and the Platform MCP Server modules are CE.

Comment only on these, each with the file and one sentence:

1. **An agentic file or directory classified CE**: a new file that starts with the CE marker, a new `ce` entry or an `except` entry for code that runs agents, tools, chats or any feature listed above as EE.
2. **CE code moved into an EE file**: a pull request that deletes or shrinks a CE file and adds the same code to an EE one. The moved code stays CE: the new file keeps the CE marker (and goes to `except` in an EE directory).
3. **A directory classified `ce` that agentic code is about to land in**: a `ce` entry for a directory whose name or content is agentic; it should be `mixed` or `<dir>/**` in `ee`.
4. **A hand-written license header**, or one copied into a file: headers come from `backend/scripts/license_headers.py --fix` only.

When unsure whether something is agentic, say so and suggest EE: it can still become CE later.
