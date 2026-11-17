# Knowledge bases

A knowledge base stores documents and indexed content that agents search, so their answers are grounded in trusted material. This page gives you a quick tour of the knowledge base screens.

## The list page

In the left navigation, open `Agent Studio > Knowledge`. The list page is where you start: search by name or description, filter by visibility or tags, and compare existing collections before creating a new one so you avoid duplicate silos. Each card shows enough to judge a collection at a glance — what it is about, its visibility, and how much indexed content it holds. If you have permission, `New Knowledge Base` creates one from files, GitHub, Notion, or cloud storage.

## The detail page

Opening a knowledge base shows a header with the name, its badges (`System managed`, `Company-managed`, `Shared`, `Read-only`), and a few actions that serve different purposes:

- `Test Search` lets you judge answer quality before changing anything. It is disabled until the knowledge base has indexed content.
- `Re-embed` is a maintenance action that refreshes the searchable index.
- delete removes the whole knowledge base, not a single document.

The detail page is organized into tabs. `Overview`, `Configuration`, and `Activity` are always present; `Documents` and `Import Sources` appear for knowledge bases you populate yourself (`Import Sources` is hidden for system-managed ones).

### Overview

`Overview` is the fastest way to understand the current state: how much content is indexed, how often it is queried, the embedding model, and which agents are bound to it.

### Documents

`Documents` is the main content view — upload files, paste text, download, delete, and reprocess documents that are stuck or failed. Open it when your real question is whether the source content is actually present, whether ingestion failed, or whether an old file version is still indexed.

Each document keeps its original source text, not just the searchable chunks it was split into. That is what makes `download` return the file you put in, and it is also what agents read when they need a whole document rather than the fragments semantic search returns.

### Import Sources

`Import Sources` manages automatic sync (website, Notion, OneDrive/SharePoint, Google Drive, GitHub, and more). It shows each source's schedule and last-sync status, so it is useful both for setup and for answering "why isn't the knowledge base up to date?". Some source types require a connected integration first; when that is missing, the option stays visible but disabled and the UI explains what is needed.

A knowledge base can hold several sources of the same type, for example two Notion pages or two repositories. Each sync replaces and removes only the documents its own source imported, so the sources add up rather than replace each other, and `Force re-sync` purges only that source's documents and the documents of its type that belong to no source. A page or file two sources both reach is stored once. Documents synced before this rule existed belong to no source yet: the next sync of each source takes over the ones it finds, and may still remove the ones another source of the same type has not taken over yet, which that source imports again at its own next sync. After every source has synced once, they no longer touch each other's documents.

When you choose a specific integration for a source, you can only choose one you can see. The source then syncs with that integration every time, on its schedule or with `Sync now`, whoever starts it. If the person the source syncs for (whoever set it up, else the knowledge base's owner) later loses access to that integration - it is no longer shared with them, or `Restrict access` leaves them out - the source keeps syncing with it, and the security audit lists the source so that an administrator can share the integration with them or have the source use another one.

When a sync fails, the full error is shown only to the people who can manage the knowledge base: its owner, the people it is shared with who may configure it, and administrators (only administrators for a company-managed knowledge base). That text is what fixing the source takes, and it can name internal hosts, addresses or paths. Everyone else who can open the knowledge base sees a short, fixed description instead - for example `The sync timed out.` or `The source was disabled after repeated failed syncs.` - followed by a note that people who can manage the knowledge base can see the details. The full error is always kept, and the server log records it with the knowledge base and source ids.

### Configuration

`Configuration` covers the knowledge base's identity (name, description, tags), its visibility and sharing, and the `Read-only content`, `Index HTML as rendered text` and `Describe PDF figures` switches. System-managed knowledge bases stay read-only here.

Turn `Read-only content` on when a collection is **ground truth** — the authoritative version of something other work is checked against. A read-only knowledge base stays fully searchable, so agents keep using it for answers; what changes is that nothing can rewrite it. Agents lose write access to it even if an agent's configuration grants writing, its documents cannot be added, edited or deleted, and the knowledge base itself cannot be deleted until you turn the switch back off.

Two things deliberately keep working on a read-only knowledge base: configured `Import Sources` still refresh it, and `Re-embed` still works. That is so a collection whose authority lives in another system — a policy wiki, a specification repository — stays both locked against edits and up to date. To freeze such a knowledge base completely, disable its import sources as well.

`Index HTML as rendered text` (off by default) decides how HTML documents are searched. Off, an HTML document is indexed as its source markup, exactly as before the switch existed. On, it is indexed as the text a reader sees: the page text in reading order, with a summary of the data its scripts embed first, and styles, scripts and SVG drawing instructions left out. Turn it on for exported pages and reports, whose markup often outweighs their text so much that searches match tags instead of sentences. The stored document does not change either way (opening or downloading it still returns the original HTML), and only what is indexed differs. Changing the switch re-indexes nothing by itself: it applies to documents added or changed afterwards (by hand, by an agent or by an import source), and `Re-embed` applies it to the documents already there. It only affects documents that are HTML; data, text and code files (JSON, CSV, Markdown, plain text, scripts), and text an import source has already converted, such as Markdown from a crawl, are indexed as they are. A page with no visible text or data, a page too large to render whole, or one that cannot be rendered is indexed as its source; with the switch on, the `Embedding completed` entry `Activity` shows for an HTML document says which of the two it was indexed as.

`Describe PDF figures` (off by default) decides whether what a PDF shows as pictures is searchable. Off, a PDF is indexed from its text layer only: a diagram, a graph, a chart, a screenshot, or a page with no text layer at all (a scan, an export whose text was converted to outlines) is not. On, each PDF uploaded to the knowledge base (with the `Upload` button, through the API, or as a workspace source) is read page by page when it is indexed: the pages that carry an image, vector drawings or almost no text are rendered and described by the chat model the platform uses for the person who added the document, and each description is indexed right after its page's text. A graph is described as its nodes and its oriented relations, a screenshot by the text it shows, a chart by its values, and a page without text layer is transcribed whole. Text read from an image is treated as quoted content, never as instructions. The stored document does not change (opening or downloading it still returns the PDF's text layer).

