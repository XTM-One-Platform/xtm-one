# Agent Interoperability — Literature Review

**Date:** 2026-08-13
**Status:** Complete — feeds the scoring in `agent-interop-benchmark.md`
**Scope:** External / third-party agent communication only. XTM One's internal handover/delegate/consult/transfer routing is out of scope.

> A survey of the cross-vendor agent-interoperability space as of August 2026, assembled as the evidence base for a go/no-go on A2A v1.0. This document establishes *what exists and what is true*. The scoring and the recommendation live in the companion benchmark document.

**Contents:** §1 Method · §2 Source register · §3 A2A v1.0 · §4 Other candidates · §5 Adoption evidence · §6 Inventory and exclusions · §7 Conformance tooling · §8 Demand signal · §9 Open evidence gaps · §10 Sources

---

## 1. Method

Two rules were applied while gathering evidence, because the issue's functional requirements turn on them.

**Rule 1 — capability claims carry a version and a date.** Every statement about what a protocol does cites a specification document, its version, and the date it was published or accessed. The space moves fast enough that an undated claim is worthless within a quarter.

**Rule 2 — adoption evidence is separated from announcement.** A claim is recorded as *verified production* only when there is a named organisation, a named product surface, and a source that is **not** a launch press release — vendor product documentation, a changelog, or a public repository. Everything else stays in the *announced* column, including foundation press releases and "N organisations support us" counts. Section 5 keeps the two apart on purpose.

Where evidence was not found, this document says `no evidence found` rather than inferring.

### 1.1 A note on secondary-source date drift

Public secondary sources actively disagree on when A2A v1.0 shipped: January, March, and April 2026 all circulate in blog posts and analyst write-ups. The primary source — the release history in the `a2aproject/A2A` repository — gives **v1.0.0 on 2026-03-12**. That date is used throughout. This is a concrete illustration of why Rule 1 exists.

### 1.2 Three interoperability axes

Most published comparisons in this space compare things that do not compete. Three distinct questions get called "agent interoperability":

| Axis | Question | XTM One status |
| --- | --- | --- |
| **Definition portability** | Can another platform read my agent's *definition*? | **Shipped** — Microsoft AgentSchema import/export (`dev-docs/agentschema-format.md`) |
| **Packaging / distribution** | Can I *ship* a skill or tool bundle across platforms? | Tracked separately (agent-plugins.org) |
| **Runtime wire protocol** | Can my agent *task another vendor's running agent*, live? | **Nothing. Greenfield.** |

Only the third axis is the subject of the A2A evaluation request and of this study. Candidates are admitted or excluded on that basis in §6.

---

## 2. Source register

Accessed 2026-08-13 unless otherwise noted.

