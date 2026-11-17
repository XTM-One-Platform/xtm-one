# XTM One in the XTM Suite

This section explains how XTM One appears inside the other products of the XTM Platform: OpenCTI, OpenAEV and OpenCRQ.

XTM One is the core platform of the XTM Platform. It holds what the products share: the context of your organization, central authentication, shared integrations, and agents that work across products. OpenCTI (threat intelligence), OpenAEV (adversarial exposure validation) and OpenCRQ (governance, risk and compliance) are its modules, and each one keeps ownership of its own use case.

You do not always have to open the XTM One application directly to use XTM One capabilities. In some deployments, those capabilities are surfaced inside the product where you are already working.

## What this section is for

Use these pages when you want to understand:

- where XTM One features can appear outside the main XTM One application
- what the user experience usually looks like in another product
- why some AI actions are available, hidden, or disabled
- how OpenCTI, OpenAEV and OpenCRQ expose XTM One to end users
- which access point to use for a given task

## What stays the same across products

Even when you use XTM One from another XTM Suite product, a few principles stay the same:

- the product you are in remains the main working context
- XTM One provides the shared context and the agents behind the feature
- your current user permissions still matter
- feature availability depends on how your administrators configured the platform

## How a product knows which agent to use

Each connected product declares the AI capabilities it needs — summarize this container, generate this message, process this bundle. In XTM One, those capabilities are called intents, and an administrator binds an agent to each one. That binding decides which of your agents answers when a user clicks an AI action in OpenCTI, OpenAEV, or OpenCRQ, and whether the action appears at all.

You do not need to configure anything to use the features: XTM One ships with bindings for its built-in agents. If your organization wants its own agent behind one of these actions, see [`Intents`](../../administration/intents.md).

## What can vary by deployment

Not every organization exposes the same XTM One features in the same way.

What can vary includes:

- which actions are available
- which objects can trigger those actions
- which assistant or agent is used behind the scenes
- whether the result is immediate, streamed, or handled in the background

## Pages in this section

- [`Common experience across the XTM Suite`](common-experience.md)
- [`XTM One in OpenCTI`](opencti.md)
- [`Indicator decay re-check`](indicator-decay.md)
- [`XTM One in OpenAEV`](openaev.md)
- [`XTM One in OpenCRQ`](opencrq.md)

## Next step

Start with [`Common experience across the XTM Suite`](common-experience.md), then move to the product-specific pages that match your environment.
