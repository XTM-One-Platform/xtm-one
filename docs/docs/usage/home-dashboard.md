# Home dashboard

The Dashboard is the landing page of XTM One, and `Dashboard` at the top of the left navigation brings you back to it. In XTM One it is the CTEM command center: one view of your exposure across the modules connected to XTM One (OpenCTI, OpenAEV and OpenCRQ) and of the agents that work on it.

## What the Dashboard is for

Use the Dashboard to:

- see your exposure posture and each stage of your exposure management loop
- read your AI exposure briefing and its recommended actions
- check which modules are connected and healthy
- see what waits for you: notices, work items, setup steps
- follow recent agent activity and open the main work areas

## What you see first

What the Dashboard shows depends on whether a module is connected yet and on your [edition](../administration/editions-and-licensing.md):

- **No module connected yet: the welcome page.** It introduces XTM One and shows a `Connect OpenCTI` card and a `Connect OpenAEV` card, each with the steps to connect that platform. Administrators also see the `Registration token` to give the platforms. This page is mainly for the initial rollout: your administrator usually completes it.
- **Community Edition, the first time: a one-time introduction.** Once a module is connected, a Community Edition platform shows a page that presents what Enterprise Edition adds, with the connected modules. `Explore the command center` takes you to the command center, which you land on directly from then on. `Start Free Trial` and `Contact Sales` open Filigran's pages, and administrators can `Activate License`.
- **Otherwise: the command center**, described below.

When an administrator has switched AI off, the home page says so instead (see [Running without AI](../administration/running-without-ai.md)).

## Dashboard notices

The top of the Dashboard gathers what needs attention, each as a notice you can click:

- `N work items are waiting for you`: work items that wait for your decision, or items of yours that no agent owns and that are past their due date. The notice names the item waiting longest. `Review` opens that item when it is the only one; when several wait, it opens `Tasks > Work Items` on the `Waiting for me` view with the `All` scope, which lists every item the notice counts (your own, company-managed and shared ones), oldest first. Pick `Mine` there to narrow the list to your own items; the link keeps the scope, so a bookmark reopens the same list.
- Model configuration: `No default model configured` or `No embedding provider configured`, when agents or knowledge bases cannot run yet. Administrators get a `Configure` button.
- Knowledge base health: a knowledge base that needs attention, indexing stalled, re-indexing in progress or stale embeddings, with a link to the documentation or the settings.
- Platform Health (administrators): the critical findings open in [Platform Health](health.md), their count and the most recent one, with `Review`.
- Your own OAuth integrations that share your personal credentials with other people, with `Stop sharing` and `Keep sharing`.
- On Community Edition, the upgrade path to Enterprise Edition.

When nothing needs attention, no notice is shown.

## The command center

The command center opens with a greeting, a summary of your exposure posture, and the `Start chatting` button. Next to it you see when the exposure data was last synced; the refresh button next to that time fetches your data again, for you only.

The product pills under the greeting show each module: OpenCTI, OpenAEV and OpenCRQ, then the XTM Hub. A connected module shows its edition, version and health, and opens the platform when you click it. For an administrator the health dot also reads [Platform Health](health.md): red on an open critical finding, amber on an open warning, with the count when you point at it. A module that is not connected offers `Connect OpenCTI` (or `Connect OpenAEV`), which explains how to connect it; OpenCRQ shows `Soon` until an OpenCRQ platform connects.

The XTM Hub pill follows the platform's [registration with the XTM Hub](../administration/settings.md#xtm-hub-registration): a green dot while the XTM Hub confirms it, a red one when it did not at the last hourly check or when the platform must be registered again. A platform that is not registered shows the XTM Hub integrations you can use instead (a green dot while one is connected), and otherwise reads `Connect XTM Hub`: it opens the registration for an administrator and the integration form for anyone else. The pill opens your XTM Hub. See the [XTM Hub integration](resources/integrations.md#xtm-hub) for what agents can do with it.

## Exposure posture

The posture score, out of 100, sums up your exposure across the five stages of the exposure lifecycle. Click the posture ring (or the `Exposure posture` card) to see `How the Exposure Posture is computed`: a weighted average of the five stage scores, with the weight and score of each stage. A stage with no connected module is left out of the average, so it never drags the score down.

