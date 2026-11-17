# External Agent Interoperability — Benchmark and Recommendation

**Date:** 2026-08-13
**Companion:** `dev-docs/agent-interop-literature-review.md` — all sources, cited here as **S1**–**S36**
**Scope:** §1–§14 cover external / third-party agent communication only. Internal handover/delegate/consult/transfer routing is out of that scope, as is any change to MCP-based tool access.
**Scope extension:** §15 asks, at the product owner's request, whether the retained solution should also replace internal handover. It sits **outside** that boundary and is written as input to a separate issue.

> Scores A2A v1.0 against its alternatives on criteria fixed before scoring, evaluated against XTM One's actual code and against its named first interop targets, not against the abstract problem. The requirement is **bidirectional** — external agents task XTM One agents, and XTM One agents delegate outward — which eliminates MCP as the interop substrate and makes **A2A v1.0** the retained standard, implemented server-first per §11.

**Contents:** §1 TL;DR · §2 Constraint baseline · §3 Criteria and weights · §4 Candidate set · §5 Comparison matrix · §6 Interop targets · §7 Reachability gate · §8 Implementation cost · §9 Robustness · §10 Demand signal · §11 Recommendation · §12 Answers to the A2A evaluation request's open questions · §13 Security findings · §14 Honest caveats · §15 Scope extension: internal handover · §16 Sources

---

## 1. TL;DR

**Recommendation: ADOPT A2A v1.0 AND IMPLEMENT BOTH DIRECTIONS — server (inbound) first, client (outbound) second — behind the security prerequisites (F1–F3, §13). Confidence: high on the standard, medium on timing, because both target A2A surfaces are Preview.**

Four findings drive the recommendation.

1. **The requirement is bidirectional and concrete.** The targets are customer-operated agents on Vertex and Foundry, and the requirement runs both ways: their agents task XTM One agents, and XTM One agents delegate to theirs (§10.1). Inbound-only coverage does not satisfy it.
2. **Outbound eliminates MCP.** Neither platform publishes a hosted agent as an MCP server (§6.1), so the incumbent protocol cannot cover half the requirement — no matter how well it scores in §5. It is the gate's only categorical elimination, and under a bidirectional requirement it is decisive (§7.2).
3. **Only A2A covers both directions as one standard.** Bespoke vendor HTTP also reaches both targets, but per vendor and per direction, with no discovery, no agent-level semantics and no task lifecycle. A2A amortises a single integration across both targets, both directions, and any future A2A-speaking partner (§7.2).
4. **The A2A path is well supported where it matters.** Foundry's outbound A2A tool fetches non-Foundry agent cards **anonymously by default**, and its Control Plane registration discovers a custom agent's card at `/.well-known/agent-card.json`, with OAuth2, custom-header and managed-identity auth (S21); Vertex consumes third-party A2A endpoints via SDK or plain HTTP (S20). XTM One already mounts `/.well-known` and runs an OAuth 2.1 authorization server (§2.3).

One useful bridge while the server lands: both platforms consume MCP servers as agent tools (S22, S20), so a customer's Foundry or Vertex agent can already invoke an XTM One agent synchronously through the shipped `/mcp/user` endpoint. That is an interim validation path, not the deliverable — it advertises a free-text tool rather than an agent with skills, offers no discovery, and its mid-task clarification primitive is being withdrawn (§7.3).

