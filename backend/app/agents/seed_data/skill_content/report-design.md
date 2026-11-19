# Report Design

How to turn "a report on ...", "a brief for the board" or "an executive readout of ..." into a document a reader can act on, with `generate_report`. The tool description is the contract (every block and its fields, validated on save); this skill is the craft: gather the data first, dress the report in its template, state the bottom line, give each piece of content the block made for it, follow the arc of the report asked for, cite every number, revise in place and review.

A report is read and printed; slides are presented (`generate_presentation`, the Presentation Design skill); a film is watched (the Video Studio, the Video Production skill). When the request names none, "report", "brief", "readout" and "assessment" mean this tool.

**The person chooses; you make them aware.** What the person set wins (the template, the reader, the length, the theme). A choice the brief left open (the template or the theme, one page or a full report, the classification, a PDF or not) is stated in your first answer with the default you take and the alternatives in one line, so one reply changes it. Never add a section, a recommendation or a file the person did not ask for without saying so.

## 1. Working method

1. **Read the brief.** Who reads it (a CISO, a board, a SOC lead, an auditor), what they must decide, the period and scope, the length (a one-page brief is 5-7 blocks, an executive report 8-14, never past 40).
2. **Gather the data before writing a word.** Use the tools you carry: OpenCTI for threats, actors, campaigns, indicators, vulnerabilities and reports; OpenAEV for simulations, scenarios, prevention and detection results and findings; OpenCRQ for frameworks, controls, assessments and risk scenarios; your knowledge bases; the web for public facts. Note each figure with where it came from and as of when. A report written from memory is an opinion, not a report.
3. **Pick the template.** Omit `template_id` to get the platform's brand: a report that names none wears the seeded brand report template of the platform (`search_templates(kind="report")` lists it with any other report template). Pass a `template_id` only for the template the user named or a company template that is not the brand. `theme` picks the scheme of that palette: `light` (the default, made for print and PDF) or `dark` for a screen-only readout.
4. **Outline in one short message** when the report is long or the brief leaves a real choice open: one line per block, its kind and what it says. Otherwise build at once.
5. **One complete `generate_report` call**, then put the returned `[[REPORT:...]]` marker in your reply exactly as given: the report renders where the marker stands.
6. **Revise in place.** A correction is `generate_report` again with `report_id` and the FULL new block list (`get_report` first when the blocks are no longer in front of you). Never create a second report to fix the first.
7. **A file only when asked.** `export_report` is offered when the request asks for a file (a PDF to send); its `[[FILE:...]]` marker goes in the reply too. Otherwise the reader exports from the report itself.

## 2. The masthead: what, as of when, for whom, and the verdict

- `header.title` says what the report is about; `eyebrow` is the kicker ("Cyber Threat Intelligence", "Exposure Validation", "Compliance"); `subtitle` gives the scope and the period.
- `tags` frame it, at most eight: the as-of date, the classification (TLP), the audience, the analytic confidence.
- `verdict` is the bottom line, readable on its own: a short `grade` shown large, a `label` above it, one or two sentences of `rich_text` saying why.
- `footer` repeats the classification and the date on every printed page.

```json
{"title": "Ransomware activity against European manufacturing", "eyebrow": "Cyber Threat Intelligence", "subtitle": "Third quarter 2026, sector and peer view", "tags": ["As of 30 Sep 2026", "TLP:AMBER", "Audience: executive committee", "Confidence: moderate"], "verdict": {"grade": "ELEVATED THREAT", "label": "Assessment", "rich_text": "<p>Two groups doubled their activity against the sector this quarter and one of them targets our ERP stack.</p>"}}
```

## 3. Which block carries what

- **Figures** - `kpi_row`, 1-6 items: `value` pre-formatted as it should read ("63%", "1,204", "> 21k"), `label` what it counts, `note` where it comes from and its caveat.
- **The narrative** - `prose`: a `lead` paragraph, the `rich_text` body, an `alert` for the one caveat the reader must not miss.
- **What the reader must decide** - `decisions`, 1-6 items, each a `title` and its `text`. The same block lays out analytic items (mechanisms, drivers), each with its own `confidence`.
- **How an attack or a process unfolds** - `chain`, 1-8 steps; `note` says what the chain does and does not claim.
- **Findings, their evidence and confidence** - `assessment_table`: `statement`, `confidence` (high, medium, low) with `confidence_label` for the analyst's own wording, `nature` (the kind of evidence: "official attribution", "vendor advisory", "simulation result") and `appraisal`.
- **Ways it could go** - `scenarios`, 1-6 items, each with its `impact`.
- **Exposure, one situation per row** - `risk_table`: `situation`, `level` (critical, elevated, moderate, controlled), `posture` (what to do about it).
- **A grade that depends on two things** - `matrix`: likelihood x impact, tactic x platform, framework domain x maturity. One cell per column label in every row; a 4x4 matrix flattened into a table loses 12 of its 16 judgements.
- **The one position that matters** - `callout`, with a `score` ("4/5") and its `score_label`.
- **The plan** - `timeline`, phases with a deadline `label` ("0-7 days"), a `headline` and their `items`.
- **What to tick** - `checklist` (`columns` 1 or 2); **what the reader fills in** - `fill_in_box` (a field without `value` prints a blank line).
- **Detail that would break the flow** - `annex`: a required `summary`, then `items`, `rich_text` or a `table`; every annex prints open.
- **Where it all comes from** - `sources`: each item a `label`, a `url` (http or https only), a `note` and a `ref` ("S01") the body cites, then `limits` on method and coverage.

Number the sections (`number`: "01", "02", "R" for recommendations, "A.1" for an annex) when the report has more than five: it builds the table of contents.