A row of cards follows:

- `Exposure posture`, with the number of stages covered;
- three cards specific to the vertical you chose, for example `Known-exploited`, `Detection coverage` and `Validation success` for Cyber. Each one opens the matching page in the module that provides it;
- [`Agents`](agents.md), with the number of active assignments;
- `Agentic executions` over the last 30 days, which opens `Operations > Executions` (see [Operations](work-history.md)).

A figure that has no data yet is shown greyed as a sample, so you see what the card will hold once the module is connected.

## Act now

`Act now` lists what to do first: the work items and tool approvals waiting for you, then the facts read from the connected modules (known-exploited exposures, failed simulation expectations, detection coverage gaps) and the setup steps left. Each row says in one line why it is there; click it to open the stage page that holds the figures behind it. `Ask AI` opens a new chat with the row's context already written in the message box, asking the assistant to guide you step by step through the fix; review it and send it yourself.

When the rows come from module-wide figures rather than from your [organization context](../administration/organization.md), the card says `These actions are generic`, and administrators get `Set up organization context` to fill it in.

When the organization context lists a software inventory, the known-exploited row counts only the known-exploited vulnerabilities that OpenCTI relates to the software of that stack (matched by CPE, else by product and vendor), and its line names the products it matched, for example `Known-exploited and affects Fortinet FortiOS in your software inventory`. This row is then tailored, so the generic notice no longer counts it. Without a software inventory, or when OpenCTI holds no matching software, the row keeps the module-wide count.

The same notice appears on the other widgets whose content does not use your organization context yet:

- `Exposure briefing` says `This briefing is generic` while no organization context was ever saved: the briefing analyst reads the sector, regions, crown jewels, software inventory and PIRs of the context to rank its findings. Set it up, then generate the briefing again.
- `Stats and coverage` says `These figures are module-wide` when its known-exploited figure counts every such vulnerability in OpenCTI because the context has no software inventory. Each figure under `Findings` opens the stage page that holds it.

Administrators get the `Set up organization context` (or `Review organization context`) action; other users are told that an administrator can set it up.

## Choose a vertical

The switcher at the top right of the Dashboard chooses the vertical the command center reads your exposure for: `Cyber` (cyber attack-surface exposure, the default), `FIMI` (foreign information manipulation and interference) or `Fraud` (digital fraud, phishing and brand abuse). The posture, the stage cards, their analysts and the briefing all change with it. Your choice is kept on your profile.

## Exposure lifecycle stages

The `Exposure lifecycle` row shows one card per stage: `Scoping`, `Discovery`, `Prioritization`, `Validation` and `Remediation`. Each card gives the stage score and its main figures, its trend, the agent that analyses it (`Owned by` its name), and, when a module that feeds it is missing, a button to connect it.

Click a card to open the stage page. It shows the stage score, the AI assessment of the stage (a headline, what it means and a `Recommended action`), all its metrics, its history and the modules that feed it. `Refresh` (or `Generate`, the first time) writes a new assessment for you and your current vertical, using only what you can see in the connected modules. The `Company Context` card invites you to describe your software inventory, crown-jewel assets and risk priorities in the Company Context knowledge base, so the analyst can judge what actually matters to you.

The sixth card, `Governance & Risk`, is reserved for OpenCRQ: until an OpenCRQ platform joins, it shows greyed sample figures.

!!! note
    AI assessments are an Enterprise Edition capability. On Community Edition, the stage scores and metrics stay available and the assessment is replaced by an explanation.

## AI exposure briefing

The `Latest exposure briefing` card is written by the `Exposure Briefing Analyst` agent: a summary of your exposure, its key findings and the focus for the week. It is generated for each user every month, from what that user can see in the connected modules, and `Generate my briefing` writes yours now. Until your first briefing exists, the card shows a sample preview.

`View full briefing` opens the Exposure briefing page: the headline and summary, the posture trend, the cross-product insight, every key finding with its recommended action, the `Next steps` and the `Recommended focus`. From there you can follow up on a step in a chat or hand it to an agent. `Refresh` writes a new briefing for you and your current vertical.

