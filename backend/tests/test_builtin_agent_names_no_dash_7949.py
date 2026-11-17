"""Seeded built-in agent names carry no em or en dash.

The product copy rule bans the em dash; the built-in names are shown on
/flows, the agents list and the browser extension pickers as they are
seeded, so a dash in the seed reaches every screen.
"""

from app.agents.builtin_agents import BUILTIN_AGENTS


def test_no_builtin_agent_name_has_an_em_or_en_dash():
    dashes = ("\u2014", "\u2013")
    offenders = [
        a["name"] for a in BUILTIN_AGENTS if any(d in a["name"] for d in dashes)
    ]
    assert offenders == []


def test_browser_extension_agents_keep_their_browser_prefix():
    names = [a["name"] for a in BUILTIN_AGENTS if a["slug"].startswith("browser-")]
    assert names
    assert all(name.startswith("Browser: ") for name in names)
