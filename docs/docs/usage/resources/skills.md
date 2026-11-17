# Skills

A skill is a reusable instruction set, a packaged method that you attach to agents. This page gives you a quick tour of the skill screens, then explains how agents use skills and how packages are imported.

## The list page

In the left navigation, open `Agent Studio > Skills`. The list page is a reusable behavior library: search by name, description, or purpose, and filter by visibility or tags. Each card answers three questions without opening the detail page — is the skill currently active, is it centrally maintained or locally owned, and (for URL-based skills) whether it should be edited here or refreshed from its source. If you have permission, `Add Skill` creates one by URL, paste, or file upload, and `Import Package` imports one or more skills from a file (see [Importing skills from an archive](#importing-skills-from-an-archive)).

## The detail page

Opening a skill shows a header with its name, its source type (`URL-based Skill` or `Direct Content Skill`), its `Enabled` or `Disabled` state, and badges. `Export` downloads the skill. For URL-based skills the header also has `Refresh`, which fetches the content again from the source URL — the quickest way to answer "did the source change outside XTM One, and do I need the latest version here?". The page is organized into `Catalog`, `Instructions`, `Resources`, `Versioning`, and `Activity` tabs.

### Catalog

`Catalog` holds what describes the skill and who can reach it:

- `Name` and `Description`. The description is what agents read to decide when to load the skill (see [How a skill reaches the agent](#how-a-skill-reaches-the-agent)).
- `Compatibility` and `Tags`.
- `URL`, and for a skill kept in GitHub, `Private repository access` and `Tracked branch` (see [Skills from a private GitHub repository](#skills-from-a-private-github-repository)).
- `Enabled` controls whether the skill is currently available to agents — a skill can stay saved but stop affecting agents immediately, including mid-conversation.
- `Hide from chat` controls discoverability in chat, not whether the skill exists.
- Sharing settings control who can reuse it.
- `License` is the skill's licence and copyright notice (the `license` field of a SKILL.md frontmatter is read into it). The `License and attribution` section shows it with the full text of the licence the skill bundles as `LICENSE.md` at its root. It is metadata about the skill: the agent never reads it, and it travels with the skill when it is exported or installed from the AI Catalog.

### Instructions

`Instructions` shows the SKILL.md as rendered markdown, with its frontmatter kept as a raw block above it. Images in the text are not loaded: a skill can come from another user or a remote URL, and the page does not fetch what it points to.

`Edit` opens the markdown content, where the actual operating method lives. The editor includes a variable insert control for reusable values.

### Resources

`Resources` lists the custom tools a skill package created, next to the resource path each one came from, with a link to open each tool (see [What a package's files become](#what-a-packages-files-become)).

### Versioning

Each content change records a new version. `Versioning` lists them with their date, author and commit message, and marks the `Active` one. Select two versions, then `Compare`, to see what changed; the rollback button of a version makes its content current again, and the content you replace stays in the list. Administrators can also lock a version so it cannot be deleted, and delete a version that is not active.

### Activity

`Activity` is the audit view. During troubleshooting it helps you tell a skill change apart from an agent configuration change or a normal difference in model behavior.

## How a skill reaches the agent

An agent does not carry the full text of every skill attached to it. Its prompt
carries a **catalog**: one entry per skill with the name, the description, the
compatibility note, and the names of any bundled files (except its `LICENSE.md`). When a task matches an
entry, the agent calls `load_skill` and the full instructions arrive at that
point in the conversation. If the skill is a package, the agent can then read
individual bundled files with `read_skill_resource`.

A long skill written with Markdown headings can also be read **one section at a
time**: `load_skill` with `section` (the heading text, or its number such as
`16`) returns only that section and its subsections. A section read does not
count as a load, so it also brings a rule back after the conversation pruned the
first full load; a title that matches no section, or several, answers the list
of the skill's sections. Headings inside code blocks are not sections.

On a long skill the agent can look before it reads: `load_skill` with
`outline: true` lists every heading with its index and size, and nothing else.
`section` also accepts that index (`#12`) or a heading path (`Rules > Holds`),
as `get_skill` does. A section longer than one answer is read by window:
`max_chars` sets the size (at most the per-load cap), and a cut answer names the
`offset` the next call continues from. An outline or a windowed read does not
count as a load either.

### Editing a long skill from a conversation

An agent with the agent builder tools reads and changes a long skill by section,
never resending it whole:

- `get_skill` with `outline` lists the headings (index, level, offset, length);
  with `section` (a heading text as `load_skill` accepts it, a heading path such
  as `Rules > Holds`, or an index such as `#3`) it returns that section verbatim.
- `update_skill` takes a `mode`: `replace` (the whole body, the default),
  `replace_section` (one section, its heading line included, every other
  character kept), `insert_after` (a new section after the one named), `append`,
  or `edit` (a list of exact find/replace operations; nothing changes unless
  every one applies, and a text found more than once needs `replace_all`).
- A `replace` that would remove more than half of a skill of 2,000 characters or
  more is refused unless `confirm_shrink` is set. A content that is only a
  placeholder, or shorter than 50 characters, never replaces such a skill: the
  refusal names the current length and nothing changes.
- Every content change made this way writes a skill version, so a bad edit is
  rolled back from the skill's history like an edit made in the page.
- `update_skill` and `update_prompt` also change who owns and who reaches the
  skill or prompt: `owner` (an email or user id) transfers it, an
  administrator's action recorded in the audit log; `users` with
  `users_permission` (`use`, the default, or `manage`) shares it with people, and
  `shared_group_ids` with `shared_group_permissions` with groups, each list
  applied by `binding_mode` (`replace`, the default, `add` or `remove`; `none`
  clears it). The owner named in `users` is reported, never stored, and a
  company-managed skill is not shared with individual people. `get_skill` and
  `get_prompt` read back the owner, the groups and the people with their access.

This is why the description matters more than it looks: it is the only thing the
agent sees until it decides to load. A description that does not say *when* to
use the skill leaves the agent guessing.

Three consequences worth knowing:

- attaching ten skills costs about the same prompt space as attaching one — the
  cost scales with the number of skills, not the size of their content
- every load shows up in the run trace as a tool call, so a trace finally
  answers "which skill actually influenced this answer?"
- a skill that is disabled, unshared, or made private stops being loadable
  immediately, even in a conversation that is already running (detaching a
  skill from the agent takes effect on the next message or run, like every
  other change to an agent's configuration)

### Keeping one skill always loaded

On an agent's `Configuration` tab, each attached skill has a pin. Pinning it puts
the full instructions in the system prompt on every turn, exactly as before this
behaviour existed — the right choice for the one skill that defines how the agent
works, rather than one it should reach for occasionally. A pinned skill does not
appear in the catalog, because there is nothing left to load.

Pin sparingly. Pinning every skill re-creates the oversized prompt the catalog
exists to avoid.

## Skills from a private GitHub repository

A URL-based skill normally fetches its `SKILL.md` anonymously, which only works for a public file. When the file lives in a **private** repository, select a connected GitHub integration under `Private repository access`, and every fetch is authenticated with that integration's stored credential instead.

Both GitHub integration types work, because both authenticate the same API:

- a personal access token — a fine-grained token must list the repository and grant `Contents: Read`
- an OAuth App or GitHub App connection — the current token is read on every refresh, so a rotated token is picked up with no change to the skill

Nothing is copied onto the skill. It stores only a reference to the integration, so rotating the credential in `Integrations` is the single place a token is ever managed. You can attach any integration you can already see: your own, one shared with your groups, or a company-managed one.

`Tracked branch` decides which version is served:

- leave it empty to follow the repository's **default branch**, which means each refresh returns the latest commit merged there — the usual choice when skills are reviewed by pull request and merged
- set a branch name to follow that branch instead
- set a commit SHA to deliberately pin a fixed version that will not move

The URL you paste may point at the GitHub file page (`github.com/<owner>/<repo>/blob/<branch>/…`), at `raw.githubusercontent.com`, or at the contents API. All three are recognised. A branch name containing a `/` is ambiguous in those URLs, so set `Tracked branch` explicitly in that case.

The content is validated when you save: XTM One reads the file once with the credential you selected, and refuses to save with a specific message when the token is rejected, when the token cannot see the repository, or when the path does not exist on the tracked branch. You therefore find out immediately rather than discovering a stale skill later.

After that, refresh is on the normal schedule: URL-based skills are re-fetched when their cached content is more than a day old, and the check runs hourly. A newly merged change appears within one refresh cycle rather than instantly — use `Refresh` in the header when you need it right away.

### When the credential stops working

If the attached credential expires, is revoked, or loses access to the repository, the skill shows a `Credential rejected` badge on both the list page and the detail header.

That state is deliberately different from a broken URL:

- the skill stays **enabled** and agents keep using the last content that was successfully fetched
- refresh keeps retrying daily instead of quarantining the skill, because the fix is rotating a token, not repairing a URL
- as soon as the credential works again, the next refresh clears the badge on its own

To recover, re-authorize the integration in `Integrations`, or attach a different one to the skill. Operators also get distinct log entries for an authentication failure, a missing path, and a provider outage, so a token problem is never reported as a dead URL.

Setting `Private repository access` back to `Public repository (no credential)` detaches the credential. That is a configuration change only: it does not re-read the source, so it works even when the repository is private and unreadable without a token. The content already cached is kept, and the next scheduled refresh then behaves like any other unreachable URL.

## Importing skills from an archive

`Import Package` on the list page accepts a single `SKILL.md` / `.txt` file, or a `.zip` archive.

A ZIP may hold **several skills at once** — a folder exported from a repository, or a collection shared by another team. Nothing is written straight away: XTM One reads the archive first and opens a review screen listing the skills it found (with their description, resource and version counts), the files it ignored grouped by reason (a README with no frontmatter, an image, archive metadata such as `__MACOSX`), and a checkbox per skill. A single-skill package goes through the same review, showing one row.

### What counts as a skill in the archive

A skill is a markdown file with YAML frontmatter declaring `name` and `description`, and the directory holding it is that skill's root. Those roots are found wherever they are in the archive, so all of these work — mixed together in the same ZIP if need be:

- `<skill-name>/SKILL.md` with its `scripts/`, `references/`, `assets/`, `examples/` and `versionning/` folders — the [Agent Skills](https://agentskills.io) layout, repeated as many times as needed
- `<skill-name>/<anything>.md` — folders that name the file after the skill rather than using `SKILL.md`
- `<skill-name>.md` at the root of the archive — a flat folder of skill files
- any of the above under one or more wrapper folders, which is what zipping a `skills/` directory produces (`skills/<skill-name>/SKILL.md`), README included

The frontmatter `name` is always what the skill is called, even when the folder is named differently — a mismatch is a warning on the review screen, not an error. When a folder contains both `SKILL.md` and other markdown files, `SKILL.md` is the skill and the rest is ignored.

Files inside a skill's `scripts/`, `references/`, `assets/`, `examples/` and `versionning/` folders belong to that skill at any depth, and are never mistaken for skills of their own even when they carry frontmatter — a reference document stays a reference document, and a `versionning/` snapshot stays a version. Anything else inside a skill folder (an unmapped `templates/` directory, for instance) is listed as ignored rather than imported. When a single folder holds several skill files, resources cannot be attributed to one of them: they are listed as ignored and the skills are imported without them.

### Collisions and partial failures

A skill whose name you already have is flagged on the review screen, with a choice per skill: `Skip` leaves the existing skill untouched (the default), `Replace` overwrites it with the archive's version, `Import as new` creates a copy under a suffixed name.

Each skill is then imported on its own. A skill that fails — malformed frontmatter, a duplicate name inside the archive, a package requiring a tool this platform does not have — never rolls back the skills that already landed. The result screen lists every skill as created, replaced, skipped or failed, with the reason.

## What a package's files become

A skill package is more than its `SKILL.md`. The mapped folders are turned into real platform objects at import time.

### `scripts/` become Custom Tools

!!! note "Custom tools are off by default"
    In XTM One, custom tools are switched off by default. The tools a package creates are still created, but agents cannot run them until an administrator turns custom tools on (see [Tools](tools.md)).

Every executable script in `scripts/` is imported as an enabled Custom Tool, so the agents using the skill can actually run it. The tool is named `<skill-name>-<file-name>` (`pdf-extract-convert` for `scripts/convert.py`), described as `Imported from scripts/convert.py`, and tagged `skill-import` plus the skill name so you can find every tool a skill created. Language comes from the extension: `.py` is Python, `.js` / `.mjs` / `.cjs` are JavaScript. A file with any other extension is **not** turned into a tool — it is still stored with the skill and downloadable from it, and the import reports `Skipped non-executable script resources` so the omission is visible rather than silent.

There are two shapes:

- **One tool per file** — the default. Each script stands on its own.
- **One tool for the whole folder** — used when `scripts/` looks like a Python project, which XTM One detects as an `__init__.py` alongside more than one `.py` file. Modules that import each other only work if they are unpacked together, so the entire `scripts/` tree is embedded in a single tool: at run time it is extracted into a temporary directory, that directory is put on `sys.path` and becomes the working directory, and the entrypoint runs as `__main__`. Anything passed as the tool's `argv` input becomes `sys.argv` for the script.

The entrypoint of a Python project is chosen among the top-level scripts that contain `__main__`, preferring `main.py`, then `run.py`, then the first in alphabetical order; when several qualify, the import says which one it picked. The embedded workspace is capped at 10 MB compressed — a larger `scripts/` folder is refused with a message asking you to reduce or split the package. Imported tools execute in the same hardened sandbox as any other Custom Tool: importing a package does not create a privileged execution path.

### Who can run them, and what happens on delete

Materialized tools inherit the skill's visibility — a company-managed or group-shared skill produces company-managed or group-shared tools, because agents belonging to other users must be able to execute them. A private skill produces private tools. A tool name that is already taken is suffixed rather than overwriting anything.

Agents that already reference the skill are **wired up automatically**: the new tools (and reference knowledge bases) are added to their configuration, so an existing agent does not silently miss the scripts of a skill it already uses. Deleting the skill deletes the tools and knowledge bases its import created and removes them from every agent that had been linked; re-importing with `Replace` does the same cleanup first, so repeated imports never accumulate orphaned tools. The tools a skill created are listed on its `Resources` tab, next to the resource path each one came from.

### `references/` become Knowledge Base documents

Text files in `references/` (or `reference/`) are imported as documents in a knowledge base named `<skill-name>-references`, tagged the same way, and bound to the agents using the skill. Files whose type cannot be read as text are skipped and reported as `Skipped non-text reference resources`.

The knowledge base and its documents exist as soon as the import returns, but the documents are **queued** for vectorization like any uploaded document — they stay `pending` until the platform worker processes them, and only then are they searchable by agents. A knowledge base whose documents are all still pending means the worker has not picked them up yet, not that the import missed them. `assets/` and `examples/` are stored with the skill as-is, and are never turned into tools or documents.

### Controlling the mapping: `assets/xtm-resource-map.json`

Everything above is inference — convenient defaults for a package that ships nothing but files. A package can decide for itself instead by including `assets/xtm-resource-map.json`:

```json
{
  "schema_version": 1,
  "builtins": ["web_search"],
  "scripts": [
    {
      "path": "scripts/convert.py",
      "tool": {
        "name": "pdf-to-markdown",
        "description": "Convert a PDF to markdown",
        "language": "python",
        "workspace_mode": "project",
        "input_schema": {
          "type": "object",
          "properties": { "argv": { "type": "array", "items": { "type": "string" } } }
        }
      }
    }
  ],
  "references": [{ "path": "references/format.md" }]
}
```

Points that matter when writing one:

- listing `scripts` **replaces** inference — only the entries you list become tools, which is how a Python project exposes more than one entrypoint
- `workspace_mode` is `single_file` or `project`, and overrides the detection described above
- `input_schema` is the tool's input contract; without it the tool takes no declared input
- `builtins` requests platform built-in tools by ID. The platform refuses to import a package asking for a built-in it does not have, and the bulk review screen flags it before you approve rather than at write time
- a malformed manifest fails the import with the exact field at fault, instead of falling back to inference

Exporting a skill writes this manifest back into the package with the mapping that was actually resolved, so a skill exported from one platform and imported into another creates the same tools.

## Packaged skills carry their own resources

The skill card and picker show what a skill carries, for example `1 KB · 2 tools`. Those resources belong to the skill, not to the agent:

- attaching such a skill to an agent also attaches the knowledge bases and custom tools it carries, so instructions that say "consult `references/playbook.md`" actually reach the content
- detaching the skill removes them again
- in the agent's `Knowledge Bases` and `Custom Tools` sections they are marked `Via skill` and cannot be detached by hand — detach the skill instead

Two consequences are worth knowing. A company-managed agent only accepts company-managed knowledge bases, so a packaged knowledge base that is not company-managed is skipped rather than attached — publish the skill company-managed if its references must follow it there. And resources you attached yourself are never touched by this: only the ones a package created are added and removed automatically.

Deleting the skill, or re-importing its package with `replace`, deletes the resources that import created and unbinds them everywhere.

## Exporting a skill

A skill is exported as a package (a ZIP holding `SKILL.md`, its resources and its `versionning/` history) or as JSON. A package needs a `name` and a `description` in the frontmatter of `SKILL.md` and of every version snapshot, but a skill created by pasting or uploading plain instructions keeps them in its own `Name` and `Description` fields. The export writes the missing ones from those fields, so you do not have to repeat them in the content:

- a value the frontmatter already has is never changed, and the stored content is not modified;
- the exported `name` is the skill's name in package format: lowercase letters, digits and single hyphens, 64 characters at most (`Code Review` becomes `code-review`), and it names the package's root folder;
- when the skill's name has no usable character (for example only non-Latin letters), the export stays unavailable and the skill page says which field to fix.

## Read-only states

Company-managed skills can be read-only for non-admins, shared skills stay editable in a group context, and some built-in skills are non-editable. When a page is read-only, inspection still works.

## Empty states on the list page

If no skills exist yet, the page suggests creating the first skill by URL, paste, or file upload.

If filters remove all visible skills, the page shows `No skills match your filter`.

## Good habits

- Check whether a skill is enabled before blaming the agent for missing behavior.
- When an agent ignored a skill, read the run trace: no `load_skill` call means the description did not match the task, which is a description problem, not a content problem.
- Write descriptions that say when to use the skill, not just what it contains.
- Use `Refresh` on URL-based skills when the source was updated.
- Treat a `Credential rejected` badge as an integration task, not a skill task — fix the token in `Integrations`.
- Check which agents use a shared skill before changing it.
- Keep compatibility and description fields meaningful so reuse stays easy.
