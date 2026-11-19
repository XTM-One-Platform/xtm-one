"""The two README diagrams: (a) the XTM Platform and CTEM loop, one shared
design for all five; (b) one product data-flow diagram per product, same
canvas, same three-column grammar (inputs, the product, outcomes)."""
import math
import svgkit as k
import textpath as tp
from theme import PRODUCTS

STAGES = [
    ("Scoping", "opencti"),
    ("Discovery", "opencti"),
    ("Prioritization", "opencrq"),
    ("Validation", "openaev"),
    ("Mobilization", "xtm-one"),
]


def platform(t: dict) -> str:
    tp.reset_defs()
    P = t["products"]
    b = [k.header(t, "The XTM Platform and the CTEM loop",
                  "One agentic core, three open source modules, one tradecraft library, five CTEM stages")]
    # ---- left: the CTEM loop ring
    cx, cy, r = 372, 486, 238
    pw, ph = 236, 84
    pts = []
    for i in range(5):
        a = math.radians(-90 + 72 * i)
        pts.append((cx + r * math.cos(a), cy + r * math.sin(a), -90 + 72 * i))
    # arcs between stages (drawn first, under the pills)
    for i in range(5):
        a0 = math.radians(pts[i][2] + 25)
        a1 = math.radians(pts[i][2] + 72 - 25)
        x0, y0 = cx + r * math.cos(a0), cy + r * math.sin(a0)
        x1, y1 = cx + r * math.cos(a1), cy + r * math.sin(a1)
        b.append(f'<path d="M{x0:.1f} {y0:.1f}A{r} {r} 0 0 1 {x1:.1f} {y1:.1f}" stroke="{t["accent"]}" stroke-width="3" fill="none" marker-end="url(#arr)"/>')
    b.append(f'<circle cx="{cx}" cy="{cy}" r="138" fill="{t["card"]}" stroke="{t["line"]}" stroke-width="1.5"/>')
    b.append(k.text("CTEM", cx, cy - 6, 44, t["fg"], "heading", 700, "middle"))
    b.append(k.text("Continuous threat", cx, cy + 32, 19, t["muted"], "body", 400, "middle"))
    b.append(k.text("exposure management", cx, cy + 56, 19, t["muted"], "body", 400, "middle"))
    for i, (x, y, _) in enumerate(pts):
        name, prod = STAGES[i]
        col = P[prod]
        b.append(f'<rect x="{x - pw / 2:.1f}" y="{y - ph / 2:.1f}" width="{pw}" height="{ph}" rx="{ph / 2}" fill="{t["card"]}" stroke="{col}" stroke-width="2.5"/>')
        b.append(f'<circle cx="{x - pw / 2 + 34:.1f}" cy="{y:.1f}" r="19" fill="{col}"/>')
        b.append(k.text(str(i + 1), x - pw / 2 + 34, y + 8, 22, t["page"], "heading", 700, "middle"))
        b.append(k.text(name, x - pw / 2 + 64, y - 3, 21, t["fg"], "heading", 600, maxw=pw - 74))
        b.append(k.text(PRODUCTS[prod]["name"], x - pw / 2 + 64, y + 23, 17, col, "body", 600, maxw=pw - 74))
    # ---- right: the products
    X0, X1 = 720, 1384
    Wd = X1 - X0
    one_y, one_h = 196, 132
    b.append(k.card(t, X0, one_y, Wd, one_h, "XTM One", "Core and agentic layer: agents, fleets, MCP, A2A",
                    color=P["xtm-one"], product="xtm-one", title_size=30, sub_size=19, pad=32))
    b.append(k.accent_bar(X0 + 10, one_y, one_h, P["xtm-one"]))
    mods = [("opencti", "OpenCTI", "Threat intelligence"),
            ("opencrq", "OpenCRQ", "Risk quantification"),
            ("openaev", "OpenAEV", "Exposure validation")]
    gap = 26
    mw = (Wd - 2 * gap) / 3
    my, mh = 398, 178
    for j, (p, name, role) in enumerate(mods):
        x = X0 + j * (mw + gap)
        mc = x + mw / 2
        b.append(k.line(mc, one_y + one_h + 4, mc, my - 2, t["accent"]))
        b.append(f'<rect x="{x:.1f}" y="{my}" width="{mw:.1f}" height="{mh}" rx="{k.RADIUS}" fill="{t["card"]}" stroke="{t["line"]}" stroke-width="1.5"/>')
        b.append(f'<rect x="{x:.1f}" y="{my}" width="{mw:.1f}" height="8" rx="4" fill="{P[p]}"/>')
        b.append(k.mark(p, mc - 26, my + 30, 52, P[p]))
        b.append(k.text(name, mc, my + 120, 26, t["fg"], "heading", 600, "middle", maxw=mw - 24))
        b.append(k.text(role, mc, my + 150, 18, t["muted"], "body", 400, "middle", maxw=mw - 24))
    hub_y, hub_h = 648, 112
    for j in (0, 2):
        mc = X0 + j * (mw + gap) + mw / 2
        b.append(k.line(mc, hub_y - 4, mc, my + mh + 6, t["muted"], dashed=True, marker="arrm"))
    b.append(k.card(t, X0, hub_y, Wd, hub_h, "XTM Hub", "Tradecraft library, one-click deploy to every module",
                    color=P["xtm-hub"], product="xtm-hub", title_size=26, sub_size=18, pad=32))
    b.append(k.accent_bar(X0 + 10, hub_y, hub_h, P["xtm-hub"]))
    return k.canvas(t, "The XTM Platform and the CTEM loop", b)


