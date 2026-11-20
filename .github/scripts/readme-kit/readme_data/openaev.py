"""OpenAEV fills the README template (doc 33 v3). Copy carried over from the v2
README of OpenAEV-Platform/openaev (docs/9473-readme-ctem, #9474), reshaped to
the 13 sections; gallery and walkthrough captures from the lab on synthetic data."""

DATA = {
    "key": "openaev",
    "name": "OpenAEV",
    "repo": "OpenAEV-Platform/openaev",
    "page": "https://filigran.io/products/openaev",
    "docs": "https://docs.openaev.io",
    "docs_host": "docs.openaev.io",
    "docker": "openaev/platform",
    "ci": ("core-ci.yml", "main"),
    "license_badge": "Apache 2.0",
    "tagline": "The open source adversarial exposure validation platform: prove which threats get through your defenses.",
    "pitch": (
        "OpenAEV turns threat intelligence from [OpenCTI](https://github.com/OpenCTI-Platform/opencti) into attack "
        "scenarios, runs them against your real endpoints, cloud and security stack, and tells you which techniques "
        "were prevented, which were detected and which got through. It is the validation stage of CTEM in the XTM "
        "Platform: validated exposure is priced by [OpenCRQ](https://github.com/OpenCRQ-Platform/opencrq) and "
        "taken to a verified fix by [XTM One](https://github.com/XTM-One-Platform/xtm-one)."
    ),
    "hero_alt": ("The OpenAEV home: the exposure command center with assets at risk, the adversary, vulnerability, "
                 "prevention, detection and breached stages and their scores (synthetic data)"),
    "why_intro": ("Most security teams know their vulnerabilities and their threat landscape. Few can say, with "
                  "evidence, whether their controls stop the techniques the adversaries that target them use."),
    "why": [
        ("Threat-informed.", "Scenarios come from the intrusion sets, campaigns, reports and attack patterns in "
         "OpenCTI, not from a generic checklist."),
        ("Real execution, safe by design.", "Payloads run through agents and implants on your own endpoints, inside "
         "the scope you define, and every action expects a prevention and a detection."),
        ("Exposure you can act on.", "Results become findings and gaps with detection and remediation guidance, "
         "tracked from one run to the next."),
    ],
    "capabilities": {
        "Scoping": "Assets, asset groups and endpoints in scope, and the threats from OpenCTI security coverage "
                   "that the scenarios test.",
        "Discovery": "CVEs, open ports, credentials, shares and Active Directory weaknesses extracted from execution "
                     "output, deduplicated across runs and enriched with CVSS.",
        "Prioritization": "The exploration graph and attack path map show which findings an attacker chains to "
                          "reach what matters, chokepoints first.",
        "Validation": "Breach and attack simulation, atomic tests and attack chaining on Windows, Linux and macOS, "
                      "with prevention and detection expectations scored by collectors.",
        "Mobilization": "Detection and remediation guidance on every gap, results written back to OpenCTI as "
                        "security coverage, and dashboards for each audience.",
    },
    "gallery": [
        ("Scenarios", "Injects on network and endpoints, built from the techniques and CVEs of a threat.",
         "A CVE and TTP validation scenario with its injects: Nuclei CVE scans on the network and payloads on "
         "endpoints"),
        ("Threat arsenal", "MITRE ATT&CK mapped actions by domain and platform, ready to run as a test.",
         "The threat arsenal: MITRE ATT&CK mapped actions filtered by domain, platform and status"),
        ("Exploration graph", "Every asset, finding and technique of a run, and how they connect.",
         "The OpenAEV exploration graph: endpoints, findings and techniques of a simulation linked in one graph"),
        ("Estate overview", "Every asset group of the estate, sized and coloured by how often attacks got through.",
         "The estate overview graph: asset groups sized by asset count and coloured by failure rate, linked to the "
         "scenarios that tested them"),
        ("Custom dashboards", "Widgets over simulations, findings and coverage, for each audience.",
         "A custom dashboard with the exposure command center widget and prevention, detection and vulnerability "
         "breakdowns"),
        ("Findings", "Every CVE and exposure a run surfaced, tied to the asset it was found on.",
         "The findings of a simulation: CVEs found by Nuclei scans, each tied to the endpoint it was found on"),
    ],
    "walkthrough": ("From the exposure command center to a scenario, its execution and the findings it produced, "
                    "in 15 seconds."),
    "walkthrough_alt": ("Animated walkthrough: the exposure command center, a scenario and its injects, the threat "
                        "arsenal, the exploration graph, the estate view, a custom dashboard and the findings"),
    "how": (
        "OpenAEV builds scenarios from OpenCTI security coverage and the XTM Hub scenarios library, runs them "
        "through the OpenAEV agent and implant, Caldera or the EDR executors, and reaches network, cloud, web and "
        "email through injectors. Collectors read your EDR, SIEM and XDR to score each expectation; findings, "
        "scores and remediation go back to OpenCTI, OpenCRQ and XTM One."
    ),
    "flow_alt": ("How OpenAEV works: OpenCTI threats and XTM Hub scenarios flow into scenarios run by agents and "
                 "executors on your environment, scored by collectors into findings and remediation"),
    "roles": {
        "CTI": "Threat intelligence that becomes a test: intrusion sets, campaigns and reports from OpenCTI turned "
               "into scenarios, results written back as security coverage.",
        "SOC": "Proof of which techniques the EDR, SIEM and XDR detect, scored automatically by collectors after "
               "every run.",
        "Detection engineering": "Detection remediation on every undetected technique, so each gap comes with the "
                                 "rule that closes it.",
        "Red and purple team": "Atomic tests, attack chaining and the autonomous attack path orchestrator of XTM "
                               "One against your authorized scope.",
        "Vulnerability and exposure management": "Findings from execution output with CVSS, and the attack path "
                                                 "that shows which of them an attacker can chain.",
        "Risk and CISO": "Prevention, detection and vulnerability scores over time and the validated exposure "
                         "OpenCRQ prices into expected loss.",
    },
    "quickstart": (
        "Requirements: Docker with Docker Compose.\n\n"
        "```bash\n"
        "git clone https://github.com/OpenAEV-Platform/docker.git openaev-docker && cd openaev-docker\n"
        "cp .env.sample .env      # set the admin credentials, tokens and passwords\n"
        "docker compose -p openaev -f docker-compose.yml up -d\n"
        "```\n\n"
        "Open the URL set by `OPENAEV_HOST` and `OPENAEV_PORT` in `.env`, install an agent on a test endpoint and "
        "launch your first atomic test. The stack also starts XTM One next to OpenAEV.\n\n"
        "- **Docker**: [`openaev/platform`](https://hub.docker.com/r/openaev/platform), one tag per "
        "[release](https://github.com/OpenAEV-Platform/openaev/releases).\n"
        "- **Helm**: the [installation guide](https://docs.openaev.io/latest/deployment/platform/installation/).\n"
        "- **The whole XTM Platform**: [FiligranHQ/xtm-docker](https://github.com/FiligranHQ/xtm-docker)."
    ),
    "editions": (
        "| | Community Edition | Enterprise Edition |\n|---|---|---|\n"
        "| License | [Apache 2.0](LICENSE) | [OpenAEV Enterprise Edition License](LICENSE) |\n"
        "| Scenarios, simulations, atomic tests, findings, dashboards | Yes | Yes |\n"
        "| Generative AI and the autonomous attack path | | Yes |\n"
        "| Multi-tenancy | | Yes |\n\n"
        "Enable the Enterprise Edition in **Settings > Filigran Experience** with a license; the "
        "[documentation](https://docs.openaev.io/latest/administration/enterprise/) lists what it adds."
    ),
    "integrations": [
        ("XTM Platform:", "OpenCTI threats in and security coverage back, validated exposure to OpenCRQ, "
         "remediation and the autonomous attack path with XTM One."),
        ("[XTM Hub](https://hub.filigran.io/cybersecurity-solutions/openaev-scenarios):", "the OpenAEV scenarios "
         "library and integrations, deployed in one click."),
        ("Execution:", "the OpenAEV [agent](https://github.com/OpenAEV-Platform/agent) and "
         "[implant](https://github.com/OpenAEV-Platform/implant), Caldera, CrowdStrike Falcon, SentinelOne, "
         "Microsoft Defender for Endpoint, Tanium and Palo Alto Cortex."),
        ("Scoring:", "[collectors](https://github.com/OpenAEV-Platform/collectors) for your EDR, SIEM and XDR, and "
         "[injectors](https://github.com/OpenAEV-Platform/injectors) for network, cloud, web and email."),
    ],
    "license_text": ("OpenAEV is open source under the [Apache 2.0 license](LICENSE); the Enterprise Edition "
                     "features are under the OpenAEV Enterprise Edition License, as stated in [LICENSE](LICENSE) "
                     "and in each source file header."),
}