| # | Source | Publisher | Date | Version | Access |
| --- | --- | --- | --- | --- | --- |
| S1 | [A2A Protocol Specification](https://a2a-protocol.org/latest/specification/) | A2A Project / Linux Foundation | accessed 2026-08-13 | 1.0.0 | Open |
| S2 | [a2aproject/A2A release history](https://github.com/a2aproject/A2A/releases) | A2A Project | v1.0.0 2026-03-12; v1.0.1 2026-05-28; v0.3.0 2025-07-30 | — | Open |
| S3 | [LF launches the A2A Protocol Project](https://www.linuxfoundation.org/press/linux-foundation-launches-the-agent2agent-protocol-project-to-enable-secure-intelligent-communication-between-ai-agents) | Linux Foundation | 2025-06-23 | — | Open (press release) |
| S4 | [A2A surpasses 150 organizations](https://www.linuxfoundation.org/press/a2a-protocol-surpasses-150-organizations-lands-in-major-cloud-platforms-and-sees-enterprise-production-use-in-first-year) | Linux Foundation | 2026-04-09 | — | Open (press release) |
| S5 | [Enable incoming A2A on a Foundry agent](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/enable-agent-to-agent-endpoint) | Microsoft Learn | `ms.date` 2026-07-29, updated 2026-07-31 | A2A 1.0 + 0.3 | Open |
| S6 | [The 2026-07-28 MCP Specification](https://blog.modelcontextprotocol.io/posts/2026-07-28/) | MCP project | 2026-07-28 | 2026-07-28 | Open |
| S7 | [MCP specification 2026-07-28](https://modelcontextprotocol.io/specification/2026-07-28/) | MCP project | 2026-07-28 | 2026-07-28 | Open |
| S8 | [i-am-bee/acp](https://github.com/i-am-bee/acp) | IBM Research / BeeAI | **archived 2025-08-27** | — | Open (read-only) |
| S9 | [LF welcomes the AGNTCY Project](https://www.linuxfoundation.org/press/linux-foundation-welcomes-the-agntcy-project-to-standardize-open-multi-agent-system-infrastructure-and-break-down-ai-agent-silos) | Linux Foundation | 2025-07-29 | — | Open (press release) |
| S10 | [a2aproject/a2a-tck](https://github.com/a2aproject/a2a-tck) | A2A Project | accessed 2026-08-13 | — | Open |
| S11 | [a2aproject/a2a-itk](https://github.com/a2aproject/a2a-itk) + [dashboard](https://a2aproject.github.io/a2a-itk/dashboard) | A2A Project | accessed 2026-08-13 | — | Open (repo read; see §7.2) |
| S12 | [a2a-sdk on PyPI](https://pypi.org/project/a2a-sdk/) | A2A Project | 1.1.2 on 2026-07-22 | 1.1.2 | Open |
| S13 | [Ask HN: Is anyone using A2A?](https://news.ycombinator.com/item?id=48582679) | Hacker News | ~2026-06-23 (approximate — see note) | — | Open |
| S14 | [SAP-samples/joule-a2a-agent-toolkit](https://github.com/SAP-samples/joule-a2a-agent-toolkit/) | SAP | accessed 2026-08-13 | — | Open |
| S15 | [Agent2Agent Protocol](https://www.salesforce.com/agentforce/ai-agents/agent2agent-protocol/) | Salesforce | accessed 2026-08-13 | — | Open (product page) |
| S16 | [Agent Network Protocol white paper](https://w3c-cg.github.io/ai-agent-protocol/) / [W3C AI Agent Protocol CG](https://www.w3.org/groups/cg/agentprotocol/) | W3C Community Group | CG first meeting 2025-06-18; white paper 2025-11 | draft | Open |
| S17 | [A survey of agent interoperability protocols: MCP, ACP, A2A, ANP](https://arxiv.org/pdf/2505.02279) | arXiv | 2025-05 | — | Open |
| S18 | [Governance Gaps in Agent Interoperability Protocols: What MCP, A2A, and ACP Cannot Express](https://arxiv.org/abs/2606.31498) | arXiv | 2026-06-30 | — | Open |
| S19 | [a2aproject/a2a-python](https://github.com/a2aproject/a2a-python) | A2A Project | accessed 2026-08-13 | — | Open |
| S20 | [Use an Agent2Agent agent](https://docs.cloud.google.com/gemini-enterprise-agent-platform/scale/runtime/use-an-a2a-agent) (Agent Runtime / Vertex AI Agent Engine) | Google Cloud | accessed 2026-08-13 | **Preview**, Pre-GA Offerings Terms | Open |
| S21 | [Connect to an A2A agent endpoint from Foundry Agent Service](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/tools/agent-to-agent) | Microsoft Learn | `ms.date` 2026-08-05, updated 2026-08-07 | A2A 1.0 + 0.3 | Open |
| S22 | [Connect agents to MCP server endpoints](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/tools/model-context-protocol) | Microsoft Learn | accessed 2026-08-13 | — | Open |
| S23 | [How Elicitation in MCP Brings Human-in-the-Loop to AI Tools](https://thenewstack.io/how-elicitation-in-mcp-brings-human-in-the-loop-to-ai-tools/) · [Non-blocking HITL with MCP MRTR](https://aaif.io/blog/non-blocking-human-in-the-loop-agents-re-engineering-agentic-runloops-and-state-machines-with-mr) | The New Stack; Agentic AI Foundation | accessed 2026-08-13 | — | Open (secondary) |
| S24 | [github.com/agntcy](https://github.com/agntcy) — org repository metrics via the GitHub API: 52 repos, ~2 155 stars, per-repo languages, push dates and SDK star counts | AGNTCY | queried 2026-08-13 | — | Open |
| S25 | [Agent Directory Service](https://datatracker.ietf.org/doc/draft-mp-agntcy-ads/) (`draft-mp-agntcy-ads`) | IETF Internet-Draft (individual submission) | -00 2025-10-17; latest **-02** 2026-07-06 | draft-02 | Open |
| S26 | [The AGNTCY Agent Directory Service: Architecture and Implementation](https://arxiv.org/pdf/2509.18787) | arXiv | 2025-09 | — | Open |
| S27 | [OASF adds Open Agent Specification support](https://outshift.cisco.com/blog/ai-ml/open-agent-specification-support-and-deployment-data) | Cisco Outshift | accessed 2026-08-13 | — | Open (vendor blog) |
| S28 | [docs.agntcy.org](https://docs.agntcy.org/oasf/open-agentic-schema-framework/) · [agntcy/oasf](https://github.com/agntcy/oasf) | AGNTCY | accessed 2026-08-13 | — | Open |
| S29 | [`reasoningEngines.streamQuery`](https://docs.cloud.google.com/vertex-ai/generative-ai/docs/reference/rest/v1/projects.locations.reasoningEngines/streamQuery) · [Manage deployed agents](https://cloud.google.com/agent-builder/agent-engine/manage/overview) | Google Cloud | accessed 2026-08-13 | v1 | Open |
| S30 | [What is Microsoft Foundry Agent Service?](https://learn.microsoft.com/en-us/azure/foundry/agents/overview) | Microsoft Learn | `ms.date` 2026-05-27, updated 2026-07-09 | — | Open |
| S31 | [FiligranHQ/xtm-mcp](https://github.com/FiligranHQ/xtm-mcp) | Filigran | v1.0.0 2026-03-05; archived 2026-07-05 | — | Open (read-only), accessed 2026-08-13 |
| S32 | ServiceNow community technical articles on A2A in AI Agent Fabric — "Enable MCP and A2A for your agentic workflows", "Expose ServiceNow AI Agents to External Systems with Google A2A" (servicenow.com/community) | ServiceNow | accessed 2026-08-13 | Now Assist AI Agents 6.0.x | Open (vendor community) |
| S33 | [Announcing the AI Agent Standards Initiative](https://www.nist.gov/news-events/news/2026/02/announcing-ai-agent-standards-initiative-interoperable-and-secure) | NIST CAISI | 2026-02-17 | — | Open |
| S34 | [Huawei open-sources A2A-T (MWC 2026)](https://www.huawei.com/en/news/2026/2/mwc-a2at-opensource) · TM Forum IG1453 / IG1453A | Huawei; TM Forum | 2026-02 / 2026-03 | — | Open |
| S35 | [MCP joins the Agentic AI Foundation](https://blog.modelcontextprotocol.io/posts/2025-12-09-mcp-joins-agentic-ai-foundation/) | MCP project / Linux Foundation AAIF | 2025-12-09 | — | Open |
| S36 | [mcp on PyPI](https://pypi.org/project/mcp/) | MCP project | 1.28.1 on 2026-06-26; 2.0.0 on 2026-07-28 | — | Open |

**Note on S13.** The thread was retrieved with a relative timestamp ("51 days ago" as of 2026-08-13), giving an approximate posting date of **~2026-06-23**. The exact date was not confirmed against the item's own metadata. Treated as approximate throughout.

### 2.1 Sources that were inaccessible or could not be verified

| Source | Why it matters | Status |
| --- | --- | --- |
| Fierce Network article on Huawei "A2A-T" | Inherited from the A2A evaluation request as a domain-specific A2A variant | **Could not be located**, even by site-restricted search (2026-08-13). A2A-T itself is verifiable first-hand: TM Forum IG1453/IG1453A and Huawei's MWC 2026 open-sourcing (S34) |
| A2A ITK *rendered* dashboard | The issue asks what the ITK actually validates | Inspected 2026-08-13: reachable and lists all six SDKs, but showed **zero completed runs** — the repository matrix remains the only usable evidence. See §7.2 |
| Filigran `xtm-mcp` embedded-successor claim | Would establish the ecosystem XTM One most plausibly interoperates with | The repository itself is primary (S31): `FiligranHQ/xtm-mcp` — v1.0.0 released 2026-03-05, archived 2026-07-05, OpenCTI 6.8 tie-in in the README. The claim that an embedded successor is on the roadmap appears only in secondary coverage and remains unverified |
| Notion product database; sales/CS/partner conversations | The demand-signal question from the A2A evaluation request | **Out of reach for this pass.** Recorded as explicitly unanswerable, not as a null result |

---

## 3. A2A v1.0 — reference candidate

**Governance.** Created by Google, contributed to the Linux Foundation, project launched 2025-06-23 (S3). The Technical Steering Committee spans AWS, Cisco, Google, IBM Research, Microsoft, Salesforce, SAP and ServiceNow — sourced from the project's own `GOVERNANCE.md` (in S2's repository); the launch press release quotes seven vendors and mentions neither the TSC nor IBM. This is the strongest governance story in the space and is not seriously disputed.

**Versions** (S2): v0.2.5 2025-06-30 → v0.2.6 2025-07-17 → v0.3.0 2025-07-30 → **v1.0.0 2026-03-12** → v1.0.1 2026-05-28. The spec uses `Major.Minor` for compatibility decisions; patch numbers are excluded (S1).

**Transports.** Three bindings: JSON-RPC 2.0, gRPC, and HTTP+JSON/REST. v1.0 was a large refactor separating the application protocol definition from its transport mappings (S1, S2).

**Discovery.** An **Agent Card** — a metadata document with `id`, `name`, `description`, `provider`, `capabilities` (`streaming`, `pushNotifications`, `extendedAgentCard`), `skills`, `securitySchemes`, and `interfaces` (protocol-binding declarations, each with an optional `tenant`). An optional `signature` field carries integrity verification. The conventional discovery path is `.well-known/agent-card.json`, though implementations may serve it elsewhere (S1, and S5 does exactly that).

**Security schemes.** API key, HTTP Basic/Bearer, OAuth 2.0 (authorization code, client credentials, **device code**), OpenID Connect, and mutual TLS (S1). v1.0 removed the implicit and password grants and added device code and PKCE (S2).

**Core data model** (S1):

- `Task` — `id`, `contextId`, `status`, `artifacts`, `history`, `metadata`.
- `TaskStatus.state` — `SUBMITTED`, `WORKING` (in progress); `INPUT_REQUIRED`, `AUTH_REQUIRED` (interrupted); `COMPLETED`, `FAILED`, `CANCELED`, `REJECTED` (terminal).
- `Message` — `messageId`, optional `contextId`/`taskId`, `role` (`ROLE_USER` / `ROLE_AGENT`), `parts`, `metadata`, `extensions`, `referenceTaskIds`.
- `Part` — a union of exactly one of `text`, `raw` (bytes/base64), `url` (file reference), or `structuredData` (JSON).
- `Artifact` — `id`, `title`, optional `description`, `parts`, `metadata`.

> **Correction to the A2A evaluation request and the implementation request.** Both describe the v1.0 Part union as `TextPart` / `FilePart` / `DataPart`. That is the **v0.3** naming. In v1.0 the variants are `text` / `raw` / `url` / `structuredData` (S1). Any implementation scoping should use the v1.0 names.

**Streaming.** Two operations — *Send Streaming Message* (initial Task/Message then `TaskStatusUpdateEvent` / `TaskArtifactUpdateEvent`) and *Subscribe to Task*. Both require `AgentCard.capabilities.streaming: true` (S1).

**Error model.** Machine-readable code, human-readable message, optional structured details array with an `@type` per ProtoJSON. Named errors include `TaskNotFoundError`, `TaskNotCancelableError`, `PushNotificationNotSupportedError`, `UnsupportedOperationError`, `ContentTypeNotSupportedError`, `VersionNotSupportedError` (S1).

**Multi-tenancy.** A `tenant` field appears on all operation requests and on `AgentInterface` declarations in the Agent Card. The spec defines it as: *"Optional. Opaque routing identifier. Must match the `tenant` value from the selected `AgentInterface` in the Agent Card when that field is set."* (S1). It is **optional and opaque** — a routing hint, not a tenancy model. This matters for the open question on mapping `tenant` and is answered in the benchmark.

**v1.0 breaking-change surface** (S2), relevant to the v0.3-compatibility question the parent raises: package prefix change; non-complex IDs in requests; `TaskPushNotificationConfig` and `PushNotificationConfig` combined; config pluralisation in `ListTaskPushNotificationConfigs`; "cancelled" → "canceled"; enum format aligned to ADR-001 ProtoJSON; redundant `final` removed from `TaskStatusUpdateEvent`; `extendedAgentCard` moved to `AgentCapabilities`; OAuth flows modernised; `v1` removed from HTTP binding URLs; deprecated fields dropped from `a2a.proto`.

**SDKs.** Official SDKs for Python, Go, Java, JavaScript/TypeScript and C#/.NET. The Python SDK (S12, S19) is at **1.1.2, released 2026-07-22**, supports Python 3.10–3.14, covers JSON-RPC, HTTP+JSON/REST and gRPC across both v1.0 and v0.3 compatibility mode, and ships optional extras including `fastapi`, `http-server`, `grpc`, `signing`, and `encryption`. XTM One's backend is Python/FastAPI, so the SDK's shape is a genuine fit.

---

## 4. Other candidates

### 4.1 MCP as an interoperability substrate

**Governance.** Originated at Anthropic; donated in December 2025 to the Agentic AI Foundation (AAIF), a directed fund under the Linux Foundation (S35). Explicit feature-lifecycle governance arrived with the 2026-07-28 revision (S6).

**The 2026-07-28 revision is the material development for this review** (S6, S7), and it postdates the A2A evaluation request by a week. It is described by the project as the largest revision since launch:

- **Stateless core.** The `initialize`/`initialized` handshake and session IDs are gone. Each request carries its own protocol version, client identity and capabilities, so requests can land on any instance behind a plain round-robin load balancer.
- **Tasks extension** (`io.modelcontextprotocol/tasks`) — promoted from experimental to a formal extension. Polling via `tasks/get`, a new `tasks/update`, and change notifications through a `subscriptions/listen` stream. **This is MCP acquiring the long-running-work lifecycle that was previously A2A's clearest differentiator.**
- **Multi Round-Trip Requests (MRTR).** Servers return `resultType: "input_required"` with the requests they need; clients retry with `inputResponses`. Mid-call confirmation without a persistent connection.
- **Authorization hardening.** RFC 9207 issuer validation against authorization-server mix-up, client credentials bound to their issuing server, `application_type` for desktop/CLI flows. **Dynamic Client Registration is formally deprecated in favour of Client ID Metadata Documents (CIMD).**
- **Deprecations.** Sampling (SEP-2577), Roots and Logging are deprecated, with a minimum twelve-month support window before removal.
- **Governance.** A feature lifecycle (Active / Deprecated / Removed), an Extensions framework, and a requirement that Standards Track SEPs reach Final only when matching scenarios land in the conformance suite.

**Positioning.** The ecosystem framing — including in the A2A evaluation request's own notes — is that MCP is for tool access and A2A for agent coordination, and that they are complementary rather than competing. That framing is **less accurate after 2026-07-28** than it was before. With the Tasks extension and MRTR, an MCP server that is itself an agent can accept long-running work, report progress, and ask for input mid-task. Note that S6 does not itself address agent-to-agent use or agents-as-servers; the overlap is an inference from the primitives, and is marked as such.

**Relevance to XTM One.** Not hypothetical. XTM One already exposes its agents as MCP tools over Streamable HTTP JSON-RPC 2.0 (`POST /mcp/user`, `POST /mcp/global`) and already consumes external MCP servers as agent tools.

#### 4.1.1 What MRTR actually does — and what it does not

**The direction is server → client, never server → another server.** The specification's design principles bar a server from seeing into other servers, and cross-server interaction stays under the host's control (S7). The server returns `resultType: "input_required"` naming what it needs, and the client **re-sends the original request** with the answers attached in `inputResponses`. There is no open channel and no server-side suspension — which is precisely what makes it compatible with the new stateless core.

**MRTR is not inherently human-in-the-loop.** It carries three input kinds, and only one of them requires a person:

| Input kind | What answers it | Human required? | Status in 2026-07-28 |
| --- | --- | --- | --- |
| **Sampling** (`sampling/createMessage`) | The client's **host model** — the spec's own flow is Client → Host → AI → back (S7) | **No** | **Deprecated** (SEP-2577). New implementations SHOULD NOT adopt it; existing ones SHOULD migrate to calling LLM provider APIs directly. Minimum twelve-month runway (S6) |
| **Elicitation** | The **user**. The server supplies a JSON Schema; the client renders a form or prompt (S23) | **Yes, as specified** | Active |
| **Roots** | Client configuration (filesystem scope) | No | Active — but carries no clarification semantics |

> **This is the finding that makes "MRTR enables MCP agent-to-agent interop" doubtful.** The one flavour that delivers *autonomous* agent-to-agent feedback — sampling, where the calling agent's own model answers with no person involved — is precisely the flavour being retired. The flavour that survives, elicitation, is specified as collecting input from a human. So the autonomous path is deprecated and the durable path is human-facing. Building agent-to-agent clarification on MRTR means either adopting a primitive the specification tells you not to adopt, or relying on unspecified client behaviour.

Two further qualifications, both recorded as uncertainty rather than as findings:

- **Elicitation is not *guaranteed* to involve a human either.** The specification describes the client rendering a form for the user, but nothing prevents an agentic client from answering autonomously. That is client behaviour, not a spec guarantee — so an optimistic reading is possible, it simply is not underwritten by the standard.
- **Whether either target platform implements MRTR at all is unverified.** `no evidence found` for Foundry or Vertex, and the revision is two weeks old at the time of writing.

Net: MRTR genuinely improves the inbound path — a server can pause for input instead of failing or guessing. What it does not do is provide a standards-backed, autonomous, agent-to-agent negotiation channel.

#### 4.1.2 The inbound path is real, and already available

Distinct from the above, and not dependent on it: **both target platforms consume MCP servers as agent tools.** Foundry documents connecting an agent to an MCP server endpoint over streamable HTTP with Entra OAuth (S22), and Vertex Agent Runtime likewise consumes MCP tools (S20).

A customer's Foundry or Vertex agent can therefore invoke an XTM One agent today, through the `/mcp/user` endpoint that already ships, with no new external surface. What MCP cannot do is the reverse — reach *their* agent (§5.3).

One practical qualification: XTM One is pinned to `MCP_PROTOCOL_VERSION = "2025-03-26"`, which predates both the Tasks extension and MRTR. The inbound path works today for synchronous invocation, but acquiring long-running task semantics or mid-task input requires the 2026-07-28 upgrade first — which is also a major SDK bump: the pinned `mcp==1.28.1` (2026-06-26) predates the revision, and support begins at `mcp` 2.0.0 (S36).

### 4.2 ACP — Agent Communication Protocol

**Excluded — consolidated into A2A.** IBM Research launched ACP in March 2025 to power BeeAI: a REST-based protocol with negotiation semantics and FIPA-ACL heritage, later under Linux Foundation governance. The repository was **archived on 2025-08-27** and its README states that ACP is now part of A2A under the Linux Foundation, pointing to a migration guide (S8).

This is a substantive finding, not bookkeeping: one of the four candidates named in the issue's scope no longer exists as an independent standard, and its disappearance is *consolidation into the baseline candidate*. It strengthens A2A's governance case and shrinks the alternative set.

### 4.3 AGNTCY

**Retained, scored, but not a like-for-like competitor.** Open-sourced by Cisco (Outshift) in March 2025 with LangChain and Galileo; welcomed by the Linux Foundation on 2025-07-29 (S9). Formative members include Cisco, Dell, Google Cloud, Oracle and Red Hat; 65+ supporting companies claimed. Components:

- **OASF** (Open Agent Schema Framework) — discovery via attribute-based capability taxonomies.
- **Agent Identity** — cryptographically verifiable identity and access control across organisational boundaries.
- **SLIM** — messaging; "multi-modal, human-in-the-loop and quantum-safe" is the LF press release's wording (S9), while the current project docs describe it as secure peer-to-peer messaging with end-to-end MLS encryption.
- Observability.

AGNTCY is best understood as *infrastructure around* agent interop — discovery, identity, observability — rather than a task-lifecycle wire protocol. Its identity layer is the part that addresses a real A2A weakness (see §5.4).

What the evidence establishes:

- **Active, multi-component development.** 52 repositories, ~2 155 stars in total (S24, queried 2026-08-13). The flagship components are `oasf` (328★), `slim` (203★), `dir` (175★) and `identity` (99★); `slim`, `dir`, `coffeeAgntcy` (a published end-to-end reference application), `csit` and `docs` were all pushed within two days of the query. This is a live project, not a paper one.
- **Genuine standardisation effort.** The Agent Directory Service is an IETF Internet-Draft (S25; latest revision `draft-mp-agntcy-ads-02`, 2026-07-06 — an individual submission, not a working-group item), with a companion architecture-and-implementation paper (S26). Neither is a press release.
- **One named vendor integration.** OASF and the Directory integrate with Oracle's Open Agent Specification — Agent Spec is Oracle's own specification, so the Cisco Outshift post announcing the support (S27, 2026-01-14) is itself the integration evidence. It remains a capability announcement, not a customer report.
- **Its own wire protocol is abandoned, formally.** `acp-spec` (168★) and `acp-sdk` (88★) — AGNTCY's *Agent Connect Protocol*, distinct from IBM's identically-named ACP — were last pushed in May and June 2025 respectively and are now archived repositories, while the rest of the org moves weekly. That is a live signal, and it corroborates the consolidation story in §4.2: the wire-protocol layer is where AGNTCY has stopped competing.
- **Scale is modest.** ~2 155 stars across the whole organisation, flagship at 328, against MCP's ~18 000 registry servers and hundreds of millions of monthly SDK downloads (~470M/month reported July 2026 — S6).
- **The canonical implementations are not Python.** `oasf` is Elixir, `slim` is Rust, `dir` and `identity` are Go, `identity-service` is TypeScript (S24). Python exists at the periphery: `coffeeAgntcy` is a Python reference application (107★), but `dir-sdk-python` has 3 stars, `slim-mcp-python` has 2, `app-sdk` self-describes as "not the canonical SDK for AGNTCY subsystems", and `workflow-srv` (74★, the largest Python component, itself archived) targets the abandoned ACP. This is the concrete basis for the implementation-cost scores in the benchmark, and it is a statement about language fit rather than about project quality.

**Still `no evidence found` for named production deployments.** The distinction now carries more weight, because the search behind it was thorough rather than cursory: what exists is active engineering, standards work and a vendor integration — not a customer running it.

### 4.4 ANP — Agent Network Protocol

**Excluded — research-grade.** A W3C Community Group effort (CG first meeting 2025-06-18; white paper published 2025-11) targeting a decentralised "HTTP of the agentic web", built on W3C DIDs with `did:wba` identity, plus WNS handles, agent description and discovery specs (S16). The meta-protocol specification remains an unreleased draft.

Exclusion reason: no released stable specification, `no evidence found` for enterprise or production adoption, and a decentralised-identity premise that does not match XTM One's deployment model (single-company deployments with an existing centralised identity story). Worth re-checking if the W3C track produces a Recommendation.

### 4.5 Framework-native interop paths

**Retained as a candidate class, but the finding is that they collapse onto A2A.**

- **Microsoft Foundry** (S5) — documented support for exposing a Foundry agent as an A2A endpoint, and for calling remote A2A endpoints via an A2A tool. Details in §5.2 (incoming) and §5.3 (outbound, plus Vertex) — together the most instructive adoption evidence in this review.
- **Google ADK, LangGraph, CrewAI, LlamaIndex, Semantic Kernel, AutoGen, Microsoft Agent Framework for .NET** — reported to ship native A2A support. Source is vendor/community blog material rather than a single primary index; treated as *announced* pending per-framework verification.

So "framework-native" is largely not a distinct wire protocol. Where these frameworks talk to agents they did not build, they increasingly speak A2A; where they do not, they speak a proprietary vendor HTTP API. The class is scored on that basis.

### 4.6 Generic baseline — authenticated HTTP + JSON / OpenAPI

**Retained, and it is not a straw man.** The honest comparator is "formalise and document what XTM One already exposes", which is more than nothing:

| Existing surface | Path |
| --- | --- |
| Agents as MCP tools | `backend/app/api/v1/mcp_endpoint.py` — `POST /mcp/user`, `POST /mcp/global` |
| Stateless task execution | `backend/app/api/v1/extension.py` — `POST /api/v1/extension/execute-task` |
| Agent-as-model (OpenAI-compatible) | `backend/app/api/v1/llm_proxy.py` — not mounted when `platform_mode == "xtm_one"` |

An external partner can already invoke an XTM One agent today, with authentication, through at least two of these. What is missing is a *published contract*, a task lifecycle, and discovery — not the ability to call an agent.

---

## 5. Adoption evidence

Per Rule 2, the two columns are kept apart.

### 5.1 The evidence table

| Claim | Announced / marketing | Verified production | Source |
| --- | --- | --- | --- |
| A2A has 150+ supporting organisations | ✅ Foundation press release, 2026-04-09 | — | S4 |
| A2A has production deployments across supply chain, financial services, insurance and IT operations | ✅ Foundation press release | ✗ No named customer, product surface, or independent account located | S4 |
| Microsoft Foundry supports **incoming** A2A (hosting an agent) | ✅ | ⚠️ **Documented, but public preview**; text-only, no streaming, JSONRPC-only for v1.0 — §5.2 | S5 |
| Microsoft Foundry supports **outgoing** A2A (calling an external agent) | ✅ | ✅ GA Python SDK, six auth modes, anonymous third-party card discovery — §5.3 | S21 |
| Google Vertex AI Agent Runtime exposes agents over A2A | ✅ | ⚠️ **Documented, but Preview** (Pre-GA terms); streaming supported, authenticated card only — §5.3 | S20 |
| SAP Joule accepts A2A agents | ✅ | ✅ Public SAP-owned toolkit repository for building and connecting A2A agents to Joule | S14 |
| Salesforce Agentforce exposes A2A | ✅ Product page | ✗ Product page is marketing surface; no technical documentation of a live endpoint reviewed | S15 |
| ServiceNow AI Agent Fabric embeds A2A | ✅ Secondary sources | ⚠️ First-party technical articles exist on servicenow.com (Now Assist AI Agents 6.0.x, Zurich Patch 4+ — S32); no formal docs-portal product documentation located | S32 |
| Practitioners use A2A in production | — | ⚠️ At least three self-reported first-hand accounts in S13; unnamed employers | S13 |
| A2A SDK downloads ≈10.9M/month vs MCP ≈257M/month | — | ⚠️ Single commenter claim in S13; the MCP side is directionally consistent with the ~470M/month the MCP project reported in July 2026 (S6) | S13, S6 |
| MCP is broadly deployed | ✅ | ✅ **Directly verifiable inside XTM One** — the platform runs MCP in both directions in production today | repo |

### 5.2 The most instructive single data point

> **Scope.** This section describes **incoming** A2A, where Foundry *hosts* an agent that others call. Foundry's **outbound** A2A tool, where a Foundry agent calls an external endpoint, is a different feature with different maturity and is covered in §5.4. The limitations below apply to the incoming direction only.

Microsoft Foundry is one of the headline A2A adoption proofs. Its own documentation on **incoming** A2A (S5, dated 2026-07-29, updated 2026-07-31) states:

- The feature is **public preview**, "provided without a service-level agreement", and "not recommended for production workloads".
- For A2A **v1.0, only the JSONRPC transport is supported**. HTTP+JSON and gRPC are not. (v0.3 supports HTTP+JSON but not gRPC.)
- **Only the text modality is supported.** File data and other non-text modalities are not.
- **Streaming responses (server-sent events) are not supported.**
- All A2A URLs require Microsoft Entra ID authentication; **anonymous access to the agent card is not supported**, so the card is served from a custom path (`agentCard/v1.0`) rather than the conventional `.well-known/agent-card.json`.
- Foundry's card carries the version **in the path** (`…/agentCard/v0.3`, `…/agentCard/v1.0`); a request naming no version returns 404 rather than a default.

Read plainly: the flagship enterprise A2A integration is a preview-grade, text-only, non-streaming, single-transport subset that breaks the protocol's own anonymous-discovery convention and serves each protocol version at its own path. That is not an argument that A2A is bad — it is a precise measure of how far "supported by" sits from "deeply used in production", and it is exactly the distinction the issue asked to make visible.

### 5.3 The two platforms XTM One actually needs — capability detail

The first interop targets are customer agents on **Google Vertex AI Agent Runtime** and **Microsoft Azure Foundry**. Both are documented from primary vendor sources.

| | Vertex AI Agent Runtime (S20) | Azure Foundry (S5 incoming, S21 outbound) |
| --- | --- | --- |
| Status | **Preview**, Pre-GA Offerings Terms | **Preview** for incoming; outbound A2A tool type `a2a_preview` on a GA Python SDK (`azure-ai-projects>=2.0.0`) |
| Agent card | **Not public.** Authenticated retrieval via `handle_authenticated_agent_card` | Incoming: custom path `…/agentCard/v1.0` and `…/agentCard/v0.3`, Entra required, **anonymous access not supported** |
| A2A operations exposed | `on_message_send`, `on_get_task`, `on_cancel_task`, `handle_authenticated_agent_card` | v1.0 + v0.3; **v1.0 is JSONRPC-only**; text modality only; no SSE streaming; each version served at its own card path, with no version-less default |
| Streaming | **Supported** — message send returns an async iterator | **Not supported** |
| Caller credentials | Google Cloud credentials with `cloud-platform` scope, bearer token | Microsoft Entra token; **Foundry Agent Consumer** role or higher |
| Client options | Agent Platform SDK (`vertexai`), official A2A Python SDK (`a2a-sdk>=0.3.4`), or plain HTTP (`httpx`/`requests`) | Python (GA), C#, TypeScript (preview), Java, REST |
| Can call **out** to a third-party A2A endpoint | ✅ via A2A Python SDK or plain HTTP | ✅ A2A tool. Auth modes: `none`, custom header keys, OAuth2 (own app registration), user Entra token, project managed identity, agentic identity |
| Discovers a third-party agent card | — | **Anonymously by default** for non-Foundry targets (`send_credentials_for_agent_card` to opt in). Control Plane registration discovers a custom agent's card at `/.well-known/agent-card.json` |
| Exposes its agents over **MCP** | ✗ `no evidence found` — consumes MCP tools, does not publish agents as MCP servers | ✗ `no evidence found` — consumes MCP servers as tools, and a *toolbox* publishes an MCP endpoint, but a Foundry **agent** is exposed via A2A |

Two consequences worth stating plainly, because they decide the benchmark:

1. **Neither platform exposes a hosted agent as an MCP server.** Anything that needs to call one of those agents must speak A2A.
2. **Both are markedly better at calling out than at being called.** Foundry's outbound tool is broader than its incoming endpoint, and it discovers third-party cards at the conventional `/.well-known/agent-card.json` with standard OAuth2 — which makes a standards-compliant A2A server the cheapest way to become reachable from either platform.

Caveat on S20: Google's documentation is mid-migration between the `cloud.google.com/vertex-ai` tree and the Gemini Enterprise Agent Platform tree. Operation names, the authenticated-card mechanism, credentials and streaming behaviour were confirmed; **the exact A2A protocol version Agent Runtime serves was not**.

### 5.4 Reported criticisms

From S13 (~2026-06-23), recorded as practitioner sentiment rather than fact:

- A2A "assumes the existence of well-defined agent identity", which does not inherently exist — called a flawed premise by one infrastructure developer. (Notably, this is the gap AGNTCY's identity layer and ANP's `did:wba` both target.)
- The gRPC binding is "painful"; complexity overhead is significant.
- Perceived as over-engineered relative to current needs; one commenter implemented it and then found "we didn't need an agent on the other side".
- Authority, trust and access-control mechanisms seen as inadequately addressed.
- Prompt-caching, latency and token-cost concerns.

The academic literature echoes the governance point: S18 (2026-06) — "Governance Gaps in Agent Interoperability Protocols: What MCP, A2A, and ACP Cannot Express" — argues that current protocols (it analyses five, adding ANP and ERC-8004) cannot express governance properties such as voting and dissent preservation. S17 (2025-05) is the standard four-protocol survey but predates both A2A v1.0 and the ACP consolidation, so its comparative conclusions are stale.

---

## 6. Candidate inventory and exclusion register

| Candidate | Axis | Decision | Reason |
| --- | --- | --- | --- |
| **A2A v1.0** | Runtime wire | **Retained** | Baseline / reference candidate |
| **MCP (2026-07-28)** | Runtime wire (post-Tasks) | **Retained** | Tasks extension + MRTR give it agent-task lifecycle; already in production at XTM One |
| **AGNTCY** (OASF / Identity / SLIM) | Discovery + identity + messaging | **Retained** | Named in issue scope; overlaps partially, complements elsewhere |
| **Framework-native** (ADK, Foundry, LangGraph, AutoGen) | Runtime wire | **Retained as a class** | Named in issue scope; finding is that they mostly resolve to A2A or a vendor API |
| **Generic HTTP+JSON / OpenAPI** | Runtime wire | **Retained** | The honest "no new standard" comparator; partially already built |
| ACP (IBM / BeeAI) | Runtime wire | **Excluded** | Repository archived 2025-08-27; merged into A2A under the LF (S8). No longer an independent standard |
| ANP (Agent Network Protocol) | Runtime wire | **Excluded** | No released stable spec; `no evidence found` for production use; decentralised-DID premise mismatches XTM One's deployment model (S16) |
| Microsoft AgentSchema | Definition portability | **Excluded — wrong axis** | Already adopted by XTM One (`dev-docs/agentschema-format.md`). Adjacent prior art, not a competitor |
| Agent Plugins (agent-plugins.org) | Packaging | **Excluded — wrong axis** | Tracked separately |
| AP2 (agent payments) | Domain-specific | **Excluded** | Out of domain for XTM One |
| Huawei "A2A-T" | Domain-specific | **Excluded** | Telecom vertical — out of domain. Verifiable first-hand (TM Forum IG1453/IG1453A; Huawei open-sourcing at MWC 2026 — S34); the Fierce Network article inherited from the A2A evaluation request could not be located |

---

## 7. Conformance and interoperability tooling

### 7.1 A2A TCK (Technology Compatibility Kit)

`a2aproject/a2a-tck` (S10) validates implementations across all three transports — gRPC, JSON-RPC and HTTP+JSON. Tests are organised by RFC 2119 requirement level: `MUST` failures are hard failures; `SHOULD` failures are marked xfail and do not block compatibility; `MAY` tests are skipped when the agent does not declare the capability. Reports are generated in multiple formats.

Important limitation: **the TCK does not confer certification.** The documentation does not define an overall pass criterion, and there is no formal compliance programme behind it.

### 7.2 A2A ITK (Integration Test Kit)

`a2aproject/a2a-itk` (S11) verifies compatibility *across* SDK implementations and versions, dispatching nested instructions through chains of agents over varied transports. It covers standard messaging, streaming payloads, push notifications and task resubscription. A public dashboard exists at `a2aproject.github.io/a2a-itk/dashboard`.

The support matrix as read from the repository on 2026-08-13:

| SDK | v0.3 | v1.0 | Status |
| --- | --- | --- | --- |
| Python | ✅ | ✅ | Stable |
| Go | ✅ | ✅ | Stable |
| TypeScript | ✅ | ✅ | Stable |
| Java | — | ⚠️ | Preliminary |
| Rust | — | ⚠️ | No current mount support |
| .NET | — | — | Placeholder |

Two caveats that the marketing framing does not carry. First, the repository showed only ~52 commits (2026-08-13) — this is young tooling, not a mature conformance regime. Second, **.NET is a placeholder**, which is worth noting precisely because Microsoft Agent Framework for .NET is one of the headline A2A integrations.

Per §2.1: the *rendered* dashboard was inspected on 2026-08-13 — it lists all six SDKs but showed zero completed runs, so the matrix above (from the repository) remains the only usable evidence. The issue explicitly warns against treating a published dashboard as a guarantee, and that warning applies to this table too.

### 7.3 Other candidates

| Candidate | Conformance tooling |
| --- | --- |
| MCP | The 2026-07-28 governance SEPs require Standards Track SEPs to have matching scenarios in the conformance suite before reaching Final (S6) — a real structural mechanism. Maturity of the suite itself: `no evidence found` |
| AGNTCY | `csit` — "Continuous System Integration Testing for Agntcy Projects" (59★, active) (S24). Internal CI across its own components rather than a published third-party conformance regime like A2A's TCK |
| Framework-native | `no evidence found` (vendor-internal at best) |
| Generic HTTP+JSON | Generic OpenAPI/contract-testing tooling exists; nothing agent-specific |

---

## 8. Demand signal

Only GitHub was searched. Notion, sales/CS conversations and direct partner asks were **out of reach and are recorded as explicitly unanswerable**, not as a null result.

**GitHub sweep, `XTM-One-Platform/xtm-one`, 2026-08-13.** Every issue mentioning A2A, agent2agent, interoperability, or external agents:

| Title | Author | Created |
| --- | --- | --- |
| evaluate and support A2A v1.0 for external/third-party agent interoperability | Romain D. | 2026-07-21 |
| literature review and benchmark of A2A alternatives | Antoine Gaston | 2026-08-10 |
| implement A2A v1.0 external agent interoperability | Antoine Gaston | 2026-08-10 |
| Agent Plugins portable package format | — | — |
| API gateway for XTM One | — | — (adjacent) |

An org-wide issue search returned no additional results. The org contains eight repositories, none public — seven internal, one private (`xtm-one`, `ai-gateway`, `e2e-tests`, `evaluation_metrics`, `llm_as_a_judge_benchmark`, `voice-to-spec`, `inference-lab`, `agentic-platform`); only `xtm-one` was searched in depth.

**Finding: zero externally-originated demand *in the issue tracker*.** All three A2A issues are internally authored. No customer, partner or prospect request is visible in the searched sources.

This is a genuine result within its scope, and a bounded one. It cannot distinguish "no demand exists" from "demand exists but is recorded somewhere this pass could not read".

### 8.1 Demand established outside the sweep

Stated directly by the product owner (2026-08-13): XTM One's first external agent interoperability targets are **customers running agents on Google Vertex AI** and **customers running agents on Microsoft Azure Foundry**, and the requirement is **explicitly bidirectional** — their agents must be able to task XTM One agents, and XTM One agents must be able to delegate to theirs. Inbound-only coverage does not satisfy it.

This is product-owner direction, not a written artefact — it is not traceable to a ticket, a Notion entry or a customer email the way §2's sources are, and it should be substantiated against a named customer before implementation is scoped. It is recorded here rather than in §2 for that reason.

The GitHub result above stands: a live customer requirement is absent from the issue tracker, which is worth noticing on its own.

---

## 9. Open evidence gaps

1. **Demand substantiation** — product-owner direction (§8.1) names Vertex and Foundry customers and requires both directions, but is not backed by a written artefact. Notion, sales/CS and partner conversations remain unread. Substantiating one named customer is still the highest-value evidence action.
2. **Salesforce's production surface** — marketing page only; no technical endpoint documentation reviewed. ServiceNow has first-party technical articles (S32), though no formal docs-portal pages were located.
3. **The LF "production deployments" claim** (S4) names industries but no customers. Not independently corroborated.
4. **SDK download figures** (S13) — a single commenter claim; the MCP side is directionally consistent with the ~470M/month the project itself reported (S6), the A2A side remains unverified.
5. **ITK results** — the rendered dashboard lists all six SDKs but publishes zero completed runs (2026-08-13); the support matrix remains asserted rather than demonstrated.
6. **Filigran ecosystem MCP/A2A posture** — `xtm-mcp` release and archive dates are primary-sourced (S31); the embedded-successor claim rests on secondary coverage only.
7. **Per-framework A2A support** (ADK, LangGraph, CrewAI, LlamaIndex, Semantic Kernel, AutoGen) — asserted by vendor/community material, not verified framework by framework.
8. **Huawei "A2A-T"** — verifiable via TM Forum IG1453 and Huawei's MWC 2026 open-sourcing (S34); the Fierce Network article inherited from the A2A evaluation request could not be located. The out-of-domain exclusion is unaffected.
9. **Vertex A2A protocol version** — S20's operation names, authenticated-card mechanism, credentials and streaming behaviour were confirmed, but the exact A2A version Agent Runtime serves was not. Google's documentation is mid-migration between the `cloud.google.com/vertex-ai` and Gemini Enterprise Agent Platform trees.
10. **Direction of first customer use** — the requirement itself is bidirectional (§8.1), so direction does not gate whether either surface is needed; what remains open is which direction a first customer exercises first — a sequencing input only.
11. **MRTR client support** — whether Foundry's or Vertex's MCP clients implement the 2026-07-28 MRTR flow, and how they handle an `input_required` result with no human present, is unverified. The revision is two weeks old at the time of writing.
12. **Search effort was not equal across candidates.** A2A and AGNTCY were each investigated across many searches and primary sources. **ANP was not** — its exclusion rests on a single search and is the least-examined judgement in this document.

---

## 10. Sources

All sources are listed with publisher, date and version in §2. Sources that were inaccessible, paywalled, or verifiable only through secondary reporting are listed separately in §2.1, and every claim resting on them is marked in place.
