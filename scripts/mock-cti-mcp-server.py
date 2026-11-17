#!/usr/bin/env python
"""A stdio MCP server with canned CTI answers, for exercising Deep Investigation's MCP tool by hand.

Deep Investigation can call a designated MCP tool. Verifying that end to end
needs a real server on the other side of a real connection — a test double
inside the process proves the querier's logic and nothing about the handshake,
the tool schema, or what the block finally renders.

This is that server, and it is deliberately a fixture rather than a product:
every answer is canned, nothing is fetched, and no key is needed. It exists so
someone can watch a `tool_result` citation appear in the block.

Run it through the backend's own virtualenv — that is where `mcp` is installed,
and the dev stack runs the backend natively, so the platform can spawn exactly
this command:

    Transport  stdio
    Command    <repo>/backend/.venv/bin/python
    Arguments  <repo>/scripts/mock-cti-mcp-server.py

Then open the server's page, press "Discover Tools", and press
"Use for investigation" on `lookup_indicator`. Leave the subject argument
blank: the tool takes exactly one required text parameter, which is the case
the investigation resolves from the schema on its own.

Three subjects behave differently on purpose, because a source that always
answers proves only one of the states the block has to show:

    anything            a short canned dossier  -> completed, 1 finding
    "nothing"           an empty answer         -> empty, 0 findings
    "boom"              a tool error            -> error, invalid_response
"""

from __future__ import annotations

import sys

try:
    from mcp.server.fastmcp import FastMCP
except ModuleNotFoundError:  # pragma: no cover - operator-facing guidance
    sys.exit(
        "The `mcp` package is missing. Run this with the backend virtualenv:\n"
        "  <repo>/backend/.venv/bin/python <repo>/scripts/mock-cti-mcp-server.py"
    )

mcp = FastMCP("Mock CTI")


@mcp.tool()
def lookup_indicator(indicator: str) -> str:
    """Look up a canned threat-intelligence dossier for an indicator.

    One required text parameter and nothing else: this is the shape the
    investigation can place a subject into without being told which argument
    to use.
    """
    subject = (indicator or "").strip()
    if subject.lower() == "nothing":
        # A tool that ran and found nothing. The block must show this as
        # `empty`, not as a failure — the tool works, the subject is absent.
        return ""
    if subject.lower() == "boom":
        raise RuntimeError("the upstream feed refused this query")
    # Deliberately NOT self-invalidating. An earlier version ended with "this
    # is not real intelligence and must not be treated as such", which is a
    # sensible thing to write in a fixture and a useless thing to return from
    # one: a well-behaved model reads it and refuses to cite the result, so
    # the MCP half of the pipeline could never be shown working end to end.
    # Provenance is carried where it belongs — every citation of this is
    # labelled "Mock CTI · lookup_indicator", naming the fixture server.
    return (
        f"Internal Threat Feed record — {subject}\n\n"
        f"{subject} was first observed on 2026-02-11 in a credential-phishing "
        "campaign attributed to a financially motivated actor. The campaign "
        "used two staging domains and a single loader family. Internal "
        "confidence: medium. Last reviewed 2026-03-02 by the fixture feed."
    )


if __name__ == "__main__":
    mcp.run()