def flow(t: dict, product: str, title: str, subtitle: str, inputs, core, outputs) -> str:
    tp.reset_defs()
    P = t["products"]
    col = P[product]
    b = [k.header(t, title, subtitle)]
    top, bottom = 196, 776
    # column headers
    cols = [(56, 456, "Inputs"), (536, 904, PRODUCTS[product]["name"]), (984, 1384, "Outcomes")]
    for i, (x0, x1, lab) in enumerate(cols):
        if i == 1:
            b.append(k.mark(product, x0, top - 40, 26, col))
            b.append(k.text(lab.upper(), x0 + 36, top - 18, 16, col, "heading", 700))
        else:
            b.append(k.text(lab.upper(), x0, top - 18, 16, t["muted"], "heading", 600))
    n = 4
    gap = 20
    ch = (bottom - top - (n - 1) * gap) / n
    # core panel
    b.append(f'<rect x="536" y="{top}" width="368" height="{bottom - top}" rx="{k.RADIUS + 6}" fill="{t["card2"]}" stroke="{col}" stroke-width="2.5"/>')
    cg = 16
    cch = (bottom - top - 40 - (n - 1) * cg) / n
    for i, (tt, ss) in enumerate(core):
        y = top + 20 + i * (cch + cg)
        b.append(k.card(t, 556, y, 328, cch, tt, ss, title_size=21, sub_size=16, pad=30))
        b.append(f'<rect x="566" y="{y + 18:.1f}" width="5" height="{cch - 36:.1f}" rx="2.5" fill="{col}"/>')
        if i < n - 1:
            b.append(k.line(720, y + cch + 1, 720, y + cch + cg - 1, col, marker=None, width=2))
    for i, (tt, ss) in enumerate(inputs):
        y = top + i * (ch + gap)
        b.append(k.card(t, 56, y, 400, ch, tt, ss, title_size=22, sub_size=17))
        b.append(k.curve(460, y + ch / 2, 532, (top + bottom) / 2 + (i - 1.5) * 40, t["accent"]))
    for i, (tt, ss) in enumerate(outputs):
        y = top + i * (ch + gap)
        b.append(k.card(t, 984, y, 400, ch, tt, ss, title_size=22, sub_size=17))
        b.append(k.curve(908, (top + bottom) / 2 + (i - 1.5) * 40, 982, y + ch / 2, col))
    return k.canvas(t, title, b)


