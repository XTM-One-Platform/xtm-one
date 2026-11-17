# Intelligence Sharing

## Why Share?

Intelligence sharing creates a network effect - threats identified by one organisation protect the entire community. However, sharing must be governed, reciprocal, and risk-managed.

**Golden rule for an agent**: sharing is an outbound, often irreversible action. Prepare everything (content, TLP, distribution, recipients), show it, and publish, push or enable a feed only when the user or the assignment explicitly asks for it.

## Sharing Models

### 1. ISACs (Information Sharing and Analysis Centers)
Sector-specific sharing communities.

| ISAC | Sector | URL |
|------|--------|-----|
| FS-ISAC | Financial Services | fs-isac.com |
| H-ISAC | Healthcare | h-isac.org |
| IT-ISAC | Technology | it-isac.org |
| E-ISAC | Energy | eisac.com |
| A-ISAC | Aviation | a-isac.com |
| Auto-ISAC | Automotive | automotiveisac.com |

**Benefits**: Sector-relevant intelligence, trusted community, pre-vetted members, established TLP norms.

Many ISACs operate a MISP instance or a TAXII server for members; the mechanics below apply to them.

### 2. FIRST (Forum of Incident Response and Security Teams)
Global incident response community. Membership-based, includes national CERTs and enterprise security teams.

**Sharing via**: FIRST mailing lists, TF-CSIRT community, CTI SIG.

### 3. MISP (Malware Information Sharing Platform)
Open-source threat intelligence platform for sharing IOCs and threat data.

**How to share via MISP** (with the MISP integration):
1. Check the values first: `check_misp_warninglists` flags known-benign values (public resolvers, CDNs, top domains) that must not be shared as malicious. Check for an existing event on the same activity with `search_misp_events` / `search_misp_attributes` and add to it rather than duplicating.
2. Create an event with appropriate distribution: `create_misp_event` (title stating what the activity is, date, threat level, analysis state, distribution).
3. Add attributes (IOCs) with correct types: `add_misp_attribute`. Use `describe_misp_attribute_types` when unsure of the type/category pair. Set the IDS flag only on indicators fit for detection; context-only values (victim sector, a benign redirector) go in without it.
4. Tag with TLP, ATT&CK techniques, and galaxies: `add_misp_tag` (for example `tlp:amber`, ATT&CK galaxy tags); `list_misp_galaxies` / `list_misp_galaxy_clusters` to find the right cluster.
5. Set distribution: Your organisation only / This community / Connected communities / All communities / a Sharing group (`list_misp_organisations` helps confirm who is in the community).
6. Publish event: `publish_misp_event` - only on explicit request, after the user has seen the event content. Publishing pushes the event to every synchronised instance allowed by the distribution.

### 4. STIX/TAXII (Automated Sharing)
Standards for structured intelligence exchange.

- **STIX 2.1**: Format for representing intelligence. To build a bundle, load the **STIX Bundle Authoring** skill (or the **OpenCTI STIX Bundle Construction** skill for OpenCTI-specific conventions) and validate it with `stix_bundle_validator`.
- **TAXII 2.1**: Transport protocol for exchanging STIX bundles.

**TAXII concepts:**
- Collections: Server-hosted repositories of STIX objects
- Channels: Push-based distribution
- API roots: Discovery endpoints

**Sharing from OpenCTI** (with the OpenCTI integration): `manage_opencti_data_sharing` manages the three outbound sharing mechanisms of OpenCTI.

| `share_type` | What partners receive | Typical use |
|--------------|----------------------|-------------|
| `taxii_collection` | A TAXII 2.1 collection of STIX objects matching the filters | Partner TIPs and ISAC peers polling your intelligence |
| `live_stream` | A live STIX event stream of matching creations / updates / deletions | Near-real-time sync to a partner OpenCTI or SIEM |
| `feed` | A CSV feed of matching objects with chosen attributes | Simple consumers (firewalls, legacy tools) |

Workflow:
1. `manage_opencti_data_sharing` action `list` with each `share_type` to see what is already shared.
2. Design the filters so they enforce TLP: restrict by marking definition (for example only TLP:CLEAR and TLP:GREEN for a community collection), and by entity type, label or confidence as needed. Pass filters as a JSON string FilterGroup.
3. `create` with a name and description stating audience and TLP ceiling; type-specific options in `config` (for example `taxii_public`, `include_inferences` for a TAXII collection). Keep it non-public unless the user says it is meant to be public.
4. `toggle` (enabled true/false) starts or pauses a live stream, and publishes or unpublishes a feed or collection - an explicit-request action.
5. Make sure the objects carry the right TLP before they enter a filter: `add_opencti_marking`.

### 5. Bilateral/Multilateral Sharing
Direct sharing with trusted partners under agreed terms.

**Requirements:**
- Sharing agreement (legal framework)
- TLP adherence by both parties
- Secure communication channel
- Reciprocity expectation

For a one-off package to a partner, produce the file with `generate_file` (STIX bundle, CSV, MISP JSON) or follow the **IOC Export** skill, and let the user deliver it over the agreed channel.

## TLP and Sharing

| TLP | Sharing Scope | Automated Sharing? |
|-----|--------------|-------------------|
| RED | Named recipients only | No |
| AMBER+STRICT | Own organisation | No |
| AMBER | Organisation + need-to-know | Restricted TAXII (non-public collection, authenticated partners only) |
| GREEN | Community | ISAC portals, MISP community distribution |
| CLEAR | Unrestricted | Public TAXII feeds, blogs |

Never downgrade a marking to make something shareable: re-marking requires the originator's consent. The shared product takes the most restrictive TLP of what it contains. Load the **TLP Guide** skill if it is attached for the full rules.

## What to Share

### Do Share
- IOCs from confirmed incidents (with context)
- TTPs observed in your environment
- Detection rules that work
- Vulnerability intelligence relevant to sector
- Anonymised incident lessons learned

### Don't Share
- Source-identifying information without source consent
- Victim-identifying information without victim consent
- Raw internal telemetry or logs
- Information that reveals your security posture
- Speculation presented as intelligence

Before sharing, strip internal hostnames, usernames, internal IP ranges and ticket references from descriptions and comments.

## Sharing Quality

Shared intelligence should meet the same quality standards as internal products:
- Source assessment (Admiralty Scale: reliability A-F, credibility 1-6)
- Confidence levels (Very High 90-100, High 75-89, Moderate 50-74, Low 25-49, Very Low 0-24)
- MITRE ATT&CK mapping (verify IDs with `search_mitre_attack`)
- Proper TLP marking
- Context (not just raw IOCs - explain what they mean)

The number one complaint about shared intelligence: "Just IOCs with no context." Always share the WHY alongside the WHAT. In MISP, put the narrative in the event info and a comment or report object; in OpenCTI, share the Report container with its objects rather than loose indicators.

## Legal Considerations

- Comply with data protection regulations (GDPR, sector-specific rules)
- Ensure sharing agreements cover liability
- TLP is a trust agreement, not a legal framework - complement with contracts
- Be aware of export control regulations for certain technical intelligence
- Document all sharing decisions for audit trail (what, to whom, under which TLP and agreement, when, who approved): record them in the run output and, when the team keeps one, in agent memory (`store_memory`) or an OpenCTI Note