```json
{"block": "kpi_row", "title": "The quarter in figures", "number": "01", "items": [{"value": "142", "label": "simulations run", "note": "OpenAEV, 1 Jul - 30 Sep"}, {"value": "63%", "label": "techniques prevented", "note": "of 1,204 expectations"}, {"value": "18", "label": "detection gaps", "note": "no alert within one hour"}]}
```

```json
{"block": "assessment_table", "title": "Key judgements", "number": "02", "rows": [{"statement": "The group gains access through exposed VPN appliances before deploying ransomware.", "confidence": "high", "nature": "vendor advisory and incident data", "appraisal": "Consistent across three independent reports (S01, S02, S04)."}, {"statement": "It is preparing a campaign against ERP vendors in the region.", "confidence": "low", "confidence_label": "low to moderate", "nature": "single source", "appraisal": "Plausible given past targeting, but not corroborated."}]}
```

```json
{"block": "risk_table", "title": "Our exposure", "number": "03", "rows": [{"situation": "Two internet-facing VPN appliances run a firmware with a known-exploited vulnerability.", "level": "critical", "posture": "Patch within 72 hours or take them offline."}, {"situation": "EDR prevents the ransomware payload on every tested Windows server.", "level": "controlled", "posture": "Keep the policy; retest monthly."}]}
```

```json
{"block": "matrix", "title": "Detection by tactic and platform", "number": "04", "axis_label": "Tactic / platform", "column_labels": ["Windows", "Linux", "macOS"], "rows": [{"label": "Initial access", "cells": [{"label": "Detected", "level": "controlled"}, {"label": "Partial", "level": "moderate"}, {"label": "Missed", "level": "critical"}]}, {"label": "Exfiltration", "cells": [{"label": "Partial", "level": "moderate"}, {"label": "Missed", "level": "critical"}, {"label": "Missed", "level": "critical"}]}], "note": "Graded on the last run of each scenario; untested combinations are left out."}
```

```json
{"block": "timeline", "title": "Remediation plan", "number": "R", "phases": [{"label": "0-7 days", "headline": "Close what is exploited today", "items": ["Patch or isolate the two VPN appliances.", "Block the five command-and-control domains at the proxy."]}, {"label": "Within 30 days", "headline": "Close the detection gaps", "items": ["Add exfiltration detections on Linux and macOS.", "Rerun the ransomware scenario and compare."]}]}
```

```json
{"block": "sources", "title": "Sources", "items": [{"ref": "S01", "label": "MITRE ATT&CK - Phishing (T1566)", "url": "https://attack.mitre.org/techniques/T1566/", "note": "Technique reference"}, {"ref": "S02", "label": "OpenAEV - ransomware scenario, run of 28 Sep", "note": "Prevention and detection results"}], "limits": "<p>Activity counts cover reports ingested in OpenCTI only; peer incidents not publicly reported are not included.</p>"}
```

## 4. The arcs of the reports you are asked for

- **Threat brief** (OpenCTI). Verdict on the threat level; `kpi_row` of activity; `prose` on what happened; `chain` of how they operate; `assessment_table` of key judgements; `risk_table` of what it means for us; `decisions` or `timeline` of what to do; `sources`.
- **Exposure or validation readout** (OpenAEV). Verdict on prevention and detection; `kpi_row` of results; `matrix` of tactic x platform (or control x outcome); `risk_table` of the gaps; `timeline` of the remediation; an `annex` with the per-simulation table; `sources`.
- **Compliance gap report** (OpenCRQ). Verdict on coverage; `kpi_row` of controls assessed, compliant and in gap; `matrix` of domain x maturity or a `risk_table`; `assessment_table` of the findings; `timeline` of the remediation; a `fill_in_box` for the owner's sign-off; `sources`.
- **Post-incident review**. Verdict; `prose` summary; `chain` of the compromise; `assessment_table` of root causes; `decisions`; `timeline` of corrective actions; `checklist` of lessons; `sources`.
- **Weekly CTEM briefing**. Verdict on the week's posture; `kpi_row` across the connected products; one `callout` for the priority of the week; `risk_table` of the top exposures; `decisions`; `sources`. Lead with what the connected data shows, and mention a product that is not connected only at the end.
- **Vulnerability advisory**. Verdict; `kpi_row` (severity, exploitation status, affected assets); `prose`; `scenarios`; `timeline` of the patch plan; `sources`.

## 5. Numbers, confidence and sources

- **Every figure comes from a tool result** in this conversation, and its `note` or a source says where from and as of when. A figure you could not get is written "not available", never estimated without saying so. Never invent an identifier, an actor, a count or a date.
- **Separate the claim from its appraisal**: `statement` says what is judged, `appraisal` how solid it is, `nature` what kind of evidence backs it, `confidence` how sure you are.
- **Cite**: every source you used is an entry of `sources` with a `ref`, and the body names it ("(S03)"). A product entity the tools returned with a URL gets that URL.
- **Keep the markup simple**: `rich_text` takes paragraphs, emphasis, lists, links, code, h3/h4 and small tables; the layout and the colours are the renderer's.

## 6. Review before answering

- The verdict reads alone and matches the body; the first block after it is the most important one.
- Each piece of content sits in the block made for it: figures in a `kpi_row`, a two-variable grade in a `matrix`, a plan in a `timeline`.
- Every number has its source and date; every judgement its confidence; no source is cited that is not listed.
- The length fits the reader: an executive reads the masthead and the first three blocks.
- The classification is in the tags and the footer.
- Say in two or three sentences what the report concludes and what the reader should check, and put the `[[REPORT:...]]` marker in the reply.
