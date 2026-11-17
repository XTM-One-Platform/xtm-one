# Relationships and nesting

Objects in XTM One are rarely isolated. Most of the platform works because one object references, includes, or configures another. When you look at a page, it helps to know whether you are editing the object itself, editing a child that belongs to it, or linking a reusable object that lives elsewhere.

## Linked resources vs. nested configuration

Two patterns cover most of the product:

- **Linked resources** are references between standalone objects — an agent references knowledge bases, prompts, skills, integrations, MCP servers, and tools; a group gives users access to shared objects. The referenced object keeps its own identity and life elsewhere, so one prompt, skill, MCP server, or knowledge base can serve many agents. Changing a shared resource can therefore affect more than one page.
- **Nested configuration** is stored inside a parent object rather than as a separate shared item — an assignment belongs to one agent, a persona belongs to its agent, channel behavior belongs to the agent or the shared bot, personal tool overrides belong to the user profile. Here you are editing the current object, not a library item.

Agents are where most of this comes together — they contain assignments, reference reusable resources, point to handover targets, and expose themselves through chat, channels, or MCP. That is why the agent detail page is often the best place to understand the rest of the model.

## Overrides and inheritance

Some settings inherit rather than link one-to-one: channel behavior can inherit from the channel bot, profile preferences can override platform defaults, and user-level MCP exposure can be narrower than the global setting. When a value is empty, it usually means `inherit`, not `disabled`.

## How to read a page

If the page shows a reusable entity card or picker, you are usually linking objects, and a change can affect shared behavior. If it shows inline form fields with no separate entity page, you are usually editing nested configuration that stays local.

## Next step

Continue with [`Containers and scopes`](containers-and-scopes.md), which explains how work is grouped and where organizational boundaries apply.
