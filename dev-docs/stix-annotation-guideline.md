# STIX annotation guideline (CTI reports to OpenCTI)

How a report's knowledge is recorded as a STIX 2.1 bundle that OpenCTI imports, for the grounded
extraction (`backend/app/services/stix_extraction/`) and for anyone annotating a gold set to evaluate
it. One rule per ambiguous case, its OpenCTI reason, an example. Where a gold set follows another
convention, the evaluation says so (`stix.*.aligned.closed` reads only the types a dataset annotates);
the extraction does not change to fit it.

## What is recorded

- **What the report establishes about a threat**: the actors, their malware and tools, the techniques
  they use, their victims (organizations, sectors, places), the vulnerabilities exploited, the
  infrastructure and the indicators. An object no fact of the report involves is left out, except an
  indicator value, which is knowledge alone.
- **Not recorded as objects**: the report's publisher (it is the report's `created_by_ref`), its
  authors, the pages it links to, products and companies named only in passing.
- **Every object is grounded**: a name the text writes (or an ATT&CK id or a CVE it writes out).
  Nothing the text does not support.

## Actors

| Case | Rule | OpenCTI reason |
| --- | --- | --- |
| Intrusion set or threat actor | A named group (APT41, Lazarus Group, BianLian) is an **intrusion-set**. A **threat-actor** is a named individual or persona. | OpenCTI merges intrusion sets by name and alias; a group typed as a threat actor becomes a second object. |
| Two names of one group | One object, the other names as `aliases` ("Secret Blizzard" and "Turla"). | OpenCTI matches on every alias. |
| A ransomware brand | "BianLian ransomware" is both the group (intrusion-set) and its malware (malware, `malware_types: ransomware`), the group `uses` the malware. | Both are tracked in OpenCTI, under one name each. |
| "the group", "the attackers", "this malware" | Never an object. A relationship the sentence states through such a mention is attached to the named object it refers to. | A generic name would create an object per report that merges with nothing. |

## Campaigns and incidents

- A **campaign** is a named operation ("Operation ForumTroll"), or one activity against several victims.
- An **incident** is one attack on one victim organization (a breach, an intrusion, a ransomware attack).
- An activity the report does not name is recorded only when the report is about that one activity,
  named in the report's own words ("the victim's name" + "data breach"; the actor's name + "campaign
  against" + its targets). A report about an actor in general, a vulnerability or a trend has none.
- The campaign is `attributed-to` its actor; its malware, techniques and victims hang on it (`uses`,
  `targets`).

## Locations

| Case | Rule |
| --- | --- |
| Where an actor, a campaign or a malware comes from | `originates-from` the location. "located-at" for an actor is read as its origin. |
| Where a victim, an infrastructure or a place is | `located-at` the location. |
| A victim country | The threat `targets` the country. A city or a region of a country targeted also makes the country targeted (victimology is read by country). |
| Names | OpenCTI's names: countries by ISO 3166-1 ("Russia" is "Russian Federation", the text's name and the ISO codes as aliases); regions by the STIX region vocabulary ("Southeast Asia" is "South-eastern Asia"). A place named only by a direction ("South" in "South and East Asia") is its region. |
| Location types | Every location says what it is, by its STIX property (`city`, a `country` code, a `region`) or by `x_opencti_location_type` (City, Country, Region): with neither, OpenCTI's import reads a place with a country code as a country, a city included. |

## Sectors and organizations

- A **sector** is a class of victims (`identity`, `identity_class: class`), named as OpenCTI's sector
  dataset names it ("Healthcare" is "Health", "Government" is "Government and administrations"), the
  report's own words kept as aliases.
- An **organization** is a named one (`identity_class: organization`): "French Ministry of Defence",
  not "the ministry".
- A sector is not a place and a place is not a sector: "U.S. state government" is an organization or
  a sector, never a location.

## Techniques

- A technique is an **ATT&CK technique** (an `attack-pattern` with its `x_mitre_id`), from an id the
  report writes or from the behaviour it describes, checked against the technique's ATT&CK definition.
- **Sub-technique or parent**: the sub-technique when the text says that much ("spearphishing
  attachment" is T1566.001), the parent otherwise ("phishing" is T1566). Never both for one behaviour.
- **Names**: as ATT&CK shows them, a sub-technique as "Parent: Sub" ("Phishing: Spearphishing
  Attachment"), its own name as an alias. The id, not the name, identifies it.
- A technique words name that ATT&CK does not have (a tactic, a vulnerability class) is not a
  technique.

## Indicators and observables

- Every IOC value the report writes (a hash, an address, a domain, a URL, a file name, a registry
  key) is an **observable** of its STIX type, refanged ("evil[.]com" is "evil.com").
- An **indicator** carries the observable's STIX pattern and is `based-on` it. It `indicates` a threat
  when the report says so, or when the page's layout does: a table row naming one threat beside the
  value, a list under a heading naming one threat. A name inside a value of the list (a file path
  naming a malware) is not a heading.
- An indicator indicates what it is a sign of: the hashes of a tool's own file indicate the tool; the
  addresses of the next table, or a language a stage is written in, do not.
- A product a vulnerability is in is a `software` observable, its vendor and version in their own
  properties, not in its name.

## Relationships

- Only the relationships OpenCTI accepts between the two types (its relationship schema); a verb the
  report uses that STIX names otherwise is read as STIX's ("located at" for an actor's country is
  `originates-from`, "targeting" a CVE is `exploits`).
- **Stated or implied**: a relationship one passage states (confidence 85), or one a careful analyst
  records from what the report says without one sentence saying it (confidence 50, "the group's
  malware runs the command" makes the group use the technique). Both are recorded; the confidence
  lets an import keep only the stated ones.
- Every relationship carries the passage it was read from.

## For a gold set

- One bundle per report, in OpenCTI's STIX 2.1 (the import's standard ids are not expected to match).
- Record what the report establishes, as above, not every mention; a gold that annotates every
  mention, or other types, is scored in its own types (`*.aligned.closed`) beside the plain reading.
- Mark what a second annotator or an adjudicator added (`x_xtm_adjudicated`), so a result can be read
  on the analysts' own objects alone.
