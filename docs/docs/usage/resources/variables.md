# Variables

Variables store reusable values that resolve at runtime in personas, prompts, skills, and tools, so you do not repeat key values by hand. This page explains what the `Variables` page shows and how it works.

!!! note "Variables are off by default"
    In XTM One, custom variables are switched off by default, and `Variables` does not appear in the left navigation. An administrator turns them on by switching off `Disable custom variables` in `Settings > Security`, or with its environment variable (see [Settings > Security options](../../deployment/configuration.md#settings-security-options)).

Unlike most other resources, variables do not use a list page plus detail page pattern. They use one management page with create and edit dialogs.

## The management page

In the left navigation, open `Tools & Integrations > Variables`. The page is a board of reusable values that resolve at runtime in personas, prompts, skills, and tools. You can search by name, description, or value pattern and filter by tags, then create, edit, copy, or delete a variable. If you have permission, `New Variable` adds one.

Each variable is a card, not a table row. The card shows enough to decide quickly whether it is the right variable, whether it is sensitive, and whether it still looks current. Non-secret variables show their value preview directly; secret variables mask it behind a lock icon and a `Secret` badge, with a reveal toggle on the card itself. Each card also has a copy action for the variable reference.

## Editing behavior

Clicking a card opens an edit dialog — there is no separate detail page or tabs. The create and edit dialogs share the same fields; the key decision is whether to mark the variable `Secret`, which changes how it is displayed and handled later.

For a secret variable, the value field does not reveal the stored value on edit. The placeholder tells you to enter a new value only when you want to update it, which reinforces that secret handling differs from plain text.

A secret variable can also read its value from a vault an administrator granted you: **From a vault** opens the secret picker, and the field then shows the vault, the path and the key instead of a value. The value is read from the vault each time a run needs it. See [Secret vaults](../../deployment/secret-vaults.md#pick-a-secret).

## Inserting a variable

The **Variable** button next to a persona, an agent's instructions, an assignment prompt, a prompt of the library, a skill, a flow node's text or a custom tool lists the variables you may use, secret ones badged `Secret`, and inserts the reference at the cursor. **Create a secret variable from a vault secret**, at the bottom of that list, picks a secret from a vault, creates a secret variable holding its reference (only you see it until you share it) and inserts it: a prompt never holds a vault reference itself.

## Copying a reference

The UI lets you copy the reference format directly instead of typing it. The copied format uses the variable ID (for example `{{var_id.<id>}}`) and can be reused in personas, prompts, skills, and tools.

## A secret variable in an integration credential

An integration's API key or client secret may hold a reference to a **secret** variable, alone or inside a longer value: `users API-Key {{var_id.<id>}}`. The reference is stored as written and replaced by the variable's value each time the integration calls its service, so the key itself never has to be pasted into the integration.

- Only a secret variable can be referenced there. Saving the integration is refused when the variable does not exist, is not secret, or is not visible to you and to the integration's owner.
- The reference resolves for the integration's owner, with the same visibility rules as below (company-managed variables only for an integration with no owner).
- If the variable is later deleted, made non-secret, emptied or no longer shared with the owner, the integration's calls fail with "secret unavailable" until the reference is fixed. The value stays out of traces and logs, as for any secret variable.
- A key without a reference behaves exactly as before.

## Who a reference resolves for

A reference is replaced by its value only for the person the work runs as, and only if that person can see the variable: their own variables, the ones shared with them or one of their groups, and company-managed ones (unless restricted to an audience they are not in). In a chat that is the person chatting. An assignment runs as the person who triggered it, else its run-as user, else its owner. A flow runs as its owner, or as the person who ran it. Administrators get no exception.

When an agent's automations run as someone else, share the variable with that person or their group, or make it company-managed. Otherwise the reference is sent to the model as written. The agent's page lists the variables it references that have no value yet, and the references to variables that no longer exist.

## Where a secret value goes

The value of a secret variable, stored in XTM One or read from a vault, reaches the model and the tools it calls. It is never kept anywhere else in clear:

- What a run or a chat stores (its messages, the tool calls and their results, an approval waiting for a reviewer) holds a marker instead of the value. When the work resumes (an approved tool call, an answered question, the next message of a conversation, the next step of a flow), the model and the tools get the value back.
- Traces and server logs hold the same marker, error messages included (a failing tool's output, for instance).
- What an agent remembers of a run or a chat, and what it remembers about you, holds the marker too. A later run or conversation reads that memory with the marker, never the value.
- A command an agent runs on [XTM One Desktop](runners.md) gets the value on your machine. The command history and the approval card show `[secret]`, and so does what the command prints back, wherever the value appears in it: the agent reads `[secret]` there too. If the variable is no longer shared with you by the time the machine picks the command up, the command gets the marker instead of the value. If the value changes, or stops being readable, while the command runs, what it prints back is not kept: the command is recorded as failed, since that output could hold the old value. A command that uses a secret variable is never added to the machine's allowed or blocked commands.
- When the model repeats the value in its answer, you see `[secret]`: in the chat while it streams, in the conversation afterwards (its reasoning and tool calls included), in its title, in a channel, in a notification, and in the answers of the OpenAI-compatible API, the MCP agent tools and the browser extension. A reviewer approving a tool call sees `[secret]` in its arguments too.
- An error recorded for a failed flow or background task shows `[secret]` in place of the value.

A value shorter than 8 characters is not replaced: it would turn unrelated words and numbers into markers. Keep credentials longer than that, which they are in practice. The marker belongs to the run or the conversation that wrote it: copied into another one, it is never turned back into the value. What you type in a message is never turned back into a value either: the model reads `[secret]` there. Each message of a conversation resolves the variables you may read at that moment, so once a variable is no longer shared with you, the next message no longer gets its value.

## Good habits

- Use `Secret` for credentials or sensitive values.
- Copy the reference instead of typing it from memory.
- Add clear descriptions so other users know where the variable should be used.
- Use tags when the same workspace contains many operational variables.