The `Recommended actions` card lists what to fix first: the steps of your briefing, and actions such as triaging known-exploited exposures, closing validation gaps or connecting a missing module, each with its own button. `Open briefing` opens the full briefing.

!!! note
    The exposure briefing is an Enterprise Edition capability. On Community Edition, the card and the page explain this and your numeric posture stays available.

## Value and top value drivers

On a Copilot deployment, the `Value` card shows the hours saved and the net value of the last 30 days: the whole organization for an administrator, your own agents and fleets otherwise. `See the value` opens the Value page.

The `Top value drivers` card beside it ranks the agents of the same report by hours saved: each row shows the agent, its share of the first agent's hours, the hours saved and its runs, and opens the agent. `All agents` opens the `By agent` tab of the Value page. A narrow card keeps the name and the hours. An agent that no longer exists is shown as `Deleted agent`, without a link. Until a unit of work is declared, the card offers `Declare a unit of work`.

An administrator sets who sees the organization's value in `Settings` > `Organization` > `Rates by role`, card `Value visibility and privacy`: `Organization totals for every user` lets every user switch the Value page to the organization's figures; `Smallest group shown apart` (5 people by default, 2 to 50) folds any fleet, agent, unit of work or week backed by fewer people into `Other` for users who are not administrators, in exports and agent tools too; `By person breakdown for administrators` turns on that breakdown, and every read of it is written to the audit log.

## Live Activity and latest results

`Live Activity` lists the latest agent runs as they happen. `All` opens `Operations > Executions`. Until a first run exists, the panel shows a greyed sample.

`Latest Results & Digests` shows the most recent digests and reports your agents produced. Click one to open its run, or `View all` to list them all in `Operations > Executions`.

## How to use the Dashboard day to day

1. Read the notices at the top and deal with what waits for you.
2. Check the posture and the stage that changed the most, and open its page if you need the details.
3. Read your briefing and pick a recommended action.
4. Use `Start chatting` to work on it with an assistant.
5. Open `Work > Operations` to see what your agents already did.

## Customize the dashboard

You can build your own dashboard: it is a grid of widgets you move, resize, remove and add back. The widgets are the posture overview, each KPI card, the AI exposure briefing, the exposure lifecycle title, each stage of the exposure lifecycle (and the Governance & Risk card), the recommended actions, the live activity, and the latest results and digests.

1. Select `Customize`, next to the vertical switcher at the top right of the dashboard. The widgets stay on screen, framed, and a bar at the top lists what you can do.
2. Drag a widget to move it; the others make room. Drag its bottom right corner to resize it.
3. Select the cross on a widget to remove it. `Add a widget` lists the widgets you removed; one you add back goes below the others.
4. Select `Done` to use the dashboard again: while you customize it, its widgets do not open anything.

Each change is saved to your profile at once, so the dashboard looks the same on your next sign-in and on any device. Each vertical (Cyber, FIMI, Fraud) keeps its own layout.

The dashboard of a Copilot deployment is built the same way, with `Customize` at its top right. Its widgets are the status header, each KPI card (agents, conversations, agentic executions, integrations, documents), the agent list, the last conversations, the live activity, the latest results and digests, the `Value` card and the `Top value drivers` card beside it. It keeps one layout. `Reset to default` puts it back to the original layout. Your layout is yours only: it does not change anyone else's dashboard.

A widget adapts to the size you give it: the smaller it is, the fewer secondary details it shows, the most important ones staying. For example, a small posture overview shows the posture score next to its title instead of the posture ring, and the connected products as logos (point at one to see its name, version and health); a small stage card keeps its score and its main figures; the recommended actions show as many actions as fit. Make a widget bigger to see everything again.

In a narrow window the widgets are shown one below the other, in the order of your layout, and cannot be rearranged until you widen the window.

## If the dashboard looks different from another user's

That is usually normal. The dashboard varies based on:

- which modules are connected, and your edition
- the vertical each person chose
- what each person can see in the connected modules: the briefing and the stage assessments are written for you, from your own access
- whether your account has access to the related areas
- the layout each person built (see [Customize the dashboard](#customize-the-dashboard))

## Next step

The next chapter explains [Chat](chat.md), which is the fastest way to start working with XTM One.
