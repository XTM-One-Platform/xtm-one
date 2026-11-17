# Trust, review, and confidence

XTM One does not reduce trust to a single score. You judge it through ownership, run status, review steps, and source visibility — deciding whether an answer is ready to use, still needs review, where it came from, and who owns the underlying configuration.

## Ownership

Who manages an object is the first trust signal. **Personal** objects are yours — good for testing, but not a shared standard. **Group-shared** objects are collaborative team assets that can change as the group edits them. **Company-managed** objects usually represent the most stable, broadly approved standard, and are often the best starting point.

## Run status

Execution state is the second signal. `Running` means work is still in progress. `Awaiting input` means the system is waiting for a human decision, so the output is not final. `Completed` means the run finished normally, though you may still want to review the result before acting on it externally. `Failed` means the work did not complete and should not be treated as final.

Some workflows pause for a person on purpose — a draft to review before sending, a question that needs a human answer, an approval gate mid-workflow. When you see this, the platform is intentionally asking for human judgment.

## Provenance

To judge an output, look at which agent produced it, which tools or integrations were used, which knowledge base or source material was involved, and whether the configuration was shared or private. The run detail page and the surrounding object pages are the best places to inspect that context.

## Good working habits

- Treat AI output as proposed work, not automatic truth.
- Prefer company-managed or well-reviewed shared resources for important tasks.
- Review `Awaiting Input` and draft states carefully.
- Open the related run or object page when you need more context.

## Next step

Continue with [`Meaning of dates and status`](dates-and-status.md), which explains how to interpret timestamps and lifecycle signals across XTM One.
