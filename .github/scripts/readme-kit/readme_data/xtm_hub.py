"""XTM Hub fills the README template (doc 33 v3). Copy carried over from the v2
README of XTM-Hub/xtm-hub (main after #4382), reshaped to the 13 sections with
no Mermaid; gallery and walkthrough captures from hub.filigran.io style lab data."""

DATA = {
    "key": "xtm-hub",
    "name": "XTM Hub",
    "repo": "XTM-Hub/xtm-hub",
    "page": "https://filigran.io/platforms/xtm-hub/",
    "docs": "https://docs.hub.filigran.io",
    "docs_host": "docs.hub.filigran.io",
    "docker": "",
    "license_badge": "source available",
    "tagline": "The tradecraft library and gateway of the XTM Platform: CTEM content you deploy in one click into your own OpenCTI and OpenAEV.",
    "pitch": (
        "[XTM Hub](https://hub.filigran.io) gives security teams ready-made tradecraft for Continuous Threat Exposure "
        "Management (CTEM): [OpenCTI](https://github.com/OpenCTI-Platform/opencti) dashboards, custom views, "
        "integrations and playbooks to know the adversary and map the exposure, and "
        "[OpenAEV](https://github.com/OpenAEV-Platform/openaev) scenarios to prove your defenses against real "
        "attacker techniques. Connect your instance once, then deploy any resource into it in one click."
    ),
    "hero_alt": ("XTM Hub home: the XTM Platform overview, the public roadmap and the newest and most deployed "
                 "OpenCTI dashboards, OpenAEV scenarios and OpenCTI integrations"),
    "why_intro": ("A CTEM program runs on tradecraft: dashboards, integrations, playbooks and attack scenarios. "
                  "Each one takes days to build, and most teams rebuild what another team already got right."),
    "why": [
        ("A shared library, curated.", "Dashboards, custom views, integrations, playbooks and scenarios, "
         "searchable by use case, type and compatibility."),
        ("One click to your instance.", "A deployment sends the resource straight to the OpenCTI or OpenAEV you "
         "pick, with no export, no file to move and no manual import."),
        ("One gateway for the platform.", "Instance registration for OpenCTI, OpenAEV and XTM One, the public "
         "roadmap with feature voting and personal API tokens."),
    ],
    "capabilities": {
        "Scoping": "OpenCTI custom dashboards for sectors and threat topics: the actors, malware, techniques and "
                   "vulnerabilities that target organizations like yours.",
        "Discovery": "OpenCTI integrations (threat feeds, enrichment, third-party tools), filtered by use case, "
                     "integration type, deployment type and license.",
        "Prioritization": "OpenCTI custom views that put the threats and exposures that matter in front of each "
                          "analyst.",
        "Validation": "OpenAEV scenarios by category: atomic testing, attack simulation, purple team, tabletop "
                      "exercises, OpenCTI coverage and the CTEM framework.",
        "Mobilization": "OpenCTI playbooks that automate triage and response, deployed in one click to OpenCTI "
                        "Enterprise Edition.",
    },
    "gallery": [
        ("Dashboards library", "Start from sector and threat dashboards instead of an empty workspace.",
         "The OpenCTI custom dashboards library: sector and threat dashboards as cards with search, sort and use "
         "case filters"),
        ("One-click deploy", "Read what a dashboard shows, then send it to your OpenCTI.",
         "A banking sector dashboard in the library with its description, author and the Deploy in OpenCTI action"),
        ("Scenarios library", "Attack scenarios by category to validate your controls in OpenAEV.",
         "The OpenAEV scenarios library with category filters for atomic testing, attack simulation, purple team "
         "and tabletop exercises"),
        ("Scenario detail", "The objective and compatibility of a test before you run it.",
         "A Linux privilege escalation scenario with its objective, compatibility version and the Deploy in "
         "OpenAEV action"),
        ("Integrations catalog", "Find the feed or enrichment that fills a gap in your intelligence.",
         "The OpenCTI integrations catalog filtered by compatibility version, use case, integration type, "
         "deployment type, status and license"),
        ("Playbooks library", "Automation you apply as is or adapt to your workflow.",
         "The OpenCTI playbooks library with ready-to-use automation playbooks as cards"),
    ],
    "walkthrough": "From the library to a dashboard deployed in your OpenCTI, in 15 seconds.",
    "walkthrough_alt": ("Animated walkthrough: the XTM Hub home, the dashboards library, a dashboard detail and "
                        "its one-click deployment to a connected OpenCTI"),
    "how": (
        "XTM Hub serves curated content to the instances you connect. Registration links an OpenCTI, OpenAEV or XTM "
        "One instance to your organization; a deployment sends the resource straight to the instance you pick. "
        "Under the hood: a GraphQL API (Express, Apollo Server, PostgreSQL, Elasticsearch, S3-compatible storage) "
        "and a Next.js web application, in one TypeScript monorepo."
    ),
    "flow_alt": ("How XTM Hub works: dashboards, integrations, playbooks and scenarios flow through one-click "
                 "deployment into your registered OpenCTI, OpenAEV and XTM One instances"),
    "roles": {
        "CTI": "Sector dashboards and curated feeds to start from, deployed into OpenCTI in one click.",
        "SOC": "Enrichment integrations and custom views that put the alerts that matter in front of each analyst.",
        "Detection engineering": "Playbooks and integrations that route intelligence to the detection stack.",
        "Red and purple team": "Ready-made OpenAEV attack scenarios by category, from atomic tests to purple "
                               "team exercises.",
        "Vulnerability and exposure management": "Scenarios that test the techniques that reach your assets and "
                                                 "dashboards on the vulnerabilities exploited in the wild.",
        "Risk and CISO": "One library shared across every instance you run, and the public XTM Platform roadmap.",
    },
    "quickstart": (
        "**Use it**: XTM Hub runs as a service at [hub.filigran.io](https://hub.filigran.io). Sign in, open a "
        "library and connect your instance from the first deployment.\n\n"
        "**Run it locally**: Node from [`.nvmrc`](.nvmrc) and Yarn from `packageManager` in "
        "[`package.json`](package.json).\n\n"
        "```bash\n"
        "corepack enable && yarn install\n"
        "docker compose -f xtm-hub-dev/docker-compose.yml up -d   # PostgreSQL, S3, Elasticsearch, Mailpit\n"
        "yarn dev:api && yarn dev:front                           # API on :4002, web application on :3002\n"
        "```\n\n"
        "- **Docs**: [registration, deployment and API tokens](https://docs.hub.filigran.io).\n"
        "- **Helm**: the chart used to deploy the Hub is in [`chart/`](chart).\n"
        "- **Contributors**: validation commands and coding rules in [AGENTS.md](AGENTS.md)."
    ),
    "editions": (
        "| | Free service | Source code |\n|---|---|---|\n"
        "| License | [hub.filigran.io](https://hub.filigran.io) terms | [Source available](LICENSE) |\n"
        "| Libraries, one-click deploy, registration, roadmap | Yes | Read, fork for personal use |\n"
        "| Production and commercial use of the code | | Not permitted |\n\n"
        "Some content depends on the edition of the product it deploys to: one-click deployment of playbooks "
        "needs OpenCTI Enterprise Edition."
    ),
    "integrations": [
        ("XTM Platform:", "OpenCTI, OpenAEV and XTM One register with the Hub and receive its content in one click."),
        ("[Libraries](https://hub.filigran.io):", "OpenCTI dashboards, custom views, integrations and playbooks, "
         "and OpenAEV scenarios in one place."),
        ("Connectors:", "the [OpenCTI connectors](https://github.com/OpenCTI-Platform/connectors) are listed in "
         "the integrations catalog."),
        ("API:", "personal API tokens (read, read and write, admin) with a lifetime for your scripts and pipelines."),
    ],
    "license_text": "XTM Hub is source available under the terms of [LICENSE](LICENSE).",
}
