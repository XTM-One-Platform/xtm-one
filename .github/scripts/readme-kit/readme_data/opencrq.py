"""OpenCRQ fills the README template (doc 33 v3). Copy carried over from the v2
README of OpenCRQ-Platform/opencrq (main after #3250), reshaped to the 13
sections with no Mermaid; gallery and walkthrough captures from the lab on
synthetic data."""

DATA = {
    "key": "opencrq",
    "name": "OpenCRQ",
    "repo": "OpenCRQ-Platform/opencrq",
    "page": "https://filigran.io/products/opencrq",
    "docs": "https://github.com/OpenCRQ-Platform/opencrq/tree/main/docs",
    "docs_host": "in the repository",
    "docker": "filigran/opencrq",
    "ci": ("ci.yml", "main"),
    "private": True,  # private repository: static stars and release badges
    "license_badge": "Apache 2.0",
    "tagline": "Threat-informed cyber risk quantification: know what each exposure would cost you, and fix what removes the most loss.",
    "pitch": (
        "OpenCRQ turns the threats you know in [OpenCTI](https://github.com/OpenCTI-Platform/opencti), the defenses "
        "you prove in [OpenAEV](https://github.com/OpenAEV-Platform/openaev) and the assets and controls you run into "
        "the financial loss to expect, risk scenario by risk scenario. It is the prioritization stage of CTEM in the "
        "XTM Platform: one number per risk, with its range and its causes, so the next control, patch or budget line "
        "goes where it removes the most loss, and [XTM One](https://github.com/XTM-One-Platform/xtm-one) gets the "
        "fix done."
    ),
    "hero_alt": ("The OpenCRQ home dashboard: the risk scope with its tenant, scopes and scenarios, and the "
                 "controls that would reduce the annual loss exposure most, each priced (synthetic data)"),
    "why_intro": ("A CTEM program finds more exposures than any team can fix. Severity scores rank them by how bad a "
                  "flaw is in general, not by what it would cost your organization."),
    "why": [
        ("Threat-informed and evidence-based.", "Each risk scenario starts from the actors that target your sector "
         "and geography, and control effectiveness comes from what your attack simulations blocked or detected."),
        ("Financial.", "Results are an annualized loss exposure with its range, split by loss driver, so they read "
         "in the language of a board and a budget."),
        ("Actionable.", "Every result names the control, the finding or the technique that would reduce it most, "
         "priced in the same currency."),
    ],
    "capabilities": {
        "Scoping": "Risk scopes per legal entity or business line, primary assets that carry the loss and the "
                   "organization's financial profile, sector and countries.",
        "Discovery": "Supporting assets with their dependencies, consolidated across sources, CVE and technique "
                     "findings, and the threats relevant to each tenant.",
        "Prioritization": "A risk scenario per attack outcome quantified by Monte Carlo: annualized loss exposure "
                          "with p10, mean and p90 and a loss exceedance curve against your tolerance.",
        "Validation": "Controls mapped to MITRE ATT&CK techniques with their prevention and detection coverage, "
                      "their effectiveness taken from OpenAEV simulation results.",
        "Mobilization": "Risk treatment recommendations, treatment plans, budget reviews and a remediation pace "
                        "dashboard of new findings against closing capacity.",
    },
    "gallery": [
        ("Risk scenarios", "Every risk you carry, priced and ranked, with its range, outcome and owner.",
         "Risk scenarios of a fictitious bank: each scenario with its annual loss exposure, its p10 to p90 range, "
         "its attack outcome, its targets and its owner"),
        ("Quantification", "How attacks end against your defenses and what a loss event costs.",
         "Quantification of a data breach scenario: attack run outcomes, loss event frequency and annualized loss "
         "expectancy with p10, mean and p90"),
        ("Budget reviews", "What the security budget spent and the loss it avoided, scope by scope.",
         "Budget reviews of a fictitious bank: scenarios outside tolerance, loss avoided per year, build return "
         "and spend, with one review per period"),
        ("Exposure flow", "Where the annual loss comes from, down to the technique that would stop it.",
         "Risk exposure flow: a Sankey diagram of annual loss expectancy from threat actors to attack outcomes, "
         "technique chokepoints and loss drivers"),
        ("Controls", "Each control's strength, coverage and measured effectiveness, and what it protects.",
         "Controls grouped by domain with their strength, ATT&CK coverage and measured effectiveness"),
        ("Threats", "The actors and campaigns that match your attack outcomes, with their CVEs and techniques.",
         "Threats read from OpenCTI: active threats, their match with the tenant's attack outcomes, observed "
         "activity, CVEs and techniques"),
    ],
    "walkthrough": "From the home dashboard to a risk scenario, its quantification and the control that cuts it, in 15 seconds.",
    "walkthrough_alt": ("Animated walkthrough: the OpenCRQ home dashboard, a risk scenario, its Monte Carlo "
                        "quantification and the treatment that reduces it"),
    "how": (
        "OpenCRQ reads threats from an OpenCTI live stream and assets, verdicts and CVEs from OpenAEV, builds one "
        "risk scenario per attack outcome on a primary asset and runs a Monte Carlo simulation in the FAIR "
        "vocabulary: threat event frequency, attack runs across the supporting assets, loss priced through the "
        "loss drivers, and what-ifs on the same simulated worlds so the value of each lever carries no sampling noise."
    ),
    "flow_alt": ("How OpenCRQ works: OpenCTI threats, OpenAEV verdicts, assets, controls and the financial profile "
                 "flow into risk scenarios and the quantification engine, which produces financial risk, "
                 "prioritized remediation and executive reporting"),
    "roles": {
        "CTI": "The actors and campaigns that drive the loss, and the techniques that are the chokepoints.",
        "SOC": "The techniques where a detection removes the most loss, from OpenAEV verdicts priced in the same "
               "currency.",
        "Detection engineering": "A ranking of undetected techniques by the loss a detection would remove.",
        "Red and purple team": "Attack simulation results turned into control effectiveness and a priced risk "
                               "reduction.",
        "Vulnerability and exposure management": "Findings ranked by the loss they carry on your estate, not by a "
                                                 "generic severity score.",
        "Risk and CISO": "The annual loss exposure for the board, a tolerance and its trend, treatment plans and "
                         "the spend justified by the loss it avoids.",
    },
    "quickstart": (
        "Requirements: Docker with Docker Compose.\n\n"
        "```bash\n"
        "git clone https://github.com/OpenCRQ-Platform/opencrq.git && cd opencrq/docker\n"
        "cp .env.sample .env      # set XTM_ONE__URL, XTM_ONE__TOKEN and XTM_ONE__PLATFORM_URL\n"
        "docker compose up --build --pull always -d\n"
        "```\n\n"
        "Open http://localhost:3020, then connect OpenCTI and OpenAEV from the connectors guides.\n\n"
        "- **Guides**: [quick start](docs/quick-start.md) and [Docker deployment](docs/deployment/docker.md).\n"
        "- **Docker**: [`filigran/opencrq`](https://hub.docker.com/r/filigran/opencrq).\n"
        "- **Helm**: not available as a public chart; run the Docker image on Kubernetes with your own "
        "manifests and the [configuration](docs/deployment/configuration.md) reference.\n"
        "- **Configuration**: [configuration](docs/deployment/configuration.md) and "
        "[architecture](docs/deployment/architecture.md).\n"
        "- **Releases**: the [GitHub releases page](https://github.com/OpenCRQ-Platform/opencrq/releases)."
    ),
    "editions": (
        "| | Community Edition | Enterprise Edition |\n|---|---|---|\n"
        "| License | [Apache 2.0](LICENSE) | [OpenCRQ Enterprise Edition License](LICENSE) |\n"
        "| Risk scenarios, quantification, controls, dashboards | Yes | Yes |\n"
        "| Multi-tenancy and the Filigran license | | Yes |\n"
        "| The XTM One assistant | | Yes |\n\n"
        "A source file without a license header belongs to the Community Edition; see [LICENSE](LICENSE)."
    ),
    "integrations": [
        ("XTM Platform:", "OpenCTI threats through its [connector](docs/deployment/opencti-connector.md), OpenAEV "
         "assets and verdicts through its [connector](docs/deployment/openaev-connector.md), XTM One for Ariane "
         "and sign-in."),
        ("[XTM Hub](https://hub.filigran.io):", "integrations, dashboards and scenarios for the XTM Platform."),
        ("Vulnerability data:", "NIST NVD, CISA known exploited vulnerabilities and FIRST EPSS."),
        ("Estate and compliance:", "Microsoft Defender for Endpoint, Entra ID and Intune through Microsoft Graph, "
         "and Drata for NIST CSF 2.0 evidence."),
    ],
    "license_text": ("OpenCRQ is open source under the [Apache 2.0 license](LICENSE); the Enterprise Edition "
                     "features are under the OpenCRQ Enterprise Edition License, as stated in [LICENSE](LICENSE) "
                     "and in each source file header."),
}
