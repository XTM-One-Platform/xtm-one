# SGDSN/VIGINUM - capitalising FIMI knowledge in OpenCTI (operational summary)

Operational summary, in English, of two VIGINUM methodology publications:

- *Guide d'utilisation d'OpenCTI pour la lutte contre les manipulations de l'information d'origine étrangère - Partie 1 : Capitaliser la connaissance*, version 2.0, April 2025 (<https://github.com/VIGINUM-FR/Doctrine-OpenCTI>). The full French text, with every diagram described, is the document `viginum-doctrine-opencti-fr-annotee.md` of this knowledge base.
- *Définitions et objectifs du concept de « mode opératoire informationnel » (MOI)*, January 2026 (<https://www.sgdsn.gouv.fr/viginum/publications/definitions-et-objectifs-du-concept-de-mode-operatoire-informationnel-moi>), annotated in `viginum-moi-definitions-fr-annotee.md`.

Source : site internet du secrétariat général de la défense et de la sécurité nationale (SGDSN), <https://www.sgdsn.gouv.fr/viginum/publications>.

VIGINUM's own caveat applies: the doctrine's examples are fictitious or adjusted to illustrate the method; they are not VIGINUM's official capitalisation.

---

## 1. Why capitalise, and the vocabulary

Capitalising means identifying, modelling and keeping knowledge in a structured, durable and shareable form. In OpenCTI it standardises modelling between analysts, keeps critical knowledge, makes contextual and technical search possible and eases sharing with other counter-FIMI actors. Sources can be internal (analysis reports) or external (vendor reports, press articles, official statements).

**Imputation vs attribution.** VIGINUM separates *imputation* (technically linking observables to an information manipulation set) from *attribution* (a political choice). In OpenCTI both are the STIX relationship `attributed-to`.

**Information Manipulation Set (IMS, French MOI).** A set of behaviours, tools, tactics, techniques and procedures (TTPs) and adversary resources used by a malicious actor or group of actors in one or more digital information operations. It is the information-space counterpart of an intrusion set: in OpenCTI it is modelled as an **Intrusion Set**. An IMS is not an actor: one actor can run several IMS.

A set qualifies as an IMS only if it meets both conditions:

1. **Clandestinity**: the operators make visible efforts to prevent imputation (linking their digital assets together) and attribution (to an actor). Official channels that openly display their affiliation (a ministry's accounts, a state-backed foundation's content) are not an IMS; nor is an unattributed outlet whose assets are openly linked under one banner.
2. **Coordination**: the digital assets are used in a coordinated way and presumed operated by the same actor or group of actors working for the same sponsor, with strong technical or behavioural links. Accounts merely posting the same content, or sites merely sharing an IP address, are not enough. Two coherent sets that cannot be technically linked stay two IMS (VIGINUM keeps Storm-1679 and Matriochka separate) until evidence supports merging them.

**The five levels**, from the military doctrine, applied to information manipulation:

| Level | What it holds | Example (Doppelgänger, publicly imputed) |
|---|---|---|
| Political | sponsors who decide to use digital information activities | Russian Presidential Administration |
| Strategic | the malicious actor (operators tasked, recruited or funded) and its IMS | Social Design Agency (actor), Doppelgänger (IMS) |
| Operational | information campaigns pursuing the strategic objectives (promote, denigrate, polarise, incite physical action) | campaigns accusing Ukraine and "the West" of war crimes |
| Tactical | information operations implementing a campaign | fake sites, fake maps and victim lists |
| Technical | digital assets controlled by the operators | tribunalukraine.info, truemaps.info |

Chain to keep in mind: sponsor → actor → IMS → campaign → operation → digital assets.

## 2. General principles

- OpenCTI builds knowledge graphs in STIX 2.1: entities and observables linked by **named** and **directional** relationships. The direction matters: an intrusion set is attributed to an actor, never the reverse.
- **Inferences** derive new relationships from the graph (for example: if A is attributed to B and B to C, A is attributed to C; if an intrusion set uses a narrative and is attributed to an actor, the actor uses the narrative by inference). They are what let an analyst find every set that targeted the same organisation, sector or person, every set using a tool, narrative or TTP, every set linked to an actor, and every technical element linked to a campaign, set or actor.
- Always prefer a **qualified** relationship (`attributed-to`, `cooperates-with`, `located-at`, `targets`, `uses`...) to the generic `related-to`, which produces no inference. `related-to` is the expected link only for observables.
- Capitalise what the source document states, without judging its truthfulness: the graph records who said what, when, and with which marking.

## 3. Capitalising a report

Knowledge is capitalised through the **Report** object, manually or automatically.

**Overview tab, mandatory fields**

1. Name: the exact title of the capitalised document.
2. Description: a textual summary of its content.
3. Report type: internal production or external report.
4. Date: the document's publication date.
5. External reference: link to the original document (open source or internal database).
6. Marking: the document's marking, for example TLP:CLEAR or TLP:RED.
7. Processing status, kept up to date (see §6).
8. Author: the author or authors of the document.

Optional when relevant: confidence level, labels, assignees.

**Knowledge tab.** The graph is built here, and it is what produces inferences. Capitalise imputations and official attribution statements, victimology, the means and methods of attack, and the technical elements of the campaign.

## 4. The campaign-centred graph

The **Campaign** is the central entity. Every entity of a report (IP address, narrative, targeted country, etc.) is linked to an **annual campaign**, which keeps a time marker on an actor's or set's activity and shows how its techniques evolve. Except for observables, relationships are drawn **in a star from the campaign**.

### Canonical relationships

Relationship names are the OpenCTI ones; the target of each row is checked against the OpenCTI relationship mapping.

| Source | Relationship | Target |
|--------|-------------|--------|
| Campaign | attributed-to | Intrusion Set |
| Campaign | attributed-to | Threat Actor |
| Intrusion Set | attributed-to | Threat Actor |
| Threat Actor | cooperates-with | Threat Actor |
| Threat Actor | part-of | Threat Actor |
| Threat Actor | located-at | Country |
| Campaign | uses | Attack Pattern |
| Campaign | uses | Tool |
| Campaign | uses | Narrative |
| Campaign | uses | Channel |
| Channel | uses | Narrative |
| Channel | amplifies | Channel |
| Channel | publishes | Media Content |
| Channel | publishes | Url |
| Channel | publishes | Domain Name |
| Campaign | targets | Sector |
| Campaign | targets | Country |
| Campaign | targets | Organization |
| Campaign | targets | Individual |
| Campaign | targets | Event |
| Organization | part-of | Sector |
| Organization | located-at | Country |
| Event | located-at | Country |
| IPv4 Address | related-to | Campaign |
| Email Address | related-to | Campaign |
| Phone Number | related-to | Campaign |
| Domain Name | related-to | Campaign |
| Url | related-to | Campaign |
| Autonomous System | related-to | Campaign |
| Media Content | related-to | Campaign |
| Domain Name | resolves-to | IPv4 Address |
| Url | related-to | Domain Name |

"Threat Actor" covers both Threat Actor (Group) and Threat Actor (Individual).

### Attribution and imputation

A campaign is attributed, depending on the data available, to an intrusion set (IMS) or directly to a threat actor. An intrusion set is attributed to one or more threat actors, which can be part of other actors or cooperate with them; this rebuilds organisation charts (an IMS, a known individual, a unit of a foreign intelligence service).

1. **Always show the origin of the threat.** For presumed state activity, the graph always contains the threat actor standing for the origin (for example a meta-actor "Russian actors"), so the document can be found from that actor's page.
2. **Link the intrusion set to that actor only when the document justifies it:**
   - the document gives technical elements linking the set to the actor → create `Intrusion Set attributed-to Threat Actor`;
   - the document reports a public attribution statement, substantiated or not → create the relationship, to keep the trace of who attributed what, when, with which marking;
   - the document only says in passing that the set is "likely" run by a government, without substantiation → show the actor in the graph but create **no** attribution relationship.

### Means and methods

The campaign `uses` its attack patterns (DISARM techniques, named with their identifier, for example `[T0013] Create Inauthentic Websites`), its channels, its tools and its narratives. Means can also be linked together, for example a channel `uses` a narrative.

### Technical elements (observables)

Email addresses, IPv4 addresses, domain names, phone numbers, URLs, autonomous systems and media content are linked to the campaign with `related-to`. Observables can be linked together, for example a domain name `resolves-to` an IPv4 address.

### Social-media activity

A campaign `uses` one or more channels; channels `amplify` other channels; a channel `publishes` media content, URLs or domain names.

### Victimology

The campaign `targets` sectors, countries, events, individuals and organisations. Victimology entities are linked together with `located-at` (an event or an organisation located in a country) and `part-of` (an organisation part of a sector).

## 5. Choosing the entity: malicious or legitimate

The entity type follows the role in the operation, not the nature of the object:

| Kind | Malicious | Legitimate |
|---|---|---|
| Legal person | Threat Actor (Group) | Organization |
| Natural person | Threat Actor (Individual) | Individual |
| Online presence | Channel | Domain Name |

A site or account operated by the attacker is a **Channel**; a legitimate site (an impersonated media outlet, a quoted site) is a **Domain Name** observable. An organisation or person targeted or impersonated is an Organization or Individual, never a Threat Actor.

## 6. Conventions

**Campaign names (annual campaigns).** `Campagnes de <set or actor> en <year>`, for example "Campagnes de RRN en 2023", "Campagnes de Storm-1516 en 2019". An activity not yet attributed gets a non-attributed campaign naming its nature and detection date, for example "Campagne non-attribuée de deepfakes de célébrités sur Bluesky en décembre 2023". Once attributed, it is merged into the annual campaign of the actor or set.

**Channel names.** Set the channel type, and name the channel after the account or domain: Facebook `facebook.com/username` (user, group or page ID as alias), X `twitter.com/username` (handle as alias), Telegram `t.me/channelorgroupname` (group name as alias), a domain `mechantemanoeuvre.ru`. When the accounts are unknown but the platform use matters, a generic channel (`bluesky.com`, `x.com`).

**Media content names.** `[Vidéo] title`, `[Article] title`, `[Podcast] title`, `[Tweet] title`, `[Facebook] post title`, `[Contenu sponsorisé] sponsored post title`; a URL is mandatory.

**Events and narratives are broad.** An event must be wide enough to be targeted by several operations ("Jeux olympiques de Paris 2024", not one race). A narrative must be wide enough to be used by several actors or over a long period ("Néocolonialisme", not one specific accusation).

**Sectors are high level**: state institutions; economic, industrial and scientific interests; cultural heritage; state personalities; foreign policy; national security and defence; plus activity sectors such as media or NGOs.

**Labels** carry cross-cutting themes (elections, artificial intelligence, Olympic Games, the operation's language).

**Processing status of a report.** `NEW` (default, not yet capitalised), `IN_PROGRESS` (being capitalised, not complete), `ANALYZED` (fully capitalised, set by the analyst when done), `CLOSED` (errors and duplicates, to be deleted).

**Supporting relationships.** Most relationships must be justified by the document: fill the start and/or end of observation, the document's author and its marking.

**Dating relationships.** Always edit the dates when creating a relationship; otherwise they default to the document's publication date.

- Victimology, tools and methods, four cases: start and end observed → both dates; start observed, activity ongoing → start only; end observed of an activity running since an unknown date → end only; activity observed with no precise date → no date.
- Attribution: the start of observation is the publication date of the document stating the attribution, and an attribution never has an end date (delete the default one).

## 7. Main entities

| Entity | Origin | Use |
|---|---|---|
| Campaign | native STIX | central entity; annual campaign attributed to an intrusion set or actor, or non-attributed |
| Threat Actor (Group) | native STIX | organisation at the origin of, supervising, funding, sponsoring or supporting the activity; also meta-actors such as "Russian actors" |
| Threat Actor (Individual) | native STIX | natural person in that role (intelligence officer, employee of a company running operations, propagandist) |
| Intrusion Set | native STIX | the IMS, used when the precise actor is unknown or when the IMS concept is used to track operations |
| Channel | Filigran addition | social-media account or domain used by an actor or IMS |
| Event | Filigran addition | broad physical event exploited by a campaign (sport event, summit, election, diplomatic visit, natural disaster) |
| Individual | native STIX | natural person who is not a threat actor (target, impersonated person) |
| Organization | native STIX | legal person that is not a threat actor (targeted company, NGO, impersonated media) |
| Sector | native STIX | high-level interests targeted, and activity sectors |
| Infrastructure | native STIX | resources exploited by the attackers, such as the hosting providers of their sites |
| Narrative | Filigran addition | broad narrative used by an IMS or actor during a campaign |
| Attack Pattern | native STIX | attacker action from the DISARM framework (for example trolls amplification T0049.001, create inauthentic accounts T0097, facilitate state propaganda T0002) |
| Tool | native STIX | legitimate software exploited by an IMS or actor (redirection service, bot management) |

Most relevant observables: email address, IPv4 address, media content, domain name (legitimate sites only), phone number, autonomous system, URL.

## 8. Related reference

The DISARM framework, translated into French by VIGINUM, is in `viginum-disarm-fr-referentiel-bilingue.md` (French and English names and descriptions of every tactic, technique and sub-technique). The case studies VIGINUM published (RRN, Portal Kombat, Matriochka, UN-notorious BIG, Storm-1516, African Initiative, Rokh Solis, GlobUs...) are in the "VIGINUM FIMI Publications" knowledge base.
