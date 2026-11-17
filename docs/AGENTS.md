# Documentation — Agent Instructions

## What belongs here

This directory contains **product documentation** for XTM One, written for end users and customers.

Update files here when a new feature ships or an existing feature changes in a user-visible way.

## What does NOT belong here

Do not create the following in this directory:

- Plans, assessments, or implementation notes
- Architecture documents or technical deep-dives
- Product requirements documents (PRDs)
- Feature drafts or internal design notes
- Developer or agent-facing references

That material belongs in `dev-docs/` at the root of the repository.

## How to update product documentation

### File placement

Each topic has its own markdown file under `docs/docs/`. The nav structure is declared in `docs/mkdocs.yml`. Add or update the nav entry when you create a new file.

The folder structure mirrors the OpenCTI and OpenAEV documentation (audience-based top tabs):

- `docs/docs/deployment/` — Deployment & Setup tab (overview, installation, configuration, authentication, upgrade)
- `docs/docs/usage/` — User Guide tab (getting started and feature pages)
- `docs/docs/usage/foundations/` — core concepts the user needs before navigating the UI
- `docs/docs/usage/resources/` — one page per resource type (integrations, knowledge bases, MCP servers, prompts, skills, tools, variables)
- `docs/docs/usage/xtm-suite/` — how XTM One appears inside other XTM Suite products
- `docs/docs/administration/` — Administration tab (introduction, settings)
- `docs/docs/development.md` — Development tab

Do not add new top-level nav tabs. The tab set (Home, Deployment & Setup, User Guide, Administration, Development) is aligned with the other Filigran products — new pages go inside an existing tab.

File names: kebab-case, no UUID suffixes, no "in XTM One" suffix (e.g. `agents.md`, `knowledge-bases.md`).

### Tone and voice

Read the existing pages before writing. The documentation follows these conventions:

- Address the reader as "you"
- Use present tense throughout
- Write in plain language — avoid jargon, API names, or internal identifiers
- Use bold backtick labels for UI element names (e.g. `Agents`, `Overview`, `Save`)
- Describe what the user sees and what they can do, not how the system works internally
- Organize each page with short H2 sections; keep each section focused on one task or concept
- End most pages with a "Next step" section pointing to the logical continuation

### Formatting rules

- Never add an in-page table of contents ("Contents" lists of anchor links) — the theme renders the page TOC on the right automatically
- Never wrap headings in bold markers (`## **Title**` is forbidden; write `## Title`)
- Never use `---` horizontal rules as section separators — headings provide the structure
- H1 titles are short and match the nav label (e.g. `# Agents`, not `# Agents in XTM One`)
- Use admonitions (`!!! note`, `!!! warning`, `!!! tip`) for callouts instead of bold paragraphs

### What to add for a new feature

1. If the feature is a new top-level section: create a new file, add it to the nav in `mkdocs.yml`
2. If the feature adds to an existing page: find the relevant file and add a new H2 section
3. If the feature changes how an existing page works: update the affected paragraphs in place

Do not duplicate content. If two features share a concept, link to the canonical page rather than restating it.
