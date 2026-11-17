# Reuse and deduplication

XTM One works best when teams reuse shared building blocks instead of recreating the same setup many times. Without a clear reuse model, you quickly end up with duplicate agents, copied prompts that drift apart, repeated tool configuration, and confusion about which version is the real one.

## Reuse first

Before creating something new, check whether the platform already has a shared agent, a reusable prompt or skill, an existing knowledge base, a configured MCP server or integration, or a catalog item you can add instead of rebuilding. In many environments, reuse is the right default.

The platform keeps one shared object when several agents point to the same prompt, skill, MCP server, or knowledge base, or when a company-managed or group-shared object is intentionally reused. It is not silently duplicated.

## When to make a copy

XTM One creates or expects a copy when you duplicate an agent, add a catalog item to your account, or export and re-import an object. Do this on purpose when you need different behavior, are testing a private variation, do not have edit rights on the shared original, or want the shared object to stay stable for everyone else.

The practical rule: edit the shared object when the whole team should benefit; duplicate it when only your version should change. This matters most for prompts, skills, and agents. Do not create a copy just because you found the shared version first.

## Avoiding duplicate setups

- Search before creating.
- Check whether a company-managed or shared version already exists.
- Use `Duplicate` only when behavior must diverge.
- Reuse prompts, skills, MCP servers, and knowledge bases whenever possible.
- Use the `AI Catalog` when you want a structured starting point rather than a manual rebuild.

XTM One also deduplicates operationally — for example, agent memory maintenance avoids storing the same fact repeatedly. From a user perspective the key idea is simple: prefer a stable shared source when one already exists.

## Next step

Continue with [`Trust, review, and confidence`](trust-and-confidence.md), which explains how to judge AI output and understand operational trust signals in XTM One.
