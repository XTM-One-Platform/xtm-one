"""XTM One fills the README template (doc 33 v3). Copy carried over from the v2
README of XTM-One-Platform/xtm-one (docs/readme-ctem-424), reshaped to the
13 sections; gallery and walkthrough captures from the lab on synthetic data."""

DATA = {
    "key": "xtm-one",
    "name": "XTM One",
    "repo": "XTM-One-Platform/xtm-one",
    "page": "https://filigran.io/products/xtm-one",
    "docs": "https://docs.xtmone.io/latest/",
    "docs_host": "docs.xtmone.io",
    "docker": "xtmone/platform",
    "ci": ("ci.yml", "main"),
    "license_badge": "Apache 2.0",
    "tagline": "The open source agentic core of the XTM Platform: Agentic CTEM, from threat signal to verified fix.",
    "pitch": (
        "XTM One gives security teams one command center for Continuous Threat Exposure Management (CTEM). "
        "It reads the threats that target you from [OpenCTI](https://github.com/OpenCTI-Platform/opencti), "
        "proves which of them get through your defenses with [OpenAEV](https://github.com/OpenAEV-Platform/openaev), "
        "prices the risk with [OpenCRQ](https://github.com/OpenCRQ-Platform/opencrq), and puts AI agents and fleets "
        "to work on the exposures that matter, under your control."
    ),
    "hero_alt": ("The XTM One CTEM command center: the overall exposure posture, a score for each of the five CTEM "
                 "stages, the connected OpenCTI, OpenAEV and OpenCRQ platforms and the next actions to take"),
    "why_intro": ("Security teams drown in findings and still cannot answer the question that matters: which "
                  "exposures will an adversary actually use against us, and are they fixed?"),
    "why": [
        ("Threat-informed, not severity-informed.", "Exposures are ranked by the adversaries that target your "
         "sector and regions, by the techniques your defenses failed to stop in validation and by the expected "
         "loss, not by a raw CVSS score."),
        ("One posture across the five CTEM stages.", "Scoping, Discovery, Prioritization, Validation and "
         "Mobilization are scored from the data already in OpenCTI, OpenAEV and OpenCRQ, so the program sees "
         "where it is weakest."),
        ("Agents that do the work, humans that decide.", "Stage analysts, an exposure briefing and ready-made "
         "CTEM fleets read your platforms, explain what changed and propose the next actions; approvals stay "
         "with your team."),
    ],
    "capabilities": {
        "Scoping": "The attack surface and threat landscape that matter to you: sectors, regions, primary assets, "
                   "tech stack and priority intelligence requirements, read by every CTEM agent.",
        "Discovery": "The vulnerabilities, assets and threat activity in that scope, the attacker techniques "
                     "never tested, and the blind spots where a connected source returns no data.",
        "Prioritization": "A funnel from every known vulnerability down to the few that are relevant, "
                          "exploitable and open, ranked by exploitation, targeting adversaries and expected loss.",
        "Validation": "Proof of whether prevention and detection hold against the techniques that matter, from "
                      "OpenAEV simulations, failed expectations and detection coverage.",
        "Mobilization": "Remediation cases grouped by fix, with their assets, primary assets and known "
                        "exploitation, followed to closure and re-test by agents and fleets.",
    },
    "gallery": [
        ("Discovery", "See which techniques of the adversaries that target you were never tested.",
         "The CTEM Discovery stage: threat techniques from OpenCTI against the techniques tested in OpenAEV, "
         "with the untested ones counted"),
        ("Prioritization", "Read which of thousands of known vulnerabilities are exploited and open on the assets that matter.",
         "The CTEM Prioritization stage: the AI assessment of the open vulnerabilities and the stage metrics, "
         "known-exploited and critical CVEs"),
        ("Remediation cases", "One case per fix, ranked, with the primary assets and exploitation it touches.",
         "CTEM remediation cases: one row per fix with its priority, issue count, primary assets, known "
         "exploitation and state"),
        ("Case detail", "Every issue behind a fix, from intelligence and validation, with the assets at stake.",
         "A remediation case with its open issues from OpenCTI and OpenAEV, known exploited vulnerabilities, "
         "maximum CVSS and affected assets"),
        ("CTEM fleets", "A team of agents that takes a vulnerability from triage to a verified fix.",
         "The Vulnerability prioritization to verified fix fleet: its automations, the value it delivered and "
         "the graph of its orchestrator and member agents"),
        ("Agentic chat", "Ask in plain words; agents answer from your platforms with your permissions.",
         "The XTM One chat: an agent answers a CTEM question with sources from OpenCTI and OpenAEV"),
    ],
    "walkthrough": "From the command center to a remediation case and the fleet that closes it, in 15 seconds.",
    "walkthrough_alt": ("Animated walkthrough: the CTEM command center, the Prioritization funnel, a remediation "
                        "case and the fleet that takes it to a verified fix"),
    "how": (
        "XTM One connects to OpenCTI, OpenAEV and OpenCRQ with the user's own identity, computes the CTEM posture "
        "from their data and runs agents, fleets and flows on schedules, webhooks, platform events and work items. "
        "It is self-hosted with PostgreSQL (pgvector), Redis and an S3-compatible store, works with your own AI "
        "provider or a local model, and runs agent code in an isolated sandbox with closed egress."
    ),
    "flow_alt": ("How XTM One works: OpenCTI, OpenAEV and OpenCRQ data and events flow into the agentic core, "
                 "which computes the CTEM posture and drives agents, fleets and remediation cases"),
    "roles": {
        "CTI": "Search, read and write STIX 2.1 knowledge in OpenCTI in natural language with your own "
               "permissions; extract objects and TTPs from reports, grounded in their source.",
        "SOC": "Threat hunting packages from high-value indicators, simulation results and failed expectations, "
               "and agents that answer in the chat, in MCP clients or over A2A.",
        "Detection engineering": "STIX patterns converted to SPL, KQL, Elastic, Sigma, YARA, Snort and Suricata; "
                                 "detections generated for the techniques OpenAEV proved undetected.",
        "Red and purple team": "Adversary emulation scenarios mapped to MITRE ATT&CK, built from the techniques "
                               "of the adversaries that target you and run as OpenAEV simulations.",
        "Vulnerability and exposure management": "The prioritization funnel, remediation cases by fix and the "
                                                 "fleet that triages, validates, quantifies and re-tests.",
        "Risk and CISO": "The exposure posture and its trend, the exposure briefing, expected loss from OpenCRQ "
                         "and the value the agents and fleets delivered.",
    },
    "quickstart": (
        "Requirements: Docker with Docker Compose.\n\n"
        "```bash\n"
        "git clone https://github.com/XTM-One-Platform/xtm-one.git && cd xtm-one\n"
        "cp .env.sample .env      # set SECRET_KEY, DB_PASSWORD, ADMIN_EMAIL and ADMIN_PASSWORD\n"
        "docker compose up -d\n"
        "```\n\n"
        "Open http://localhost:4000, sign in with `ADMIN_EMAIL` and `ADMIN_PASSWORD`, add an AI provider in "
        "**Settings** (or run without AI), then register your OpenCTI, OpenAEV and OpenCRQ instances.\n\n"
        "- **Docker**: [`xtmone/platform`](https://hub.docker.com/r/xtmone/platform) and "
        "[`xtmone/worker`](https://hub.docker.com/r/xtmone/worker), one tag per "
        "[release](https://github.com/XTM-One-Platform/xtm-one/releases).\n"
        "- **Helm**: [Kubernetes and Helm](https://docs.xtmone.io/latest/deployment/installation/#kubernetes-and-helm).\n"
        "- **Docs**: [installation](https://docs.xtmone.io/latest/deployment/installation/), "
        "[configuration](https://docs.xtmone.io/latest/deployment/configuration/) and "
        "[upgrade](https://docs.xtmone.io/latest/deployment/upgrade/)."
    ),
    "editions": (
        "| | Community Edition | Enterprise Edition |\n|---|---|---|\n"
        "| License | [Apache 2.0](LICENSE) | [XTM One Enterprise Edition License](LICENSE) |\n"
        "| CTEM command center, stage metrics, remediation cases | Yes | Yes |\n"
        "| Agentic chat, agents, fleets, flows, assignments | | Yes |\n"
        "| Knowledge bases, skills, MCP servers, custom tools, A2A | | Yes |\n"
        "| Exposure briefing and stage assessments | | Yes |\n\n"
        "The Community Edition runs with AI off. Enterprise Edition is active with a Filigran license, or once "
        "a connected OpenCTI, OpenAEV or OpenCRQ platform is in Enterprise Edition "
        "([editions and licensing](https://docs.xtmone.io/latest/administration/editions-and-licensing/))."
    ),
    "integrations": [
        ("XTM Platform:", "OpenCTI, OpenAEV and OpenCRQ through their APIs and events; their native MCP servers "
         "are served by XTM One."),
        ("[XTM Hub](https://hub.filigran.io):", "integrations, dashboards and scenarios for the XTM Platform."),
        ("Ecosystem:", "threat intelligence, detection and response, exposure and validation tools through the "
         "[integration catalog](https://docs.xtmone.io/latest/)."),
        ("MCP and A2A:", "connect remote MCP servers as agent tools, expose XTM One to MCP clients and to other "
         "agents over A2A."),
    ],
    "license_text": ("XTM One is open source under the [Apache 2.0 license](LICENSE); the Enterprise Edition "
                     "features are under the XTM One Enterprise Edition License, as stated in [LICENSE](LICENSE) "
                     "and in each source file header."),
}
