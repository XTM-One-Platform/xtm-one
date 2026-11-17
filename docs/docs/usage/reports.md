# Reports

A report is a structured, printable document an agent writes for you: an executive brief, a threat assessment, a readout for management. It renders inside the conversation, opens on a page of its own for a full read, and exports to an A4 PDF that looks exactly like the screen.

A report is not a slide deck and not a file. For slides, ask for a presentation (see [Presentations](presentations.md)); for a Word, Excel or plain PDF file, ask for a file (see [Files](files.md)).

## Ask for a report

Ask an assistant that can write reports in your own words, for example:

> Write an executive brief on the ransomware activity we saw this quarter: the bottom line first, the three decisions I need from the board, the attack chain, and the sources.

The assistant writes the report and it appears in the conversation as a card. The assistants a person reaches by default can write reports out of the box: the `General Assistant`, and on XTM One the `CTEM Assistant`, the `XTM-One Assistant` and the assistant of each product (`OpenCTI Assistant`, `OpenAEV Assistant`, `OpenCRQ Assistant`, `FIMI Analyst`). Any other agent can write them once the `Report Generation` built-in tool is turned on in its `Configuration`.

An agent can also write a report during an assignment run. The report then belongs to the person the run acts for, like any other report.

!!! tip "Brief it like an analyst"
    Say who reads the report, what you want them to decide, and the as-of date or classification to show. The assistant puts the bottom line in a verdict box at the top, then picks a section type for each part: key figures, decisions to take, an attack or process chain, an assessment table with the confidence of each finding, scenarios, a risk table or a two-way risk matrix, a highlighted position, a phased action plan, a checklist, a form to fill in, collapsible annexes and the sources.

## What a report looks like

Every report has the same parts:

- A masthead with the title, an optional subtitle, small tags (as-of date, classification, audience) and, when there is one, the verdict: the bottom line with its grade.
- A table of contents built from the numbered sections.
- The sections, up to 40 per report, each drawn by the platform from the data the agent supplied. The agent never writes the layout or the styling, so every report reads the same way.
- An optional footer line.

A report has one of three looks: light, the default, made to be printed; dark, for on-screen reading; and Filigran, the brand look with cyan accents on a dark background. Ask for the one you want.

## The report card in the chat

The card shows the report's title and its number of sections, with the report below it in a scrollable frame. The toolbar of the card offers:

- `Read fullscreen`, to read the report across the whole screen.
- `Export report`, the export menu (see [Export to PDF](#export-to-pdf)).
- `Open in a new tab`, which opens the report on its own page.

## Change a report

Ask for the change in the same conversation: "move the scenarios before the risk table", "lower the verdict to moderate", "add the CISA advisory to the sources". The assistant rewrites the same report, and the card already in the conversation shows the new version. There is no second copy: the link to the report stays the same.

Only an agent working for the report's owner can change it. An agent working for someone else, an administrator included, cannot rewrite your report.

## Read a report on its own page

`Open in a new tab` opens the report on a page of its own, without the sidebar and the header of the platform, at full height. The browser tab carries the report's title, and the `Export` menu sits in the top-right corner.

This is also where a report sent to a messaging platform leads. Slack, Microsoft Teams, WhatsApp and the other channels cannot show a report, so the message carries a link to this page instead. The reader signs in to open it, and must be allowed to read it (see [Who can read a report](#who-can-read-a-report)).

## Export to PDF

The `Export` menu, on the chat card and on the report's own page, offers two exports:

| Export | What you get |
| --- | --- |
| `PDF (vector)` | The platform prints the report on its own server, on A4, with every annex open and the table of contents left out, and downloads the file. The result does not depend on your browser. |
| `Print to PDF (browser)` | The fallback that needs nothing from the server: the report opens in a new tab with every annex open and your browser's print dialog starts. Choose **Save as PDF** as the destination. |

If `PDF (vector)` fails, an `Export failed` message gives the reason; `Print to PDF (browser)` still works. Exports are limited per person per minute, decks and reports together; administrators find the details in [Presentation export](../deployment/configuration.md#presentation-export).

You can also ask the agent for the PDF ("export this report to PDF"). It then attaches the PDF to the conversation as a file you can download, and it is listed with your other files under [`Files`](files.md).

## Who can read a report

A report can hold anything the agent read while writing it, so it is as private as the conversation that produced it:

- You read your own reports.
- Administrators read every report.
- Nobody else can open it, even with the link: a report has no sharing of its own. A person who opens a report they may not read sees `This report is no longer available.`

When the owner's account is deleted, the report stays, readable by administrators only.

## Templates and branding

A report can wear a template of the `Report` kind, which replaces its colours and fonts with your brand's (see [Templates](resources/templates.md)). Ask for it by name ("use our Sales template"). The report stays linked to the template: editing the template restyles the report the next time it is opened or exported.

The template is applied with the reader's own access. A reader who cannot see the template, or a report whose template was deleted, gets the report in its built-in look, with nothing lost.

## Where reports are listed

There is no list of reports in the sidebar: a report lives in the conversation, or the assignment run, that produced it. Deleting that conversation does not delete the report, and its link keeps working.

Administrators find every report of the platform under `Objects`, filtered on `Reports`.

## Next step

Give your reports your brand with [Templates](resources/templates.md), or turn the same material into slides with [Presentations](presentations.md).
