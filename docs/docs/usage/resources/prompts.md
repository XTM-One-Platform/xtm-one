# Prompts

A prompt is a reusable text instruction: it keeps the same wording across chat requests, agent personas and recurring work. This page gives you a quick tour of the prompt screens.

## The list page

In the left navigation, open `Agent Studio > Prompts`. The list page is a reusable library: search by name or a remembered wording fragment, filter by visibility or tags, and check whether a prompt already exists before creating another version. Each card shows a content preview so you can judge relevance and style before opening it. If you have permission, `Add Prompt` creates a repeatable instruction others can reuse.

## The detail page

Opening a prompt shows a header with its name, badges (`Company-managed`, `Shared`, `Read-only`), and a delete action when you can manage it. The page is organized into `Overview`, `Configuration`, `Versioning`, and `Activity` tabs.

### Overview

`Overview` combines usage metrics with a readable content preview. It helps you tell whether the prompt is a draft or already reused in real workflows, and — through the `Agents Using` card — how many downstream uses an edit would affect.

### Configuration

`Configuration` is where you read and edit the prompt. The distinctions that matter are durable:

- editing the content changes what the prompt says
- `Hide from chat` changes where it is surfaced (it drops the prompt from the chat picker while keeping it available for agent configuration)
- sharing settings change who can reuse it

The content editor includes a variable insert control, so you can add platform variables without memorizing the syntax.

### Versioning

Each time you save a change to the prompt's content, a new version is recorded. `Versioning` lists them, newest first, with the date, the author and the commit message, and marks the `Active` one.

- Select two versions, then `Compare`, to see what changed between them.
- The rollback button of a version makes its content the current one again. You can add a commit message; the content you replace stays in the list.
- Administrators can also lock a version so it cannot be deleted, and delete a version that is not active.

### Activity

`Activity` answers who changed the prompt, when, and whether a recent run problem started after an edit — especially useful for shared prompts.

## Read-only and shared states

Private prompts are editable by their owner, shared prompts are visible in a group context, and company-managed prompts are visible to everyone but read-only for non-admins. When a prompt is read-only, you can still inspect its content and usage.

## Good habits

- Read the content preview before creating a duplicate prompt.
- Check `Agents Using` before editing a shared prompt.
- Use `Hide from chat` when the prompt is operational and not meant for casual use.
- Keep names and descriptions clear enough that other teams can reuse the prompt without guessing.