FLOWS = {
    "xtm-one": dict(
        title="How XTM One works",
        subtitle="The agentic core that runs CTEM across OpenCTI, OpenAEV and OpenCRQ",
        inputs=[("Your security team", "Web UI, command center, chat"),
                ("MCP clients", "Any client, API key or OAuth 2.1"),
                ("A2A agents", "Remote agents call and are called"),
                ("XTM Platform modules", "OpenCTI, OpenAEV, OpenCRQ events")],
        core=[("Identity", "SSO, groups, permissions"),
              ("Shared context", "Organization, knowledge, skills"),
              ("Agents and fleets", "Flows, triggers, secure sandbox"),
              ("MCP servers", "OpenCTI, OpenAEV, OpenCRQ")],
        outputs=[("CTEM command center", "Posture across the five stages"),
                 ("Actions on the modules", "With the user's own permissions"),
                 ("Remediation cases", "From finding to verified fix"),
                 ("Your AI provider", "Cloud or local, AI switchable off")]),
    "opencti": dict(
        title="How OpenCTI works",
        subtitle="From scattered sources to one threat knowledge graph your CTEM program acts on",
        inputs=[("Feeds and connectors", "TAXII, RSS, CSV, STIX bundles"),
                ("Analysts", "Reports, cases, investigations"),
                ("XTM Hub", "Integrations, dashboards, playbooks"),
                ("OpenAEV", "Security coverage results")],
        core=[("STIX 2.1 knowledge graph", "Every fact with source, confidence"),
              ("Inference rules", "Explainable derived knowledge"),
              ("Playbooks and drafts", "Qualify, enrich and route"),
              ("Dashboards", "Widgets on any query")],
        outputs=[("Threat landscape", "Actors, techniques, vulnerabilities"),
                 ("Detection content", "Indicators, Sigma, YARA, streams"),
                 ("OpenAEV and OpenCRQ", "Threats to test and to quantify"),
                 ("XTM One", "Context for agents and Ariane")]),
    "openaev": dict(
        title="How OpenAEV works",
        subtitle="Real attacker techniques, run safely against your defenses, measured end to end",
        inputs=[("OpenCTI", "Intrusion sets, attack patterns"),
                ("XTM Hub", "Scenarios and integrations"),
                ("Your environment", "Endpoints, cloud, network, people"),
                ("EDR, SIEM, XDR", "Telemetry through collectors")],
        core=[("Attack scenarios", "Atomic tests built from real threats"),
              ("Threat arsenal", "Actions and payloads"),
              ("Executors", "Agent, implant, Caldera, EDR"),
              ("Expectations", "Prevention, detection, vulnerability")],
        outputs=[("Validated exposure", "Scores and findings per technique"),
                 ("Remediation", "Fixes ranked and sent to XTM One"),
                 ("Security coverage", "Written back to OpenCTI"),
                 ("Risk inputs", "Tested verdicts for OpenCRQ")]),
    "opencrq": dict(
        title="How OpenCRQ works",
        subtitle="Threats and tested defenses in, financial risk and ranked remediation out",
        inputs=[("OpenCTI", "Actors, campaigns, techniques"),
                ("OpenAEV", "Prevention and detection verdicts"),
                ("Assets and controls", "Dependencies, controls, findings"),
                ("Financial profile", "Sector, revenue, loss drivers")],
        core=[("Risk scenarios", "Attack outcome on a primary asset"),
              ("Simulated worlds", "Control presence and effectiveness"),
              ("Monte Carlo attack runs", "In the FAIR vocabulary"),
              ("What-if analysis", "The value of each control")],
        outputs=[("Financial risk", "Annualized loss, p10 to p90"),
                 ("Loss exceedance curve", "Per scenario and portfolio"),
                 ("Prioritized remediation", "Ranked by loss removed"),
                 ("Executive reporting", "Dashboards and treatment plans")]),
    "xtm-hub": dict(
        title="How XTM Hub works",
        subtitle="Curated tradecraft, deployed in one click to the instances you connect",
        inputs=[("Filigran and community", "Curated, reviewed tradecraft"),
                ("OpenCTI content", "Dashboards, integrations, playbooks"),
                ("OpenAEV content", "Scenarios and atomic tests"),
                ("Your organization", "Registered instances")],
        core=[("Library", "Search, browse, compare"),
              ("Instance registration", "OpenCTI, OpenAEV, XTM One"),
              ("One-click deploy", "No export, no manual import"),
              ("GraphQL API", "PostgreSQL, Elasticsearch, S3")],
        outputs=[("OpenCTI", "Dashboards, playbooks, connectors"),
                 ("OpenAEV", "Ready-to-run scenarios"),
                 ("XTM One", "Connected to the Hub"),
                 ("Your security team", "Tradecraft reused, not rebuilt")]),
}


def product(t: dict, p: str) -> str:
    f = FLOWS[p]
    return flow(t, p, f["title"], f["subtitle"], f["inputs"], f["core"], f["outputs"])
