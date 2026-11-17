# TLP Guide

Traffic Light Protocol (TLP) v2.0. Every intelligence output MUST have a TLP designation. Decide it before the product is shared, and state it at the top of the product.

## TLP Definitions

### TLP:RED: Named Recipients Only

**When to use**: information that would cause significant harm if shared beyond the named recipients. Sources may face risk.

- Share with: only the named recipients, in person or via secure channels
- Do NOT share with: anyone not explicitly named, even within the same organisation
- Examples: active zero-day exploitation details shared to a specific CISO; source-identifying intelligence; ongoing law enforcement operation details

### TLP:AMBER+STRICT: Organisation Only

**When to use**: information that requires support to be effectively acted upon, limited to the recipient's organisation only.

- Share with: members of the recipient's own organisation on a need-to-know basis
- Do NOT share with: external parties, clients, partners, or affiliated organisations
- Examples: targeted threat intelligence specific to one organisation's infrastructure; internal investigation findings

### TLP:AMBER: Organisation + Need-to-Know

**When to use**: information that requires support to be effectively acted upon, and may be shared with clients/partners on a need-to-know basis.

- Share with: members of the recipient's organisation AND their clients/partners who need the information to protect themselves
- Do NOT share with: the general public, social media, or parties without a clear need-to-know
- Examples: sector-specific threat briefings shared with ISAC members; IOCs for active campaigns affecting a supply chain

### TLP:GREEN: Community

**When to use**: information useful for awareness within a broader community but not for public disclosure.

- Share with: the wider community (sector peers, security community) but not via publicly accessible channels
- Do NOT share with: the general public, social media, public blogs, or news outlets
- Examples: general threat landscape updates shared at closed conferences; new TTP descriptions shared within an ISAC

### TLP:CLEAR: Unrestricted

**When to use**: information that carries minimal or no foreseeable risk of misuse. Public sharing is acceptable.

- Share with: anyone, through any channel
- Examples: published CVE details, public vendor advisories, general best practices, published threat reports

## Decision Tree

```
Is this information source-identifying or would sharing risk the source?
  -> YES -> TLP:RED

Would sharing beyond the recipient's org cause harm?
  -> YES -> Does it need to stay within a single org?
    -> YES -> TLP:AMBER+STRICT
    -> NO (partners/clients may need it) -> TLP:AMBER

Is this useful for community awareness but not public?
  -> YES -> TLP:GREEN

Is this safe for public consumption?
  -> YES -> TLP:CLEAR
```

A product inherits the most restrictive TLP of the information it contains: if one input is TLP:AMBER, the product cannot be lower than TLP:AMBER unless that input is removed or its originator agrees to a downgrade. When the input comes from OpenCTI or MISP, read its existing markings (on the OpenCTI entity from `get_opencti_entity`, on the MISP event or attribute from `get_misp_event`) before choosing.

## Default TLP by Product Type

| Product Type | Default TLP | Rationale |
|-------------|-------------|-----------|
| Flash report (active incident) | AMBER | Often contains org-specific context |
| Threat actor profile | GREEN | General awareness value |
| Campaign report | AMBER | May contain victim-identifying information |
| IOC list (enriched) | GREEN | Broadly useful for detection |
| Threat assessment | AMBER | Often tailored to specific stakeholder |
| Detection rules (SIGMA/YARA/KQL) | GREEN | Broadly useful for defence |
| STIX bundle | GREEN | Designed for automated sharing |
| Strategic landscape report | GREEN | General awareness |

These are defaults: always evaluate the specific content and adjust.

## Implementation

In a product header or metadata block:

```yaml
tlp: GREEN
```

In report headers:

```
TLP:GREEN - This information may be shared within the security community
but should not be published on publicly accessible channels.
```

In a STIX bundle, reference the standard TLP marking definition from each object's `object_marking_refs`. The **OpenCTI STIX Bundle Construction** skill covers this if it is attached.

### Marking objects in OpenCTI

When the user asks to mark an object in OpenCTI with a TLP:
1. Find the object's OpenCTI ID (`search_opencti` or `get_opencti_entity`) and check its current markings.
2. Find the OpenCTI ID of the TLP marking definition to apply (for example from an object that already carries it, or by searching marking definitions with `search_opencti`). OpenCTI ships the TLP v2.0 markings, including TLP:AMBER+STRICT and TLP:CLEAR.
3. Apply it with `add_opencti_marking` (entity ID + marking definition ID). Remove a wrong or outdated marking with `remove_opencti_marking`, for example when information has become public and the user asks for a downgrade.
4. When creating objects (`create_opencti_entity`, `create_opencti_observable`, `create_opencti_indicator`, `create_opencti_container`), pass the marking definition IDs at creation time instead of marking afterwards.

Markings restrict who can see an object in OpenCTI, so adding a more restrictive marking can hide it from users who could see it before. Say so to the user before applying TLP:RED or TLP:AMBER+STRICT. Never change markings unless the user or the assignment asks for it.

In MISP, apply the TLP taxonomy tag (for example `tlp:amber`, `tlp:amber+strict`, `tlp:clear`) with `add_misp_tag`, when asked.

Use only the tools you actually have; when OpenCTI or MISP is not connected, state the TLP in the product and say that no platform marking was applied.

## Common Mistakes

- Over-classifying (TLP:RED for everything): reduces trust and sharing
- Under-classifying (TLP:CLEAR for IOCs with victim context): risks harm
- Not including TLP at all
- Forgetting to downgrade TLP when information becomes public
- Sharing TLP:AMBER information on public Slack channels or social media; check the audience of a channel before an agent posts a product there
