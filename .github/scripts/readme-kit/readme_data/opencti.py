"""OpenCTI fills the README template (doc 33 v3). Copy carried over from the v2
README of OpenCTI-Platform/opencti (issue/20179-readme-ctem, #20182), reshaped to
the 13 sections; gallery and walkthrough captures from the lab on synthetic data."""

DATA = {
    "key": "opencti",
    "name": "OpenCTI",
    "repo": "OpenCTI-Platform/opencti",
    "page": "https://filigran.io/products/opencti",
    "docs": "https://docs.opencti.io",
    "docs_host": "docs.opencti.io",
    "docker": "opencti/platform",
    "ci": ("ci-main.yml", "master"),
    "license_badge": "Apache 2.0 + EE",
    "tagline": "The open source threat intelligence platform at the heart of Continuous Threat Exposure Management.",
    "pitch": (
        "OpenCTI turns cyber threat intelligence into a living knowledge graph: who targets you, how they operate, "
        "which vulnerabilities and tools they rely on, and what your defenses already cover. It is the threat "
        "intelligence foundation of CTEM in the XTM Platform: know the adversary in OpenCTI, validate your exposure "
        "to it with [OpenAEV](https://github.com/OpenAEV-Platform/openaev), quantify the risk with "
        "[OpenCRQ](https://github.com/OpenCRQ-Platform/opencrq), and drive the whole loop with "
        "[XTM One](https://github.com/XTM-One-Platform/xtm-one)."
    ),
    "hero_alt": ("The OpenCTI home dashboard: the latest reports, the most active threats and the targeted sectors, "
                 "the indicators ingested and the ATT&CK techniques observed (synthetic data)"),
    "why_intro": ("Exposure management starts with the threat. Before you can decide which exposure to fix first, "
                  "you need to know which adversaries matter to your sector and region, which techniques they use "
                  "and which vulnerabilities they exploit in the wild."),
    "why": [
        ("Structured, not scattered.", "Every report, feed and analyst note lands in one STIX 2.1 knowledge graph, "
         "each fact linked to its source, its confidence and its first and last seen dates."),
        ("Actionable for CTEM.", "Attack patterns, vulnerabilities and indicators carry straight into validation "
         "(OpenAEV), risk quantification (OpenCRQ), detection and the agents of XTM One."),
        ("Open by design.", "An Apache 2.0 core, a GraphQL API, a Python client and a large catalog of connectors "
         "to the tools you already run."),
    ],
    "capabilities": {
        "Scoping": "Priority intelligence requirements, sectors, regions and the threats that target them, scored "
                   "and surfaced so the program starts from the adversaries that matter.",
        "Discovery": "Threat actors, campaigns, malware, tools, techniques, vulnerabilities and indicators in one "
                     "STIX 2.1 graph, fed by TAXII, RSS, CSV, JSON and hundreds of connectors.",
        "Prioritization": "Vulnerabilities ranked by the adversaries that exploit them, inference rules that "
                          "derive targeting and attribution, decay rules that age indicators out.",
        "Validation": "The ATT&CK matrix of any threat and its security coverage, with the OpenAEV counterpart of "
                      "every shared object and its simulation results written back.",
        "Mobilization": "Cases, playbooks, dashboards, STIX, CSV and PDF exports, live streams and TAXII "
                        "collections that put the knowledge into the hands of every team.",
    },
    "gallery": [
        ("Knowledge graph", "Threats, techniques, targets and vulnerabilities, explorable in one investigation.",
         "An OpenCTI investigation graph: four ransomware intrusion sets linked to the techniques, malware, "
         "vulnerabilities, sectors and countries they share"),
        ("Threat actor", "Who the adversary is, what it targets and how it operates, on one page.",
         "An OpenCTI intrusion set overview: aliases, targeted sectors and regions, latest reports and activity"),
        ("ATT&CK matrix", "The techniques of a threat on the MITRE ATT&CK matrix, with their coverage.",
         "The MITRE ATT&CK matrix of an intrusion set in OpenCTI, the techniques it uses highlighted"),
        ("Dashboards", "Widgets on any query, variables and presets, shared publicly when needed.",
         "An OpenCTI custom dashboard with time series, distribution and list widgets on threat activity"),
        ("Playbooks", "Qualify, enrich and route what you ingest in a visual editor.",
         "The OpenCTI playbook editor: a flow of steps that filters, enriches and routes incoming knowledge"),
        ("Vulnerability context", "Which threats exploit a CVE and which malware carries it, before you patch.",
         "An OpenCTI vulnerability with the intrusion sets and malware that exploit it, their authors, labels "
         "and markings"),
    ],
    "walkthrough": "From a report to its knowledge graph, the threat behind it and its ATT&CK coverage, in 15 seconds.",
    "walkthrough_alt": ("Animated walkthrough: an OpenCTI report, its knowledge graph, the intrusion set behind it "
                        "and its ATT&CK matrix"),
    "how": (
        "OpenCTI ingests feeds, reports and connector data into a STIX 2.1 knowledge graph stored in Elasticsearch "
        "or OpenSearch, with Redis, RabbitMQ and an S3-compatible store. Workers apply inference and decay rules, "
        "playbooks route the knowledge, and the GraphQL API, live streams and TAXII collections serve it to "
        "OpenAEV, OpenCRQ, XTM One and every integration."
    ),
    "flow_alt": ("How OpenCTI works: feeds, reports and connectors flow into the STIX 2.1 knowledge graph, which "
                 "serves OpenAEV, OpenCRQ, XTM One, detection tools and exports"),
    "roles": {
        "CTI": "One knowledge graph for every report, feed and note, with source, confidence and markings on "
               "every fact, and reports generated from a container, a threat or a victim.",
        "SOC": "Indicators and observables with their context, sightings, and live streams and TAXII collections "
               "to the SIEM and the EDR.",
        "Detection engineering": "Indicators translated between STIX, Sigma and YARA, and the ATT&CK coverage of "
                                 "every threat to decide what to detect next.",
        "Red and purple team": "The techniques of the adversaries that target you, sent to OpenAEV as scenarios "
                               "and read back as security coverage.",
        "Vulnerability and exposure management": "Vulnerabilities linked to the threats and campaigns that exploit "
                                                 "them, so the exploited ones come first.",
        "Risk and CISO": "Priority intelligence requirements, dashboards on the threat landscape and the "
                         "intelligence OpenCRQ prices into expected loss.",
    },
    "quickstart": (
        "Requirements: Docker with Docker Compose.\n\n"
        "```bash\n"
        "git clone https://github.com/OpenCTI-Platform/docker.git opencti-docker && cd opencti-docker\n"
        "cp .env.sample .env      # replace every changeme and ChangeMe value before the first start\n"
        "docker compose up -d\n"
        "```\n\n"
        "Open http://localhost:8080 and sign in with the admin account of `.env`. The stack also starts XTM One "
        "next to OpenCTI.\n\n"
        "- **Docker**: [`opencti/platform`](https://hub.docker.com/r/opencti/platform), one tag per "
        "[release](https://github.com/OpenCTI-Platform/opencti/releases).\n"
        "- **Helm and Terraform**: [installation guide](https://docs.opencti.io/latest/deployment/installation/).\n"
        "- **Demonstration**: [demo.opencti.io](https://demo.opencti.io), reset every night on reference data."
    ),
    "editions": (
        "| | Community Edition | Enterprise Edition |\n|---|---|---|\n"
        "| License | [Apache 2.0](LICENSE) | [OpenCTI Enterprise Edition License](LICENSE) |\n"
        "| Knowledge graph, investigations, dashboards, connectors | Yes | Yes |\n"
        "| Playbooks and priority intelligence requirements | | Yes |\n"
        "| AI: reports, file to STIX, natural language search, Ariane | | Yes |\n\n"
        "The [documentation](https://docs.opencti.io) marks every Enterprise Edition feature; to get a license, "
        "[contact Filigran](https://filigran.io/contact)."
    ),
    "integrations": [
        ("XTM Platform:", "OpenAEV scenarios from OpenCTI threats and coverage written back, OpenCRQ reads the "
         "intelligence, XTM One powers Ariane and the agents."),
        ("[XTM Hub](https://hub.filigran.io/cybersecurity-solutions/open-cti-integrations):", "the connector "
         "catalog, dashboards and feeds for OpenCTI."),
        ("Connectors:", "import, enrichment and export connectors in "
         "[OpenCTI-Platform/connectors](https://github.com/OpenCTI-Platform/connectors)."),
        ("Standards and APIs:", "STIX 2.1, TAXII 2.1, the GraphQL API and the "
         "[Python client](https://github.com/OpenCTI-Platform/client-python)."),
    ],
    "license_text": ("OpenCTI is open source under the [Apache 2.0 license](LICENSE); the Enterprise Edition "
                     "features are under the OpenCTI Enterprise Edition License, as stated in [LICENSE](LICENSE) "
                     "and in each source file header."),
}
