# Files

`Files` is the one place that lists every file you and your agents exchanged — the documents you uploaded in a conversation and the outputs your agents produced (reports, exports, generated images, spreadsheets), wherever they were made.

Until now each file lived only behind the context that produced it: the files panel of one conversation, the attachments of one run. `Files` collects them all so you can find a deliverable again without remembering which chat or which assignment produced it.

## Open Files

Choose `Work > Files` from the left sidebar. The `Work` group holds what your agents produce — `Operations` (their runs), `Presentations` (their decks) and `Files` (everything else they output, plus what you gave them).

The `Work` hub page itself shows the three at a glance, with counts and the most recently changed items of each.

## What is listed

You see exactly the files you are allowed to download:

- files you uploaded, and files generated for you, in your own conversations
- files of conversations that were shared with you
- outputs of assignment runs you can open — your own agents' runs, and runs of company-managed agents

Nothing is listed that you could not open: a file that appears in the list can always be downloaded.

Generated files include what an agent brought in from the web: every page or document it reads with `Web Fetch` is kept as a file of the conversation (a PDF, a spreadsheet or a data file as the document itself, a web page as its HTML source), so it can be downloaded, handed to another tool or added to a knowledge base. Reading the same page twice with identical content keeps one file. A report an agent exported to PDF is a file too.

!!! note "What is not here"
    The design assets of presentation templates (logos, diagrams, fonts) are managed from their template under `Agent Studio > Templates` and are not repeated here — a fresh install ships a few hundred of them, and they would bury your real files. Presentations themselves have their own page, [Presentations](presentations.md); a deck exported to PDF or PowerPoint does show up in `Files`, because the export is a file.

    Administrators do not get a wider view here. An administrator reaches another user's conversation files only through the audited break-glass access on the conversation itself, never through this list.

## Find a file

Three filters sit next to the search box. Each is a segmented control; pick one segment of each.

| Filter | Segments | Question it answers |
|---|---|---|
| Where the file lives | `Everywhere` · `Conversations` · `Runs` · `Other` | in a chat, from an assignment run, or unbound (dataset uploads, tool tests, flow uploads) |
| How it got there | `Any origin` · `Uploaded` · `Generated` | did a person upload it, or did an agent produce it |
| Visibility | `Mine` · `Company` · `Shared` · `All` | your own files and contexts; files of company-managed runs; files shared with you; or every file you can read. With no segment in the address, the page opens on the first one holding a file: `Mine`, else `Company`, else `Shared`, else `All` |

Search matches the file name. The counter on the right of the filter row is the total for the current filters, and the list is paginated when it is long. When `Mine` holds nothing but other segments do, the page says so and offers `Show all`.

The visibility segment is kept in the address as `?vis=`. An older link that carries `?owner=` still opens: `?owner=shared` ("Shared with me"), which also listed company-run files, opens on `All` so it hides none of them, and `?owner=mine` opens `Mine`. The address switches to `?vis=` on your next change.

`Group` (on by default) folds the list by conversation or run, so the files of one piece of work sit together under its title and owner. Switch it off for a flat, most-recent-first list where each row also shows its context.

Filters, search, grouping and page are kept in the URL, so a filtered view can be bookmarked or sent to a colleague — they will see the same view, limited to what they may read.

## Act on a file

Each row shows the file name, type and size, when it was added and by whom, and a badge telling whether it was `Uploaded` or `Generated`. From the row's menu:

- `Download` fetches the file.
- `Open conversation` / `Open run` takes you to the place the file came from, so you can read it in context or ask the assistant for a new version.
- `Delete` removes the file and its storage object. It is available to the file's owner, the owner of the conversation it lives in, or whoever can manage the run that produced it — someone a conversation was merely shared with can read the file but not delete it.

Deleting a file does not break the message or the deck that referenced it: they show the file as no longer available.

## Jump from a chat or a run

Anywhere the platform shows a file — a chat message, a run's attachments — a link to it in `Files` opens the page already scoped to that conversation or run, with the file highlighted. The `Showing files of` chip at the top of the list names the scope; close it to return to the full list with the same filters.

## Next step

Agents also produce slide decks. See [Presentations](presentations.md) for how decks are created, shared, and exported.
