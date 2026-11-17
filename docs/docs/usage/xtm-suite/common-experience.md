# Common experience across the XTM Suite

When XTM One is exposed inside another XTM Suite product, the experience is usually contextual rather than standalone.

## What this means

You usually start from the product you are already using, not from the XTM One home page.

That means:

- you stay inside the current product
- the current object or workflow provides the context
- XTM One is used to interpret, summarize, plan, or automate based on that context

## How a request usually works

The common pattern looks like this:

1. You open an object or workflow in the current product.
2. You trigger an XTM One action from that context.
3. The product sends the relevant context to XTM One.
4. XTM One routes the request to the right assistant or agent.
5. The result comes back into the product, or continues as a longer-running task.

From an end-user perspective, the important point is simple: you do not need to re-explain everything manually. The current product context is part of the request.

## How permissions work

XTM One is not meant to silently bypass the permissions of the product you are in.

In practice, this means:

- what you can trigger still depends on your current account
- what XTM One can read or do is scoped by the connected product and your access
- an AI action should not grant you broader rights than you already have

## Why an action may be hidden or disabled

An XTM One action can be unavailable for several reasons:

- the product is not connected to XTM One
- the feature is not enabled in your deployment
- no agent is bound to that use case — see [`Intents`](../../administration/intents.md)
- the relevant capability is not available for your business context
- your permissions do not allow the underlying operation

If a button or menu entry is missing, that does not automatically mean something is broken.

## What results can look like

Depending on the feature, the result may appear as:

- a summary
- extracted information
- a proposed action or draft
- a streamed answer
- a background task that finishes later

Some workflows can also include a human review step before anything sensitive is applied.

## The in-product assistant panel

When the product's version supports it, the assistant panel of OpenCTI and OpenAEV also carries what the XTM One chat offers around a conversation:

- **Your usage quota**: the same count and limit as the indicator of the XTM One chat, for the same period. It is not shown on Community Edition, where AI is off.
- **Your prompts**: the prompts you can use in the XTM One chat - your own, the ones shared with you or your groups, and the company-managed ones - newest first. A prompt set to `Hide from chat` is not offered.
- **Rating an answer**: a thumbs up or down, with an optional comment of up to 2,000 characters. It is the same rating as in the XTM One chat: rating again replaces it, removing it takes it back, the agent's managers see it in its `Feedback` tab, and it is still there when you reopen the conversation.
- **Reports, presentations and videos**: the assistant of each product writes reports, builds presentations and makes videos from your data. Each one is a card in the answer, with its title, its size (slides, sections, scenes and length) and `Open in XTM One`; where the product supports it, the card also downloads it (PDF or PowerPoint for a presentation, PDF for a report, MP4 for a finished video) and follows a video while it renders. A product on an older version shows a link to the same XTM One page.

## How to use the feature well

- Start the action from the most relevant object, not from a generic page.
- Expect the answer to be tailored to the current context.
- Review the result before using it in an operational decision.
- If the action is unavailable, check with your administrator whether the integration or routing is configured.

## Next step

Continue with [`XTM One in OpenCTI`](opencti.md), then read [`XTM One in OpenAEV`](openaev.md) if your organization uses it.
