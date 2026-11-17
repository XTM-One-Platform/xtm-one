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

"""The code interpreter's sandbox workspace.

A directory that survives across ``code_interpreter`` calls of one
conversation (or one assignment run), across turns and across a resumed run:
``workspace/`` in the sandbox's working directory, ``WORKSPACE_DIR`` in its
environment. Contract: ``dev-docs/reference/sandbox-workspace.md``.

- ``paths``: what a workspace path may be (the one rule).
- ``manifest``: entries, quota and file limit, diff and listing (pure).
- ``store``: PostgreSQL manifest + object-store contents; the access rule.
- ``session``: one turn's handle; every read and write goes through it.
- ``local`` / ``remote``: the workspace in the local subprocess sandbox and
  in an OpenSandbox container.
- ``run``: what one ``code_interpreter`` call did with it, as the result says.
- ``kernel``: ``session=true``, the long-lived kernel.
- ``ingest``: ``to_workspace_path`` on the tools that return external content.
- ``repository``: ``materialize_repository``'s archive extraction.
- ``checkout``: the record it leaves beside the tree and the change set a commit reads from it.
- ``packages``: server-side wheel installs into ``workspace/.venv``.
- ``gc``: the sweep (idle TTL, archived or deleted conversations, orphans).
- ``metrics`` / ``audit``: what operators see.
"""