Two distinctions the reader should carry. Foundry's **incoming** A2A (preview, JSONRPC-only, text-only, non-streaming) and its **outbound** A2A tool (GA Python SDK, six auth modes) are different features with different maturity, and conflating them misreads A2A's practical reach. And **MRTR is not a standards-backed agent-to-agent negotiation channel**: its autonomous flavour (sampling, answered by the calling agent's model) is deprecated, while the surviving flavour (elicitation) is specified as human-facing — so "MRTR enables MCP interop" is doubtful in both directions (§7.3).

---

## 2. XTM One constraint baseline

Candidates are scored against this, not against the abstract problem. All paths verified on 2026-08-13.

### 2.1 There is no tenant or organization entity

No `Tenant` or `Organization` model class exists in `backend/app/models/`. Every occurrence of "tenant" in the backend belongs to an integration provider (Microsoft Entra tenant IDs and similar), not to a platform concept.

Isolation is a **three-tier ownership model** carried on every shareable entity — `user_id`, `is_company_managed`, `shared_group_ids` (+ `shared_group_permissions`) — enforced by `user_visible_filter()` and `can_manage()` in [helpers.py](backend/app/api/v1/helpers.py). One deployment equals one company.

### 2.2 Agents already have external surfaces

| Surface | Path | Auth |
| --- | --- | --- |
| Agents as MCP tools | [mcp_endpoint.py](backend/app/api/v1/mcp_endpoint.py) — `POST /mcp/user`, `/mcp/global` | `/mcp/user`: `fcp-` API key or OAuth 2.1 JWT. `/mcp/global`: dedicated `mcp-` global key (bcrypt-checked against `PlatformSettings`) or OAuth — **not** `fcp-` ([mcp_endpoint.py:239](backend/app/api/v1/mcp_endpoint.py#L239)) |
| Platform tools as MCP | [mcp_platform.py](backend/app/api/v1/mcp_platform.py) — `/mcp/opencti|openaev|opencrq` | same, plus Ed25519 identity delegation |
| Stateless task execution | [extension.py](backend/app/api/v1/extension.py) — `POST /api/v1/extension/execute-task` | `get_current_user` — session JWT, `fcp-` key or trusted platform JWT; gated to `browser-`-prefixed task slugs ([extension.py:44](backend/app/api/v1/extension.py#L44)) |
| Agent-as-model | [llm_proxy.py](backend/app/api/v1/llm_proxy.py) | `fcp-` key — **not mounted when `platform_mode == "xtm_one"`** |
| Definition portability | [agentschema/](backend/app/services/agentschema/) | — |

### 2.3 Substantial OAuth reuse already exists

[mcp_oauth.py](backend/app/api/v1/mcp_oauth.py) is a full OAuth 2.1 authorization server: RFC 9728 protected-resource metadata, RFC 8414 AS metadata, RFC 7591 dynamic client registration (`DCR_RATE_LIMIT = 5`/hour, bounded client table, kill switch), and PKCE **S256-only** (`SUPPORTED_CODE_CHALLENGE_METHODS = ("S256",)`, [mcp_oauth.py:67](backend/app/api/v1/mcp_oauth.py#L67)). The `/.well-known` prefix is already mounted ([main.py:3214](backend/app/main.py#L3214)).

**Gap:** RFC 8628 Device Code — listed in the A2A evaluation request for evaluation and required outright by the implementation request — is not implemented.

### 2.4 Run model

[assignment.py](backend/app/models/assignment.py): `AssignmentRun` with `parent_run_id` for the delegation chain, and the documented lifecycle at [assignment.py:417](backend/app/models/assignment.py#L417):

```
pending → delayed → running → awaiting_human → completed / failed
```

Durable long-running work goes through `OrchestrationExecution` plus a transactional outbox ([orchestration.py](backend/app/models/orchestration.py), [orchestration_engine.py](backend/app/services/orchestration_engine.py)).

`awaiting_human` is a genuine structural match for both A2A's `INPUT_REQUIRED` task state and MCP's MRTR `resultType: "input_required"`. The match is cleaner on the A2A side: A2A's state is agent-to-agent by design, whereas MRTR's surviving flavour expects a human to answer (§7.3).

### 2.5 Concrete gaps against A2A v1.0's data model

- **No `Part` union.** Content is split across `ChatMessage.content`, `ChatMessage.files: list[FileRef]` ([base.py:1067](backend/app/llm/base.py#L1067), [:1083](backend/app/llm/base.py#L1083)) and stringly-typed `ToolResult.content` ([base.py:431](backend/app/tools/base.py#L431)). A2A v1.0 needs a union of `text` / `raw` / `url` / `structuredData`.
- **No A2A-shaped task surface.** `SubAgentTask` does have a paginated, visibility-scoped REST surface (`GET /sub-agent-tasks`, [chat.py:6405](backend/app/api/v1/chat.py#L6405)) and `AssignmentRun` has UI-scoped list endpoints — but neither speaks A2A's `ListTasks` contract (task states, artifacts, pagination semantics), so `ListTasks` is still a new public surface, mapped rather than greenfield.
- **No structured error envelope.** Free-text strings plus ad-hoc classifiers (`is_rate_limit_error()`, `overflow_error_type()`).
- **Chat/agent streaming is SSE in its own vocabulary** — `POST /api/v1/web/chat/messages` in [chat_web_stream.py](backend/app/api/v1/chat_web_stream.py) streams a turn's frames to a web session. SSE patterns exist elsewhere in the API too (datasets, activity, public chat, platform, LLM proxy), so the machinery is familiar — but none speaks A2A's streaming events, and none is on the agent-invocation path an A2A server would extend.
- **No agent-card or signature-verification concept.** Confirmed greenfield: zero repo hits for `A2A` / `agent2agent`.

### 2.6 The MCP implementation is pinned to an old revision

`MCP_PROTOCOL_VERSION = "2025-03-26"` ([mcp_endpoint.py:58](backend/app/api/v1/mcp_endpoint.py#L58)); the SDK is `mcp==1.28.1` (`backend/pyproject.toml`), uploaded 2026-06-26 and predating the current revision. The current specification is **2026-07-28** (S6/S7), and SDK support for it begins at `mcp` **2.0.0** (released 2026-07-28; the 1.x line receives critical fixes only — S36).

This is stated here rather than buried: MCP scores highest in §5 partly because XTM One already runs it, but XTM One runs a revision roughly sixteen months old. With MCP not retained as the interop solution (§7, §11), the upgrade is off the interop critical path — but it still gates any use of the inbound bridge beyond synchronous invocation (§11.1), and it remains platform debt (F5, F6): the 2026-07-28 revision removed the `initialize` handshake, deprecated sampling/roots/logging, and deprecated the DCR flow this codebase implements.

### 2.7 Auth and RBAC posture

Binary roles (`admin` / `user`, no capability table). `fcp-` API keys are **unscoped and non-expiring** and authenticate the REST API, the LLM proxy and `/mcp/user` alike (`/mcp/global` requires a separate `mcp-` global key). `is_admin=True` bypasses `user_visible_filter` entirely. `SECRET_KEY` simultaneously signs session JWTs, derives the Fernet key for every stored secret, and derives the Ed25519 cross-platform identity.

Every one of these becomes a named finding once agents are exposed to external callers (§13).

---

## 3. Evaluation criteria and weights

**Fixed on 2026-08-13, before any candidate was scored.** Scores are 0–5 and weighted to a 100-point total.

Rubric anchors: **0** = absent or actively disqualifying · **1** = exists on paper only · **2** = usable with significant work or significant gaps · **3** = adequate, with known limits · **4** = strong · **5** = best available, with evidence.

| # | Criterion | Weight | Weighting rationale |
| --- | --- | --- | --- |
| 1 | Real-world adoption evidence | **20** | The parent issue's actual open question. Governance is already known-strong for A2A; adoption is what is unproven. Weighted highest so the answer turns on evidence rather than on branding |
| 2 | Security and identity model | **15** | XTM One is a security platform. Exposing agents to external callers is the highest-risk surface in the whole proposal |
| 3 | Transport and data-model fit | **12** | Determines whether the existing run model can back the protocol or whether a parallel one is needed |
| 4 | Governance and standard maturity | **12** | Spec stability and breaking-change record. Deliberately scored **separately** from adoption, per the parent's instruction |
| 5 | Implementation cost — XTM One as **server** | **10** | The issue requires server and client costed separately; they are genuinely asymmetric here |
| 6 | Implementation cost — XTM One as **client** | **10** | ditto |
| 7 | Multi-tenancy fit | **8** | Lower than it first appears: XTM One has no tenant entity, so this is a mapping question, not a blocking one |
| 8 | Conformance / interop test tooling | **7** | Determines whether "compliant" is verifiable or merely asserted |
| 9 | Lock-in and exit cost | **6** | Real but secondary while nothing is committed |

Higher is better throughout, including the two cost criteria — a **5** means *low* cost.

> **Limitation.** The criteria and weights above were fixed before the matrix in §5 was populated, but that ordering is self-reported rather than evidenced by commit history. §9.1's sensitivity analysis is included so the conclusion can be checked without relying on it.

---

## 4. Candidate set

Retained (full inventory and exclusion register in the literature review, §6):

| ID | Candidate |
| --- | --- |
| **A** | A2A v1.0 (v1.0.0, 2026-03-12) |
| **B** | MCP as an interoperability substrate (revision 2026-07-28) |
| **C** | AGNTCY — OASF / Agent Identity / SLIM |
| **D** | Framework-native paths (Google ADK, Microsoft Foundry, LangGraph, AutoGen) |
| **E** | Generic baseline — authenticated HTTP+JSON / OpenAPI, i.e. formalising what already exists |

Excluded with reasons recorded: **ACP** (archived 2025-08-27, merged into A2A — S8), **ANP** (no stable spec, no production evidence — S16), **Microsoft AgentSchema** (wrong axis, already adopted), **Agent Plugins** (wrong axis), **AP2** and **Huawei A2A-T** (out of domain; A2A-T is independently verifiable — TM Forum IG1453, Huawei's MWC 2026 open-sourcing, S34 — but a telecom vertical).

---

## 5. Comparison matrix

**This table ranks candidates; it does not decide the outcome** — §7 does, and the gate is applied first.

Each cell carries a source reference or an explicit `no evidence found`.

| # | Criterion (weight) | **A** A2A | **B** MCP | **C** AGNTCY | **D** Framework | **E** Generic |
| --- | --- | :-: | :-: | :-: | :-: | :-: |
| 1 | Adoption evidence (20) | 3 | 5 | 2 | 3 | 5 |
| 2 | Security & identity (15) | 4 | 4 | 4 | 2 | 2 |
| 3 | Transport & data fit (12) | 3 | 5 | 2 | 2 | 4 |
| 4 | Governance & maturity (12) | 5 | 4 | 3 | 1 | 3 |
| 5 | Server cost (10) | 2 | 5 | 1 | 2 | 4 |
| 6 | Client cost (10) | 3 | 5 | 1 | 2 | 3 |
| 7 | Multi-tenancy fit (8) | 3 | 4 | 2 | 2 | 4 |
| 8 | Conformance tooling (7) | 4 | 3 | 2 | 1 | 2 |
| 9 | Lock-in / exit (6) | 4 | 4 | 3 | 1 | 5 |
| | **Weighted total** | **68.4** | **89.0** | **45.6** | **39.0** | **72.0** |
| | **Evidence confidence** | High | High | Medium | Low | High |

**Reading the confidence row.** Scores are only as good as the search behind them, and a `no evidence found` marker looks identical whether the search was exhaustive or cursory. The row records how much evidence each column actually rests on:

- **A2A, MCP — high.** Both were investigated across many searches and primary sources: specifications, release histories, SDK registries, conformance repositories and vendor documentation.
- **Generic — high**, for a different reason: it is XTM One's own code, verified directly against the repository.
- **AGNTCY — medium.** Adoption, governance and conformance rest on a thorough search (52 repositories, an IETF draft, an architecture paper, language and SDK data — S24–S28). Criteria **3** and **7** do not, and are the weakest cells in the table.
- **Framework-native — low.** Per-framework A2A support was never verified framework by framework; criteria **3**, **7** and **8** are inferences rather than findings.

**Individually low-confidence cells:** AGNTCY 3 and 7; Framework 3, 7 and 8. None is load-bearing — both candidates fail the reachability gate (§7) — but a reader should not treat those numbers as equal in weight to the rest.

### 5.1 Justifications

**1 — Adoption.** *A2A 3:* real vendor documentation exists (S5 Microsoft, S14 SAP toolkit repository), but the strongest single artefact is preview-grade (§1, finding 2); the 150-org figure is a foundation press release (S4); production-deployment claims name industries, not customers. At least three first-hand practitioner accounts in S13, alongside a reported ~10.9M vs ~257M monthly SDK download gap against MCP (single-commenter figures; the MCP side is directionally consistent with the ~470M/month the MCP project itself reported in July 2026 — S6). *MCP 5:* directly verifiable — XTM One runs it in production in both directions today. *AGNTCY 2:* active multi-component development (52 repos, ~2 155 stars, weekly pushes — S24), an IETF Internet-Draft for the Agent Directory Service (S25), a companion architecture paper (S26) and one reported vendor integration with Oracle's Open Agent Specification. Held at 2 rather than 3 because named **production** users remain `no evidence found` and the community scale is modest against MCP's. *Framework 3:* the frameworks are widely deployed, but as an interop *path* they are fragmented and mostly resolve to A2A anyway. *Generic 5:* HTTP+JSON is universally adopted and three variants already ship in this codebase.

**2 — Security & identity.** *A2A 4:* broad scheme support — API key, HTTP Basic/Bearer, OAuth 2.0 (auth code, client credentials, device code), OIDC, mTLS — plus optional Agent Card signatures; v1.0 removed implicit and password grants (S1, S2). Held below 5 by the S13 critique that A2A presumes a well-defined agent identity that does not inherently exist. *MCP 4:* the 2026-07-28 revision added RFC 9207 issuer validation, bound client credentials to their issuing server, and moved from DCR to CIMD (S6). *AGNTCY 4:* cryptographically verifiable cross-organisation identity is the project's core proposition and targets precisely A2A's weak spot — scored on the model, with the adoption penalty taken in criterion 1. *Framework 2:* whatever the vendor provides; Foundry mandates Entra ID (S5), which is sound but vendor-bound. *Generic 2:* built from scratch on top of §2.7's posture, which is the weakest link.

**3 — Transport & data fit.** *A2A 3:* task states map well (`INPUT_REQUIRED` ↔ `awaiting_human`, `SUBMITTED`/`WORKING` ↔ `pending`/`running`), but the `Part` union, A2A's streaming events against the platform's own SSE frames, and the ProtoJSON error envelope are all real mapping work (§2.5). *MCP 5:* XTM One already speaks Streamable HTTP JSON-RPC 2.0 in both directions; the Tasks extension's **polling** model (`tasks/get`) fits the `AssignmentRun` model better than streaming does, and MRTR's `input_required` maps onto `awaiting_human` (S6). *AGNTCY 2:* **low confidence** — SLIM is a distinct messaging substrate rather than an HTTP surface, so it would not sit beside the existing endpoints, but no fit analysis against XTM One's data model was carried out. *Generic 4:* fits by construction, but supplies no task-lifecycle semantics.

**4 — Governance.** *A2A 5:* Linux Foundation, multi-vendor TSC, dated release history, a documented breaking-change surface, an ADR process, and it absorbed a competitor (S2, S3, S8). Genuinely best in class. *MCP 4:* donated in December 2025 to the Agentic AI Foundation, a Linux Foundation directed fund (S35), with a formal feature lifecycle and a twelve-month deprecation window, but the 2026-07-28 revision is self-described as the largest since launch and deprecates four established features — the governance is new, and the churn is real (S6). *Framework 1:* single-vendor.

**5 — Server cost.** *A2A 2:* new public endpoint, agent-card generation/hosting/signing, `Part` mapping, a new paginated `ListTasks` surface, an A2A-shaped SSE path, version negotiation, per-agent opt-in, and RBAC that does not currently exist (§2.7). *MCP 5:* `/mcp/user` and `/mcp/global` already expose agents; the increment is adopting the Tasks extension — but see §2.6, which is where MCP's cost actually sits. *Generic 4:* `POST /api/v1/extension/execute-task` exists; the work is a published contract, an OpenAPI document and docs.

**6 — Client cost.** *A2A 3:* `a2a-sdk` 1.1.2 (2026-07-22) supports Python 3.10–3.14 with a `fastapi` extra and covers all three transports (S12, S19) — a good fit for this backend; external-agent registration can reuse the `MCPServer` model; card signature verification is new. *MCP 5:* [mcp_client.py](backend/app/tools/mcp_client.py) already does discovery, circuit breaking and failure tracking across stdio / SSE / Streamable HTTP, with OAuth header injection supplied by `build_mcp_runtime()` ([helpers.py:1527](backend/app/api/v1/helpers.py#L1527)). *Generic 3:* cheap per partner, amortises across none.

**7 — Multi-tenancy.** *A2A 3:* the spec's `tenant` is an **optional opaque routing identifier** (S1), so a server with no tenancy concept can simply omit it — a clean answer, but one that yields nothing if a partner sets it. *MCP 4:* no tenant concept to reconcile; existing per-user/per-group/platform scoping already governs those surfaces. *AGNTCY 2, Framework 2:* **low confidence** — `no evidence found`; neither candidate's tenancy model was investigated. *Generic 4:* self-defined.

**8 — Conformance.** *A2A 4:* the TCK covers three transports across RFC 2119 levels, and the ITK publishes a cross-SDK dashboard (S10, S11) — comfortably best in class. Held below 5 because the TCK confers no certification, the ITK repository is young (~46 commits), and **.NET is a placeholder** despite Microsoft Agent Framework for .NET being a headline integration. *MCP 3:* the governance SEPs require conformance-suite scenarios before a Standards Track SEP reaches Final — a real mechanism — but suite maturity is `no evidence found`. *AGNTCY 2:* `csit`, a live continuous-integration suite across its own components (S24) — internal CI rather than a published third-party conformance regime, so below A2A's TCK/ITK. *Framework 1:* `no evidence found`.

**9 — Lock-in.** *Generic 5:* nothing to exit, and nothing gained. *A2A / MCP 4:* LF-governed, multi-vendor, multiple SDKs; exit is deleting an endpoint. *Framework 1:* highest lock-in by construction.

---

## 6. The interoperability targets

Everything downstream is evaluated against two named platforms rather than against the abstract problem: **customer agents on Google Vertex AI Agent Runtime** and **customer agents on Microsoft Azure Foundry** (§10.1). Both are documented from primary vendor sources.

The two tables below are organised by protocol family so that **every candidate in §4 is visibly covered**, including the ones the answer is "no" for. The `Bears on` column maps each row to the candidate and gate direction it determines in §7.

### 6.1 What the targets *expose* — determines the **outbound** gate

Can XTM One call their agent using this protocol?

| Protocol family | Bears on | Vertex AI Agent Runtime | Azure Foundry |
| --- | :-: | --- | --- |
| **A2A** | A · outbound | ✅ Authenticated agent card via `handle_authenticated_agent_card`; `on_message_send`, `on_get_task`, `on_cancel_task`. Streaming supported. **Preview**, Pre-GA terms (S20) | ✅ v1.0 + v0.3, but **JSONRPC only** for v1.0, **text only**, **no streaming**; card at a custom path, Entra required, anonymous access unsupported; the card is served per version by path, with no version-less default. **Preview** (S5, S30) |
| **MCP** | B · outbound | ✗ `no evidence found` — consumes MCP tools, does not publish agents as MCP servers | ✗ `no evidence found` — a *toolbox* publishes an MCP endpoint for tools, but a Foundry **agent** is exposed via A2A (S21, S22) |
| **OASF / SLIM / AGNTCY Identity** | C · outbound | ✗ `no evidence found` | ✗ `no evidence found` |
| **Vendor-specific HTTP** | D, E · outbound | ✅ `POST /v1/{name}:query` and `:streamQuery` on `reasoningEngines` (S29) | ✅ The **Responses API** is the single entry point for every agent type; published agents are invocable programmatically, with an **Invocations protocol** for custom apps and services (S30) |

### 6.2 What the targets *consume* — determines the **inbound** gate

Can their agent call an XTM One agent exposed this way?

| Protocol family | Bears on | Vertex AI Agent Runtime | Azure Foundry |
| --- | :-: | --- | --- |
| **A2A** | A · inbound | ✅ via the A2A Python SDK or plain HTTP (S20) | ✅ A2A tool on a GA Python SDK. Auth: none, custom header keys, OAuth2, user Entra token, project managed identity, agentic identity. **Discovers a third-party card anonymously by default**; Control Plane registration reads `/.well-known/agent-card.json` (S21) |
| **MCP** | B · inbound | ✅ consumes MCP tools (S20) | ✅ streamable HTTP, Entra OAuth (S22) |
| **OASF / SLIM / AGNTCY Identity** | C · inbound | ✗ `no evidence found` | ✗ `no evidence found` |
| **Arbitrary HTTP** | D, E · inbound | ✅ by writing a custom tool | ✅ by writing a custom tool or function |

**Caller credentials required:** Vertex needs Google Cloud credentials with `cloud-platform` scope; Foundry needs a Microsoft Entra token and the Foundry Agent Consumer role or higher.

### 6.3 Two asymmetries that drive the analysis

- **Both platforms are better at calling out than at being called.** Foundry's outbound A2A tool ships on a GA SDK with six authentication modes; its incoming A2A endpoint is preview, text-only and non-streaming.
- **Both consume MCP but neither publishes agents over it.** That single fact is what separates the inbound and outbound verdicts for candidate B in §7.2, and it is the only categorical elimination the gate produces — under a bidirectional requirement, the decisive one.

---

## 7. Reachability gate

A weighted average cannot express "this candidate cannot perform the task at all" — it will let a cheap, well-adopted, incumbent option outscore the only option that actually connects. Reachability is therefore a **precondition evaluated before the matrix decides anything**, not a criterion inside it.

The gate is applied **twice per candidate**, reading the verdicts directly off §6.1 (what the targets expose → outbound) and §6.2 (what they consume → inbound). Failing the outbound gate does not mean a candidate is useless for interop, and the two must not be conflated.

### 7.1 Results

| Candidate | Inbound | Outbound | Basis |
| --- | :-: | :-: | --- |
| **A** A2A v1.0 | ✅ | ✅ | Both directions supported by both targets. Each target needs its own credential flow outbound — neither serves an anonymous card |
| **B** MCP | ✅ **ships today** | ❌ | Targets consume MCP but publish no agent over it. The only categorical elimination in the gate |
| **C** AGNTCY | ❌ | ❌ | `no evidence found` for OASF, SLIM or AGNTCY Identity on either target |
| **D** Framework-native | ✅ | ✅ per vendor | Vendor SDK against each platform's own HTTP entry point |
| **E** Generic HTTP+JSON | ✅ | ✅ per vendor | Same entry points, called directly with `httpx` |

### 7.2 What the results establish

**Only AGNTCY is eliminated outright.** Both directions fail, and the failure is about target support rather than about AGNTCY's own maturity — its components are well documented (S24–S28); what is absent is any sign of them on Vertex or Foundry.

**MCP is eliminated for outbound only, and is the cheapest option inbound** — it already ships. Its limits inbound are not reachability but richness: the customer hand-configures an endpoint instead of discovering a card, what is advertised is a tool with free-text input rather than an agent with skills and identity, and long-running task semantics need the 2026-07-28 upgrade (§2.6). Under the bidirectional requirement (§10.1) the outbound failure is disqualifying: MCP remains the tool-access substrate and an interim inbound bridge (§11.1), not the interop solution.

**A2A's advantage is amortisation, not unique reachability.** This corrects a natural but wrong reading of the gate. Framework-native and generic HTTP both reach both targets — one A2A client integration serves both targets and any future A2A-speaking partner, whereas those paths mean N bespoke integrations for N vendors, each with its own SDK, auth model, payload shape and lifecycle. With bidirectional interop required outright rather than discovered customer by customer, that amortisation is decisive now rather than eventually: the bespoke path pays per vendor *and per direction*, and provides neither discovery nor a task lifecycle at either end.

### 7.3 MRTR does not change MCP's outbound answer

If MCP's 2026-07-28 revision lets a server ask for input mid-task, could inbound-plus-MRTR substitute for agent-to-agent interop entirely? Two answers, both negative.

**It cannot do outbound, structurally.**

- **The direction is server → client, never server → another server.** The specification's design principles bar a server from seeing into other servers; cross-server interaction stays under host control (S7). An XTM One agent behind an MCP endpoint cannot use MRTR to reach a third agent.
- **It is a re-send, not a callback.** The server returns `resultType: "input_required"`, and the client re-issues the original request with `inputResponses` attached. Nothing is suspended server-side — that is what makes it work with the stateless core.

**Its autonomous flavour is deprecated — the more important finding.**

MRTR is **not** inherently human-in-the-loop. It carries three input kinds, and only one needs a person:

| Input kind | What answers it | Human required? | Status in 2026-07-28 |
| --- | --- | --- | --- |
| **Sampling** | The client's **host model** — spec flow is Client → Host → AI → back (S7) | **No** | **Deprecated** (SEP-2577), twelve-month runway; new implementations SHOULD NOT adopt (S6) |
| **Elicitation** | The **user** — server sends a JSON Schema, client renders a form (S23) | **Yes, as specified** | Active |
| **Roots** | Client configuration | No | Active, but no clarification semantics |

> **The pincer:** the one flavour that delivers *autonomous* agent-to-agent feedback — sampling, answered by the calling agent's own model with no person involved — is exactly the flavour being retired. The flavour that survives is specified as human-facing. So building agent-to-agent clarification on MRTR means either adopting a primitive the specification tells you not to adopt, or depending on unspecified client behaviour.

Two qualifications, recorded as uncertainty rather than as findings:

- **Elicitation is not *guaranteed* to involve a human.** Nothing prevents an agentic client from auto-answering; the spec simply does not underwrite it. An optimistic reading is possible, just not standards-backed.
- **Whether either target platform implements MRTR at all is unverified.** `no evidence found` for Foundry or Vertex; the revision is two weeks old.

**What this leaves.**

MRTR still improves the inbound path in a real way: an XTM One agent can pause for input instead of failing or guessing, which maps onto the existing `awaiting_human` state (§2.4). But it improves it for **human-attended** flows. For a fully autonomous agent-to-agent exchange, MCP's standards-backed answer is being removed rather than added.

Practical consequence (§11.1): any flow that needs the XTM One agent to ask the *calling agent* for clarification mid-task is beyond what the interim MCP bridge can carry — that capability is precisely A2A's `INPUT_REQUIRED`, and precisely what MCP is withdrawing. It is one more reason the bridge is a bridge, not a substitute for the A2A server.

---

## 8. Implementation cost, separated

### 8.1 XTM One as an A2A **server** — the expensive direction

| Work item | Basis |
| --- | --- |
| Agent Card generation, hosting, JWS signing + JSON canonicalisation | Nothing exists; `/.well-known` prefix is available ([main.py:3214](backend/app/main.py#L3214)) |
| New A2A endpoint (transport choice) | Foundry supports only JSONRPC for v1.0 (S5), which effectively decides it if Foundry is a target |
| `Part` union mapping | `ChatMessage.content` / `files` / `ToolResult.content` → `text` / `raw` / `url` / `structuredData` (§2.5) |
| `ListTasks` with pagination | New public surface; `AssignmentRun` list endpoints are UI-scoped |
| Task state mapping | Mostly clean; `awaiting_human` ↔ `INPUT_REQUIRED`. `AUTH_REQUIRED` and `REJECTED` have no counterpart |
| SSE streaming path | The web chat's SSE is session-bound and speaks its own frames; the LLM proxy's SSE is not mounted in XTM One mode |
| Structured error envelope | Free-text errors today (§2.5) |
| `A2A-Version` negotiation + v0.3 compatibility | Both target platforms speak **v1.0** and declare no version when fetching a card, so an undeclared fetch is served v1.0 (S5); v0.3 is served to a partner that asks for it |
| Per-agent, per-scope exposure opt-in + RBAC | Binary roles today; no capability expresses "may be exposed externally" (§2.7) |
| Device Code flow (RFC 8628) | Not implemented (§2.3) |

The RBAC and API-key items are the ones that should worry a security platform: today, any `fcp-` key is an unscoped, non-expiring bearer of its user's full identity across every surface.

### 8.2 XTM One as an A2A **client** — the cheap direction

| Work item | Reuse available |
| --- | --- |
| External agent registration + credential storage | `MCPServer` model and its encrypted-credential pattern map almost directly |
| Agent Card retrieval + signature verification | New; must be **fail-closed** |
| Invocation, timeout, cancellation, failure handling | `consult`/`delegate` semantics and `_AGENT_TOOL_TIMEOUT` patterns already exist |
| SDK | `a2a-sdk` 1.1.2 with the `fastapi` extra, Python 3.10–3.14 (S12) |
| Frontend registration surface | `frontend/src/pages/mcp-server-detail` is a direct template |

**Client-side is roughly a third of the cost of server-side and carries no new external attack surface.**

> **Costing note.** Two qualifications stand. The table above **understates** client-side cost for these specific targets — neither serves an anonymous card, so each needs its own credential flow (Google `cloud-platform`-scoped tokens for Vertex, Entra tokens for Foundry — S20, S5). And it **overstates** server-side cost relative to pass 1, because Foundry discovers a standard card at `/.well-known/agent-card.json` anonymously with OAuth2 (S21), which the existing OAuth 2.1 server can serve.

### 8.3 The other candidates, costed the same way

Criteria 5 and 6 score every candidate; this table carries the reasoning behind those cells.

| Candidate | As **server** | As **client** |
| --- | --- | --- |
| **B** MCP | **Score 5.** `/mcp/user` and `/mcp/global` already expose agents over Streamable HTTP JSON-RPC 2.0. The increment is adopting the Tasks extension so long-running runs are pollable rather than synchronous. The real cost is not the extension — it is the §2.6 revision upgrade underneath it (handshake removal, DCR → CIMD, four deprecations) | **Score 5.** [mcp_client.py](backend/app/tools/mcp_client.py) already does discovery, circuit breaking and failure counting across stdio / SSE / Streamable HTTP, with OAuth header injection supplied by `build_mcp_runtime()` ([helpers.py:1527](backend/app/api/v1/helpers.py#L1527)). Consuming an external agent-as-MCP-server needs no new transport code |
| **C** AGNTCY | **Score 1.** Three separate specifications (OASF, Identity, SLIM), and the canonical implementations are in languages this backend does not use: `oasf` is **Elixir**, `slim` is **Rust**, `dir` and `identity` are **Go** (S24). A reference application exists (`coffeeAgntcy`, Python, 107★), but the Python bindings for the components that would matter are `dir-sdk-python` (3★) and `slim-mcp-python` (2★), and `app-sdk` explicitly self-describes as "not the canonical SDK for AGNTCY subsystems". SLIM is also a distinct messaging substrate, not an HTTP surface that could sit beside the existing endpoints | **Score 1.** Same language mismatch, plus an identity model with no counterpart in XTM One's auth stack |
| **D** Framework-native | **Score 2.** Per-vendor work with no amortisation: a Foundry-shaped surface does not serve an ADK caller. In practice this collapses into building A2A anyway, since that is what the frameworks target — at which point it is §8.1's cost with vendor-specific constraints layered on (S5's JSONRPC-only, text-only, no-streaming subset) | **Score 2.** One integration per vendor, each with its own SDK, auth model and lifecycle |
| **E** Generic HTTP+JSON | **Score 4.** `POST /api/v1/extension/execute-task` already accepts a task and returns a result. The work is a published contract, an OpenAPI document, a task-status surface and partner-facing docs — not new protocol machinery | **Score 3.** `httpx` plus a bespoke contract per partner. Cheap for the first partner, linear thereafter, and it amortises across none |

---

## 9. Robustness of the result

### 9.1 Sensitivity to the incumbency advantage

A weighted matrix that hands victory to the incumbent deserves scepticism, so the obvious objection is tested directly: remove both implementation-cost criteria (5 and 6, worth 20 points — the entire incumbency advantage) and rescore over the remaining 80 points.

| Candidate | Full (100) | Cost criteria removed (of 80) | Normalised to 100 |
| --- | :-: | :-: | :-: |
| **B** MCP | 89.0 | 69.0 | **86.3** |
| **E** Generic | 72.0 | 58.0 | 72.5 |
| **A** A2A | 68.4 | 58.4 | 73.0 |
| **C** AGNTCY | 45.6 | 41.6 | 52.0 |
| **D** Framework | 39.0 | 31.0 | 38.8 |

MCP still wins, and its margin barely moves. The one change is that A2A and the generic baseline swap places and land effectively tied (73.0 vs 72.5) — which is itself informative: absent implementation cost, adopting A2A today buys about as much as writing down the contract that already exists.

### 9.2 What would change the answer

Raising criterion 1 (adoption) to 30 and dropping criteria 5 and 6 to 5 each would not flip the result either, because MCP scores 5 on adoption. The result is genuinely driven by MCP's adoption and fit, not by the weighting.

But the honest conclusion is that **no weighting flips this matrix, because what decides the outcome is not in it.** Two preconditions sit outside the scoring: whether demand exists at all (§10), and which *directions* the requirement covers (§7). Neither is a property of a protocol, so neither can be a criterion. The requirement is bidirectional (§10.1); the gate then removes MCP outright, and among the survivors the generic baseline's higher total (72.0 vs 68.4) does not survive contact with what the requirement actually asks for — discovery, agent-level semantics and a task lifecycle at both ends, amortised across partners rather than rebuilt per vendor and per direction (§7.2). The matrix ranks quality; the requirement and the gate select.

---

## 10. Demand signal

GitHub sweep, 2026-08-13 (full table in the literature review, §8): every issue in `XTM-One-Platform/xtm-one` touching A2A, interoperability or external agents is **internally authored** — the A2A evaluation request (Romain D., 2026-07-21), the literature review and the implementation request (Antoine Gaston, 2026-08-10), plus adjacent ones on a portable plugin format and an API gateway. An org-wide search returned nothing further.

**Zero externally-originated demand within the searched scope.** Notion, sales/CS conversations and direct partner asks were out of reach for that sweep.

### 10.1 Demand established outside the sweep

**Source: product owner, stated directly on 2026-08-13.** XTM One's first external agent interoperability targets are **customers running agents on Google Vertex AI** and **customers running agents on Microsoft Azure Foundry**, and the requirement is **explicitly bidirectional**: their agents must be able to task XTM One agents, and XTM One agents must be able to delegate to theirs. Inbound-only coverage does not satisfy it.

Recorded honestly: this is **product-owner direction, not a written artefact**. It is not traceable to a ticket, a Notion entry or a customer email in the way §2's sources are. It should be substantiated with at least one concrete customer reference before Phase 1 is scoped (§11.3).

The bounded GitHub result above is retained rather than deleted: it remains an accurate statement that the demand is not recorded in the issue tracker, which is itself worth fixing.

### 10.2 Ecosystem observation

At low confidence: OpenCTI, OpenAEV and OpenCRQ are already integrated over MCP through `/mcp/{opencti,openaev,opencrq}`, so the intra-Filigran case needs no new protocol. Filigran shipped an `xtm-mcp` server and has since archived it — v1.0.0 released 2026-03-05, archived 2026-07-05, OpenCTI 6.8 tie-in in its README (`FiligranHQ/xtm-mcp`, S31); a reported embedded successor rests on secondary coverage only and is not leaned on.

---

## 11. Recommendation

### 11.1 Implement A2A v1.0, both directions

The requirement is bidirectional (§10.1), so both of the A2A evaluation request's surfaces are in scope. Sequencing is decided by reachability and reuse, not by whether each direction is needed.

**Phase 1 — A2A server (inbound).** The direction that makes XTM One reachable from both platforms' *stronger* side — their outbound tooling (GA-grade on Foundry) rather than their preview incoming endpoints:

- Standard Agent Card at `/.well-known/agent-card.json` — Foundry's Control Plane discovers it, and fetches it **anonymously by default** for non-Foundry targets (S21), so the card must be safe to expose unauthenticated.
- **A2A v1.0 over JSON-RPC**; Foundry supports only JSONRPC for v1.0 (S5), so that covers both targets without gRPC or HTTP+JSON work.
- **v0.3 compatibility is a requirement, not a fallback** — Foundry's card is served per version by path (`…/agentCard/v0.3`, `…/agentCard/v1.0`), and its outbound `a2a` tool accepts only `a2a_version: "1.0"`, so both must be served (S5).
- OAuth 2.0 client credentials, reusing the existing OAuth 2.1 server (§2.3). Per-agent, per-scope opt-in, never on by default.
- Cost is §8.1's table — the expensive direction. The RBAC and API-key items (F1–F3) are prerequisites inside it, not parallel work.

**Phase 2 — A2A client (outbound).** Roughly a third of the server's cost and no new inbound attack surface (§8.2), but costlier per target than a generic estimate suggests: neither platform serves an anonymous card, so each needs its own credential flow — Google `cloud-platform`-scoped tokens for Vertex (S20), Entra tokens for Foundry (S5). Registration and credential storage reuse the `MCPServer` pattern; card signature verification is new and must be **fail-closed** (F7).

**Interim bridge, not a phase.** While Phase 1 lands, the shipped `/mcp/user` endpoint already lets a customer's Foundry or Vertex agent invoke an XTM One agent synchronously — Foundry's MCP tool connection (streamable HTTP, Entra OAuth) is the documented path (S22, S20) — which is useful for validating the integration with a named customer early. It is not a substitute: no discovery, a free-text tool rather than an agent with skills and identity, and anything beyond synchronous invocation needs the MCP 2026-07-28 upgrade first (§2.6), whose autonomous mid-task clarification primitive is being withdrawn anyway (§7.3).

**Security prerequisites (F1–F3, §13) apply before any external exposure — bridge included.** `/mcp/user` *already* exposes agents to external callers on unscoped, non-expiring API keys and binary RBAC. Those findings are live in production today; putting a named customer behind that surface, or standing the A2A server up next to it, makes them current risk to resolve, not A2A's cost.

### 11.2 Confidence: high on the standard, medium on timing

High that A2A v1.0 is the right standard: the requirement is bidirectional (§10.1), MCP fails the outbound gate categorically (§7.1), and the bespoke alternative pays per vendor and per direction while providing neither discovery nor a task lifecycle (§7.2, §9.2).

Medium on timing, because both platforms' A2A surfaces are **Preview** (§11.3) and the demand statement is still product-owner direction rather than a written customer artefact (§10.1).

### 11.3 Evidence gaps limiting this recommendation

1. **The demand statement is unsubstantiated in writing** (§10.1) — product-owner direction, no named customer artefact. Substantiate before Phase 1 is scoped.
2. **Both target A2A surfaces are Preview** — Vertex under Pre-GA terms (S20), Foundry's incoming A2A explicitly not recommended for production (S5). Breaking changes mid-build are permitted and likely (T3).
3. **Autonomous mid-task clarification over MCP is doubtful, not merely unverified** (§7.3) — this bounds what the interim bridge can carry: the standards-backed autonomous route (sampling) is deprecated, the surviving route (elicitation) is specified as human-facing, and whether either target's MCP client implements the 2026-07-28 flow at all is unknown. A2A's `INPUT_REQUIRED` covers the same need natively.
4. **Vertex specifics** were assembled from documentation mid-migration between the `cloud.google.com/vertex-ai` and Gemini Enterprise Agent Platform trees; the exact A2A version served was not confirmed.
5. **Salesforce's A2A surface** is confirmed only as a marketing page; **ServiceNow** has first-party technical articles describing A2A in AI Agent Fabric (S32), though no formal docs-portal product documentation was located.
6. **The LF "production deployments" claim** (S4) names industries, not customers.
7. **ITK rendered dashboard** — inspected 2026-08-13: reachable and listing all six SDKs, but with zero completed runs, so the repository matrix remains the only usable evidence. **.NET is a placeholder**, which matters if a customer integration is .NET-based.

### 11.4 Signals to monitor

T1 — a named customer ask — has fired (§10.1). The rest are monitoring signals.

| # | Signal | Why it matters now |
| --- | --- | --- |
| T1 | ✅ **Fired.** Named customer targets on Vertex and Foundry, requirement explicitly bidirectional | Basis of the current recommendation |
| T2 | The demand statement fails to substantiate against a real customer | **Stop signal.** Would pause Phase 1 scoping — see §11.3 gap 1 |
| T3 | Either target platform's A2A surface changes shape while in Preview | Both are Pre-GA; breaking changes are permitted and likely |
| T4 | Microsoft Foundry's incoming A2A reaches GA with streaming and non-text modalities | Removes the limitation forcing text-only, JSONRPC-only scoping |
| T5 | The ITK dashboard shows .NET and Java stable on v1.0 | Removes the conformance caveat; relevant if a customer integration is .NET-based |
| T6 | A Filigran-ecosystem platform ships an A2A surface | Would extend the case from customer interop to intra-ecosystem lingua franca |
| T7 | NIST CAISI publishes an AI Agent Interoperability Profile naming A2A | Would move A2A toward a procurement requirement. NIST's AI Agent Standards Initiative is confirmed first-hand (announced 2026-02-17 — S33); **the profile name, Q4-2026 timing and A2A anchoring remain secondary reporting — verify before relying on them** |

Suggested cadence: re-read this document when Phase 1 lands, and re-run the benchmark if T2 or T3 fires.

---

## 12. Answers to the A2A evaluation request's open questions

| Question | Answer |
| --- | --- |
| Is there business/customer demand, or is this "worth watching"? | **There is demand, and it is explicitly bidirectional.** First targets are customer agents on Vertex AI and Azure Foundry (§10.1). Recorded as product-owner direction, not yet substantiated against a named customer — that substantiation is a prerequisite (§11.3) |
| Should XTM One prioritise **server** or **client**? | **Both — the requirement is bidirectional (§10.1). Server first, client second (§11.1).** Server first because it makes XTM One reachable from both platforms' stronger direction — Foundry discovers a card anonymously at `/.well-known/agent-card.json` with OAuth2 (S21) — and reuses the existing OAuth 2.1 server; client second because it is roughly a third of the cost with no new inbound attack surface (§8.2) but needs a bespoke credential flow per target (S20, S5). The implementation request keeps the two surfaces behind independent flags |
| Should the security model be reviewed before implementation? | **Yes.** §13 lists the findings. Note that several are pre-existing and matter independently of A2A |
| How would A2A's `tenant` map to XTM One's model? | **It does not, and it does not need to.** `tenant` is an *optional opaque routing identifier* (S1), not a tenancy model, and XTM One has no tenant entity at all (§2.1). As a server: omit it, and reject or explicitly ignore a partner-supplied value. **Do not map it to `is_company_managed`** — that is a visibility flag, not a routing identifier, and conflating them would create the divergent second tenancy concept the A2A evaluation request warns against |
| Should this sit under an existing Epic or a new one? | **Unanswerable** — Notion was out of reach (§10) |

---

## 13. Findings for security / compliance review

Raised, not resolved, per the issue's scope. F1–F4 and F9 are **pre-existing** and warrant attention independently of any A2A decision; F5–F8 are conditional on implementation.

**Note:** F1–F3 are not costs that A2A would introduce. `/mcp/user` already exposes agents to external callers under exactly these conditions, in production, today. They are current risk to be reviewed on their own merits, and they gate the interim bridge (§11.1) as much as the Phase 1 server.

| # | Finding | Trigger |
| --- | --- | --- |
| F1 | `fcp-` API keys are unscoped and non-expiring, and authenticate the REST API, the LLM proxy and `/mcp/user` alike (`/mcp/global` takes a separate, equally unscoped `mcp-` global key) | Pre-existing |
| F2 | Binary RBAC — no capability can express "may expose an agent externally" or "may register an external agent" | Pre-existing |
| F3 | `is_admin=True` bypasses `user_visible_filter` entirely | Pre-existing |
| F4 | `SECRET_KEY` signs session JWTs, derives the Fernet key for all stored secrets, and derives the Ed25519 cross-platform identity — a single point of compromise | Pre-existing |
| F5 | XTM One's MCP DCR implementation is deprecated by MCP 2026-07-28 in favour of CIMD (S6) | On MCP upgrade |
| F6 | `MCP_PROTOCOL_VERSION` is pinned to `2025-03-26`; the current revision removes the `initialize` handshake and deprecates sampling, roots and logging | On MCP upgrade |
| F7 | No agent-card signature-verification concept exists. If an A2A client is ever built, verification must be **fail-closed** — failure blocks invocation rather than warning and proceeding | On A2A client |
| F8 | Data residency when tasks cross vendor boundaries is unresolved (flagged in the study and implementation requests, not addressed here) | On any external interop |
| F9 | stdio MCP servers have no command allow-list — any user who can create an MCP server can spawn an arbitrary PATH executable in the backend container | Pre-existing, adjacent |
| F10 | Any design that relies on MCP **sampling** for autonomous agent-to-agent clarification is building on a primitive the specification tells new implementations not to adopt, with a twelve-month removal runway (§7.3) | On MCP upgrade / bridge use |

---

## 14. Honest caveats

- **The matrix did not decide this; the gate did.** §5 still ranks MCP highest, and that ranking is not wrong on its own terms — MCP is better adopted, better fitting and vastly cheaper *for the work it can do*. It simply cannot reach a Vertex or Foundry agent. A weighted average has no way to express that, which is why §7 exists and runs first. Readers should treat §5 as a comparison of *how good* each candidate is, and §7 as the question of *whether it applies at all*.
- **The matrix is not the decision procedure.** Two things decide this and neither is in it: whether a candidate can reach the targets (§7), and whether it covers both required directions (§7.1, §10.1). A weighted average can express neither, because both are preconditions rather than qualities. §5 ranks candidates within a direction; it does not select one.
- **MCP and A2A remain non-substitutable in one direction only.** Inbound — an external system invoking an XTM One agent — MCP already handles, and a customer willing to use it needs nothing new. Outbound delegation to a vendor-hosted agent is where only A2A reaches. MRTR does not change this (§7.3). A bidirectional requirement therefore selects A2A by construction.
- **The interim bridge is real but not free, and not unlimited.** `/mcp/user` works today for *synchronous* invocation. Long-running tasks and mid-task clarification require the MCP 2026-07-28 upgrade — the largest revision in MCP's history, which also deprecates the DCR flow this codebase implements (§2.6, F5). And autonomous agent-to-agent clarification is not reliably available even after that upgrade, because MCP is withdrawing the primitive that provided it (§7.3).
- **MRTR is not simply "human-in-the-loop", and the precise reading is worse for MCP than the loose one.** It carries an autonomous flavour, but that is the flavour being deprecated; what survives is human-facing by specification (§7.3). This limits how much weight the interim bridge can bear.
- **Both target platforms are Preview.** Vertex under Pre-GA Offerings Terms, Foundry's incoming A2A explicitly not recommended for production. Integrating against them means accepting breaking changes mid-build.
- **The recommendation is *implement against Preview surfaces*, with eyes open.** A2A's general adoption evidence is still thinner than its press releases (§5.1), both target A2A surfaces permit breaking changes (T3), and the security prerequisites (§11.1) are real work that must land first.
- **"MCP is nearly free" is qualified.** §2.6: the platform is sixteen months behind the specification, and the revision it needs is the largest in MCP's history.
- **Search effort was not equal across candidates.** A2A and AGNTCY were each investigated across many searches and primary sources. **ANP was not** — its exclusion rests on a single search and is the least-examined judgement in this document. Scores are only as good as the search behind them, and a `no evidence found` marker looks identical whether the search was thorough or cursory.
- **Scores are judgements, not measurements.** The rubric is documented and the arithmetic is checkable, but a reader who weights adoption differently is entitled to a different total. §9 exists so disagreement can be tested rather than asserted.
- **The criteria-before-scoring ordering is self-reported**, not proven by commit history (§3).

---

## 15. Scope extension — could A2A replace internal agent handover?

> **This section is outside the scope of this study and of the A2A evaluation request.** Both explicitly exclude internal agent-to-agent routing. It is added at the product owner's request (2026-08-13) to ask whether the retained solution should *also* replace the internal handover mechanism. Treat it as a proposal for a separate issue, not as part of this study's deliverable. It has not been through the same evidence discipline as §1–§14: the A2A facts are sourced, the XTM One facts are code-verified, but no external literature was surveyed on internal-transport substitution.

### 15.1 What internal handover is today

Four typed relationships, keyed per target in `Agent.handover_types` ([agent.py:309–336](backend/app/models/agent.py#L309)):

| Type | Injected tool | Semantics |
| --- | --- | --- |
| `delegate` (default) | `spawn_background_task` | Async fire-and-forget; parent continues |
| `consult` | `consult_agent` | **Synchronous nested `run_agent_chat()` in-process** ([agent_chat.py:2967](backend/app/services/agent_chat.py#L2967)); answer returns as a tool result |
| `transfer` | `transfer_to_agent` | Terminal tool; the nested transfer run resolves at [agent_chat.py:4249](backend/app/services/agent_chat.py#L4249) and the API caller then permanently re-binds the conversation ([chat.py:5444](backend/app/api/v1/chat.py#L5444)) |
| `mandatory_transfer` | *(none — hidden from the LLM)* | Deterministic post-loop routing via an LLM classifier ([routing_evaluator.py](backend/app/services/routing_evaluator.py)) |

Supporting machinery: `AgentDelegation` as a unified audit trail ([delegation.py](backend/app/services/delegation.py)), `SubAgentTask` for background work ([sub_agent.py](backend/app/services/sub_agent.py), `_MAX_DELEGATION_DEPTH = 5`, `_SUB_AGENT_TASK_TIMEOUT = 900`), cycle protection via `visited_agents` and `handover_cycle_error()` ([handover.py:892](backend/app/services/builtin_tools/handover.py#L892)), and frontend graph validation (`validateHandoverGraph()` in [graph-utils.ts:321](frontend/src/lib/graph-utils.ts#L321), surfaced by [handover-warnings.tsx](frontend/src/components/ui/handover-warnings.tsx); [agent-flow.tsx](frontend/src/components/ui/agent-flow.tsx) renders the topology but performs no validation).

### 15.2 Three tiers, assessed separately

Conflating these is what makes "use A2A internally too" sound simple. They have opposite cost/benefit.

**Tier 1 — External A2A agents become addressable as handover targets. ✅ Recommended.**

Let `handover_agent_ids` reference external A2A agents alongside internal ones, with `handover_types` still selecting `delegate` / `consult`. An external agent then appears in the flow graph, in `AgentDelegation` audit records, and in the delegation UI exactly like an internal peer.

This is the inverse of the question as posed, and it is where the value actually sits: it gives uniformity in the places that matter — the flow author's mental model, the audit trail, the `DelegationsTab` and `agent-flow` surfaces — without paying network cost for calls that are currently in-process. It also reuses the transport built for §11.1 Phase 2 rather than requiring anything new — it belongs after Phase 2 lands, as an extension of the same client work.

**Tier 2 — Align internal vocabulary with A2A's data model. ⚠️ Conditional.**

Adopt A2A's `Part` union (`text` / `raw` / `url` / `structuredData`) and task-state vocabulary internally, so the boundary mapping in §2.5 disappears rather than being written twice. `awaiting_human` already matches `INPUT_REQUIRED`.

Worth doing **only as a by-product** of the Phase 1 work, never as a standalone refactor. `ChatMessage`, `FileRef` and `ToolResult` are load-bearing across the entire agent loop; changing them for aesthetic alignment would be a large, risky change with no user-visible benefit.

**Tier 3 — Replace the internal handover transport with A2A. ❌ Reject.**

Eight reasons, in descending weight:

1. **It converts function calls into network calls.** `consult` is a nested `run_agent_chat()` in the same process and DB session. Under A2A it becomes JSON-RPC over HTTP to yourself: serialisation, network stack, auth round-trip, and a new failure mode, in exchange for nothing.
2. **A2A cannot express `transfer` or `mandatory_transfer`.** A2A models "create a task on a remote agent, receive results". `transfer_to_agent` re-binds the *conversation* — the user now talks to a different agent. `mandatory_transfer` is post-loop routing chosen by a classifier and deliberately hidden from the LLM. Neither is a task delegation. A2A would have to carry them as metadata and reimplement the semantics anyway.
3. **A2A's opacity principle destroys the shared context.** Internal handover passes `handover_context` (source agent, original question, `source_agent_response` on transfer, `visited_agents`) and `file_context`; a transfer additionally keeps the conversation's Redis `session_vars` by staying on the same `conversation_id`. A2A treats agents as opaque to each other *by design* — that is the point of the standard. Internally, that opacity is pure loss.
4. **Topology invariants have no A2A counterpart.** `_MAX_DELEGATION_DEPTH = 5`, the `visited_agents` cycle chain, and the handover editor's consult hint (`consultsBackgroundDelegator()`) all assume a known delegation graph. A2A is peer-to-peer and stateless about topology.
5. **The typed relationship model would be flattened.** Four types encoding distinct intent collapse into one verb.
6. **The audit trail is already unified.** `AgentDelegation` covers consult, transfer and background task in one place. A2A adds nothing here.
7. **It inverts the security posture.** Internal handover runs in-process under the caller's identity. A2A would require minting and validating tokens for internal calls — a larger auth surface, made worse by F1–F4.
8. **Steering and human-input pausing are mid-loop concerns.** `drain_steering_queue()` injects user corrections at iteration boundaries; nested consult and transfer runs receive no `steering_queue` and are unsteerable by construction. A2A's `INPUT_REQUIRED` is a task-level state, not a mid-loop injection point.

### 15.3 The one real argument on the other side

There is genuine duplication worth fixing: `_consult_agent` and `_transfer_to_agent` are implemented **twice** — once for chat ([agent_chat.py:2853, :3134](backend/app/services/agent_chat.py#L2853)) and again for autonomous runs ([assignment_engine.py:2570, :2759](backend/app/services/assignment_engine.py#L2570)). A single protocol would collapse them.

But the correct fix is a shared internal abstraction, not a network protocol. Adopting A2A to deduplicate two Python functions would be the most expensive possible refactor of a problem solvable by extracting a module.

### 15.4 Recommendation for the extension

**Adopt Tier 1. Take Tier 2 only as a by-product of Phase 1. Reject Tier 3.**

Suggested framing if this becomes an issue: *"external A2A agents as first-class handover targets"* — which is an extension of the client work, not a rewrite of internal routing.

**What would change this:** if XTM One ever executes agents in genuinely separate processes or clusters where in-process nesting is impossible, Tier 3's first objection weakens and the question deserves a fresh look. Note that `OrchestrationExecution` with leases already provides distributed durable execution without requiring a wire protocol between agents, so that day is not close.

---

## 16. Sources

All sources with publisher, date, version and access status are in `dev-docs/agent-interop-literature-review.md` §2, and sources that were inaccessible, paywalled or verifiable only through secondary reporting are in §2.1 of that document. Every claim in this benchmark that rests on an unverified source is marked in place.