Each described page costs one model call, and at most 30 pages are described per document (the pages without text layer first). The platform's chat model must accept images. The switch applies to PDFs uploaded while it is on: PDFs uploaded before, documents from an import source and pasted text are indexed as before. When the figures of a document could not all be described (the model is unavailable, cannot read images or timed out, the 30-page cap), the document is still searchable by its text and `Documents` marks it `Figures partly described` or `Figures not described`, with the reason on hover; the `Embedding completed` entry in `Activity` says the same. `Re-embed` retries the pages that failed, for up to 7 days after the upload; pages already described are never paid for again. Turning the switch off indexes the text layer alone from the next re-embed. An exported knowledge base carries the descriptions, so an imported copy indexes the same text.

### Activity

`Activity` is the audit view — when content changed, whether a sync ran recently, and who edited the configuration. Check it when answer quality shifts. `Document updated` entries also cover agent write-backs: an agent with write access to a knowledge base can revise a stored document, which re-indexes it. The document shows `pending` until re-indexing finishes, so a document an agent just changed is briefly not searchable — that is expected, not a failure.

Sync entries follow the same rule as `Import Sources`: a failed sync, a sync alert, a quarantined source and a configuration error show the full error to people who can manage the knowledge base, and a fixed sentence to everyone else. The activity search only looks through what you are shown, so it never finds text you cannot see.

When a sync source is quarantined (disabled after repeated failures, repeated "not found" errors or repeated hangs), the knowledge base owner gets a notification in the platform naming the knowledge base; on a company-managed knowledge base every administrator gets it too, each person once. The source stays disabled until it is re-enabled or force re-synced.

## Badges that set expectations

- `System managed` means the platform maintains the content automatically; `Documents` may be read-only and point you to `Import Sources` instead.
- `Content locked` (`Locked` on the list page) means the knowledge base is marked read-only: searchable as usual, but nothing writes to it. This is about the content.
- `Company-managed` plus `Read-only` means you can view it but not change it. This one is about permissions — who may administer the knowledge base — which is why a collection can carry both badges for different reasons.
- `Shared` means the knowledge base belongs to a shared group rather than to one person.

## Good habits

- Start on `Overview` before editing anything.
- Use `Test Search` before deciding a knowledge base is weak.
- Check `Documents` when results look incomplete, and `Import Sources` when freshness is the issue.
- Read the badges first — they explain why some actions are missing.
- Mark authoritative collections `Read-only` rather than relying on each agent's configuration being right — the lock lives on the knowledge base, so it holds no matter what an agent is granted later.
- Keep very large documents out of knowledge bases you expect agents to read whole. A document above roughly 200 000 characters is not loaded into an agent's context at all: the platform records it as a work item instead, and the agent is steered to search it for the relevant passages. Splitting it into meaningful documents usually works better than leaving one enormous file.
